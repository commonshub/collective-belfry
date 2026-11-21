"""Villagers: a profile page per person, for the community's website (docs/website-accounts.md).

Every account (never an agent) has a **slug**, the name its page has on the website
(``https://<domain>/villagers/<slug>``, served by the site's own ``/villagers/_.html``):

- a member's or an admin's is their username in the signer (changed in the chat, never here);
  a member without one (they hold their own key) has no page;
- a guest's is kept on their contact (``belfry_slug``), made the first time it is asked for
  from their name (``ada-lovelace``, then ``-2``, ``-3``…; ``guest-<n>`` for someone without a
  name) and changed with ``POST /api/site/profile {"slug"}``.

Slugs and usernames are one set of names: a guest's slug is never a username, another guest's
slug, a reserved name or a channel's name, and nobody gets a username that is another
contact's slug (usernames.py asks ``guest_holder``).

Who sees a profile is the account's own choice (``belfry_profile_visibility`` on the contact),
one of three levels. Villagers are members, admins, and guests who hold a ticket of an event
(``is_villager``, ``sitesessions.has_ticket``); the owner always sees everything:

- ``listed`` (``DEFAULT_VISIBILITY``, also what nothing stored means): name, picture, headline,
  slug and role for anyone; about, links and tags for villagers;
- ``private``: everything for villagers only (``villagers``, the word before 2026-11-21, is
  read and accepted as ``private``);
- ``public``: everything for anyone.

Contacts that existed when the three levels came were made ``private`` once (what
``villagers`` promised them; register.Register._profile_visibility_once).

- ``GET /api/villagers/<slug>`` (NIP-98 optional; a site session reads as its account).
- ``GET /api/villagers``: the public and listed profiles (a listed one with its public part).

Needs the belfry's own register (409 ``unsupported`` otherwise).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import retries
from .core import Services, m2o_id
from .events import Limiter
from .holder_invites import ApiError, authenticate

log = logging.getLogger(__name__)

API = "/api/villagers"
DEFAULT_VISIBILITY = "listed"
VISIBILITIES = ("listed", "private", "public")
SYNONYMS = {"villagers": "private"}  # the word older site code sends
LIST_SECONDS = 60  # how long the list of public profiles is kept
LIST_MAX = 500
NAME_SECONDS = 5  # how long "this username is that key" (or nobody's) is believed
PUBLIC_SECONDS = 60  # how long an unsigned answer about a public or listed profile may be cached
NOBODY = "Somebody"
PLACEHOLDER = re.compile(r"^guest-\d+(?:-\d+)?$")  # a slug made for someone without a name
READS = Limiter(120, 60)  # reads a minute per address of the network
NO_STORE = {"Cache-Control": "no-store"}
VARY = {"Vary": "Authorization"}  # a kept unsigned answer is never one for someone signed
_lock = threading.RLock()
_names: dict[str, tuple[float, str]] = {}
_list: dict[str, Any] = {"at": 0.0, "rows": None}


def forget() -> None:
    """A profile, a slug or a visibility changed: the list is read again."""
    _list.update(at=0.0, rows=None)
    from . import eventchat

    eventchat.forget_people()  # names and slugs shown in an event's conversation


def forget_names() -> None:
    _names.clear()


# ── slugs ───────────────────────────────────────────────────────────────────────────────────
def _contact(svc: Services, partner: int) -> dict[str, Any]:
    return (
        svc.odoo.read_one(
            "res.partner",
            int(partner),
            ["belfry_slug", "belfry_slug_auto", "belfry_profile_visibility"],
        )
        or {}
    )


def visibility_of(row: dict[str, Any]) -> str:
    v = row.get("belfry_profile_visibility")
    v = SYNONYMS.get(v, v) if isinstance(v, str) else v
    return v if v in VISIBILITIES else DEFAULT_VISIBILITY


def guest_holder(svc: Services, name: str) -> str:
    """Whose slug NAME is among the guests: the key of that contact's account (so the contact
    itself may still take the name as a username when it becomes a member), a word that is
    nobody's key when the contact has no key any more, "" when no guest has it. The slug left
    on the contact of someone who is a member now counts for nothing (their username is their
    slug) and is taken off."""
    from . import sitesessions

    if not name or not sitesessions.own(svc):
        return ""
    for r in svc.odoo.search_read("res.partner", [["belfry_slug", "=", name]], ["id"]):
        pid = int(r["id"])
        acct = sitesessions.account(svc, pid)
        if acct is not None and acct["role"] != "guest":
            svc.odoo.write("res.partner", [pid], {"belfry_slug": False, "belfry_slug_auto": False})
            continue
        if acct is not None:
            return str(acct["pubkey"])
        # no active pass: the name stays theirs (they find their page again when they return)
        keys = [
            str(p["keycast_pubkey"])
            for p in svc.odoo.search_read(
                "belfry.pass", [["partner_id", "=", pid]], ["keycast_pubkey"]
            )
            if p.get("keycast_pubkey")
        ]
        return keys[-1] if keys else f"contact:{pid}"
    return ""


def _free(svc: Services, name: str, mine: str) -> bool:
    """Whether a guest whose account key is MINE may have NAME as their slug."""
    from . import usernames

    return usernames.problem(name) is None and usernames.holder(svc, name) in ("", mine)


def _default(svc: Services, acct: dict[str, Any]) -> str:
    """A slug for a guest who has none: their name (then -2, -3…), ``guest-<n>`` without one."""
    from . import usernames

    base = usernames.slug(acct["name"])
    if not base or base == "guest" or usernames.DEFAULT.fullmatch(base):
        base = f"guest-{acct['partner']}"
    for n in range(1, 200):
        tail = "" if n == 1 else f"-{n}"
        name = base[: usernames.MAX - len(tail)].strip("-") + tail
        if _free(svc, name, acct["pubkey"]):
            return name
    return ""


def guest_slug(svc: Services, acct: dict[str, Any], fresh: bool = False) -> str | None:
    """A guest's slug, made and kept on the contact the first time (FRESH: made again)."""
    from . import usernames

    have = usernames.clean(_contact(svc, acct["partner"]).get("belfry_slug"))
    if have and not fresh:
        return have
    with _lock:
        have = usernames.clean(_contact(svc, acct["partner"]).get("belfry_slug"))
        if have and not fresh:
            return have
        made = _default(svc, acct)
        if not made or made == have:
            return have or None
        svc.odoo.write(
            "res.partner", [acct["partner"]], {"belfry_slug": made, "belfry_slug_auto": True}
        )
        usernames._taken.pop(made, None)
        return made


