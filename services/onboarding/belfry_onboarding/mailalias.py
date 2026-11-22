"""A channel's e-mail address, chosen by a belfry admin (docs/mail.md, "A channel's address").

A channel's address is derived: the plain slug of its name when that reaches it, else its id
(``mailforward.alias``). That ties the address to the name (#contact can't be hello@) and to who
made the channel. So a belfry admin may give a channel an address of their choice.

- The setting is the belfry's, not a Nostr event (an address is routing, and a member's event
  could not be trusted with it): register params ``belfry.mail_alias.<channel>`` (the alias) and
  ``belfry.mail_alias_of.<alias>`` (the channel, so an alias has one owner whatever was written
  before). REGISTER=belfry only.
- ``POST /api/mail-alias`` (belfry.<domain>/api/v1/mail-alias, NIP-98): reading and checking by
  whoever may edit the channel (``mailforward.may_edit``), setting by the belfry's admins only.
- One way in and one way out: ``mail.channel_id`` asks ``channel_of`` first (mail in, the From
  a shared inbox may answer as, and the names a member can't take as a username),
  ``mailforward.alias`` asks ``custom`` first (the From of forwarded copies, what the apps show).
- The channel's id and its plain slug keep reaching it: nothing bounces after a change. An
  alias an admin replaced or took away is free again.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)

PATH = "/api/mail-alias"
PARAM = "belfry.mail_alias."  # + channel → its alias
OWNER = "belfry.mail_alias_of."  # + alias → its channel
VALID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,29}$")
# names people can't take as a username because they could pass for the belfry, and that are
# just what a channel is for: an admin may give them to one (hello@ for #contact)
FOR_CHANNELS = frozenset({"hello", "info", "support", "help"})
# names with a meaning of their own in the mail system (mail.py, nostr.json), on top of the
# names nobody gets as a username
OWN_MEANING = frozenset({"postmaster", "abuse", "bounces", "_smtp", "_groups"})

_lock = threading.Lock()  # one alias written at a time: two admins can't both get a name


def reserved() -> frozenset[str]:
    from . import usernames

    return frozenset((usernames.RESERVED | OWN_MEANING) - FOR_CHANNELS)


def own(svc: Services) -> bool:
    from .register import Register

    return isinstance(svc.odoo, Register)


# ── the stored alias ────────────────────────────────────────────────────────────────────────
def custom(svc: Services, channel: str) -> str:
    """The alias an admin chose for CHANNEL, "" for none (or without the own register)."""
    if not own(svc) or not channel:
        return ""
    try:
        name = str(svc.odoo.param(PARAM + channel, "") or "")
    except Exception as exc:  # noqa: BLE001 — the address falls back to the derived one
        log.info("mail: alias of %s not read: %s", channel, type(exc).__name__)
        return ""
    return name if VALID.fullmatch(name) else ""


def channel_of(svc: Services, local: str) -> str | None:
    """The channel whose chosen alias is LOCAL, None for none. Both params must agree: an
    owner record left behind by a half-done change names nobody."""
    if not own(svc) or not VALID.fullmatch(local or ""):
        return None
    try:
        channel = str(svc.odoo.param(OWNER + local, "") or "")
    except Exception as exc:  # noqa: BLE001
        log.info("mail: alias owner not read: %s", type(exc).__name__)
        return None
    return channel if channel and custom(svc, channel) == local else None


def _forget(*names: str) -> None:
    """What mail.resolve and usernames.holder remember about NAMES for a minute goes."""
    from . import mail, usernames

    with mail._lock:
        for name in names:
            mail._cache.pop(name, None)
    for name in names:
        usernames._taken.pop(name, None)


def _write(svc: Services, channel: str, name: str) -> str:
    """Store NAME ("" = none) as CHANNEL's alias; returns the alias it had."""
    old = custom(svc, channel)
    if old and old != name and channel_of(svc, old) == channel:
        svc.odoo.set_param(OWNER + old, "")
    if name:
        svc.odoo.set_param(OWNER + name, channel)
    svc.odoo.set_param(PARAM + channel, name)
    _forget(*(n for n in (old, name) if n))
    return old


# ── is a name free ──────────────────────────────────────────────────────────────────────────
def clean(svc: Services, wanted: Any) -> str:
    """WANTED as typed: trimmed, lowercased, without "@<this belfry's domain>"."""
    name = str(wanted or "").strip().lower()
    dom = (svc.settings.nip05_domain or "").lower()
    return name.removesuffix("@" + dom) if dom else name


