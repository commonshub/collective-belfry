"""Roles in a group, ticket codes, the attendee list and checking people in (checkin.py)."""
# ruff: noqa: F811 — the fixtures come from test_events

from __future__ import annotations

import json
import re
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, checkin, events, guests
from belfry_onboarding.app import create_app

from .test_events import (  # noqa: F401 — fixtures
    GROUP,
    HOST,
    D,
    World,
    call,
    make_event,
    member,
    pk,
    rsvc,
    ticket,
    world,
)
from .test_holder_invites import nip98


@pytest.fixture
def cw(world: World, monkeypatch) -> World:
    monkeypatch.setattr(checkin, "groups_pubkey", lambda svc: pk(world.gkey))
    world.belfry_admins = set()  # type: ignore[attr-defined]
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: world.belfry_admins)
    return world


def roles_post(c, key, data):
    body = json.dumps(data).encode()
    time.sleep(1.05)
    return c.post(
        "/api/v1/group-roles",
        content=body,
        headers={
            **HOST,
            "Authorization": nip98(
                key, "https://belfry.belfry.test/api/v1/group-roles", "POST", body
            ),
        },
    )


def hold(w: World, key: PrivateKey, tid: str = "std") -> None:
    w.put(w.gkey, 9000, [["h", GROUP], ["p", pk(key), "member"], ["ticket", f"{D}:{tid}"]])


def test_ticket_codes_are_stable_and_per_event(rsvc):
    a = checkin.ticket_code(rsvc, D, "a" * 64)
    assert re.fullmatch(r"[a-z2-7]{16}", a)
    assert a == checkin.ticket_code(rsvc, D, "a" * 64)
    assert a != checkin.ticket_code(rsvc, "2026-zzzzzz", "a" * 64)
    assert a != checkin.ticket_code(rsvc, D, "b" * 64)


