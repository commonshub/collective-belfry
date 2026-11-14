"""The look of every page this service renders: name, logo, colours, fonts and footer links.

Two layers, so a belfry can be rebranded and still updated without losing the changes:

1. The base theme, shipped with the code (``themes/belfry/``): Belfry's look, the only one. No
   community's look is in the code, Open Source Village's neither.
2. The community's own files in THEME_DIR (``infra/data/theme/`` on the server, mounted
   read-only; ``belfry theme set DIR`` fills it). They are data, never in git, so ``belfry update``
   never touches them:

   - ``custom.css``: loaded after the base; sets the variables the pages use (``--accent``,
     ``--bg``, ``--font-title``, … see ``themes/belfry/theme.css``) or any other rule;
   - ``logo.svg`` (or ``.png``) and ``favicon.svg`` (or ``.png``/``.ico``);
   - any fonts or images ``custom.css`` points at (``url(my-font.woff2)``);
   - ``theme.json``: ``{"name": …, "url": …, "footer": …, "links": [["/help", "Help"], …],
     "wordmark": true, "scheme": "dark"}``.

   ``/theme/custom/own.css`` serves ``custom.css`` (or nothing) to the apps on apps.<domain>, so one
   folder restyles the pages and the apps (skills/belfry-theme).

The community's name is COMMUNITY_NAME (``belfry config``: BELFRY_NAME), its website SITE_URL.
Pages use only the variables, so a theme restyles them without a fork. Files are served at
``/theme/belfry/…`` and ``/theme/custom/…`` on every host. ``/theme/belfry/logo.svg`` and
``favicon.svg`` are drawn in the branding's two colours when it has them (``mark_svg``).
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

log = logging.getLogger(__name__)
E = html.escape

BUILTIN = Path(__file__).parent / "themes"
SERVED = {".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff2", ".woff", ".gif"}
LOGOS = ("logo.svg", "logo.png", "logo.webp")
FAVICONS = ("favicon.svg", "favicon.png", "favicon.ico")


@dataclass
class Theme:
    base: str
    name: str
    url: str
    logo: str
    logo_is_wordmark: bool
    favicon: str
    css: list[str]
    footer: str
    links: list[tuple[str, str]] = field(default_factory=list)
    scheme: str = "light dark"


def custom_dir(settings: Any) -> Path | None:
    d = getattr(settings, "theme_dir", "") or ""
    return Path(d) if d and Path(d).is_dir() else None


def _first(folder: Path | None, names: tuple[str, ...]) -> Path | None:
    if folder is None:
        return None
    return next((folder / n for n in names if (folder / n).is_file()), None)


def _version(p: Path) -> str:
    return str(int(p.stat().st_mtime))


def load(settings: Any) -> Theme:
    """The theme for these settings; never fails (a broken theme.json is logged and skipped)."""
    # One base look, Belfry's. Any other look (Open Source Village's too) is a theme folder
    # (infra/data/theme, `belfry theme set DIR`): data that no update overwrites.
    name = getattr(settings, "community_name", "") or ""
    site = (getattr(settings, "site_url", "") or "").rstrip("/")
    console = (getattr(settings, "console_url", "") or "").rstrip("/")
    public = (getattr(settings, "public_url", "") or "").rstrip("/")
    name = name or "Belfry"
    links = [(f"{public}/changelog", "Changelog")]
    if getattr(settings, "git_repo", ""):
        links.append(((site or public) + "/git", "Code"))
    if console:
        links.append((console + "/", "This belfry"))
    theme = Theme(
        base="belfry",
        name=name,
        url=site or "/",
        logo="/theme/belfry/logo.svg",
        logo_is_wordmark=False,
        favicon="/theme/belfry/favicon.svg",
        css=["/theme/belfry/theme.css", "/theme/compat.css"],
        footer="Run by Belfry" if name == "Belfry" else f"{name}, run by Belfry",
        links=links,
    )
    folder = custom_dir(settings)
    if folder is None:
        return theme
    css = folder / "custom.css"
    if css.is_file():
        theme.css.append(f"/theme/custom/custom.css?v={_version(css)}")
    logo = _first(folder, LOGOS)
    if logo:
        theme.logo = f"/theme/custom/{logo.name}?v={_version(logo)}"
        theme.logo_is_wordmark = False
    icon = _first(folder, FAVICONS)
    if icon:
        theme.favicon = f"/theme/custom/{icon.name}?v={_version(icon)}"
    meta = folder / "theme.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text())
            theme.name = str(data.get("name") or theme.name)
            theme.url = str(data.get("url") or theme.url)
            theme.footer = str(data.get("footer") or theme.footer)
            if isinstance(data.get("links"), list):
                theme.links = [(str(h), str(t)) for h, t in data["links"]]
            if data.get("wordmark") is True:
                theme.logo_is_wordmark = True
            if data.get("scheme") in ("light", "dark", "light dark"):
                theme.scheme = str(data["scheme"])
        except (ValueError, TypeError) as exc:
            log.warning("theme: %s is not usable: %s", meta, exc)
    return theme


# Set by create_app (like claim.TEST_NOTICE); claim.page reads it.
CURRENT: Theme | None = None


def current() -> Theme:
    global CURRENT
    if CURRENT is None:
        from .settings import Settings

        try:
            CURRENT = load(Settings())
        except Exception:  # noqa: BLE001 — tests without settings: the legacy look
            CURRENT = load(object())
    return CURRENT


FRAME_STYLE = """
 body { font: 16px/1.5 var(--font-sans); margin: 0; background: var(--bg); color: var(--fg); }
 main { max-width: 640px; margin: 0 auto; padding: 32px 16px 48px; }
 header.site { margin: 0 0 24px; } header.site a { display: inline-flex; align-items: center; gap: 10px; text-decoration: none; color: var(--fg); }
 header.site img { height: 28px; width: auto; display: block; }
 header.site span { font: 600 1.2rem/1 var(--font-title); letter-spacing: -0.01em; }
 .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius, 12px); padding: 24px; }
 h1, h2, h3 { font-family: var(--font-title); letter-spacing: -0.01em; }
 h1 { font-size: 1.6rem; font-weight: 600; margin: 0 0 4px; line-height: 1.2; }
 .muted { color: var(--fg-muted); } a { color: var(--accent); }
 .qr { margin: 20px 0; } .qr img { display: block; width: 280px; max-width: 100%; background: #fff; padding: 12px; border-radius: 12px; box-sizing: border-box; }
 .btn { display: inline-block; background: var(--accent); color: var(--on-accent); font-weight: 600; padding: 10px 18px; border-radius: 8px; text-decoration: none; border: 0; }
 .btn:hover { background: var(--accent-hover); }
 .ok { color: var(--ok, var(--accent)); font-weight: 600; } .bad { color: var(--accent-2); font-weight: 600; }
 code { font-family: var(--font-mono); word-break: break-all; background: var(--surface-deep); padding: 2px 6px; border-radius: 4px; }
 footer { margin-top: 24px; font-size: 0.85rem; color: var(--fg-faint); }
 footer nav { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px 14px; } footer nav a { color: var(--fg-muted); }
 .test-notice { background: var(--accent-2); color: var(--on-accent); font-weight: 600; text-align: center; padding: 8px 16px; font-size: 0.95rem; }
"""


# Pages written for the first belfry say "Open Source Village"; any other belfry shows its own name.
OSV_WORDS = (
    ("Open Source Village pass", "{name} pass"),
    ("Village pass", "membership pass"),
    ("village tokens", "community tokens"),
    ("Open Source Village", "{name}"),
)


def renamed(text: str, name: str, escape: bool = True) -> str:
    """TEXT with the first belfry's name replaced by NAME, escaped for HTML unless the caller
    escapes the whole text afterwards (the page title)."""
    for old, new in OSV_WORDS:
        text = text.replace(old, new.format(name=E(name) if escape else name))
    return text


def frame(title: str, body: str, notice: str = "", wide: bool = False, bare: bool = False) -> str:
    """The page: logo, BODY, footer. BARE leaves out the footer (the first-run setup wizard)."""
    t = current()
    title, body = renamed(title, t.name, escape=False), renamed(body, t.name)
    css = "".join(f'<link rel="stylesheet" href="{E(h)}">' for h in t.css)
    strip = f'<div class="test-notice" role="note">{E(notice)}</div>' if notice else ""
    brand = (
        f'<img src="{E(t.logo)}" alt="{E(t.name)}">'
        if t.logo_is_wordmark
        else f'<img src="{E(t.logo)}" alt=""><span>{E(t.name)}</span>'
    )
    links = "".join(f'<a href="{E(h)}">{E(text)}</a>' for h, text in t.links)
    width = "<style>main{max-width:960px}</style>" if wide else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="{t.scheme}">
<link rel="icon" href="{E(t.favicon)}">
{css}
<title>{E(title)}</title>
<style>{FRAME_STYLE}</style>{width}<script src="/static/busy.js" defer></script></head><body>{strip}<main>
<header class="site"><a href="{E(t.url)}">{brand}</a></header>
{body}
{"" if bare else f"<footer>{E(t.footer)}<nav>{links}</nav></footer>"}
</main></body></html>"""


# ── the mark in the belfry's own colours ─────────────────────────────────────────────────────
# Xavier, 2026-11-14: "the logo should make use of the two colors defined by the theme of the
# belfry when set." The Arms (themes/belfry/logo.svg: shield, tower over water) is drawn with the
# branding's colours (branding.py, kind-0 profile of the belfry key): the shield in the primary,
# the tower and the water in the secondary (white, or near black, when the secondary is missing or
# reads badly on the primary). Without a primary colour the static file is served as before.
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
LIGHT_BG, DARK_BG, WHITE, INK = "#ffffff", "#09090b", "#ffffff", "#09090b"
_SHIELD = "M8 6a2 2 0 0 1 2-2h44a2 2 0 0 1 2 2v24c0 17-11 29-24 35C19 59 8 47 8 30Z"
_TOWER = "M22 43V24h-2v-6h4v3h3v-3h4v3h2v-3h4v3h3v-3h4v6h-2v19Z"
_DOOR = "M29 43v-5a3 3 0 0 1 6 0v5Z"
_WATER = "M16 49q4-2.5 8 0t8 0t8 0t8 0"


def _luminance(color: str) -> float:
    def ch(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mark_colors(primary: str, secondary: str = "") -> tuple[str, str] | None:
    """(shield, tower) for the branding's colours, or None to keep the plain mark. Only #rrggbb
    ever reaches the SVG; the tower takes the secondary when it stands out on the shield."""
    if not HEX.match(primary or ""):
        return None
    shield = primary.lower()
    sec = (secondary or "").lower()
    if HEX.match(sec) and contrast(sec, shield) >= 2.2:
        return shield, sec
    return shield, max((WHITE, INK), key=lambda c: contrast(c, shield))


def mark_svg(primary: str, secondary: str = "", favicon: bool = False) -> str | None:
    """The Arms in these colours (None without a valid primary). A shield too close to the page's
    background (light or dark) gets a thin outline in that scheme so it never disappears."""
    got = mark_colors(primary, secondary)
    if got is None:
        return None
    shield, tower = got
    rules = []
    for query, bg, line in (
        ("not (prefers-color-scheme:dark)", LIGHT_BG, INK),
        ("(prefers-color-scheme:dark)", DARK_BG, "#fafafa"),
    ):
        if contrast(shield, bg) < 1.6:
            rules.append(f"@media {query}{{.f{{stroke:{line};stroke-width:1.5}}}}")
    style = f"<style>{''.join(rules)}</style>" if rules else ""
    if favicon:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 70">{style}'
            f'<path class="f" fill="{shield}" d="{_SHIELD}"/>'
            f'<path fill="{tower}" d="{_TOWER}"/></svg>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 70">{style}'
        f'<path class="f" fill="{shield}" d="{_SHIELD}"/>'
        f'<path fill="{tower}" d="{_TOWER}"/>'
        f'<path fill="{shield}" d="{_DOOR}"/>'
        f'<path d="{_WATER}" fill="none" stroke="{tower}" stroke-width="2.2" stroke-linecap="round"/>'
        "</svg>"
    )


def register(app: FastAPI) -> None:
    mimetypes.add_type("font/woff2", ".woff2")

    def serve(folder: Path, rel: str, max_age: int) -> FileResponse:
        target = (folder / rel).resolve()
        if (
            not target.is_relative_to(folder.resolve())
            or target.suffix.lower() not in SERVED
            or any(part.startswith(".") for part in Path(rel).parts)
            or not target.is_file()
        ):
            raise HTTPException(status_code=404)
        return FileResponse(
            target,
            headers={
                "Cache-Control": f"public, max-age={max_age}",
                "Access-Control-Allow-Origin": "*",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/theme/custom/own.css", include_in_schema=False)
    async def own_css():
        """The community's custom.css for the apps (empty without one), so they can always link it;
        next to the folder's files, so its relative url()s work."""
        folder = custom_dir(app.state.services.settings)
        css = folder / "custom.css" if folder else None
        body = css.read_bytes() if css and css.is_file() else b""
        return Response(
            body,
            media_type="text/css",
            headers={
                "Cache-Control": "public, max-age=300",
                "Access-Control-Allow-Origin": "*",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/theme/custom/{rel:path}", include_in_schema=False)
    async def custom_file(rel: str):
        folder = custom_dir(app.state.services.settings)
        if folder is None:
            raise HTTPException(status_code=404)
        return serve(folder, rel, 3600)

    async def colored_mark(request: Request, name: str, favicon: bool) -> Response:
        from starlette.concurrency import run_in_threadpool

        from . import branding

        try:
            b = await run_in_threadpool(branding.current, request.app.state.services)
        except Exception:  # noqa: BLE001 — the plain mark then
            b = {}
        svg = mark_svg(b.get("primary", ""), b.get("secondary", ""), favicon=favicon)
        if svg is None:
            return serve(BUILTIN, f"belfry/{name}", 300)
        tag = '"' + hashlib.sha256(svg.encode()).hexdigest()[:16] + '"'
        headers = {
            "Cache-Control": "public, max-age=300",
            "ETag": tag,
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=headers)
        return Response(svg, media_type="image/svg+xml", headers=headers)

    @app.get("/theme/belfry/logo.svg", include_in_schema=False)
    async def logo_svg(request: Request):
        return await colored_mark(request, "logo.svg", favicon=False)

    @app.get("/theme/belfry/favicon.svg", include_in_schema=False)
    async def favicon_svg(request: Request):
        return await colored_mark(request, "favicon.svg", favicon=True)

    @app.get("/theme/{rel:path}", include_in_schema=False)
    async def builtin_file(rel: str):
        return serve(BUILTIN, rel, 86400)
