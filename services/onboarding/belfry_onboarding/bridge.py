"""The OpenID4VP-to-OIDC bridge: Authentik's ``pass-bridge`` OAuth source talks OIDC to this
module, which asks the participant's wallet for the village pass through walt.id Verifier2 and
turns the verified presentation into an ID token with ``sub`` = member number.

Flow: Authentik -> GET /bridge/authorize (QR page) -> wallet presents to Verifier2 -> the page
polls GET /bridge/poll/<sid> -> redirect to Authentik with a code -> POST /bridge/token
(client secret + PKCE) -> ID token and access token -> GET /bridge/userinfo.

State is in memory (one instance; a restart only drops logins in progress). Decision 7 of the
plan wanted the SUNET verifier patched for this; see docs/NEXT_STEPS.md §3e for why this is a
few hundred lines over Verifier2 instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import jwt

from . import credentials
from .claim import ClaimError, page, qr_svg
from .clients.verifier import village_pass_policies, village_pass_query
from .core import Services

log = logging.getLogger(__name__)

AUTH_TTL = 600  # seconds a login attempt (QR page) stays valid
CODE_TTL = 300
TOKEN_TTL = 300
SCOPES = ("openid", "profile", "email")


@dataclass
class AuthSession:
    sid: str
    client_id: str
    redirect_uri: str
    state: str | None
    nonce: str | None
    code_challenge: str | None
    code_challenge_method: str | None
    verifier_session_id: str
    authorization_request_url: str
    created: float = field(default_factory=time.time)
    claims: dict[str, Any] | None = None
    code: str | None = None
    code_used: bool = False
    failed: list[str] | None = None


class BridgeStore:
    def __init__(self) -> None:
        self.sessions: dict[str, AuthSession] = {}
        self.codes: dict[str, str] = {}  # code -> sid
        self.tokens: dict[str, tuple[str, float]] = {}  # access token -> (sid, expires)

    def sweep(self) -> None:
        now = time.time()
        for sid in [s for s, a in self.sessions.items() if now - a.created > AUTH_TTL + CODE_TTL]:
            auth = self.sessions.pop(sid)
            if auth.code:
                self.codes.pop(auth.code, None)
        for tok in [t for t, (_s, exp) in self.tokens.items() if exp < now]:
            self.tokens.pop(tok, None)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def discovery(public_url: str) -> dict[str, Any]:
    base = public_url.rstrip("/") + "/bridge"
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "userinfo_endpoint": f"{base}/userinfo",
        "jwks_uri": f"{base}/jwks",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["ES256"],
        "scopes_supported": list(SCOPES),
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": ["sub", "preferred_username", "npub", "pass_tier", "member_number"],
    }


def public_jwk(jwk: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in jwk.items() if k in ("kty", "crv", "x", "y", "kid", "alg", "use")}


class BridgeError(Exception):
    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status = status
        self.error = error
        self.description = description


def start_authorization(svc: Services, store: BridgeStore, params: dict[str, str]) -> AuthSession:
    store.sweep()
    if params.get("client_id") != svc.settings.bridge_client_id:
        raise BridgeError(400, "invalid_client", "unknown client_id")
    if params.get("response_type") != "code":
        raise BridgeError(400, "unsupported_response_type", "only response_type=code")
    redirect_uri = params.get("redirect_uri")
    if not redirect_uri or not redirect_uri.startswith(("https://", "http://")):
        raise BridgeError(400, "invalid_request", "redirect_uri missing")
    method = params.get("code_challenge_method")
    if params.get("code_challenge") and method not in (None, "S256"):
        raise BridgeError(400, "invalid_request", "only code_challenge_method=S256")
    if svc.verifier is None:
        raise BridgeError(503, "temporarily_unavailable", "verifier not configured")
    session = svc.verifier.create_session(
        village_pass_query(svc.settings.vct, ["member_number", "npub", "pass_tier"]),
        village_pass_policies(svc.settings.credential_issuer, check_status=True),
    )
    auth = AuthSession(
        sid=secrets.token_urlsafe(16),
        client_id=params["client_id"],
        redirect_uri=redirect_uri,
        state=params.get("state"),
        nonce=params.get("nonce"),
        code_challenge=params.get("code_challenge"),
        code_challenge_method=method,
        verifier_session_id=session["sessionId"],
        authorization_request_url=session["bootstrapAuthorizationRequestUrl"],
    )
    store.sessions[auth.sid] = auth
    log.info("bridge: login %s started (verifier session %s)", auth.sid, auth.verifier_session_id)
    return auth


def _redirect(auth: AuthSession, **params: str) -> str:
    from urllib.parse import urlencode

    if auth.state:
        params["state"] = auth.state
    sep = "&" if "?" in auth.redirect_uri else "?"
    return f"{auth.redirect_uri}{sep}{urlencode(params)}"


def poll(svc: Services, store: BridgeStore, sid: str) -> dict[str, Any]:
    auth = store.sessions.get(sid)
    if auth is None:
        raise BridgeError(404, "invalid_request", "unknown login")
    if auth.code:
        return {"status": "done", "redirect": _redirect(auth, code=auth.code)}
    if auth.failed is not None:
        return {
            "status": "failed",
            "failed": auth.failed,
            "redirect": _redirect(auth, error="access_denied", error_description="pass refused"),
        }
    if time.time() - auth.created > AUTH_TTL:
        return {
            "status": "failed",
            "failed": ["expired"],
            "redirect": _redirect(auth, error="access_denied", error_description="expired"),
        }
    assert svc.verifier is not None
    info = svc.verifier.session_info(auth.verifier_session_id)
    if info.get("status") == "SUCCESSFUL":
        cred = credentials.presented_credential(info) or {}
        if not credentials.issued_by_belfry(cred, svc.issuer_x5c[:1]):
            log.warning("bridge: login %s presented a pass not signed by Belfry's key", sid)
            auth.failed = ["issuer-certificate"]
            return poll(svc, store, sid)
        claims = cred.get("credentialData") or {}
        if not claims.get("member_number"):
            auth.failed = ["member_number not disclosed"]
            return poll(svc, store, sid)
        auth.claims = claims
        auth.code = secrets.token_urlsafe(32)
        store.codes[auth.code] = sid
        log.info("bridge: login %s verified for %s", sid, claims.get("member_number"))
        return {"status": "done", "redirect": _redirect(auth, code=auth.code)}
    if info.get("attempted") or info.get("status") in ("FAILED", "ERROR", "EXPIRED", "REJECTED"):
        from .claim import failed_policies

        auth.failed = failed_policies(info) or [str(info.get("status"))]
        return poll(svc, store, sid)
    return {"status": "pending"}


def authenticate_client(svc: Services, form: dict[str, str], authorization: str | None) -> None:
    client_id, secret = form.get("client_id"), form.get("client_secret")
    if authorization and authorization.lower().startswith("basic "):
        try:
            raw = base64.b64decode(authorization.split(" ", 1)[1]).decode()
            client_id, secret = raw.split(":", 1)
            from urllib.parse import unquote

            client_id, secret = unquote(client_id), unquote(secret)
        except Exception as exc:
            raise BridgeError(401, "invalid_client", "bad basic auth") from exc
    expected = svc.settings.bridge_client_secret
    if (
        not expected
        or client_id != svc.settings.bridge_client_id
        or not secret
        or not hmac.compare_digest(secret, expected)
    ):
        raise BridgeError(401, "invalid_client", "client authentication failed")


def exchange_code(
    svc: Services, store: BridgeStore, form: dict[str, str], authorization: str | None
) -> dict[str, Any]:
    authenticate_client(svc, form, authorization)
    if form.get("grant_type") != "authorization_code":
        raise BridgeError(400, "unsupported_grant_type")
    code = form.get("code") or ""
    sid = store.codes.pop(code, None)
    auth = store.sessions.get(sid) if sid else None
    if auth is None or auth.code != code or auth.code_used or auth.claims is None:
        raise BridgeError(400, "invalid_grant", "unknown or used code")
    if time.time() - auth.created > AUTH_TTL + CODE_TTL:
        raise BridgeError(400, "invalid_grant", "code expired")
    if form.get("redirect_uri") and form["redirect_uri"] != auth.redirect_uri:
        raise BridgeError(400, "invalid_grant", "redirect_uri mismatch")
    if auth.code_challenge:
        verifier = form.get("code_verifier") or ""
        digest = b64url(hashlib.sha256(verifier.encode()).digest())
        if not hmac.compare_digest(digest, auth.code_challenge):
            raise BridgeError(400, "invalid_grant", "PKCE verification failed")
    auth.code_used = True
    now = int(time.time())
    access_token = secrets.token_urlsafe(32)
    store.tokens[access_token] = (auth.sid, now + TOKEN_TTL)
    id_claims = {
        "iss": svc.settings.public_url.rstrip("/") + "/bridge",
        "aud": auth.client_id,
        "iat": now,
        "exp": now + TOKEN_TTL,
        "auth_time": now,
        **user_claims(auth.claims),
    }
    if auth.nonce:
        id_claims["nonce"] = auth.nonce
    key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(svc.bridge_jwk))
    id_token = jwt.encode(
        id_claims, key, algorithm="ES256", headers={"kid": svc.bridge_jwk.get("kid")}
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "id_token": id_token,
        "scope": "openid profile",
    }


def user_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """OIDC claims from the presented pass. ``sub`` is the member number (immutable, never
    reassigned); ``preferred_username`` is Authentik's username for it."""
    member_number = str(claims["member_number"])
    out: dict[str, Any] = {
        "sub": member_number,
        "member_number": member_number,
        "preferred_username": member_number.lower(),
    }
    if claims.get("npub"):
        out["npub"] = claims["npub"]
    if claims.get("pass_tier"):
        out["pass_tier"] = claims["pass_tier"]
    return out


