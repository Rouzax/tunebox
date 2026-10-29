import struct
import zlib

import pytest

from tunebox import png
from tunebox.imagemeta import strip

GPS = b"GPS 52.0907 N 5.1214 E"
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
APP0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
DQT = b"\xff\xdb\x00\x05\x00\x01\x02"
SOF0 = b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
SOS = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
# Entropy-coded data: a stuffed 0xFF00 and a restart marker, never FFD9.
SCAN = b"\x12\xff\x00\x34\xff\xd0\x56"


def segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def exif(orientation: int | None, order: str = ">") -> bytes:
    """An Exif APP1 payload: IFD0 with a make, maybe an Orientation, and the
    GPS text as the make's value."""
    entries = [(0x010F, 2, len(GPS), 8 + 2 + 12 * 2 + 4)]
    if orientation is not None:
        entries.append((0x0112, 3, 1, orientation << (16 if order == ">" else 0)))
    head = (b"MM" if order == ">" else b"II") + struct.pack(order + "HI", 42, 8)
    ifd = struct.pack(order + "H", len(entries))
    for tag, kind, count, value in entries:
        ifd += struct.pack(order + "HHII", tag, kind, count, value)
    return b"Exif\x00\x00" + head + ifd + struct.pack(order + "I", 0) + GPS


def phone_jpeg(orientation: int | None = 6, order: str = ">") -> bytes:
    icc = segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + b"p" * 20)
    return (
        SOI
        + APP0
        + segment(0xE1, exif(orientation, order))
        + segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>" + GPS)
        + segment(0xFE, b"comment " + GPS)
        + icc
        + segment(0xE2, b"MPF\x00" + b"m" * 16)
        + segment(0xEE, b"Adobe" + b"\x00" * 7)
        + segment(0xED, b"Photoshop 3.0\x00" + GPS)
        + segment(0xE0, b"JFXX\x00\x10" + GPS)
        + segment(0xF3, b"JPG3 " + GPS)
        + segment(0xE5, b"vendor " + GPS)
        + DQT
        + SOF0
        + SOS
        + SCAN
        + EOI
        + SOI
        + segment(0xE1, exif(1))
        + EOI
    )


@pytest.mark.parametrize("order", [">", "<"])
def test_a_phone_jpeg_loses_its_metadata_and_keeps_its_rotation(order: str) -> None:
    out = strip(phone_jpeg(6, order), "jpg")
    assert GPS not in out
    assert b"xmpmeta" not in out and b"comment" not in out and b"MPF" not in out
    assert b"Photoshop" not in out and b"JFXX" not in out and b"JPG3" not in out
    assert b"vendor" not in out
    oriented = segment(
        0xE1,
        b"Exif\x00\x00MM\x00*\x00\x00\x00\x08"
        + b"\x00\x01\x01\x12\x00\x03\x00\x00\x00\x01\x00\x06\x00\x00\x00\x00\x00\x00",
    )
    icc = segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + b"p" * 20)
    adobe = segment(0xEE, b"Adobe" + b"\x00" * 7)
    assert out == SOI + APP0 + oriented + icc + adobe + DQT + SOF0 + SOS + SCAN + EOI


def test_a_jpeg_without_a_rotation_gets_no_exif() -> None:
    out = strip(phone_jpeg(None), "jpg")
    assert b"\xff\xe1" not in out and b"Exif" not in out
    assert out.endswith(SOS + SCAN + EOI)


@pytest.mark.parametrize("orientation", [1, 9])
def test_an_upright_or_unknown_orientation_is_no_rotation(orientation: int) -> None:
    assert b"Exif" not in strip(phone_jpeg(orientation), "jpg")


@pytest.mark.parametrize(
    "payload",
    [
        b"Exif\x00\x00XX",  # no byte order
        b"Exif\x00\x00MM\x00\x2b\x00\x00\x00\x08",  # not TIFF
        b"Exif\x00\x00MM\x00*\x00\x00\xff\xff",  # IFD past the end
        b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x01\x01\x12",  # entry cut short
        b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x01\x01\x12\x00\x04"
        + b"\x00\x00\x00\x01\x00\x00\x00\x06",  # Orientation that is no SHORT
        b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x00",  # no entries
    ],
)
def test_an_unreadable_exif_means_no_rotation(payload: bytes) -> None:
    body = SOI + segment(0xE1, payload) + SOF0 + SOS + SCAN + EOI
    assert strip(body, "jpg") == SOI + SOF0 + SOS + SCAN + EOI


def test_the_rotation_goes_after_the_jfif_segment_only() -> None:
    body = SOI + segment(0xE1, exif(3)) + DQT + SOF0 + SOS + SCAN + EOI
    out = strip(body, "jpg")
    assert out.startswith(SOI + b"\xff\xe1") and out.endswith(DQT + SOF0 + SOS + SCAN + EOI)