def _person(svc: Services, name: str) -> str:
    """Whose username (or guest page) NAME is, "" for nobody. Unlike usernames.holder, a signer
    that does not answer is an error: an alias written over a member's address would take
    nothing from them (members come first, mail.resolve) but could never work."""
    from . import usernames
    from .holder_invites import ApiError

    try:
        who = str(svc.keycast.pubkey_for_name(name) or "")
    except Exception as exc:  # noqa: BLE001
        log.info("mail: signer not asked about an alias: %s", type(exc).__name__)
        raise ApiError(503, "signer", "Try again in a moment.") from exc
    return who or usernames._guest_slug(svc, name)


def state(svc: Services, channel: str, name: str) -> str:
    """Whether NAME (cleaned) can be CHANNEL's address: ``available``, ``taken``, ``invalid``
    or ``reserved``. Whatever reaches CHANNEL today (its alias, its plain slug, its id) is
    available to it, a reserved word included: #support already answers at support@."""
    from . import mail, mailforward, usernames

    if not VALID.fullmatch(name):
        return "invalid"
    if name == mailforward.alias(svc, channel):
        return "available"
    reaches = mail.channel_id(svc, name)
    if reaches == channel:
        return "taken" if _person(svc, name) else "available"
    if name in reserved() or usernames.DEFAULT.fullmatch(name):
        return "reserved"
    if reaches or _person(svc, name):
        return "taken"
    return "available"


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def info(svc: Services, caller: str, channel: str) -> dict[str, Any]:
    from . import checkin, mailforward

    name = mailforward.alias(svc, channel)
    return {
        "channel": channel,
        "alias": name,
        "address": f"{name}@{(svc.settings.nip05_domain or '').lower()}",
        "custom": bool(custom(svc, channel)),
        "may_set": caller in checkin._belfry_admins(svc),
    }


def api(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from . import checkin, mail, mailforward
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "A chosen address needs the belfry's own register.")
    channel = str(data.get("channel") or "").strip()
    if not mailforward.GROUP_RE.match(channel):
        raise ApiError(422, "invalid", 'Send {"channel"}.')
    if not mail.channel_exists(svc, channel):
        raise ApiError(404, "unknown_channel", "No such channel.")
    # Reading the address is for every member (their app writes to it and answers from it;
    # the lead's change, 2026-11-22). A check tells which names are taken: the channel's admins.
    from . import whois

    if whois.kinds(svc).get(caller) not in ("member", "agent"):
        raise ApiError(403, "not_allowed", "Only members see a channel's address.")
    if ("check" in data or "alias" in data) and not mailforward.may_edit(svc, caller, channel):
        raise ApiError(403, "not_allowed", "Only the channel's admins see this.")
    if "alias" in data:
        if caller not in checkin._belfry_admins(svc):
            raise ApiError(403, "not_allowed", "Only the belfry's admins choose an address.")
        wanted = data["alias"]
        if wanted is None or wanted == "":
            with _lock:
                old = _write(svc, channel, "")
            if old:
                log.info("mail: address of %s back to automatic by %s", channel, caller[:12])
            return info(svc, caller, channel)
        if not isinstance(wanted, str):
            raise ApiError(422, "invalid", "An address is a word.")
        name = clean(svc, wanted)
        if checkin.is_archived(svc, channel):
            raise ApiError(409, "archived", "This channel is archived. Unarchive it first.")
        with _lock:
            found = state(svc, channel, name)
            if found == "invalid":
                raise ApiError(
                    422,
                    "invalid",
                    "An address is 1 to 30 lowercase letters, digits, dots, dashes or "
                    "underscores, starting with a letter or a digit.",
                )
            if found == "reserved":
                raise ApiError(409, "reserved", "That name is kept for the belfry.")
            if found == "taken":
                raise ApiError(409, "taken", "That name is taken.")
            _write(svc, channel, name)
        log.info("mail: address of %s set to %s@ by %s", channel, name, caller[:12])
        return info(svc, caller, channel)
    if "check" in data:
        name = clean(svc, data["check"])[:64]
        return {"state": state(svc, channel, name), "alias": name}
    return info(svc, caller, channel)


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.post(PATH)
    async def mail_alias(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        out = await run_in_threadpool(api, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})


__all__ = ["register", "api", "custom", "channel_of", "state", "reserved"]
