"""Default avatars: the pattern, the SVG and PNG routes."""

from __future__ import annotations

import struct
import zlib

from fastapi.testclient import TestClient

from belfry_onboarding import avatar
from belfry_onboarding.app import create_app

KEY = "a3f1" + "07" + "00" * 29  # bits 0xa3f1, tint index 7 % 5 = 2 (orange)


def test_pattern_is_mirrored_and_matches_the_browser_version():
    cells, tint = avatar.pattern(KEY)
    assert tint == "#f89b0e"
    # the same vector is checked in services/village/test/avatar.test.js
    assert cells == [
        (0, 0),
        (0, 4),
        (1, 0),
        (1, 1),
        (1, 2),
        (1, 3),
        (1, 4),
        (2, 3),
        (4, 0),
        (4, 4),
        (3, 0),
        (3, 1),
        (3, 2),
        (3, 3),
        (3, 4),
    ]
    for x, y in cells:
        assert (4 - x, y) in cells or x == 2


def test_png_is_a_valid_image():
    data = avatar.png(KEY, size=64)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (64, 64)
    idat = data[data.index(b"IDAT") + 4 : data.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert len(raw) == 64 * (1 + 64 * 3)
    # pixel (0, 0) is the background, the first cell (offset 12 px) is the tint
    assert raw[1:4] == bytes.fromhex("1a231d")
    row = 12 * (1 + 64 * 3)
    assert raw[row + 1 + 12 * 3 : row + 1 + 13 * 3] == bytes.fromhex("f89b0e")


def test_routes(services):
    with TestClient(create_app(services)) as c:
        r = c.get(f"/avatar/{KEY}.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert "immutable" in r.headers["cache-control"]
        assert r.headers["access-control-allow-origin"] == "*"
        r = c.get(f"/avatar/{KEY.upper()}.svg")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
        assert 'fill="#f89b0e"' in r.text
        assert c.get("/avatar/abc.png").status_code == 404
        assert c.get(f"/avatar/{KEY}.gif").status_code == 404
