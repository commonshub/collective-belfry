"""One-shot messages after a form post ("Saved.", "That code is not right."), carried in a
short-lived signed cookie, never in the URL: a page must not show text taken from its address,
or anyone could send a link that makes the belfry say whatever they want.

    response = RedirectResponse("/members", status_code=303)
    flash.put(response, svc, msg="Saved.")
    ...
    msg, err = flash.take(request, svc, response)   # read once; the cookie is cleared
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

COOKIE = "belfry_flash"
SECONDS = 120
_FALLBACK = secrets.token_bytes(32)  # no pass secret configured: a per-process key is enough


def _key(svc: Any) -> str | bytes:
    return getattr(getattr(svc, "settings", None), "pass_page_secret", "") or _FALLBACK


def put(response: Response, svc: Any, msg: str = "", err: str = "") -> Response:
    if msg or err:
        now = int(time.time())
        claims = {"kind": "flash", "msg": msg[:300], "err": err[:300], "exp": now + SECONDS}
        token = jwt.encode(claims, _key(svc), "HS256")
        response.set_cookie(COOKIE, token, max_age=SECONDS, httponly=True, samesite="lax", path="/")
    return response


def redirect(svc: Any, to: str, msg: str = "", err: str = "") -> RedirectResponse:
    return put(RedirectResponse(to, status_code=303), svc, msg, err)  # type: ignore[return-value]


def peek(request: Request, svc: Any) -> tuple[str, str]:
    token = request.cookies.get(COOKIE)
    claims: dict[str, Any] = {}
    if token:
        try:
            claims = jwt.decode(token, _key(svc), algorithms=["HS256"])
        except jwt.PyJWTError:
            claims = {}
    if claims.get("kind") != "flash":
        return "", ""
    return str(claims.get("msg") or ""), str(claims.get("err") or "")


def clear(response: Response, request: Request) -> Response:
    if COOKIE in request.cookies:
        response.delete_cookie(COOKIE, path="/")
    return response