def test_viewer_block_ticket_code_roles(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    make_event(cw, ada)
    ticket(cw, ada, "std", "Standard")
    hold(cw, bea)
    with TestClient(create_app(rsvc)) as c:
        me = call(c, bea, "GET", f"/{D}").json()["viewer"]
        code = checkin.ticket_code(rsvc, D, pk(bea))
        assert me["ticket_code"] == code
        assert me["ticket_url"] == f"https://chat.belfry.test/events/{D}/t/{code}"
        assert me["checked_in"] is None and me["roles"] == [] and me["may_check_in"] is False
        host = call(c, ada, "GET", f"/{D}").json()
        assert host["me"]["roles"] == ["host"] and host["me"]["may_check_in"] is True
        assert host["me"]["ticket_code"] is None  # the host holds no ticket
        out = call(c, cy, "GET", f"/{D}").json()["viewer"]
        assert out["ticket_code"] is None and out["may_check_in"] is False


def test_implicit_ticket_rsvp_going_has_a_code(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    ev = make_event(cw, ada)
    coord = f"31923:{pk(ada)}:{D}"
    cw.put(bea, 31925, [["a", coord], ["e", ev["id"]], ["status", "accepted"], ["d", coord]])
    with TestClient(create_app(rsvc)) as c:
        me = call(c, bea, "GET", f"/{D}").json()["me"]
        assert me["ticket_code"] == checkin.ticket_code(rsvc, D, pk(bea))
        got = call(c, ada, "POST", f"/{D}/attendees", {}).json()
        assert [p["pubkey"] for p in got["people"]] == [pk(bea)]
        assert got["people"][0]["ticket"] == f"{D}:free" and got["going"] == 1


def test_group_roles_steward_cohost_and_errors(rsvc, cw, keycast, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    out = member(rsvc, "out@belfry.test", "Out")
    make_event(cw, ada)
    ticket(cw, ada, "std", "Standard")
    hold(cw, bea)
    hold(cw, cy)
    with TestClient(create_app(rsvc)) as c:
        # an attendee can't change roles; nor can an outsider
        r = roles_post(c, bea, {"channel": GROUP, "pubkey": pk(cy), "role": "steward"})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(out), "role": "steward"})
        assert r.status_code == 409 and r.json()["error"] == "not_in_group"
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(cy), "role": "boss"})
        assert r.status_code == 422
        # steward: a label by the groups key
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(cy), "role": "steward"})
        assert r.status_code == 200 and r.json() == {"role": "steward"}
        lab = cw.grp.events[-1]
        assert lab["kind"] == 1985 and lab["pubkey"] == pk(cw.gkey)
        assert ["l", "steward", "belfry/group-role"] in lab["tags"]
        assert ["by", pk(ada)] in lab["tags"] and ["p", pk(cy)] in lab["tags"]
        me = call(c, cy, "GET", f"/{D}").json()["me"]
        assert me["roles"] == ["steward"] and me["may_check_in"] is True
        # a steward can check in but not change roles
        r = roles_post(c, cy, {"channel": GROUP, "pubkey": pk(bea), "role": "steward"})
        assert r.status_code == 403
        # a label by someone else than the groups key counts for nothing
        cw.put(
            bea,
            1985,
            [
                ["L", "belfry/group-role"],
                ["l", "steward", "belfry/group-role"],
                ["p", pk(bea)],
                ["h", GROUP],
            ],
        )
        assert call(c, bea, "GET", f"/{D}").json()["me"]["may_check_in"] is False
        # co-host: a 9000 with the admin role; steward cleared
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(cy), "role": "admin"})
        assert r.json() == {"role": "admin"}
        put = [e for e in cw.grp.events if e["kind"] == 9000][-1]
        assert ["p", pk(cy), "admin"] in put["tags"]
        assert ["l", "none", "belfry/group-role"] in cw.grp.events[-1]["tags"]
        assert pk(cy) in events.load(rsvc, D).hosts
        me = call(c, cy, "GET", f"/{D}").json()["me"]
        assert me["roles"] == ["cohost"] and me["host"] is True
        # the co-host may now change roles too
        r = roles_post(c, cy, {"channel": GROUP, "pubkey": pk(bea), "role": "steward"})
        assert r.status_code == 200
        # back to attendee: a 9000 tagged role member; a ticket's 9000 never demotes
        hold(cw, cy)
        assert pk(cy) in events.load(rsvc, D).hosts
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(cy), "role": "member"})
        assert r.json() == {"role": "member"}
        assert ["role", "member"] in cw.grp.events[-1]["tags"]
        assert pk(cy) not in events.load(rsvc, D).hosts
        # the creator stays host
        r = roles_post(c, cy, {"channel": GROUP, "pubkey": pk(ada), "role": "member"})
        assert r.status_code == 403
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(ada), "role": "member"})
        assert r.status_code == 409 and r.json()["error"] == "always_admin"
        # stewards are for events only
        cw.put(ada, 9007, [["h", "garden"]])
        cw.put(cw.gkey, 9000, [["h", "garden"], ["p", pk(bea)]])
        r = roles_post(c, ada, {"channel": "garden", "pubkey": pk(bea), "role": "steward"})
        assert r.status_code == 409 and r.json()["error"] == "events_only"
        r = roles_post(c, ada, {"channel": "garden", "pubkey": pk(bea), "role": "admin"})
        assert r.json() == {"role": "admin"}
        # a belfry admin may change roles anywhere
        cw.belfry_admins = {pk(out)}
        r = roles_post(c, out, {"channel": "garden", "pubkey": pk(bea), "role": "member"})
        assert r.json() == {"role": "member"}


def test_admin_needs_a_saved_account_and_no_agent(rsvc, cw, keycast, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    gus = PrivateKey()
    make_event(cw, ada)
    hold(cw, gus)
    monkeypatch.setattr(guests, "_saved_email", lambda svc, p: False)
    with TestClient(create_app(rsvc)) as c:
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(gus), "role": "admin"})
        assert r.status_code == 409 and r.json()["error"] == "guest_not_saved"
        monkeypatch.setattr(guests, "_is_agent", lambda svc, p: True)
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(gus), "role": "admin"})
        assert r.status_code == 409 and r.json()["error"] == "agent"
        # a guest can be a steward
        monkeypatch.setattr(guests, "_is_agent", lambda svc, p: False)
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(gus), "role": "steward"})
        assert r.json() == {"role": "steward"}


