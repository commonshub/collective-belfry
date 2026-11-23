"""A batch of printed cards as a PDF (docs/cards.md §5), drawn here with fpdf2: what "Generate
PDF" on ``belfry.<domain>/cards`` answers.

The cards are the canvas boards' (cards/print-sheet, cards/print-sheet-channels): the same
elements at the same sizes relative to the card, in Inter. The boards give sizes in CSS pixels
for an A6 card (397 × 561.5 upright, 561.5 × 397 on its side); here they are scaled to the cell.

A browser's own printing could not be trusted with the page (Safari ignores ``@page``), so the
page is ours:

- A4 pages, upright or on their side as the cards need (``layout``); the PDF page itself is in
  that orientation.
- Nothing is drawn within ``MARGIN`` (8 mm) of a paper edge: a printer's own margins cut nothing.
- 1, 2 or 4 equal cells a page, each as large as the printable area allows at the card's own
  proportions; the block of cells is centred on the page.
- Dashed cut lines between cells, from one printable edge to the other; none at the paper's edge.
- The QR code is drawn as vector squares, with four modules of white kept clear around it.

Everything drawn goes through ``Sheet``, which keeps the box of each thing it draws: the tests
measure the margins from that.
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import segno
from fpdf import FPDF

log = logging.getLogger(__name__)

FONTS = Path(__file__).parent / "fonts"
A4 = (210.0, 297.0)
MARGIN = 8.0  # mm from every paper edge: the printable area
PER_PAGE = (1, 2, 4)
# the board's cards, in its own pixels
GROUP = (397.0, 561.5)
TOPIC = (561.5, 397.0)
INK = (0, 0, 0)
GREY = (0x3A, 0x3A, 0x3C)
FINE = (0x6C, 0x6C, 0x70)
RULE = (0xC7, 0xC7, 0xCC)
CUT = (0xB0, 0xB0, 0xB5)
QUIET = 4  # modules of white around the QR code
# Inter's own metrics (hhea), as parts of the font size
ASCENT, DESCENT = 0.96875, 0.2412

KIND_LINE = {
    "group": ("Group", " · people you get to know. Join and say hi."),
    "topic": ("Topic", " · come and go, share what you know."),
}
SCAN = {"group": "Scan to join this group", "topic": "Scan to contribute to this topic"}
NO_ACCOUNT = "No account required"


def layout(kind: str, per_page: int) -> dict[str, Any]:
    """Where PER_PAGE cards of KIND go on an A4: the page's size (mm), columns and rows, the
    size of a cell (the card, as large as the printable area allows at its own proportions),
    where the block starts, and how many mm one of the board's pixels is.

    Upright cards (groups): four or one on an upright page, two side by side on a page on its
    side. Cards on their side (topics): four or one on a page on its side, two one above the
    other on an upright page."""
    upright = kind != "topic"
    card = GROUP if upright else TOPIC
    if per_page == 4:
        cols, rows = 2, 2
        page = A4 if upright else A4[::-1]
    elif per_page == 2:
        cols, rows = (2, 1) if upright else (1, 2)
        page = A4[::-1] if upright else A4
    else:
        cols, rows = 1, 1
        page = A4 if upright else A4[::-1]
    area = (page[0] - 2 * MARGIN, page[1] - 2 * MARGIN)
    scale = min(area[0] / cols / card[0], area[1] / rows / card[1])
    cell = (card[0] * scale, card[1] * scale)
    block = (cell[0] * cols, cell[1] * rows)
    return {
        "page": page,
        "side": "portrait" if page[0] < page[1] else "landscape",
        "cols": cols,
        "rows": rows,
        "cell": cell,
        "block": block,
        "x0": (page[0] - block[0]) / 2,
        "y0": (page[1] - block[1]) / 2,
        "scale": scale,
    }


@dataclass
class Sheet:
    """The PDF being drawn, and the box (x0, y0, x1, y1, what) of everything drawn on each page."""

    pdf: FPDF
    boxes: list[list[tuple[float, float, float, float, str]]] = field(default_factory=list)
    logo: tuple[bytes, float] = (b"", 1.0)
    name: str = ""

    def page(self, size: tuple[float, float]) -> None:
        self.pdf.add_page(format=size)
        self.boxes.append([])

    def _note(self, x0: float, y0: float, x1: float, y1: float, what: str) -> None:
        self.boxes[-1].append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), what))

    def text(
        self,
        x: float,
        baseline: float,
        text: str,
        style: str,
        size_mm: float,
        color: tuple[int, int, int] = INK,
    ) -> float:
        """TEXT with its baseline at BASELINE; returns its width."""
        pdf = self.pdf
        family = "InterSB" if style == "SB" else "Inter"
        pdf.set_font(family, "B" if style == "B" else "", size_mm * 72 / 25.4)
        pdf.set_char_spacing(-0.01 * size_mm * 72 / 25.4)  # the board's letter-spacing
        pdf.set_text_color(*color)
        width = pdf.get_string_width(text)
        pdf.text(x, baseline, text)
        self._note(x, baseline - ASCENT * size_mm, x + width, baseline + DESCENT * size_mm, "text")
        return width

    def width(self, text: str, style: str, size_mm: float) -> float:
        pdf = self.pdf
        pdf.set_font("InterSB" if style == "SB" else "Inter", "B" if style == "B" else "")
        pdf.set_font_size(size_mm * 72 / 25.4)
        pdf.set_char_spacing(-0.01 * size_mm * 72 / 25.4)
        return pdf.get_string_width(text)

    def box(self, x: float, y: float, w: float, h: float, border: float, radius: float) -> None:
        """A rounded box whose border lies inside (x, y, w, h), as a CSS border does."""
        pdf = self.pdf
        pdf.set_draw_color(*INK)
        pdf.set_line_width(border)
        pdf.set_dash_pattern()
        half = border / 2
        pdf.rect(
            x + half,
            y + half,
            w - border,
            h - border,
            style="D",
            round_corners=True,
            corner_radius=max(radius - half, 0),
        )
        self._note(x, y, x + w, y + h, "box")

    def rule(self, x0: float, x1: float, y: float, width: float) -> None:
        pdf = self.pdf
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(width)
        pdf.set_dash_pattern()
        pdf.line(x0, y, x1, y)
        self._note(x0, y - width / 2, x1, y + width / 2, "rule")

    def cut(self, x0: float, y0: float, x1: float, y1: float) -> None:
        pdf = self.pdf
        pdf.set_draw_color(*CUT)
        pdf.set_line_width(0.2)
        pdf.set_dash_pattern(dash=1.2, gap=1.2)
        pdf.line(x0, y0, x1, y1)
        pdf.set_dash_pattern()
        across = 0.1  # half the line's width, on the sides it does not run along
        if x0 == x1:
            self._note(x0 - across, y0, x1 + across, y1, "cut")
        else:
            self._note(x0, y0 - across, x1, y1 + across, "cut")

    def qr(self, x: float, y: float, size: float, code: Any) -> float:
        """The QR code's modules as black squares in (x, y, size, size); returns one module's
        side. Runs of dark modules are one rectangle each (no hairlines between them)."""
        pdf = self.pdf
        rows = code.matrix
        n = len(rows)
        m = size / n
        pdf.set_fill_color(*INK)
        for r, row in enumerate(rows):
            c = 0
            while c < n:
                if not row[c]:
                    c += 1
                    continue
                start = c
                while c < n and row[c]:
                    c += 1
                pdf.rect(x + start * m, y + r * m, (c - start) * m, m, style="F")
        self._note(x, y, x + size, y + size, f"qr:{n}")
        return m

    def image(self, data: bytes, x: float, y: float, w: float, h: float) -> None:
        self.pdf.image(io.BytesIO(data), x=x, y=y, w=w, h=h)
        self._note(x, y, x + w, y + h, "logo")


# ── the logo ────────────────────────────────────────────────────────────────────────────────
_logo_cache: dict[str, tuple[float, tuple[bytes, float] | None]] = {}
LOGO_SECONDS = 600
LOGO_BYTES = 3_000_000


def arms(shield: str = "#000000", tower: str = "#ffffff") -> bytes:
    """The Arms (the base theme's mark) as a plain SVG: explicit colours, no style sheet."""
    from . import theme

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 70">'
        f'<path fill="{shield}" d="{theme._SHIELD}"/><path fill="{tower}" d="{theme._TOWER}"/>'
        f'<path fill="{shield}" d="{theme._DOOR}"/>'
        f'<path d="{theme._WATER}" fill="none" stroke="{tower}" stroke-width="2.2" '
        'stroke-linecap="round"/></svg>'
    ).encode()


