"""An event's conversation, photos, links and participants, for the community's own website
(docs/website-accounts.md "An event's conversation, photos, links and participants").

The website holds no signer session, so it can't read or write the groups relay. These
endpoints do it for someone who has the event's group (``ev-<d>``): admitted to it (with or
without a ticket), one of its hosts, or an admin of the belfry. Signed with NIP-98 by a website
session (it acts as its account, sitesessions.py) or by an account's own key.

- ``GET  /api/events/<link>/chat?before=&limit=``: the messages, as plain text and pictures.
- ``POST /api/events/<link>/chat``: a message, signed as the person through the signer, as the
  web chat would write it (kind 9, or a NIP-22 kind 1111 for a reply; NIP-92 ``imeta``).
- ``GET  /api/events/<link>/photos``, ``/links``, ``/participants``.

Nobody is ever named by a key here: a person is ``{"name", "picture", "slug", "role"}``.
What is read from the relay is kept ``CACHE_SECONDS`` per group (emptied by a message posted
here and by anything this server publishes); who someone is, ``PEOPLE_SECONDS``. Needs the
belfry's own register (REGISTER=belfry): 409 ``unsupported`` otherwise.
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

from . import relay as _relay_mod
from . import retries
from .core import Services
from .holder_invites import ApiError, authenticate

log = logging.getLogger(__name__)

API = "/api/events"
CACHE_SECONDS = 10.0  # what was read from the relay, per group
PEOPLE_SECONDS = 60.0  # a person's name, picture and slug
READ_MAX = 2000  # the newest messages of a group that are ever looked at
LIMIT_DEFAULT = 50
PHOTOS_DEFAULT = 60
LIMIT_MAX = 100
LINKS_MAX = 200
TEXT_MAX = 4000
IMAGES_MAX = 6
POSTS_PER_HOUR = 30  # per account
READS_PER_MINUTE = 120  # per account
EDIT_KIND = 1010
NOBODY = "Somebody"
NO_STORE = {"Cache-Control": "no-store"}
HEX = re.compile(r"^[0-9a-f]{64}$")
BECH = "02-9ac-hj-np-z"
# a mention as the apps write it (NIP-27), and a bare npub that is not part of an address
MENTION = re.compile(
    rf"nostr:(npub1[{BECH}]{{58}}|nprofile1[{BECH}]+)|(?<![\w/:.@-])(npub1[{BECH}]{{58}})"
)
# a reference to an event or a channel: an id, which nobody is shown as text
REFERENCE = re.compile(rf"nostr:(?:note|nevent|naddr)1[{BECH}]+")
NPUB = re.compile(rf"nostr:(npub1[{BECH}]{{58}})")
HASHTAG = re.compile(r"(^|[^\w&/])#(\w[\w-]{0,48})")
LINK = re.compile(r"https?://[^\s<>\"'`]+")
IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "heic": "image/heic",
    "heif": "image/heif",
}
# what the chat's own cards start with (an event, a proposal or its new version, a new group)
CARD = ("📅 ", "📝 ", "⬡ ")
CARD_TAGS = ("pod", "proposal", "version", "mail-from", "mail-original")

_lock = threading.Lock()
_snaps: dict[str, tuple[float, dict[str, Any]]] = {}
_people: dict[str, tuple[float, dict[str, Any]]] = {}


def forget() -> None:
    """Something was published: the groups are read again."""
    with _lock:
        _snaps.clear()


def forget_people() -> None:
    """A profile, a slug or a name changed (villagers.forget): people are read again."""
    with _lock:
        _people.clear()


if forget not in _relay_mod.PUBLISHED:
    _relay_mod.PUBLISHED.append(forget)


def _tag(e: dict[str, Any], name: str) -> str:
    for t in e.get("tags", []):
        if t[:1] == [name] and len(t) > 1:
            return str(t[1])
    return ""


def _has(e: dict[str, Any], name: str) -> bool:
    return any(t[:1] == [name] for t in e.get("tags", []))


def blossom(svc: Services) -> str:
    """The belfry's own Blossom, as people's pictures are addressed."""
    from . import events

    public = (getattr(svc.settings, "blossom_public_url", "") or "").rstrip("/")
    return public or f"https://blossom.{events.domain(svc)}"


# ── reading a group ─────────────────────────────────────────────────────────────────────────
def _plain(e: dict[str, Any]) -> bool:
    """A message someone wrote: not one of the chat's cards (an event, a proposal, a new
    group), not an e-mail card."""
    if any(_has(e, k) for k in CARD_TAGS) or _tag(e, "client") == "belfry-mail-out":
        return False
    return not (_has(e, "a") and (e.get("content") or "").startswith(CARD))


def _media(svc: Services, e: dict[str, Any]) -> tuple[str, list[dict], list[dict]]:
    """(the text without its attachments' addresses, images, files) of a message or an edit:
    NIP-92 ``imeta`` tags, and bare picture addresses on the belfry's own Blossom."""
    text = e.get("content") or ""
    images: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in e.get("tags", []):
        if t[:1] != ["imeta"]:
            continue
        f = {}
        for p in t[1:]:
            if isinstance(p, str) and " " in p:
                k, v = p.split(" ", 1)
                f.setdefault(k, v)
        url = f.get("url", "")
        if not re.match(r"^https?://[^\s]+$", url) or url in seen:
            continue
        seen.add(url)
        ext = (
            url.rsplit("/", 1)[-1].rpartition(".")[2].lower()
            if "." in url.rsplit("/", 1)[-1]
            else ""
        )
        kind = f.get("m") or IMAGE_TYPES.get(ext, "" if ext else "image/*")
        name = " ".join(_scrub(f.get("alt", "")).split())[:200]
        if kind.startswith("image/"):
            images.append({"url": url, "alt": name})
        else:
            files.append({"url": url, "type": kind[:100], "name": name})
    own = re.compile(
        re.escape(blossom(svc)) + r"/[0-9a-f]{64}\.(?:" + "|".join(IMAGE_TYPES) + r")(?![\w.])"
    )
    for m in own.finditer(text):
        if m.group(0) not in seen:
            seen.add(m.group(0))
            images.append({"url": m.group(0), "alt": ""})
    for url in sorted(seen, key=len, reverse=True):
        text = text.replace(url, "")
    return text.strip(), images, files


def _mention_key(word: str) -> str:
    """The key an ``npub1…`` or ``nprofile1…`` names, "" when it names none."""
    from . import nostr

    try:
        hrp, raw = nostr.bech32_decode(word)
    except ValueError:
        return ""
    if hrp == "npub" and len(raw) == 32:
        return raw.hex()
    if hrp == "nprofile":
        i = 0
        while i + 2 <= len(raw):
            kind, size = raw[i], raw[i + 1]
            value = raw[i + 2 : i + 2 + size]
            if kind == 0 and len(value) == 32:
                return value.hex()
            i += 2 + size
    return ""


def _scrub(text: str) -> str:
    """TEXT with nobody's key in it (a file's name, a line nobody's name could be put in)."""
    return REFERENCE.sub("", MENTION.sub("", text))


def _parse(svc: Services, e: dict[str, Any], edit: dict[str, Any] | None = None) -> dict[str, Any]:
    """A message as it is kept: its author's key stays in here, never in an answer."""
    text, images, files = _media(svc, edit or e)
    if e["kind"] == 1111:
        reply = _tag(e, "e") or _tag(e, "E")
    else:
        reply = _tag(e, "q")
    return {
        "id": e["id"],
        "at": int(e.get("created_at") or 0),
        "pubkey": e["pubkey"],
        "kind": e["kind"],
        "root": (_tag(e, "E"), _tag(e, "K"), _tag(e, "P")) if e["kind"] == 1111 else None,
        "text": text,
        "mentions": sorted(
            {k for m in MENTION.finditer(text) if (k := _mention_key(m.group(1) or m.group(2)))}
        ),
        "images": images,
        "files": files,
        "reply_to": reply if HEX.match(reply) else None,
        "edited_at": int(edit["created_at"]) if edit else None,
    }


def _read(svc: Services, group: str) -> dict[str, Any]:
    from . import checkin, events

    found = events.groups(
        svc,
        [
            {"kinds": [9, 1111], "#h": [group], "limit": READ_MAX},
            {"kinds": [5, 9005, EDIT_KIND], "#h": [group], "limit": READ_MAX},
            {"kinds": [9000, 9001], "#h": [group]},
        ],
    )
    found = [e for e in found if _tag(e, "h") == group]
    gk = checkin.groups_pubkey(svc)
    authors = {e["id"]: e["pubkey"] for e in found}
    gone: set[str] = set()
    for e in found:
        if e["kind"] not in (5, 9005):
            continue
        for t in e.get("tags", []):
            if t[:1] == ["e"] and len(t) > 1:
                # a moderator's removal; or the author's own (the belfry's key deletes for them)
                if e["kind"] == 9005 or e["pubkey"] in (authors.get(t[1]), gk):
                    gone.add(str(t[1]))
    # who only reads (opened a link, never joined), and who is an agent: their latest 9000
    reading: set[str] = set()
    agents: set[str] = set()
    for e in sorted(found, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        if e["kind"] not in (9000, 9001):
            continue
        for t in e.get("tags", []):
            if t[:1] != ["p"] or len(t) < 2:
                continue
            p = str(t[1])
            reading.discard(p)
            agents.discard(p)
            if e["kind"] == 9000 and _tag(e, "reading") == "1":
                reading.add(p)
            if e["kind"] == 9000 and _tag(e, "agent") == "1":
                agents.add(p)
    edits: dict[str, dict[str, Any]] = {}
    for x in sorted(found, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        if x["kind"] == EDIT_KIND and x["id"] not in gone:
            target = _tag(x, "e")
            if authors.get(target) == x["pubkey"]:  # only its author edits a message
                edits[target] = x
    written = [
        e
        for e in found
        if e["kind"] in (9, 1111)
        and e["id"] not in gone
        and e["pubkey"] not in reading
        and _plain(e)
    ]
    written.sort(key=lambda e: (e.get("created_at", 0), e.get("id", "")))
    messages = [_parse(svc, e, edits.get(e["id"])) for e in written[-READ_MAX:]]
    return {"messages": messages, "reading": reading, "agents": agents}


def snapshot(svc: Services, group: str) -> dict[str, Any]:
    """The group's messages (oldest first), who only reads it and who is an agent in it."""
    now = time.monotonic()
    with _lock:
        hit = _snaps.get(group)
        if hit and hit[0] > now:
            return hit[1]
    snap = _read(svc, group)
    with _lock:
        if len(_snaps) > 200:
            _snaps.clear()
        _snaps[group] = (time.monotonic() + CACHE_SECONDS, snap)
    return snap


# ── people ──────────────────────────────────────────────────────────────────────────────────
def people(svc: Services, keys: set[str]) -> dict[str, dict[str, Any]]:
    """name, picture and slug of each key: the name of their profile (a guest without one: the
    name on their account), never a key or an address; the slug of their profile page."""
    from . import events, sitesessions, villagers
    from .webrequests import NOT_A_NAME

    now = time.monotonic()
    out: dict[str, dict[str, Any]] = {}
    with _lock:
        for k in keys:
            hit = _people.get(k)
            if hit and hit[0] > now:
                out[k] = hit[1]
    missing = sorted(keys - set(out))
    if not missing:
        return out
    found: list[dict[str, Any]] = []
    for at in range(0, len(missing), 200):
        for read in (events.community, events.groups):
            try:
                found += read(svc, [{"kinds": [0], "authors": missing[at : at + 200]}])
            except Exception as exc:  # noqa: BLE001 — the other relay may have it
                # a relay that can't be read: 503 relay (and no "Somebody" kept for a minute)
                retries.soft(exc)
                log.info("eventchat: profiles not read: %s", type(exc).__name__)
    profiles = villagers._latest(found, 0)
    for k in missing:
        profile = profiles.get(k, {})
        try:
            acct = villagers._account_of_key(svc, k)
        except Exception as exc:  # noqa: BLE001 — a name without a page then
            log.info("eventchat: an account was not read: %s", type(exc).__name__)
            acct = None
        name = ""
        for raw in (profile.get("display_name"), profile.get("name")):
            if isinstance(raw, str) and raw.strip():
                name = " ".join(raw.split())[: sitesessions.NAME_MAX]
                break
        if not name and acct is not None and acct["role"] == "guest":
            name = acct["name"]
        if not name or NOT_A_NAME.search(name):
            name = NOBODY
        picture = profile.get("picture")
        if not (isinstance(picture, str) and sitesessions.PICTURE.match(picture)):
            picture = None
        slug = None
        if acct is not None:
            try:
                slug = villagers.slug_of(svc, acct)
            except Exception as exc:  # noqa: BLE001
                log.info("eventchat: a slug was not read: %s", type(exc).__name__)
        out[k] = {
            "name": name,
            "picture": picture and picture[: sitesessions.PICTURE_MAX],
            "slug": slug,
        }
    with _lock:
        if len(_people) > 5000:
            _people.clear()
        for k in missing:
            _people[k] = (time.monotonic() + PEOPLE_SECONDS, out[k])
    return out


class Room:
    """An event's group as one request sees it: who asks, and everyone's role."""

    def __init__(self, svc: Services, ev: Any, acct: dict[str, Any]):
        from . import checkin

        self.svc, self.ev, self.acct = svc, ev, acct
        self.st = checkin.group_state(svc, ev.group)
        self.gk = checkin.groups_pubkey(svc)
        self.known: dict[str, dict[str, Any]] = {}

    def allowed(self) -> bool:
        me = self.acct["pubkey"]
        return (
            me in self.st.members
            or me in self.st.by_role
            or me == self.ev.creator
            or me in self.ev.hosts
            or self.acct["role"] == "admin"
        )

    def role(self, pubkey: str) -> str:
        st = self.st
        if pubkey == self.ev.creator or pubkey in st.by_role:
            return "host"
        return "door" if pubkey in st.stewards else "attendee"

    def learn(self, keys: set[str]) -> None:
        self.known.update(people(self.svc, keys - set(self.known)))

    def person(self, pubkey: str) -> dict[str, Any]:
        self.learn({pubkey})
        p = self.known[pubkey]
        return {
            "name": p["name"],
            "picture": p["picture"],
            "slug": p["slug"],
            "role": self.role(pubkey),
        }

    def text(self, m: dict[str, Any]) -> str:
        """The message's text with its mentions as "@Name" and no reference to an id."""

        def named(found: re.Match) -> str:
            key = _mention_key(found.group(1) or found.group(2))
            return "@" + (self.known[key]["name"] if key in self.known else NOBODY)

        self.learn(set(m["mentions"]))
        text = MENTION.sub(named, m["text"])
        return re.sub(r"[ \t]{2,}", " ", REFERENCE.sub("", text)).strip()

    def message(self, m: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": m["id"],
            "at": m["at"],
            "by": self.person(m["pubkey"]),
            "mine": m["pubkey"] == self.acct["pubkey"],
            "text": self.text(m),
            "images": m["images"],
            "files": m["files"],
            "reply_to": m["reply_to"],
            "edited_at": m["edited_at"],
        }


def _page(rows: list[dict], before: int | None, limit: int) -> tuple[list[dict], bool]:
    """The newest LIMIT of ROWS (oldest first) older than BEFORE, and whether older ones are
    left."""
    if before is not None:
        rows = [r for r in rows if r["at"] < before]
    return rows[-limit:], len(rows) > limit


# ── the answers ─────────────────────────────────────────────────────────────────────────────
def chat(room: Room, before: int | None, limit: int) -> dict[str, Any]:
    rows, more = _page(snapshot(room.svc, room.ev.group)["messages"], before, limit)
    room.learn({m["pubkey"] for m in rows} | {k for m in rows for k in m["mentions"]})
    return {"messages": [room.message(m) for m in rows], "more": more}


def photos(room: Room, before: int | None, limit: int) -> dict[str, Any]:
    rows = [
        {"url": i["url"], "alt": i["alt"], "at": m["at"], "pubkey": m["pubkey"], "message": m["id"]}
        for m in snapshot(room.svc, room.ev.group)["messages"]
        for i in m["images"]
    ]
    rows, more = _page(rows, before, limit)
    room.learn({r["pubkey"] for r in rows})
    out = [
        {
            "url": r["url"],
            "alt": r["alt"],
            "at": r["at"],
            "by": room.person(r["pubkey"]),
            "message": r["message"],
        }
        for r in reversed(rows)
    ]
    return {"photos": out, "more": more}


def links_of(text: str) -> list[str]:
    out: list[str] = []
    for m in LINK.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]}")
        if len(url) > 10 and url not in out:
            out.append(url)
    return out


def links(room: Room) -> dict[str, Any]:
    rows = []
    for m in reversed(snapshot(room.svc, room.ev.group)["messages"]):
        attached = {x["url"] for x in [*m["images"], *m["files"]]}
        for url in links_of(room.text(m)):
            if url not in attached:
                rows.append({"url": url, "at": m["at"], "pubkey": m["pubkey"], "message": m["id"]})
        if len(rows) >= LINKS_MAX:
            break
    rows = rows[:LINKS_MAX]
    room.learn({r["pubkey"] for r in rows})
    return {
        "links": [
            {
                "url": r["url"],
                "at": r["at"],
                "by": room.person(r["pubkey"]),
                "message": r["message"],
            }
            for r in rows
        ]
    }


def participants(room: Room) -> dict[str, Any]:
    from . import community, events, whois

    svc, ev, st = room.svc, room.ev, room.st
    snap = snapshot(svc, ev.group)
    try:
        kinds = whois.kinds(svc)
    except Exception:  # noqa: BLE001 — the group's own word on agents then
        kinds = {}
    service = {room.gk, community.belfry_pubkey(svc.settings)}
    keys = {
        k
        for k in st.members | st.by_role | ({ev.creator} if ev.creator else set())
        if k not in service
        and k not in snap["reading"]
        and k not in snap["agents"]
        and kinds.get(k) != "agent"
    }
    ppl = events.people(svc, ev)
    titles = {t.d: t.title for t in events.tickets(svc, ev)}
    room.learn(keys)
    rank = {"host": 0, "door": 1, "attendee": 2}
    rows = []
    for k in keys:
        held = ppl.holders.get(k)
        rows.append(
            (
                k,
                {
                    **room.person(k),
                    "ticket_title": (titles.get(held) or "Ticket") if held else None,
                    "checked_in": bool(st.checked.get(k)),
                },
            )
        )
    rows.sort(key=lambda r: (rank[r[1]["role"]], r[1]["name"].casefold(), r[1]["slug"] or "", r[0]))
    return {"participants": [r[1] for r in rows], "count": len(rows)}


# ── writing ─────────────────────────────────────────────────────────────────────────────────
def _invalid(what: str) -> ApiError:
    return ApiError(422, "invalid", what)


def post(room: Room, data: dict[str, Any]) -> dict[str, Any]:
    from . import checkin, followers, mail, nostr, sitesessions

    svc, ev, acct = room.svc, room.ev, room.acct
    group = ev.group
    text = data.get("text", "")
    if not isinstance(text, str) or len(text.strip()) > TEXT_MAX:
        raise _invalid(f"text: at most {TEXT_MAX} characters.")
    text = text.strip()
    raw = data.get("images") or []
    if not isinstance(raw, list) or len(raw) > IMAGES_MAX:
        raise _invalid(f"images: at most {IMAGES_MAX} addresses.")
    own = re.compile(re.escape(blossom(svc)) + r"/([0-9a-f]{64})(?:\.([a-z0-9]{1,5}))?")
    images: list[tuple[str, str, str]] = []  # (address, its hash, its type)
    for url in raw:
        m = own.fullmatch(url) if isinstance(url, str) else None
        if m is None or not blossom(svc).startswith("https://"):
            raise _invalid("images: addresses of pictures on this belfry's own file server.")
        if m.group(2) and m.group(2) not in IMAGE_TYPES:
            raise _invalid("images: pictures only (jpg, png, gif, webp, avif, heic).")
        if url not in [i[0] for i in images]:
            images.append((url, m.group(1), IMAGE_TYPES.get(m.group(2) or "", "")))
    if not text and not images:
        raise _invalid(f"text: 1 to {TEXT_MAX} characters (or a picture).")
    reply_to = data.get("reply_to")
    target = None
    if reply_to is not None:
        if not (isinstance(reply_to, str) and HEX.match(reply_to)):
            raise _invalid("reply_to: a message of this conversation.")
        target = next((m for m in snapshot(svc, group)["messages"] if m["id"] == reply_to), None)
        if target is None:
            raise _invalid("reply_to: a message of this conversation.")
    if acct["pubkey"] in snapshot(svc, group)["reading"]:
        raise ApiError(403, "not_in", "Join the event first: you are only reading for now.")
    if checkin.is_archived(svc, group):
        raise ApiError(409, "archived", "This conversation is archived: nothing is added to it.")
    sign = sitesessions.signer(svc, acct)  # 409 own_key, 503 signer
    if mail._limited(f"event-chat:{acct['partner']}", POSTS_PER_HOUR, 3600):
        raise ApiError(429, "too_many", "Too many messages in a short time.")
    skip: list[str] = []
    if target is None:
        kind, tags = 9, [["h", group]]
    else:
        # NIP-22, as the web chat tags a thread reply: the thread's first message in capitals,
        # the message answered in small letters (the same one, unless that is a reply itself)
        root_id, root_kind, root_by = target["id"], str(target["kind"]), target["pubkey"]
        if target["root"] and HEX.match(target["root"][0]):
            root_id, root_kind, root_by = target["root"]
            root_kind, root_by = root_kind or "9", root_by or target["pubkey"]
        kind = 1111
        tags = [
            ["h", group],
            ["E", root_id, "", root_by],
            ["K", root_kind],
            ["P", root_by],
            ["e", target["id"], "", target["pubkey"]],
            ["k", str(target["kind"])],
            ["p", target["pubkey"]],
        ]
        skip = [target["pubkey"]]
    topics = list(dict.fromkeys(m.group(2).lower() for m in HASHTAG.finditer(text)))
    tags += [["t", t] for t in topics]
    mentioned: list[str] = []
    for m in NPUB.finditer(text):
        try:
            key = nostr.to_hex_pubkey(m.group(1))
        except ValueError:
            continue
        if key not in skip and key not in mentioned:
            mentioned.append(key)
    tags += [["p", k] for k in mentioned]
    content = text
    for url, x, kind_of in images:
        tags.append(["imeta", f"url {url}", *([f"m {kind_of}"] if kind_of else []), f"x {x}"])
        content = content + "\n" + url if content else url
    got = followers._relay(svc)
    if got is None:
        raise ApiError(503, "unavailable", "That did not work just now. Try again.")
    try:
        event = sign(
            {"kind": kind, "created_at": int(time.time()), "content": content, "tags": tags}
        )
        if event.get("pubkey") != acct["pubkey"]:
            raise RuntimeError("the signer signed for another key")
        got[0].publish(event, sign)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 — the signer or the relay
        log.warning("eventchat: a message was not posted: %s", type(exc).__name__)
        retries.check(exc)  # the relay: 503 relay
        raise ApiError(503, "unavailable", "That did not work just now. Try again.") from exc
    forget()
    return room.message(_parse(svc, event))


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def enter(svc: Services, key: str, link: str) -> Room:
    """Whoever signed with KEY, in the group of the event behind LINK: 404 ``unknown``, 403
    ``not_in``."""
    from . import events, sitesessions, villagers

    ev, _moved = events.resolve(svc, link)
    if ev is None:
        raise ApiError(404, "unknown", "No such event.")
    acted, acct = sitesessions.acting(svc, key)
    if acct is None:
        acct = villagers._account_of_key(svc, str(acted))
    room = Room(svc, ev, acct) if acct is not None else None
    if room is None or not room.allowed():
        raise ApiError(403, "not_in", "Only the people of this event read and write here.")
    return room


def _int(raw: str | None, name: str, default: int | None, low: int, high: int) -> int | None:
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw))
    except ValueError:
        raise _invalid(f"{name}: a number.") from None
    if not low <= value <= high:
        raise _invalid(f"{name}: {low} to {high}.")
    return value


