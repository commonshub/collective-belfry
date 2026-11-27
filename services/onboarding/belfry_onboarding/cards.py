"""Printed cards (docs/cards.md): an A6 card with a QR code, ``https://chat.<domain>/g/<id>``.

The first person to scan a card *claims* it: they start a group (a ``group`` card) or a topic (a
``topic`` card: a channel whose id is its hashtag) and look after it. Every later scan shows the
group and brings people in through the card's guest link, the same path as any guest link
(guests.py). An admin makes and prints the cards in the console (cardsconsole.py). Each batch
says who its people become (docs/cards.md §6): members (the default, and every card made before
the choice) once they prove an e-mail address, or guests.

- A card's id is 6 characters of ``ALPHABET`` (capitals and digits without O, I, L, 0, 1). It is
  the only thing on the card, and it is no secret once the card is claimed.
- ``GET /api/cards/<id>`` (no auth): what the card is now.
- ``POST /api/cards/<id>/claim`` (NIP-98 by a signed-in account): start the group.
- ``POST /api/cards/<id>/claim-start`` and ``/claim-verify`` (NIP-98 by a key the browser just
  made): a visitor proves an e-mail address with a 6-digit code, is signed in (as the account
  that has the address, or as a new guest) and starts the group. The mail also has a Confirm
  button: ``/claim-confirm`` (any browser) starts the group the same way, the session bound to
  the claim-start key, which ``/claim-poll`` hands to the page that waits.
- ``GET /api/cards/hashtag/<name>``: whether a topic's hashtag is free.
- Claiming a topic takes ``summary``: ``weekly`` (also when missing) | ``monthly``: the person who
  claims follows the new topic by e-mail at that frequency (followers.py, their proven address).
- ``GET /api/groups/<id>/card`` and ``POST /api/cards/<id>/retire``: for the group's stewards.

The group is made by the groups key, with the tags the chat's own "New group" writes (9007 and
9002 with ``open`` and ``public``, then the "⬡ New group" post in ``general``); a topic is a
channel as the chat's "New channel" makes one. The person who claimed is put in with the admin
role (one 9000, the shape /group-roles and co-host links write), a guest too: a guest may be the
steward of the group they started from a card, of that group only.

Needs the belfry's own register (REGISTER=belfry): 409 ``unsupported`` otherwise. The locks are
in memory: one process serves the API (as everywhere here).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id, pass_fields
from .events import Limiter
from .holder_invites import ApiError, authenticate

log = logging.getLogger(__name__)

API = "/api/cards"
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no O, I, L, 0, 1: read aloud, typed from a photo
ID = re.compile(r"^[A-HJKMNP-Z2-9]{6}$")
KINDS = ("group", "topic")
HASHTAG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])$")  # 2–30, no dash at the ends
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TITLE_MAX, ABOUT_MAX, NAME_MAX = 80, 500, 100
PARENT = "general"  # where a new group is announced
# ids with a meaning of their own in the chat: never a topic's hashtag
TYPE_PREFIXES = ("pod-", "ev-", "ws-", "prop-")
OWN_IDS = frozenset({"general", "charter", "website"})
NO_STORE = {"Cache-Control": "no-store"}
# paths the community's website and its previews never call (discovery._chat_only): starting a
# group hands out a session, and published sites run on the apex
CHAT_ONLY = re.compile(r"^/api/(?:cards/.+|groups/[^/]+/card)$")

LOOKUPS = Limiter(30, 60)  # GET /cards/<id>, per client address
HASHTAGS = Limiter(30, 60)  # GET /cards/hashtag/<name>, per client address
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_ids_lock = threading.Lock()  # one group id chosen and made at a time: two topics, one hashtag


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


def _need_own(svc: Services) -> None:
    if not own(svc):
        raise ApiError(409, "unsupported", "Printed cards need the belfry's own register.")


def new_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(6))


def card_url(svc: Services, card_id: str) -> str:
    return f"https://chat.{svc.settings.nip05_domain}/g/{card_id}"


def _card_lock(card_id: str) -> threading.Lock:
    with _locks_guard:
        if len(_locks) > 5000:
            for k in [k for k, v in _locks.items() if not v.locked()]:
                del _locks[k]
        return _locks.setdefault(card_id, threading.Lock())


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "No such card.")


def _card(svc: Services, card_id: str) -> dict[str, Any]:
    """The card of an id as it came in the address (any case); 404 for none, and for anything
    that is not an id."""
    cid = (card_id or "").upper()
    card = svc.odoo.card_get(cid) if ID.fullmatch(cid) else None
    if card is None:
        raise _not_found()
    return card


def _invalid(field: str, message: str) -> ApiError:
    exc = ApiError(422, "invalid", message)
    exc.extra["field"] = field
    return exc


def _claimed(card: dict[str, Any]) -> ApiError:
    """409 ``claimed``: someone was first; their group and the link into it."""
    exc = ApiError(409, "claimed", "Someone started this one already.")
    exc.extra.update(group=card.get("group") or "", invite=card.get("invite") or "")
    return exc


def _refuse_taken(card: dict[str, Any]) -> None:
    if card["state"] == "claimed":
        raise _claimed(card)
    if card["state"] != "unclaimed":
        raise ApiError(409, "retired", "This card is not in use any more.")


# ── what a card is now ──────────────────────────────────────────────────────────────────────
def live_title(svc: Services, card: dict[str, Any]) -> str:
    """The group's name as the relay has it now; the one it was started with when the relay
    can't say."""
    from . import checkin

    group = card.get("group") or ""
    try:
        meta = checkin.current_meta(svc, group) if group else None
    except Exception as exc:  # noqa: BLE001 — the name it started with is a fine answer
        log.info("cards: name of a card's group not read: %s", type(exc).__name__)
        meta = None
    name = next((t[1] for t in meta or [] if t[:1] == ["name"] and len(t) > 1 and t[1]), "")
    return str(name or card.get("title") or "")[:TITLE_MAX]


