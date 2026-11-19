"""Sign in with the pass: the login page asks the participant's wallet for the village pass
through walt.id Verifier2 and signs the browser in as the pass's member (2026-11-19; until then
this was the OpenID4VP-to-OIDC bridge Authentik's ``pass-bridge`` source called).

Flow: GET /login/pass (QR page; a cookie binds the attempt to this browser) -> the wallet
presents to Verifier2 -> the page polls GET /login/pass/poll/<sid> -> GET /login/pass/done/<sid>
checks the cookie, finds the partner of the presented member number and sets the session.

The pass must be signed by Belfry's issuer certificate and not be revoked (status list); the
member must still be allowed to sign in (Odoo: an active pass). State is in memory (one
instance; a restart only drops sign-ins in progress).
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from . import credentials
from .claim import page, qr_svg
from .clients.verifier import village_pass_policies, village_pass_query
from .core import Services

log = logging.getLogger(__name__)

AUTH_TTL = 600  # seconds a sign-in attempt (QR page) stays valid
COOKIE = "belfry_pass_login"


class PassLoginError(Exception):
    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status = status
        self.error = error
        self.description = description


@dataclass
class Attempt:
    sid: str
    browser: str  # the secret in this browser's cookie
    next_url: str
    verifier_session_id: str
    authorization_request_url: str
    created: float = field(default_factory=time.time)
    claims: dict[str, Any] | None = None
    failed: list[str] | None = None
    used: bool = False


class PassLoginStore:
    def __init__(self) -> None:
        self.attempts: dict[str, Attempt] = {}

    def sweep(self) -> None:
        now = time.time()
        for sid in [s for s, a in self.attempts.items() if now - a.created > AUTH_TTL]:
            self.attempts.pop(sid, None)


def start(svc: Services, store: PassLoginStore, next_url: str) -> Attempt:
    store.sweep()
    if svc.verifier is None:
        raise PassLoginError(503, "temporarily_unavailable", "The pass sign-in is not set up.")
    session = svc.verifier.create_session(
        village_pass_query(svc.settings.vct, ["member_number", "npub", "pass_tier"]),
        village_pass_policies(svc.settings.credential_issuer, check_status=True),
    )
    attempt = Attempt(
        sid=secrets.token_urlsafe(16),
        browser=secrets.token_urlsafe(24),
        next_url=next_url,
        verifier_session_id=session["sessionId"],
        authorization_request_url=session["bootstrapAuthorizationRequestUrl"],
    )
    store.attempts[attempt.sid] = attempt
    log.info(
        "pass sign-in %s started (verifier session %s)", attempt.sid, attempt.verifier_session_id
    )
    return attempt


def poll(svc: Services, store: PassLoginStore, sid: str) -> dict[str, Any]:
    attempt = store.attempts.get(sid)
    if attempt is None:
        raise PassLoginError(404, "invalid_request", "unknown sign-in")
    if attempt.claims is not None:
        return {"status": "done", "redirect": f"/login/pass/done/{sid}"}
    if attempt.failed is not None:
        return {"status": "failed", "failed": attempt.failed}
    if time.time() - attempt.created > AUTH_TTL:
        return {"status": "failed", "failed": ["expired"]}
    assert svc.verifier is not None
    info = svc.verifier.session_info(attempt.verifier_session_id)
    if info.get("status") == "SUCCESSFUL":
        cred = credentials.presented_credential(info) or {}
        if not credentials.issued_by_belfry(cred, svc.issuer_x5c[:1]):
            log.warning("pass sign-in %s: a pass not signed by Belfry's key", sid)
            attempt.failed = ["issuer-certificate"]
            return poll(svc, store, sid)
        claims = cred.get("credentialData") or {}
        if not claims.get("member_number"):
            attempt.failed = ["member_number not disclosed"]
            return poll(svc, store, sid)
        attempt.claims = claims
        log.info("pass sign-in %s verified for %s", sid, claims.get("member_number"))
        return poll(svc, store, sid)
    if info.get("attempted") or info.get("status") in ("FAILED", "ERROR", "EXPIRED", "REJECTED"):
        from .claim import failed_policies

        attempt.failed = failed_policies(info) or [str(info.get("status"))]
        return poll(svc, store, sid)
    return {"status": "pending"}


def finish(svc: Services, store: PassLoginStore, sid: str, browser: str | None) -> tuple[int, str]:
    """The partner the verified pass names and where to go; spends the attempt. Only the
    browser that started it may finish it."""
    attempt = store.attempts.get(sid)
    if attempt is None or attempt.claims is None or attempt.used:
        raise PassLoginError(400, "invalid_request", "This sign-in is not (or no longer) valid.")
    if not browser or not secrets.compare_digest(browser, attempt.browser):
        raise PassLoginError(
            400, "invalid_request", "Finish the sign-in in the browser that started it."
        )
    attempt.used = True
    number = str(attempt.claims["member_number"])
    rows = svc.odoo.search_read(
        "res.partner", [["belfry_member_number", "=", number]], ["id"], limit=1
    )
    if not rows or not svc.odoo.call("belfry.login.code", "belfry_may_sign_in", [rows[0]["id"]]):
        raise PassLoginError(403, "access_denied", "This pass is not active.")
    return int(rows[0]["id"]), attempt.next_url


def page_html(attempt: Attempt) -> str:
    url = attempt.authorization_request_url
    body = f"""
<div class="card" id="login" data-sid="{html.escape(attempt.sid)}">
<h1>Sign in with your pass</h1>
<p>Scan this code with the wallet that holds your Open Source Village pass and share your
member number. You are signed in as soon as the pass is verified.</p>
<p class="qr"><img alt="QR code with the verification request" src="{qr_svg(url)}"></p>
<p><a class="btn" href="{html.escape(url)}">Open the wallet on this device</a></p>
<p id="result" class="muted">Waiting for your wallet…</p>
<p class="muted"><a href="/login">Sign in with your e-mail address instead</a></p>
</div>
<script>
const sid = document.getElementById('login').dataset.sid;
async function poll() {{
  const r = await fetch('/login/pass/poll/' + sid); const j = await r.json();
  const el = document.getElementById('result');
  if (j.status === 'done') {{ el.className = 'ok'; el.textContent = 'Pass verified, signing you in…'; window.location = j.redirect; return; }}
  if (j.status === 'failed') {{ el.className = 'bad'; el.textContent = 'The pass was refused: ' + (j.failed || []).join(', '); return; }}
  setTimeout(poll, 1500);
}}
poll();
</script>"""
    return page("Sign in with your pass", body)


def error_page(exc: PassLoginError) -> str:
    body = f"""<div class="card"><h1>Sign-in not possible</h1>
<p class="bad">{html.escape(exc.description or exc.error)}</p>
<p><a class="btn" href="/login">Sign in</a></p></div>"""
    return page("Sign-in not possible", body)
