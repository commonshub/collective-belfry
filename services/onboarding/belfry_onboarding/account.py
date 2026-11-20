"""The participant's account page (replaces Keycast's web app at apps.<domain>, 2026-11-19).

    GET  /account                         pass, apps, identity, connected apps, key
    POST /account/connections             "Connect a Nostr app": a new bunker URL, shown once
    POST /account/connections/revoke      remove a connected app (id)
    POST /account/export                  the key as nsec, shown once (fresh sign-in only)
    GET  /account/self-custody/challenge  move to a key of one's own: what to sign
    POST /account/self-custody            the signed challenge (kind 27235) -> the move

Belfry's session (session.py) is required. Key export and the move need a sign-in younger than
SESSION_FRESH_SECONDS ("Sign in again to unlock"). The move proves the new key by a kind-27235
event signed with it whose ``challenge`` tag is an HMAC of the partner and an expiry (ten
minutes); the signer then drops the key it held and revokes every connection, and Odoo's
passes, npub and the relay whitelist follow (core.key_changed). Because Belfry can recompute a
derived key, self-custody always means a new key.

POSTs come from this origin only: the session cookie is SameSite=Lax, and an Origin header of
another site is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import time
import urllib.parse
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import core, session
from .claim import page, pass_info, qr_svg
from .clients.signer import SignerError, vine_id
from .core import Services

log = logging.getLogger(__name__)
E = html.escape

CHALLENGE_KIND = 27235
CHALLENGE_TTL = 600

CSS = """<style>
 .card + .card { margin-top: 16px; }
 h2 { font-size: 1.1rem; margin: 0 0 8px; }
 .apps { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); margin-top: 8px; }
 .apps a { display: block; padding: 12px; border: 1px solid var(--osv-border); border-radius: 10px; text-decoration: none; color: var(--osv-foreground); }
 .apps a strong { color: var(--osv-accent); display: block; }
 ul.conn { list-style: none; padding: 0; margin: 8px 0; } ul.conn li { display: flex; gap: 10px; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--osv-border); flex-wrap: wrap; }
 form.inline { display: inline; } button.btn, button.link { border: 0; font: inherit; cursor: pointer; }
 button.btn { font-weight: 600; } button.link { background: none; color: var(--osv-accent); padding: 0; text-decoration: underline; }
 .fb { display: grid; gap: 10px; margin-top: 8px; } .fb input[type=text] { box-sizing: border-box; width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--osv-border-strong); background: var(--osv-surface-deep); color: var(--osv-foreground); font: inherit; }
 .small { font-size: 0.85rem; } .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
</style>"""


class AccountError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def same_origin(request: Request, settings: Any) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    public = urllib.parse.urlsplit(settings.public_url)
    return origin.rstrip("/") == f"{public.scheme}://{public.netloc}"


def community_apps(settings: Any) -> list[tuple[str, str, str]]:
    apps = []
    for entry in (settings.community_apps or "").split(";"):
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            apps.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return apps


def load(svc: Services, partner_id: int) -> dict[str, Any]:
    partner = svc.odoo.read_one(
        "res.partner",
        partner_id,
        [
            "name",
            "email",
            "belfry_member_number",
            "belfry_npub",
            "belfry_key_custody",
            "belfry_steward",
        ],
    )
    if partner is None:
        raise AccountError(404, "This account does not exist any more.")
    account = svc.signer.account(vine_id(partner_id))
    connections = svc.signer.connections(vine_id(partner_id)) if account else []
    return {
        "partner": partner,
        "pass": pass_info(svc, partner_id),
        "account": account,
        "connections": connections,
    }


def _when(ts: Any) -> str:
    try:
        return time.strftime("%d %b %Y", time.gmtime(int(ts)))
    except (TypeError, ValueError):
        return ""


def account_page(svc: Services, data: dict[str, Any], fresh: bool, notice: str = "") -> str:
    s = svc.settings
    partner = data["partner"]
    info = data["pass"] or {}
    account = data["account"] or {}
    own = account.get("custody") == "own" or partner.get("belfry_key_custody") == "self"
    name = partner.get("name") or ""
    parts: list[str] = []
    note = f'<div class="card"><p class="ok">{E(notice)}</p></div>' if notice else ""

    # the pass
    if info:
        dates = (
            f"{E(info['date_start'])} to {E(info['date_end'])}" if info.get("date_start") else ""
        )
        links = []
        if info.get("show_url"):
            links.append(f'<a class="btn" href="{E(info["show_url"])}">Show your pass</a>')
        if info.get("claim_url"):
            links.append(f'<a href="{E(info["claim_url"])}">Add it to a wallet app</a>')
        if info.get("tokens_url"):
            links.append(
                f'<a href="{E(info["tokens_url"])}">Your {int(info.get("tokens") or 0)} village tokens</a>'
            )
        state = "active" if info.get("state") == "active" else E(str(info.get("state") or ""))
        parts.append(f"""<div class="card"><h2>Your pass</h2>
