"""Steward pages (/steward): Belfry's own UI for stewards, Odoo stays the backend (decided
2026-10-23). Stewards sign in through Authentik (application belfry-steward, group
belfry-stewards; blueprint belfry-steward-app) and can create, copy and revoke invites, mark a
pending pass paid, re-send a participant's sign-in link and revoke a pass. Every Odoo call runs
as the service user; the steward's e-mail is written on what they create or mark.

Login: authorization code + PKCE. The code is exchanged and the user read from userinfo over the
Compose network (AUTHENTIK_URL), server to server with the client secret, so no ID token is
parsed. The session is a signed cookie (HS256, STEWARD_SESSION_SECRET) for STEWARD_SESSION_HOURS;
every form carries the session's CSRF token.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import time
import urllib.parse
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import core
from .core import Services

log = logging.getLogger(__name__)

SESSION_COOKIE = "belfry_steward"
FLOW_COOKIE = "belfry_steward_flow"
FLOW_SECONDS = 600


class StewardError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# -- configuration and cookies -------------------------------------------------------------------


def enabled(svc: Services) -> bool:
    s = svc.settings
    return bool(s.steward_oidc_client_secret and s.steward_session_secret)


def redirect_uri(svc: Services) -> str:
    return svc.settings.public_url.rstrip("/") + "/steward/callback"


def secure_cookies(svc: Services) -> bool:
    return svc.settings.public_url.startswith("https://")


def _sign(svc: Services, claims: dict[str, Any], seconds: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {**claims, "iat": now, "exp": now + seconds}, svc.settings.steward_session_secret, "HS256"
    )


def _verify(svc: Services, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        return jwt.decode(token, svc.settings.steward_session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def session(svc: Services, cookie: str | None) -> dict[str, Any] | None:
    claims = _verify(svc, cookie)
    return claims if claims and claims.get("kind") == "session" else None


def check_csrf(sess: dict[str, Any], token: str | None) -> None:
    if not token or not secrets.compare_digest(token, sess.get("csrf", "")):
        raise StewardError(403, "The form expired. Reload the page and try again.")


# -- login ---------------------------------------------------------------------------------------


def start_login(svc: Services) -> tuple[str, str]:
    """Authorize URL at Authentik's public origin, and the signed flow cookie (state, PKCE)."""
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "response_type": "code",
        "client_id": svc.settings.steward_oidc_client_id,
        "redirect_uri": redirect_uri(svc),
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = (
        svc.settings.authentik_public_url.rstrip("/")
        + "/application/o/authorize/?"
        + urllib.parse.urlencode(params)
    )
    flow = _sign(svc, {"kind": "flow", "state": state, "verifier": verifier}, FLOW_SECONDS)
    return url, flow


def finish_login(svc: Services, code: str | None, state: str | None, flow_cookie: str | None):
    """Exchange the code, read the user, require the steward group. Returns the session cookie."""
    flow = _verify(svc, flow_cookie)
    if not flow or flow.get("kind") != "flow" or not state or state != flow.get("state"):
        raise StewardError(400, "The sign-in took too long or was started elsewhere. Try again.")
    if not code:
        raise StewardError(400, "Authentik returned no code.")
    base = svc.settings.authentik_url.rstrip("/")
    s = svc.settings
    token = httpx.post(
        f"{base}/application/o/token/",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(svc),
            "code_verifier": flow["verifier"],
        },
        auth=(s.steward_oidc_client_id, s.steward_oidc_client_secret),
        timeout=15,
    )
    if token.status_code != 200:
        log.warning("steward login: token exchange %s %s", token.status_code, token.text[:200])
        raise StewardError(502, "Authentik refused the sign-in. Try again.")
    access = token.json().get("access_token")
    info = httpx.get(
        f"{base}/application/o/userinfo/",
        headers={"Authorization": f"Bearer {access}"},
        timeout=15,
    )
    if info.status_code != 200:
        log.warning("steward login: userinfo %s %s", info.status_code, info.text[:200])
        raise StewardError(502, "Authentik did not return the user. Try again.")
    user = info.json()
    email = (user.get("email") or "").strip()
    if s.steward_group not in (user.get("groups") or []) or not email:
        log.warning("steward login refused for %s: not in %s", email or "?", s.steward_group)
        raise StewardError(403, "This account is not a steward.")
    log.info("steward login: %s", email)
    claims = {
        "kind": "session",
        "email": email,
        "name": user.get("name") or email,
        "csrf": secrets.token_urlsafe(24),
    }
    return _sign(svc, claims, s.steward_session_hours * 3600)


