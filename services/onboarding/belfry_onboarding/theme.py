"""The look of every page this service renders: name, logo, colours, fonts and footer links.

Two layers, so a belfry can be rebranded and still updated without losing the changes:

1. The base theme, shipped with the code (``themes/<THEME>/``): ``belfry`` (the default for new
   installs) or ``osv`` (Open Source Village, used when THEME is not set, as before themes).
2. The community's own files in THEME_DIR (``infra/data/theme/`` on the server, mounted
   read-only; ``belfry theme set DIR`` fills it). They are data, never in git, so ``belfry update``
   never touches them:

   - ``custom.css``: loaded after the base; sets the variables the pages use (``--accent``,
     ``--bg``, ``--font-title``, … see ``themes/belfry/theme.css``) or any other rule;
   - ``logo.svg`` (or ``.png``) and ``favicon.svg`` (or ``.png``/``.ico``);
   - any fonts or images ``custom.css`` points at (``url(my-font.woff2)``);
   - ``theme.json``: ``{"name": …, "url": …, "footer": …, "links": [["/help", "Help"], …]}``.

The community's name is COMMUNITY_NAME (``belfry config``: BELFRY_NAME), its website SITE_URL.
Pages use only the variables, so a theme restyles them without a fork. Files are served at
``/theme/<base>/…`` and ``/theme/custom/…`` on every host.
"""

from __future__ import annotations

import html
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)
E = html.escape

BUILTIN = Path(__file__).parent / "themes"
BASES = ("belfry", "osv")
SERVED = {".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff2", ".woff", ".gif"}
LOGOS = ("logo.svg", "logo.png", "logo.webp")
FAVICONS = ("favicon.svg", "favicon.png", "favicon.ico")
OSV_LINKS = [
    ("/help", "Help"),
    ("/feedback", "Feedback"),
    ("/rules", "Community rules"),
    ("/privacy", "Privacy"),
    ("/terms", "Terms"),
    ("/refunds", "Refunds"),
]


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
    base = (getattr(settings, "theme", "") or "osv").strip().lower()
    if base not in BASES:
        log.warning("theme: unknown THEME %r, using belfry", base)
        base = "belfry"
    name = getattr(settings, "community_name", "") or ""
    site = (getattr(settings, "site_url", "") or "").rstrip("/")
    console = (getattr(settings, "console_url", "") or "").rstrip("/")
    public = (getattr(settings, "public_url", "") or "").rstrip("/")
    if base == "osv":
        theme = Theme(
            base="osv",
            name=name or "Open Source Village",
            url="https://opensourcevillage.org",
            logo="/brand/osv-logo-horizontal-white.svg",
            logo_is_wordmark=True,
            favicon="/brand/favicon.svg",
            css=["/brand/fonts.css", "/brand/tokens.css", "/theme/osv/theme.css"],
            footer=f"{name or 'Open Source Village'}, run by Belfry",
            links=list(OSV_LINKS),
            scheme="dark",
        )
    else:
        name = "Belfry" if not name or name == "Open Source Village" else name
        links = [(f"{public}/proposals", "Proposals"), (f"{public}/changelog", "Changelog")]
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


def frame(title: str, body: str, notice: str = "", wide: bool = False, bare: bool = False) -> str:
    """The page: logo, BODY, footer. BARE leaves out the footer (the first-run setup wizard)."""
    t = current()
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
<style>{FRAME_STYLE}</style>{width}</head><body>{strip}<main>
<header class="site"><a href="{E(t.url)}">{brand}</a></header>
{body}
{"" if bare else f"<footer>{E(t.footer)}<nav>{links}</nav></footer>"}
</main></body></html>"""


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

    @app.get("/theme/custom/{rel:path}", include_in_schema=False)
    async def custom_file(rel: str):
        folder = custom_dir(app.state.services.settings)
        if folder is None:
            raise HTTPException(status_code=404)
        return serve(folder, rel, 3600)

    @app.get("/theme/{rel:path}", include_in_schema=False)
    async def builtin_file(rel: str):
        return serve(BUILTIN, rel, 86400)
