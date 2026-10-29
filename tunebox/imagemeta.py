"""Removes what a camera or phone writes into an image besides the picture.

An uploaded cover is served at /covers/ to every listener, and a phone photo
carries the place it was taken, the time and the device. The runtime has no
image library to re-encode with, so the file's segments or chunks are walked
and only those needed to draw the picture are kept, byte for byte; anything
else, known or not, is dropped. A structure that cannot be walked raises
ValueError: an image tunebox cannot read is not kept."""

from __future__ import annotations

import struct

_SOI = b"\xff\xd8"
_EOI = 0xD9
_SOS = 0xDA
_APP0 = 0xE0
_APP1 = 0xE1
_APP2 = 0xE2
_APP14 = 0xEE
# Tables and frame headers a decoder needs: SOF0-SOF15 (C4 is DHT, C8 is
# reserved, CC is DAC, the arithmetic-coding table), DNL, DQT and DRI.
_DECODE = frozenset(m for m in range(0xC0, 0xD0) if m != 0xC8) | {0xDB, 0xDC, 0xDD}
_JFIF = b"JFIF\x00"
# Identifier, version, units and density; the thumbnail that may follow
# would show the picture as it was before any crop.
_JFIF_HEAD = 12
_ICC = b"ICC_PROFILE\x00"
# An Adobe segment says whether a 4-channel JPEG is CMYK or YCCK; without it
# the colours come out inverted.
_ADOBE = b"Adobe"
_EXIF = b"Exif\x00\x00"
_ORIENTATION = 0x0112
_SHORT = 3

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# What draws a still picture and its colours. Animation chunks (acTL, fcTL,
# fdAT) go too: browsers then show the default image, and a cover is a still.
_PNG_KEEP = frozenset(
    {
        b"IHDR",
        b"PLTE",
        b"IDAT",
        b"IEND",
        b"tRNS",
        b"gAMA",
        b"cHRM",
        b"sRGB",
        b"iCCP",
        b"sBIT",
        b"pHYs",
        b"cICP",
    }
)

_WEBP_KEEP = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ICCP", b"ANIM", b"ANMF"})
_VP8X_METADATA = 0x08 | 0x04  # the EXIF and XMP flags


def strip(body: bytes, kind: str) -> bytes:
    """The image without its metadata; kind is what coverfiles.sniff_cover
    answered. A JPEG keeps its rotation in an Exif segment that holds
    nothing else, since browsers turn the picture by it."""
    if kind == "jpg":
        return _jpeg(body)
    if kind == "png":
        return _png(body)
    if kind == "webp":
        return _webp(body)
    raise ValueError(f"no metadata rule for {kind}")


def _jpeg(body: bytes) -> bytes:
    if not body.startswith(_SOI):
        raise ValueError("no start of image")
    kept: list[bytes] = []
    jfif = False
    orientation: int | None = None
    pos = len(_SOI)
    while True:
        start = pos
        if pos >= len(body) or body[pos] != 0xFF:
            raise ValueError("expected a marker")
        while pos < len(body) and body[pos] == 0xFF:
            pos += 1  # fill bytes
        if pos >= len(body):
            raise ValueError("truncated marker")
        marker = body[pos]
        pos += 1
        if marker == _EOI:
            # Phones append further JPEGs after the end, each with its own
            # Exif; those go.
            kept.append(body[start:pos])
            if orientation is not None:
                kept.insert(1 if jfif else 0, _orientation_segment(orientation))
            return _SOI + b"".join(kept)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # standalone, no payload; outside a scan it means nothing
        if marker in (0x00, 0xD8):
            raise ValueError("not a marker")
        if pos + 2 > len(body):
            raise ValueError("truncated segment")
        (length,) = struct.unpack(">H", body[pos : pos + 2])
        if length < 2 or pos + length > len(body):
            raise ValueError("bad segment length")
        payload = body[pos + 2 : pos + length]
        pos += length
        if marker == _SOS:
            pos = _scan_end(body, pos)
            kept.append(body[start:pos])
        elif marker in _DECODE:
            kept.append(body[start:pos])
        elif marker == _APP0 and not jfif and not kept and payload.startswith(_JFIF):
            if len(payload) < _JFIF_HEAD + 2:
                raise ValueError("short JFIF segment")
            jfif = True
            head = payload[:_JFIF_HEAD] + b"\x00\x00"  # no thumbnail
            kept.append(b"\xff\xe0" + struct.pack(">H", len(head) + 2) + head)
        elif (marker == _APP2 and payload.startswith(_ICC)) or (
            marker == _APP14 and payload.startswith(_ADOBE)
        ):
            kept.append(body[start:pos])
        elif marker == _APP1 and orientation is None and payload.startswith(_EXIF):
            orientation = _orientation(payload[len(_EXIF) :])


