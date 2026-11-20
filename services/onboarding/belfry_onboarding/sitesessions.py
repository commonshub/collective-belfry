"""Website accounts: people use the community's own website signed in (docs/website-accounts.md).

The website (the nsite on the apex, and its previews) never holds a signer session. It makes a
key in the browser, proves an e-mail address with a 6-digit code (``/api/login/start`` and
``/api/login/verify`` with ``"site": true``, applyverified.py) and that key becomes a **site
session** of the address's account: a row in the register, key → account. Nothing in the signer
knows the key. A request signed with it (NIP-98) is answered for the account by the few
endpoints that ask ``acting`` / ``site_account``; where something has to be signed as the person
(a comment, their profile, a request's thread) the server signs it through the signer, the way
webthreads.py signs an author's edits. Every other endpoint sees an unknown key.

- ``site_account(svc, key)``: the account a live site session key stands for, else None.
- ``acting(svc, key)``: (the key to act as, the account or None): the account's own key for a
  site session, the key itself for anyone else.
- ``POST /api/site/logout``, ``GET /api/site/me``, ``POST /api/site/profile``.

A session ends after ``IDLE_DAYS`` without use, at logout, when its account has no active pass
left, or when the account has more than ``MAX_SESSIONS`` (the least recently used go). Needs the
belfry's own register (REGISTER=belfry): 409 ``unsupported`` otherwise.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id
from .holder_invites import ApiError, authenticate
from .mailer import masked

log = logging.getLogger(__name__)

API = "/api/site"
IDLE_DAYS = 30
MAX_SESSIONS = 10
TOUCH_SECONDS = 60  # "last used" is written at most this often
NAME_MAX = 80
ABOUT_MAX = 1000
PICTURE_MAX = 500
PROFILE_EDITS_PER_HOUR = 10
PROFILE_D = "belfry:profile"  # the chat's own event for tags and place (kind 30078)
TAGS_MAX = 20
TAG = re.compile(r"^[a-z0-9-]{1,30}$")
TICKETS_MAX = 30
NO_STORE = {"Cache-Control": "no-store"}
HEX = re.compile(r"^[0-9a-f]{64}$")
PICTURE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?(/[\x21-\x7e]*)?$")
_lock = threading.RLock()


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc) and hasattr(svc.odoo, "site_get")


def _now() -> int:
    return int(time.time())


# ── the account behind a contact ────────────────────────────────────────────────────────────
def account(svc: Services, partner: int) -> dict[str, Any] | None:
    """The account of a contact with an active pass (never an agent's): its key (a member's
    pass first, else a guest's), name, proven address and role (admin, member or guest)."""
    from . import apps

    rows = [
        r
        for r in svc.odoo.search_read(
            "belfry.pass",
            [["partner_id", "=", int(partner)], ["state", "=", "active"]],
            ["keycast_pubkey", "tier", "guest_channel", "date_end"],
        )
        if r.get("keycast_pubkey") and r.get("tier") != "agent"
    ]
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("tier") == "guest")
    pubkey = str(rows[0]["keycast_pubkey"])
    row = svc.odoo.read_one("res.partner", int(partner), ["name", "email"]) or {}
    member = rows[0].get("tier") != "guest"
    role = "member" if member else "guest"
    try:
        if member and pubkey in apps.admin_pubkeys(svc):
            role = "admin"
    except Exception as exc:  # noqa: BLE001 — a member then; the admin's rights are not here
        log.info("site: admins not read: %s", type(exc).__name__)
    return {
        "partner": int(partner),
        "pubkey": pubkey,
        "name": " ".join(str(row.get("name") or "").split())[:NAME_MAX],
        "email": str(row.get("email") or "").strip().lower(),
        "role": role,
        "guest_of": sorted(
            {
                str(r["guest_channel"])
                for r in rows
                if r.get("tier") == "guest" and r.get("guest_channel")
            }
        ),
        "passes": rows,
    }


