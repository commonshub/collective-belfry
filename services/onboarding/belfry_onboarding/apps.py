"""Which apps may open sessions for members, as Nostr events on the community relay.

An app that wants members to sign in with their belfry account (``Sign in with <domain>``)
needs an admin's approval first. Asking, approving and withdrawing are events, each signed with
the key of the person who did it (the signer holds members' keys), published on the community
relay and so visible at ``belfry.<domain>/monitor``:

- kind 8550, *app approval request*: a member who tried to sign in to an app nobody approved yet.
  Signed by the member.
- kind 8551, *app approved*: an admin approved the app; from then on any member can open a session
  with it. Signed by the admin; ``e`` tags name the requests it answers.
- kind 8552, *app approval withdrawn*: an admin withdrew it. Signed by the admin.

Tags on all three: ``["r", <app>]`` (the app: its sign-in return address, ``redirect_uri``
without query or fragment), ``["client", <name>]``, ``["p", <belfry pubkey>]`` and ``["alt", …]``
(NIP-31). An app is approved when the latest 8551/8552 for it, among events signed by current
admins, is an 8551. Admins are the stewards group; an approval by someone who is no longer an
admin no longer counts.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import urllib.parse
from typing import Any

from fastapi import Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from . import community, console, flash, mypass
from .core import Services

log = logging.getLogger(__name__)

REQUEST, APPROVE, WITHDRAW = 8550, 8551, 8552
KIND_NAMES = {
    REQUEST: "app approval request",
    APPROVE: "app approved",
    WITHDRAW: "app approval withdrawn",
}
CACHE_SECONDS = 20
MAX_NAME = 60


def app_uri(raw: str) -> str:
    """The app's identity: its return address without query or fragment. https only (http for
    localhost, to develop an app). Raises ValueError."""
    u = urllib.parse.urlsplit((raw or "").strip())
    local = u.hostname in ("localhost", "127.0.0.1")
    if u.scheme != "https" and not (u.scheme == "http" and local):
        raise ValueError("an app's address must be https://")
    if not u.hostname or u.username or u.password or len(raw) > 500:
        raise ValueError("not an app address")
    host = u.hostname + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit((u.scheme, host, u.path or "/", "", ""))


def address(uri: str) -> str:
    """How an app is shown: host and path (``apps.belfry.sh/chat``). Never a name the app gives
    itself in the sign-in URL: anyone can write any name there."""
    u = urllib.parse.urlsplit(uri)
    return ((u.netloc or "") + (u.path or "")).rstrip("/") or uri


def app_name(name: str, uri: str) -> str:
    """NAME is only ever one an admin typed (the Apps tab); otherwise the address."""
    name = " ".join((name or "").split())[:MAX_NAME]
    return name or address(uri)


def belfry_pubkey(svc: Services) -> str:
    key = community.belfry_key(svc.settings)
    return key.public_key_xonly.format().hex() if key else ""


# ── members ──────────────────────────────────────────────────────────────────────────────────
def member_by_email(svc: Services, email: str) -> dict[str, Any] | None:
    """The member with this e-mail address and an active pass: partner, pubkey, name, email."""
    email = email.strip().lower()
    if "@" not in email:
        return None
    partners = svc.odoo.search_read(
        "res.partner", [["email", "=ilike", email]], ["name", "email"], limit=5
    )
    for p in partners:
        rows = svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["partner_id", "=", p["id"]]],
            ["keycast_pubkey"],
            limit=1,
        )
        if rows and rows[0].get("keycast_pubkey"):
            return {
                "partner": p["id"],
                "pubkey": rows[0]["keycast_pubkey"],
                "name": p.get("name") or email.split("@")[0],
                "email": (p.get("email") or email).lower(),
            }
    return None


def member_by_partner(svc: Services, partner: int) -> dict[str, Any] | None:
    rows = svc.odoo.read("res.partner", [partner], ["name", "email"])
    if not rows:
        return None
    m = member_by_email(svc, rows[0].get("email") or "")
    return m if m and m["partner"] == partner else None


def is_admin(svc: Services, member: dict[str, Any]) -> bool:
    return bool(member.get("email")) and member["email"].lower() in console.admin_emails(svc)


_admin_keys: dict[str, Any] = {"at": 0.0, "keys": set()}


def admin_pubkeys(svc: Services) -> set[str]:
    now = time.monotonic()
    if now - _admin_keys["at"] > CACHE_SECONDS:
        keys = set()
        for email in console.admin_emails(svc):
            m = member_by_email(svc, email)
            if m:
                keys.add(m["pubkey"])
        _admin_keys.update(at=now, keys=keys)
    return _admin_keys["keys"]


# ── the events ───────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "events": None}


def forget() -> None:
    _cache["at"] = 0.0
    _admin_keys["at"] = 0.0


def events(svc: Services) -> list[dict[str, Any]]:
    """All app events addressed to this belfry, newest first (cached a few seconds)."""
    with _lock:
        if _cache["events"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["events"]
    me = belfry_pubkey(svc)
    found = (
        community.query(svc, [{"kinds": [REQUEST, APPROVE, WITHDRAW], "#p": [me], "limit": 2000}])
        if me
        else []
    )
    found = sorted(
        (e for e in found if tag(e, "r")), key=lambda e: (e["created_at"], e["id"]), reverse=True
    )
    with _lock:
        _cache.update(at=time.monotonic(), events=found)
    return found


def tag(event: dict[str, Any], name: str) -> str:
    return next((t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == name), "")


def state(svc: Services) -> dict[str, dict[str, Any]]:
    """Every app the belfry has heard of: {uri: {name, approved, by, at, decided, requests}}.
    ``requests``: pubkeys that asked since the last decision."""
    admins = admin_pubkeys(svc)
    apps: dict[str, dict[str, Any]] = {}
    for e in events(svc):  # newest first
        uri = tag(e, "r")
        a = apps.setdefault(
            uri,
            {
                "uri": uri,
                "name": tag(e, "client") or uri,
                "approved": False,
                "decided": None,
                "requests": {},
                "last": e["created_at"],
            },
        )
        if e["kind"] in (APPROVE, WITHDRAW):
            if e["pubkey"] in admins and a["decided"] is None:
                a["decided"] = e
                a["approved"] = e["kind"] == APPROVE
                if tag(e, "client"):
                    a["name"] = tag(e, "client")
        elif e["kind"] == REQUEST and a["decided"] is None:
            a["requests"].setdefault(e["pubkey"], e)
    return apps


def display_name(svc: Services, uri: str) -> str:
    """The name an admin gave the app when approving it, else its address."""
    a = state(svc).get(uri) or {}
    return app_name(tag(a["decided"], "client") if a.get("approved") else "", uri)


def is_approved(svc: Services, uri: str) -> bool:
    return bool(state(svc).get(uri, {}).get("approved"))


def publish(
    svc: Services,
    partner: int,
    kind: int,
    uri: str,
    name: str,
    extra: list[list[str]] | None = None,
    content: str = "",
) -> dict[str, Any]:
    """Sign an app event with this member's own key (through the signer) and publish it on the
    community relay. Raises ValueError for a member whose key the signer does not hold."""
    signer = community.member_signer(svc, partner)
    if signer is None:
        raise ValueError("Your key is your own: the belfry cannot sign for you.")
    _, sign = signer
    me = belfry_pubkey(svc)
    if not me or svc.relay is None:
        raise RuntimeError("no belfry key or community relay")
    event = sign(
        {
            "kind": kind,
            "created_at": int(time.time()),
            "content": content,
            "tags": [
                ["r", uri],
                ["client", app_name(name, uri)],
                ["p", me],
                ["alt", KIND_NAMES[kind].capitalize() + f": {app_name(name, uri)}"],
                *(extra or []),
            ],
        }
    )
    # the member authenticates to the relay (NIP-42) as themself: it only stores what a member
    # with an active pass signs
    svc.relay.publish(event, sign)
    forget()
    log.info("apps: %s %s by %s", KIND_NAMES[kind], uri, event["pubkey"][:12])
    return event


def request(svc: Services, member: dict[str, Any], uri: str, name: str) -> dict[str, Any]:
    """The member asks the admins to approve the app (once: an open request is reused)."""
    a = state(svc).get(uri)
    if a and member["pubkey"] in a["requests"]:
        return {"event": a["requests"][member["pubkey"]], "count": len(a["requests"])}
    who = member.get("name") or "A member"
    event = publish(
        svc,
        member["partner"],
        REQUEST,
        uri,
        name,
        content=f"{who} asks the admins to approve {app_name(name, uri)} ({uri}), so members can "
        "open sessions with it using their own account.",
    )
    count = len((a or {}).get("requests") or {}) + 1
    return {"event": event, "count": count}


def decide(
    svc: Services, admin: dict[str, Any], uri: str, name: str, approve: bool
) -> dict[str, Any]:
    if not is_admin(svc, admin):
        raise PermissionError("Only admins can approve apps.")
    a = state(svc).get(uri) or {}
    refs = [["e", e["id"]] for e in (a.get("requests") or {}).values()][:50]
    verb = "approved" if approve else "withdrew the approval of"
    event = publish(
        svc,
        admin["partner"],
        APPROVE if approve else WITHDRAW,
        uri,
        name or a.get("name", ""),
        extra=refs,
        content=f"{admin.get('name') or 'An admin'} {verb} {app_name(name or a.get('name', ''), uri)} "
        f"({uri}).",
    )
    return event


# ── belfry.<domain>/apps: the admins' list ───────────────────────────────────────────────────
def when(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) + " UTC"


def apps_html(svc: Services, who: dict[str, Any] | None) -> str:
    import html

    E = html.escape
    if who is None:
        return console.frame(
            "Apps",
            "/apps",
            None,
            '<h1>Apps</h1><p class="muted">Apps members can open sessions in with their own account. '
            '<a class="btn" href="/login?to=/apps">Sign in</a></p>',
        )
    everything = sorted(state(svc).values(), key=lambda a: a["last"], reverse=True)
    waiting = [a for a in everything if not a["approved"] and a["requests"]]
    approved = [a for a in everything if a["approved"]]
    names = community.names_for(
        svc,
        list(
            {pk for a in waiting for pk in a["requests"]}
            | {a["decided"]["pubkey"] for a in approved}
        ),
    )
    csrf = E(who["csrf"])

    def form(
        a: dict[str, Any], action: str, label: str, primary: bool, with_name: bool = False
    ) -> str:
        name = (
            f'<input type="text" name="name" maxlength="{MAX_NAME}" placeholder="Name shown to members (optional)" '
            'style="max-width:260px">'
            if with_name
            else ""
        )
        return (
            f'<form method="post" action="/apps/{action}" class="row" style="gap:8px;margin:8px 0 0">'
            f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="uri" value="{E(a["uri"])}">'
            f'{name}<button type="submit" class="{"btn" if primary else ""}">{label}</button></form>'
        )

    rows_wait = (
        "".join(
            f'<div class="tile" style="margin:0 0 10px"><strong>{E(address(a["uri"]))}</strong>'
            f'<div class="meta">{E(a["uri"])} · asked by {len(a["requests"])} member(s): '
            f"{E(', '.join(names.get(pk, pk[:8]) for pk in list(a['requests'])[:5]))}</div>"
            + (
                form(a, "approve", "Approve", True, True) + form(a, "decline", "Decline", False)
                if who["admin"]
                else ""
            )
            + "</div>"
            for a in waiting
        )
        or '<p class="meta">No app is waiting.</p>'
    )
    rows_ok = (
        "".join(
            f"<tr><td><strong>{E(app_name(tag(a['decided'], 'client'), a['uri']))}</strong><br>"
            f'<span class="meta">{E(a["uri"])}</span></td>'
            f'<td class="meta">by {E(names.get(a["decided"]["pubkey"], a["decided"]["pubkey"][:8]))}<br>{when(a["decided"]["created_at"])}</td>'
            f"<td>{form(a, 'withdraw', 'Withdraw', False) if who['admin'] else ''}</td></tr>"
            for a in approved
        )
        or '<tr><td class="meta">No app approved yet.</td></tr>'
    )
    return console.frame(
        "Apps",
        "/apps",
        who,
        f"""<h1>Apps</h1>