def slug_of(svc: Services, acct: dict[str, Any], username: str | None = None) -> str | None:
    """The account's slug: a guest's own, a member's username (USERNAME when the caller read
    it already; None for a member the signer has no name for)."""
    from . import guests

    if acct["role"] == "guest":
        return guest_slug(svc, acct)
    if username is None:
        username = (guests._account_nip05(svc, acct["partner"]) or "").split("@")[0]
    return username.lower() or None


def plan(svc: Services, acct: dict[str, Any], slug: Any, visibility: Any) -> dict[str, Any]:
    """What ``POST /site/profile {"slug"?, "visibility"?}`` would write on the contact (nothing
    when nothing changes), after refusing: 409 ``use_chat`` (a member's slug is their
    username), 422 ``slug_invalid``, 409 ``slug_taken`` (with ``suggestions``)."""
    from . import usernames

    vals: dict[str, Any] = {}
    row = _contact(svc, acct["partner"])
    if visibility is not None and visibility != visibility_of(row):
        vals["belfry_profile_visibility"] = visibility
    if slug is None:
        return vals
    if acct["role"] != "guest":
        raise ApiError(409, "use_chat", "Your username is your page's name: change it in the chat.")
    wanted = usernames.clean(slug)
    have = usernames.clean(row.get("belfry_slug"))
    if wanted == have and wanted:
        return vals
    reason = usernames.problem(wanted)
    if not reason and usernames.DEFAULT.fullmatch(wanted):
        reason = "reserved"  # what accounts are called before they have a name
    if reason:
        exc = ApiError(422, "slug_invalid", usernames.INVALID.replace("A username", "A slug"))
        exc.extra["reason"] = reason
        raise exc
    if not _free(svc, wanted, acct["pubkey"]):
        exc = ApiError(409, "slug_taken", "That name is taken.")
        exc.extra["suggestions"] = suggestions(svc, wanted, acct["pubkey"])
        raise exc
    return {**vals, "belfry_slug": wanted, "belfry_slug_auto": False}