def _aspect(data: bytes) -> float | None:
    """Width over height of an image fpdf2 can place (a raster picture, or an SVG it can read);
    None for anything else."""
    head = data[:400].lstrip().lower()
    try:
        if head.startswith((b"<?xml", b"<svg")) or b"<svg" in head:
            from fpdf.svg import SVGObject

            svg = SVGObject(data)
            box = svg.viewbox or [0, 0, float(svg.width or 0), float(svg.height or 0)]
            trial = FPDF()
            trial.add_page()
            trial.image(io.BytesIO(data), x=10, y=10, w=10)  # it must also draw
            return float(box[2]) / float(box[3]) if box[2] and box[3] else None
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            return img.width / img.height if img.width and img.height else None
    except Exception as exc:  # noqa: BLE001 — the Arms then
        log.info("cards: a logo the PDF can't use: %s", type(exc).__name__)
        return None


def _fetch(url: str) -> bytes | None:
    import httpx

    try:
        with httpx.Client(timeout=4.0, follow_redirects=False) as client:
            r = client.get(url)
        if r.status_code != 200 or len(r.content) > LOGO_BYTES:
            return None
        return r.content
    except Exception as exc:  # noqa: BLE001
        log.info("cards: the logo was not fetched: %s", type(exc).__name__)
        return None


