"""The text pages of pass.<domain>: /privacy, /terms, /refunds, /rules and /help.

The texts are Markdown files in ``content/`` (edit them there, not here). They are rendered by a
small renderer for the subset they use: headings, paragraphs, lists, tables, block quotes, bold,
inline code and links. ``[TO FILL: …]`` marks facts the organisers still have to give and is
highlighted. ``{{name}}`` inserts a value from the deployment (see ``_values``); ``{{app_list}}``
is the community apps from ``COMMUNITY_APPS`` (Compose passes the same list as the app bar).
"""

from __future__ import annotations

import html
import os
import re
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from . import claim

CONTENT_DIR = Path(os.environ.get("CONTENT_DIR") or Path(__file__).resolve().parents[1] / "content")
PAGES = {
    "privacy": "Privacy notice",
    "terms": "Terms of participation",
    "refunds": "Refund policy",
    "rules": "Community rules",
    "help": "Help",
}

# The skill for an agent helping a visitor (skills/ in the repo, SKILLS_DIR in Compose), served at
# /skill.md with the deployment's domain in place of <domain>.
SKILL = "open-source-village/SKILL.md"

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_FILL = re.compile(r"\[TO FILL(?::[^\]]*)?\]")
_VAR = re.compile(r"\{\{(\w+)\}\}")
_SAFE_URL = re.compile(r"^(https?://|mailto:|/|#)")
_ITEM = re.compile(r"^(- |\d+\. )")
_BLOCK_START = re.compile(r"^(#|\||> |- |\d+\. )")


def _inline(text: str, values: dict[str, str]) -> str:
    """Escape, then apply the inline syntax. Values come from the deployment, not from users."""
    text = _VAR.sub(lambda m: values.get(m.group(1), ""), text)
    out = html.escape(text, quote=False)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _FILL.sub(lambda m: f'<mark class="fill">{m.group(0)}</mark>', out)

    def link(m: re.Match[str]) -> str:
        url = html.unescape(m.group(2))
        if not _SAFE_URL.match(url):
            return m.group(0)
        return f'<a href="{html.escape(url)}">{m.group(1)}</a>'

    out = _LINK.sub(link, out)
    return _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)


