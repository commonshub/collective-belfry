"""Invites by e-mail, for a pass holder or an agent acting for them: ``/api/invites``.

There is no API key. Every request carries a NIP-98 authorization (kind 27235) signed with the
holder's Nostr key, through a Keycast bunker or the holder's own signer, exactly as the relays
and Blossom check the same key. The key must hold an active pass, and the holder's rights apply:
a participant's invites count against ``belfry.invites_per_participant``, a steward's (a member of
``STEWARD_GROUP`` in Authentik) do not. Belfry mails the invite link to the invitee
(``belfry_holder_invites.py`` in Odoo); the invitee chooses and pays, and the invite shows the
new participant's npub once the pass is active.

    POST /api/invites                {"email", "name"?, "note"?}   201 the invite (200 ``member``)
    GET  /api/invites                the holder's invites by e-mail, newest first
    GET  /api/invites/<id>           one invite: state, pass_state, npub when active
    POST /api/invites/<id>/revoke    revoke an open invite

A POST signs its body: the event's ``payload`` tag is the body's SHA-256 (NIP-98). An event is
accepted once (replays within its minute are refused). Errors are ``{"error", "message"}``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .blossom import BlossomAuthError, parse_authorization, tag_values
from .clients.odoo import OdooError
from .core import Services, m2o_id
from .wallet_tokens import MAX_AGE, AuthError, check_nip98

log = logging.getLogger(__name__)

PATH = "/api/invites"


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class SeenEvents:
    """Event ids of accepted authorizations, kept while they could still be replayed."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_use(self, event_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            for key in [k for k, t in self._seen.items() if t < now]:
                del self._seen[key]
            if event_id in self._seen:
                return False
            self._seen[event_id] = now + 2 * MAX_AGE + 5
            return True


def authenticate(
    svc: Services, seen: SeenEvents, authorization: str | None, method: str, path: str, body: bytes
) -> str:
    """The pubkey (hex) that signed this request, or raises ApiError 401."""
    # the URL the client signed: the public one (pass.<domain>, or the start page's host, which
    # serves the same site), never the internal address the proxy forwards to
    urls = [u.rstrip("/") + path for u in (svc.settings.public_url, svc.settings.site_url) if u]
    try:
        event = parse_authorization(authorization)
        pubkey = check_nip98(event, method, urls)
    except (AuthError, BlossomAuthError) as exc:
        raise ApiError(401, "unauthorized", str(exc)) from exc
    if body:
        payload = tag_values(event, "payload")
        if payload[:1] != [hashlib.sha256(body).hexdigest()]:
            raise ApiError(401, "unauthorized", "payload tag is not the body's SHA-256")
    if not seen.first_use(event["id"]):
        raise ApiError(401, "unauthorized", "this authorization was used already")
    return pubkey


def holder(svc: Services, pubkey: str) -> dict[str, Any]:
    """The partner behind an active pass of ``pubkey``, and whether they are a steward."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["partner_id"],
        limit=1,
    )
    partner_id = m2o_id(rows[0].get("partner_id")) if rows else None
    if not partner_id:
        raise ApiError(403, "no_pass", "This key holds no active pass.")
    partner = (
        svc.odoo.read_one("res.partner", partner_id, ["name", "email", "belfry_authentik_user_id"])
        or {}
    )
    user_pk = partner.get("belfry_authentik_user_id")
    steward = bool(user_pk) and int(user_pk) in svc.authentik.group_member_pks(
        svc.settings.steward_group
    )
    return {"partner_id": partner_id, "email": partner.get("email") or "", "steward": steward}


def _odoo_message(exc: OdooError) -> str:
    """Odoo's UserError text, without the traceback."""
    text = (exc.data.get("message") or exc.message or "").strip()
    return text.splitlines()[0][:300] if text else "Odoo refused the request."


def _call(svc: Services, method: str, args: list[Any]) -> Any:
    try:
        return svc.odoo.call("belfry.invite", method, args)
    except OdooError as exc:
        raise ApiError(409, "refused", _odoo_message(exc)) from exc


def create_invite(svc: Services, who: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    email = body.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise ApiError(422, "invalid", "email must be an e-mail address.")
    name, note = body.get("name") or "", body.get("note") or ""
    if not isinstance(name, str) or not isinstance(note, str):
        raise ApiError(422, "invalid", "name and note must be text.")
    steward_email = who["email"] if who["steward"] else False
    row = _call(
        svc,
        "belfry_holder_invite",
        [
            who["partner_id"],
            email.strip(),
            name.strip()[:100] or False,
            note.strip()[:200] or False,
            steward_email,
        ],
    )
    log.info(
        "api: holder %s invited by e-mail (invite %s, steward %s)",
        who["partner_id"],
        row.get("id"),
        who["steward"],
    )
    return row


def register(app: FastAPI) -> None:
    """Mount /api/invites (create_app calls this)."""
    app.state.api_seen = SeenEvents()

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            {"error": exc.error, "message": exc.message},
            status_code=exc.status,
            headers={"Cache-Control": "no-store"},
        )

    async def signed_holder(request: Request, body: bytes = b"") -> tuple[Services, dict[str, Any]]:
        services: Services = request.app.state.services
        pubkey = authenticate(
            services,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            request.method,
            request.url.path,
            body,
        )
        return services, await run_in_threadpool(holder, services, pubkey)

    def answer(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    @app.post(PATH)
    async def api_invite(request: Request):
        raw = await request.body()
        services, who = await signed_holder(request, raw)
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "Send the invite as a JSON object.")
        row = await run_in_threadpool(create_invite, services, who, body)
        return answer(row, 200 if row.get("state") == "member" else 201)

    @app.get(PATH)
    async def api_invites(request: Request):
        services, who = await signed_holder(request)
        rows = await run_in_threadpool(
            _call, services, "belfry_holder_invites", [who["partner_id"]]
        )
        return answer({"steward": who["steward"], "invites": rows})

    @app.get(PATH + "/{invite_id}")
    async def api_invite_get(invite_id: int, request: Request):
        services, who = await signed_holder(request)
        row = await run_in_threadpool(
            _call, services, "belfry_holder_invite_get", [who["partner_id"], invite_id]
        )
        if not row:
            raise ApiError(404, "not_found", "No such invite.")
        return answer(row)

    @app.post(PATH + "/{invite_id}/revoke")
    async def api_invite_revoke(invite_id: int, request: Request):
        services, who = await signed_holder(request, await request.body())
        row = await run_in_threadpool(
            _call, services, "belfry_holder_invite_revoke", [who["partner_id"], invite_id]
        )
        return answer(row)