def test_a_clean_jpeg_comes_back_unchanged() -> None:
    # Fill bytes before a marker are copied as they are.
    body = SOI + APP0 + b"\xff\xff\xdb\x00\x05\x00\x01\x02" + SOF0 + SOS + SCAN + EOI
    assert strip(body, "jpg") == body


def test_a_standalone_marker_outside_a_scan_goes() -> None:
    body = SOI + APP0 + b"\xff\x01" + DQT + b"\xff\xd3" + SOF0 + SOS + SCAN + EOI
    assert strip(body, "jpg") == SOI + APP0 + DQT + SOF0 + SOS + SCAN + EOI


def test_the_jfif_thumbnail_goes_and_only_a_leading_jfif_stays() -> None:
    thumbnail = segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x01\x01" + b"\x7f" * 3)
    body = SOI + thumbnail + segment(0xE0, APP0[4:]) + DQT + SOF0 + SOS + SCAN + EOI
    assert strip(body, "jpg") == SOI + APP0 + DQT + SOF0 + SOS + SCAN + EOI
    late = SOI + DQT + APP0 + SOF0 + SOS + SCAN + EOI
    assert strip(late, "jpg") == SOI + DQT + SOF0 + SOS + SCAN + EOI


def test_segments_between_the_scans_of_a_progressive_jpeg_are_walked() -> None:
    dht = b"\xff\xc4\x00\x04\x00\x00"
    body = (
        SOI
        + APP0
        + DQT
        + SOF0
        + SOS
        + SCAN
        + segment(0xFE, b"comment " + GPS)
        + segment(0xE1, exif(6))
        + dht
        + b"\xff\xff"  # fill before the next scan
        + SOS
        + SCAN
        + EOI
    )
    out = strip(body, "jpg")
    assert GPS not in out and b"comment" not in out and b"Exif" in out
    assert out.endswith(SOF0 + SOS + SCAN + dht + b"\xff\xff" + SOS + SCAN + EOI)


def test_a_scan_without_an_end_is_refused() -> None:
    with pytest.raises(ValueError):
        strip(SOI + APP0 + SOS + SCAN, "jpg")
    with pytest.raises(ValueError):
        strip(SOI + APP0 + SOS + SCAN + b"\xff", "jpg")


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xd9",  # no SOI
        SOI,  # nothing after it
        SOI + b"\x00\xe0",  # no marker
        SOI + b"\xff\xff",  # only fill bytes
        SOI + b"\xff\xe0\x00",  # length cut short
        SOI + b"\xff\xe0\x00\x01",  # length below 2
        SOI + b"\xff\xe1\x00\x40Exif",  # segment past the end
        SOI + b"\xff\xd8",  # a second SOI
        SOI + segment(0xE0, b"JFIF\x00\x01"),  # JFIF cut short
    ],
)
def test_a_jpeg_that_cannot_be_walked_is_refused(body: bytes) -> None:
    with pytest.raises(ValueError):
        strip(body, "jpg")


@pytest.mark.parametrize(
    "body",
    [
        SOI + EOI,
        SOI + APP0 + DQT + SOS + SCAN + EOI,  # no frame header
        SOI + APP0 + DQT + SOF0 + EOI,  # no scan
        SOI + APP0 + DQT + SOS + SCAN + SOF0 + SOS + SCAN + EOI,  # a scan before the frame
    ],
    ids=["empty", "no-frame", "no-scan", "scan-first"],
)
def test_a_jpeg_without_a_picture_is_refused(body: bytes) -> None:
    with pytest.raises(ValueError):
        strip(body, "jpg")


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def small_png(*extra: bytes) -> tuple[bytes, list[bytes]]:
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    iend = chunk(b"IEND", b"")
    kept = [ihdr, chunk(b"gAMA", b"\x00\x00\xb1\x8f"), idat, iend]
    return png.SIGNATURE + kept[0] + kept[1] + b"".join(extra) + kept[2] + kept[3], kept


def test_a_png_loses_its_text_and_time_chunks() -> None:
    body, kept = small_png(
        chunk(b"tEXt", b"Location\x00" + GPS),
        chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(GPS)),
        chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + GPS),
        chunk(b"eXIf", exif(6)[6:]),
        chunk(b"tIME", b"\x07\xea\x0a\x1b\x0c\x00\x00"),
        chunk(b"prVt", GPS),
        chunk(b"acTL", b"\x00\x00\x00\x01\x00\x00\x00\x00"),
    )
    out = strip(body + b"trailing " + GPS, "png")
    assert out == png.SIGNATURE + b"".join(kept)
    assert png.read(out).pixel(0, 0) == (255, 0, 0, 255)


