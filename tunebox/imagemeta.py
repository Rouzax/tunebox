"""Removes what a camera or phone writes into an image besides the picture.

An uploaded cover is served at /covers/ to every listener, and a phone photo
carries the place it was taken, the time and the device. The runtime has no
image library to re-encode with, so whole metadata segments and chunks are
dropped and everything else is copied byte for byte. A structure that cannot
be walked raises ValueError: an image tunebox cannot read is not kept."""

from __future__ import annotations

import struct

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"
_SOS = 0xDA
_APP0 = 0xE0
_APP1 = 0xE1
_APP2 = 0xE2
_APP14 = 0xEE
_APP15 = 0xEF
_COM = 0xFE
_ICC = b"ICC_PROFILE\x00"
# An Adobe segment says whether a 4-channel JPEG is CMYK or YCCK; without it
# the colours come out inverted.
_ADOBE = b"Adobe"
_EXIF = b"Exif\x00\x00"
_ORIENTATION = 0x0112
_SHORT = 3

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_DROP = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"})

_WEBP_DROP = frozenset({b"EXIF", b"XMP "})
_VP8X_METADATA = 0x08 | 0x04  # the EXIF and XMP flags


def strip(body: bytes, kind: str) -> bytes:
    """The image without its metadata; kind is what coverfiles.sniff_cover
    answered. A JPEG keeps its rotation in a minimal Exif segment, since
    browsers turn the picture by it."""
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
    kept: list[tuple[int, bytes]] = []
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
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            kept.append((marker, body[start:pos]))
            continue
        if marker in (0x00, 0xD8, 0xD9):
            raise ValueError("no scan")
        if pos + 2 > len(body):
            raise ValueError("truncated segment")
        (length,) = struct.unpack(">H", body[pos : pos + 2])
        if length < 2 or pos + length > len(body):
            raise ValueError("bad segment length")
        payload = body[pos + 2 : pos + length]
        pos += length
        if marker == _SOS:
            # Inside entropy-coded data 0xFF is always followed by 0x00 or a
            # restart or scan marker, so the first FFD9 is the end of the
            # image. Phones append further JPEGs after it, each with its own
            # Exif; those go.
            end = body.find(_EOI, pos)
            scan = body[start:] if end < 0 else body[start : end + len(_EOI)]
            return _SOI + b"".join(_with_orientation(kept, orientation)) + scan
        if marker == _APP1 and orientation is None and payload.startswith(_EXIF):
            orientation = _orientation(payload[len(_EXIF) :])
        if _keep(marker, payload):
            kept.append((marker, body[start:pos]))


def _keep(marker: int, payload: bytes) -> bool:
    if marker == _APP0:
        return True
    if marker == _APP2:
        return payload.startswith(_ICC)
    if marker == _APP14:
        return payload.startswith(_ADOBE)
    return not (_APP1 <= marker <= _APP15 or marker == _COM)


def _with_orientation(kept: list[tuple[int, bytes]], orientation: int | None) -> list[bytes]:
    segments = [segment for _marker, segment in kept]
    if orientation is None:
        return segments
    at = 0
    while at < len(kept) and kept[at][0] == _APP0:
        at += 1
    tiff = b"MM\x00*\x00\x00\x00\x08" + struct.pack(
        ">HHHIHHI", 1, _ORIENTATION, _SHORT, 1, orientation, 0, 0
    )
    payload = _EXIF + tiff
    segments.insert(at, b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload)
    return segments


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
        if kind not in _PNG_DROP:
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
        if fourcc in _WEBP_DROP:
            continue
        if fourcc == b"VP8X":
            if length < 1:
                raise ValueError("empty VP8X chunk")
            chunk = chunk[:8] + bytes([chunk[8] & ~_VP8X_METADATA & 0xFF]) + chunk[9:]
        chunks.append(chunk)
    data = b"".join(chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(data)) + b"WEBP" + data
