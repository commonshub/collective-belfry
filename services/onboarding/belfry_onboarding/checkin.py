"""Roles in a group, tickets with a QR code, and checking people in (docs/checkin.md).

Xavier, 2026-11-15: a ticket's QR opens the event; the event's admins (host, co-hosts) and its
stewards check people in, by scanning or from the list.

Roles per group (a channel, a group, an event's ``ev-<d>``):

- ``admin``: the relay's 39001 (the relay owner, the community's admins through zooid patch 0003,
  the group's creator) plus everyone put in with the admin role: the latest 9000/9001 for that key
  in the group is a 9000 whose ``p`` tag says ``admin`` (co-host links do this, guests.py). A 9000
  tagged ``["role", "member"]`` or a 9001 ends it; a ticket's 9000 (no ``role`` tag) does not.
  zooid itself has no per-group admins (its 39001 never lists them, and it refuses their
  moderation events), so whatever such an admin does on the relay goes through the belfry, signed
  by the groups key (events.decide, the check-in below).
- ``steward``: events only. Kind 1985, ``L`` ``belfry/group-role``, ``l`` ``steward`` (``none``
  clears it), ``p``, ``h``, ``by``, by the groups key; the latest per ``p`` wins.
- ``member``: everyone else.

Tickets: everyone holding one (a 9000 with a ``ticket`` tag; on an event with only the implicit
free ticket, a member's own RSVP "going" counts too) has a code, 16 characters of lowercase
base32 of HMAC-SHA256(secret, "belfry-ticket:<d>:<pubkey>")[:10]. The QR holds
``https://chat.<domain>/events/<link>/t/<code>``.

Check-in: kind 1985, ``L`` ``belfry/ticket``, ``l`` ``checked-in`` / ``not-checked-in``, ``p``,
``h``, ``by``, by the groups key; the latest per ``p`` wins.

API (belfry.<domain>/api/v1/…, NIP-98): ``POST /group-roles``, ``POST /events/<d>/attendees``,
``POST /events/<d>/checkin``; and new fields in ``GET /events/<link>``'s ``me`` (also sent as
``viewer``): ``ticket_code``, ``ticket_url``, ``checked_in``, ``roles``, ``may_check_in``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import events
from .core import Services

ROLE_NS = "belfry/group-role"
TICKET_NS = "belfry/ticket"
ROLES = ("admin", "steward", "member", "removed")
GROUP_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
CODE_RE = re.compile(r"^[a-z2-7]{16}$")
PATH = "/api/group-roles"


def is_event_group(group: str) -> bool:
    return group.startswith(("ev-", "ws-"))


def groups_pubkey(svc: Services) -> str:
    """The groups key's pubkey: only its labels count."""
    key = getattr(getattr(svc, "groups", None), "key", None)
    return key.public_key_xonly.format().hex() if key is not None else ""


