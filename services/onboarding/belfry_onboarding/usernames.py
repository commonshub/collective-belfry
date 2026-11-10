"""A member's username: ``<username>@<domain>`` is their address (NIP-05, and their mailbox).

The signer keeps it (Keycast's ``username``). A new member starts with their name as a slug
("Ana María Pérez" → ``ana-maria-perez``; taken → ``ana-maria-perez-2``), not with their member
number, and changes it from their profile in the chat: ``POST /api/username`` (belfry.<domain>/
api/v1/username), NIP-98 by the member's own key, ``{"username": "<new>"}`` → ``{"username",
"nip05"}``. Errors: ``invalid`` (422), ``taken`` (409, also the reserved names and the names of
channels, which have a mailbox too), ``not_member`` (403: guests and agents have no address),
``own_key`` (409: a member who holds their own key sets NIP-05 in their own profile).

The profile (kind 0) on the community relay gets the new ``nip05`` and keeps everything else.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)

PATH = "/api/username"
MAX = 30
VALID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,28}[a-z0-9])?$")
# addresses with a meaning of their own (mail.py, nostr.json) or that could pass for the belfry
RESERVED = {
    "postmaster",
    "abuse",
    "admin",
    "administrator",
    "root",
    "hostmaster",
    "webmaster",
    "security",
    "support",
    "help",
    "info",
    "hello",
    "noreply",
    "no-reply",
    "mailer-daemon",
    "belfry",
    "_smtp",
    "_groups",
    "_",
}
# what an account is called before it has a name of its own: a member number ("0007",
# "osv-0007"), "member-<id>", "guest-<id>"
DEFAULT = re.compile(r"^(?:\d+|[a-z]{2,6}-\d{2,}|member-\d+|guest-\d+)$")


def slug(name: str) -> str:
    """A name as a username: ASCII letters and digits, dashes between the words."""
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    out = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:MAX].strip("-")
    return out


def _free(svc: Services, name: str) -> bool:
    """Not reserved and not the name of a channel (channels receive mail at <channel>@)."""
    if name in RESERVED or not VALID.fullmatch(name):
        return False
    try:
        from . import mail

        return not mail.channel_exists(svc, name)
    except Exception as exc:  # noqa: BLE001 — the relay not answering: the signer still checks
        log.info("usernames: channels not checked for %s: %s", name, exc)
        return True


def current(svc: Services, token: str) -> str:
    try:
        return str((svc.keycast.get_profile(token) or {}).get("username") or "").lower()
    except Exception as exc:  # noqa: BLE001
        log.info("usernames: profile not read: %s", exc)
        return ""


def give_default(svc: Services, token: str, name: str, fallback: str) -> str:
    """The account's username after making sure it is not a bare number: a member who never chose
    one gets their name as a slug (with -2, -3… when taken). Returns the username the account
    has now; FALLBACK when nothing could be read or set (best effort: the account works)."""
    fallback = fallback.lower()
    if not token:
        return fallback
    have = current(svc, token)
    if have and not DEFAULT.fullmatch(have):
        return have  # chosen already (by them, or an earlier default)
    base = slug(name)
    if not base or DEFAULT.fullmatch(base):
        return have or fallback
    for n in range(1, 30):
        tail = "" if n == 1 else f"-{n}"
        wanted = base[: MAX - len(tail)].strip("-") + tail
        if not _free(svc, wanted):
            continue
        try:
            svc.keycast.set_username(token, wanted)
            return wanted
        except ValueError:  # taken
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("usernames: %s not set: %s", wanted, exc)
            break
    return have or fallback


def publish_nip05(svc: Services, pubkey: str, sign: Any, username: str) -> bool:
    """The member's profile with the new address; everything else in it stays."""
    from . import community

    if svc.relay is None:
        return False
    try:
        found = community.query(svc, [{"kinds": [0], "authors": [pubkey], "limit": 1}])
        content: Any = {}
        if found:
            try:
                content = json.loads(found[0].get("content") or "{}")
            except ValueError:
                content = {}
        if not isinstance(content, dict):
            content = {}
        nip05 = f"{username}@{svc.settings.nip05_domain}"
        if found and content.get("nip05") == nip05:
            return False
        content["nip05"] = nip05
        event = sign(
            {"kind": 0, "created_at": int(time.time()), "tags": [], "content": json.dumps(content)}
        )
        svc.relay.publish(event, sign)
        return True
    except Exception as exc:  # noqa: BLE001 — the address works without the profile line
        log.warning("usernames: profile of %s not updated: %s", username, exc)
        return False


def backfill(svc: Services) -> dict[str, str]:
    """Every member whose username is still a number gets their name (CLI ``usernames``)."""
    from . import community, core

    done: dict[str, str] = {}
    seen: set[int] = set()
    for row in svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"]], ["partner_id", "tier", "guest_agent"]
    ):
        partner = core.m2o_id(row.get("partner_id"))
        if row.get("tier") in ("guest", "agent") or not partner or partner in seen:
            continue
        seen.add(partner)
        try:
            found = community.member_token(svc, partner)
            if found is None:
                continue
            pubkey, token = found
            before = current(svc, token)
            p = (svc.odoo.read("res.partner", [partner], ["name"]) or [{}])[0]
            after = give_default(svc, token, p.get("name") or "", before)
            if after and after != before:
                publish_nip05(
                    svc,
                    pubkey,
                    lambda u, t=token, k=pubkey: svc.keycast.sign_event(t, {"pubkey": k, **u}),
                    after,
                )
                done[before or f"partner-{partner}"] = after
        except Exception as exc:  # noqa: BLE001 — one member's failure must not stop the others
            log.warning("usernames: partner %s skipped: %s", partner, exc)
    return done


def change(svc: Services, pubkey: str, wanted: str) -> dict[str, str]:
    from . import community, core, mail
    from .holder_invites import ApiError
    from .places import member_key

    wanted = (wanted or "").strip().lower().removeprefix("@")
    if not VALID.fullmatch(wanted):
        raise ApiError(
            422,
            "invalid",
            "A username is 1 to 30 lowercase letters, digits, dots, dashes or underscores, "
            "starting and ending with a letter or a digit.",
        )
    row = member_key(svc, pubkey)
    partner = core.m2o_id(row.get("partner_id")) if row else None
    if not partner:
        raise ApiError(403, "not_member", "Only members have an address here.")
    found = community.member_token(svc, partner)
    if found is None or found[0] != pubkey:
        raise ApiError(
            409, "own_key", "You hold your own key: set your address in your own profile."
        )
    token = found[1]
    domain = svc.settings.nip05_domain
    if current(svc, token) == wanted:
        return {"username": wanted, "nip05": f"{wanted}@{domain}"}
    if not _free(svc, wanted):
        raise ApiError(409, "taken", "That name is taken.")
    try:
        svc.keycast.set_username(token, wanted)
    except ValueError as exc:
        raise ApiError(409, "taken", "That name is taken.") from exc
    publish_nip05(
        svc, pubkey, lambda u: svc.keycast.sign_event(token, {"pubkey": pubkey, **u}), wanted
    )
    with mail._lock:
        mail._cache.clear()  # the old and the new address resolve afresh
    log.info("usernames: partner %s is now %s", partner, wanted)
    return {"username": wanted, "nip05": f"{wanted}@{domain}"}


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.post(PATH)
    async def set_username(request: Request) -> JSONResponse:
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
            raise ApiError(400, "invalid_json", 'Send {"username": "<name>"}.')
        out = await run_in_threadpool(change, s, pubkey, str(data.get("username") or ""))
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