def logo(svc: Any) -> tuple[bytes, float]:
    """(image, width over height) of the belfry's logo for the cards: the logo an admin
    uploaded (branding) when it can be fetched and drawn, else the theme folder's own file,
    else the Arms in the belfry's colours (black on a belfry that chose none)."""
    from . import branding, theme

    found: tuple[bytes, float] | None = None
    try:
        look = branding.cached()
    except Exception:  # noqa: BLE001
        look = {}
    url = str(look.get("logo") or "")
    if url.startswith("https://"):
        held = _logo_cache.get(url)
        if held and time.monotonic() - held[0] < LOGO_SECONDS:
            found = held[1]
        else:
            data = _fetch(url)
            ratio = _aspect(data) if data else None
            found = (data, ratio) if data and ratio else None
            _logo_cache[url] = (time.monotonic(), found)
    if found is None:
        try:
            folder = theme.custom_dir(svc.settings)
            path = theme._first(folder, theme.LOGOS) if folder else None
            if path:
                data = path.read_bytes()
                ratio = _aspect(data)
                found = (data, ratio) if ratio else None
        except Exception as exc:  # noqa: BLE001
            log.info("cards: the theme's logo was not read: %s", type(exc).__name__)
    if found is None:
        colors = theme.mark_colors(str(look.get("primary") or ""), str(look.get("secondary") or ""))
        found = (arms(*colors) if colors else arms(), 64 / 70)
    return found


# ── a card ──────────────────────────────────────────────────────────────────────────────────
def _baseline(top: float, line: float, size: float) -> float:
    """Where the baseline of text of SIZE falls in a CSS line box of height LINE starting at TOP."""
    return top + (line - (ASCENT + DESCENT) * size) / 2 + ASCENT * size