def info(svc: Services, card_id: str) -> dict[str, Any]:
    from . import checkin

    _need_own(svc)
    card = _card(svc, card_id)
    out: dict[str, Any] = {"kind": card["kind"], "state": card["state"]}
    if card["state"] == "unclaimed":
        return out
    out["title"] = live_title(svc, card)
    if card["state"] != "claimed":
        return {**out, "state": "retired"}
    try:
        archived = checkin.is_archived(svc, card["group"])
    except Exception as exc:  # noqa: BLE001 — joining refuses an archived group anyway
        log.info("cards: archived not read: %s", type(exc).__name__)
        archived = False
    if archived:
        return {**out, "state": "archived"}
    return {**out, "group": card["group"], "invite": card["invite"]}


# ── a topic's hashtag ───────────────────────────────────────────────────────────────────────
def hashtag_state(svc: Services, name: str) -> str:
    """``free``, ``taken`` or ``invalid`` for NAME as a new channel's id: 2–30 of a–z, 0–9 and
    dashes. Taken: a channel or group with that id (a deleted one too), a name that reaches a
    channel by mail (its alias, its plain slug), someone's username or page, and the names
    nobody gets (the mail system's, the ids the chat gives a meaning). A relay or a signer that
    does not answer is an error (503), never "free"."""
    from . import events, mail, mailalias, usernames

    if not isinstance(name, str) or not HASHTAG.fullmatch(name):
        return "invalid"
    if (
        name.startswith(TYPE_PREFIXES)
        or name in OWN_IDS
        or name in mailalias.reserved()
        or name in mailalias.FOR_CHANNELS
        or usernames.DEFAULT.fullmatch(name)
    ):
        return "taken"
    found = events.groups(
        svc, [{"kinds": [39000], "#d": [name]}, {"kinds": [9007, 9008], "#h": [name]}]
    )
    if any(events.tag(e, "d" if e.get("kind") == 39000 else "h") == name for e in found):
        return "taken"
    # what mail.channel_id asks, step by step: it answers "no channel" when the relay behind
    # the slug lookup fails (right for mail, which then bounces); here that must be an error
    chosen = mailalias.channel_of(svc, name)
    if chosen and mail.channel_exists(svc, chosen):
        return "taken"
    if mail.channel_exists(svc, name) or mail.channel_by_slug(svc, name):
        return "taken"
    if mailalias._person(svc, name):  # 503 signer when it can't be asked
        return "taken"
    return "free"


# ── the form ────────────────────────────────────────────────────────────────────────────────
def _text(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def form_of(kind: str, data: dict[str, Any]) -> dict[str, str]:
    """``{"title", "about", "hashtag"}``, checked; 422 ``invalid`` with the ``field``. A topic
    needs no title (its name is its hashtag); older chats still send one."""
    title = _text(data.get("title"))
    if (not title and kind != "topic") or len(title) > TITLE_MAX:
        raise _invalid("title", f"A name, up to {TITLE_MAX} characters.")
    about = data.get("about")
    about = about.strip() if isinstance(about, str) else ""
    if len(about) > ABOUT_MAX:
        raise _invalid("about", f"A description of up to {ABOUT_MAX} characters.")
    hashtag = ""
    if kind == "topic":
        hashtag = data.get("hashtag") if isinstance(data.get("hashtag"), str) else ""
        if not HASHTAG.fullmatch(hashtag):
            raise _invalid("hashtag", "A hashtag: 2 to 30 letters, digits and dashes.")
    return {"title": title, "about": about, "hashtag": hashtag}


def slug(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:24].strip("-") or "group"


def _random_end() -> str:
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))


# ── who claims ──────────────────────────────────────────────────────────────────────────────
def account_of_key(svc: Services, pubkey: str) -> dict[str, Any]:
    """The signed-in account that may start a group: a member, or a guest with a saved e-mail.
    403 ``no_pass`` (nobody's key), ``agent``, ``guest_not_saved`` (a guest who only reads, or
    gave a name only), ``unverified`` (a buyer whose address is not proven yet)."""
    from . import guests, sales
    from .core import pass_fields

    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        pass_fields(svc, "tier", "guest_agent", "guest_unnamed", "partner_id"),
    )
    if not rows:
        raise ApiError(403, "no_pass", "This key holds no active pass.")
    if any(r.get("tier") == "agent" or r.get("guest_agent") for r in rows):
        raise ApiError(403, "agent", "An agent can't start a group from a card.")
    guest = all(r.get("tier") == "guest" for r in rows)
    if guest and (
        any(r.get("guest_unnamed") for r in rows) or not guests._saved_email(svc, pubkey)
    ):
        raise ApiError(403, "guest_not_saved", "Join with your name and e-mail address first.")
    if sales.unverified_key(svc, pubkey):
        raise ApiError(
            403, "unverified", "Confirm your e-mail address first (the link in your receipt)."
        )
    pid = next(
        (m2o_id(r.get("partner_id")) for r in rows if r.get("tier") != "guest"),
        m2o_id(rows[0].get("partner_id")),
    )
    partner = svc.odoo.read_one("res.partner", int(pid or 0), ["name"]) or {}
    return {
        "partner": int(pid or 0),
        "pubkey": pubkey,
        "name": _text(partner.get("name"))[:NAME_MAX],
        "guest": guest,
    }


