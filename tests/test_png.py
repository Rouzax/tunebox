import struct
import tracemalloc
import zlib

import pytest

from tunebox.png import PngError, read, shrink

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def ihdr(width: int, height: int, color: int = 2, interlace: int = 0) -> bytes:
    return chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, interlace))


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def filtered(rows: list[bytes], bpp: int, kinds: list[int]) -> bytes:
    """Applies the PNG filters, the way an encoder would, so the reader's
    reverse filtering is tested against an independent implementation."""
    out = bytearray()
    prior = bytes(len(rows[0]))
    for row, kind in zip(rows, kinds, strict=True):
        line = bytearray()
        for i, x in enumerate(row):
            a = row[i - bpp] if i >= bpp else 0
            b = prior[i]
            c = prior[i - bpp] if i >= bpp else 0
            predictor = [0, a, b, (a + b) // 2, paeth(a, b, c)][kind]
            line.append((x - predictor) & 0xFF)
        out += bytes([kind]) + line
        prior = row
    return bytes(out)


def make_png(
    width: int,
    height: int,
    color_type: int,
    bit_depth: int,
    rows: list[bytes],
    bpp: int,
    filters: list[int] | None = None,
    extra: bytes = b"",
    interlace: int = 0,
) -> bytes:
    kinds = filters if filters is not None else [n % 5 for n in range(height)]
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    data = zlib.compress(filtered(rows, bpp, kinds))
    # Split IDAT in two: encoders may, and readers must join them.
    half = len(data) // 2
    return (
        SIGNATURE
        + chunk(b"IHDR", header)
        + extra
        + chunk(b"IDAT", data[:half])
        + chunk(b"IDAT", data[half:])
        + chunk(b"IEND", b"")
    )


def rgba_png(width: int, height: int, pixel: tuple[int, int, int, int]) -> bytes:
    rows = [bytes(pixel) * width for _ in range(height)]
    return make_png(width, height, 6, 8, rows, 4)


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4])
def test_every_filter_reads_back(kind: int) -> None:
    # A gradient, so every predictor has something to predict.
    rows = [
        bytes((x * 7 + y * 13 + c * 50) % 256 for x in range(9) for c in range(3)) for y in range(6)
    ]
    image = read(make_png(9, 6, 2, 8, rows, 3, filters=[kind] * 6))
    assert (image.width, image.height) == (9, 6)
    for y in range(6):
        for x in range(9):
            r, g, b = rows[y][x * 3 : x * 3 + 3]
            assert image.pixel(x, y) == (r, g, b, 255)


def test_gray_and_gray_alpha() -> None:
    gray = read(make_png(2, 1, 0, 8, [bytes([0, 200])], 1))
    assert gray.pixel(1, 0) == (200, 200, 200, 255)
    gray_alpha = read(make_png(2, 1, 4, 8, [bytes([10, 20, 30, 40])], 2))
    assert gray_alpha.pixel(1, 0) == (30, 30, 30, 40)


def test_sixteen_bit_keeps_the_high_byte() -> None:
    row = bytes([0x12, 0x34, 0xAB, 0xCD, 0xFF, 0x00, 0x80, 0x00])
    image = read(make_png(1, 1, 6, 16, [row], 8))
    assert image.pixel(0, 0) == (0x12, 0xAB, 0xFF, 0x80)


def test_palette_with_transparency_and_packed_indexes() -> None:
    palette = chunk(b"PLTE", bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9]))
    alpha = chunk(b"tRNS", bytes([255, 128]))
    # Two bits per index: 0, 1, 2, 3, then 1 in a second byte.
    row = bytes([0b00011011, 0b01000000])
    image = read(make_png(5, 1, 3, 2, [row], 1, extra=palette + alpha))
    assert [image.pixel(x, 0) for x in range(5)] == [
        (255, 0, 0, 255),
        (0, 255, 0, 128),
        (0, 0, 255, 255),
        (9, 9, 9, 255),
        (0, 255, 0, 128),
    ]


def test_a_colour_key_makes_that_colour_transparent() -> None:
    key = chunk(b"tRNS", struct.pack(">HHH", 1, 2, 3))
    image = read(make_png(2, 1, 2, 8, [bytes([1, 2, 3, 4, 5, 6])], 3, extra=key))
    assert image.pixel(0, 0) == (1, 2, 3, 0)
    assert image.pixel(1, 0) == (4, 5, 6, 255)


@pytest.mark.parametrize(
    "data",
    [
        b"not a png",
        SIGNATURE + chunk(b"IEND", b""),
        SIGNATURE + ihdr(1, 1) + chunk(b"IDAT", b"junk"),
        SIGNATURE + ihdr(2, 2) + chunk(b"IDAT", zlib.compress(b"\x00")),
        SIGNATURE + ihdr(1, 1)[:12],
    ],
)
def test_broken_images_raise(data: bytes) -> None:
    with pytest.raises(PngError):
        read(data)


def test_unsupported_forms_raise() -> None:
    interlaced = make_png(1, 1, 2, 8, [bytes(3)], 3, interlace=1)
    with pytest.raises(PngError, match="interlaced"):
        read(interlaced)
    huge = SIGNATURE + ihdr(50_000, 50_000, color=6)
    with pytest.raises(PngError, match="large"):
        read(huge)


def test_shrink_fits_the_size_and_keeps_the_shape() -> None:
    small = shrink(rgba_png(200, 100, (10, 20, 30, 255)), 64)
    assert small is not None
    image = read(small)
    assert (image.width, image.height) == (64, 32)
    assert image.pixel(10, 10) == (10, 20, 30, 255)


def test_shrink_averages_without_dark_edges() -> None:
    # Half transparent black-ish, half opaque white: a transparent pixel's
    # colour must not darken the average.
    rows = [(bytes((0, 0, 0, 0)) * 2 + bytes((255, 255, 255, 255)) * 2) for _ in range(4)]
    image = read(shrink(make_png(4, 4, 6, 8, rows, 4), 1) or b"")
    assert image.pixel(0, 0) == (255, 255, 255, 128)


def test_an_opaque_image_is_written_without_alpha() -> None:
    small = shrink(rgba_png(40, 40, (1, 2, 3, 255)), 20) or b""
    assert small[25] == 2  # colour type in IHDR: RGB


def test_a_small_enough_image_is_left_alone() -> None:
    assert shrink(rgba_png(64, 64, (0, 0, 0, 255)), 64) is None


def test_the_shrunk_logo_is_much_smaller() -> None:
    rows = [
        bytes((x * 31 + y * 17) % 256 for x in range(256) for _ in range(4)) for y in range(256)
    ]
    big = make_png(256, 256, 6, 8, rows, 4)
    small = shrink(big, 32) or b""
    assert 0 < len(small) < len(big) / 10


def test_a_short_colour_key_is_ignored() -> None:
    key = chunk(b"tRNS", b"\x00\x01")
    image = read(make_png(1, 1, 2, 8, [bytes([1, 2, 3])], 3, extra=key))
    assert image.pixel(0, 0) == (1, 2, 3, 255)


def test_image_data_is_inflated_only_as_far_as_the_image_needs() -> None:
    # 200 MB of zeros compress to about 200 kB; only the 4 bytes a 1x1 RGB
    # image needs may be inflated.
    bomb = zlib.compress(bytes(200_000_000), 9)
    data = SIGNATURE + ihdr(1, 1) + chunk(b"IDAT", bomb) + chunk(b"IEND", b"")
    tracemalloc.start()
    try:
        image = read(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert image.pixel(0, 0) == (0, 0, 0, 255)
    assert peak < 10_000_000