def userinfo(store: BridgeStore, authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise BridgeError(401, "invalid_token", "bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    entry = store.tokens.get(token)
    if entry is None or entry[1] < time.time():
        raise BridgeError(401, "invalid_token", "unknown or expired token")
    auth = store.sessions.get(entry[0])
    if auth is None or auth.claims is None:
        raise BridgeError(401, "invalid_token", "login gone")
    return user_claims(auth.claims)


def authorize_page(auth: AuthSession) -> str:
    url = auth.authorization_request_url
    body = f"""
<div class="card" id="login" data-sid="{html.escape(auth.sid)}">
<h1>Sign in with your pass</h1>
<p class="muted">Open Source Village · Belfry</p>
<p>Scan this code with the wallet that holds your Open Source Village pass and share your
member number. You will be signed in as soon as the pass is verified.</p>
<p class="qr"><img alt="QR code with the verification request" src="{qr_svg(url)}"></p>
<p><a class="btn" href="{html.escape(url)}">Open the wallet on this device</a></p>
<p id="result" class="muted">Waiting for your wallet…</p>
</div>
<script>
const sid = document.getElementById('login').dataset.sid;
async function poll() {{
  const r = await fetch('/bridge/poll/' + sid); const j = await r.json();
  const el = document.getElementById('result');
  if (j.status === 'done') {{ el.className = 'ok'; el.textContent = 'Pass verified, signing you in…'; window.location = j.redirect; return; }}
  if (j.status === 'failed') {{ el.className = 'bad'; el.textContent = 'The pass was refused: ' + (j.failed || []).join(', '); setTimeout(() => window.location = j.redirect, 2500); return; }}
  setTimeout(poll, 1500);
}}
poll();
</script>"""
    return page("Sign in with your pass", body)


def error_page(exc: BridgeError) -> str:
    body = f"""<div class="card"><h1>Sign-in not possible</h1>
<p class="bad">{html.escape(exc.error)}</p><p>{html.escape(exc.description)}</p></div>"""
    return page("Sign-in not possible", body)


__all__ = [
    "AuthSession",
    "BridgeError",
    "BridgeStore",
    "ClaimError",
    "authorize_page",
    "discovery",
    "error_page",
    "exchange_code",
    "poll",
    "public_jwk",
    "start_authorization",
    "userinfo",
]
