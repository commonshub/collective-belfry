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

from . import community, core, flash, homepage, mypass, pages, steward
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
            # keep the last known admins and ask again after the same pause, not on every page
            _admins["at"] = now
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
    admin = bool(email) and email in admin_emails(svc)
    return {
        "partner": pid,
        "name": partner.get("name") or "",
        "email": email,
        "admin": admin,
        "csrf": sess.get("csrf", ""),
        "agents_waiting": agents_waiting(svc) if admin else 0,
    }


def agents_waiting(svc: Services) -> int:
    """How many agents ask to join (the badge on Agents in the menu); 0 when unknown."""
    from . import agentrequests

    try:
        if not agentrequests.own(svc):
            return 0
        return len(svc.odoo.agent_requests("pending"))
    except Exception:  # noqa: BLE001 — a badge is not worth a failed page
        return 0


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
.btn2{display:inline-block;padding:9px 14px;border-radius:10px;border:1px solid var(--border-strong,var(--border));
background:var(--surface);color:var(--fg);text-decoration:none;font-weight:600;font-size:.92rem}
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

# ── the frame: a side menu (Xavier, 2026-11-14: "The admin side needs a redesign to be more
# usable on mobile. Make use of a left side menu bar.") ─────────────────────────────────────────
# Wide screens (≥900px): the menu stays on the left. Phones: a top bar with ☰ opens the same menu
# as a drawer. The drawer is a checkbox and labels, so it works without script; a small inline
# script adds Esc, keyboard opening, focus moving in and back, and makes the page behind inert.
# Grouped like the settings design (canvas Settings-Wide): Community, People, Belfry.
ICONS = {
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    "website": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z"/>',
    "members": '<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0M17 11a3 3 0 1 0 0-6M22 21a6 6 0 0 0-4-5.6"/>',
    "agents": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9.5 17h5"/>',
    "status": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "monitor": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "apps": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "setup": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "close": '<path d="M18 6 6 18M6 6l12 12"/>',
    "signout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
}

# (section, [(path, label, icon, who may see it: "admin" | "member")])
SECTIONS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Community", [("/", "Home", "home", "member"), ("/website", "Website", "website", "admin")]),
    (
        "People",
        [("/members", "Members", "members", "member"), ("/agents", "Agents", "agents", "admin")],
    ),
    (
        "Belfry",
        [
            ("/status", "Status", "status", "member"),
            ("/monitor", "Monitor", "monitor", "member"),
            ("/apps", "Apps", "apps", "admin"),
            ("/setup", "Setup", "setup", "admin"),
        ],
    ),
]


def icon(name: str, size: int = 20) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
        f'focusable="false">{ICONS[name]}</svg>'
    )