<p><strong>{E(info.get("tier_name") or "")}</strong> pass, {state}{", " + dates if dates else ""}.
Member number <strong>{E(info.get("member_number") or "")}</strong>.</p>
<p class="row">{" ".join(links)}</p></div>""")

    # the apps
    apps = community_apps(s)
    if apps and not own:
        cards = "".join(
            f'<a href="{E(url)}"><strong>{E(title)}</strong><span class="muted small">{E(desc)}</span></a>'
            for title, url, desc in apps
        )
        parts.append(
            f'<div class="card"><h2>Community apps</h2><div class="apps">{cards}</div></div>'
        )

    # identity
    number = (partner.get("belfry_member_number") or "").lower()
    nip05 = f"{number}@{s.nip05_domain}" if number and s.nip05_domain else ""
    parts.append(f"""<div class="card"><h2>Your identity on Nostr</h2>
<p class="small muted">Public key</p><p><code>{E(partner.get("belfry_npub") or "")}</code></p>
{f'<p class="small muted">Name</p><p><code>{E(nip05)}</code></p>' if nip05 else ""}
<p class="small muted">{"You hold this key yourself." if own else "Belfry keeps this key safe for you and signs with it in the apps you sign in to."}</p></div>""")

    if not own and account:
        # connected apps
        rows = "".join(
            f"""<li><span><strong>{E(c.get("label") or c.get("app") or "")}</strong>
<span class="muted small"> since {E(_when(c.get("created_at")))}{", used " + E(_when(c.get("last_used_at"))) if c.get("last_used_at") else ""}</span></span>
<form class="inline" method="post" action="/account/connections/revoke"><input type="hidden" name="id" value="{E(c["id"])}">
<button class="link" type="submit">Remove</button></form></li>"""
            for c in data["connections"]
        )
        listing = (
            f'<ul class="conn">{rows}</ul>' if rows else '<p class="muted">No app is signed in.</p>'
        )
        parts.append(f"""<div class="card"><h2>Signed-in apps</h2>{listing}
<p class="small muted">Another Nostr app (a phone app, an agent) can sign with your key through a
bunker URL. Make one for it here; it is shown once.</p>
<form method="post" action="/account/connections" class="fb">
<input type="text" name="label" maxlength="60" placeholder="Name of the app (e.g. Amethyst)" required>
<button class="btn" type="submit">Connect a Nostr app</button></form></div>""")

        # the key
        if fresh:
            key_actions = """<form method="post" action="/account/export" class="fb">
<button class="btn" type="submit">Show my secret key (nsec)</button></form>
<p class="small muted" style="margin-top:16px">Or move to a key of your own: with a browser
extension (NIP-07) holding the new key, press the button and sign. Belfry then forgets the key
it kept and your apps sign in with your own signer.</p>
<p><button class="btn" type="button" id="move">Move to my own key</button></p>
<p id="move-result" class="muted small"></p>"""
        else:
            key_actions = f"""<p>To see your secret key or move to a key of your own, sign in again.</p>
<p><a class="btn" href="/login?{urllib.parse.urlencode({"next": "/account", "fresh": 1})}">Sign in again to unlock</a></p>"""
        parts.append(f'<div class="card" id="key"><h2>Your key</h2>{key_actions}</div>')

    steward = (
        '<p class="small"><a href="/steward">Steward pages</a></p>'
        if partner.get("belfry_steward")
        else ""
    )
    body = f"""{note}<div class="card"><h1>Hello{", " + E(name.split(" ")[0]) if name else ""}</h1>