def _qr_size(n: int, box: float, gap_fixed: float, gap_slack: float) -> float:
    """The side of an N-module QR code in its BOX-sized place such that QUIET modules stay
    clear: GAP_FIXED is the white beside it that does not grow when it shrinks, GAP_SLACK the
    white on a side where shrinking it adds room."""
    size = min(box, gap_fixed * n / QUIET)
    return min(size, (gap_slack + box) * n / (n + QUIET))


def _brand(
    sh: Sheet, x: float, y: float, k: float, logo_h: float, size: float, room: float
) -> None:
    """The logo and the community's name on one line, in ROOM pixels: a long name is set
    smaller (down to 70 %), then cut with an ellipsis; it never leaves the card."""
    data, ratio = sh.logo
    w = min(logo_h * ratio, logo_h * 3)
    h = w / ratio
    sh.image(data, x, y + (logo_h - h) / 2 * k, w * k, h * k)
    name, left, least = sh.name, (room - w - 8) * k, 0.7 * size
    while size > least and sh.width(name, "B", size * k) > left:
        size -= 0.5
    while len(name) > 1 and sh.width(name, "B", size * k) > left:
        name = name[:-2].rstrip() + "…"
    sh.text(x + (w + 8) * k, y + _baseline(0, logo_h, size) * k, name, "B", size * k)


def _kind(sh: Sheet, x: float, y: float, k: float, kind: str) -> None:
    bold, rest = KIND_LINE[kind]
    base = y + _baseline(0, 19, 14) * k
    w = sh.text(x, base, bold, "B", 14 * k)
    sh.text(x + w, base, rest, "", 14 * k, GREY)


def draw_group(sh: Sheet, x: float, y: float, k: float, code: Any) -> None:
    """The board's group card (397 × 561.5 px) with its top left at (x, y), K mm to the pixel."""
    w, h = GROUP
    left, inner = 32.0, w - 64.0
    _brand(sh, x + left * k, y + 30 * k, k, 25, 16, inner)
    _kind(sh, x + left * k, y + 61 * k, k, "group")
    low_top = h - 26 - 112
    for label, top, bottom, rules in (
        ("Group name:", 90.0, 208.0, 2),
        ("Description:", 220.0, low_top - 16, 4),
    ):
        sh.box(x + left * k, y + top * k, inner * k, (bottom - top) * k, 2 * k, 14 * k)
        sh.text(x + (left + 16) * k, y + _baseline(top + 14, 22, 20) * k, label, "B", 20 * k)
        line = top + 14 + 22 + 10 + 22
        for _ in range(rules):
            if line < bottom - 8:
                sh.rule(x + (left + 16) * k, x + (left + inner - 16) * k, y + line * k, k)
            line += 35
    n = len(code.matrix)
    size = _qr_size(n, 112, 14, 16)  # 14 px to the words beside it, 16 px to the box above
    qy = h - 26 - size
    sh.qr(x + left * k, y + qy * k, size * k, code)
    base = y + (h - 26 - 22 + _baseline(0, 22, 16)) * k
    sh.text(x + (left + 112 + 14) * k, base, SCAN["group"], "SB", 16 * k)


def _wrap(sh: Sheet, text: str, style: str, size_mm: float, width: float) -> list[str]:
    lines, now = [], ""
    for word in text.split():
        trial = f"{now} {word}".strip()
        if now and sh.width(trial, style, size_mm) > width:
            lines.append(now)
            now = word
        else:
            now = trial
    return lines + [now] if now else lines