def register(app: FastAPI) -> None:
    from . import events, sitesessions

    reads = events.Limiter(READS_PER_MINUTE, 60)
    app.state.event_chat_reads = reads

    async def run(fn: Any, *args: Any) -> Any:
        try:
            # a relay that can't be read or written: 503 relay (retries.py)
            return await retries.run(fn, *args)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 — the signer, the register: never a trace
            log.warning("eventchat: %s failed: %s", fn.__name__, type(exc).__name__, exc_info=True)
            raise ApiError(503, "unavailable", "That can't be read just now. Try again.") from exc

    async def room_of(request: Request, link: str, method: str, body: bytes = b"") -> Room:
        s: Services = request.app.state.services
        if not sitesessions.own(s):
            raise ApiError(409, "unsupported", "This needs the belfry's own register.")
        path = request.url.path + ("?" + request.url.query if request.url.query else "")
        auth = request.headers.get("authorization")
        if not auth:
            raise ApiError(401, "unauthorized", "Sign the request (NIP-98).")
        key = authenticate(s, request.app.state.api_seen, auth, method, path, body)
        room = await run(enter, s, key, link)
        if method == "GET" and not reads.allow(f"p:{room.acct['partner']}"):
            raise ApiError(429, "too_many", "Too many requests for now. Try again in a minute.")
        return room

    def paging(request: Request, default: int) -> tuple[int | None, int]:
        q = request.query_params
        before = _int(q.get("before"), "before", None, 0, 2**40)
        return before, int(_int(q.get("limit"), "limit", default, 1, LIMIT_MAX) or default)

    @app.get(API + "/{link}/chat")
    async def event_chat(request: Request, link: str) -> JSONResponse:
        before, limit = paging(request, LIMIT_DEFAULT)
        room = await room_of(request, link, "GET")
        return JSONResponse(await run(chat, room, before, limit), headers=NO_STORE)

    @app.post(API + "/{link}/chat")
    async def event_chat_post(request: Request, link: str) -> JSONResponse:
        body = await request.body()
        room = await room_of(request, link, "POST", body)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        cid = retries.client_id(data)  # a retry is answered, not posted twice (retries.py)

        async def make() -> JSONResponse:
            return JSONResponse(await run(post, room, data), status_code=201, headers=NO_STORE)

        return await retries.once(request, room.acct["pubkey"], cid, make)

    @app.get(API + "/{link}/photos")
    async def event_photos(request: Request, link: str) -> JSONResponse:
        before, limit = paging(request, PHOTOS_DEFAULT)
        room = await room_of(request, link, "GET")
        return JSONResponse(await run(photos, room, before, limit), headers=NO_STORE)

    @app.get(API + "/{link}/links")
    async def event_links(request: Request, link: str) -> JSONResponse:
        room = await room_of(request, link, "GET")
        return JSONResponse(await run(links, room), headers=NO_STORE)

    @app.get(API + "/{link}/participants")
    async def event_participants(request: Request, link: str) -> JSONResponse:
        room = await room_of(request, link, "GET")
        return JSONResponse(await run(participants, room), headers=NO_STORE)


__all__ = ["register", "forget", "forget_people", "snapshot"]