# ── who the card makes of the people it brings in (docs/cards.md §6) ────────────────────────
def joins_as_member(card: dict[str, Any]) -> bool:
    """Whether people who join by CARD become members (the default, and every card made before
    the choice existed) rather than guests."""
    return (card.get("join_as") or "member") == "member"


def member_now(svc: Services, card: dict[str, Any], pubkey: str) -> bool:
    """The guest PUBKEY, whose address is proven, becomes a member of the belfry by CARD (the
    path a member link takes: the default role, no end, a username, the join line in the town
    square, the relay's list; their guest passes close, they stay in their channels). True when
    they are a member now; False (still a guest, logged) when it did not work."""
    from . import guests
    from .places import member_key

    terms = guests.card_terms(svc, card)
    if terms is None:
        return False
    try:
        guests.become_member(svc, pubkey, terms)
    except Exception as exc:  # noqa: BLE001 — still a guest: the group works the same
        log.warning(
            "cards: %s… not made a member by card %s: %s",
            pubkey[:8],
            card["id"],
            getattr(exc, "error", type(exc).__name__),
        )
    return member_key(svc, pubkey) is not None


# v2: once more after the WAL loss of 2026-11-26 (dbcheck.read_header); changes nothing for
# those done
MIGRATED = "belfry.cards_members_migrated.v2"


def migrate_card_guests(svc: Services) -> list[str]:
    """Once (MIGRATED): the guests who came in by a printed card whose batch makes members (every
    card made before the choice existed: §6) and whose address is proven become members now, by
    the same path (``member_now``): those who joined by its link, and a guest who started its
    group. Reading guests, guests without an address, buyers whose address is not proven yet and
    agents stay as they are. Recorded only when everyone it tried is a member: a start after a
    failure tries again (it changes nothing for those done). Returns the keys made members."""
    from . import guests, sales
    from .places import member_key

    if not own(svc) or svc.odoo.param(MIGRATED):
        return []
    by_invite: dict[int, dict[str, Any]] = {}
    by_card: dict[str, dict[str, Any]] = {}
    for card in svc.odoo.card_list():
        if not card.get("invite") or not joins_as_member(card):
            continue
        by_card[card["id"]] = card
        inv = svc.odoo.invite_by_token(card["invite"])
        if inv:
            by_invite[int(inv["id"])] = card
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["tier", "=", "guest"]],
        pass_fields(
            svc, "keycast_pubkey", "invite_id", "guest_card", "guest_agent", "guest_unnamed"
        ),
    )
    found: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: int(r["id"])):
        card = by_invite.get(m2o_id(r.get("invite_id")) or 0) or by_card.get(
            str(r.get("guest_card") or "")
        )
        if card and r.get("keycast_pubkey"):
            found.setdefault(str(r["keycast_pubkey"]), card)
    made: list[str] = []
    ok = True
    for pubkey, card in found.items():
        if member_key(svc, pubkey) is not None or guests._is_agent(svc, pubkey):
            continue
        if guests._unnamed(svc, pubkey) or not guests._saved_email(svc, pubkey):
            continue  # reading, or no address yet: a member once they save one (guest/save)
        if sales.unverified_key(svc, pubkey):
            continue
        if member_now(svc, card, pubkey):
            log.info("cards: %s… a member now (came by card %s)", pubkey[:8], card["id"])
            made.append(pubkey)
        else:
            ok = False
    if ok:
        svc.odoo.set_param(MIGRATED, str(int(time.time())))
    log.info("cards: %d card guests made members (%s)", len(made), "done" if ok else "again later")
    return made


# ── starting the group ──────────────────────────────────────────────────────────────────────
def _publish(svc: Services, kind: int, tags: list[list[str]], content: str = "") -> None:
    from . import events
    from .retries import RelayDown

    if not events.publish(svc, kind, tags, content):
        raise RelayDown


def _drop_pass(svc: Services, pass_id: int | None) -> None:
    """A guest pass made for a group that was not started after all."""
    from . import core

    if not pass_id:
        return
    try:
        core.revoke_pass(svc, int(pass_id), "the group was not started")
    except Exception:  # noqa: BLE001 — it opens nothing: the group does not exist
        log.exception("cards: guest pass %s not closed", pass_id)