def is_account_key(svc: Services, key: str) -> bool:
    """Whether KEY is itself the key of someone here (it then never becomes a site session)."""
    return bool(
        svc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", key], ["state", "=", "active"]], ["id"], limit=1
        )
    )


# ── sessions ────────────────────────────────────────────────────────────────────────────────
def site_account(svc: Services, pubkey: str | None, touch: bool = True) -> dict[str, Any] | None:
    """The account PUBKEY is a live site session of (``account`` plus ``session``: the row),
    else None: not a session, one not used for IDLE_DAYS, or one whose account has no active
    pass any more (its sessions end here). A use renews the guest's pass on the requests
    channel and, at most once a minute, the session's "last used"."""
    if not pubkey or not HEX.match(pubkey) or not own(svc):
        return None
    row = svc.odoo.site_get(pubkey)
    if row is None:
        return None
    now = _now()
    if row["used"] < now - IDLE_DAYS * 86400:
        svc.odoo.site_drop(key=pubkey)
        return None
    acct = account(svc, row["partner_id"])
    if acct is None:
        n = svc.odoo.site_drop(partner_id=row["partner_id"])
        log.info("site: %d session(s) ended: their account has no pass", n)
        return None
    if touch and now - row["used"] >= TOUCH_SECONDS:
        svc.odoo.site_touch(pubkey, now)
        row["used"] = now
        try:
            renew(svc, acct)
        except Exception as exc:  # noqa: BLE001 — the hourly expiry reads the old date
            log.warning("site: guest pass not renewed: %s", type(exc).__name__)
    return {**acct, "session": row}


def acting(svc: Services, pubkey: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """(the key to act as, the site account or None). A site session acts as its account's own
    key; any other key is itself."""
    acct = site_account(svc, pubkey)
    return (acct["pubkey"], acct) if acct else (pubkey, None)


def requests_channel(svc: Services) -> str:
    """The public requests channel (its page on and public), "" when the belfry has none."""
    from . import sales

    try:
        found = sales.requests_site(svc) if sales.own(svc) else None
    except Exception:  # noqa: BLE001
        found = None
    return found[1]["channel"] if found else ""


def renew(svc: Services, acct: dict[str, Any]) -> None:
    """A guest's pass on the requests channel ends GUEST_DAYS from today again (the identity
    does not lapse while its website sessions are used). Members' passes are not touched."""
    from . import webthreads

    channel = requests_channel(svc)
    if not channel:
        return
    end = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=webthreads.GUEST_DAYS)).isoformat()
    for p in acct["passes"]:
        if p.get("tier") == "guest" and p.get("guest_channel") == channel:
            if str(p.get("date_end") or "") < end:
                svc.odoo.write("belfry.pass", [p["id"]], {"date_end": end})


def has_channel(svc: Services, acct: dict[str, Any], channel: str) -> bool:
    """Whether the account has CHANNEL: a guest of it; a member when members read it (an open
    channel, or one they are in); an admin always."""
    from . import guests

    if acct["role"] == "admin":
        return True
    if acct["role"] == "guest":
        return channel in acct["guest_of"]
    closed = guests._closed(svc, channel)
    if closed is False:
        return True
    return guests._in_channel(svc, channel, acct["pubkey"])  # closed, or the relay can't say


def clean_name(raw: Any) -> str | None:
    """A name as given on the website: None when absent; 1–80 characters, one line, never a
    key or an e-mail address; 422 otherwise."""
    from .webrequests import NOT_A_NAME

    if raw is None:
        return None
    name = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not 1 <= len(name) <= NAME_MAX or NOT_A_NAME.search(name) or not name.isprintable():
        raise ApiError(422, "invalid", f"name: 1 to {NAME_MAX} characters.")
    return name


def before_code(svc: Services, key: str, body: dict[str, Any]) -> None:
    """What is refused before the code is tried (a refusal costs no try)."""
    if not own(svc):
        raise ApiError(409, "unsupported", "Website accounts need the belfry's own register.")
    clean_name(body.get("name"))
    if is_account_key(svc, key):
        raise ApiError(409, "key_taken", "This key is an account's own: make a new one.")


