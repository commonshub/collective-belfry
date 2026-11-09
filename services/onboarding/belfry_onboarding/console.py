"""``belfry.<domain>``: the belfry's own page. What runs, who is in, and the website.

- ``/``: a summary: how many services answer and the code's version (→ ``/status``), how many
  members and admins (→ ``/members``), and what the homepage shows (→ ``/website``).
- ``/status`` (public, also ``/status.json``): every service checked from inside the stack, with
  its answer time; the deployed code and the last change (the changelog).
- ``/members``: the members (people with an active pass) with their role. Members see names; the
  belfry's admins (the stewards group) also see e-mail addresses and add, edit and remove members
  by e-mail: adding gives a free pass (activated at once, so the welcome mail with the sign-in link
  goes out), the admin role is the stewards group, removing revokes the pass.
- ``/website``: what the apex shows, and the website versions members proposed: a member publishes
  their version as an nsite with their own key (NIP-5A, named site ``website`` or a root site) on
  this belfry's relay and Blossom server, and proposes it (a ``/proposals`` note tagged
  ``t=website`` with an ``a`` tag naming the site). An admin approves: the belfry copies that
  manifest as its own root site (NIP-5A copy, ``a``/``A`` tags) and records a snapshot (kind 5128)
  naming who proposed and who approved, which the changelog shows.

The host is CONSOLE_URL; this service answers it under ``/console`` (the middleware maps the
host's paths there). Signing in goes through the pass page (``/me`` on PUBLIC_URL, which owns the
OIDC client): ``/me/console`` hands a short ticket to ``belfry.<domain>/session``, which sets this
host's own cookie, so no session cookie is ever shared between hosts. Unlike ``/steward``, these
pages are not limited to the admin network: an admin is someone in the stewards group who signed
in with their pass.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
import time
import urllib.parse
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import community, core, flash, homepage, mypass, steward
from .claim import page
from .core import Services
from .nip46check import sign_event

log = logging.getLogger(__name__)
E = html.escape

COOKIE = "belfry_console"
NEXT_COOKIE = "belfry_pass_next"
TICKET_SECONDS = 120
STATUS_SECONDS = 30.0
ADMINS_SECONDS = 60.0
WEBSITE_TAG = "website"
SNAPSHOT_KIND = 5128


# ── hosts ───────────────────────────────────────────────────────────────────────────────────
def console_url(settings: Any) -> str:
    return (getattr(settings, "console_url", "") or "").rstrip("/")


def console_host(settings: Any) -> str:
    url = console_url(settings)
    return httpx.URL(url).netloc.decode() if url else ""


# ── sessions ────────────────────────────────────────────────────────────────────────────────
def read_session(svc: Services, cookie: str | None) -> dict[str, Any] | None:
    return mypass._verify(svc, cookie, "console") if mypass.enabled(svc) else None


_admins: dict[str, Any] = {"at": 0.0, "emails": set()}


def admin_emails(svc: Services) -> set[str]:
    now = time.monotonic()
    if now - _admins["at"] > ADMINS_SECONDS:
        try:
            emails = svc.authentik.group_member_emails(svc.settings.steward_group)
            _admins.update(at=now, emails={e.lower() for e in emails if e})
        except Exception as exc:
            log.warning("console: stewards group not readable: %s", exc)
    return _admins["emails"]


def viewer(svc: Services, sess: dict[str, Any] | None) -> dict[str, Any] | None:
    """The signed-in member: partner id, name, e-mail, admin; None without an active pass."""
    if not sess:
        return None
    pid = sess["partner"]
    row = mypass.partner_pass(svc, pid)
    if not row or row.get("state") != "active" or row.get("tier") == "guest":
        return None  # a channel's guest (guests.py) is no member of the belfry
    partner = (svc.odoo.read("res.partner", [pid], ["name", "email"]) or [{}])[0]
    email = (partner.get("email") or "").lower()
    return {
        "partner": pid,
        "name": partner.get("name") or "",
        "email": email,
        "admin": bool(email) and email in admin_emails(svc),
        "csrf": sess.get("csrf", ""),
    }


# ── status ──────────────────────────────────────────────────────────────────────────────────
def checks(settings: Any) -> list[tuple[str, str, dict[str, str]]]:
    """(name, internal URL, headers) of every service worth checking."""
    nostr = {"Accept": "application/nostr+json"}
    out = []
    from .settings import uses_register

    if not uses_register(settings):
        out.append(
            ("Registry and billing (Odoo)", settings.odoo_url.rstrip("/") + "/web/health", {})
        )
    out += [
        ("Sign-in (Authentik)", settings.authentik_url.rstrip("/") + "/-/health/live/", {}),
        ("Signer (Keycast)", settings.keycast_url.rstrip("/") + "/", {}),
    ]
    if settings.relay_url:
        out.append(("Community relay", settings.relay_url.replace("ws", "http", 1), nostr))
    out.append(("Files (Blossom)", settings.homepage_blossom_url.rstrip("/") + "/", {}))
    if settings.mint_url:
        out.append(("Tokens (mint)", settings.mint_url.rstrip("/") + "/v1/info", {}))
    if settings.issuer_url:
        out.append(("Pass issuer", settings.issuer_url, {}))
    if settings.verifier_url:
        out.append(("Pass verifier", settings.verifier_url, {}))
    if settings.groups_api_url:
        out.append(("Groups relay", settings.groups_api_url, nostr))
    if getattr(settings, "git_public_url", ""):
        out.append(("Git server", "http://git:7334/", nostr))
    return out


_status: dict[str, Any] = {"at": 0.0, "data": None}


async def check_all(settings: Any) -> list[dict[str, Any]]:
    async def one(client: httpx.AsyncClient, name: str, url: str, headers: dict[str, str]):
        start = time.monotonic()
        try:
            r = await client.get(url, headers=headers)
            up = r.status_code < 500
            detail = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            up, detail = False, type(exc).__name__
        return {
            "name": name,
            "up": up,
            "ms": int((time.monotonic() - start) * 1000),
            "detail": detail,
        }

    async with httpx.AsyncClient(timeout=4.0, follow_redirects=False) as client:
        return list(await asyncio.gather(*(one(client, *c) for c in checks(settings))))


async def status_data(svc: Services) -> dict[str, Any]:
    now = time.monotonic()
    if _status["data"] is not None and now - _status["at"] < STATUS_SECONDS:
        return _status["data"]
    services = await check_all(svc.settings)
    log_data = await run_in_threadpool(community.read_changelog, svc.settings.changelog_path) or {}
    entries = log_data.get("entries") or []
    code = next((e for e in entries if e.get("type") == "code"), None)
    data = {
        "services": services,
        "up": sum(1 for s in services if s["up"]),
        "total": len(services),
        "version": (code or {}).get("ref", "")[:12],
        "version_summary": (code or {}).get("summary", ""),
        "last_change": entries[0] if entries else None,
        "checked": int(time.time()),
    }
    _status.update(at=now, data=data)
    return data


# ── the code: version and updates (version.json / update.json from the host's belfry command) ─
def code_info(settings: Any) -> dict[str, Any]:
    """What the belfry runs and whether newer code is out (``belfry version`` / ``belfry apply``
    write version.json), and the last update asked for here (update.json, update.request)."""
    from .setup import read_json, setup_dir

    folder = setup_dir(settings)
    if folder is None:
        return {"available": False}
    return {
        "available": True,
        "version": read_json(folder / "version.json"),
        "update": read_json(folder / "update.json"),
        "requested": (folder / "update.request").is_file(),
    }


def request_update(settings: Any, by: str) -> bool:
    from .setup import setup_dir

    folder = setup_dir(settings)
    if folder is None:
        return False
    tmp = folder / "update.request.tmp"
    tmp.write_text(json.dumps({"by": by, "at": int(time.time())}))
    tmp.replace(folder / "update.request")
    return True


def code_html(info: dict[str, Any], who: dict[str, Any] | None, msg: str, err: str) -> str:
    v, up = info.get("version") or {}, info.get("update") or {}
    if not info.get("available") and not v:
        return '<h2>Code</h2><p class="muted">This belfry does not report its version.</p>'
    running = info.get("requested") or up.get("state") == "running"
    line = (
        f"Version <code>{E(v.get('short', '?'))}</code> ({E(v.get('branch', ''))}, "
        f"{E((v.get('date') or '')[:10])}): {E(v.get('subject', ''))}"
        if v
        else "Version unknown (run <code>belfry version</code> on the server)."
    )
    newer = ""
    if v.get("behind"):
        items = "".join(f"<li>{E(x)}</li>" for x in v.get("newer") or [])
        more = v["behind"] - len(v.get("newer") or [])
        newer = (
            f"<p><strong>{v['behind']} update{'s' if v['behind'] > 1 else ''} available</strong> "
            f"(latest <code>{E(v.get('latest', ''))}</code>):</p><ul>{items}"
            f"{f'<li>and {more} more</li>' if more > 0 else ''}</ul>"
        )
    elif v:
        newer = f'<p class="up">Up to date (checked {E((v.get("checked_at") or "")[:16].replace("T", " "))} UTC).</p>'
    last = ""
    if running:
        last = (
            '<p role="status"><strong>Updating…</strong> The belfry fetches the new code and restarts '
            "its services; this page may stop answering for a few minutes.</p>"
            '<meta http-equiv="refresh" content="20">'
        )
    elif up.get("state") == "ok":
        last = (
            f'<p class="meta">Last update: from <code>{E(up.get("from", ""))}</code> to '
            f"<code>{E(up.get('to', ''))}</code>, {E((up.get('at') or '')[:16].replace('T', ' '))} UTC.</p>"
        )
    elif up.get("state") == "failed":
        last = (
            f'<p class="error">The last update failed ({E((up.get("at") or "")[:16].replace("T", " "))} UTC): '
            f"{E(up.get('message', ''))}</p>"
        )
    button = ""
    if who and who["admin"] and not running and info.get("available"):
        label = "Update now" if v.get("behind") else "Check again and update"
        button = (
            '<form method="post" action="/update"><input type="hidden" name="csrf" '
            f'value="{E(who["csrf"])}"><button class="btn" type="submit">{label}</button></form>'
        )
    note = (f'<p class="up">{E(msg)}</p>' if msg else "") + (
        f'<p class="error">{E(err)}</p>' if err else ""
    )
    return f"<h2>Code</h2>{note}<p>{line}</p>{newer}{last}{button}"


# ── members ─────────────────────────────────────────────────────────────────────────────────
def members(svc: Services) -> list[dict[str, Any]]:
    passes = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"]],
        ["partner_id", "tier", "date_start", "state"],
        order="id desc",
        limit=2000,
    )
    ids = sorted({core.m2o_id(p["partner_id"]) for p in passes if p.get("partner_id")})
    partners = (
        {
            r["id"]: r
            for r in svc.odoo.read("res.partner", ids, ["name", "email", "belfry_member_number"])
        }
        if ids
        else {}
    )
    admins = admin_emails(svc)
    out, seen = [], set()
    for p in passes:
        pid = core.m2o_id(p["partner_id"]) if p.get("partner_id") else None
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        r = partners.get(pid, {})
        email = (r.get("email") or "").lower()
        out.append(
            {
                "partner": pid,
                "pass": p["id"],
                "name": r.get("name") or "",
                "email": email,
                "number": r.get("belfry_member_number") or "",
                "since": str(p.get("date_start") or "")[:10],
                "admin": bool(email) and email in admins,
            }
        )
    return sorted(out, key=lambda m: (not m["admin"], m["name"].lower()))


def add_member(svc: Services, email: str, name: str, admin: bool) -> str:
    email = email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("That is not an e-mail address.")
    if admin:
        steward.add_steward(svc, email, name.strip())
    else:
        steward.steward_pass(svc, email, name.strip())
    _admins["at"] = 0.0
    return email


def set_admin(svc: Services, email: str, admin: bool) -> None:
    ak = svc.authentik
    group = ak.find_group(svc.settings.steward_group)
    if group is None:
        raise RuntimeError(f"group {svc.settings.steward_group} missing")
    if admin:
        steward.add_steward(svc, email)
    else:
        for user in ak.find_users_by_email(email):
            ak.remove_user_from_group(group["pk"], int(user["pk"]))
    _admins["at"] = 0.0


def edit_member(svc: Services, partner: int, name: str, admin: bool) -> None:
    row = (svc.odoo.read("res.partner", [partner], ["name", "email"]) or [None])[0]
    if row is None:
        raise ValueError("No such member.")
    if name.strip() and name.strip() != row.get("name"):
        svc.odoo.write("res.partner", [partner], {"name": name.strip()})
    email = (row.get("email") or "").lower()
    if email and admin != (email in admin_emails(svc)):
        set_admin(svc, email, admin)


def remove_member(svc: Services, partner: int) -> None:
    row = (svc.odoo.read("res.partner", [partner], ["email"]) or [None])[0]
    if row is None:
        raise ValueError("No such member.")
    for p in svc.odoo.search_read(
        "belfry.pass", [["partner_id", "=", partner], ["state", "=", "active"]], ["id"]
    ):
        core.revoke_pass(svc, p["id"], "removed by an admin")
    email = (row.get("email") or "").lower()
    if email and email in admin_emails(svc):
        set_admin(svc, email, False)


# ── website ─────────────────────────────────────────────────────────────────────────────────
def site_ref(e: dict[str, Any]) -> tuple[str, str, str] | None:
    """(kind, pubkey, d) of the nsite a website proposal names."""
    for t in e.get("tags", []):
        if len(t) > 1 and t[0] == "a":
            parts = t[1].split(":", 2)
            if len(parts) == 3 and parts[0] in ("15128", "35128") and len(parts[1]) == 64:
                return parts[0], parts[1], parts[2]
    return None


def website_data(svc: Services) -> dict[str, Any]:
    belfry = homepage.belfry_pubkey(svc.settings)
    data: dict[str, Any] = {
        "mode": homepage.parse_config(svc.settings).mode,
        "current": None,
        "proposals": [],
        "names": {},
    }
    if not belfry or svc.relay is None:
        return data
    events = community.query(
        svc,
        [
            {"kinds": [homepage.ROOT_KIND], "authors": [belfry], "limit": 1},
            {"kinds": [1], "#t": [WEBSITE_TAG], "#p": [belfry], "limit": 100},
            {"kinds": [SNAPSHOT_KIND], "authors": [belfry], "limit": 1},
        ],
    )
    roots = [e for e in events if e["kind"] == homepage.ROOT_KIND]
    snaps = [e for e in events if e["kind"] == SNAPSHOT_KIND]
    if roots:
        cur = max(roots, key=lambda e: e["created_at"])
        snap = max(snaps, key=lambda e: e["created_at"]) if snaps else None
        data["current"] = {
            "at": cur["created_at"],
            "files": len(homepage.manifest_paths(cur)),
            "note": (snap or {}).get("content", ""),
            "from": next((t[1] for t in cur["tags"] if t[0] == "a"), ""),
        }
    props = [e for e in events if e["kind"] == 1 and site_ref(e)]
    data["proposals"] = sorted(props, key=lambda e: e["created_at"], reverse=True)
    if props:
        data["names"] = community.names_for(svc, [e["pubkey"] for e in props])
    return data


def approve_website(svc: Services, proposal_id: str, approver: str) -> dict[str, Any]:
    """Copy the proposed nsite as the belfry's root site (NIP-5A copying) and snapshot it."""
    key = homepage.belfry_key(svc.settings)
    if key is None or svc.relay is None:
        raise RuntimeError("no belfry key or relay")
    found = community.query(svc, [{"ids": [proposal_id]}])
    prop = next((e for e in found if e["id"] == proposal_id and e["kind"] == 1), None)
    ref = site_ref(prop) if prop else None
    if ref is None:
        raise ValueError("No such website proposal.")
    kind, pubkey, d = ref
    flt: dict[str, Any] = {"kinds": [int(kind)], "authors": [pubkey]}
    if kind == "35128":
        flt["#d"] = [d]
    manifests = [e for e in community.query(svc, [flt]) if e["pubkey"] == pubkey]
    if not manifests:
        raise ValueError("The proposed site's manifest is not on this belfry's relay.")
    src = max(manifests, key=lambda e: e["created_at"])
    paths = homepage.manifest_paths(src)
    if "/index.html" not in paths:
        raise ValueError("The proposed site has no /index.html.")
    a = f"{kind}:{pubkey}:{d}"
    origin = next((t for t in src["tags"] if t[0] == "A"), ["A", a])
    keep = [
        t for t in src["tags"] if t and t[0] in ("path", "server", "title", "description", "source")
    ]
    agg = homepage.aggregate_hash(paths)
    now = int(time.time())
    root = sign_event(
        key.secret,
        {
            "kind": homepage.ROOT_KIND,
            "created_at": now,
            "content": "",
            "tags": [*keep, ["x", agg, "aggregate"], ["a", a], origin, ["e", proposal_id]],
        },
    )
    names = community.names_for(svc, [pubkey])
    belfry = root["pubkey"]
    snapshot = sign_event(
        key.secret,
        {
            "kind": SNAPSHOT_KIND,
            "created_at": now,
            "content": f"Website updated: proposed by {names.get(pubkey, pubkey[:8])}, approved by {approver}.",
            "tags": [
                *[t for t in keep if t[0] == "path"],
                ["x", agg, "aggregate"],
                ["a", f"{homepage.ROOT_KIND}:{belfry}:"],
                origin,
                ["e", proposal_id],
                ["p", pubkey],
            ],
        },
    )
    sign = lambda e: sign_event(key.secret, e)  # noqa: E731
    svc.relay.publish(root, sign)
    svc.relay.publish(snapshot, sign)
    reply = sign(
        {
            "kind": 1,
            "created_at": now,
            "content": f"Approved by {approver} and published as the website.",
            "tags": [["e", proposal_id, "", "root"], ["p", prop["pubkey"]]],
        }
    )
    svc.relay.publish(reply, sign)
    from .website import changelog

    changelog(
        svc,
        f"**Website updated**: proposed by {names.get(pubkey, pubkey[:8])}, approved by {approver} "
        f"({len(paths)} files): https://{svc.settings.nip05_domain}",
    )
    return {"files": len(paths), "manifest": root["id"]}