def choose_group(svc: Services, card: dict[str, Any], form: dict[str, str]) -> str:
    """The id of the group to make: a topic's hashtag (409 ``hashtag_taken``), or ``pod-`` and
    the name's slug with four random characters, as the chat gives a member's group."""
    if card["kind"] != "topic":
        return f"pod-{slug(form['title'])}-{_random_end()}"
    state = hashtag_state(svc, form["hashtag"])
    if state == "invalid":
        raise _invalid("hashtag", "A hashtag: 2 to 30 letters, digits and dashes.")
    if state != "free":
        raise ApiError(409, "hashtag_taken", "This hashtag is taken. Try another one.")
    return form["hashtag"]


def make_group(
    svc: Services,
    card: dict[str, Any],
    group: str,
    form: dict[str, str],
    who: dict[str, Any],
    guest_until: str = "",
) -> None:
    """The group on the relay, by the groups key, and the person who started it in it with the
    admin role. Raises ``RelayDown`` (503 ``relay``); a group whose name could not be written is
    deleted again."""
    from . import events

    topic = card["kind"] == "topic"
    title, about = form["title"], form["about"]
    # a group as the chat's "New group" makes one (open, public); a topic as its "New channel"
    access = [] if topic else [["open"], ["public"]]
    # the groups key writes these for the person: `by` says whose they are (the creator)
    by = ["by", who["pubkey"]]
    _publish(svc, 9007, [["h", group], *access, by])
    try:
        # a topic is named by its hashtag (#bread), as every channel is; the topic in words leads
        # its description
        name = form["hashtag"] if topic else title
        # a topic without a title (the new form): its description alone
        desc = about if not topic or not title else (f"{title} · {about}" if about else title)
        _publish(svc, 9002, [["h", group], ["name", name], ["about", desc], *access, by])
        tags = [["h", group], ["p", who["pubkey"], "admin"], ["role", "admin"]]
        tags.append(["by", who["pubkey"]])
        if who["name"]:
            tags.append(["name", who["name"]])
        if guest_until:
            tags.append(["guest", guest_until])
        _publish(svc, 9000, tags)
    except Exception:
        events.publish(svc, 9008, [["h", group]])  # best effort: no half-made group stays
        raise
    # the rest is a courtesy: the group exists and has its steward
    # announced in the town square, groups as the chat's New group does, topics with a link
    # that the apps show as the channel's card
    if topic:
        from .followers import chat_link

        words = " · ".join(x for x in (title, about) if x)
        text = f"# New topic: #{group}{' · ' + words if words else ''}"
        link = chat_link(svc, group)
        tags = [["h", PARENT], ["t", "new-topic"], ["channel", group], by]
        if not events.publish(svc, 9, tags, f"{text}\n{link}" if link else text):
            log.warning("cards: %s not announced in #%s", group, PARENT)
    else:
        text = f"⬡ New group: {title}{' · ' + about if about else ''}"
        if not events.publish(svc, 9, [["h", PARENT], ["pod", group], by], text):
            log.warning("cards: %s not announced in #%s", group, PARENT)
    name = who["name"] or "Somebody"
    what = "topic" if topic else "group"
    line = f"{name} created the {what} from card {card['id']}"
    # ["card", "1"] marks the line (apps take its p as the creator for groups made before `by`);
    # ["card_id", id] names the card, which the apps show as the approved boards draw it
    tags = [["h", group], ["p", who["pubkey"]], ["card", "1"], ["card_id", card["id"]], by]
    if not events.publish(svc, 9, tags, line):
        log.warning("cards: no first line in %s", group)


def start(
    svc: Services,
    card: dict[str, Any],
    form: dict[str, str],
    who: dict[str, Any],
    group: str,
    pass_id: int | None = None,
) -> str:
    """WHO starts GROUP from CARD (the card's lock is held, the card is unclaimed). A guest gets
    a guest pass for the group first (PASS_ID: the pending one of a guest made just now), so the
    relay puts them in as a guest of it. Returns the group's id."""
    from . import guests

    until = ""
    fresh = pass_id is not None
    if who["guest"] and joins_as_member(card):
        # a members' card (§6): the proven person is a member of the belfry before the group is
        # made, so the group gets a member as its steward (no guest pass, no `guest` tag)
        if fresh:
            svc.odoo.write("belfry.pass", [int(pass_id)], {"state": "active"})
        if member_now(svc, card, who["pubkey"]):
            who, pass_id, fresh = {**who, "guest": False}, None, False
    if who["guest"]:
        if not fresh:
            got = svc.odoo.add_guest_channel(who["pubkey"], group, who["pubkey"], guests.DAYS)
            pass_id = int(got["pass_id"]) if got and got.get("kind") == "guest" else None
            if pass_id is None:  # no pass to add to (an admin whose own pass lapsed): no guest
                who = {**who, "guest": False}
        if pass_id:
            # their own group: this pass has no end while the card is theirs (docs/cards.md §4)
            until = svc.odoo.card_steward_pass(card["id"], int(pass_id))
    try:
        make_group(svc, card, group, form, who, until)
        if fresh:
            svc.odoo.write("belfry.pass", [int(pass_id)], {"state": "active"})
        token = svc.odoo.card_invite(who["partner"], who["pubkey"], group, guests.DAYS)
        ok = svc.odoo.card_claim(
            card["id"],
            group,
            token,
            form["title"] or form["hashtag"],
            who["partner"],
            who["pubkey"],
            int(time.time()),
        )
    except Exception:
        if who["guest"]:
            _drop_pass(svc, pass_id)
        raise
    if not ok:  # another process was first (the lock is this process's only)
        log.error("cards: %s claimed twice; %s stays without its card", card["id"], group)
        raise _claimed(svc.odoo.card_get(card["id"]) or card)
    log.info(
        "cards: %s started %s (%s) by partner %s", card["id"], group, card["kind"], who["partner"]
    )
    return group


