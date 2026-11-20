"""A member's username: ``<username>@<domain>`` is their address (NIP-05, and their mailbox).

The signer keeps it (Keycast's ``username``). A new member starts with their name as a slug
("Ana María Pérez" → ``ana-maria-perez``; taken → ``ana-maria-perez-2``), not with their member
number, and changes it from their profile in the chat: ``POST /api/username`` (belfry.<domain>/
api/v1/username), NIP-98 by the member's own key, ``{"username": "<new>"}`` → ``{"username",
"nip05"}``. Errors: ``invalid`` (422), ``taken`` (409, also the reserved names and the names of
channels, which have a mailbox too), ``not_member`` (403: guests and agents have no address),
``own_key`` (409: a member who holds their own key sets NIP-05 in their own profile).

The profile (kind 0) on the community relay gets the new ``nip05`` and keeps everything else.

Choosing one while joining (2026-11-19; docs/membership.md "A name and a username"):
``POST /api/username/check`` (belfry.<domain>/api/v1/username/check), NIP-98 by any key (an
applicant's browser key, a guest's, a member's): ``{"username"}`` → ``{"username", "state":
"available" | "taken" | "invalid", "reason"?, "suggestions"}``; ``{"name"}`` alone →
``{"suggested", "suggestions"}``. Suggestions: the first name, first-last, then first-2, -3…
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
from .events import Limiter

log = logging.getLogger(__name__)

PATH = "/api/username"
CHECK = "/api/username/check"
CHECKS = Limiter(60, 60)  # availability checks a minute, per key and per address of the network
TAKEN_SECONDS = 5  # how long "taken" is remembered (a form that checks while someone types)
INVALID = (
    "A username is 1 to 30 lowercase letters, digits, dots, dashes or underscores, "
    "starting and ending with a letter or a digit."
)
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
    "bounces",
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


def clean(wanted: Any) -> str:
    return str(wanted or "").strip().lower().removeprefix("@")


def problem(name: str) -> str | None:
    """Why NAME can't be a username (VALID, MAX, RESERVED): ``short`` (nothing), ``long``,
    ``chars``, ``reserved``; None: it can."""
    if not name:
        return "short"
    if len(name) > MAX:
        return "long"
    if not VALID.fullmatch(name):
        return "chars"
    if name in RESERVED:
        return "reserved"
    return None


_taken: dict[str, tuple[float, str]] = {}


def holder(svc: Services, name: str) -> str:
    """Who has NAME: an account's key (its username in the signer), ``#`` (a channel: it has a
    mailbox too), or nobody (""). One signer lookup and the check of ``_free``; "taken" is
    remembered for a few seconds. Best effort: the signer still refuses a taken name."""
    held = _taken.get(name)
    if held and time.monotonic() - held[0] < TAKEN_SECONDS:
        return held[1]
    who = ""
    try:
        who = str(svc.keycast.pubkey_for_name(name) or "").lower()
    except Exception as exc:  # noqa: BLE001
        log.info("usernames: signer not asked about a name: %s", exc)
    if not who:
        who = _guest_slug(svc, name)  # a guest's page on the website has that name
    if not who and not _free_name(svc, name):
        who = "#"
    if who:
        if len(_taken) > 2000:
            _taken.clear()
        _taken[name] = (time.monotonic(), who)
    return who


def candidates(name: str) -> list[str]:
    """Usernames to offer for NAME, best first: the first name, first-last, first-2, -3…"""
    words = [w for w in slug(name).split("-") if w]
    if not words:
        return []
    first = words[0]
    out = [first]
    if len(words) > 1:
        out.append(f"{first}-{words[-1]}"[:MAX].strip("-"))
    for n in range(2, 10):
        tail = f"-{n}"
        out.append(first[: MAX - len(tail)] + tail)
    return out


def suggestions(svc: Services, name: str, mine: str = "", limit: int = 3) -> list[str]:
    """Up to LIMIT of ``candidates`` that are valid and free (or the caller's own, MINE)."""
    out: list[str] = []
    for c in candidates(name):
        if c in out or problem(c) or DEFAULT.fullmatch(c):
            continue
        if holder(svc, c) not in ("", mine):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def require(svc: Services, raw: Any, name: str = "", mine: str = "") -> str:
    """RAW as a username someone may ask for: 422 ``username_invalid`` (with ``reason``), 409
    ``username_taken`` (with ``suggestions``, from NAME when given)."""
    from .holder_invites import ApiError

    wanted = clean(raw)
    reason = problem(wanted)
    if reason:
        exc = ApiError(422, "username_invalid", INVALID)
        exc.extra["reason"] = reason
        raise exc
    if holder(svc, wanted) not in ("", mine):
        exc = ApiError(409, "username_taken", "That name is taken.")
        exc.extra["suggestions"] = suggestions(svc, name or wanted, mine)
        raise exc
    return wanted


def check(svc: Services, key: str, body: dict[str, Any]) -> dict[str, Any]:
    """The answer of ``POST /api/username/check`` for the key that signed."""
    from .holder_invites import ApiError

    name = " ".join(str(body.get("name") or "").split())[:200]
    raw = body.get("username")
    if raw is None or (not str(raw).strip() and name):
        if not name:
            raise ApiError(400, "missing", 'Send {"username": "<name>"} or {"name": "<name>"}.')
        found = suggestions(svc, name, key)
        return {"suggested": found[0] if found else "", "suggestions": found}
    wanted = clean(raw)[:64]
    reason = problem(wanted)
    if reason:
        return {
            "username": wanted,
            "state": "invalid",
            "reason": reason,
            "suggestions": suggestions(svc, name or wanted, key),
        }
    if holder(svc, wanted) in ("", key):
        return {"username": wanted, "state": "available", "suggestions": []}
    return {
        "username": wanted,
        "state": "taken",
        "suggestions": suggestions(svc, name or wanted, key),
    }


def _guest_slug(svc: Services, name: str) -> str:
    """The key of the guest whose page on the website is called NAME (villagers.py: slugs and
    usernames are one set of names), "" for nobody. Best effort."""
    try:
        from . import villagers

        return villagers.guest_holder(svc, name)
    except Exception as exc:  # noqa: BLE001
        log.info("usernames: guests' slugs not checked: %s", type(exc).__name__)
        return ""


def _free(svc: Services, name: str, mine: str = "") -> bool:
    """``_free_name``, and not the slug of another contact's page (MINE: the key of the
    account asking; a guest who becomes a member may take their own)."""
    return _free_name(svc, name) and _guest_slug(svc, name) in ("", mine)


def _free_name(svc: Services, name: str) -> bool:
    """Not reserved and not the name of a channel (channels receive mail at <channel>@)."""
    if name in RESERVED or not VALID.fullmatch(name):
        return False
    try:
        from . import mail

        return not mail.channel_id(svc, name)  # a channel's id or its plain slug (bugs@)
    except Exception as exc:  # noqa: BLE001 — the relay not answering: the signer still checks
        log.info("usernames: channels not checked for %s: %s", name, exc)
        return True


def current(svc: Services, token: str) -> str:
    try:
        return str((svc.keycast.get_profile(token) or {}).get("username") or "").lower()
    except Exception as exc:  # noqa: BLE001
        log.info("usernames: profile not read: %s", exc)
        return ""


def give_default(
    svc: Services, token: str, name: str, fallback: str, wanted: str = "", mine: str = ""
) -> str:
    """The account's username after making sure it is not a bare number: a member who never chose
    one gets their name as a slug (with -2, -3… when taken); with WANTED (the username they
    asked for when they applied) that one when it is free, else the first free of
    ``candidates``. Returns the username the account has now; FALLBACK when nothing could be
    read or set (best effort: the account works). MINE: the account's key (the slug of its own
    page as a guest does not stand in the way)."""
    fallback = fallback.lower()
    if not token:
        return fallback
    have = current(svc, token)
    if have and not DEFAULT.fullmatch(have):
        return have  # chosen already (by them, or an earlier default)
    tries: list[str] = []
    wanted = clean(wanted)
    if wanted:
        tries = [wanted, *candidates(name)]
    base = slug(name)
    if base and not DEFAULT.fullmatch(base):
        for n in range(1, 30):
            tail = "" if n == 1 else f"-{n}"
            tries.append(base[: MAX - len(tail)].strip("-") + tail)
    for name_ in dict.fromkeys(tries):
        if DEFAULT.fullmatch(name_) or not _free(svc, name_, mine):
            continue
        try:
            svc.keycast.set_username(token, name_)
            return name_
        except ValueError:  # taken
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("usernames: %s not set: %s", name_, exc)
            break
    return have or fallback


def publish_nip05(
    svc: Services, pubkey: str, sign: Any, username: str, more: dict[str, str] | None = None
) -> bool:
    """The member's profile with the new address (and MORE: ``name``, ``display_name``);
    everything else in it stays."""
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
        new = {**(more or {}), "nip05": f"{username}@{svc.settings.nip05_domain}"}
        if found and all(content.get(k) == v for k, v in new.items()):
            return False
        content.update(new)
        # never the second of the profile it replaces (a tie is settled by the events' ids)
        at = max(int(time.time()), int(found[0].get("created_at") or 0) + 1 if found else 0)
        event = sign({"kind": 0, "created_at": at, "tags": [], "content": json.dumps(content)})
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

    wanted = clean(wanted)
    if not VALID.fullmatch(wanted):
        raise ApiError(422, "invalid", INVALID)
    row = member_key(svc, pubkey)
    partner = core.m2o_id(row.get("partner_id")) if row else None
    if not partner:
        raise ApiError(403, "not_member", "Only members have an address here.")
    from . import sales

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
    domain = svc.settings.nip05_domain
    if current(svc, token) == wanted:
        return {"username": wanted, "nip05": f"{wanted}@{domain}"}
    if not _free(svc, wanted, pubkey):
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
    _taken.pop(wanted, None)
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

    @app.post(CHECK)
    async def check_username(request: Request) -> JSONResponse:
        from .sales import client_ip

        s = request.app.state.services
        body = await request.body()
        key = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", CHECK, body
        )
        if not CHECKS.allow("key:" + key) or not CHECKS.allow("ip:" + client_ip(request)):
            raise ApiError(429, "too_many", "Too many requests for now. Try again in a minute.")
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", 'Send {"username": "<name>"}.')
        out = await run_in_threadpool(check, s, key, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