# ── pages ───────────────────────────────────────────────────────────────────────────────────
STYLE = (
    community.STYLE
    + """<style>
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.tile{border:1px solid var(--border);border-radius:10px;padding:14px;text-decoration:none;color:inherit;display:block}
.tile:hover{border-color:var(--accent)} .big{font-size:1.6rem;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:.92rem} td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--border)}
.up{color:var(--accent)} .down{color:var(--accent-2)} form.inline{display:inline}
details summary{cursor:pointer}
</style>"""
)


HOME_STYLE = """<style>
main{max-width:1040px} header.site{display:none}
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:0 0 20px}
.bar .brand{display:inline-flex;align-items:center;gap:10px;text-decoration:none;color:var(--fg);font:700 1.15rem/1 var(--font-title)}
.bar .brand img{height:28px;width:auto;display:block}
.bar .right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.who{display:inline-flex;align-items:center;gap:8px;color:var(--fg-muted);font-size:.92rem}
.av{width:32px;height:32px;border-radius:99px;background:var(--surface-deep);border:1px solid var(--border);
display:inline-grid;place-items:center;font-weight:600;color:var(--fg)}
.btn2{display:inline-block;padding:9px 14px;border-radius:10px;border:1px solid var(--border-strong,var(--border));
background:var(--surface);color:var(--fg);text-decoration:none;font-weight:600;font-size:.92rem}
.manage{display:flex;gap:4px;flex-wrap:wrap;margin:0 0 24px;border-bottom:1px solid var(--border)}
.manage a{padding:8px 12px;text-decoration:none;color:var(--fg-muted);border-bottom:2px solid transparent;margin-bottom:-1px}
.manage a.on{color:var(--fg);border-bottom-color:var(--fg);font-weight:600}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px}
.eyebrow{margin:0;font-size:.8rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--fg-faint)}
.hero{display:flex;flex-direction:column;gap:14px;max-width:640px;padding:40px 0 32px}
.hero h1{font-size:3rem;line-height:1.05;margin:0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;margin:0 0 28px}
.app{display:flex;align-items:center;gap:14px;padding:16px;background:var(--surface);border:1px solid var(--border);
border-radius:14px;text-decoration:none;color:var(--fg)} .app:hover{border-color:var(--fg-faint)}
.app .ic{width:44px;height:44px;border-radius:12px;background:var(--surface-deep);display:grid;place-items:center;flex:none;overflow:hidden}
.app .ic img{width:44px;height:44px} .app b{display:block} .app small{color:var(--fg-faint);font-size:.85rem}
.strip{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden;margin:0 0 28px}
.strip .head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--border);flex-wrap:wrap}
.strip h2{margin:0;font-size:1rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.stat{padding:16px 20px;text-decoration:none;color:var(--fg);border-right:1px solid var(--border);display:flex;flex-direction:column;gap:2px}
.stat:last-child{border-right:0} .stat:hover{background:var(--surface-deep)}
.stat .k{font-size:.8rem;color:var(--fg-faint)} .stat b{font-size:1.4rem} .stat .s{font-size:.82rem;color:var(--fg-muted)}
.steps{padding:14px 20px;border-top:1px solid var(--border);background:var(--surface-deep);display:flex;flex-direction:column;gap:10px}
.steps h3{margin:0;font-size:.85rem} .step{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:99px;background:var(--ok)} .dot.bad{background:var(--accent-2)}
.cols{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}
.cols > *{flex:1 1 300px}
.foot{margin-top:32px;padding-top:16px;border-top:1px solid var(--border);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:.85rem;color:var(--fg-faint)}
.foot a{color:var(--fg-muted)}
.chip{display:inline-block;padding:5px 10px;border-radius:99px;background:var(--surface-deep);font-size:.85rem}
</style>"""

