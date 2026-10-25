"""Default avatars: ``GET /avatar/<hex pubkey>.png`` and ``.svg``.

A participant without a picture gets a mirrored 5×5 pixel pattern in one of five village tints on
the card colour, drawn from the public key (the layout of minidenticons, MIT). The starter
profile published at activation (core.starter_events) names the PNG as its ``picture``, so
clients outside the village (Amethyst, Damus, Primal) show it too; the village apps draw the same
picture in the browser (services/village/web/shared/avatar.js), keep the two in step.

The picture depends on the key alone, so responses are cached for a year. The PNG is written
here with zlib (no imaging library in the image).
"""

from __future__ import annotations

import re
import struct
import zlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

BACKGROUND = "#1a231d"
TINTS = ["#d6ff52", "#a8d672", "#f89b0e", "#7fc4a0", "#e8f5b0"]
PNG_SIZE = 256  # 8 units of 32 px: the 5×5 pattern and 1.5 units of margin on each side
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CACHE = "public, max-age=31536000, immutable"


def pattern(pubkey: str) -> tuple[list[tuple[int, int]], str]:
    """The cells (x, y), both 0–4, and the tint for a 64-hex public key."""
    key = pubkey.lower() if HEX64.match(pubkey.lower()) else "0" * 64
    bits = int(key[0:4], 16)
    tint = TINTS[int(key[4:6], 16) % len(TINTS)]
    cells = [(7 - i // 5 if i > 14 else i // 5, i % 5) for i in range(25) if bits & (1 << (i % 15))]
    return cells, tint


def svg(pubkey: str) -> str:
    cells, tint = pattern(pubkey)
    rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1"/>' for x, y in cells)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1.5 -1.5 8 8" '
        'shape-rendering="crispEdges">'
        f'<rect x="-1.5" y="-1.5" width="8" height="8" fill="{BACKGROUND}"/>'
        f'<g fill="{tint}">{rects}</g></svg>'
    )


def _rgb(colour: str) -> bytes:
    return bytes.fromhex(colour.removeprefix("#"))


def png(pubkey: str, size: int = PNG_SIZE) -> bytes:
    cells, tint = pattern(pubkey)
    unit = size // 8
    offset = unit * 3 // 2
    on = set(cells)
    bg, fg = _rgb(BACKGROUND), _rgb(tint)
    rows = []
    for py in range(size):
        cy = (py - offset) // unit if py >= offset else -1
        row = bytearray(b"\x00")  # filter type 0
        for px in range(size):
            cx = (px - offset) // unit if px >= offset else -1
            row += fg if (cx, cy) in on else bg
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def avatar_url(public_url: str, pubkey: str) -> str:
    return f"{public_url.rstrip('/')}/avatar/{pubkey.lower()}.png"


_png_cache: dict[str, bytes] = {}


def register(app: FastAPI) -> None:
    """Mount /avatar/<pubkey>.png|.svg (create_app calls this)."""

    @app.get("/avatar/{name}", include_in_schema=False)
    async def default_avatar(name: str) -> Response:
        key, _, ext = name.lower().partition(".")
        if not HEX64.match(key) or ext not in ("png", "svg"):
            raise HTTPException(status_code=404, detail="no such avatar")
        headers = {"Cache-Control": CACHE, "Access-Control-Allow-Origin": "*"}
        if ext == "svg":
            return Response(svg(key), media_type="image/svg+xml", headers=headers)
        if key not in _png_cache:
            if len(_png_cache) > 2000:
                _png_cache.clear()
            _png_cache[key] = png(key)
        return Response(_png_cache[key], media_type="image/png", headers=headers)
