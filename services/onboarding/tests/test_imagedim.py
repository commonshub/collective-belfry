import struct
import zlib

from belfry_onboarding import imagedim


def _png(w: int, h: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )


def _jpeg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
    dht = b"\xff\xc4" + struct.pack(">H", 4) + b"\x00\x00"
    sof = b"\xff\xc2" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + dht + sof + b"\xff\xd9"


def test_a_pictures_size_is_read_from_its_first_bytes():
    assert imagedim.size(_png(640, 480)) == (640, 480)
    assert imagedim.size(b"GIF89a" + struct.pack("<HH", 12, 7) + bytes(20)) == (12, 7)
    # the frame header is found after other segments, and a Huffman table is not taken for it
    assert imagedim.size(_jpeg(4032, 3024)) == (4032, 3024)
    vp8x = (
        b"RIFF"
        + bytes(4)
        + b"WEBPVP8X"
        + bytes(8)
        + (799).to_bytes(3, "little")
        + (599).to_bytes(3, "little")
    )
    assert imagedim.size(vp8x) == (800, 600)


def test_what_is_not_a_readable_picture_has_no_size():
    assert imagedim.size(b"") is None
    assert imagedim.size(b"%PDF-1.7 not a picture") is None
    assert imagedim.size(b"\xff\xd8\xff") is None  # a JPEG cut short
    assert imagedim.size(_png(0, 10)) is None
    assert imagedim.size(b"\x89PNG\r\n\x1a\n" + bytes(4)) is None


def test_an_untyped_picture_is_known_by_its_first_bytes():
    assert imagedim.kind(_png(1, 1)) == "image/png"
    assert imagedim.kind(_jpeg(1, 1)) == "image/jpeg"
    assert imagedim.kind(b"GIF89a") == "image/gif"
    assert imagedim.kind(bytes(4) + b"ftypheic") == "image/heic"
    assert imagedim.kind(b"%PDF-1.7") == ""