def menu_items(who: dict[str, Any]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """The sections and pages this person may open: admins everything, members Home, Members,
    Status and Monitor (the pages they could open before, behind the Manage row)."""
    out = []
    for section, items in SECTIONS:
        mine = [(h, t, i) for h, t, i, need in items if need == "member" or who.get("admin")]
        if mine:
            out.append((section, mine))
    return out


def initial(name: str) -> str:
    return (name.strip()[:1] or "?").upper()


def brand_mark() -> tuple[str, str]:
    """(logo URL, name): the branding's uploaded logo wins over the theme's mark (which is drawn
    in the branding's colours, theme.mark_svg)."""
    from . import branding, theme

    t = theme.current()
    logo = branding.cached().get("logo") or t.logo
    return logo, t.name


def logo_img(alt: str = "", cls: str = "") -> str:
    """The belfry's logo; an uploaded one that does not load (another host blocked, gone) falls
    back to the theme's mark."""
    from . import theme

    logo, _name = brand_mark()
    mark = theme.current().logo
    fallback = (
        f" onerror=\"this.onerror=null;this.src='{E(mark)}'\""
        if logo != mark and "'" not in mark
        else ""
    )
    klass = f' class="{cls}"' if cls else ""
    return f'<img{klass} src="{E(logo)}" alt="{E(alt)}"{fallback}>'


def topbar(who: dict[str, Any] | None) -> str:
    """The bar for visitors (not signed in): the belfry's name and Sign in; no menu."""
    from . import theme

    t = theme.current()
    logo, name = brand_mark()
    brand = (
        logo_img(name)
        if t.logo_is_wordmark and logo == t.logo
        else f"{logo_img()}<span>{E(name)}</span>"
    )
    if who:
        right = '<a href="/logout" class="who">Sign out</a>'
    else:
        right = '<a class="btn" href="/login">Sign in</a>'
    return f'<div class="bar"><a class="brand" href="/">{brand}</a><span class="right">{right}</span></div>'


def side_menu(current: str, who: dict[str, Any]) -> str:
    _logo, name = brand_mark()
    groups = ""
    for section, items in menu_items(who):
        links = ""
        for h, t, i in items:
            on = ' aria-current="page"' if h == current else ""
            badge = ""
            if h == "/agents" and int(who.get("agents_waiting") or 0):
                n = int(who["agents_waiting"])
                badge = f'<span class="badge" aria-label="{n} waiting">{n}</span>'
            links += f'<a href="{h}"{on}>{icon(i)}<span class="lbl">{t}</span>{badge}</a>'
        groups += f'<p class="sec">{section}</p>{links}'
    role = " · admin" if who.get("admin") else ""
    return f"""<aside class="side" id="side" aria-label="Menu">
<div class="side-top"><a class="side-brand" href="/">{logo_img()}<span>{E(name)}</span></a>
<label for="nav-open" class="icon-btn close" aria-label="Close the menu" tabindex="0" role="button">{icon("close")}</label></div>
<nav class="side-nav" aria-label="Manage">{groups}</nav>
<div class="side-me"><span class="av" aria-hidden="true">{E(initial(who.get("name", "")))}</span>
<span class="me-name"><b>{E(who.get("name", ""))}</b><small>{E(who.get("email", ""))}{role}</small></span>
<a href="/logout" class="icon-btn" title="Sign out" aria-label="Sign out">{icon("signout")}</a></div>
</aside>"""


SHELL_STYLE = """<style>
main{max-width:none;padding:0;margin:0} header.site{display:none}
input,select,textarea{font-size:16px}
.shell{min-height:100vh;min-height:100dvh}
.nav-toggle{position:fixed;opacity:0;pointer-events:none;width:1px;height:1px;left:-99px}
.side{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;
gap:2px;padding:20px 12px 12px;box-sizing:border-box}
.side-top{display:flex;align-items:center;gap:8px;padding:0 6px 14px}
.side-brand{display:flex;align-items:center;gap:10px;flex:1;min-width:0;text-decoration:none;color:var(--fg);
font:700 1.05rem/1.2 var(--font-title)}
.side-brand img{width:32px;height:32px;object-fit:contain;border-radius:8px;flex:none}
.side-brand span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.side-nav{display:flex;flex-direction:column;gap:2px;flex:1}
.side .sec{margin:0;padding:14px 10px 4px;font-size:.75rem;font-weight:600;letter-spacing:.04em;
text-transform:uppercase;color:var(--fg-faint)}
.side .sec:first-child{padding-top:4px}
.side-nav a{display:flex;align-items:center;gap:12px;padding:9px 10px;border-radius:8px;text-decoration:none;
color:var(--fg-muted);font-size:.95rem;min-height:24px}
.side-nav a:hover{background:var(--surface-deep);color:var(--fg)}
.side-nav a[aria-current=page]{background:var(--surface-deep);color:var(--fg);font-weight:600;
box-shadow:inset 3px 0 0 var(--accent)}
.side-nav .lbl{flex:1}
.badge{font-size:.75rem;font-weight:600;color:#fff;background:var(--accent-2,#b91c1c);border-radius:99px;
padding:1px 7px;line-height:1.4}
.side-me{display:flex;align-items:center;gap:10px;padding:12px 6px 4px;margin-top:12px;border-top:1px solid var(--border)}
.side-me .me-name{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.25}
.side-me b{font-size:.92rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.side-me small{font-size:.8rem;color:var(--fg-faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.av{width:32px;height:32px;border-radius:99px;background:var(--surface-deep);border:1px solid var(--border);
display:inline-grid;place-items:center;font-weight:600;color:var(--fg);flex:none}
.icon-btn{width:40px;height:40px;display:inline-grid;place-items:center;border-radius:10px;color:var(--fg-muted);
cursor:pointer;flex:none;text-decoration:none}
.icon-btn:hover{background:var(--surface-deep);color:var(--fg)}
.icon-btn:focus-visible,.side-nav a:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.mbar{display:none}
.content{min-width:0;display:flex;flex-direction:column}
.page{flex:1;width:100%;max-width:1040px;box-sizing:border-box;margin:0 auto;padding:32px 32px 24px}
.content > footer{max-width:1040px;width:100%;box-sizing:border-box;margin:0 auto;padding:0 32px 32px}
.tscroll{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
.tscroll table{min-width:520px} .ph-only{display:none}
@media (max-width:640px){
.tscroll table.list{min-width:0} table.list tr:first-child:has(th){display:none}
table.list,table.list tbody,table.list tr,table.list td{display:block;width:auto}
table.list tr{border-bottom:1px solid var(--border);padding:10px 0}
table.list td{border:0;padding:2px 0} table.list td:empty{display:none} .ph-only{display:inline}
}
.panel{overflow-wrap:anywhere}
@media (min-width:900px){
.shell{display:grid;grid-template-columns:260px minmax(0,1fr)}
.side{position:sticky;top:0;height:100vh;height:100dvh;overflow-y:auto}
.side .close,.shade{display:none}
}
@media (max-width:899.98px){
.mbar{display:flex;align-items:center;gap:6px;position:sticky;top:0;z-index:20;padding:8px 10px;
background:var(--bg);border-bottom:1px solid var(--border)}
.mbar b{flex:1;min-width:0;font-size:1.05rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mbar .mlogo{width:28px;height:28px;object-fit:contain;border-radius:6px}
.side{position:fixed;z-index:40;top:0;bottom:0;left:0;width:min(300px,86vw);overflow-y:auto;
transform:translateX(-102%);transition:transform .25s cubic-bezier(.2,0,0,1),visibility 0s .25s;visibility:hidden;
box-shadow:0 0 40px rgba(0,0,0,.25)}
.shade{position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.45);opacity:0;pointer-events:none;
transition:opacity .2s}
.nav-toggle:checked ~ .side{transform:none;visibility:visible;transition:transform .25s cubic-bezier(.2,0,0,1)}
.nav-toggle:checked ~ .shade{opacity:1;pointer-events:auto}
body:has(.nav-toggle:checked){overflow:hidden}
.page{padding:20px 16px 16px} .content > footer{padding:0 16px 24px}
.panel{padding:18px 16px;border-radius:12px}
.hero{padding:16px 0 20px} .hero h1{font-size:2.2rem}
.stats{grid-template-columns:1fr 1fr} .stat{border-right:0;border-bottom:1px solid var(--border);padding:14px 16px}
.stat:nth-child(odd){border-right:1px solid var(--border)} .stat b{font-size:1.2rem}
.strip .head{padding:12px 16px} .steps{padding:12px 16px}
}
@media (prefers-reduced-motion:reduce){.side,.shade{transition:none!important}}
</style>"""

# the drawer without script is the checkbox; this adds Esc, Enter/Space on the labels, focus into
# the menu and back to ☰, and keeps the page behind out of reach while it is open
SHELL_SCRIPT = """<script>
(function(){var t=document.getElementById('nav-open');if(!t)return;
var side=document.getElementById('side'),content=document.querySelector('.content'),
btn=document.querySelector('.mbar .menu-btn');
function sync(){var open=t.checked&&getComputedStyle(side).position==='fixed';
if(btn)btn.setAttribute('aria-expanded',open?'true':'false');
if(content){if(open)content.setAttribute('inert','');else content.removeAttribute('inert');}
if(open){var f=side.querySelector('[aria-current=page]')||side.querySelector('a');if(f)f.focus();}}
t.addEventListener('change',function(){sync();if(!t.checked&&btn)btn.focus();});
document.querySelectorAll('label[for=nav-open][role=button]').forEach(function(l){
l.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();l.click();}});});
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&t.checked){t.checked=false;
t.dispatchEvent(new Event('change'));}});
window.addEventListener('pageshow',function(){if(t.checked){t.checked=false;sync();}});
})();
</script>"""


def _scroll_tables(body: str) -> str:
    """Wide tables scroll inside their own box, never the page (375px phones)."""
    return body.replace("<table", '<div class="tscroll"><table').replace(
        "</table>", "</table></div>"
    )


def frame(title: str, current: str, who: dict[str, Any] | None, body: str) -> str:
    """A console page. Signed-in people get the side menu (a drawer on phones); visitors the bar
    with Sign in. Fixed text, the settings and the viewer only: nothing from the URL."""
    from . import theme
    from .claim import TEST_NOTICE

    inner = body if current == "/" else f'<div class="panel">{_scroll_tables(body)}</div>'
    t = theme.current()
    links = "".join(f'<a href="{E(h)}">{E(text)}</a>' for h, text in t.links)
    foot = f"<footer>{E(t.footer)}<nav>{links}</nav></footer>"
    if who is None:
        page_ = f'<div class="shell"><div class="content"><div class="page">{topbar(None)}{inner}</div>{foot}</div></div>'
        return theme.frame(title, STYLE + HOME_STYLE + SHELL_STYLE + page_, TEST_NOTICE, bare=True)
    shown = title if current != "/" else "Home"
    page_ = f"""<div class="shell">
<input type="checkbox" id="nav-open" class="nav-toggle" tabindex="-1" aria-hidden="true">
<label for="nav-open" class="shade" aria-hidden="true"></label>
{side_menu(current, who)}
<div class="content">
<header class="mbar"><label for="nav-open" class="icon-btn menu-btn" role="button" tabindex="0"
aria-label="Open the menu" aria-controls="side" aria-expanded="false">{icon("menu")}</label>
{logo_img(cls="mlogo")}<b>{E(shown)}</b></header>
<div class="page">{inner}</div>{foot}</div></div>{SHELL_SCRIPT}"""
    return theme.frame(title, STYLE + HOME_STYLE + SHELL_STYLE + page_, TEST_NOTICE, bare=True)


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
    no_admin: bool = False,
    agents_waiting: int = 0,
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
    if who is None and no_admin:
        body = f"""<section class="hero"><p class="eyebrow">A new belfry</p>
<h1>{E(name)}</h1>
<p class="muted" style="margin:0">This belfry has no admin yet. The first person to open the setup
becomes its admin, then invites the others.</p>
<div class="row"><a class="btn" href="/setup">Set up this belfry</a></div></section>
<div class="panel"><h2 style="margin:0 0 8px;font-size:1rem">Or let your agent help</h2>
<p class="muted" style="margin:0">An agent can ask to join now and set up the channels once you
let it in: give it <a href="/agents.md">{E(console_url(s))}/agents.md</a>.</p></div>
{foot}"""
        return frame(name, "/", None, body)
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
        if agents_waiting:
            steps.append(
                (
                    f"{agents_waiting} agent{'s' if agents_waiting > 1 else ''} asking to join.",
                    "/agents",
                    "Review",
                )
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
            f'<tr><td class="who-cell"><b>{E(m["name"])}</b></td>{email}'
            f"<td>{'admin' if m['admin'] else 'member'}</td>"
            f'<td class="meta"><span class="ph-only">since </span>{E(m["since"])}</td>{actions}</tr>'
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
<h2>Everyone</h2><table class="list">{head}{"".join(rows)}</table>""",
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
<code>{E(kind)}:{E(pk[:8])}…:{E(d)}</code></div>{approve}</div>"""
        )
    note = (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
        f'<p class="bad">{E(err)}</p>' if err else ""
    )
    skill = f"{pages.skills_base(s)}/skills/belfry-homepage.md"
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


PLACEHOLDER_STYLE = """<style>
main{max-width:680px;padding-top:56px}
.ph{text-align:center}
.ph .logo{width:160px;height:160px;object-fit:contain;border-radius:28px;margin:0 auto 24px;display:block}
.ph h1{font-size:2.6rem;line-height:1.1;margin:0 0 12px}
.ph .lead{font-size:1.15rem;margin:0 auto 28px;color:var(--fg-muted);max-width:34rem}
.ph .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:center;margin:0 0 8px}
.ph .card{margin:32px 0 0;text-align:left}
.ph h2{font-size:1.05rem;margin:0 0 8px}
.ph blockquote{margin:12px 0;padding:12px 14px;border-left:3px solid var(--accent);
background:var(--surface-deep);border-radius:0 8px 8px 0;word-break:break-word}
.ph .small{font-size:.9rem}
.ph .foot{margin-top:40px}
</style>"""


def placeholder_html(settings: Any, svc: Services | None = None) -> str:
    """What the apex shows before a website is published (HOMEPAGE=nsite, no manifest yet), in
    the belfry's look (theme.frame). The logo in big (the branding's, else Belfry's mark), the
    community's name and description (the branding's ``description``, else a default line), and
    a link to the chat. With no admin yet, a "Set up this belfry" button and the agent route
    instead of the chat link. Fixed text plus the settings and branding; nothing from the
    request; no script needed."""
    from . import agentrequests, appshome, branding, setup, theme

    name = settings.community_name or "This community"
    domain = httpx.URL(settings.site_url).host if settings.site_url else "this domain"
    console = console_url(settings) or settings.public_url
    host = httpx.URL(console).host if console else ""
    agents_md = agentrequests.instructions_url(settings) or f"{console}/agents.md"
    brand: dict[str, str] = {}
    no_admin = False
    if svc is not None:
        try:
            brand = branding.current(svc)
        except Exception:  # noqa: BLE001 — the defaults then
            brand = {}
        try:
            no_admin = not setup.admin_known(svc)
        except Exception:  # noqa: BLE001 — unsure: show the ordinary placeholder
            no_admin = False
    # same origin as the page (the apex serves /theme/*), or the branding's https logo
    logo = brand.get("logo") or theme.current().logo
    about = brand.get("description") or branding.DEFAULT_DESCRIPTION
    head = f"""<img class="logo" src="{E(logo)}" alt="">
<h1>{E(name)}</h1>
<p class="lead">{E(about)}</p>"""
    if no_admin:
        body = f"""<div class="ph">{head}
<div class="row"><a class="btn" href="{E(console)}/setup">Set up this belfry</a></div>
<p class="muted small">This belfry has no admin yet. The first person to open the setup becomes
its admin, then invites the others. No code needed.</p>
<div class="card"><h2>Or let your agent do it</h2>
<p class="muted">An agent can ask to join now, and set up the channels once you let it in. Give
it this:</p>
<blockquote>Join the belfry at {E(domain)} as my agent: follow {E(agents_md)}</blockquote>
<p class="muted small">Then open the setup yourself: its request waits for you.</p></div></div>"""
    else:
        chat = appshome.app_url(settings, "chat")
        body = f"""<div class="ph">{head}
<div class="row"><a class="btn" href="{E(chat)}">Open the chat</a></div>
<p class="small foot muted"><a href="{E(console)}">{E(host)}</a> · the belfry's members,
status and settings</p></div>"""
    return theme.frame(name, PLACEHOLDER_STYLE + body)


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
        from . import agentrequests, setup

        no_admin = me is None and not await run_in_threadpool(setup.admin_known, services)
        waiting = 0
        if me and me["admin"] and agentrequests.own(services):
            waiting = len(await run_in_threadpool(services.odoo.agent_requests, "pending"))
        return HTMLResponse(
            overview_html(
                services,
                status,
                mem if me else None,
                counts,
                site,
                me,
                info,
                no_admin=no_admin,
                agents_waiting=waiting,
            ),
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
        if (
            not path.startswith(
                (
                    "/console",
                    "/brand/",
                    "/me/",
                    "/theme/",
                    "/static/",
                    "/invite/",
                    "/mentions/",
                    "/skills/",
                )
            )
            and path != "/skill.md"
        ):
            request.scope["path"] = "/console" + (path if path != "/" else "")
        return None
    # the skills live on belfry.<domain>: old links to pass.<domain>/skills/… move there
    if path.startswith("/skills/") or path == "/skill.md":
        return RedirectResponse(console_url(settings) + path, status_code=301)
    if path.startswith("/console"):
        rest = path.removeprefix("/console") or "/"
        query = ("?" + request.url.query) if request.url.query else ""
        return RedirectResponse(console_url(settings) + rest + query, status_code=302)
    return None


__all__ = ["register", "map_host", "placeholder_html", "console_url", "json"]