def suggestions(svc: Services, wanted: str, mine: str, limit: int = 3) -> list[str]:
    """Up to LIMIT free slugs close to WANTED: wanted-2, wanted-3…"""
    from . import usernames

    out: list[str] = []
    for n in range(2, 12):
        tail = f"-{n}"
        name = wanted[: usernames.MAX - len(tail)].strip("-._") + tail
        if name not in out and not usernames.DEFAULT.fullmatch(name) and _free(svc, name, mine):
            out.append(name)
            if len(out) >= limit:
                break
    return out


def apply(svc: Services, acct: dict[str, Any], vals: dict[str, Any]) -> bool:
    """Write what ``plan`` found; the slug is checked once more, with the write (two people
    asking for one name at the same moment)."""
    from . import usernames

    if not vals:
        return False
    with _lock:
        wanted = vals.get("belfry_slug")
        if wanted and not _free(svc, wanted, acct["pubkey"]):
            exc = ApiError(409, "slug_taken", "That name is taken.")
            exc.extra["suggestions"] = suggestions(svc, wanted, acct["pubkey"])
            raise exc
        svc.odoo.write("res.partner", [acct["partner"]], vals)
        if wanted:
            usernames._taken.pop(wanted, None)
    forget()
    log.info("villagers: partner %s changed %s", acct["partner"], ", ".join(sorted(vals)))
    return True


def renamed(svc: Services, acct: dict[str, Any]) -> None:
    """A guest gave a name: a slug that was made for someone without one (``guest-<n>``) and
    never chosen becomes their name. Best effort."""
    try:
        row = _contact(svc, acct["partner"])
        have = str(row.get("belfry_slug") or "")
        if acct["role"] == "guest" and row.get("belfry_slug_auto") and PLACEHOLDER.match(have):
            guest_slug(svc, acct, fresh=True)
    except Exception as exc:  # noqa: BLE001 — the slug they have still works
        log.info("villagers: slug not made from the new name: %s", type(exc).__name__)


# ── finding someone ─────────────────────────────────────────────────────────────────────────
def _username_key(svc: Services, name: str) -> str:
    """The key the signer has NAME for ("" for nobody), believed for NAME_SECONDS."""
    now = time.monotonic()
    hit = _names.get(name)
    if hit and now - hit[0] < NAME_SECONDS:
        return hit[1]
    try:
        key = str(svc.keycast.pubkey_for_name(name) or "").lower()
    except Exception as exc:  # noqa: BLE001 — nobody, for now (not remembered)
        log.info("villagers: signer not asked about a name: %s", type(exc).__name__)
        return ""
    if len(_names) > 5000:
        _names.clear()
    _names[name] = (now, key)
    return key


def _account_of_key(svc: Services, key: str) -> dict[str, Any] | None:
    """The account whose own key is KEY (an active pass), never an agent's."""
    from . import sitesessions

    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", key]],
        ["partner_id", "tier"],
    )
    for r in rows:
        pid = m2o_id(r.get("partner_id"))
        if pid and r.get("tier") != "agent":
            acct = sitesessions.account(svc, int(pid))
            if acct is not None and acct["pubkey"] == key:
                return acct
    return None


