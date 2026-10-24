"""Renders the home-screen icons of the community apps into brand/apps/ (run from the repo root:
``python3 brand/make-app-icons.py``; needs Pillow). Each icon is the app's symbol in the lime
accent with the Open Source Village mark cut into it, on the dark green of tokens.css, so the
installed apps read as one family and still differ at a glance.

Outputs per app: ``<app>-512.png`` and ``<app>-192.png`` (rounded square on transparent, purpose
"any"), ``<app>-maskable-512.png`` (full bleed, symbol inside the 80 % safe zone) and
``<app>-180.png`` (apple-touch-icon, full bleed: iOS rounds the corners itself)."""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

BRAND = Path(__file__).resolve().parent
OUT = BRAND / "apps"
BG = (0x2B, 0x3A, 0x31, 255)
ACCENT = (0xD6, 0xFF, 0x52, 255)
S = 1024  # drawing size; downscaled at the end


def osv_mark(size: int) -> Image.Image:
    """The white mark from the manifest icon as an alpha mask, cropped and scaled to ``size``."""
    src = Image.open(BRAND / "web-app-manifest-512x512.png").convert("RGBA")
    # transparent corners would read as white: flatten onto black first
    flat = Image.alpha_composite(Image.new("RGBA", src.size, (0, 0, 0, 255)), src)
    lum = flat.convert("L").point(lambda v: 255 if v > 160 else 0)
    box = lum.getbbox()
    mark = lum.crop(box)
    w, h = mark.size
    scale = size / max(w, h)
    return mark.resize((round(w * scale), round(h * scale)), Image.LANCZOS)


def chat_symbol(d: ImageDraw.ImageDraw, c: float, r: float):
    """A speech bubble centred on (c, c) with radius-ish r; returns the mark's centre."""
    left, top, right, bottom = c - r, c - r * 0.82, c + r, c + r * 0.62
    d.rounded_rectangle((left, top, right, bottom), radius=r * 0.38, fill=ACCENT)
    d.polygon(
        [(c - r * 0.55, bottom - 2), (c - r * 0.62, bottom + r * 0.42), (c - r * 0.12, bottom - 2)],
        fill=ACCENT,
    )
    return c, (top + bottom) / 2, (bottom - top) * 0.62


def calendar_symbol(d: ImageDraw.ImageDraw, c: float, r: float):
    """A calendar page with its binding rings; returns the mark's centre and size."""
    left, top, right, bottom = c - r * 0.9, c - r * 0.72, c + r * 0.9, c + r * 0.9
    d.rounded_rectangle((left, top, right, bottom), radius=r * 0.22, fill=ACCENT)
    band = top + r * 0.42
    d.rectangle((left, band, right, band + r * 0.07), fill=BG)
    for x in (c - r * 0.45, c + r * 0.45):
        d.rounded_rectangle(
            (x - r * 0.09, top - r * 0.22, x + r * 0.09, top + r * 0.2), radius=r * 0.09, fill=BG
        )
        d.rounded_rectangle(
            (x - r * 0.05, top - r * 0.18, x + r * 0.05, top + r * 0.16), radius=r * 0.05, fill=ACCENT
        )
    return c, (band + bottom) / 2 + r * 0.03, (bottom - band) * 0.66


def vote_symbol(d: ImageDraw.ImageDraw, c: float, r: float):
    """A ticked ballot dropping into the slot of a ballot box; returns the mark's centre and size."""
    top = c - r * 0.15
    d.rounded_rectangle((c - r * 0.95, top, c + r * 0.95, c + r * 0.9), radius=r * 0.18, fill=ACCENT)
    # the slot, then the ballot standing in it, tilted by a few pixels' worth of offset
    d.rounded_rectangle(
        (c - r * 0.62, top + r * 0.05, c + r * 0.62, top + r * 0.2), radius=r * 0.07, fill=BG
    )
    bl, br, bt = c - r * 0.42, c + r * 0.42, c - r * 1.0
    d.rounded_rectangle((bl, bt, br, top + r * 0.12), radius=r * 0.08, fill=ACCENT)
    d.rectangle((bl, top + r * 0.04, br, top + r * 0.12), fill=ACCENT)
    w = r * 0.1
    d.line(
        [(c - r * 0.2, bt + r * 0.5), (c - r * 0.04, bt + r * 0.66), (c + r * 0.24, bt + r * 0.32)],
        fill=BG,
        width=round(w),
        joint="curve",
    )
    body_top = top + r * 0.2
    return c, (body_top + c + r * 0.9) / 2, (c + r * 0.9 - body_top) * 0.66


