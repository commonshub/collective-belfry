"""Belfry as the OpenID provider of one client: walt.id Issuer2's external login, which signs
the participant in during an authorization-code credential offer (the claim page's sign-in
offer, HAIP wallets). Replaces Authentik's ``belfry-issuer`` application (2026-11-19).

    GET  /oidc/.well-known/openid-configuration
    GET  /oidc/jwks
    GET  /oidc/authorize   needs Belfry's session (else the login page first), issues a code
    POST /oidc/token       client secret (basic or post) and PKCE; an ES256 ID token
    GET  /oidc/userinfo

``sub`` is the member number. The issuer takes the credential's data from the offer
(``issuer_state``), not from these claims; the sign-in only proves the wallet's holder is a
participant. Codes and tokens are in memory (one instance).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import jwt

from .core import Services

log = logging.getLogger(__name__)

CODE_TTL = 300
TOKEN_TTL = 300
SCOPES = ("openid", "profile", "email")


class OidcError(Exception):
    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status = status
        self.error = error
        self.description = description


@dataclass
class Grant:
    client_id: str
    redirect_uri: str
    nonce: str | None
    code_challenge: str | None
    claims: dict[str, Any]
    created: float = field(default_factory=time.time)
    used: bool = False


class OidcStore:
    def __init__(self) -> None:
        self.codes: dict[str, Grant] = {}
        self.tokens: dict[str, tuple[dict[str, Any], float]] = {}

    def sweep(self) -> None:
        now = time.time()
        for code in [c for c, g in self.codes.items() if now - g.created > CODE_TTL]:
            self.codes.pop(code, None)
        for tok in [t for t, (_c, exp) in self.tokens.items() if exp < now]:
            self.tokens.pop(tok, None)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def issuer(svc: Services) -> str:
    return svc.settings.public_url.rstrip("/") + "/oidc"


def discovery(svc: Services) -> dict[str, Any]:
    base = issuer(svc)
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
        "claims_supported": ["sub", "member_number", "name", "email", "npub"],
    }


def public_jwk(jwk: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in jwk.items() if k in ("kty", "crv", "x", "y", "kid", "alg", "use")}


def check_request(svc: Services, params: dict[str, str]) -> None:
    s = svc.settings
    if not s.issuer_oidc_client_secret or params.get("client_id") != s.issuer_oidc_client_id:
        raise OidcError(400, "invalid_client", "unknown client_id")
    if params.get("response_type") != "code":
        raise OidcError(400, "unsupported_response_type", "only response_type=code")
    redirect_uri = params.get("redirect_uri") or ""
    # the issuer's own callback (<ISSUER_PUBLIC_URL>/openid4vci/external/oauth/callback)
    if not redirect_uri.startswith(s.issuer_public_url.rstrip("/") + "/"):
        raise OidcError(400, "invalid_request", "redirect_uri is not the issuer's")
    if params.get("code_challenge") and params.get("code_challenge_method") not in (None, "S256"):
        raise OidcError(400, "invalid_request", "only code_challenge_method=S256")


def member_claims(svc: Services, partner_id: int) -> dict[str, Any]:
    partner = svc.odoo.read_one(
        "res.partner", partner_id, ["name", "email", "belfry_member_number", "belfry_npub"]
    )
    if not partner or not partner.get("belfry_member_number"):
        raise OidcError(403, "access_denied", "no member number")
    claims: dict[str, Any] = {
        "sub": partner["belfry_member_number"],
        "member_number": partner["belfry_member_number"],
        "name": partner.get("name") or partner["belfry_member_number"],
    }
    if partner.get("email"):
        claims["email"] = partner["email"]
        claims["email_verified"] = True
    if partner.get("belfry_npub"):
        claims["npub"] = partner["belfry_npub"]
    return claims


def authorize(svc: Services, store: OidcStore, params: dict[str, str], partner_id: int) -> str:
    """The redirect back to the client with a code, for the signed-in partner."""
    store.sweep()
    check_request(svc, params)
    if not svc.odoo.call("belfry.login.code", "belfry_may_sign_in", [partner_id]):
        raise OidcError(403, "access_denied", "no active pass")
    code = secrets.token_urlsafe(32)
    store.codes[code] = Grant(
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        nonce=params.get("nonce"),
        code_challenge=params.get("code_challenge"),
        claims=member_claims(svc, partner_id),
    )
    out = {"code": code}
    if params.get("state"):
        out["state"] = params["state"]
    sep = "&" if "?" in params["redirect_uri"] else "?"
    return params["redirect_uri"] + sep + urllib.parse.urlencode(out)


def authenticate_client(svc: Services, form: dict[str, str], authorization: str | None) -> None:
    client_id, secret = form.get("client_id"), form.get("client_secret")
    if authorization and authorization.lower().startswith("basic "):
        try:
            raw = base64.b64decode(authorization.split(" ", 1)[1]).decode()
            client_id, secret = raw.split(":", 1)
            client_id, secret = urllib.parse.unquote(client_id), urllib.parse.unquote(secret)
        except Exception as exc:
            raise OidcError(401, "invalid_client", "bad basic auth") from exc
    expected = svc.settings.issuer_oidc_client_secret
    if (
        not expected
        or client_id != svc.settings.issuer_oidc_client_id
        or not secret
        or not hmac.compare_digest(secret, expected)
    ):
        raise OidcError(401, "invalid_client", "client authentication failed")


def exchange_code(
    svc: Services, store: OidcStore, form: dict[str, str], authorization: str | None
) -> dict[str, Any]:
    authenticate_client(svc, form, authorization)
    if form.get("grant_type") != "authorization_code":
        raise OidcError(400, "unsupported_grant_type")
    grant = store.codes.pop(form.get("code") or "", None)
    if grant is None or grant.used or time.time() - grant.created > CODE_TTL:
        raise OidcError(400, "invalid_grant", "unknown, used or expired code")
    if form.get("redirect_uri") and form["redirect_uri"] != grant.redirect_uri:
        raise OidcError(400, "invalid_grant", "redirect_uri mismatch")
    if grant.code_challenge:
        digest = b64url(hashlib.sha256((form.get("code_verifier") or "").encode()).digest())
        if not hmac.compare_digest(digest, grant.code_challenge):
            raise OidcError(400, "invalid_grant", "PKCE verification failed")
    grant.used = True
    now = int(time.time())
    access_token = secrets.token_urlsafe(32)
    store.tokens[access_token] = (grant.claims, now + TOKEN_TTL)
    id_claims = {
        "iss": issuer(svc),
        "aud": grant.client_id,
        "iat": now,
        "exp": now + TOKEN_TTL,
        "auth_time": now,
        **grant.claims,
    }
    if grant.nonce:
        id_claims["nonce"] = grant.nonce
    if not svc.bridge_jwk:
        raise OidcError(503, "temporarily_unavailable", "no signing key (BRIDGE_JWK_PATH)")
    key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(svc.bridge_jwk))
    id_token = jwt.encode(
        id_claims, key, algorithm="ES256", headers={"kid": svc.bridge_jwk.get("kid")}
    )
    log.info("oidc: %s signed in to %s", grant.claims.get("sub"), grant.client_id)
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "id_token": id_token,
        "scope": "openid profile email",
    }


def userinfo(store: OidcStore, authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise OidcError(401, "invalid_token", "bearer token required")
    entry = store.tokens.get(authorization.split(" ", 1)[1].strip())
    if entry is None or entry[1] < time.time():
        raise OidcError(401, "invalid_token", "unknown or expired token")
    return dict(entry[0])
