"""apps.<domain>/: the community's apps, in the belfry's look (not the signer's page).

The proxy sends only ``/`` of apps.<domain> here (rewritten to ``/apps-home``), with the theme's
files; every other path stays the signer's (Keycast) or an installed app's. The list is the folders
of APPS_DIR (infra/data/apps, mounted read-only), the apps ``belfry app install`` put there; each
app's name and icon come from its web manifest, with a fixed name and line for the apps a belfry
ships (chat).

Two parts: **your apps**, the ones this belfry runs (the installed ones, chat by default, and the
EXTRAS an admin added), for everyone; and **can be added**, the EXTRAS not running yet, shown to
admins only (the page asks the console, ``/apps/role``, with the console's cookie). Adding one goes
through the console (``/apps/add``: an admin, a confirmation, then EXTRA_APPS through the setup
folder; the host starts the app)."""

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
# Apps a belfry can add (compose profiles app-<key>; Caddy snippet osv-app): title, line, hosts.
# They come from the first belfry and still wear Open Source Village's look (``osv_look``).
EXTRAS: dict[str, dict[str, Any]] = {
    "calendar": {
        "title": "Calendar",
        "about": "The community's events and your own calendars.",
        "hosts": ["calendar"],
    },
    "vote": {"title": "Vote", "about": "Anonymous polls, one vote per member.", "hosts": ["vote"]},
    "wallet": {
        "title": "Wallet",
        "about": "Community tokens: receive them and pass them on.",
        "hosts": ["wallet"],
    },
    "groups": {
        "title": "Groups",
        "about": "Rooms for teams, topics and projects that anyone can open.",
        "hosts": ["groups"],
    },
    "village": {
        "title": "Directory, market, photos and programme",
        "about": "Who is here, offers between members, shared photos, what is on.",
        "hosts": ["people", "market", "photos", "programme"],
    },
}
for _x in EXTRAS.values():
    _x["osv_look"] = True


def added(settings: Any) -> list[str]:
    """The EXTRAS this belfry runs: all with Open Source Village's suite, else EXTRA_APPS."""
    if getattr(settings, "osv_apps", False):
        return list(EXTRAS)
    raw = (getattr(settings, "extra_apps", "") or "").split()
    return [k for k in EXTRAS if k in raw]


def base(settings: Any) -> str:
    """Where installed apps live: the community's own domain, https://<domain>/<name>/, so they
    share one origin (one sign-in) and the address members know."""
    d = getattr(settings, "nip05_domain", "") or ""
    return f"https://{d}" if d and "." in d else ""


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
.apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;
margin:14px 0 28px}
.app{display:flex;gap:14px;align-items:center;padding:16px;border:1px solid var(--border);
border-radius:12px;color:inherit;text-decoration:none;background:var(--surface,transparent)}
a.app:hover{border-color:var(--accent)}
.app img,.app .ph{width:48px;height:48px;border-radius:12px;flex:none}
.app .ph{display:grid;place-items:center;background:var(--surface-deep);font-weight:600}
.app b{display:block}.app span{color:var(--fg-muted);font-size:.9rem}
.app .tag{display:block;margin-top:6px;font-size:.78rem;color:var(--fg-faint)}
.app.more{border-style:dashed;align-items:flex-start}
.app .add{margin-left:auto;align-self:center;font-size:.9rem;font-weight:600;padding:7px 12px;
border-radius:10px;
border:1px solid var(--border-strong,var(--border));color:inherit;text-decoration:none;
white-space:nowrap}
h2.part{font-size:.8rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
color:var(--fg-faint);margin:8px 0 0}
.links{display:flex;gap:16px;flex-wrap:wrap}
</style>"""


def _icon(a: dict[str, Any], site: str = "") -> str:
    if a.get("icon"):
        return f'<img src="{E(site + a["icon"])}" alt="">'
    return f'<div class="ph">{E(a["title"][:1])}</div>'


def page(
    name: str,
    apps: list[dict[str, Any]],
    pass_url: str,
    console_url: str,
    domain: str = "",
    extras_on: list[str] | None = None,
    site: str = "",
) -> str:
    extras_on = extras_on or []
    mine = "".join(
        f'<a class="app" href="{E(site)}/{E(a["name"])}/">{_icon(a, site)}'
        f"<div><b>{E(a['title'])}</b><span>{E(a['about'])}</span></div></a>"
        for a in apps
    ) + "".join(
        f'<a class="app" href="https://{E(EXTRAS[k]["hosts"][0])}.{E(domain)}/">'
        f"{_icon(EXTRAS[k])}<div><b>{E(EXTRAS[k]['title'])}</b>"
        f"<span>{E(EXTRAS[k]['about'])}</span></div></a>"
        for k in extras_on
        if domain
    )
    more = "".join(
        f'<div class="app more">{_icon(x)}<div><b>{E(x["title"])}</b><span>{E(x["about"])}</span>'
        + ('<span class="tag">Not restyled for Belfry yet</span>' if x["osv_look"] else "")
        + f'</div><a class="add" href="{E(console_url)}/apps/add?app={E(k)}">Add</a></div>'
        for k, x in EXTRAS.items()
        if k not in extras_on
    )
    admin_part = (
        f'<section id="more" hidden><h2 class="part">Can be added · admins only</h2>'
        f'<p class="muted">Members only see the apps above. Adding one starts it on this server '
        f'(a few minutes).</p><div class="apps">{more}</div></section>'
        f"<script>fetch({json.dumps(console_url + '/apps/role')},{{credentials:'include'}})"
        ".then(r=>r.ok?r.json():{}).then(d=>{if(d.admin)document.getElementById('more').hidden=false})"
        ".catch(()=>{})</script>"
        if more and console_url
        else ""
    )
    body = (
        f"{STYLE}<h1>{E(name)}'s apps</h1>"
        '<p class="muted">Members sign in with their e-mail address. Everything stays on the '
        "community's own server.</p>"
        '<h2 class="part">Your apps</h2>'
        + (f'<div class="apps">{mine}</div>' if mine else "<p>No apps are installed yet.</p>")
        + admin_part
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
                s.nip05_domain,
                added(s),
                base(s),
            ),
            headers={"Cache-Control": "no-cache"},
        )