def find(svc: Services, slug: str) -> dict[str, Any] | None:
    """The account (with an active pass) whose slug is SLUG: a member by their username in the
    signer, a guest by their contact."""
    from . import sitesessions, usernames

    slug = (slug or "").lower()
    if not usernames.VALID.fullmatch(slug) or len(slug) > usernames.MAX:
        return None
    key = _username_key(svc, slug)
    if key:
        acct = _account_of_key(svc, key)
        if acct is not None and acct["role"] != "guest":
            return acct
    for r in svc.odoo.search_read("res.partner", [["belfry_slug", "=", slug]], ["id"]):
        acct = sitesessions.account(svc, int(r["id"]))
        if acct is not None and acct["role"] == "guest":
            return acct
    return None


def caller(svc: Services, key: str | None) -> dict[str, Any] | None:
    """The account a signing key stands for: a site session's account, or the account whose
    own key it is; None for anyone else (and for an agent)."""
    from . import sitesessions

    if not key:
        return None
    acted, acct = sitesessions.acting(svc, key)
    return acct if acct is not None else _account_of_key(svc, str(acted))


def is_villager(svc: Services, acct: dict[str, Any] | None) -> bool:
    """A member, an admin, or a guest who holds a ticket of an event."""
    from . import sitesessions

    if acct is None:
        return False
    return acct["role"] != "guest" or sitesessions.has_ticket(svc, acct["pubkey"])


# ── what a profile says ─────────────────────────────────────────────────────────────────────
def _text(profile: dict[str, Any], k: str, limit: int) -> str | None:
    v = profile.get(k)
    return v[:limit] if isinstance(v, str) and v.strip() else None


def _shown(profile: dict[str, Any], acct: dict[str, Any]) -> dict[str, Any]:
    from . import sitesessions as ss

    return {
        "name": _text(profile, "display_name", ss.NAME_MAX)
        or _text(profile, "name", ss.NAME_MAX)
        or acct["name"]
        or NOBODY,
        "picture": _text(profile, "picture", ss.PICTURE_MAX),
        "about": _text(profile, "about", ss.ABOUT_MAX),
    }


PARTIAL = ("slug", "name", "picture", "headline", "role")  # what anyone sees of a listed one


def profile(
    svc: Services,
    acct: dict[str, Any],
    slug: str,
    viewer: dict[str, Any] | None,
    partial: bool = False,
) -> dict[str, Any]:
    """The page of an account: never its address, its key or its nip05. PARTIAL: only what a
    ``listed`` profile shows to anyone (``PARTIAL``)."""
    from . import sitesessions

    content, _at = sitesessions._profile(svc, acct)
    try:
        extras = sitesessions.extras_of(svc, acct)
    except Exception as exc:  # noqa: BLE001 — the rest still answers
        retries.soft(exc)  # but a relay that can't be read: 503 relay
        log.info("villagers: tags not read: %s", type(exc).__name__)
        extras = {"tags": [], "headline": None, "links": []}
    shown = _shown(content, acct)
    if partial:
        return {
            "slug": slug,
            "name": shown["name"],
            "picture": shown["picture"],
            "headline": extras["headline"],
            "role": acct["role"],
            "visibility": "listed",
            "partial": True,
        }
    return {
        "slug": slug,
        **shown,
        "headline": extras["headline"],
        "tags": extras["tags"],
        "links": extras["links"],
        "role": acct["role"],
        "visibility": visibility_of(_contact(svc, acct["partner"])),
        "self": viewer is not None and viewer["partner"] == acct["partner"],
        "partial": False,
    }


