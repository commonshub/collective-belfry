"""Belfry's sign-in session: one signed cookie on PUBLIC_URL's host (pass.<domain>).

Replaces the Authentik session (2026-11-19). The login page (login.py) sets it after an e-mail
link or code, the pass from a wallet, or a one-time link from the activation mail (signin.py).
The account page, the apps' sign-in (connect.py), the pass on the phone (/me) and the steward
pages read it. It names the Odoo partner, when and how they signed in; whether they may still
sign in (an active pass, or a steward) is asked of Odoo where it matters.

The cookie is HttpOnly, SameSite=Lax (the apps' sign-in is a top-level navigation) and Secure
on https. Logging out clears it; the apps' connections are revoked by the apps themselves.
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import Response

COOKIE = "belfry_session"
ALG = "HS256"


@dataclass(frozen=True)
class Session:
    partner_id: int
    auth_time: int  # when the member last proved who they are (link, code, pass)
    method: str  # email, link, pass, welcome


def _secret(settings: Any) -> str:
    secret = settings.session_secret or ""
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET is not set (32 characters at least)")
    return secret


def configured(settings: Any) -> bool:
    return len(settings.session_secret or "") >= 32


def encode(settings: Any, partner_id: int, method: str, auth_time: int | None = None) -> str:
    now = int(time.time())
    claims = {
        "typ": "belfry-session",
        "pid": int(partner_id),
        "auth": int(auth_time or now),
        "m": method,
        "iat": now,
        "exp": now + int(settings.session_days) * 86400,
    }
    return jwt.encode(claims, _secret(settings), algorithm=ALG)


def decode(settings: Any, value: str | None) -> Session | None:
    if not value or not configured(settings):
        return None
    try:
        claims = jwt.decode(value, _secret(settings), algorithms=[ALG])
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "belfry-session" or not isinstance(claims.get("pid"), int):
        return None
    return Session(int(claims["pid"]), int(claims.get("auth") or 0), str(claims.get("m") or ""))


def current(request: Request) -> Session | None:
    settings = request.app.state.services.settings
    return decode(settings, request.cookies.get(COOKIE))


def is_fresh(settings: Any, session: Session | None) -> bool:
    """Signed in within SESSION_FRESH_SECONDS (key export, self-custody)."""
    return bool(session) and time.time() - session.auth_time <= settings.session_fresh_seconds


def _secure(settings: Any) -> bool:
    return settings.public_url.startswith("https://")


def set_cookie(
    response: Response, settings: Any, partner_id: int, method: str, auth_time: int | None = None
) -> None:
    response.set_cookie(
        COOKIE,
        encode(settings, partner_id, method, auth_time),
        max_age=int(settings.session_days) * 86400,
        path="/",
        httponly=True,
        secure=_secure(settings),
        samesite="lax",
    )


def clear_cookie(response: Response, settings: Any) -> None:
    response.delete_cookie(
        COOKIE, path="/", secure=_secure(settings), httponly=True, samesite="lax"
    )


# -- where to go after signing in ---------------------------------------------------------------


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def allowed_origins(settings: Any) -> set[str]:
    """Origins a sign-in or logout may end at: this service, the start page, Belfry's apps
    (their registered redirect URIs) and the public app URLs."""
    urls = [settings.public_url, settings.site_url, settings.apps_public_url]
    urls += [
        settings.chat_public_url,
        settings.calendar_public_url,
        settings.wallet_public_url,
    ]
    for entry in (settings.oauth_clients or "").split(","):
        if "=" in entry:
            urls.append(entry.split("=", 1)[1].strip())
    for entry in (settings.community_apps or "").split(";"):
        parts = entry.split("|")
        if len(parts) >= 2:
            urls.append(parts[1].strip())
    return {o for o in (_origin(u) for u in urls if u) if o}


def safe_next(
    settings: Any, target: str | None, default: str = "/account", extra: set[str] = frozenset()
) -> str:
    """A path on this service, or a URL on an allowed origin (or one of ``extra``: the origins
    of apps signed in to lately, for their logout); anything else is ``default``."""
    target = (target or "").strip()
    if not target:
        return default
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    origin = _origin(target)
    if origin and (origin in allowed_origins(settings) or origin in extra):
        return target
    return default