ADMIN_TABS = [
    ("/status", "Status"),
    ("/members", "Members"),
    ("/website", "Website"),
    ("/monitor", "Monitor"),
    ("/apps", "Apps"),
    ("/setup", "Setup"),
]
MEMBER_TABS = [("/members", "Members"), ("/monitor", "Monitor")]


def initial(name: str) -> str:
    return (name.strip()[:1] or "?").upper()


def topbar(who: dict[str, Any] | None) -> str:
    from . import theme

    t = theme.current()
    brand = (
        f'<img src="{E(t.logo)}" alt="{E(t.name)}">'
        if t.logo_is_wordmark
        else f'<img src="{E(t.logo)}" alt=""><span>{E(t.name)}</span>'
    )
    if who:
        manage = '<a class="btn2" href="/status">Manage</a>' if who["admin"] else ""
        right = (
            f'{manage}<span class="who">{E(who["name"])}{" · admin" if who["admin"] else ""}'
            f'<span class="av" aria-hidden="true">{E(initial(who["name"]))}</span></span>'
            '<a href="/logout" class="who">Sign out</a>'
        )
    else:
        right = '<a class="btn" href="/login">Sign in</a>'
    return f'<div class="bar"><a class="brand" href="/">{brand}</a><span class="right">{right}</span></div>'


