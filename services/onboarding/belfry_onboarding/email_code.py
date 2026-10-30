"""An app proves an e-mail address for its own Nostr key: ``/api/email-code``.

For apps that hold the participant's key themselves (one key per Belfry, derived from the
participant's seed). Every request is NIP-98 signed with that key, as ``/api/invites`` and
``/api/me``; the key needs no pass. Odoo mails a 6-digit code (``belfry_key_code.py``: 10 minutes,
five wrong tries, five codes per address and ten per key in half an hour) and, with the right
code, records the key on the address's contact, creating it when there is none. A contact that
holds another key keeps it: the answer names that key, and moving to the app's key is the
participant's own step (their account page).

    POST /api/email-code           {"email", "app"?}           202 sent (the same for any address)
    POST /api/email-code/verify    {"email", "code", "name"?}  200 bound

Errors are ``{"error", "message"}``: 429 ``too_many``, 422 ``wrong_code`` (with ``tries_left``),
410 ``expired`` (ask for a new code), 409 ``other_key`` (with the contact's ``npub``) or
``key_taken``, 422 ``invalid``, 400 ``invalid_json``, 401 ``unauthorized``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .clients.odoo import OdooError
from .core import Services
from .holder_invites import ApiError, _odoo_message, authenticate

log = logging.getLogger(__name__)

PATH = "/api/email-code"


def _body(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "Send a JSON object.")
    return body


def _text(body: dict[str, Any], key: str, required: bool = False) -> str:
    value = body.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ApiError(422, "invalid", f"{key} must be text.")
    return value.strip()


def _call(svc: Services, method: str, args: list[Any]) -> dict[str, Any]:
    try:
        return svc.odoo.call("belfry.key.code", method, args)
    except OdooError as exc:
        raise ApiError(422, "invalid", _odoo_message(exc)) from exc


def send(svc: Services, pubkey: str, body: dict[str, Any]) -> dict[str, Any]:
    email = _text(body, "email", required=True)
    app = _text(body, "app")[:60]
    row = _call(svc, "belfry_key_code_send", [email, pubkey, app or False])
    if row.get("state") == "too_many":
        raise ApiError(429, "too_many", "Too many codes for this address or key. Try again later.")
    log.info("api: e-mail code for key %s", pubkey)
    return {"state": "sent", "expires_at": row.get("expires_at")}


def verify(svc: Services, pubkey: str, body: dict[str, Any]) -> dict[str, Any]:
    email = _text(body, "email", required=True)
    code = _text(body, "code", required=True)
    name = _text(body, "name")[:100]
    row = _call(svc, "belfry_key_code_verify", [email, pubkey, code, name or False])
    state = row.get("state")
    if state == "wrong_code":
        err = ApiError(422, "wrong_code", "That code is not right.")
        err.extra = {"tries_left": int(row.get("tries_left") or 0)}
        raise err
    if state == "expired":
        raise ApiError(410, "expired", "No usable code: ask for a new one.")
    if state == "key_taken":
        raise ApiError(409, "key_taken", "This key belongs to someone else.")
    if state == "other_key":
        err = ApiError(
            409, "other_key", "This address holds another key in this community. Use that one."
        )
        err.extra = {"npub": row.get("npub")}
        raise err
    log.info("api: key %s verified an e-mail address", pubkey)
    return {
        "state": "bound",
        "email": row.get("email"),
        "npub": row.get("npub"),
        "member": bool(row.get("member")),
    }


def register(app: FastAPI) -> None:
    """Mount /api/email-code (create_app calls this, after holder_invites)."""

    async def signed(request: Request) -> tuple[Services, str, dict[str, Any]]:
        services: Services = request.app.state.services
        raw = await request.body()
        pubkey = authenticate(
            services,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            request.method,
            request.url.path,
            raw,
        )
        return services, pubkey, _body(raw)

    def answer(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    @app.post(PATH)
    async def api_email_code(request: Request):
        services, pubkey, body = await signed(request)
        return answer(await run_in_threadpool(send, services, pubkey, body), 202)

    @app.post(PATH + "/verify")
    async def api_email_code_verify(request: Request):
        services, pubkey, body = await signed(request)
        return answer(await run_in_threadpool(verify, services, pubkey, body))