def role_admins(found: list[dict[str, Any]]) -> set[str]:
    """Keys put in GROUP with the admin role and not taken out since (9000/9001 of one group)."""
    state: dict[str, bool] = {}
    for e in sorted(found, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        k = e.get("kind")
        if k not in (9000, 9001):
            continue
        for t in e.get("tags", []):
            if t[:1] != ["p"] or len(t) < 2:
                continue
            p = str(t[1])
            if k == 9001:
                state[p] = False
            elif len(t) > 2 and t[2] == "admin":
                state[p] = True
            elif events.tag(e, "role") == "member":
                state[p] = False
    return {p for p, on in state.items() if on}


def latest_labels(found: list[dict[str, Any]], ns: str, signer: str) -> dict[str, tuple[str, int]]:
    """Per ``p``: the latest label of NS by SIGNER → (value, created_at)."""
    out: dict[str, tuple[str, int]] = {}
    for e in sorted(found, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        if e.get("kind") != 1985 or not signer or e.get("pubkey") != signer:
            continue
        if events.tag(e, "L") != ns:
            continue
        value = next(
            (t[1] for t in e.get("tags", []) if t[:1] == ["l"] and len(t) > 2 and t[2] == ns),
            None,
        )
        p = events.tag(e, "p")
        if value and p:
            out[p] = (str(value), int(e.get("created_at", 0)))
    return out


@dataclass
class GroupState:
    group: str
    relay_admins: set[str]  # 39001
    creator: str | None
    by_role: set[str]  # put in with the admin role
    stewards: set[str]
    members: set[str]  # 39002, plus anyone whose latest 9000/9001 is a 9000
    checked: dict[str, int] = field(default_factory=dict)  # pubkey → checked in at

    @property
    def admins(self) -> set[str]:
        return self.relay_admins | self.by_role | ({self.creator} if self.creator else set())


def group_state(svc: Services, group: str) -> GroupState:
    found = events.groups(
        svc,
        [
            {"kinds": [39001, 39002], "#d": [group]},
            {"kinds": [9007, 9000, 9001], "#h": [group]},
            {"kinds": [1985], "#h": [group], "#L": [ROLE_NS, TICKET_NS]},
        ],
    )
    relay_admins: set[str] = set()
    listed: set[str] = set()
    latest_list: dict[int, int] = {}
    for e in found:
        k = e.get("kind")
        if k in (39001, 39002) and events.tag(e, "d") == group:
            if e.get("created_at", 0) < latest_list.get(k, 0):
                continue
            latest_list[k] = e.get("created_at", 0)
            keys = {str(t[1]) for t in e.get("tags", []) if t[:1] == ["p"] and len(t) > 1}
            if k == 39001:
                relay_admins = keys
            else:
                listed = keys
    made = sorted(
        (e for e in found if e.get("kind") == 9007 and events.tag(e, "h") == group),
        key=lambda e: e["created_at"],
    )
    put: dict[str, bool] = {}
    for e in sorted(found, key=lambda e: e.get("created_at", 0)):
        if e.get("kind") in (9000, 9001) and events.tag(e, "h") == group:
            p = events.tag(e, "p")
            if p:
                put[p] = e["kind"] == 9000
    gk = groups_pubkey(svc)
    in_group = [e for e in found if events.tag(e, "h") == group]
    roles = latest_labels(in_group, ROLE_NS, gk)
    checks = latest_labels(in_group, TICKET_NS, gk)
    return GroupState(
        group=group,
        relay_admins=relay_admins,
        creator=made[0]["pubkey"] if made else None,
        by_role=role_admins(in_group),
        stewards={p for p, (v, _) in roles.items() if v == "steward"}
        if is_event_group(group)
        else set(),
        members=listed | {p for p, on in put.items() if on},
        checked={p: at for p, (v, at) in checks.items() if v == "checked-in"},
    )


def _belfry_admins(svc: Services) -> set[str]:
    from . import apps

    try:
        return set(apps.admin_pubkeys(svc))
    except Exception:  # noqa: BLE001 — without the list, only the group's own admins
        return set()


def viewer_roles(
    svc: Services, st: GroupState, pubkey: str, creator: str | None = None
) -> list[str]:
    """Of host / cohost / steward. Host = the creator; co-host = any other admin of the group
    except the community's admins and the relay's own keys (they administer every group)."""
    creator = creator or st.creator
    out = []
    if pubkey == creator:
        out.append("host")
    elif pubkey in st.by_role or (
        pubkey in st.relay_admins
        and pubkey not in _belfry_admins(svc)
        and pubkey != groups_pubkey(svc)
    ):
        out.append("cohost")
    if pubkey in st.stewards and not out:
        out.append("steward")
    return out


def may_check_in(svc: Services, st: GroupState, pubkey: str, creator: str | None = None) -> bool:
    if not pubkey:
        return False
    return (
        bool(viewer_roles(svc, st, pubkey, creator))
        or pubkey in st.admins
        or pubkey in _belfry_admins(svc)
    )


# ── archived groups (docs/archive.md) ────────────────────────────────────────────────────────
ARCHIVED = "archived"
ALWAYS_OPEN = "general"  # the one channel every belfry keeps open


def archivable(group: str) -> bool:
    """Channels and groups (``pod-…``). Events and proposals have their own cancel / status."""
    return not group.startswith(("ev-", "ws-", "prop-"))


def is_archived(svc: Services, group: str) -> bool:
    """Whether GROUP is archived: the latest of its 9002s (and the relay's 39000 mirror of the
    last one) that carries an ``archived`` tag says ``"1"``. The clients read it the same way, so
    an edit by a client that doesn't know the tag (its 9002 has none) changes nothing. False when
    the relay can't be asked: nothing is refused on a guess."""
    try:
        found = events.groups(
            svc,
            [{"kinds": [39000], "#d": [group]}, {"kinds": [9007, 9002], "#h": [group]}],
        )
    except Exception as exc:  # noqa: BLE001 — the relay not answering
        from . import retries

        retries.soft(exc)  # the website's calls: 503 relay
        return False
    value = None
    for e in sorted(found, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        k = e.get("kind")
        if events.tag(e, "d" if k == 39000 else "h") != group or k not in (39000, 9007, 9002):
            continue
        v = events.tag(e, ARCHIVED)
        if v is not None:
            value = v
    return value == "1"


def refuse_archived(svc: Services, group: str) -> None:
    """409 ``archived``: nothing changes in an archived group but unarchiving it."""
    from .holder_invites import ApiError

    if is_archived(svc, group):
        raise ApiError(409, "archived", "This group is archived. Unarchive it first.")


# ── changing a role ─────────────────────────────────────────────────────────────────────────
def _label(svc: Services, ns: str, value: str, pubkey: str, group: str, caller: str) -> bool:
    return events.publish(
        svc,
        1985,
        [["L", ns], ["l", value, ns], ["p", pubkey], ["h", group], ["by", caller]],
    )


def set_role(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from . import guests
    from .holder_invites import ApiError

    group = str(data.get("channel") or "").strip()
    target = str(data.get("pubkey") or "").strip().lower()
    role = data.get("role")
    if not GROUP_RE.match(group) or not HEX.match(target) or role not in ROLES:
        raise ApiError(
            422,
            "invalid",
            'Send {"channel", "pubkey", "role": "admin" | "steward" | "member" | "removed"}.',
        )
    st = group_state(svc, group)
    belfry_admins = _belfry_admins(svc)
    if caller not in st.admins and caller not in belfry_admins:
        raise ApiError(403, "not_allowed", "Only the group's admins change roles.")
    refuse_archived(svc, group)
    if role == "steward" and not is_event_group(group):
        raise ApiError(409, "events_only", "Stewards are for events.")
    inside = target in st.members or target in st.admins
    if not inside and group.startswith("ev-") and events.D_RE.match(group[3:]):
        ev = events.load(svc, group[3:])
        if ev is not None:
            ppl = events.people(svc, ev)
            inside = target in ppl.holders or ppl.rsvps.get(target) in ("accepted", "tentative")
    if not inside:
        raise ApiError(409, "not_in_group", "This person is not in the group.")
    always = st.relay_admins | ({st.creator} if st.creator else set()) | belfry_admins
    was_admin = target in st.admins
    was_steward = target in st.stewards
    relay_fail = ApiError(503, "relay", "The group could not be reached. Try again.")
    if role == "admin":
        if guests._is_agent(svc, target):
            raise ApiError(409, "agent", "An agent can't be an admin.")
        if not guests._saved_email(svc, target):
            raise ApiError(409, "guest_not_saved", "This person has no e-mail address yet.")
        if not was_admin and not events.publish(
            svc,
            9000,
            [["h", group], ["p", target, "admin"], ["role", "admin"], ["by", caller]],
        ):
            raise relay_fail
        if was_steward and not _label(svc, ROLE_NS, "none", target, group, caller):
            raise relay_fail
        return {"role": "admin"}
    if role == "removed":
        # out of the group (a 9001 by the groups key; for an event that also takes the ticket
        # back, as a host's removal does): co-hosts can't publish a 9001 on the relay themselves
        if target in always:
            raise ApiError(409, "always_admin", "The creator and the belfry's admins stay admins.")
        if was_admin and not (st.admins - {target}):
            raise ApiError(409, "last_admin", "A group keeps at least one admin.")
        if not events.publish(svc, 9001, [["h", group], ["p", target], ["by", caller]]):
            raise relay_fail
        if was_steward and not _label(svc, ROLE_NS, "none", target, group, caller):
            raise relay_fail
        return {"role": "removed"}
    # member or steward
    if was_admin:
        if target in always:
            raise ApiError(409, "always_admin", "The creator and the belfry's admins stay admins.")
        if not (st.admins - {target}):
            raise ApiError(409, "last_admin", "A group keeps at least one admin.")
        if not events.publish(
            svc,
            9000,
            [["h", group], ["p", target, "member"], ["role", "member"], ["by", caller]],
        ):
            raise relay_fail
    if role == "steward" and not was_steward:
        if not _label(svc, ROLE_NS, "steward", target, group, caller):
            raise relay_fail
    if role == "member" and was_steward:
        if not _label(svc, ROLE_NS, "none", target, group, caller):
            raise relay_fail
    return {"role": role}


# ── requests to join a closed channel or group, answered by any of its admins ───────────────
REQUESTS_PATH = "/api/group-requests/decide"


def decide_request(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    """``{"channel", "pubkey", "action": "approve" | "decline"}`` by an admin of the channel (by
    the relay's list, as its creator, or by role) or a belfry admin: the request (the person's
    latest unanswered 9021) is answered by the groups key, naming who decided. The relay takes
    a 9000 / 9001 only from the few admins it knows, so an admin by role decides through here."""
    from .holder_invites import ApiError

    group = str(data.get("channel") or "").strip()
    target = str(data.get("pubkey") or "").strip().lower()
    action = data.get("action")
    if not GROUP_RE.match(group) or not HEX.match(target) or action not in ("approve", "decline"):
        raise ApiError(
            422, "invalid", 'Send {"channel", "pubkey", "action": "approve" | "decline"}.'
        )
    if is_event_group(group):
        raise ApiError(409, "event", "An event's requests are answered on the event.")
    st = group_state(svc, group)
    if caller not in st.admins and caller not in _belfry_admins(svc):
        raise ApiError(403, "not_allowed", "Only the admins of this channel answer its requests.")
    refuse_archived(svc, group)
    found = events.groups(
        svc,
        [
            {"kinds": [9021, 9022], "#h": [group], "authors": [target]},
            {"kinds": [9000, 9001], "#h": [group], "#p": [target]},
        ],
    )
    found = [e for e in found if events.tag(e, "h") == group]
    asks = [e for e in found if e.get("kind") == 9021 and e.get("pubkey") == target]
    if not asks:
        if target in st.members and action == "approve":
            return {"already": True}
        raise ApiError(409, "no_request", "This person has no open request here.")
    ask = max(asks, key=lambda e: e.get("created_at", 0))
    after = [
        e
        for e in found
        if e is not ask
        and e.get("created_at", 0) >= ask.get("created_at", 0)
        and (
            (e.get("kind") == 9022 and e.get("pubkey") == target)
            or (e.get("kind") in (9000, 9001) and events.tag(e, "p") == target)
        )
    ]
    if after:
        last = max(after, key=lambda e: e.get("created_at", 0))
        if last.get("kind") == 9000 and action == "approve":
            return {"already": True}
        raise ApiError(409, "no_request", "This person has no open request here.")
    relay_fail = ApiError(503, "relay", "The group could not be reached. Try again.")
    if action == "decline":
        tags = [["h", group], ["p", target], ["by", caller], ["e", ask["id"]]]
        if not events.publish(svc, 9001, tags):
            raise relay_fail
        return {"declined": True}
    tags = [["h", group], ["p", target, "member"], ["by", caller], ["e", ask["id"]]]
    if not events.publish(svc, 9000, tags):
        raise relay_fail
    return {"approved": True}


# ── a group's name and description, by any of its admins ────────────────────────────────
META_PATH = "/api/group-meta"
FLAGS = ("closed", "open", "private", "public", "hidden", "restricted")


def current_meta(svc: Services, group: str) -> list[list[str]] | None:
    """The group's metadata as the relay has it (its latest 39000), as 9002 tags (no ``h``)."""
    found = [
        e
        for e in events.groups(svc, [{"kinds": [39000], "#d": [group]}])
        if e.get("kind") == 39000 and events.tag(e, "d") == group
    ]
    if not found:
        return None
    latest = max(found, key=lambda e: e.get("created_at", 0))
    return [list(map(str, t)) for t in latest.get("tags", []) if t and t[0] not in ("d", "-")]


def set_meta(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    """``{"channel", "name"?, "about"?, "picture"?, "closed"?, "prompt"?, "vouches"?,
    "archived"?}``: a new 9002 by the groups key with every other tag of the current metadata kept
    (zooid replaces the whole metadata with each 9002). For admins by role, whose own 9002 the
    relay refuses. ``archived`` (true / false) archives or unarchives the group (docs/archive.md):
    while it is archived only ``{"channel", "archived": false}`` passes, anything else is
    ``409 archived``; ``general`` is never archived (``409 always_open``)."""
    from .holder_invites import ApiError

    group = str(data.get("channel") or "").strip()
    if not GROUP_RE.match(group):
        raise ApiError(422, "invalid", 'Send {"channel", "name"?, "about"?}.')
    if ARCHIVED in data and not isinstance(data[ARCHIVED], bool):
        raise ApiError(422, "invalid", "archived: true or false.")
    st = group_state(svc, group)
    if caller not in st.admins and caller not in _belfry_admins(svc):
        raise ApiError(403, "not_allowed", "Only the group's admins change it.")
    tags = current_meta(svc, group)
    if tags is None:
        raise ApiError(404, "not_found", "No such group.")
    if data.get(ARCHIVED) is True:
        if group == ALWAYS_OPEN:
            raise ApiError(409, "always_open", "This channel can't be archived.")
        if not archivable(group):
            raise ApiError(422, "invalid", "Events and proposals can't be archived.")
    was_archived = is_archived(svc, group)
    others = [k for k in ("name", "about", "picture", "closed", "prompt", "vouches") if k in data]
    if was_archived and (others or data.get(ARCHIVED) is not False):
        raise ApiError(409, "archived", "This group is archived. Unarchive it first.")

    def put(name: str, value: str | None) -> None:
        nonlocal tags
        tags = [t for t in tags if t[0] != name]
        if value:
            tags.append([name, value])

    if "name" in data:
        name = " ".join(str(data["name"] or "").split())[:100]
        if not name:
            raise ApiError(422, "invalid", "A name, please.")
        put("name", name)
    if "about" in data:
        put("about", str(data["about"] or "")[:2000])
    if "picture" in data:
        pic = str(data["picture"] or "")
        if pic and not pic.startswith("https://"):
            raise ApiError(422, "invalid", "picture: an https:// URL.")
        put("picture", pic[:500])
    if "closed" in data:
        if not isinstance(data["closed"], bool):
            raise ApiError(422, "invalid", "closed: true or false.")
        tags = [t for t in tags if t[0] not in ("closed", "open")]
        tags.append(["closed"] if data["closed"] else ["open"])
    if "prompt" in data:
        put("prompt", str(data["prompt"] or "")[:500])
    if "vouches" in data:
        try:
            n = int(data["vouches"] or 0)
        except (TypeError, ValueError):
            n = -1
        if not 0 <= n <= 5:
            raise ApiError(422, "invalid", "vouches: 0 to 5.")
        put("vouches", str(n) if n else None)
    if ARCHIVED in data:
        # always said, "0" too: the latest 9002 that says anything about it wins
        put(ARCHIVED, "1" if data[ARCHIVED] else "0")
    archived = data[ARCHIVED] if ARCHIVED in data else was_archived
    if not events.publish(svc, 9002, [["h", group], *tags]):
        raise ApiError(503, "relay", "The group could not be reached. Try again.")
    meta = {t[0]: (t[1] if len(t) > 1 else True) for t in tags}
    return {
        "channel": group,
        "name": meta.get("name", ""),
        "about": meta.get("about", ""),
        "closed": "closed" in meta,
        "archived": archived,
    }


# ── deleting an empty channel (Xavier, 2026-11-17: Settings → Channels deletes a channel nobody
# wrote in, archives one with messages) ─────────────────────────────────────────────────────────
DELETE_PATH = "/api/group-delete"
# what people post in a group: an empty group has none of these (its own metadata, roles, web
# setting, labels and reactions don't count)
CONTENT_KINDS = [9, 10, 11, 12, 20, 1063, 1068, 1111, 9802, 30023, 31922, 31923]


def is_empty(svc: Services, group: str) -> bool:
    """Whether nobody posted in GROUP. A relay that does not answer raises: nothing is deleted on
    a guess (and without a relay the 9008 can't be published either)."""
    found = events.groups(svc, [{"kinds": CONTENT_KINDS, "#h": [group], "limit": 1}])
    return not any(events.tag(e, "h") == group for e in found)


def delete_group(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    """``{"channel"}``: a 9008 by the groups key, for the group's admins and the belfry's. Only an
    empty channel (409 ``not_empty``: archive it instead); never #general (409 ``always_open``),
    nor events and proposals."""
    from .holder_invites import ApiError

    group = str(data.get("channel") or "").strip()
    if not GROUP_RE.match(group):
        raise ApiError(422, "invalid", 'Send {"channel"}.')
    if group == ALWAYS_OPEN:
        raise ApiError(409, "always_open", "This channel can't be deleted.")
    if not archivable(group):
        raise ApiError(422, "invalid", "Events and proposals can't be deleted here.")
    st = group_state(svc, group)
    if caller not in st.admins and caller not in _belfry_admins(svc):
        raise ApiError(403, "not_allowed", "Only the group's admins delete it.")
    if current_meta(svc, group) is None:
        raise ApiError(404, "not_found", "No such group.")
    if not is_empty(svc, group):
        raise ApiError(409, "not_empty", "People wrote in this channel: archive it instead.")
    if not events.publish(svc, 9008, [["h", group]]):
        raise ApiError(503, "relay", "The group could not be reached. Try again.")
    logging.getLogger(__name__).info("group-delete: %s deleted by %s", group, caller[:12])
    return {"channel": group, "deleted": True}


# ── tickets ─────────────────────────────────────────────────────────────────────────────────
def ticket_code(svc: Services, d: str, pubkey: str) -> str:
    secret = (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()
    mac = hmac.new(secret, f"belfry-ticket:{d}:{pubkey}".encode(), hashlib.sha256).digest()
    return base64.b32encode(mac[:10]).decode().lower()


def ticket_url(svc: Services, ev: events.Event, code: str) -> str:
    return f"https://chat.{events.domain(svc)}/events/{events.link_of(svc, ev)}/t/{code}"


def holders(ev: events.Event, ppl: events.People, tks: list[events.Ticket]) -> dict[str, str]:
    """Who has a ticket → its ``d``: the 9000s, and on an event with only the implicit free
    ticket, everyone whose own RSVP says going."""
    out = dict(ppl.holders)
    if len(tks) == 1 and tks[0].implicit:
        for p, s in ppl.rsvps.items():
            if s == "accepted" and p not in out:
                out[p] = tks[0].d
    return out


def viewer_fields(
    svc: Services, ev: events.Event, viewer: str, ppl: events.People
) -> dict[str, Any]:
    """The check-in fields of ``GET /events/<link>``'s viewer block."""
    st = group_state(svc, ev.group)
    tks = events.tickets(svc, ev)
    mine = holders(ev, ppl, tks)
    code = ticket_code(svc, ev.d, viewer) if viewer in mine else None
    held = ppl.holders.get(viewer)
    title = next((t.title for t in tks if t.d == held), "Ticket") if held else None
    return {
        "ticket_title": title,
        "ticket_code": code,
        "ticket_url": ticket_url(svc, ev, code) if code else None,
        "checked_in": st.checked.get(viewer) if viewer in mine else None,
        "roles": viewer_roles(svc, st, viewer, ev.creator),
        "may_check_in": may_check_in(svc, st, viewer, ev.creator),
    }


def _person(
    svc: Services,
    ev: events.Event,
    st: GroupState,
    ppl: events.People,
    tks: dict[str, events.Ticket],
    pubkey: str,
    ticket_d: str,
    name: str,
) -> dict[str, Any]:
    t = tks.get(ticket_d)
    roles = viewer_roles(svc, st, pubkey, ev.creator)
    return {
        "pubkey": pubkey,
        "name": name,
        "ticket": ticket_d,
        "ticket_title": t.title if t else "",
        "rsvp": ppl.rsvps.get(pubkey, "accepted"),
        "checked_in": st.checked.get(pubkey),
        "role": roles[0] if roles else "member",
    }


def _check_allowed(svc: Services, ev: events.Event, caller: str) -> GroupState:
    from .holder_invites import ApiError

    st = group_state(svc, ev.group)
    if not may_check_in(svc, st, caller, ev.creator):
        raise ApiError(403, "not_allowed", "Only the event's hosts and stewards check people in.")
    return st


def attendees(svc: Services, ev: events.Event, caller: str) -> dict[str, Any]:
    st = _check_allowed(svc, ev, caller)
    ppl = events.people(svc, ev)
    tks = events.tickets(svc, ev)
    have = holders(ev, ppl, tks)
    by_d = {t.d: t for t in tks}
    names = events.names(svc, sorted(have))
    people = [_person(svc, ev, st, ppl, by_d, p, d, names.get(p, "")) for p, d in have.items()]
    people.sort(key=lambda x: ((x["name"] or "~").casefold(), x["pubkey"]))
    return {
        "people": people,
        "going": sum(1 for x in people if x["rsvp"] == "accepted"),
        "checked_in": sum(1 for x in people if x["checked_in"]),
    }


def checkin(svc: Services, ev: events.Event, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    st = _check_allowed(svc, ev, caller)
    code = data.get("code")
    pubkey = data.get("pubkey")
    undo = data.get("undo") is True
    if (code is None) == (pubkey is None):
        raise ApiError(422, "invalid", 'Send {"code"} or {"pubkey"}, and "undo": true to undo.')
    ppl = events.people(svc, ev)
    tks = events.tickets(svc, ev)
    have = holders(ev, ppl, tks)
    who = None
    if code is not None:
        code = str(code).strip().lower()
        if CODE_RE.match(code):
            who = next(
                (p for p in have if hmac.compare_digest(ticket_code(svc, ev.d, p), code)), None
            )
    else:
        p = str(pubkey).strip().lower()
        who = p if HEX.match(p) and p in have else None
    if who is None:
        raise ApiError(404, "no_ticket", "This is not a ticket for this event.")
    before = st.checked.get(who)
    if data.get("peek") is True:  # who it is and where they stand, nothing recorded
        name = events.names(svc, [who]).get(who, "")
        person = _person(svc, ev, st, ppl, {t.d: t for t in tks}, who, have[who], name)
        return {"person": person, "already": before is not None}
    already = (before is None) if undo else (before is not None)
    if not already:
        value = "not-checked-in" if undo else "checked-in"
        if not _label(svc, TICKET_NS, value, who, ev.group, caller):
            raise ApiError(503, "relay", "The event could not be reached. Try again.")
        if undo:
            st.checked.pop(who, None)
        else:
            st.checked[who] = int(time.time())
    name = events.names(svc, [who]).get(who, "")
    person = _person(svc, ev, st, ppl, {t.d: t for t in tks}, who, have[who], name)
    return {"person": person, "already": already}


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    async def signed(request: Request, path: str) -> tuple[Services, str, dict[str, Any]]:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", path, body
        )
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return s, caller, data

    async def event_of(s: Services, d: str) -> events.Event:
        ev = await run_in_threadpool(events.load, s, d)
        if ev is None:
            raise ApiError(404, "not_found", "No such event.")
        return ev

    @app.post(PATH)
    async def group_roles(request: Request) -> JSONResponse:
        s, caller, data = await signed(request, PATH)
        out = await run_in_threadpool(set_role, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(REQUESTS_PATH)
    async def group_request_decide(request: Request) -> JSONResponse:
        s, caller, data = await signed(request, REQUESTS_PATH)
        out = await run_in_threadpool(decide_request, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(META_PATH)
    async def group_meta(request: Request) -> JSONResponse:
        s, caller, data = await signed(request, META_PATH)
        out = await run_in_threadpool(set_meta, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(DELETE_PATH)
    async def group_delete(request: Request) -> JSONResponse:
        s, caller, data = await signed(request, DELETE_PATH)
        out = await run_in_threadpool(delete_group, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(events.API + "/{d}/attendees")
    async def event_attendees(request: Request, d: str) -> JSONResponse:
        s, caller, _data = await signed(request, f"{events.API}/{d}/attendees")
        ev = await event_of(s, d)
        out = await run_in_threadpool(attendees, s, ev, caller)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post(events.API + "/{d}/checkin")
    async def event_checkin(request: Request, d: str) -> JSONResponse:
        s, caller, data = await signed(request, f"{events.API}/{d}/checkin")
        ev = await event_of(s, d)
        out = await run_in_threadpool(checkin, s, ev, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})


__all__ = [
    "register",
    "set_role",
    "set_meta",
    "attendees",
    "checkin",
    "ticket_code",
    "group_state",
    "is_archived",
    "refuse_archived",
]