def nav(current: str, who: dict[str, Any] | None) -> str:
    """The bar, and under it the pages to manage the belfry (none on the home page)."""
    tabs = ADMIN_TABS if who and who["admin"] else MEMBER_TABS
    links = "".join(
        f'<a href="{h}" class="{"on" if h == current else ""}">{t}</a>' for h, t in tabs
    )
    sub = (
        f'<nav class="manage" aria-label="Manage"><a href="/">← Home</a>{links}</nav>'
        if current != "/"
        else ""
    )
    return topbar(who) + sub


def frame(title: str, current: str, who: dict[str, Any] | None, body: str) -> str:
    inner = body if current == "/" else f'<div class="panel">{body}</div>'
    return page(title, STYLE + HOME_STYLE + nav(current, who) + inner, wide=True)


def mail_state(s: Any) -> tuple[str, str]:
    host = (getattr(s, "smtp_host", "") or "").lower()
    name = {"smtp.lettermint.co": "Lettermint", "smtp.resend.com": "Resend"}.get(host)
    if not host or host == "mailpit":
        return "", "Not set up: nobody can sign in"
    sender = getattr(s, "mail_from", "") or f"belfry@{s.nip05_domain}"
    return (name or host), sender


def app_tiles(svc: Services) -> str:
    from . import appshome

    tiles = ""
    for a in appshome.installed(svc.settings.apps_dir):
        src = appshome.icon_url(svc.settings, a)
        icon = f'<img src="{E(src)}" alt="">' if src else f"<b>{E(a['title'][:1])}</b>"
        tiles += (
            f'<a class="app" href="{E(appshome.app_url(svc.settings, a["name"]))}"><span class="ic">{icon}</span>'
            f"<span><b>{E(a['title'])}</b><small>{E(a['about'])}</small></span></a>"
        )
    pass_url = svc.settings.public_url.rstrip("/")
    tiles += (
        f'<a class="app" href="{E(pass_url)}/me"><span class="ic"><b>✓</b></span>'
        "<span><b>Your pass</b><small>Your membership, invites and devices</small></span></a>"
    )
    return f'<section class="apps" aria-label="Apps">{tiles}</section>'


