"""The width and height of a picture from its first bytes, without an image library.

For the ``dim`` entry of a NIP-92 ``imeta`` tag (``dim <width>x<height>``): with it an app keeps
the picture's place while it loads. PNG, GIF, JPEG and WebP; anything else, or bytes that do not
read, give None. Never raises.
"""

from __future__ import annotations

import struct

MAX = 65_535


def _jpeg(b: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            return None
        marker = b[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
            i += 2
            continue
        size = struct.unpack(">H", b[i + 2 : i + 4])[0]
        # the start-of-frame markers, but not DHT (C4), JPG (C8) and DAC (CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", b[i + 5 : i + 9])
            return w, h
        if size < 2:
            return None
        i += 2 + size
    return None


def _webp(b: bytes) -> tuple[int, int] | None:
    kind = b[12:16]
    if kind == b"VP8X" and len(b) >= 30:
        w = int.from_bytes(b[24:27], "little") + 1
        h = int.from_bytes(b[27:30], "little") + 1
        return w, h
    if kind == b"VP8 " and len(b) >= 30 and b[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", b[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L" and len(b) >= 25 and b[20] == 0x2F:
        n = int.from_bytes(b[21:25], "little")
        return (n & 0x3FFF) + 1, ((n >> 14) & 0x3FFF) + 1
    return None


def size(body: bytes) -> tuple[int, int] | None:
    """(width, height) of the picture in BODY, or None."""
    try:
        b = bytes(body[:262_144])
        found: tuple[int, int] | None = None
        if b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR" and len(b) >= 24:
            found = struct.unpack(">II", b[16:24])
        elif b[:6] in (b"GIF87a", b"GIF89a") and len(b) >= 10:
            found = struct.unpack("<HH", b[6:10])
        elif b[:2] == b"\xff\xd8":
            found = _jpeg(b)
        elif b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            found = _webp(b)
        if found and 0 < found[0] <= MAX and 0 < found[1] <= MAX:
            return int(found[0]), int(found[1])
    except Exception:  # noqa: BLE001
        return None
    return None


def kind(body: bytes) -> str:
    """The MIME type of the picture in BODY by its first bytes ("" when it is none of ours): a
    picture mailed as application/octet-stream is still shown as one."""
    b = bytes(body[:16])
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[4:8] == b"ftyp" and b[8:12] in (b"heic", b"heix", b"mif1", b"heif", b"hevc"):
        return "image/heic"
    return ""