def _scan_end(body: bytes, pos: int) -> int:
    """Where the entropy-coded data after a scan header ends: the next
    marker. Inside it 0xFF is followed by 0x00 (a stuffed byte) or a
    restart marker; any other byte after 0xFF starts a marker, which may
    be an APPn or COM between the scans of a progressive JPEG."""
    while True:
        at = body.find(b"\xff", pos)
        if at < 0 or at + 1 >= len(body):
            raise ValueError("no end of image")
        follower = body[at + 1]
        if follower == 0x00 or 0xD0 <= follower <= 0xD7:
            pos = at + 2
            continue
        return at


def _orientation_segment(orientation: int) -> bytes:
    tiff = b"MM\x00*\x00\x00\x00\x08" + struct.pack(
        ">HHHIHHI", 1, _ORIENTATION, _SHORT, 1, orientation, 0, 0
    )
    payload = _EXIF + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def _orientation(tiff: bytes) -> int | None:
    """The Orientation of IFD0 when it turns or mirrors the picture (2 to 8);
    anything unreadable is no orientation, never an error."""
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None:
        return None
    try:
        (magic, ifd) = struct.unpack_from(order + "HI", tiff, 2)
        if magic != 42:
            return None
        (count,) = struct.unpack_from(order + "H", tiff, ifd)
        for n in range(count):
            tag, kind, _count, value = struct.unpack_from(order + "HHIH", tiff, ifd + 2 + 12 * n)
            if tag == _ORIENTATION and kind == _SHORT:
                return value if 2 <= value <= 8 else None
    except struct.error:
        return None
    return None


def _png(body: bytes) -> bytes:
    if not body.startswith(_PNG_SIGNATURE):
        raise ValueError("no PNG signature")
    out = [_PNG_SIGNATURE]
    pos = len(_PNG_SIGNATURE)
    while True:
        if pos + 12 > len(body):
            raise ValueError("truncated chunk")
        (length,) = struct.unpack(">I", body[pos : pos + 4])
        end = pos + 12 + length
        if end > len(body):
            raise ValueError("truncated chunk")
        kind = body[pos + 4 : pos + 8]
        if kind in _PNG_KEEP:
            # Copied whole: the CRC covers type and data, so it stays valid.
            out.append(body[pos:end])
        pos = end
        if kind == b"IEND":
            return b"".join(out)


def _webp(body: bytes) -> bytes:
    if len(body) < 12 or body[:4] != b"RIFF" or body[8:12] != b"WEBP":
        raise ValueError("no WebP header")
    (size,) = struct.unpack("<I", body[4:8])
    riff_end = 8 + size
    if size < 4 or riff_end > len(body):
        raise ValueError("bad RIFF size")
    chunks: list[bytes] = []
    pos = 12
    while pos < riff_end:
        if pos + 8 > riff_end:
            raise ValueError("truncated chunk")
        fourcc = body[pos : pos + 4]
        (length,) = struct.unpack("<I", body[pos + 4 : pos + 8])
        end = pos + 8 + length + (length & 1)  # odd chunks carry a pad byte
        if end > riff_end:
            raise ValueError("truncated chunk")
        chunk = body[pos:end]
        pos = end
        if fourcc not in _WEBP_KEEP:
            continue
        if fourcc == b"VP8X":
            if length < 1:
                raise ValueError("empty VP8X chunk")
            chunk = chunk[:8] + bytes([chunk[8] & ~_VP8X_METADATA & 0xFF]) + chunk[9:]
        chunks.append(chunk)
    data = b"".join(chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(data)) + b"WEBP" + data