def overview_html(
    svc: Services,
    status: dict[str, Any],
    mem: list[dict[str, Any]] | None,
    counts: dict[str, int],
    site: dict[str, Any],
    who: dict[str, Any] | None,
    code: dict[str, Any] | None = None,
) -> str:
    s = svc.settings
    name = s.community_name or "This community"
    healthy = status["up"] == status["total"]
    foot = (
        f'<div class="foot"><span><span class="dot{"" if healthy else " bad"}"></span> '
        + (
            "All services working"
            if healthy
            else f"{status['up']} of {status['total']} services answering"
        )
        + ' · <a href="/status">status</a></span></div>'
    )
    if who is None:
        from . import appshome

        chips = "".join(
            f'<span class="chip">{E(a["title"])}</span>' for a in appshome.installed(s.apps_dir)
        )
        body = f"""<section class="hero"><p class="eyebrow">A community on its own server</p>
<h1>{E(name)}</h1>
<div class="row"><a class="btn" href="/login">Sign in with your e-mail</a></div>
<p class="muted" style="margin:0;font-size:.9rem">No password: we send you a 6-digit code.</p></section>
<div class="cols"><div class="panel"><h2 style="margin:0 0 8px;font-size:1rem">Inside, for members</h2>
<p class="muted" style="margin:0 0 12px">Everything is kept on {E(name)}'s own server, and your account is yours to take with you.</p>
<div class="row">{chips or '<span class="chip">Chat</span>'}<span class="chip">Your pass</span></div></div>
<div class="panel"><h2 style="margin:0 0 8px;font-size:1rem">How to join</h2>
<p class="muted" style="margin:0">Members invite new members: ask someone you know in {E(name)} for an invite link.</p></div></div>
{foot}"""
        return frame(name, "/", None, body)

    first = who["name"].split()[0] if who["name"].strip() else ""
    admin = ""
    if who["admin"]:
        prov, sender = mail_state(s)
        current = site.get("current")
        v = (code or {}).get("version") or {}
        behind = int(v.get("behind") or 0)
        ver = (
            f"{behind} update{'s' if behind > 1 else ''}"
            if behind
            else ("Up to date" if v else "Unknown")
        )
        steps = []
        if not prov:
            steps.append(("Set up e-mail: nobody can sign in until it works.", "/setup", "Set up"))
        if counts["members"] <= 1:
            steps.append(
                (f"Invite your first members: {E(name)} has only you.", "/members", "Invite people")
            )
        if not current and site.get("mode") == "nsite":
            steps.append(
                ("Publish a website: the homepage is a placeholder.", "/website", "Website")
            )
        if behind:
            steps.append(
                (f"A newer version of Belfry is out ({behind} changes).", "/status", "Update")
            )
        steps_html = (
            '<div class="steps"><h3>Next steps</h3>'
            + "".join(
                f'<div class="step"><span>{t}</span><a class="btn" href="{h}">{E(label)}</a></div>'
                for t, h, label in steps
            )
            + "</div>"
            if steps
            else ""
        )
        n = counts["members"]
        admin = f"""<section class="strip" aria-label="Your belfry"><div class="head"><h2>Your belfry</h2>
<span><span class="dot{"" if healthy else " bad"}"></span> {"All" if healthy else status["up"]} of {status["total"]} services working</span></div>
<div class="stats">
<a class="stat" href="/members"><span class="k">Members</span><b>{n}</b><span class="s">{counts["admins"]} admin{"" if counts["admins"] == 1 else "s"}</span></a>
<a class="stat" href="/setup#mail"><span class="k">E-mail</span><b>{"Working" if prov else "Not set"}</b><span class="s">{E(prov + " · " + sender if prov else sender)}</span></a>
<a class="stat" href="/website"><span class="k">Website</span><b>{"Live" if current else "Placeholder"}</b><span class="s">{E("Published " + community.when(current["at"]) if current else "Nothing published yet")}</span></a>
<a class="stat" href="/status"><span class="k">Version</span><b>{E(ver)}</b><span class="s">{E(v.get("short", status.get("version") or "?"))}{E(" · " + (v.get("date") or "")[:10]) if v.get("date") else ""}</span></a>
</div>{steps_html}</section>"""
    me = next((m for m in mem or [] if m["partner"] == who["partner"]), None)
    since = f"Member since {me['since']}" if me and me.get("since") else "Member"
    num = f" · number {me['number']}" if me and me.get("number") else ""
    you = f"""<div class="panel"><h2 style="margin:0 0 8px;font-size:1rem">You</h2>
<p class="muted" style="margin:0 0 10px;font-size:.92rem">{E(since + num)} · {E(who["email"])}</p>
<div class="row" style="gap:16px"><a href="{E(s.public_url)}/me">Invite someone</a><a href="{E(s.public_url)}/me">Your pass and devices</a></div></div>"""
    others = ""
    if mem is not None and len(mem) > 1:
        names = ", ".join(E(m["name"]) for m in mem[:12]) + ("…" if len(mem) > 12 else "")
        others = f"""<div class="panel"><h2 style="margin:0 0 8px;font-size:1rem">{len(mem)} members</h2>
<p class="muted" style="margin:0 0 10px;font-size:.92rem">{names}</p><a href="/members">Everyone</a></div>"""
    body = f"""<h1 style="font-size:1.9rem;margin:8px 0 24px">Welcome{", " + E(first) if first else ""}</h1>
{admin}{app_tiles(svc)}<div class="cols">{you}{others}</div>{foot}"""
    return frame(name, "/", who, body)