def _table(rows: list[str], values: dict[str, str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head, body = cells[0], cells[2:]
    th = "".join(f"<th>{_inline(c, values)}</th>" for c in head)
    trs = "".join(
        "<tr>" + "".join(f"<td>{_inline(c, values)}</td>" for c in row) + "</tr>" for row in body
    )
    table = f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"
    return f'<div class="table">{table}</div>'


def render(markdown: str, values: dict[str, str] | None = None) -> tuple[str, str]:
    """(title, HTML body) of a Markdown text; the title is its first ``# `` heading."""
    values = values or {}
    title = ""
    blocks: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if m := re.match(r"^(#{1,3}) (.*)$", line):
            level = len(m.group(1))
            if level == 1 and not title:
                title = m.group(2).strip()
            blocks.append(f"<h{level}>{_inline(m.group(2).strip(), values)}</h{level}>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows, values))
        elif line.startswith("> "):
            text = []
            while i < len(lines) and lines[i].startswith(">"):
                text.append(lines[i][1:].strip())
                i += 1
            blocks.append(f'<p class="note">{_inline(" ".join(text), values)}</p>')
        elif _ITEM.match(line):
            ordered = not line.startswith("- ")
            items: list[list[str]] = []
            while i < len(lines) and (
                _ITEM.match(lines[i]) or (lines[i].startswith("  ") and items)
            ):
                if _ITEM.match(lines[i]):
                    items.append([_ITEM.sub("", lines[i])])
                else:
                    items[-1].append(lines[i].strip())
                i += 1
            tag = "ol" if ordered else "ul"
            lis = "".join(f"<li>{_inline(' '.join(it), values)}</li>" for it in items)
            blocks.append(f"<{tag}>{lis}</{tag}>")
        elif m := _VAR.fullmatch(line.strip()):
            # a value on its own line is a block (e.g. {{app_list}}), inserted as built
            blocks.append(values.get(f"{m.group(1)}__html", ""))
            i += 1
        else:
            text = []
            while i < len(lines) and lines[i].strip() and not _BLOCK_START.match(lines[i]):
                text.append(lines[i].strip())
                i += 1
            blocks.append(f"<p>{_inline(' '.join(text), values)}</p>")
    return title, "\n".join(blocks)


def community_apps(raw: str) -> list[tuple[str, str, str]]:
    """``Name|URL|Description;…`` (Compose's community app list) as tuples; http(s) URLs only."""
    out = []
    for entry in (raw or "").split(";"):
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) >= 2 and parts[0] and re.match(r"^https?://", parts[1]):
            out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return out


def _values(settings: Any) -> dict[str, str]:
    public = (getattr(settings, "public_url", "") or "").rstrip("/")
    # the list includes this help page itself (for the app bar); leave it out here
    listed = community_apps(os.environ.get("COMMUNITY_APPS", ""))
    apps = [a for a in listed if not a[1].endswith("/help")]
    app_list = "".join(
        f'<li><a href="{html.escape(url)}"><strong>{html.escape(name)}</strong></a>'
        f"{' · ' + html.escape(text) if text else ''}</li>"
        for name, url, text in apps
    )
    return {
        "account": getattr(settings, "apps_public_url", "") or "/",
        "pass_page": f"{public}/me" if public else "/me",
        "app_list__html": f'<ul class="apps">{app_list}</ul>' if app_list else "",
    }


@cache
def _source(name: str) -> str:
    return (CONTENT_DIR / f"{name}.md").read_text(encoding="utf-8")


PAGE_STYLE = """<style>
 .prose h1 { margin-bottom: 16px; }
 .prose h2 { font-size: 1.15rem; margin: 28px 0 8px; }
 .prose h3 { font-size: 1rem; margin: 20px 0 6px; }
 .prose p, .prose li { color: var(--osv-foreground); }
 .prose ul, .prose ol { padding-left: 1.3em; }
 .prose li { margin: 4px 0; }
 .prose .note { border-left: 3px solid var(--osv-accent-2); padding: 8px 12px;
   background: var(--osv-surface-deep); border-radius: 6px; }
 .prose mark.fill { background: #f89b0e33; color: #f8c06e; padding: 0 4px; border-radius: 4px; }
 .prose .table { overflow-x: auto; margin: 12px 0; }
 .prose table { border-collapse: collapse; width: 100%; font-size: 0.95rem; }
 .prose th, .prose td { text-align: left; vertical-align: top; padding: 8px 10px;
   border-bottom: 1px solid var(--osv-border); }
 .prose th { color: var(--osv-foreground-muted); font-weight: 600; }
 .prose .apps { list-style: none; padding: 0; }
 .prose .apps li { margin: 8px 0; }
</style>"""


def page_html(name: str, settings: Any) -> str:
    title, body = render(_source(name), _values(settings))
    return claim.page(title or PAGES[name], f'<div class="card prose">{body}</div>{PAGE_STYLE}')


def site_domain(settings: Any) -> str:
    """The domain of the deployment: SITE_URL's host, else PUBLIC_URL's without ``pass.``; empty
    when neither names one (a local run on localhost ports)."""
    site = urlparse(getattr(settings, "site_url", "") or "").hostname
    if site:
        return site
    host = urlparse(getattr(settings, "public_url", "") or "").hostname or ""
    return host.removeprefix("pass.") if host.startswith("pass.") else ""


def skill_text(settings: Any) -> str:
    # a local checkout finds the folder itself; in the image it is mounted (SKILLS_DIR)
    env = os.environ.get("SKILLS_DIR")
    skills_dir = Path(env) if env else Path(__file__).resolve().parents[3] / "skills"
    text = (skills_dir / SKILL).read_text(encoding="utf-8")
    domain = site_domain(settings)
    return text.replace("<domain>", domain) if domain else text


def register(app: FastAPI) -> None:
    for name in PAGES:

        def handler(request: Request, _name: str = name) -> HTMLResponse:
            return HTMLResponse(page_html(_name, request.app.state.services.settings))

        app.add_api_route(
            f"/{name}", handler, methods=["GET"], response_class=HTMLResponse, name=f"page_{name}"
        )

    @app.get("/skill.md", include_in_schema=False)
    def skill(request: Request) -> Response:
        text = skill_text(request.app.state.services.settings)
        return Response(text, media_type="text/markdown; charset=utf-8")