def claim(svc: Services, card_id: str, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    """A signed-in account starts the card's group → ``{"group"}``."""
    from . import followers

    _need_own(svc)
    card = _card(svc, card_id)
    form = form_of(card["kind"], data)
    summary = followers.summary_choice(data)
    _refuse_taken(card)
    who = account_of_key(svc, caller)
    with _card_lock(card["id"]):
        card = _card(svc, card["id"])
        _refuse_taken(card)
        with _ids_lock:
            group = choose_group(svc, card, form)
            start(svc, card, form, who, group)
    who, made = _as_member(svc, card, who, {})
    return {"group": group, **made, **_follow_topic(svc, card, who, group, summary)}


def _follow_topic(
    svc: Services, card: dict[str, Any], who: dict[str, Any], group: str, summary: str
) -> dict[str, Any]:
    """The person who started a topic follows it by e-mail (``{"summary": "weekly"}``), best
    effort: ``{}`` for a group card, or when it could not be saved."""
    from . import followers

    if card["kind"] != "topic" or not followers.follow(svc, who["partner"], group, summary):
        return {}
    return {"summary": summary}


# ── a visitor: an e-mail address, a code or the mail's Confirm button, then the group ────────
PURPOSE = "card"
CLAIM_SECONDS = 15 * 60  # the code and the Confirm button (register.CARD_CODE_MINUTES)
POLLS = Limiter(30, 60)  # POST /cards/<id>/claim-poll, per key
CONFIRMS = Limiter(20, 60)  # POST /cards/<id>/claim-confirm, per client address
FORM_FIELDS = ("name", "title", "about", "hashtag")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _email_of(data: dict[str, Any]) -> str:
    email = _text(data.get("email"))
    if not EMAIL.fullmatch(email) or len(email) > 200:
        raise _invalid("email", "That is not an e-mail address.")
    return email


def _visitor_form(svc: Services, card: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """What a visitor fills in before the code: the card's form, their name, the topic's summary
    and the client; 422 ``invalid`` (``field``), 409 ``hashtag_taken``."""
    from . import followers

    form: dict[str, Any] = dict(form_of(card["kind"], data))
    name = _text(data.get("name"))
    if not name or len(name) > NAME_MAX:
        raise _invalid("name", "Your name, please.")
    form["name"] = name
    form["summary"] = followers.summary_choice(data)
    form["client"] = str(data.get("client") or "")[:40]
    if card["kind"] == "topic" and hashtag_state(svc, form["hashtag"]) != "free":
        raise ApiError(409, "hashtag_taken", "This hashtag is taken. Try another one.")
    return form


def claim_start(
    svc: Services, card_id: str, key: str, data: dict[str, Any], ip: str
) -> dict[str, Any]:
    """Mails the 6-digit code and a Confirm button. Nothing is made before the proof; the form
    (``name``, ``title``, ``about``, ``hashtag``) is kept with the pending claim for 15 minutes, so
    that the button can start the group. Older chats send the e-mail address alone (and the form
    with the code): their mail has the code only."""
    from . import applyverified

    _need_own(svc)
    card = _card(svc, card_id)
    _refuse_taken(card)
    email = _email_of(data)
    legacy = not any(f in data for f in FORM_FIELDS)
    words: dict[str, Any] = {"card": card["id"]}
    if not legacy:
        form = _visitor_form(svc, card, data)
        token = secrets.token_urlsafe(32)
        svc.odoo.card_claim_put(
            card["id"], key, email, form, _hash(token), int(time.time()) + CLAIM_SECONDS
        )
        words["mail"] = {
            "kind": card["kind"],
            "name": form["name"],
            "title": form["title"],
            "hashtag": form["hashtag"],
            "url": f"{card_url(svc, card['id'])}?confirm={token}",
        }
    try:
        applyverified._send(svc, email, key, PURPOSE, words, ip)
    except ApiError as exc:
        if not legacy:  # no mail: no link that could confirm it
            svc.odoo.card_claim_move(card["id"], key, "pending", "done")
        if exc.error == "invalid":
            exc.extra["field"] = "email"
        raise
    return {"sent": True}


def _check_code(svc: Services, email: str, key: str, code: str, card_id: str) -> None:
    """The right code for this card, used; 400 ``wrong_code`` (``tries_left``), 410 ``expired``."""
    from . import applyverified

    try:
        data = applyverified._check(svc, email, key, code, PURPOSE)
    except ApiError as exc:
        if exc.error == "wrong_code":
            exc.status = 400
        raise
    if data.get("card") != card_id:  # a code asked for another card
        raise ApiError(410, "expired", "That code expired. Send a new one.")


def _signer_down(exc: Exception) -> ApiError:
    log.warning("cards: the signer did not answer: %s", type(exc).__name__)
    return ApiError(503, "signer", "The signer isn't answering: try again in a moment.")


def _existing(svc: Services, member: dict[str, Any], key: str, client: str) -> tuple[dict, dict]:
    """The account that has the proven address: a session on it bound to KEY. 409 ``agent``,
    ``own_key`` (it signs with its own key: no session to hand out), ``key_taken``; 503."""
    from . import guests
    from .places import member_key

    pubkey = str(member["pubkey"])
    partner = int(member["partner"])
    if guests._is_agent(svc, pubkey):
        raise ApiError(409, "agent", "This address is an agent's: agents sign with their key.")
    opened = guests._sign_in_as(svc, partner, key, client)
    if not opened:
        raise ApiError(409, "own_key", "This account signs in with its own key.")
    row = svc.odoo.read_one("res.partner", partner, ["name"]) or {}
    who = {
        "partner": partner,
        "pubkey": pubkey,
        "name": _text(row.get("name"))[:NAME_MAX],
        "guest": member_key(svc, pubkey) is None,
    }
    return who, opened


def _new_guest(
    svc: Services, group: str, name: str, email: str, key: str, client: str
) -> tuple[dict, dict, int]:
    """A new guest of GROUP with this proven address and name: a contact, a pending guest pass,
    an account in the signer and its first session, bound to KEY. The pass opens once the group
    exists (``start``)."""
    from . import apps as apps_mod
    from . import core, guests, nostr
    from .clients.keycast import ClientKeyTaken

    got = svc.odoo.web_guest(group, name, email, guests.DAYS)
    if got.get("kind") != "new":  # an account after all (it had none a moment ago)
        raise ApiError(503, "failed", "That did not work just now. Try again.")
    pid, pass_id = int(got["partner_id"]), int(got["pass_id"])
    try:
        row = svc.odoo.read_one("res.partner", pid, ["name"]) or {}
        named = _text(row.get("name"))[:NAME_MAX] or name
        try:
            pubkey, npub, token = core._ensure_keycast_user(
                svc, {"id": pid, "name": named}, f"guest-{pid}"
            )
            svc.odoo.write("res.partner", [pid], {"belfry_npub": npub})
            svc.odoo.write(
                "belfry.pass", [pass_id], {"keycast_pubkey": pubkey, "guest_client": key}
            )
            label = apps_mod.NATIVE_CLIENTS.get(client, "Chat")
            bunker = svc.keycast.create_bunker(token, label, key) if token else {}
        except ClientKeyTaken as exc:
            raise ApiError(
                409, "key_taken", "This device's key is linked to another account: start again."
            ) from exc
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _signer_down(exc) from exc
        if not bunker.get("bunker_url"):
            raise _signer_down(RuntimeError("no session"))
    except Exception:
        _drop_pass(svc, pass_id)
        raise
    who = {"partner": pid, "pubkey": pubkey, "name": named, "guest": True}
    opened = {
        "signed_in": True,
        "bunker_url": bunker["bunker_url"],
        "pubkey": pubkey,
        "npub": nostr.hex_to_npub(pubkey),
    }
    if nip05 := guests._account_nip05(svc, pid):
        opened["nip05"] = nip05
    return who, opened, pass_id


def _expired() -> ApiError:
    return ApiError(410, "expired", "That code expired. Send a new one.")


def _make(
    svc: Services, card: dict[str, Any], form: dict[str, Any], email: str, key: str
) -> tuple[dict, dict, str]:
    """The proven visitor starts CARD's group (the card's lock is held, the card is unclaimed):
    signed in as the account that has EMAIL, or as a new guest, the session bound to KEY →
    (who, the signed-in body, the group)."""
    from . import applyverified

    member = applyverified._account(svc, email)
    with _ids_lock:
        group = choose_group(svc, card, form)
        if member is not None:
            who, opened = _existing(svc, member, key, form["client"])
            start(svc, card, form, who, group)
        else:
            who, opened, pass_id = _new_guest(svc, group, form["name"], email, key, form["client"])
            start(svc, card, form, who, group, pass_id)
    return (*_as_member(svc, card, who, opened), group)


def _as_member(
    svc: Services, card: dict[str, Any], who: dict[str, Any], opened: dict[str, Any]
) -> tuple[dict, dict]:
    """After a members' card made a guest a member (``start``): WHO says so, and the signed-in
    body says ``"member": true`` with their new name@domain."""
    from . import guests
    from .places import member_key

    if not who["guest"] or not joins_as_member(card) or member_key(svc, who["pubkey"]) is None:
        return who, opened
    opened = {**opened, "member": True}
    if nip05 := guests._account_nip05(svc, int(who["partner"])):
        opened["nip05"] = nip05
    return {**who, "guest": False}, opened


def _done_claim(claim: dict[str, Any] | None) -> bool:
    """A pending claim that started its group already (by its code or its button)."""
    return claim is not None and claim["state"] in ("confirmed", "done")


def claim_verify(svc: Services, card_id: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
    """The code: the visitor is signed in (the account that has the address, or a new guest whose
    first session is KEY) and starts the group → ``{"signed_in": true, "bunker_url", "pubkey",
    "npub", "nip05"?, "group"}``. The form comes from claim-start, or from this body (older
    chats: it wins when it is there); it is checked before the code is spent. 410 ``expired``
    when the mail's Confirm button started it already."""
    _need_own(svc)
    card = _card(svc, card_id)
    claim = svc.odoo.card_claim_get(card["id"], key)
    if _done_claim(claim):
        raise _expired()
    stored = claim if claim is not None and claim["state"] == "pending" else None
    if any(f in data for f in FORM_FIELDS) or stored is None:
        form = _visitor_form(svc, card, data)
        email = _email_of(data)
    else:
        form = dict(stored["form"])
        email = _email_of(data) if data.get("email") else stored["email"]
        if card["kind"] == "topic" and hashtag_state(svc, form["hashtag"]) != "free":
            raise ApiError(409, "hashtag_taken", "This hashtag is taken. Try another one.")
    if "client" in data:
        form["client"] = str(data.get("client") or "")[:40]
    _refuse_taken(card)
    _check_code(svc, email, key, str(data.get("code") or ""), card["id"])
    with _card_lock(card["id"]):
        if _done_claim(svc.odoo.card_claim_get(card["id"], key)):
            raise _expired()  # the button was quicker
        card = _card(svc, card["id"])
        _refuse_taken(card)  # someone was first: nobody is made, nothing is started
        who, opened, group = _make(svc, card, form, email, key)
        if claim is not None:
            svc.odoo.card_claim_move(card["id"], key, "pending", "done")
    follow = _follow_topic(svc, card, who, group, form["summary"])
    return {**opened, "group": group, **follow}


def claim_confirm(
    svc: Services, card_id: str, clicker: str, data: dict[str, Any]
) -> dict[str, Any]:
    """The mail's Confirm button, opened by any browser (CLICKER): the address is proven by the
    link, and the claim happens as the code's would, the session bound to the key that started
    it (the page that waits) → ``{"confirmed": true, "group", "same_device"}``, and the
    signed-in body too when CLICKER is that key. 410 ``expired`` (also when used, or when the
    code was quicker), 409 ``claimed``."""
    _need_own(svc)
    card = _card(svc, card_id)
    token = data.get("token")
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        raise _expired()
    claim = svc.odoo.card_claim_by_token(card["id"], _hash(token))
    if claim is None or claim["state"] != "pending" or claim["expires_at"] <= time.time():
        raise _expired()
    starter, email, form = claim["pubkey"], claim["email"], dict(claim["form"])
    _refuse_taken(card)
    if card["kind"] == "topic" and hashtag_state(svc, form["hashtag"]) != "free":
        raise ApiError(409, "hashtag_taken", "This hashtag is taken. Try another one.")
    same = clicker == starter
    with _card_lock(card["id"]):
        now = svc.odoo.card_claim_by_token(card["id"], _hash(token))
        if now is None or now["state"] != "pending" or now["expires_at"] <= time.time():
            raise _expired()
        card = _card(svc, card["id"])
        _refuse_taken(card)
        who, opened, group = _make(svc, card, form, email, starter)
        # one claim only: the code of this address and key works no more
        svc.odoo.key_code_expire(email, starter, PURPOSE)
        follow = _follow_topic(svc, card, who, group, form["summary"])
        result = {**opened, "group": group, **follow}
        if same:  # the page that waits gets nothing more: this browser has it
            svc.odoo.card_claim_move(card["id"], starter, "pending", "done")
        else:
            svc.odoo.card_claim_move(card["id"], starter, "pending", "confirmed", result)
    log.info("cards: %s confirmed by its link (same device: %s)", card["id"], same)
    out: dict[str, Any] = {"confirmed": True, "group": group, "same_device": same}
    return {**out, **result} if same else out


def claim_poll(svc: Services, card_id: str, key: str) -> tuple[dict[str, Any], int]:
    """The page that waits (the key that started the claim): 202 ``{"pending": true}``, then
    once the mail's button was clicked elsewhere the signed-in body (200), handed out once. 410
    ``expired`` (the claim ran out, or its session was handed out already), 404 (no claim of
    this key)."""
    _need_own(svc)
    card = _card(svc, card_id)
    if not POLLS.allow("key:" + key):
        raise ApiError(429, "too_many", "Too many requests for now. Try again in a minute.")
    claim = svc.odoo.card_claim_get(card["id"], key)
    if claim is None:
        raise ApiError(404, "not_found", "No claim of this card is waiting here.")
    # a confirmed session waits for the page as long again as the code worked
    if claim["state"] == "confirmed" and claim["expires_at"] + CLAIM_SECONDS > time.time():
        if svc.odoo.card_claim_move(card["id"], key, "confirmed", "done"):
            return dict(claim["result"] or {}), 200
    if claim["state"] == "pending" and claim["expires_at"] > time.time():
        return {"pending": True}, 202
    raise _expired()


# ── for the group's stewards ────────────────────────────────────────────────────────────────
def _may_manage(svc: Services, caller: str, group: str) -> bool:
    """The group's stewards (its admins, by the relay's list, as its creator or by role) and the
    belfry's admins."""
    from . import checkin

    if caller in checkin._belfry_admins(svc):
        return True
    return bool(group) and caller in checkin.group_state(svc, group).admins


def group_card(svc: Services, caller: str, group: str) -> dict[str, Any]:
    """``{"id", "kind", "started_by": {"name", "pubkey"}, "joined_by_scan"}`` for GROUP's
    stewards; 403 ``not_allowed``, 404 when the group has no card in use."""
    from . import checkin

    _need_own(svc)
    if not checkin.GROUP_RE.match(group or ""):
        raise ApiError(404, "not_found", "No such group.")
    if not _may_manage(svc, caller, group):
        raise ApiError(403, "not_allowed", "Only the group's stewards see its card.")
    card = svc.odoo.card_of_group(group)
    if card is None or card["state"] != "claimed":
        raise ApiError(404, "not_found", "This group has no card.")
    partner = svc.odoo.read_one("res.partner", int(card.get("claimed_by") or 0), ["name"]) or {}
    return {
        "id": card["id"],
        "kind": card["kind"],
        "started_by": {
            "name": _text(partner.get("name"))[:NAME_MAX] or "Somebody",
            "pubkey": card.get("claimed_key") or "",
        },
        "joined_by_scan": svc.odoo.card_invite_uses(card.get("invite")),
    }


def retire(svc: Services, card_id: str, caller: str) -> dict[str, Any]:
    """The card stops: it starts nothing and its link brings nobody in. By the stewards of its
    group or a belfry admin (a card nobody claimed: a belfry admin)."""
    _need_own(svc)
    card = _card(svc, card_id)
    if not _may_manage(svc, caller, card.get("group") or ""):
        raise ApiError(403, "not_allowed", "Only the group's stewards retire its card.")
    if svc.odoo.card_retire(card["id"]):
        log.info("cards: %s retired by %s…", card["id"], caller[:8])
    return {"state": "retired"}


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from . import retries
    from .sales import client_ip

    def svc(request: Request) -> Services:
        return request.app.state.services

    def limited(request: Request, limiter: Limiter) -> None:
        if not limiter.allow("ip:" + client_ip(request)):
            raise ApiError(429, "too_many", "Too many requests for now. Try again in a minute.")

    async def signed(request: Request, method: str, path: str) -> tuple[str, dict[str, Any]]:
        raw = await request.body() if method == "POST" else b""
        key = authenticate(
            svc(request),
            request.app.state.api_seen,
            request.headers.get("authorization"),
            method,
            path,
            raw,
        )
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return key, body

    @app.get(API + "/hashtag/{name}")
    async def card_hashtag(request: Request, name: str) -> JSONResponse:
        limited(request, HASHTAGS)
        _need_own(svc(request))
        state = await retries.run(hashtag_state, svc(request), name)
        return JSONResponse({"state": state}, headers=NO_STORE)

    @app.get(API + "/{card_id}")
    async def card_info(request: Request, card_id: str) -> JSONResponse:
        limited(request, LOOKUPS)
        out = await run_in_threadpool(info, svc(request), card_id)
        return JSONResponse(out, headers=NO_STORE)

    @app.post(API + "/{card_id}/claim")
    async def card_claim(request: Request, card_id: str) -> JSONResponse:
        key, body = await signed(request, "POST", f"{API}/{card_id}/claim")
        out = await retries.run(claim, svc(request), card_id, key, body)
        return JSONResponse(out, headers=NO_STORE)

    @app.post(API + "/{card_id}/claim-start")
    async def card_claim_start(request: Request, card_id: str) -> JSONResponse:
        key, body = await signed(request, "POST", f"{API}/{card_id}/claim-start")
        out = await run_in_threadpool(
            claim_start, svc(request), card_id, key, body, client_ip(request)
        )
        return JSONResponse(out, status_code=202, headers=NO_STORE)

    @app.post(API + "/{card_id}/claim-verify")
    async def card_claim_verify(request: Request, card_id: str) -> JSONResponse:
        key, body = await signed(request, "POST", f"{API}/{card_id}/claim-verify")
        out = await retries.run(claim_verify, svc(request), card_id, key, body)
        return JSONResponse(out, headers=NO_STORE)

    @app.post(API + "/{card_id}/claim-confirm")
    async def card_claim_confirm(request: Request, card_id: str) -> JSONResponse:
        limited(request, CONFIRMS)
        key, body = await signed(request, "POST", f"{API}/{card_id}/claim-confirm")
        out = await retries.run(claim_confirm, svc(request), card_id, key, body)
        return JSONResponse(out, headers=NO_STORE)

    @app.post(API + "/{card_id}/claim-poll")
    async def card_claim_poll(request: Request, card_id: str) -> JSONResponse:
        key, _body = await signed(request, "POST", f"{API}/{card_id}/claim-poll")
        out, status = await run_in_threadpool(claim_poll, svc(request), card_id, key)
        return JSONResponse(out, status_code=status, headers=NO_STORE)

    @app.post(API + "/{card_id}/retire")
    async def card_retire(request: Request, card_id: str) -> JSONResponse:
        key, _body = await signed(request, "POST", f"{API}/{card_id}/retire")
        out = await retries.run(retire, svc(request), card_id, key)
        return JSONResponse(out, headers=NO_STORE)

    @app.get("/api/groups/{group}/card")
    async def group_card_api(request: Request, group: str) -> JSONResponse:
        key, _body = await signed(request, "GET", f"/api/groups/{group}/card")
        out = await retries.run(group_card, svc(request), key, group)
        return JSONResponse(out, headers=NO_STORE)


__all__ = [
    "register",
    "info",
    "claim",
    "claim_start",
    "claim_verify",
    "claim_confirm",
    "claim_poll",
    "retire",
    "group_card",
]