def wallet_symbol(d: ImageDraw.ImageDraw, c: float, r: float):
    """A village token: a coin with a rim; returns the mark's centre and size."""
    d.ellipse((c - r * 0.95, c - r * 0.95, c + r * 0.95, c + r * 0.95), fill=ACCENT)
    w = r * 0.08
    d.ellipse((c - r * 0.8, c - r * 0.8, c + r * 0.8, c + r * 0.8), outline=BG, width=round(w))
    return c, c, r * 0.95


def people_symbol(d: ImageDraw.ImageDraw, c: float, r: float):
    """Two people, the one in front larger; returns the mark's centre and size (on the front body)."""
    # behind, to the right
    d.ellipse((c + r * 0.12, c - r * 0.9, c + r * 0.62, c - r * 0.4), fill=ACCENT)
    d.rounded_rectangle((c - r * 0.05, c - r * 0.3, c + r * 0.95, c + r * 0.55), radius=r * 0.4, fill=ACCENT)
    # in front, with a gap of background around it
    gap = r * 0.07
    d.ellipse((c - r * 0.78 - gap, c - r * 0.68 - gap, c - r * 0.12 + gap, c - r * 0.02 + gap), fill=BG)
    d.rounded_rectangle((c - r * 1.0 - gap, c + r * 0.06 - gap, c + r * 0.22 + gap, c + r * 0.95 + gap), radius=r * 0.45, fill=BG)
    d.ellipse((c - r * 0.78, c - r * 0.68, c - r * 0.12, c - r * 0.02), fill=ACCENT)
    d.rounded_rectangle((c - r * 1.0, c + r * 0.06, c + r * 0.22, c + r * 0.95), radius=r * 0.42, fill=ACCENT)
    return c - r * 0.39, c + r * 0.52, r * 0.62


def market_symbol(d: ImageDraw.ImageDraw, c: float, r: float):
    """A market basket with its handle; returns the mark's centre and size."""
    w = r * 0.14
    d.arc((c - r * 0.62, c - r * 1.0, c + r * 0.62, c + r * 0.2), start=180, end=360, fill=ACCENT, width=round(w))
    top, bottom = c - r * 0.35, c + r * 0.9
    d.polygon([(c - r * 1.0, top), (c + r * 1.0, top), (c + r * 0.72, bottom), (c - r * 0.72, bottom)], fill=ACCENT)
    d.rectangle((c - r * 1.0, top - r * 0.02, c + r * 1.0, top + r * 0.18), fill=ACCENT)
    return c, (top + bottom) / 2 + r * 0.06, (bottom - top) * 0.62


def symbol_layer(draw_symbol, r: float) -> Image.Image:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cx, cy, size = draw_symbol(ImageDraw.Draw(layer), S / 2, r)
    mark = osv_mark(round(size))
    hole = Image.new("L", (S, S), 0)
    hole.paste(mark, (round(cx - mark.width / 2), round(cy - mark.height / 2)))
    # the mark is cut out of the symbol, so the background shows through it
    alpha = ImageChops.subtract(layer.getchannel("A"), hole)
    layer.putalpha(alpha)
    return layer


def render(name: str, draw_symbol):
    # "any": rounded square, symbol at 31 % radius
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(base).rounded_rectangle((0, 0, S - 1, S - 1), radius=S * 0.22, fill=BG)
    any_icon = Image.alpha_composite(base, symbol_layer(draw_symbol, S * 0.31))
    # maskable and apple: full bleed, symbol inside the safe zone (radius 40 % of the size)
    full = Image.new("RGBA", (S, S), BG)
    masked = Image.alpha_composite(full, symbol_layer(draw_symbol, S * 0.26))
    for size in (512, 192):
        any_icon.resize((size, size), Image.LANCZOS).save(OUT / f"{name}-{size}.png", optimize=True)
    masked.resize((512, 512), Image.LANCZOS).save(OUT / f"{name}-maskable-512.png", optimize=True)
    masked.convert("RGB").resize((180, 180), Image.LANCZOS).save(OUT / f"{name}-180.png", optimize=True)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    render("chat", chat_symbol)
    render("calendar", calendar_symbol)
    render("vote", vote_symbol)
    render("wallet", wallet_symbol)
    render("people", people_symbol)
    render("market", market_symbol)