def answer(svc: Services, slug: str, key: str | None) -> dict[str, Any]:
    """``GET /villagers/<slug>`` for whoever signed (KEY, or nobody). A villager and the owner
    see everything; anyone else sees a public profile, the public part of a listed one, and
    is told the same thing about a private profile and about no profile: 403
    ``villagers_only``."""
    viewer = caller(svc, key)
    villager = is_villager(svc, viewer)
    acct = find(svc, slug)
    if acct is None:
        if villager:
            raise ApiError(404, "unknown", "Nobody here has that name.")
        raise ApiError(403, "villagers_only", "Only villagers see this profile.")
    level = visibility_of(_contact(svc, acct["partner"]))
    mine = viewer is not None and viewer["partner"] == acct["partner"]
    full = villager or mine or level == "public"
    if not full and level != "listed":
        raise ApiError(403, "villagers_only", "Only villagers see this profile.")
    return profile(svc, acct, slug.lower(), viewer, partial=not full)


# ── the public list ─────────────────────────────────────────────────────────────────────────
def _latest(found: list[dict[str, Any]], kind: int) -> dict[str, dict[str, Any]]:
    """The newest event of KIND per author, its content as a dict."""
    newest: dict[str, dict[str, Any]] = {}
    for e in found:
        if e.get("kind") != kind or not isinstance(e.get("pubkey"), str):
            continue
        old = newest.get(e["pubkey"])
        if old is None or (e.get("created_at", 0), e.get("id", "")) > (
            old.get("created_at", 0),
            old.get("id", ""),
        ):
            newest[e["pubkey"]] = e
    out: dict[str, dict[str, Any]] = {}
    for k, e in newest.items():
        try:
            content = json.loads(e.get("content") or "{}")
        except ValueError:
            content = {}
        out[k] = content if isinstance(content, dict) else {}
    return out