def test_a_clean_png_comes_back_unchanged() -> None:
    body, _kept = small_png()
    assert strip(body, "png") == body


@pytest.mark.parametrize(
    "body",
    [
        b"\x89PNX\r\n\x1a\n",
        png.SIGNATURE + b"\x00\x00\x00\x0dIHDR",  # header without data and CRC
        png.SIGNATURE + chunk(b"IHDR", b"\x00" * 13)[:-1],  # CRC cut short
        png.SIGNATURE + chunk(b"IHDR", b"\x00" * 13),  # no IEND
    ],
)
def test_a_png_that_cannot_be_walked_is_refused(body: bytes) -> None:
    with pytest.raises(ValueError):
        strip(body, "png")


def test_a_png_without_a_picture_is_refused() -> None:
    _body, (ihdr, gama, idat, iend) = small_png()
    short = chunk(b"IHDR", b"\x00" * 12)
    for chunks in (
        [iend],
        [ihdr, iend],  # no image data
        [gama, ihdr, idat, iend],  # header not first
        [short, idat, iend],  # header of the wrong length
        [ihdr, ihdr, idat, iend],  # a second header
    ):
        with pytest.raises(ValueError):
            strip(png.SIGNATURE + b"".join(chunks), "png")


def riff(*chunks: bytes) -> bytes:
    data = b"".join(chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(data)) + b"WEBP" + data


def webp_chunk(fourcc: bytes, data: bytes) -> bytes:
    pad = b"\x00" if len(data) % 2 else b""
    return fourcc + struct.pack("<I", len(data)) + data + pad


def test_a_webp_loses_its_exif_and_xmp_chunks() -> None:
    flags = 0x20 | 0x10 | 0x08 | 0x04  # ICC, alpha, EXIF, XMP
    vp8x = webp_chunk(b"VP8X", bytes([flags]) + b"\x00" * 9)
    iccp = webp_chunk(b"ICCP", b"icc")
    vp8l = webp_chunk(b"VP8L", b"\x2f\x00\x00\x00\x00")
    body = riff(
        vp8x,
        iccp,
        vp8l,
        webp_chunk(b"EXIF", exif(6)[6:]),
        webp_chunk(b"XMP ", b"<x>" + GPS),
        webp_chunk(b"NOTE", GPS),
    )
    out = strip(body + b"trailing" + GPS, "webp")
    assert GPS not in out
    cleared = webp_chunk(b"VP8X", bytes([0x20 | 0x10]) + b"\x00" * 9)
    assert out == riff(cleared, iccp, vp8l)
    assert struct.unpack("<I", out[4:8])[0] == len(out) - 8


def test_a_clean_webp_comes_back_unchanged() -> None:
    body = riff(webp_chunk(b"VP8 ", b"\x00" * 11))
    assert strip(body, "webp") == body


@pytest.mark.parametrize(
    "body",
    [
        b"RIFF\x04\x00\x00\x00WEBX",
        b"RIFF\xff\x00\x00\x00WEBP",  # RIFF size past the end
        b"RIFF\x00\x00\x00\x00WEBP",  # RIFF size below the form type
        riff(b"VP8 \x00"),  # chunk header cut short
        riff(b"VP8 \x20\x00\x00\x00abc"),  # chunk past the RIFF size
        riff(webp_chunk(b"VP8X", b"")),  # VP8X without its flags
        riff(webp_chunk(b"VP8X", b"\x02" + b"\x00" * 9)),  # the animation flag
        # Metadata hidden in an animation frame.
        riff(webp_chunk(b"ANIM", b"\x00" * 6), webp_chunk(b"ANMF", b"\x00" * 16 + b"EXIF" + GPS)),
    ],
)
def test_a_webp_that_cannot_be_walked_is_refused(body: bytes) -> None:
    with pytest.raises(ValueError):
        strip(body, "webp")


@pytest.mark.parametrize(
    "body",
    [
        riff(),
        riff(webp_chunk(b"VP8X", b"\x10" + b"\x00" * 9)),
        riff(webp_chunk(b"VP8X", b"\x10" + b"\x00" * 9), webp_chunk(b"ALPH", b"\x00")),
        riff(webp_chunk(b"ICCP", b"icc"), webp_chunk(b"EXIF", exif(6)[6:])),
    ],
    ids=["empty", "vp8x-only", "alpha-only", "metadata-only"],
)
def test_a_webp_without_a_picture_is_refused(body: bytes) -> None:
    with pytest.raises(ValueError):
        strip(body, "webp")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        strip(b"GIF89a", "gif")