<p class="muted">Apps members can open sessions in with their own {E(svc.settings.nip05_domain)} account
("Sign in with {E(svc.settings.nip05_domain)}"). Only admins approve an app. Requests and decisions are signed Nostr events on the community relay, kinds 8550–8552: see them
live on <a href="{E(svc.settings.public_url.rstrip("/"))}/monitor">the monitor</a>.</p>
<h2>Waiting · {len(waiting)}</h2>{rows_wait}
<h2>Approved · {len(approved)}</h2><table>{rows_ok}</table>""",
    )


def register(app: Any) -> None:

    def svc(request: Request) -> Services:
        return request.app.state.services

    async def viewer(request: Request) -> dict[str, Any] | None:
        services = svc(request)
        sess = console.read_session(services, request.cookies.get(console.COOKIE))
        return await run_in_threadpool(console.viewer, services, sess) if sess else None

    @app.get("/console/apps", response_class=HTMLResponse)
    async def apps_page(request: Request):
        services = svc(request)
        who = await viewer(request)
        msg, err = flash.peek(request, services)
        body = await run_in_threadpool(apps_html, services, who)
        note = (f'<p class="ok">{_e(msg)}</p>' if msg else "") + (
            f'<p class="bad">{_e(err)}</p>' if err else ""
        )
        body = body.replace("<h1>Apps</h1>", "<h1>Apps</h1>" + note, 1)
        return flash.clear(HTMLResponse(body, headers={"Cache-Control": "no-store"}), request)

    async def decided(request: Request, csrf: str, uri: str, name: str, approve: bool, verb: str):
        services = svc(request)
        who = await viewer(request)
        origin = request.headers.get("origin")
        if (
            who is None
            or not who["admin"]
            or not secrets.compare_digest(csrf or "", who["csrf"] or "x")
            or (origin and origin != mypass._origin(console.console_url(services.settings)))
        ):
            return flash.redirect(services, "/apps", err="Only admins can approve apps.")
        try:
            uri = app_uri(uri)
            member = await run_in_threadpool(member_by_partner, services, who["partner"])
            if member is None:
                raise ValueError("Your account has no active pass.")
            await run_in_threadpool(decide, services, member, uri, name, approve)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return flash.redirect(services, "/apps", err=str(exc))
        return flash.redirect(
            services, "/apps", msg=f"{address(uri)} {verb}. The event is on the relay."
        )

    @app.post("/console/apps/approve")
    async def approve(
        request: Request, csrf: str = Form(""), uri: str = Form(""), name: str = Form("")
    ):
        return await decided(request, csrf, uri, name, True, "approved")

    @app.post("/console/apps/decline")
    async def decline(request: Request, csrf: str = Form(""), uri: str = Form("")):
        return await decided(request, csrf, uri, "", False, "declined")

    @app.post("/console/apps/withdraw")
    async def withdraw(request: Request, csrf: str = Form(""), uri: str = Form("")):
        return await decided(request, csrf, uri, "", False, "withdrawn")


def _e(text: str) -> str:
    import html

    return html.escape(text)