# -- actions -------------------------------------------------------------------------------------

INVITE_FIELDS = [
    "note",
    "state",
    "url",
    "expires_at",
    "create_date",
    "steward_email",
    "steward_id",
    "inviter_name",
    "inviter_partner_id",
    "used_by_partner_id",
]
FEEDBACK_FIELDS = [
    "create_date",
    "partner_id",
    "email",
    "page_url",
    "happened",
    "expected",
    "screenshot_name",
    "state",
    "handled_by",
]
PASS_FIELDS = [
    "partner_id",
    "tier",
    "state",
    "date_start",
    "date_end",
    "member_number",
    "invoice_id",
    "manual_paid_by",
    "login_mail_sent_at",
]


REPORT_FIELDS = [
    "create_date",
    "app",
    "target_kind",
    "reason",
    "note",
    "target_excerpt",
    "target_url",
    "target_pass_id",
    "target_partner_id",
    "reporter_partner_id",
    "state",
    "handled_by",
]
REPORT_LABELS = {
    "spam": "Spam",
    "harassment": "Harassment or hate",
    "illegal": "Illegal content",
    "scam": "Scam or fraud",
    "other": "Other",
    "profile": "directory entry",
    "listing": "offer",
    "photo": "photo",
    "comment": "comment",
    "session": "programme session",
    "people": "Directory",
    "market": "Marketplace",
    "photos": "Photos",
    "programme": "Programme",
}


def dashboard_data(svc: Services) -> dict[str, Any]:
    odoo = svc.odoo
    invites = odoo.search_read("belfry.invite", [], INVITE_FIELDS, limit=50, order="id desc")
    passes = odoo.search_read("belfry.pass", [], PASS_FIELDS, limit=100, order="id desc")
    partner_ids = sorted({core.m2o_id(p["partner_id"]) for p in passes if p.get("partner_id")})
    partners = (
        {r["id"]: r for r in odoo.read("res.partner", partner_ids, ["name", "email"])}
        if partner_ids
        else {}
    )
    invoice_ids = sorted({core.m2o_id(p["invoice_id"]) for p in passes if p.get("invoice_id")})
    invoices = (
        {
            r["id"]: r
            for r in odoo.read(
                "account.move", invoice_ids, ["name", "amount_total", "payment_state"]
            )
        }
        if invoice_ids
        else {}
    )
    reports = odoo.search_read("belfry.report", [], REPORT_FIELDS, limit=50, order="id desc")
    feedback = odoo.search_read("belfry.feedback", [], FEEDBACK_FIELDS, limit=100, order="id desc")
    return {
        "feedback": feedback,
        "invites": invites,
        "passes": passes,
        "partners": partners,
        "invoices": invoices,
        "reports": reports,
    }


def create_invite(svc: Services, sess: dict[str, Any], note: str) -> dict[str, Any]:
    return svc.odoo.call(
        "belfry.invite",
        "belfry_steward_create",
        [sess["email"], note.strip()[:200] or False, sess.get("name") or False],
    )


def revoke_invite(svc: Services, invite_id: int) -> None:
    svc.odoo.call("belfry.invite", "action_revoke", [[invite_id]])


