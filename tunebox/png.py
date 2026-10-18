"""Just enough PNG to shrink station logos.

Logos are downloaded at whatever size their host offers (often 512 px for a
list icon of about 40), and the runtime has no image library, so tunebox
reads non-interlaced PNGs with zlib and writes a small copy. Other formats
and interlaced PNGs are served as they are."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A logo larger than this is not a logo; refuse before allocating for it.
MAX_PIXELS = 4096 * 4096
# Channels per pixel for each colour type.
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_DEPTHS = {0: (8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}


class PngError(ValueError):
    """Not a PNG this module reads."""


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    rgba: bytes  # 8-bit RGBA, rows top to bottom

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        i = (y * self.width + x) * 4
        r, g, b, a = self.rgba[i : i + 4]
        return r, g, b, a


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not data.startswith(SIGNATURE):
        raise PngError("not a PNG")
    chunks: list[tuple[bytes, bytes]] = []
    pos = len(SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) != length:
            raise PngError("truncated chunk")
        chunks.append((kind, body))
        pos += 12 + length
        if kind == b"IEND":
            break
    return chunks


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> list[bytes]:
    rows: list[bytes] = []
    prior = bytes(stride)
    pos = 0
    for _ in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prior[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prior[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                if i >= bpp:
                    line[i] = (line[i] + _paeth(line[i - bpp], prior[i], prior[i - bpp])) & 0xFF
                else:
                    line[i] = (line[i] + prior[i]) & 0xFF
        elif kind != 0:
            raise PngError(f"unknown filter {kind}")
        rows.append(bytes(line))
        prior = rows[-1]
    return rows


def read(data: bytes) -> Image:
    """Decodes a non-interlaced PNG to 8-bit RGBA."""
    chunks = _chunks(data)
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise PngError("no header")
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    if color not in _CHANNELS or depth not in _DEPTHS[color] or not width or not height:
        raise PngError(f"unsupported colour type {color} at {depth} bits")
    if interlace:
        raise PngError("interlaced")
    if width * height > MAX_PIXELS:
        raise PngError("too large")
    palette = b"".join(body for kind, body in chunks if kind == b"PLTE")
    alpha = b"".join(body for kind, body in chunks if kind == b"tRNS")
    channels = _CHANNELS[color]
    bits = channels * depth
    stride = (width * bits + 7) // 8
    try:
        raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    except zlib.error as error:
        raise PngError(f"bad image data: {error}") from None
    if len(raw) < (stride + 1) * height:
        raise PngError("image data too short")
    rows = _unfilter(raw, height, stride, max(1, bits // 8))
    out = bytearray(width * height * 4)
    o = 0
    for row in rows:
        for x in range(width):
            if depth < 8:
                bit = x * depth
                index = (row[bit >> 3] >> (8 - depth - (bit & 7))) & ((1 << depth) - 1)
                values = [index]
            else:
                step = depth // 8
                start = x * channels * step
                values = [row[start + c * step] for c in range(channels)]
                if alpha and color in (0, 2):
                    sample = row[start : start + channels * step]
                    full = [
                        int.from_bytes(sample[c * step : c * step + step]) for c in range(channels)
                    ]
                    key = list(struct.unpack(f">{channels}H", alpha[: channels * 2]))
                    values.append(0 if full == key else 255)
            if color == 3:
                index = values[0]
                if index * 3 + 3 > len(palette):
                    raise PngError("palette index out of range")
                r, g, b = palette[index * 3 : index * 3 + 3]
                a = alpha[index] if index < len(alpha) else 255
            elif color in (0, 4):
                r = g = b = values[0]
                a = values[1] if len(values) > 1 else 255
            else:
                r, g, b = values[:3]
                a = values[3] if len(values) > 3 else 255
            out[o : o + 4] = bytes((r, g, b, a))
            o += 4
    return Image(width, height, bytes(out))


def _resize(image: Image, width: int, height: int) -> Image:
    """Box filter. Colours are weighted by alpha, so a transparent pixel's
    (meaningless) colour does not bleed into its neighbours."""
    src, sw, sh = image.rgba, image.width, image.height
    out = bytearray(width * height * 4)
    o = 0
    for oy in range(height):
        y0, y1 = oy * sh // height, max(oy * sh // height + 1, (oy + 1) * sh // height)
        for ox in range(width):
            x0, x1 = ox * sw // width, max(ox * sw // width + 1, (ox + 1) * sw // width)
            r = g = b = a = 0
            for y in range(y0, y1):
                i = (y * sw + x0) * 4
                for _ in range(x1 - x0):
                    weight = src[i + 3]
                    r += src[i] * weight
                    g += src[i + 1] * weight
                    b += src[i + 2] * weight
                    a += weight
                    i += 4
            count = (y1 - y0) * (x1 - x0)
            if a:
                out[o : o + 4] = bytes((r // a, g // a, b // a, (a + count // 2) // count))
            o += 4
    return Image(width, height, bytes(out))


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def write(image: Image) -> bytes:
    """Encodes RGBA, or RGB when every pixel is opaque. Each row uses the
    filter with the smallest sum of signed differences (the usual guess at
    what compresses best)."""
    opaque = all(image.rgba[i] == 255 for i in range(3, len(image.rgba), 4))
    bpp = 3 if opaque else 4
    rows: list[bytes] = []
    for y in range(image.height):
        row = image.rgba[y * image.width * 4 : (y + 1) * image.width * 4]
        if opaque:
            row = b"".join(row[i : i + 3] for i in range(0, len(row), 4))
        rows.append(row)
    data = bytearray()
    prior = bytes(len(rows[0]))
    for row in rows:
        none = row
        sub = bytes((row[i] - (row[i - bpp] if i >= bpp else 0)) & 0xFF for i in range(len(row)))
        up = bytes((row[i] - prior[i]) & 0xFF for i in range(len(row)))
        candidates = [(0, none), (1, sub), (2, up)]
        kind, best = min(candidates, key=lambda c: sum(v if v < 128 else 256 - v for v in c[1]))
        data += bytes([kind]) + best
        prior = row
    header = struct.pack(">IIBBBBB", image.width, image.height, 8, 2 if opaque else 6, 0, 0, 0)
    return (
        SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(data), 9))
        + _chunk(b"IEND", b"")
    )


def shrink(data: bytes, size: int) -> bytes | None:
    """A copy that fits in size x size, or None when the image already
    fits. Raises PngError for what this module cannot read."""
    if len(data) >= 24 and data.startswith(SIGNATURE):
        width, height = struct.unpack(">II", data[16:24])
        if max(width, height) <= size:
            return None
    image = read(data)
    scale = size / max(image.width, image.height)
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    return write(_resize(image, width, height))