def test_last_admin(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    # an old group: no create event and no relay list; Bea is its only admin by role
    cw.put(cw.gkey, 9000, [["h", "old"], ["p", pk(bea), "admin"]])
    cw.belfry_admins = {pk(ada)}
    with TestClient(create_app(rsvc)) as c:
        r = roles_post(c, ada, {"channel": "old", "pubkey": pk(bea), "role": "member"})
        assert r.status_code == 409 and r.json()["error"] == "last_admin"


def test_attendees_and_check_in(rsvc, cw, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    stew = member(rsvc, "st@belfry.test", "St")
    out = member(rsvc, "out@belfry.test", "Out")
    make_event(cw, ada)
    ticket(cw, ada, "std", "Standard")
    ticket(cw, ada, "vip", "Supporter")
    hold(cw, bea)
    hold(cw, cy, "vip")
    hold(cw, stew)
    known = {pk(bea): "Bea", pk(cy): "Cy", pk(stew): "St"}
    monkeypatch.setattr(events, "names", lambda svc, pks: {k: known[k] for k in pks if k in known})
    cw.put(
        cw.gkey,
        1985,
        [
            ["L", "belfry/group-role"],
            ["l", "steward", "belfry/group-role"],
            ["p", pk(stew)],
            ["h", GROUP],
        ],
    )
    with TestClient(create_app(rsvc)) as c:
        for who in (bea, out):
            r = call(c, who, "POST", f"/{D}/attendees", {})
            assert r.status_code == 403 and r.json()["error"] == "not_allowed"
            r = call(c, who, "POST", f"/{D}/checkin", {"pubkey": pk(cy)})
            assert r.status_code == 403
        got = call(c, stew, "POST", f"/{D}/attendees", {}).json()
        assert [p["name"] for p in got["people"]] == ["Bea", "Cy", "St"]
        assert got["going"] == 3 and got["checked_in"] == 0
        cyrow = got["people"][1]
        assert cyrow["ticket_title"] == "Supporter" and cyrow["role"] == "member"
        assert got["people"][2]["role"] == "steward"
        # by code
        code = checkin.ticket_code(rsvc, D, pk(cy))
        r = call(c, stew, "POST", f"/{D}/checkin", {"code": code}).json()
        assert r["already"] is False and r["person"]["pubkey"] == pk(cy)
        assert r["person"]["checked_in"] and r["person"]["name"] == "Cy"
        lab = cw.grp.events[-1]
        assert lab["kind"] == 1985 and ["l", "checked-in", "belfry/ticket"] in lab["tags"]
        assert ["by", pk(stew)] in lab["tags"] and lab["pubkey"] == pk(cw.gkey)
        r = call(c, ada, "POST", f"/{D}/checkin", {"code": code.upper()}).json()
        assert r["already"] is True and r["person"]["checked_in"] == lab["created_at"]
        assert call(c, cy, "GET", f"/{D}").json()["me"]["checked_in"] == lab["created_at"]
        # a wrong code, another event's code, someone without a ticket
        for bad in (
            {"code": "a" * 16},
            {"code": checkin.ticket_code(rsvc, "2026-zzzzzz", pk(cy))},
            {"code": "nope"},
            {"pubkey": pk(out)},
        ):
            r = call(c, ada, "POST", f"/{D}/checkin", bad)
            assert r.status_code == 404 and r.json()["error"] == "no_ticket", bad
        assert call(c, ada, "POST", f"/{D}/checkin", {}).status_code == 422
        # by pubkey, then undo
        r = call(c, ada, "POST", f"/{D}/checkin", {"pubkey": pk(bea)}).json()
        assert r["already"] is False
        got = call(c, ada, "POST", f"/{D}/attendees", {}).json()
        assert got["checked_in"] == 2
        r = call(c, ada, "POST", f"/{D}/checkin", {"pubkey": pk(bea), "undo": True}).json()
        assert r["already"] is False and r["person"]["checked_in"] is None
        assert ["l", "not-checked-in", "belfry/ticket"] in cw.grp.events[-1]["tags"]
        r = call(c, ada, "POST", f"/{D}/checkin", {"pubkey": pk(bea), "undo": True}).json()
        assert r["already"] is True
        assert call(c, ada, "POST", f"/{D}/attendees", {}).json()["checked_in"] == 1
        # a belfry admin may check in too
        cw.belfry_admins = {pk(out)}
        assert call(c, out, "POST", f"/{D}/attendees", {}).status_code == 200
        # a check-in label by someone else counts for nothing
        cw.put(
            bea,
            1985,
            [
                ["L", "belfry/ticket"],
                ["l", "checked-in", "belfry/ticket"],
                ["p", pk(bea)],
                ["h", GROUP],
            ],
        )
        assert call(c, ada, "POST", f"/{D}/attendees", {}).json()["checked_in"] == 1


def test_removed_by_a_co_host_through_the_belfry(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    dan = member(rsvc, "dan@belfry.test", "Dan")
    make_event(cw, ada)
    ticket(cw, ada, "std", "Standard")
    hold(cw, cy)
    hold(cw, dan)
    cw.put(cw.gkey, 9000, [["h", GROUP], ["p", pk(bea), "admin"]])  # Bea co-hosts by role
    cw.put(
        cw.gkey,
        1985,
        [
            ["L", "belfry/group-role"],
            ["l", "steward", "belfry/group-role"],
            ["p", pk(dan)],
            ["h", GROUP],
        ],
    )
    with TestClient(create_app(rsvc)) as c:
        r = roles_post(c, cy, {"channel": GROUP, "pubkey": pk(dan), "role": "removed"})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        r = roles_post(c, bea, {"channel": GROUP, "pubkey": pk(dan), "role": "removed"})
        assert r.json() == {"role": "removed"}
        out = [e for e in cw.grp.events if e["kind"] == 9001][-1]
        assert out["pubkey"] == pk(cw.gkey)
        assert ["p", pk(dan)] in out["tags"] and ["by", pk(bea)] in out["tags"]
        assert ["l", "none", "belfry/group-role"] in cw.grp.events[-1]["tags"]
        assert pk(dan) not in events.people(rsvc, events.load(rsvc, D)).holders  # ticket back
        # the creator stays
        r = roles_post(c, bea, {"channel": GROUP, "pubkey": pk(ada), "role": "removed"})
        assert r.status_code == 409 and r.json()["error"] == "always_admin"
        # a co-host by role can be removed too (another admin is left)
        r = roles_post(c, ada, {"channel": GROUP, "pubkey": pk(bea), "role": "removed"})
        assert r.json() == {"role": "removed"}
        assert pk(bea) not in events.load(rsvc, D).hosts
    # the last admin of an old group can't be removed
    cw.put(cw.gkey, 9000, [["h", "old"], ["p", pk(cy), "admin"]])
    cw.belfry_admins = {pk(ada)}
    with TestClient(create_app(rsvc)) as c:
        r = roles_post(c, ada, {"channel": "old", "pubkey": pk(cy), "role": "removed"})
        assert r.status_code == 409 and r.json()["error"] == "last_admin"


def meta_post(c, key, data):
    body = json.dumps(data).encode()
    time.sleep(1.05)
    url = "https://belfry.belfry.test/api/v1/group-meta"
    return c.post(
        "/api/v1/group-meta",
        content=body,
        headers={**HOST, "Authorization": nip98(key, url, "POST", body)},
    )


def test_group_meta_keeps_the_other_tags(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    cw.put(ada, 9007, [["h", "pod-garden"]])
    cw.put(
        cw.gkey,
        39000,
        [
            ["d", "pod-garden"],
            ["name", "Garden"],
            ["about", "Old"],
            ["picture", "https://x.test/p.jpg"],
            ["closed"],
            ["prompt", "Why?"],
            ["vouches", "2"],
        ],
    )
    cw.put(cw.gkey, 9000, [["h", "pod-garden"], ["p", pk(bea), "admin"]])
    cw.put(cw.gkey, 9000, [["h", "pod-garden"], ["p", pk(cy)]])
    with TestClient(create_app(rsvc)) as c:
        r = meta_post(c, cy, {"channel": "pod-garden", "name": "x"})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        r = meta_post(c, bea, {"channel": "pod-garden", "name": "  Roof   garden ", "about": "New"})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "channel": "pod-garden",
            "name": "Roof garden",
            "about": "New",
            "closed": True,
            "archived": False,
        }
        e = cw.grp.events[-1]
        assert e["kind"] == 9002 and e["pubkey"] == pk(cw.gkey)
        for t in (
            ["h", "pod-garden"],
            ["name", "Roof garden"],
            ["about", "New"],
            ["picture", "https://x.test/p.jpg"],
            ["closed"],
            ["prompt", "Why?"],
            ["vouches", "2"],
        ):
            assert t in e["tags"], t
        assert not any(t[0] == "d" for t in e["tags"])
        r = meta_post(c, bea, {"channel": "pod-garden", "closed": False, "vouches": 0})
        tags = cw.grp.events[-1]["tags"]
        assert ["open"] in tags and ["closed"] not in tags
        assert not any(t[0] == "vouches" for t in tags)
        assert meta_post(c, bea, {"channel": "pod-garden", "name": ""}).status_code == 422
        assert meta_post(c, bea, {"channel": "nope", "name": "x"}).status_code in (403, 404)
        assert (
            meta_post(c, bea, {"channel": "pod-garden", "picture": "http://x"}).status_code == 422
        )


def test_peek_records_nothing(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    ticket(cw, ada, "std", "Standard")
    hold(cw, bea)
    code = checkin.ticket_code(rsvc, D, pk(bea))
    with TestClient(create_app(rsvc)) as c:
        n = len(cw.grp.events)
        r = call(c, ada, "POST", f"/{D}/checkin", {"code": code, "peek": True}).json()
        assert r["already"] is False and r["person"]["pubkey"] == pk(bea)
        assert r["person"]["ticket_title"] == "Standard" and r["person"]["checked_in"] is None
        assert len(cw.grp.events) == n
        call(c, ada, "POST", f"/{D}/checkin", {"code": code})
        r = call(c, ada, "POST", f"/{D}/checkin", {"code": code, "peek": True}).json()
        assert r["already"] is True and r["person"]["checked_in"]
        bad = call(c, ada, "POST", f"/{D}/checkin", {"code": "a" * 16, "peek": True})
        assert bad.status_code == 404 and bad.json()["error"] == "no_ticket"


def decide_post(c, key, data):
    body = json.dumps(data).encode()
    time.sleep(1.05)
    url = "https://belfry.belfry.test/api/v1/group-requests/decide"
    return c.post(
        "/api/v1/group-requests/decide",
        content=body,
        headers={**HOST, "Authorization": nip98(key, url, "POST", body)},
    )


def test_an_admin_by_role_answers_a_request_to_a_closed_channel(rsvc, cw, keycast, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    dan = member(rsvc, "dan@belfry.test", "Dan")
    cw.put(ada, 9007, [["h", "board"]])
    cw.admins("board", [pk(ada)])
    cw.put(cw.gkey, 9000, [["h", "board"], ["p", pk(bea), "admin"], ["role", "admin"]])
    with TestClient(create_app(rsvc)) as c:
        ask = {"channel": "board", "pubkey": pk(cy), "action": "approve"}
        assert decide_post(c, bea, ask).json()["error"] == "no_request"
        req = cw.put(cy, 9021, [["h", "board"]], "I keep the books")
        assert decide_post(c, dan, ask).json()["error"] == "not_allowed"
        assert decide_post(c, bea, {**ask, "action": "maybe"}).status_code == 422
        r = decide_post(c, bea, ask)  # Bea is an admin by role only
        assert r.json() == {"approved": True}
        put = cw.grp.events[-1]
        assert put["kind"] == 9000 and put["pubkey"] == pk(cw.gkey)
        assert ["p", pk(cy), "member"] in put["tags"] and ["by", pk(bea)] in put["tags"]
        assert ["e", req["id"]] in put["tags"]
        assert decide_post(c, bea, ask).json() == {"already": True}
        # declined: a 9001 naming who decided; then nothing is open
        time.sleep(1.05)
        cw.put(dan, 9021, [["h", "board"]], "Me too")
        no = {"channel": "board", "pubkey": pk(dan), "action": "decline"}
        assert decide_post(c, ada, no).json() == {"declined": True}
        assert cw.grp.events[-1]["kind"] == 9001 and ["by", pk(ada)] in cw.grp.events[-1]["tags"]
        assert decide_post(c, ada, no).json()["error"] == "no_request"
        # an event's requests go through the event
        r = decide_post(c, ada, {"channel": GROUP, "pubkey": pk(cy), "action": "approve"})
        assert r.status_code == 409 and r.json()["error"] == "event"