def mark_paid(svc: Services, sess: dict[str, Any], pass_id: int) -> str:
    """Registers the payment in Odoo; Odoo's invoice-paid webhook then activates the pass. The
    activation is also run here, so the page shows the result without waiting for the hook
    (activate_pass is idempotent)."""
    svc.odoo.call("belfry.pass", "belfry_steward_mark_paid", [[pass_id], sess["email"]])
    row = svc.odoo.read_one("belfry.pass", pass_id, ["invoice_id"])
    invoice_id = core.m2o_id(row["invoice_id"]) if row else None
    return core.activate_pass(svc, invoice_id) if invoice_id else "not_found"


def handle_report(svc: Services, sess: dict[str, Any], report_id: int, action: str) -> str:
    log.info("steward %s: report %s %s", sess["email"], report_id, action)
    return svc.odoo.call(
        "belfry.report", "belfry_report_handle", [[report_id], action, sess["email"]]
    )


def handle_feedback(svc: Services, sess: dict[str, Any], feedback_id: int, action: str) -> str:
    return svc.odoo.call(
        "belfry.feedback", "belfry_feedback_handle", [[feedback_id], action, sess["email"]]
    )


def feedback_screenshot(svc: Services, feedback_id: int) -> tuple[bytes, str] | None:
    row = svc.odoo.read_one("belfry.feedback", feedback_id, ["screenshot", "screenshot_name"])
    if not row or not row.get("screenshot"):
        return None
    name = row.get("screenshot_name") or "screenshot.png"
    kind = {"jpg": "image/jpeg", "webp": "image/webp"}.get(name.rsplit(".", 1)[-1], "image/png")
    return base64.b64decode(row["screenshot"]), kind


def send_login_link(svc: Services, pass_id: int) -> str:
    return core.send_login_link(svc, pass_id)


def revoke_pass(svc: Services, sess: dict[str, Any], pass_id: int) -> str:
    log.info("steward %s revokes pass %s", sess["email"], pass_id)
    return core.revoke_pass(svc, pass_id, "steward")


def add_steward(svc: Services, email: str, name: str = "") -> dict[str, Any]:
    """Make the Authentik user with this e-mail a steward (CLI ``add-steward``). A participant
    keeps their account; anyone else gets a new passwordless user (path belfry/stewards), who
    signs in by magic link."""
    email = email.strip()
    if "@" not in email:
        raise ValueError(f"not an e-mail address: {email!r}")
    ak = svc.authentik
    group = ak.find_group(svc.settings.steward_group)
    if group is None:
        raise RuntimeError(
            f"group {svc.settings.steward_group} missing (blueprint belfry-steward-app)"
        )
    users = ak.find_users_by_email(email)
    if users:
        user, created = users[0], False
    else:
        local = "".join(c if c.isalnum() else "-" for c in email.lower())
        user = ak.create_user(
            username=f"steward-{local}"[:150],
            name=name or email,
            email=email,
            attributes={"belfry_steward": True},
            path="belfry/stewards",
        )
        created = True
    ak.add_user_to_group(group["pk"], int(user["pk"]))
    log.info("add_steward: %s (Authentik user %s, created=%s)", email, user["pk"], created)
    return {"email": email, "authentik_user": int(user["pk"]), "created": created}


# -- pages ---------------------------------------------------------------------------------------

E = html.escape

