"""People added to a meeting by its host (Xavier, 2026-11-12): "I should even be able to add someone
that isn't part of the belfry just by name and optionally email. This should create an identity
for them. If no email provided then only the person who created the identity can transfer that new
identity to someone else."

    POST /api/meeting-guests           {"channel": "ev-<d>", "name", "email"?}
        by a member who is a host (admin) of that meeting's chat. With an address: the account
        behind it (webthreads.web_identity, the same as a web thread's author: a member stays
        themselves, anyone else becomes a guest with a key in the signer). Without one: a new
        guest account held by the host (its pass's ``guest_inviter``), "not saved" in the chat.
        → ``{"pubkey", "held"?: true, "member"?: true}``; the person is in the meeting's chat.
    POST /api/meeting-guests/transfer  {"pubkey", "email"}
        by the host who made that name-only account: the address goes on it and the person is
        told they can sign in with it (the sign-in proves the address). → ``{"transferred": true}``

Errors are ``{"error", "message"}``: 422 invalid, 403 not_host / guest, 404 no_channel,
409 not_yours / has_email / taken / unsupported, 503 signer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)

PATH = "/api/meeting-guests"
DAYS = 365  # a meeting's guest stays known (attendance, the chat's history) for a year
EV = re.compile(r"^ev-\d{4}-[a-z0-9]{6}$")


def add(svc: Services, caller: str, channel: str, name: str, email: str) -> dict[str, Any]:
    from . import core, guests, mail, mentions, webthreads
    from .places import member_key
    from .holder_invites import ApiError

    if not mail.channel_exists(svc, channel):
        raise ApiError(404, "no_channel", "No such meeting.")
    if caller not in mail.group_admins(svc, channel):
        raise ApiError(403, "not_host", "Only the meeting's hosts add people to it.")
    if email:
        got = webthreads.web_identity(svc, channel, name, email)
        if got is None:
            raise ApiError(503, "signer", "The account could not be made just now. Try again.")
        member = bool(member_key(svc, got["pubkey"]))
        if member:  # a member: just into the meeting's chat
            guests._channel_event(svc, 9000, [["h", channel], ["p", got["pubkey"], "member"]])
        return {"pubkey": got["pubkey"], **({"member": True} if member else {})}
    made = svc.odoo.held_guest(channel, name, caller, DAYS)
    pid = int(made["partner_id"])
    row = svc.odoo.read_one("res.partner", pid, ["name"]) or {}
    partner = {"id": pid, "name": row.get("name") or name}
    try:
        pubkey, npub, _ucan = core._ensure_keycast_user(svc, partner, f"guest-{pid}")
    except Exception as exc:  # noqa: BLE001 — the signer is down: the pass stays unused
        svc.odoo.write("belfry.pass", [made["pass_id"]], {"state": "revoked"})
        log.warning("meeting guest: no key in the signer: %r", exc)
        raise ApiError(503, "signer", "The account could not be made just now. Try again.") from exc
    svc.odoo.write("res.partner", [pid], {"belfry_npub": npub})
    svc.odoo.write("belfry.pass", [made["pass_id"]], {"keycast_pubkey": pubkey, "state": "active"})
    mentions.save(svc, pid, f="off")  # no address yet: nothing to mail
    guests.admitted(svc, made["pass_id"])
    return {"pubkey": pubkey, "held": True}


def transfer(svc: Services, caller: str, pubkey: str, email: str) -> dict[str, Any]:
    from . import guests, webthreads
    from .holder_invites import ApiError

    out = svc.odoo.transfer_held_guest(caller, pubkey, email)
    state = out.get("state")
    if state == "not_yours":
        raise ApiError(409, "not_yours", "Only who added this person can hand their account over.")
    if state == "has_email":
        raise ApiError(409, "has_email", "This account has an e-mail already.")
    if state == "taken":
        raise ApiError(409, "taken", "That address has an account already.")
    for ch in out.get("channels") or []:
        guests.saved_label(svc, pubkey, ch)
    base = (svc.settings.public_url or "").rstrip("/")
    chat = base.replace("://", "://chat.", 1) if base and "://chat." not in base else base
    try:
        webthreads._mailer(svc).send(
            email,
            "Your account is ready",
            [
                "Someone you met added you to a meeting and made you an account, so you can see "
                "it, its chat and who was there.",
                f"Sign in with this address ({email}) to use it.",
            ],
            button=(chat or base, "Open the chat") if (chat or base) else None,
        )
    except Exception as exc:  # noqa: BLE001 — the address is saved either way
        log.warning("meeting guest: the hand-over mail failed: %r", exc)
    return {"transferred": True}


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    async def signed(request: Request, path: str) -> tuple[Services, str, dict[str, Any]]:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", path, body
        )
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        from . import guests, invite

        if not invite.own(s) or not hasattr(s.odoo, "held_guest"):
            raise ApiError(409, "unsupported", "This needs the belfry's own register.")
        rows = await run_in_threadpool(
            s.odoo.search_read,
            "belfry.pass",
            [["state", "=", "active"], ["keycast_pubkey", "=", caller]],
            ["tier"],
        )
        if not rows:
            raise ApiError(403, "guest", "Only members add people to a meeting.")
        guests._inviter(rows)
        return s, caller, data

    @app.post(PATH)
    async def meeting_guest(request: Request) -> JSONResponse:
        s, caller, data = await signed(request, PATH)
        channel = str(data.get("channel") or "")
        name = str(data.get("name") or "").strip()[:100]
        email = str(data.get("email") or "").strip().lower()
        if not EV.match(channel) or not name or (email and ("@" not in email or len(email) > 254)):
            raise ApiError(422, "invalid", 'Send {"channel": "ev-…", "name", "email"?}.')
        out = await run_in_threadpool(add, s, caller, channel, name, email)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(PATH + "/transfer")
    async def meeting_guest_transfer(request: Request) -> JSONResponse:
        s, caller, data = await signed(request, PATH + "/transfer")
        pubkey = str(data.get("pubkey") or "").lower()
        email = str(data.get("email") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", pubkey) or "@" not in email or len(email) > 254:
            raise ApiError(422, "invalid", 'Send {"pubkey": "<hex>", "email"}.')
        out = await run_in_threadpool(transfer, s, caller, pubkey, email)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