def status_html(status: dict[str, Any], who: dict[str, Any] | None, code: str = "") -> str:
    rows = "".join(
        f'<tr><td>{E(x["name"])}</td><td class="{"up" if x["up"] else "down"}">{"up" if x["up"] else "down"}</td>'
        f'<td class="meta">{x["ms"]} ms · {E(x["detail"])}</td></tr>'
        for x in status["services"]
    )
    last = status.get("last_change") or {}
    return frame(
        "Status",
        "/status",
        who,
        f"""<h1>Status</h1><p class="muted">{status["up"]} of {status["total"]} services answer, checked from inside the belfry at
{community.when(status["checked"])}.</p>
<table><tr><th>Service</th><th>State</th><th>Answer</th></tr>{rows}</table>
{code}
<p class="meta">Last change: {E(last.get("summary", "—"))} · started by {E(", ".join(last.get("by") or []) or "—")}.
All changes: <a href="/changelog">changelog</a>. Machine-readable: <a href="/status.json">status.json</a>.</p>""",
    )


def members_html(mem: list[dict[str, Any]], who: dict[str, Any] | None, msg: str, err: str) -> str:
    if who is None:
        return frame(
            "Members",
            "/members",
            None,
            f"""<h1>Members</h1><p class="muted">{len(mem)} members. The list is for members:
<a class="btn" href="/login?to=/members">Sign in</a></p>""",
        )
    admin = who["admin"]
    csrf = E(who["csrf"])
    rows = []
    for m in mem:
        email = f'<td class="meta">{E(m["email"])}</td>' if admin else ""
        actions = ""
        if admin:
            actions = f"""<td><details><summary>Edit</summary>
<form method="post" action="/members/{m["partner"]}/edit"><input type="hidden" name="csrf" value="{csrf}">
<p><input type="text" name="name" value="{E(m["name"])}"></p>
<p><label><input type="checkbox" name="admin" value="1" {"checked" if m["admin"] else ""}> admin</label></p>
<p><button class="btn" type="submit">Save</button></p></form>
<form method="post" action="/members/{m["partner"]}/remove" onsubmit="return confirm('Remove {E(m["name"])}? Their pass ends now; their key stays theirs.')">
<input type="hidden" name="csrf" value="{csrf}"><button type="submit">Remove</button></form></details></td>"""
        rows.append(
            f"<tr><td>{E(m['name'])}</td>{email}<td>{'admin' if m['admin'] else 'member'}</td>"
            f'<td class="meta">{E(m["since"])}</td>{actions}</tr>'
        )
    head = (
        "<tr><th>Name</th>"
        + ("<th>E-mail</th>" if admin else "")
        + "<th>Role</th><th>Since</th>"
        + ("<th></th>" if admin else "")
        + "</tr>"
    )
    add = ""
    if admin:
        add = f"""<h2>Add a member</h2><form method="post" action="/members/add"><input type="hidden" name="csrf" value="{csrf}">
<p><input type="text" name="email" placeholder="e-mail address" required></p>
<p><input type="text" name="name" placeholder="name (optional)"></p>
<p><label><input type="checkbox" name="admin" value="1"> admin (can add and remove members, approve the website)</label></p>
<p><button class="btn" type="submit">Add</button></p>
<p class="meta">They get a pass at once and an e-mail with a sign-in link. Removing someone ends their pass; their Nostr key stays theirs.</p></form>"""
    note = (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
        f'<p class="bad">{E(err)}</p>' if err else ""
    )
    return frame(
        "Members",
        "/members",
        who,
        f"""<h1>Members</h1><p class="muted">{len(mem)} members with an active pass.</p>{note}{add}
<h2>Everyone</h2><table>{head}{"".join(rows)}</table>""",
    )


def website_html(
    svc: Services, site: dict[str, Any], who: dict[str, Any] | None, msg: str, err: str
) -> str:
    s = svc.settings
    domain = httpx.URL(s.site_url).host if s.site_url else "<domain>"
    current = site.get("current")
    if current:
        now = f"Published {community.when(current['at'])}, {current['files']} files. {E(current['note'])}"
    elif site["mode"] == "nsite":
        now = "Nothing published yet: the address shows a placeholder."
    else:
        now = f"The address shows {'the join page' if site['mode'] == 'start' else 'another website (proxy)'}."
    items = []
    for e in site.get("proposals") or []:
        kind, pk, d = site_ref(e)
        approve = ""
        if who and who["admin"]:
            approve = f"""<form method="post" action="/website/{e["id"]}/approve" class="inline">
<input type="hidden" name="csrf" value="{E(who["csrf"])}"><button class="btn" type="submit">Approve and publish</button></form>"""
        items.append(
            f"""<div class="item"><strong>{E(community.subject(e))}</strong>
<div class="meta">by {E(site["names"].get(e["pubkey"], ""))} · {community.when(e["created_at"])} ·
<code>{E(kind)}:{E(pk[:8])}…:{E(d)}</code> · <a href="{E(s.public_url)}/proposals/{e["id"]}">discussion</a></div>{approve}</div>"""
        )
    note = (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
        f'<p class="bad">{E(err)}</p>' if err else ""
    )
    skill = f"{s.public_url.rstrip('/')}/skills/belfry-homepage.md"
    return frame(
        "Website",
        "/website",
        who,
        f"""<h1>Website</h1><p>https://{E(domain)}: {now}</p>{note}
<h2>How it gets updated</h2>
<ol><li><strong>Ask your agent</strong>: “Build the website of {E(s.community_name)} and publish it on {E(domain)}. Follow {E(skill)}.”</li>
<li><strong>A member's agent</strong> publishes the site as an nsite with the member's key on this belfry, and proposes it (it appears below).</li>
<li><strong>An admin approves</strong> it here; the belfry then publishes it under its own key. Admins can also publish directly from the server (<code>belfry homepage publish</code>).</li></ol>
<h2>Proposed versions</h2>{"".join(items) or '<p class="muted">None yet.</p>'}""",
    )