STYLE = """
:root { color-scheme: dark; }
body { margin: 0; background: var(--osv-background, #2b3a31); color: var(--osv-foreground, #fff);
  font-family: var(--osv-font-sans, Geist, system-ui, sans-serif); font-size: 15px;
  line-height: 1.5; }
header, main { max-width: 1100px; margin: 0 auto; padding: 16px; }
header { display: flex; justify-content: space-between; align-items: center; gap: 12px;
  flex-wrap: wrap; }
header .who { color: var(--osv-foreground-muted, #c9d1cb); font-size: 14px; }
h1 { font-size: 22px; margin: 0; } h2 { font-size: 18px; margin: 28px 0 10px; }
section, .card { background: var(--osv-surface, #1a231d);
  border: 1px solid var(--osv-border, #ffffff29); border-radius: 12px; padding: 16px;
  margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px 6px; vertical-align: top;
  border-bottom: 1px solid var(--osv-border, #ffffff29); }
th { color: var(--osv-foreground-muted, #c9d1cb); font-weight: 500; }
.wrap { overflow-x: auto; }
button, .btn { background: var(--osv-accent, #d6ff52); color: var(--osv-on-accent, #1a231d);
  border: 0; border-radius: 8px; padding: 6px 12px; font: inherit; font-weight: 600;
  cursor: pointer; }
button:hover { background: var(--osv-accent-hover, #c5ee41); }
button.quiet { background: transparent; color: var(--osv-foreground, #fff); font-weight: 500;
  border: 1px solid var(--osv-border, #ffffff29); }
form.inline { display: inline; }
input[type=text] { background: var(--osv-background, #2b3a31); color: inherit;
  border: 1px solid var(--osv-border, #ffffff29); border-radius: 8px; padding: 6px 10px;
  font: inherit; min-width: 240px; }
code, .link { font-family: ui-monospace, monospace; font-size: 13px; word-break: break-all; }
.msg { border-left: 4px solid var(--osv-accent, #d6ff52); }
.err { border-left: 4px solid var(--osv-accent-2, #f89b0e); }
.muted { color: var(--osv-foreground-muted, #c9d1cb); }
h1 img { vertical-align: middle; margin-right: 8px; }
footer { max-width: 1100px; margin: 0 auto; padding: 16px; font-size: 12px;
  color: var(--osv-foreground-faint, #8e9a91); }
.state { font-size: 12px; padding: 2px 8px; border-radius: 99px;
  border: 1px solid var(--osv-border, #ffffff29); }
"""


def page(title: str, body: str, sess: dict[str, Any] | None = None) -> str:
    who = ""
    if sess:
        who = (
            f'<div class="who">{E(sess["name"])} &middot; <form class="inline" method="post" '
            f'action="/steward/logout"><input type="hidden" name="csrf" value="{E(sess["csrf"])}">'
            '<button class="quiet">Sign out</button></form></div>'
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{E(title)} · Open Source Village stewards</title>"
        "<link rel='stylesheet' href='/brand/fonts.css'>"
        "<link rel='stylesheet' href='/brand/tokens.css'>"
        f"<style>{STYLE}</style>"
        "<link rel='icon' href='/brand/favicon.svg'>"
        "</head><body><header><h1><img src='/brand/osv-logo-horizontal-white.svg' height='28' "
        f"alt='Open Source Village'> Stewards</h1>{who}</header><main>{body}</main>"
        "<footer>Open Source Village, run by Belfry</footer></body></html>"
    )


def error_page(exc: StewardError) -> str:
    return page(
        "Error",
        f'<div class="card err"><p>{E(exc.message)}</p>'
        '<p><a class="btn" href="/steward">Back to the steward pages</a></p></div>',
    )


def _form(
    action: str, sess: dict[str, Any], label: str, quiet: bool = False, confirm: str = ""
) -> str:
    onsubmit = f' onsubmit="return confirm({E(repr(confirm))})"' if confirm else ""
    cls = ' class="quiet"' if quiet else ""
    return (
        f'<form class="inline" method="post" action="{E(action)}"{onsubmit}>'
        f'<input type="hidden" name="csrf" value="{E(sess["csrf"])}">'
        f"<button{cls}>{E(label)}</button></form>"
    )


def _date(value: Any) -> str:
    return E(str(value)[:10]) if value else ""