def draw_topic(sh: Sheet, x: float, y: float, k: float, code: Any) -> None:
    """The board's topic card (561.5 × 397 px), on its side."""
    w, h = TOPIC
    left, right_w, gap = 28.0, 150.0, 20.0
    inner = w - 2 * left - gap - right_w
    _brand(sh, x + left * k, y + 24 * k, k, 23, 15, inner)
    _kind(sh, x + left * k, y + 53 * k, k, "topic")
    # the hashtag: "#" and a line to write on
    top = 82.0
    sh.box(x + left * k, y + top * k, inner * k, 62 * k, 2 * k, 14 * k)
    hash_w = sh.text(x + (left + 16) * k, y + _baseline(top + 12, 34, 34) * k, "#", "B", 34 * k)
    sh.rule(
        x + (left + 16 + 6) * k + hash_w, x + (left + inner - 16) * k, y + (top + 12 + 28) * k, k
    )
    # about: three lines
    top, bottom = 154.0, h - 24
    sh.box(x + left * k, y + top * k, inner * k, (bottom - top) * k, 2 * k, 14 * k)
    sh.text(x + (left + 16) * k, y + _baseline(top + 12, 22, 18) * k, "About:", "B", 18 * k)
    line = top + 12 + 22 + 4 + 28
    for _ in range(3):
        sh.rule(x + (left + 16) * k, x + (left + inner - 16) * k, y + line * k, k)
        line += 29
    # the right column, from the bottom up: the small print, the words, the QR code
    rx = left + inner + gap
    fine_top = bottom - 22
    sh.text(x + rx * k, y + _baseline(fine_top, 22, 12) * k, NO_ACCOUNT, "", 12 * k, FINE)
    words = _wrap(sh, SCAN["topic"], "SB", 16 * k, right_w * k)
    words_top = fine_top - 8 - 21 * len(words)
    for i, text in enumerate(words):
        sh.text(x + rx * k, y + _baseline(words_top + 21 * i, 21, 16) * k, text, "SB", 16 * k)
    n = len(code.matrix)
    size = _qr_size(n, right_w, gap, 8)  # 20 px to the boxes beside it, 8 px to the words below
    sh.qr(x + rx * k, y + (words_top - 8 - right_w) * k, size * k, code)


# ── the batch ───────────────────────────────────────────────────────────────────────────────
def render(
    svc: Any, kind: str, per_page: int, urls: list[str], community: str, title: str = ""
) -> tuple[bytes, Sheet, dict[str, Any]]:
    """The PDF of cards for URLS (one card each): its bytes, the sheet with every box drawn, and
    the layout used."""
    kind = "topic" if kind == "topic" else "group"
    per_page = per_page if per_page in PER_PAGE else 4
    lay = layout(kind, per_page)
    pdf = FPDF(unit="mm", format=lay["page"])
    pdf.set_auto_page_break(False)
    pdf.set_margin(0)
    pdf.set_compression(True)
    pdf.add_font("Inter", "", str(FONTS / "Inter-Regular.ttf"))
    pdf.add_font("Inter", "B", str(FONTS / "Inter-Bold.ttf"))
    pdf.add_font("InterSB", "", str(FONTS / "Inter-SemiBold.ttf"))
    pdf.set_title(title or "Printed cards")
    pdf.set_creator("Belfry")
    sh = Sheet(pdf, logo=logo(svc), name=" ".join(str(community or "").split())[:60])
    draw = draw_topic if kind == "topic" else draw_group
    k, (cw, ch) = lay["scale"], lay["cell"]
    pw, ph = lay["page"]
    for i, url in enumerate(urls):
        slot = i % per_page
        if slot == 0:
            sh.page(lay["page"])
            # cut lines between the cells, from one printable edge to the other
            if lay["cols"] == 2:
                sh.cut(pw / 2, MARGIN, pw / 2, ph - MARGIN)
            if lay["rows"] == 2:
                sh.cut(MARGIN, ph / 2, pw - MARGIN, ph / 2)
        col, row = slot % lay["cols"], slot // lay["cols"]
        code = segno.make(url, error="q", micro=False)
        draw(sh, lay["x0"] + col * cw, lay["y0"] + row * ch, k, code)
    return bytes(pdf.output()), sh, lay


def filename(batch: str) -> str:
    """ "<batch name>.pdf", with nothing a header or a file system could trip over."""
    name = re.sub(r"[^A-Za-z0-9 ._-]+", "", batch or "").strip(" .")[:60]
    return (re.sub(r"\s+", " ", name) or "cards") + ".pdf"


__all__ = ["render", "layout", "filename", "logo", "MARGIN", "Sheet"]