def placeholder_html(settings: Any) -> str:
    """What the apex shows before a website is published (HOMEPAGE=nsite, no manifest yet)."""
    name = settings.community_name or "This community"
    domain = httpx.URL(settings.site_url).host if settings.site_url else "this domain"
    console = console_url(settings) or settings.public_url
    skill = f"{settings.public_url.rstrip('/')}/skills/belfry-homepage.md"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{E(name)}</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f3ec;--ink:#1f1d1a;--muted:#5d574e;--line:#d9d2c3;--accent:#a4532b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16140f;--ink:#efe9dc;--muted:#a69f91;--line:#3a352c;--accent:#e08a5c}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 system-ui,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:56px 20px}} h1{{font-size:2rem;margin:0 0 8px}}
.muted{{color:var(--muted)}} a{{color:var(--accent)}}
blockquote{{margin:20px 0;padding:14px 16px;border-left:3px solid var(--accent);background:color-mix(in srgb,var(--line) 35%,transparent);border-radius:0 8px 8px 0}}
footer{{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);font-size:.92rem}}
</style></head><body><main>
<h1>{E(name)}</h1>
<p class="muted">The website of {E(name)} will be here.</p>
<p>To publish it, ask your agent:</p>
<blockquote>Build the website of {E(name)} and publish it on {E(domain)} as an nsite. Follow {E(skill)}</blockquote>
<p><strong>Who can publish:</strong> the belfry's admins publish directly. Any member can propose a version; an admin approves it,
and the belfry publishes it under its own key. Versions and approvals are listed at
<a href="{E(console)}/website">{E(httpx.URL(console).host)}/website</a>.</p>
<footer><a href="{E(console)}">{E(httpx.URL(console).host)}</a>: the belfry's status and members ·
<a href="{E(settings.public_url)}">join or sign in</a></footer>
</main></body></html>"""


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    async def who(request: Request) -> dict[str, Any] | None:
        services = svc(request)
        sess = read_session(services, request.cookies.get(COOKIE))
        return await run_in_threadpool(viewer, services, sess) if sess else None

    def secure(request: Request) -> bool:
        return console_url(svc(request).settings).startswith("https://")

    async def admin_form(request: Request, csrf: str) -> dict[str, Any] | None:
        me = await who(request)
        if me is None or not me["admin"] or not secrets.compare_digest(csrf or "", me["csrf"]):
            return None
        return me

    def back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
        # the message rides in a signed cookie, never in the URL (flash.py)
        return flash.redirect(app.state.services, path, msg, err)

    # sign-in: belfry.<domain>/login → pass.<domain>/me/console → belfry.<domain>/session
    @app.get("/console/login")
    async def login(request: Request, to: str = "/"):
        to = to if to.startswith("/") and not to.startswith("//") else "/"
        base = svc(request).settings.public_url.rstrip("/")
        return RedirectResponse(
            f"{base}/me/console?{urllib.parse.urlencode({'to': to})}", status_code=303
        )

    @app.get("/me/console")
    async def handoff(request: Request, to: str = "/"):
        services = svc(request)
        to = to if to.startswith("/") and not to.startswith("//") else "/"
        sess = mypass._verify(services, request.cookies.get(mypass.SESSION_COOKIE), "session")
        if sess is None:
            response = RedirectResponse("/me/login", status_code=303)
            response.set_cookie(
                NEXT_COOKIE,
                "/me/console?" + urllib.parse.urlencode({"to": to}),
                max_age=600,
                httponly=True,
                secure=mypass._secure(services),
                samesite="lax",
                path="/me",
            )
            return response
        ticket = mypass._sign(
            services, {"kind": "ticket", "partner": sess["partner"]}, TICKET_SECONDS
        )
        target = (
            console_url(services.settings)
            + "/session?"
            + urllib.parse.urlencode({"t": ticket, "to": to})
        )
        return RedirectResponse(target, status_code=303)

    @app.get("/console/session")
    async def session(request: Request, t: str = "", to: str = "/"):
        services = svc(request)
        claims = mypass._verify(services, t, "ticket")
        if claims is None:
            return RedirectResponse("/login", status_code=303)
        cookie = mypass._sign(
            services,
            {"kind": "console", "partner": claims["partner"], "csrf": secrets.token_urlsafe(24)},
            services.settings.pass_page_session_days * 86400,
        )
        to = to if to.startswith("/") and not to.startswith("//") else "/"
        response = RedirectResponse(to, status_code=303)
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=services.settings.pass_page_session_days * 86400,
            httponly=True,
            secure=secure(request),
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/console/logout")
    async def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/console", response_class=HTMLResponse)
    @app.get("/console/", response_class=HTMLResponse, include_in_schema=False)
    async def overview(request: Request):
        services = svc(request)
        me = await who(request)
        status = await status_data(services)
        mem = await run_in_threadpool(members, services)
        site = await run_in_threadpool(website_data, services)
        counts = {"members": len(mem), "admins": sum(1 for m in mem if m["admin"])}
        info = await run_in_threadpool(code_info, services.settings) if me and me["admin"] else {}
        return HTMLResponse(
            overview_html(services, status, mem if me else None, counts, site, me, info),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/console/status", response_class=HTMLResponse)
    async def status(request: Request):
        services = svc(request)
        me = await who(request)
        msg, err = flash.peek(request, services)
        data = await status_data(services)
        info = await run_in_threadpool(code_info, services.settings)
        if not (info.get("version") or {}) and data["version"]:
            # no version.json (an older host command): the changelog's last code entry
            info["version"] = {"short": data["version"], "subject": data["version_summary"]}
        code = code_html(info, me, msg, err)
        page_ = HTMLResponse(status_html(data, me, code), headers={"Cache-Control": "no-store"})
        return flash.clear(page_, request)

    @app.get("/console/monitor", response_class=HTMLResponse)
    async def monitor(request: Request):
        me = await who(request)
        if me is None:
            body = (
                "<h1>Monitor</h1><p>The community relay, live. For members: "
                '<a href="/login?to=/monitor">sign in</a>.</p>'
            )
        else:
            body = community.monitor_body()
        return HTMLResponse(
            frame("Monitor", "/monitor", me, body),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/console/monitor/stream")
    async def monitor_stream(request: Request, author: str = "", kind: str = ""):
        if await who(request) is None:
            return JSONResponse({"detail": "members only"}, status_code=401)
        from fastapi.responses import StreamingResponse

        return StreamingResponse(
            community.relay_stream(svc(request), community.monitor_filter(author, kind), request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/console/monitor/names")
    async def monitor_names(request: Request, p: str = ""):
        if await who(request) is None:
            return JSONResponse({"detail": "members only"}, status_code=401)
        keys = community.hex_keys(p)[:50]
        return JSONResponse(await run_in_threadpool(community.names_for, svc(request), keys))

    @app.get("/console/status.json")
    async def status_json(request: Request):
        services = svc(request)
        info = await run_in_threadpool(code_info, services.settings)
        return JSONResponse({**await status_data(services), "code": info.get("version") or {}})

    @app.post("/console/update")
    async def update(request: Request, csrf: str = Form("")):
        me = await admin_form(request, csrf)
        if me is None:
            return back("/status", err="Only admins can update the belfry. Sign in again.")
        services = svc(request)
        if not await run_in_threadpool(request_update, services.settings, me.get("email", "")):
            return back("/status", err="This belfry cannot update from here: run belfry update.")
        log.info("console: update asked for by %s", me.get("email"))
        return back("/status", msg="Update asked for. It starts within a few seconds.")

    @app.get("/console/changelog")
    async def changelog_redirect(request: Request):
        return RedirectResponse(
            svc(request).settings.public_url.rstrip("/") + "/changelog", status_code=302
        )

    @app.get("/console/members", response_class=HTMLResponse)
    async def members_page(request: Request):
        services = svc(request)
        msg, err = flash.peek(request, services)
        me = await who(request)
        mem = await run_in_threadpool(members, services)
        return flash.clear(
            HTMLResponse(members_html(mem, me, msg, err), headers={"Cache-Control": "no-store"}),
            request,
        )

    @app.post("/console/members/add")
    async def members_add(
        request: Request,
        csrf: str = Form(""),
        email: str = Form(""),
        name: str = Form(""),
        admin: str = Form(""),
    ):
        me = await admin_form(request, csrf)
        if me is None:
            return back(
                "/members", err="Only admins can add members. Sign in again if you are one."
            )
        try:
            added = await run_in_threadpool(add_member, svc(request), email, name, admin == "1")
        except Exception as exc:
            log.warning("console: add %s by %s failed: %s", email, me["email"], exc)
            return back(
                "/members",
                err=str(exc)[:200]
                if isinstance(exc, ValueError)
                else "Adding failed; see the logs.",
            )
        log.info("console: %s added %s (admin=%s)", me["email"], added, admin == "1")
        return back("/members", msg=f"{added} is a member now and got a sign-in link.")

    @app.post("/console/members/{partner}/edit")
    async def members_edit(
        partner: int,
        request: Request,
        csrf: str = Form(""),
        name: str = Form(""),
        admin: str = Form(""),
    ):
        me = await admin_form(request, csrf)
        if me is None:
            return back("/members", err="Only admins can edit members.")
        if partner == me["partner"] and admin != "1":
            return back("/members", err="You can't remove your own admin role; ask another admin.")
        try:
            await run_in_threadpool(edit_member, svc(request), partner, name, admin == "1")
        except ValueError as exc:
            return back("/members", err=str(exc))
        log.info("console: %s edited partner %s", me["email"], partner)
        return back("/members", msg="Saved.")

    @app.post("/console/members/{partner}/remove")
    async def members_remove(partner: int, request: Request, csrf: str = Form("")):
        me = await admin_form(request, csrf)
        if me is None:
            return back("/members", err="Only admins can remove members.")
        if partner == me["partner"]:
            return back("/members", err="You can't remove yourself; ask another admin.")
        try:
            await run_in_threadpool(remove_member, svc(request), partner)
        except ValueError as exc:
            return back("/members", err=str(exc))
        log.info("console: %s removed partner %s", me["email"], partner)
        return back("/members", msg="Removed: their pass ended.")

    @app.get("/console/website", response_class=HTMLResponse)
    async def website(request: Request):
        services = svc(request)
        msg, err = flash.peek(request, services)
        site = await run_in_threadpool(website_data, services)
        return flash.clear(
            HTMLResponse(website_html(services, site, await who(request), msg, err)), request
        )

    @app.post("/console/website/{event}/approve")
    async def website_approve(event: str, request: Request, csrf: str = Form("")):
        me = await admin_form(request, csrf)
        if me is None:
            return back("/website", err="Only admins can approve the website.")
        try:
            result = await run_in_threadpool(
                approve_website, svc(request), event, me["name"] or me["email"]
            )
        except ValueError as exc:
            return back("/website", err=str(exc))
        hp = getattr(request.app.state, "homepage", None)
        if hp is not None:
            hp._site = None  # show it now, not after the manifest cache expires
        log.info("console: %s approved website %s", me["email"], event)
        return back("/website", msg=f"Published: {result['files']} files.")

    @app.get("/console/skills/{name}.md", include_in_schema=False)
    async def skills(name: str, request: Request):
        return RedirectResponse(
            f"{svc(request).settings.public_url.rstrip('/')}/skills/{name}.md", status_code=302
        )


API_V1 = "/api/v1"  # the belfry's API, on belfry.<domain> (pass.<domain>/api/… still answers)


def map_host(request: Request) -> Response | None:
    """On the console host, its paths live under /console; elsewhere /console/* moves there."""
    settings = request.app.state.services.settings
    host = console_host(settings)
    if not host:
        return None
    path = request.scope["path"]
    if request.headers.get("host", "").lower() == host:
        # the belfry's API: belfry.<domain>/api/v1/<name> is the endpoint /api/<name>
        if path.startswith(API_V1 + "/"):
            request.scope["path"] = "/api/" + path.removeprefix(API_V1 + "/")
            request.scope["raw_path"] = request.scope["path"].encode()
            return None
        # invites and the mention-mail page are links people get: they live here, not on pass.
        if not path.startswith(
            ("/console", "/brand/", "/me/", "/theme/", "/static/", "/invite/", "/mentions/")
        ):
            request.scope["path"] = "/console" + (path if path != "/" else "")
        return None
    if path.startswith("/console"):
        rest = path.removeprefix("/console") or "/"
        query = ("?" + request.url.query) if request.url.query else ""
        return RedirectResponse(console_url(settings) + rest + query, status_code=302)
    return None


__all__ = ["register", "map_host", "placeholder_html", "console_url", "json"]