def login(
    svc: Services, key: str, email: str, body: dict[str, Any], agent: str = ""
) -> dict[str, Any]:
    """EMAIL was just proven by KEY (the right code) on the website: KEY becomes a site session
    of the address's account; an address nobody has gets the identity a requester from the web
    gets (a guest of the requests channel). Never a ``bunker_url``."""
    from . import applyverified, device, guests, nostr, webthreads

    name = clean_name(body.get("name"))
    channel = requests_channel(svc)
    new = False
    with _lock:
        member = applyverified._account(svc, email)
        if member is None:
            if applyverified.take_over(svc, email, key, verified_only=True):
                return {"state": "pending"}
            if not channel:
                return {"state": "unknown", "proof": applyverified.make_proof(svc, email, key)}
            me = webthreads.web_identity(svc, channel, name or "Guest", email)
            if me is None:
                raise ApiError(503, "signer", "The signer isn't answering: try again in a moment.")
            partner, new = int(me["partner"]), True
        else:
            partner = int(member["partner"])
        acct = account(svc, partner)
        if acct is None:
            raise ApiError(409, "agent", "This address is an agent's: agents sign with their key.")
        if acct["role"] == "guest" and channel and channel not in acct["guest_of"]:
            # a guest of something else (an event, another channel): of the requests channel too
            if webthreads.web_identity(svc, channel, acct["name"] or "Guest", email) is not None:
                acct = account(svc, partner) or acct
        renew(svc, acct)
        label = device.label(agent) or "Browser"
        svc.odoo.site_put(key, partner, label[:60], _now(), MAX_SESSIONS)
        svc.odoo.site_prune(_now() - IDLE_DAYS * 86400)
    log.info("site: %s logged in on the website (key %s…, %s)", masked([email]), key[:8], label)
    out: dict[str, Any] = {
        "state": "signed_in",
        "site": True,
        "pubkey": acct["pubkey"],
        "npub": nostr.hex_to_npub(acct["pubkey"]),
        "nip05": guests._account_nip05(svc, partner) or None,
        "name": acct["name"] or "Guest",
        "role": acct["role"],
    }
    if new:
        out["new"] = True
    return out


def closed(svc: Services, pass_row: dict[str, Any]) -> None:
    """A pass closed (core._close_pass): the contact's site sessions end unless they still
    have an active pass."""
    if not own(svc):
        return
    partner = m2o_id(pass_row.get("partner_id"))
    if partner and account(svc, partner) is None:
        n = svc.odoo.site_drop(partner_id=partner)
        if n:
            log.info("site: %d session(s) ended with pass %s", n, pass_row.get("id"))


def prune(svc: Services) -> int:
    return svc.odoo.site_prune(_now() - IDLE_DAYS * 86400) if own(svc) else 0


# ── signing as the person ───────────────────────────────────────────────────────────────────
def signer(svc: Services, acct: dict[str, Any]) -> Any:
    """What signs as the account (the signer holds its key); 409 ``own_key`` for someone who
    holds their own key (their own signer signs, not the belfry), 503 ``signer``."""
    from . import community

    try:
        found = community.member_signer(svc, acct["partner"])
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "signer", "The signer isn't answering: try again in a moment.") from exc
    if found is None or found[0] != acct["pubkey"]:
        raise ApiError(409, "own_key", "You hold your own key: do this in the chat, with it.")
    return found[1]


