"""apps.<domain>/: the community's apps, in the belfry's look (not the signer's page).

The proxy sends only ``/`` of apps.<domain> here (rewritten to ``/apps-home``), with the theme's
files; every other path stays the signer's (Keycast) or an installed app's. The list is the folders
of APPS_DIR (infra/data/apps, mounted read-only), the apps ``belfry app install`` put there; each
app's name and icon come from its web manifest, with a fixed name and line for the apps a belfry
ships (chat)."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from . import theme

E = html.escape
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
KNOWN = {
    "chat": ("Chat", "Channels, messages, events and the shared inbox."),
}


def installed(apps_dir: str) -> list[dict[str, Any]]:
    root = Path(apps_dir)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not NAME.match(d.name) or not (d / "index.html").is_file():
            continue
        if not (root / "_routes" / f"{d.name}.caddy").is_file():
            continue  # not served (removed)
        man: dict[str, Any] = {}
        try:
            man = json.loads((d / "manifest.webmanifest").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        title, about = KNOWN.get(d.name, (None, ""))
        title = title or str(man.get("short_name") or man.get("name") or d.name.capitalize())
        icon = ""
        for ic in man.get("icons") or []:
            src = str(ic.get("src") or "") if isinstance(ic, dict) else ""
            if src and "://" not in src and not src.startswith(("/", "data:")) and ".." not in src:
                icon = f"/{d.name}/{src.removeprefix('./')}"
                break
        out.append({"name": d.name, "title": title[:40], "about": about, "icon": icon})
    return out


STYLE = """<style>
.apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin:20px 0}
.app{display:flex;gap:14px;align-items:center;padding:16px;border:1px solid var(--border);
border-radius:12px;color:inherit;text-decoration:none;background:var(--surface,transparent)}
.app:hover{border-color:var(--accent)}
.app img,.app .ph{width:48px;height:48px;border-radius:12px;flex:none}
.app .ph{display:grid;place-items:center;background:var(--surface-deep);font-weight:600}
.app b{display:block}.app span{color:var(--muted);font-size:.9rem}
.links{display:flex;gap:16px;flex-wrap:wrap}
</style>"""


def page(name: str, apps: list[dict[str, Any]], pass_url: str, console_url: str) -> str:
    cards = "".join(
        f'<a class="app" href="/{E(a["name"])}/">'
        + (
            f'<img src="{E(a["icon"])}" alt="">'
            if a["icon"]
            else f'<div class="ph">{E(a["title"][:1])}</div>'
        )
        + f"<div><b>{E(a['title'])}</b><span>{E(a['about'])}</span></div></a>"
        for a in apps
    )
    body = (
        f"{STYLE}<h1>{E(name)}'s apps</h1>"
        '<p class="muted">Members sign in with their e-mail address. Everything stays on the '
        "community's own server.</p>"
        + (f'<div class="apps">{cards}</div>' if apps else "<p>No apps are installed yet.</p>")
        + '<p class="links">'
        + f'<a href="{E(pass_url)}/me">Your account</a>'
        + (f'<a href="{E(console_url)}">The belfry</a>' if console_url else "")
        + "</p>"
    )
    return theme.frame(f"{name}: apps", body)


def register(app: FastAPI) -> None:
    @app.get("/apps-home", response_class=HTMLResponse, include_in_schema=False)
    async def apps_home(request: Request):
        s = request.app.state.services.settings
        apps = await run_in_threadpool(installed, s.apps_dir)
        return HTMLResponse(
            page(
                theme.current().name,
                apps,
                s.public_url.rstrip("/"),
                (getattr(s, "console_url", "") or "").rstrip("/"),
            ),
            headers={"Cache-Control": "no-cache"},
        )