<p class="muted small">Signed in to Open Source Village as {E(partner.get("email") or "")}.
<a href="/logout">Sign out</a></p>{steward}</div>
{"".join(parts)}{CSS}{MOVE_SCRIPT if (fresh and not own and account) else ""}"""
    return page("Your account", body)


MOVE_SCRIPT = """<script>
document.getElementById('move')?.addEventListener('click', async () => {
  const out = document.getElementById('move-result');
  if (!window.nostr) { out.textContent = 'No signer found in this browser (a NIP-07 extension such as nos2x or Alby).'; return; }
  try {
    const c = await (await fetch('/account/self-custody/challenge')).json();
    const pubkey = await window.nostr.getPublicKey();
    if (!confirm('Move your Open Source Village account to the key ' + pubkey.slice(0, 12) + '…? Belfry forgets the key it kept for you.')) return;
    const event = await window.nostr.signEvent({kind: c.kind, created_at: Math.floor(Date.now() / 1000), tags: [['challenge', c.challenge]], content: ''});
    const r = await fetch('/account/self-custody', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({event})});
    const j = await r.json();
    out.textContent = r.ok ? 'Done: your account uses your own key now.' : (j.message || 'The move failed.');
    if (r.ok) setTimeout(() => location.reload(), 1500);
  } catch (e) { out.textContent = 'The move failed: ' + e; }
});
</script>"""


def bunker_page(label: str, bunker_url: str) -> str:
    body = f"""<div class="card"><h1>Connect {E(label)}</h1>
<p>Paste this bunker URL into the app (or scan it). It works for one app: the first one that
connects with it keeps it. It is shown only now.</p>
<p class="qr"><img alt="QR code with the bunker URL" src="{qr_svg(bunker_url)}"></p>
<p><code id="b">{E(bunker_url)}</code></p>
<p class="row"><button class="btn" type="button" onclick="navigator.clipboard.writeText(document.getElementById('b').textContent)" style="border:0;font:inherit;font-weight:600;cursor:pointer">Copy</button>
<a href="/account">Back to your account</a></p></div>"""
    return page("Connect a Nostr app", body)


def nsec_page(nsec: str) -> str:
    body = f"""<div class="card"><h1>Your secret key</h1>
<p class="bad">Anyone with this key can post and read messages as you. Keep it in a password
manager or a signer app; never paste it into a website.</p>
<p><code>{E(nsec)}</code></p>
<p><a class="btn" href="/account">Back to your account</a></p></div>"""
    return page("Your secret key", body)


# -- self-custody ---------------------------------------------------------------------------------


def challenge_for(settings: Any, partner_id: int, expires: int) -> str:
    mac = hmac.new(
        settings.session_secret.encode(),
        f"belfry/self-custody/v1|{partner_id}|{expires}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{expires}.{mac}"


def check_move(settings: Any, partner_id: int, event: Any) -> str:
    """The new key, from a valid signed challenge; AccountError otherwise."""
    from .blossom import event_id, verify_signature

    if not isinstance(event, dict) or event.get("kind") != CHALLENGE_KIND:
        raise AccountError(422, "Sign the challenge as a kind-27235 event.")
    tags = event.get("tags") if isinstance(event.get("tags"), list) else []
    given = next(
        (t[1] for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "challenge"), ""
    )
    try:
        expires = int(str(given).split(".", 1)[0])
    except ValueError:
        expires = 0
    if expires < time.time() or not hmac.compare_digest(
        str(given), challenge_for(settings, partner_id, expires)
    ):
        raise AccountError(422, "The challenge has expired. Try again.")
    if abs(int(event.get("created_at") or 0) - time.time()) > CHALLENGE_TTL:
        raise AccountError(422, "The event's time is off.")
    try:
        ok = event_id(event) == event.get("id") and verify_signature(event)
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise AccountError(422, "The signature does not hold.")
    return str(event["pubkey"]).lower()


def move_to_own_key(svc: Services, partner_id: int, new_pubkey: str) -> str:
    vid = vine_id(partner_id)
    account = svc.signer.account(vid)
    if account is None:
        raise AccountError(404, "There is no key to move.")
    old = account["pubkey"]
    if new_pubkey == old:
        raise AccountError(
            409, "That is the key Belfry keeps for you. Make a new key for yourself."
        )
    taken = svc.odoo.search_read(
        "belfry.pass",
        [["nostr_pubkey", "=", new_pubkey], ["partner_id", "!=", partner_id]],
        ["id"],
        limit=1,
    )
    if taken:
        raise AccountError(409, "This key belongs to another participant.")
    svc.signer.ensure_account(vid, own_pubkey=new_pubkey)
    return core.key_changed(svc, vid, old, new_pubkey)


def register(app: FastAPI) -> None:
    """Mount /account (create_app calls this)."""

    def svc(request: Request) -> Services:
        return request.app.state.services

    def signed_in(request: Request) -> session.Session | None:
        return session.current(request)

    def to_login(fresh: bool = False) -> Response:
        params = {"next": "/account"}
        if fresh:
            params["fresh"] = "1"
        return no_store(RedirectResponse("/login?" + urllib.parse.urlencode(params), 303))

    @app.exception_handler(AccountError)
    async def account_error(_request: Request, exc: AccountError):
        body = f"""<div class="card"><h1>Your account</h1><p class="bad">{E(exc.message)}</p>