def _listed_accounts(svc: Services) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(account, contact) of everyone who has a page (``find`` answers for them) and shows up
    in the list: a public or listed profile (nothing stored is listed), an active pass that is
    not an agent's; a guest with a slug (not ``guest-<n>``, made for someone without a name),
    or who chose public or listed on the website (their slug is made then, as before); a member
    whatever theirs (their username, read later)."""
    from . import sitesessions

    partners: list[int] = []
    for r in svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"]], ["partner_id", "tier", "keycast_pubkey"]
    ):
        pid = m2o_id(r.get("partner_id"))
        if pid and r.get("tier") != "agent" and r.get("keycast_pubkey") and pid not in partners:
            partners.append(int(pid))
    if not partners:
        return []
    fields = ["belfry_slug", "belfry_slug_auto", "belfry_profile_visibility"]
    fields += sitesessions.GUEST_FIELDS
    out = []
    for row in svc.odoo.read("res.partner", sorted(partners), fields):
        if visibility_of(row) == "private":
            continue
        acct = sitesessions.account(svc, int(row["id"]))
        if acct is None:
            continue
        if acct["role"] == "guest":
            slug = str(row.get("belfry_slug") or "")
            chosen = row.get("belfry_profile_visibility") in VISIBILITIES  # set on the website
            if not slug and not chosen:
                continue  # no page yet, and never asked for one: not made by a list
            if slug and row.get("belfry_slug_auto") and PLACEHOLDER.match(slug):
                continue
        out.append((acct, row))
        if len(out) >= LIST_MAX:
            break
    return out


def _build(svc: Services) -> list[dict[str, Any]]:
    from . import community, events, sitesessions

    accts = _listed_accounts(svc)
    if not accts:
        return []
    keys = [a["pubkey"] for a, _ in accts]
    found: list[dict[str, Any]] = []
    for read in (events.community, events.groups):
        try:
            found += read(svc, [{"kinds": [0], "authors": keys}])
        except Exception as exc:  # noqa: BLE001 — the names on the accounts then
            retries.soft(exc)  # the API: 503 relay, and no list without its names is kept
            log.info("villagers: profiles not read: %s", type(exc).__name__)
    profiles = _latest(found, 0)
    members = [a["pubkey"] for a, _ in accts if a["role"] != "guest"]
    extras: dict[str, dict[str, Any]] = {}
    if members:
        try:
            got = community.query(
                svc, [{"kinds": [30078], "authors": members, "#d": [sitesessions.PROFILE_D]}]
            )
            extras = _latest(
                [
                    e
                    for e in got
                    if any(t[:2] == ["d", sitesessions.PROFILE_D] for t in e.get("tags", []))
                ],
                30078,
            )
        except Exception as exc:  # noqa: BLE001
            retries.soft(exc)
            log.info("villagers: tags not read: %s", type(exc).__name__)
    rows = []
    for acct, r in accts:
        try:
            slug = slug_of(svc, acct)
        except Exception as exc:  # noqa: BLE001 — one account's failure leaves the others
            log.info("villagers: a slug was not read: %s", type(exc).__name__)
            slug = None
        if not slug or (acct["role"] == "guest" and PLACEHOLDER.match(slug)):
            continue  # a guest-<n> is someone without a name: never listed
        shown = _shown(profiles.get(acct["pubkey"], {}), acct)
        more = (
            sitesessions.contact_extras(r)
            if acct["role"] == "guest"
            else sitesessions.content_extras(extras.get(acct["pubkey"], {}))
        )
        row: dict[str, Any] = {
            "slug": slug,
            "name": shown["name"],
            "picture": shown["picture"],
            "headline": more["headline"],
            "role": acct["role"],
        }
        if visibility_of(r) == "public":
            row.update(tags=more["tags"], links=more["links"], partial=False)
        else:
            row["partial"] = True
        rows.append(row)
    rows.sort(key=lambda v: (v["name"].casefold(), v["slug"]))
    return rows


def listing(svc: Services) -> list[dict[str, Any]]:
    """The public and listed profiles, by name; read again every LIST_SECONDS and after a
    change."""
    now = time.monotonic()
    rows = _list["rows"]
    if rows is not None and now - _list["at"] < LIST_SECONDS:
        return rows
    rows = _build(svc)
    _list.update(at=now, rows=rows)
    return rows


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from . import sitesessions
    from .sales import client_ip

    services = getattr(app.state, "services", None)
    if services is not None and not sitesessions.own(services):
        log.info("villagers: profile visibility not migrated (Odoo is the register: no pages)")

    def ready(request: Request) -> Services:
        s: Services = request.app.state.services
        if not sitesessions.own(s):
            raise ApiError(409, "unsupported", "Profiles need the belfry's own register.")
        if not READS.allow("ip:" + client_ip(request)):
            raise ApiError(429, "too_many", "Too many requests for now. Try again in a minute.")
        return s

    @app.get(API)
    async def villagers_list(request: Request) -> JSONResponse:
        s = ready(request)
        # a relay that can't be read: 503 relay, never a list without its names (retries.py)
        rows = await retries.run(listing, s)
        signed = bool(request.headers.get("authorization"))
        cache = "no-store" if signed else f"public, max-age={LIST_SECONDS}"
        return JSONResponse({"villagers": rows}, headers={"Cache-Control": cache, **VARY})

    @app.get(API + "/{slug}")
    async def villager(request: Request, slug: str) -> JSONResponse:
        s = ready(request)
        auth = request.headers.get("authorization")
        key = None
        if auth:  # optional; one that is sent must be right
            key = authenticate(s, request.app.state.api_seen, auth, "GET", f"{API}/{slug}", b"")
        out = await retries.run(answer, s, slug, key)
        # an unsigned answer (public, or a listed one's public part) may be kept a moment; one
        # for someone signed never, and Vary keeps a kept one from a signed request
        if key is None and out["visibility"] in ("public", "listed"):
            cache = {"Cache-Control": f"public, max-age={PUBLIC_SECONDS}"}
            return JSONResponse(out, headers={**cache, **VARY})
        return JSONResponse(out, headers={**NO_STORE, **VARY})


__all__ = ["register", "DEFAULT_VISIBILITY", "find", "slug_of", "guest_holder", "forget"]