def dashboard(sess: dict[str, Any], data: dict[str, Any], msg: str = "", err: str = "") -> str:
    parts: list[str] = []
    if msg:
        parts.append(f'<div class="card msg">{msg}</div>')
    if err:
        parts.append(f'<div class="card err">{E(err)}</div>')

    feedback = data.get("feedback") or []
    rows = []
    for f in feedback:
        who = f["partner_id"][1] if f.get("partner_id") else (f.get("email") or "not signed in")
        page_link = (
            f'<br><a href="{E(f["page_url"])}" target="_blank" rel="noopener" class="muted">'
            f"{E(f['page_url'][:80])}</a>"
            if f.get("page_url")
            else ""
        )
        expected = (
            f"<br><span class='muted'>Expected: {E(f['expected'])}</span>"
            if f.get("expected")
            else ""
        )
        shot = (
            f' <a href="/steward/feedback/{f["id"]}/screenshot" target="_blank">screenshot</a>'
            if f.get("screenshot_name")
            else ""
        )
        action = "done" if f["state"] == "new" else "reopen"
        button = _form(
            f"/steward/feedback/{f['id']}/{action}",
            sess,
            "Done" if action == "done" else "Reopen",
            quiet=True,
        )
        rows.append(
            f"<tr><td>{_date(f.get('create_date'))}</td><td>{E(who)}</td>"
            f"<td class='pre'>{E(f.get('happened') or '')}{expected}{page_link}{shot}</td>"
            f"<td><span class='state'>{E(f['state'])}</span></td>"
            f"<td>{button}</td></tr>"
        )
    new_count = sum(1 for f in feedback if f["state"] == "new")
    parts.append(
        f"<section id='feedback'><h2>Feedback{f' ({new_count} new)' if new_count else ''}</h2>"
        "<p class='muted'>From the feedback page (the app bar's Feedback link).</p>"
        "<div class='wrap'><table><thead><tr><th>Date</th><th>From</th><th>What</th>"
        "<th>State</th><th></th></tr></thead><tbody>"
        + ("".join(rows) or "<tr><td colspan=5 class='muted'>No feedback yet.</td></tr>")
        + "</tbody></table></div></section>"
    )

    reports = data.get("reports") or []
    rows = []
    for r in reports:
        label = REPORT_LABELS.get
        target_pass = core.m2o_id(r.get("target_pass_id"))
        author = r["target_partner_id"][1] if r.get("target_partner_id") else "no pass"
        reporter = r["reporter_partner_id"][1] if r.get("reporter_partner_id") else ""
        what = f"{label(r.get('app'), '')}: {label(r.get('target_kind'), '')}"
        if r.get("target_url"):
            what = f'<a href="{E(r["target_url"])}" target="_blank" rel="noopener">{E(what)}</a>'
        else:
            what = E(what)
        detail = "".join(
            f"<br><span class='muted'>{E(t)}</span>"
            for t in (r.get("target_excerpt"), r.get("note") and f"Note: {r['note']}")
            if t
        )
        actions = []
        if r["state"] == "open":
            actions.append(
                _form(
                    f"/steward/reports/{r['id']}/hide",
                    sess,
                    "Hide content",
                    confirm="Hide this content in the community apps?",
                )
            )
            actions.append(
                _form(f"/steward/reports/{r['id']}/dismiss", sess, "Dismiss", quiet=True)
            )
        else:
            actions.append(_form(f"/steward/reports/{r['id']}/reopen", sess, "Reopen", quiet=True))
        if target_pass:
            actions.append(
                _form(
                    f"/steward/passes/{target_pass}/revoke",
                    sess,
                    "Revoke author's pass",
                    quiet=True,
                    confirm="Revoke the author's pass? Door, apps and relay access end, and "
                    "their content disappears from the apps.",
                )
            )
        state = E(r["state"]) + (
            f"<br><span class='muted'>{E(r['handled_by'])}</span>" if r.get("handled_by") else ""
        )
        rows.append(
            f"<tr><td>{_date(r.get('create_date'))}</td><td>{what}{detail}</td>"
            f"<td>{E(label(r.get('reason'), ''))}</td><td>{E(author)}</td><td>{E(reporter)}</td>"
            f"<td><span class='state'>{state}</span></td><td>{' '.join(actions)}</td></tr>"
        )
    open_count = sum(1 for r in reports if r["state"] == "open")
    parts.append(
        f"<section id='reports'><h2>Reports{f' ({open_count} open)' if open_count else ''}</h2>"
        "<p class='muted'>Reports from the directory, marketplace, photos and programme. "
        "Hidden content stays on the relay but the apps stop showing it. Reporters are never "
        "told to the author.</p><div class='wrap'><table><thead><tr><th>Date</th><th>What</th>"
        "<th>Reason</th>"
        "<th>Author</th><th>Reported by</th><th>State</th><th></th></tr></thead><tbody>"
        + ("".join(rows) or "<tr><td colspan=7 class='muted'>No reports.</td></tr>")
        + "</tbody></table></div></section>"
    )

    rows = []
    for inv in data["invites"]:
        who = inv.get("inviter_name") or inv.get("steward_email") or ""
        if inv.get("inviter_partner_id"):
            who += " <span class='muted'>(participant)</span>"
        else:
            who = E(who)
        if inv.get("used_by_partner_id"):
            who += f"<br><span class='muted'>for {E(inv['used_by_partner_id'][1])}</span>"
        actions = ""
        if inv["state"] == "open":
            actions = (
                f'<button class="quiet" type="button" data-copy="{E(inv["url"])}">'
                "Copy link</button> "
                + _form(
                    f"/steward/invites/{inv['id']}/revoke",
                    sess,
                    "Revoke",
                    quiet=True,
                    confirm="Revoke this invite? The link stops working.",
                )
            )
        rows.append(
            f"<tr><td>{_date(inv.get('create_date'))}</td><td>{E(inv.get('note') or '')}</td>"
            f"<td>{who}</td><td><span class='state'>{E(inv['state'])}</span></td>"
            f"<td>{_date(inv.get('expires_at'))}</td><td>{actions}</td></tr>"
        )
    parts.append(
        "<section><h2>Invites</h2>"
        '<form method="post" action="/steward/invites">'
        f'<input type="hidden" name="csrf" value="{E(sess["csrf"])}">'
        '<input type="text" name="note" maxlength="200" '
        'placeholder="Who is it for? (only stewards see this)"> '
        "<button>Create invite</button></form>"
        '<div class="wrap"><table><thead><tr><th>Created</th><th>Note</th><th>Invited by</th>'
        "<th>State</th><th>Expires</th><th></th></tr></thead><tbody>"
        + ("".join(rows) or "<tr><td colspan=6 class='muted'>No invites yet.</td></tr>")
        + "</tbody></table></div></section>"
    )

    rows = []
    for p in data["passes"]:
        partner = data["partners"].get(core.m2o_id(p.get("partner_id")), {})
        invoice = data["invoices"].get(core.m2o_id(p.get("invoice_id")), {})
        pay = ""
        if invoice:
            pay = (
                f"{E(invoice.get('name') or '')} · {invoice.get('amount_total', 0):.2f} · "
                f"{E(invoice.get('payment_state') or '')}"
            )
            if p.get("manual_paid_by"):
                pay += f"<br><span class='muted'>marked paid by {E(p['manual_paid_by'])}</span>"
        actions = []
        if (
            p["state"] == "pending"
            and invoice
            and invoice.get("payment_state") not in ("paid", "in_payment")
        ):
            actions.append(
                _form(
                    f"/steward/passes/{p['id']}/mark-paid",
                    sess,
                    "Mark paid",
                    confirm="Register the full payment of this invoice "
                    "(cash or bank transfer received)?",
                )
            )
        if p["state"] == "active":
            actions.append(
                _form(
                    f"/steward/passes/{p['id']}/login-link",
                    sess,
                    "Send new sign-in link",
                    quiet=True,
                )
            )
            actions.append(
                _form(
                    f"/steward/passes/{p['id']}/revoke",
                    sess,
                    "Revoke",
                    quiet=True,
                    confirm="Revoke this pass? Door, apps and relay access end.",
                )
            )
        rows.append(
            f"<tr><td>{E(p.get('member_number') or '')}</td><td>{E(partner.get('name') or '')}"
            f"<br><span class='muted'>{E(partner.get('email') or '')}</span></td>"
            f"<td>{E(p.get('tier') or '')}</td><td><span class='state'>{E(p['state'])}</span></td>"
            f"<td>{_date(p.get('date_start'))} – {_date(p.get('date_end'))}</td><td>{pay}</td>"
            f"<td>{' '.join(actions)}</td></tr>"
        )
    parts.append(
        "<section><h2>Passes</h2><div class='wrap'><table><thead><tr><th>Member</th><th>Holder</th>"
        "<th>Tier</th><th>State</th><th>Valid</th><th>Invoice</th><th></th></tr></thead><tbody>"
        + ("".join(rows) or "<tr><td colspan=7 class='muted'>No passes yet.</td></tr>")
        + "</tbody></table></div></section>"
    )
    parts.append(
        "<script>document.addEventListener('click',e=>{const b=e.target.closest('[data-copy]');"
        "if(!b)return;navigator.clipboard.writeText(b.dataset.copy).then(()=>{b.textContent='Copied';"
        "setTimeout(()=>b.textContent='Copy link',1500)})});</script>"
    )
    return page("Stewards", "".join(parts), sess)