<p><a class="btn" href="/account">Back to your account</a></p></div>"""
        return no_store(HTMLResponse(page("Your account", body), status_code=exc.status))

    @app.get("/account", response_class=HTMLResponse)
    async def account(request: Request, notice: str = ""):
        current = signed_in(request)
        if current is None:
            return to_login()
        services = svc(request)
        data = await run_in_threadpool(load, services, current.partner_id)
        notices = {
            "removed": "The app is signed out.",
            "moved": "Your account uses your own key now.",
        }
        return no_store(
            HTMLResponse(
                account_page(
                    services,
                    data,
                    session.is_fresh(services.settings, current),
                    notices.get(notice, ""),
                )
            )
        )

    @app.post("/account/connections", response_class=HTMLResponse)
    async def connect_app(request: Request):
        current = signed_in(request)
        services = svc(request)
        if current is None or not same_origin(request, services.settings):
            return to_login()
        form = await request.form()
        label = str(form.get("label") or "").strip()[:60] or "Nostr app"
        if not await run_in_threadpool(
            services.odoo.call, "belfry.login.code", "belfry_may_sign_in", [current.partner_id]
        ):
            raise AccountError(403, "Your pass is not active.")
        try:
            conn = await run_in_threadpool(
                services.signer.create_connection, vine_id(current.partner_id), "outside", label
            )
        except SignerError as exc:
            raise AccountError(403 if exc.status < 500 else 502, exc.message or exc.code) from exc
        log.info("partner %s made a bunker connection for %r", current.partner_id, label)
        return no_store(HTMLResponse(bunker_page(label, conn["bunker_url"])))

    @app.post("/account/connections/revoke")
    async def remove_app(request: Request):
        current = signed_in(request)
        services = svc(request)
        if current is None or not same_origin(request, services.settings):
            return to_login()
        form = await request.form()
        await run_in_threadpool(
            services.signer.revoke, vine_id(current.partner_id), str(form.get("id") or "") or "-"
        )
        return no_store(RedirectResponse("/account?notice=removed", 303))

    @app.post("/account/export", response_class=HTMLResponse)
    async def export(request: Request):
        current = signed_in(request)
        services = svc(request)
        if current is None or not same_origin(request, services.settings):
            return to_login()
        if not session.is_fresh(services.settings, current):
            return to_login(fresh=True)
        try:
            nsec = await run_in_threadpool(services.signer.export, vine_id(current.partner_id))
        except SignerError as exc:
            raise AccountError(409, exc.message or exc.code) from exc
        log.warning("partner %s exported their key", current.partner_id)
        return no_store(HTMLResponse(nsec_page(nsec)))

    @app.get("/account/self-custody/challenge")
    async def challenge(request: Request) -> JSONResponse:
        current = signed_in(request)
        services = svc(request)
        if current is None:
            return JSONResponse(
                {"error": "unauthorized", "message": "Sign in first."}, status_code=401
            )
        expires = int(time.time()) + CHALLENGE_TTL
        return JSONResponse(
            {
                "challenge": challenge_for(services.settings, current.partner_id, expires),
                "kind": CHALLENGE_KIND,
                "expires_in": CHALLENGE_TTL,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/account/self-custody")
    async def self_custody(request: Request) -> JSONResponse:
        current = signed_in(request)
        services = svc(request)
        if current is None or not same_origin(request, services.settings):
            return JSONResponse(
                {"error": "unauthorized", "message": "Sign in first."}, status_code=401
            )
        if not session.is_fresh(services.settings, current):
            return JSONResponse(
                {"error": "stale", "message": "Sign in again first."}, status_code=401
            )
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        try:
            new_pubkey = check_move(
                services.settings, current.partner_id, (body or {}).get("event")
            )
            result = await run_in_threadpool(
                move_to_own_key, services, current.partner_id, new_pubkey
            )
        except AccountError as exc:
            return JSONResponse(
                {"error": "refused", "message": exc.message}, status_code=exc.status
            )
        log.info(
            "partner %s moved to their own key %s (%s)", current.partner_id, new_pubkey, result
        )
        return JSONResponse({"outcome": "moved_to_own_key", "pubkey": new_pubkey})