# ── GET /site/me ────────────────────────────────────────────────────────────────────────────
def _profile(svc: Services, acct: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """(the account's latest kind 0 as a dict, its time): the community relay's (members) or
    the groups relay's (guests, who are not on the other one)."""
    from . import events

    found: list[dict[str, Any]] = []
    for read in (events.community, events.groups):
        try:
            found += read(svc, [{"kinds": [0], "authors": [acct["pubkey"]], "limit": 1}])
        except Exception as exc:  # noqa: BLE001 — the other relay may have it
            log.info("site: a profile was not read: %s", type(exc).__name__)
    found = [e for e in found if e.get("kind") == 0 and e.get("pubkey") == acct["pubkey"]]
    if not found:
        return {}, 0
    latest = max(found, key=lambda e: (e.get("created_at", 0), e.get("id", "")))
    try:
        content = json.loads(latest.get("content") or "{}")
    except ValueError:
        content = {}
    return (content if isinstance(content, dict) else {}), int(latest.get("created_at") or 0)


def _chat_tag(raw: Any) -> str:
    """A tag as the chat reads one (people.ts ``clean``)."""
    if not isinstance(raw, str):
        return ""
    t = re.sub(r"\s+", "-", raw.strip().lower().removeprefix("#"))
    return re.sub(r"[^\w-]", "", t)[:32]


def _extras(svc: Services, acct: dict[str, Any]) -> dict[str, Any] | None:
    """A member's latest kind 30078 ``d`` = ``belfry:profile`` on the community relay (what the
    chat keeps their tags and place in), None when they have none. Raises when the relay can't
    be read: never written over blind."""
    from . import community

    found = community.query(
        svc, [{"kinds": [30078], "authors": [acct["pubkey"]], "#d": [PROFILE_D]}]
    )
    found = [
        e
        for e in found
        if e.get("kind") == 30078
        and e.get("pubkey") == acct["pubkey"]
        and any(t[:2] == ["d", PROFILE_D] for t in e.get("tags", []))
    ]
    return max(found, key=lambda e: (e.get("created_at", 0), e.get("id", "")), default=None)


def _extras_content(event: dict[str, Any] | None) -> dict[str, Any]:
    try:
        content = json.loads((event or {}).get("content") or "{}")
    except ValueError:
        content = {}
    return content if isinstance(content, dict) else {}


def tags_of(svc: Services, acct: dict[str, Any]) -> list[str]:
    """The account's tags: a member's are the chat's own list (their ``belfry:profile`` event,
    read as the chat reads it); a guest's are kept on the contact (guests are not on the
    members' relay, so the chat doesn't show them)."""
    if acct["role"] == "guest":
        row = svc.odoo.read_one("res.partner", acct["partner"], ["belfry_tags"]) or {}
        raw = row.get("belfry_tags")
    else:
        raw = _extras_content(_extras(svc, acct)).get("tags")
    return clean_tags(raw)


def clean_tags(raw: Any) -> list[str]:
    """A stored list of tags as the chat reads one: each cleaned, none twice, 30 at most."""
    out: list[str] = []
    for t in raw if isinstance(raw, list) else []:
        t = _chat_tag(t)
        if t and t not in out:
            out.append(t)
    return out[:30]


def tickets(svc: Services, acct: dict[str, Any]) -> list[dict[str, Any]]:
    """The events the account is admitted to with a ticket (the groups relay's 9000s that name
    its key), soonest last; cancelled and unreadable events are left out."""
    from . import checkin, events

    key = acct["pubkey"]
    found = events.groups(svc, [{"kinds": [9000], "#p": [key]}])
    ds = sorted(
        {
            str(events.tag(e, "h"))[3:]
            for e in found
            if str(events.tag(e, "h") or "").startswith("ev-") and events.tag(e, "ticket")
        }
    )
    out = []
    for d in ds[:TICKETS_MAX]:
        ev = events.load(svc, d)
        if ev is None:
            continue
        ppl = events.people(svc, ev)
        held = ppl.holders.get(key)
        if not held:
            continue
        tks = events.tickets(svc, ev)
        t = next((t for t in tks if t.d == held), None)
        more = checkin.viewer_fields(svc, ev, key, ppl)
        out.append(
            {
                "event_link": events.link_of(svc, ev),
                "event_title": ev.title,
                "start": events._iso(ev.start),
                "end": events._iso(ev.end),
                "ticket": held,
                "ticket_title": t.title if t else "Ticket",
                "ticket_url": more.get("ticket_url"),
                "checked_in": more.get("checked_in"),
                "test": key in ppl.tests,
                "cancelled": ev.status == "cancelled",
            }
        )
    out.sort(key=lambda r: (r["start"] or "", r["event_link"]))
    return out


def _request_url(svc: Services, snap: dict[str, Any], t: dict[str, Any]) -> str:
    """The request's address as webthreads.thread_url gives it (by its number on the website's
    own page), without reading the channel again."""
    from urllib.parse import quote

    from . import webthreads

    page = snap["site"].get("page")
    if page:
        return f"{page}?id={t['number']}"
    return f"{webthreads._public_base(svc)}/{quote(snap['slug'])}/{t['root']['id']}"


def _requests(svc: Services, acct: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """(the account's own requests, its comments) in the requests channel."""
    from . import webrequests, webthreads

    try:
        snap = webrequests.snapshot(svc)
    except ApiError:
        return [], []
    reqs, comments = [], []
    for t in snap["threads"]:
        mine = webrequests.is_mine(svc, t, acct)
        if not (mine or t["public"]):
            continue
        root = webrequests._root(snap, t, mine)
        title = webthreads._title(root)
        if mine:
            reqs.append(
                {
                    "id": root["id"],
                    "number": t["number"],
                    "title": title,
                    "state": webrequests._state(t),
                    "created_at": root["created_at"],
                    "url": _request_url(svc, snap, t),
                }
            )
        for c in webrequests._comments(snap, t, mine):
            if c["pubkey"] == acct["pubkey"]:
                comments.append(
                    {
                        "request": {"number": t["number"], "title": title},
                        "id": c["id"],
                        "at": c["created_at"],
                        "text": webthreads._body(c),
                    }
                )
    reqs.sort(key=lambda r: r["number"], reverse=True)
    comments.sort(key=lambda c: c["at"], reverse=True)
    return reqs, comments[:100]


def me(svc: Services, acct: dict[str, Any]) -> dict[str, Any]:
    from . import guests, nostr

    profile, _at = _profile(svc, acct)

    def text(k: str, limit: int) -> str | None:
        v = profile.get(k)
        return v[:limit] if isinstance(v, str) and v.strip() else None

    nip05 = guests._account_nip05(svc, acct["partner"]) or None
    try:
        mine = tickets(svc, acct)
    except Exception as exc:  # noqa: BLE001 — the rest still answers
        log.warning("site: tickets not read: %s", type(exc).__name__)
        mine = []
    try:
        reqs, comments = _requests(svc, acct)
    except Exception as exc:  # noqa: BLE001
        log.warning("site: requests not read: %s", type(exc).__name__)
        reqs, comments = [], []
    try:
        tags = tags_of(svc, acct)
    except Exception as exc:  # noqa: BLE001
        log.warning("site: tags not read: %s", type(exc).__name__)
        tags = []
    current = acct["session"]["key"]
    return {
        "name": text("display_name", NAME_MAX) or text("name", NAME_MAX) or acct["name"] or "Guest",
        "username": nip05.split("@")[0] if nip05 else None,
        "nip05": nip05,
        "picture": text("picture", PICTURE_MAX),
        "about": text("about", ABOUT_MAX),
        "tags": tags,
        "email": acct["email"] or None,
        "role": acct["role"],
        "pubkey": acct["pubkey"],
        "npub": nostr.hex_to_npub(acct["pubkey"]),
        "tickets": mine,
        "requests": reqs,
        "comments": comments,
        "sessions": [
            {
                "label": s["label"],
                "created": s["created"],
                "last_used": s["used"],
                "current": s["key"] == current,
            }
            for s in svc.odoo.site_list(acct["partner"])
        ],
    }


# ── POST /site/profile ──────────────────────────────────────────────────────────────────────
def _clean_profile(body: dict[str, Any]) -> dict[str, Any]:
    """{"name"?, "about"?, "picture"?, "tags"?} checked: what to set (a value) or take out
    (None). ``tags``: lowercased, without doubles; each is checked against the ones the account
    has in ``set_tags``."""
    unknown = set(body) - {"name", "about", "picture", "tags"}
    if unknown or not body:
        raise ApiError(422, "invalid", "Send name, about, picture and / or tags.")
    out: dict[str, Any] = {}
    if "tags" in body:
        raw = body["tags"]
        if not isinstance(raw, list) or len(raw) > 60 or not all(isinstance(t, str) for t in raw):
            raise ApiError(422, "invalid", "tags: a list of words.")
        tags = list(dict.fromkeys(t.strip().lower() for t in raw))
        if len(tags) > TAGS_MAX or not all(tags):
            raise ApiError(422, "invalid", f"tags: at most {TAGS_MAX}, none empty.")
        out["tags"] = tags
    if "name" in body:
        name = clean_name(body["name"])
        if name is None:
            raise ApiError(422, "invalid", f"name: 1 to {NAME_MAX} characters.")
        out["name"] = name
    if "about" in body:
        about = body["about"]
        if about is None or about == "":
            out["about"] = None
        elif not isinstance(about, str) or len(about.strip()) > ABOUT_MAX:
            raise ApiError(422, "invalid", f"about: at most {ABOUT_MAX} characters.")
        else:
            out["about"] = about.strip() or None
    if "picture" in body:
        picture = body["picture"]
        if picture is None or picture == "":
            out["picture"] = None
        elif (
            not isinstance(picture, str) or len(picture) > PICTURE_MAX or not PICTURE.match(picture)
        ):
            raise ApiError(422, "invalid", "picture: an https address.")
        else:
            out["picture"] = picture
    return out


def set_tags(svc: Services, acct: dict[str, Any], tags: list[str], sign: Any) -> bool:
    """The account's tags become TAGS; whether anything changed. A member: their
    ``belfry:profile`` event (the list the chat shows and edits) with only ``tags`` changed,
    the rest of its content and its tags kept, signed through the signer, on the community
    relay. A guest: on the contact. A new tag is 1–30 of a–z, 0–9 and -; one they have already
    (written in the chat, whose rule is wider) may stay as it is."""
    from . import events

    guest = acct["role"] == "guest"
    try:
        event = None if guest else _extras(svc, acct)
        have = tags_of(svc, acct) if guest else None
    except Exception as exc:  # noqa: BLE001 — never written over an event that wasn't read
        raise ApiError(503, "unavailable", "That did not work just now. Try again.") from exc
    content = _extras_content(event)
    if have is None:
        raw = content.get("tags")
        have = list(
            dict.fromkeys(filter(None, map(_chat_tag, raw if isinstance(raw, list) else [])))
        )
    if any(not TAG.match(t) and t not in have for t in tags):
        raise ApiError(422, "invalid", "tags: 1 to 30 of a-z, 0-9 and - each.")
    if tags == have[:30]:
        return False
    if mail_limited(acct):
        raise ApiError(429, "too_many", "Too many changes in a short time.")
    if guest:
        svc.odoo.write("res.partner", [acct["partner"]], {"belfry_tags": tags})
        return True
    if svc.relay is None:
        raise ApiError(503, "unavailable", "That did not work just now. Try again.")
    kept = [t for t in (event or {}).get("tags", []) if isinstance(t, list)]
    try:
        new = sign(
            {
                "kind": 30078,
                "created_at": max(_now(), int((event or {}).get("created_at") or 0) + 1),
                "tags": kept or [["d", PROFILE_D]],
                "content": json.dumps({**content, "tags": tags}, ensure_ascii=False),
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "signer", "The signer isn't answering: try again in a moment.") from exc
    if new.get("pubkey") != acct["pubkey"]:
        raise ApiError(503, "signer", "The signer isn't answering: try again in a moment.")
    try:
        svc.relay.publish(new, sign)
    except Exception as exc:  # noqa: BLE001
        log.warning("site: tags not on the community relay: %s", type(exc).__name__)
        raise ApiError(503, "unavailable", "That did not work just now. Try again.") from exc
    events.forget_reads()
    return True


def mail_limited(acct: dict[str, Any]) -> bool:
    """Whether the account used its profile changes of this hour (this one counts)."""
    from . import mail

    return mail._limited(f"site-profile:{acct['partner']}", PROFILE_EDITS_PER_HOUR, 3600)


def set_profile(svc: Services, acct: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """The account's kind 0 with these fields changed and everything else kept (nip05, fields
    this API doesn't know), signed through the signer; on the groups relay (where the website's
    names are read, and the only one a guest is on) and, for a member, the community relay.
    ``tags``: set_tags."""
    from . import events, followers, webrequests

    want = _clean_profile(body)
    sign = signer(svc, acct)
    tags = want.pop("tags", None)
    changed = set_tags(svc, acct, tags, sign) if tags is not None else False
    if tags is not None:
        log.info("site: partner %s set their tags from the website", acct["partner"])
    if not want:
        return {"ok": True, "changed": changed}
    content, at = _profile(svc, acct)
    new = dict(content)
    for k, v in want.items():
        if v is None:
            new.pop(k, None)
        else:
            new[k] = v
    if "name" in want and "display_name" in new:
        new["display_name"] = want["name"]  # the name apps show first, when the profile has one
    if new == content and at:
        if "name" in want and want["name"] != acct["name"]:
            svc.odoo.write("res.partner", [acct["partner"]], {"name": want["name"]})
        return {"ok": True, "changed": changed}
    if mail_limited(acct):
        raise ApiError(429, "too_many", "Too many changes in a short time.")
    try:
        event = sign(
            {
                "kind": 0,
                "created_at": max(_now(), at + 1),
                "tags": [],
                "content": json.dumps(new, ensure_ascii=False),
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "signer", "The signer isn't answering: try again in a moment.") from exc
    done = 0
    got = followers._relay(svc)
    if got is not None:
        try:
            got[0].publish(event, sign)
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("site: profile not on the groups relay: %s", type(exc).__name__)
    if acct["role"] != "guest" and svc.relay is not None:
        try:
            svc.relay.publish(event, sign)
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("site: profile not on the community relay: %s", type(exc).__name__)
    if not done:
        raise ApiError(503, "unavailable", "That did not work just now. Try again.")
    if "name" in want:
        # the name on the contact too: what the belfry calls them (mails, the members' list,
        # the requests API for someone whose profile can't be read)
        svc.odoo.write("res.partner", [acct["partner"]], {"name": want["name"]})
    events.forget_reads()
    webrequests.forget()
    log.info("site: partner %s changed their profile from the website", acct["partner"])
    return {"ok": True, "changed": True}


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    async def session(request: Request, method: str, path: str) -> tuple[Services, dict, dict]:
        """(services, the site account, the JSON body): 401 ``no_session`` for a key that is
        not a live site session."""
        s: Services = request.app.state.services
        raw = await request.body() if method == "POST" else b""
        key = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), method, path, raw
        )
        if not own(s):
            raise ApiError(409, "unsupported", "Website accounts need the belfry's own register.")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        acct = await run_in_threadpool(site_account, s, key)
        if acct is None:
            raise ApiError(401, "no_session", "Log in again.")
        return s, acct, body

    @app.post(API + "/logout")
    async def site_logout(request: Request) -> JSONResponse:
        s, acct, _ = await session(request, "POST", API + "/logout")
        await run_in_threadpool(lambda: s.odoo.site_drop(key=acct["session"]["key"]))
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @app.get(API + "/me")
    async def site_me(request: Request) -> JSONResponse:
        s, acct, _ = await session(request, "GET", API + "/me")
        return JSONResponse(await run_in_threadpool(me, s, acct), headers=NO_STORE)

    @app.post(API + "/profile")
    async def site_profile(request: Request) -> JSONResponse:
        s, acct, body = await session(request, "POST", API + "/profile")
        out = await run_in_threadpool(set_profile, s, acct, body)
        return JSONResponse(out, headers=NO_STORE)


__all__ = ["register", "site_account", "acting", "login", "closed", "prune", "signer"]