def created_message(invite: dict[str, Any]) -> str:
    url = E(invite["url"])
    return (
        f"Invite created. Send this link to the participant (it works once, 14 days):<br>"
        f'<span class="link">{url}</span> '
        f'<button class="quiet" type="button" data-copy="{url}">Copy link</button>'
    )


MESSAGES = {
    "revoked-invite": "Invite revoked.",
    "paid": "Payment registered. The pass is active and the sign-in mail is on its way.",
    "paid-pending": "Payment registered. The pass activates in a moment; reload to see it.",
    "link": "A new sign-in link was mailed to the participant.",
    "revoked-pass": "Pass revoked.",
    "report-hide": "The content is hidden in the apps.",
    "report-dismiss": "Report dismissed.",
    "report-reopen": "Report reopened.",
    "feedback-done": "Feedback marked done.",
    "feedback-reopen": "Feedback reopened.",
    "signed-out": "Signed out.",
}


# -- routes --------------------------------------------------------------------------------------


def register(app: FastAPI) -> None:
    """Mount the /steward routes on the FastAPI app (create_app calls this)."""

    def svc(request: Request) -> Services:
        return request.app.state.services

    def guard(request: Request) -> tuple[Services, dict[str, Any] | None]:
        services = svc(request)
        if not enabled(services):
            raise StewardError(503, "The steward pages are not configured on this server.")
        return services, session(services, request.cookies.get(SESSION_COOKIE))

    def back(msg: str = "", err: str = "", created: int | None = None) -> RedirectResponse:
        query = {k: v for k, v in (("msg", msg), ("err", err), ("created", created)) if v}
        target = "/steward" + ("?" + urllib.parse.urlencode(query) if query else "")
        return RedirectResponse(target, status_code=303)

    def signed_in(request: Request, csrf: str) -> tuple[Services, dict[str, Any]]:
        """A form post: requires the session and its CSRF token."""
        services, sess = guard(request)
        if sess is None:
            raise StewardError(401, "Your steward session ended. Sign in again.")
        check_csrf(sess, csrf)
        return services, sess

    @app.exception_handler(StewardError)
    async def steward_error(_request: Request, exc: StewardError):
        if exc.status == 401:
            return RedirectResponse("/steward/login", status_code=303)
        return HTMLResponse(error_page(exc), status_code=exc.status)

    @app.get("/steward", response_class=HTMLResponse)
    async def steward_home(request: Request, msg: str = "", err: str = "", created: int = 0):
        services, sess = guard(request)
        if sess is None:
            return RedirectResponse("/steward/login", status_code=303)
        data = await run_in_threadpool(dashboard_data, services)
        text = E(MESSAGES.get(msg, ""))
        if created:
            invite = next((i for i in data["invites"] if i["id"] == created), None)
            text = created_message(invite) if invite else text
        return HTMLResponse(dashboard(sess, data, text, err[:300]))

    @app.get("/steward/login")
    async def steward_login(request: Request):
        services, _sess = guard(request)
        url, flow = start_login(services)
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            FLOW_COOKIE,
            flow,
            max_age=FLOW_SECONDS,
            httponly=True,
            secure=secure_cookies(services),
            samesite="lax",
            path="/steward",
        )
        return response

    @app.get("/steward/callback")
    async def steward_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        services, _sess = guard(request)
        if error:
            raise StewardError(403, "Authentik did not sign you in as a steward.")
        cookie = await run_in_threadpool(
            finish_login, services, code, state, request.cookies.get(FLOW_COOKIE)
        )
        response = RedirectResponse("/steward", status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/steward")
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=services.settings.steward_session_hours * 3600,
            httponly=True,
            secure=secure_cookies(services),
            samesite="lax",
            path="/steward",
        )
        return response

    @app.post("/steward/logout")
    async def steward_logout(request: Request, csrf: str = Form("")):
        services, sess = guard(request)
        if sess is not None:
            check_csrf(sess, csrf)
        response = HTMLResponse(
            page(
                "Signed out",
                '<div class="card msg"><p>Signed out.</p>'
                '<p><a class="btn" href="/steward/login">Sign in again</a></p></div>',
            )
        )
        response.delete_cookie(SESSION_COOKIE, path="/steward")
        return response

    @app.post("/steward/invites")
    async def steward_create_invite(request: Request, csrf: str = Form(""), note: str = Form("")):
        services, sess = signed_in(request, csrf)
        invite = await run_in_threadpool(create_invite, services, sess, note)
        return back(created=invite["id"])

    @app.post("/steward/invites/{invite_id}/revoke")
    async def steward_revoke_invite(invite_id: int, request: Request, csrf: str = Form("")):
        services, _sess = signed_in(request, csrf)
        await run_in_threadpool(revoke_invite, services, invite_id)
        return back("revoked-invite")

    @app.post("/steward/passes/{pass_id}/mark-paid")
    async def steward_mark_paid(pass_id: int, request: Request, csrf: str = Form("")):
        services, sess = signed_in(request, csrf)
        try:
            result = await run_in_threadpool(mark_paid, services, sess, pass_id)
        except core.OdooError as exc:
            return back(err=f"Odoo refused: {exc}")
        return back("paid" if result in ("activated", "already_active") else "paid-pending")

    @app.post("/steward/passes/{pass_id}/login-link")
    async def steward_login_link(pass_id: int, request: Request, csrf: str = Form("")):
        services, _sess = signed_in(request, csrf)
        result = await run_in_threadpool(send_login_link, services, pass_id)
        return back("link") if result == "sent" else back(err=f"No link sent ({result}).")

    @app.post("/steward/reports/{report_id}/{action}")
    async def steward_report(report_id: int, action: str, request: Request, csrf: str = Form("")):
        services, sess = signed_in(request, csrf)
        if action not in ("hide", "dismiss", "reopen"):
            raise StewardError(404, "Unknown action.")
        await run_in_threadpool(handle_report, services, sess, report_id, action)
        return back(f"report-{action}")

    @app.post("/steward/feedback/{feedback_id}/{action}")
    async def steward_feedback(
        feedback_id: int, action: str, request: Request, csrf: str = Form("")
    ):
        services, sess = signed_in(request, csrf)
        if action not in ("done", "reopen"):
            raise StewardError(404, "Unknown action.")
        await run_in_threadpool(handle_feedback, services, sess, feedback_id, action)
        return back(f"feedback-{action}")

    @app.get("/steward/feedback/{feedback_id}/screenshot")
    async def steward_feedback_screenshot(feedback_id: int, request: Request):
        services, sess = guard(request)
        if sess is None:
            raise StewardError(401, "Sign in as a steward first.")
        found = await run_in_threadpool(feedback_screenshot, services, feedback_id)
        if found is None:
            raise StewardError(404, "No screenshot.")
        data, kind = found
        return Response(
            data,
            media_type=kind,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/steward/passes/{pass_id}/revoke")
    async def steward_revoke_pass(pass_id: int, request: Request, csrf: str = Form("")):
        services, sess = signed_in(request, csrf)
        await run_in_threadpool(revoke_pass, services, sess, pass_id)
        return back("revoked-pass")
