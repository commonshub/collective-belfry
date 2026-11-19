"""The first minute of a member who came with a member link: their name and their username
(Xavier, 2026-11-19; docs/membership.md, "A name and a username").

A member link asks for an e-mail address and its code only (guests.py); the name starts from the
address. Right after the code the chat asks for both and sends them here:

``POST /api/welcome`` (belfry.<domain>/api/v1/welcome) ``{"name", "username"}``, NIP-98 by the
new member's own key → ``{"name", "username", "nip05"}``. The contact gets the name, the signer
the username, the profile (kind 0) ``name``, ``display_name`` and ``nip05`` (its picture and
about stay). For a member whose username is still a default one or whose membership started less
than a day ago; later it is their profile screen (``POST /api/username``). Errors: 400
``missing``, 403 ``not_member`` (guests, agents), 403 ``unverified``, 409 ``own_key``, 409
``already_set``, 422 ``username_invalid``, 409 ``username_taken`` (with ``suggestions``).

The welcome mail of such a member waits for this call, so it names the address they picked; it
leaves by itself ten minutes after the membership started when the call never comes (``tick``,
on the followers' round). One welcome mail either way (register ``belfry_welcome_release``).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id

log = logging.getLogger(__name__)

PATH = "/api/welcome"
HOLD_SECONDS = 600  # how long the welcome mail waits for the name and username
FRESH_HOURS = 24  # how long after joining this call still sets them


def _ago(seconds: float) -> str:
    then = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(seconds=seconds)
    return then.strftime("%Y-%m-%d %H:%M:%S")


def _passes(svc: Services, partner: int) -> list[dict[str, Any]]:
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", partner]],
        ["tier", "state", "activated_at", "welcome_hold"],
    )
    return [r for r in rows if r.get("tier") not in ("guest", "agent")]


def _fresh(svc: Services, partner: int) -> bool:
    """Whether PARTNER's first membership started less than FRESH_HOURS ago."""
    started = sorted(str(r["activated_at"]) for r in _passes(svc, partner) if r.get("activated_at"))
    return bool(started) and started[0] > _ago(FRESH_HOURS * 3600)


def release(svc: Services, partner: int) -> bool:
    """PARTNER's welcome mail, if it still waits, with the address they have now."""
    from . import community, usernames

    sent = False
    for r in _passes(svc, partner):
        if not r.get("welcome_hold") or r.get("state") != "active":
            continue
        name = ""
        try:
            found = community.member_token(svc, partner)
            name = usernames.current(svc, found[1]) if found else ""
        except Exception as exc:  # noqa: BLE001 — the mail then names no address
            log.info("welcome: no username for partner %s: %s", partner, exc)
        if usernames.DEFAULT.fullmatch(name):
            name = ""
        sent = (
            bool(svc.odoo.call("belfry.pass", "belfry_welcome_release", [r["id"], name or False]))
            or sent
        )
    return sent


def tick(svc: Services) -> None:
    """The welcome mails nobody released (the chat never called): out after HOLD_SECONDS."""
    from . import invite

    if not invite.own(svc):
        return
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["welcome_hold", "=", True]],
        ["partner_id", "activated_at"],
    )
    for r in rows:
        partner = m2o_id(r.get("partner_id"))
        if not partner or str(r.get("activated_at") or "") > _ago(HOLD_SECONDS):
            continue
        try:
            release(svc, partner)
        except Exception as exc:  # noqa: BLE001 — the next round tries again
            log.warning("welcome: mail of partner %s not released: %s", partner, exc)


def finish(svc: Services, pubkey: str, body: dict[str, Any]) -> dict[str, str]:
    from . import community, mail, sales, usernames
    from .holder_invites import ApiError
    from .places import member_key

    row = member_key(svc, pubkey)
    partner = m2o_id(row.get("partner_id")) if row else None
    if not partner or (row or {}).get("tier") == "agent":
        raise ApiError(403, "not_member", "Only members have an address here.")
    name = " ".join(str(body.get("name") or "").split())[:100]
    if not name or not str(body.get("username") or "").strip():
        raise ApiError(400, "missing", "Send your name and the username you want.")
    if sales.is_unverified(svc, partner):
        raise ApiError(
            403, "unverified", "Confirm your e-mail address first (the link in your receipt)."
        )
    found = community.member_token(svc, partner)
    if found is None or found[0] != pubkey:
        raise ApiError(
            409, "own_key", "You hold your own key: set your address in your own profile."
        )
    token = found[1]
    have = usernames.current(svc, token)
    if have and not usernames.DEFAULT.fullmatch(have) and not _fresh(svc, partner):
        raise ApiError(409, "already_set", "Change your name and username in your profile.")
    wanted = usernames.require(svc, body.get("username"), name, pubkey)
    if have != wanted:
        try:
            svc.keycast.set_username(token, wanted)
        except ValueError as exc:  # taken since the check
            err = ApiError(409, "username_taken", "That name is taken.")
            err.extra["suggestions"] = usernames.suggestions(svc, name, pubkey)
            raise err from exc
        with mail._lock:
            mail._cache.clear()  # the old and the new address resolve afresh
        usernames._taken.pop(wanted, None)
    svc.odoo.write("res.partner", [partner], {"name": name})
    usernames.publish_nip05(
        svc,
        pubkey,
        lambda u: svc.keycast.sign_event(token, {"pubkey": pubkey, **u}),
        wanted,
        {"name": name, "display_name": name},
    )
    log.info("welcome: partner %s is %s now", partner, wanted)
    try:
        release(svc, partner)
    except Exception as exc:  # noqa: BLE001 — the round sends it
        log.warning("welcome: mail of partner %s not released: %s", partner, exc)
    return {"name": name, "username": wanted, "nip05": f"{wanted}@{svc.settings.nip05_domain}"}


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.post(PATH)
    async def welcome(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        pubkey = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", 'Send {"name": "…", "username": "…"}.')
        out = await run_in_threadpool(finish, s, pubkey, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
