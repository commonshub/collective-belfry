"""Inviting a member to co-host an event (cohost.py, docs/checkin.md)."""
# ruff: noqa: F811 — the fixtures come from test_events

from __future__ import annotations

import base64
import json
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, checkin, cohost, events, guests, mail, push
from belfry_onboarding.app import create_app
from belfry_onboarding.nip46check import nip44_decrypt

from .test_events import (  # noqa: F401 — fixtures
    GROUP,
    D,
    World,
    call,
    make_event,
    member,
    pk,
    rsvc,
    world,
)


@pytest.fixture
def cw(world: World, monkeypatch) -> World:
    monkeypatch.setattr(checkin, "groups_pubkey", lambda svc: pk(world.gkey))
    world.belfry_admins = set()  # type: ignore[attr-defined]
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: world.belfry_admins)
    # the link route's check (guests._make_host) reads the group's admins the relay's way
    monkeypatch.setattr(
        mail,
        "group_admins",
        lambda svc, ch: events.hosts_of(svc, ch)[0] | ({events.hosts_of(svc, ch)[1]} - {None}),
    )
    return world


class FakeGateway:
    sent: list = []

    def __init__(self, svc):
        pass

    def send(self, dev, payload, collapse, urgent):
        FakeGateway.sent.append((dev, payload, collapse, urgent))
        return "ok"


@pytest.fixture
def phones(rsvc, monkeypatch):
    FakeGateway.sent = []
    monkeypatch.setattr(push, "enabled", lambda svc: True)
    monkeypatch.setattr(push, "Gateway", FakeGateway)
    keys: dict[str, bytes] = {}

    def add(key: PrivateKey) -> bytes:
        import os

        k = os.urandom(32)
        row = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", pk(key)]], ["partner_id"]
        )
        dev = push._device(
            {
                "platform": "apns",
                "token": pk(key)[:40],
                "env": "sandbox",
                "key": base64.b64encode(k).decode(),
                "app": "belfry-ios",
                "name": "Phone",
            }
        )
        rsvc.odoo.push_add(
            dev | {"pubkey": pk(key), "partner_id": row[0]["partner_id"][0]}, int(time.time())
        )
        keys[pk(key)] = k
        return k

    return add


def tier(rsvc, key: PrivateKey, value: str) -> None:
    row = rsvc.odoo.search_read("belfry.pass", [["keycast_pubkey", "=", pk(key)]], ["id"])[0]
    rsvc.odoo.write("belfry.pass", [row["id"]], {"tier": value})


def post(c, key, path, data):
    return call(c, key, "POST", f"/{D}/cohost-invites{path}", data)


def test_invite_push_get_and_accept(rsvc, cw, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    devkey = phones(bea)
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, "", {"pubkey": pk(bea)})
        assert r.status_code == 200, r.text
        inv = r.json()["invite"]
        assert inv["state"] == "pending" and set(inv["by"]) == {"name", "slug", "picture"}
        assert "already" not in r.json()
        # one push, to Bea's phone, of the new type
        assert len(FakeGateway.sent) == 1
        dev, payload, collapse, urgent = FakeGateway.sent[0]
        plain = json.loads(nip44_decrypt(devkey, payload))
        assert plain["type"] == "cohost_invite" and plain["body"] == "Garden day"
        assert plain["title"].endswith("invited you to co-host")
        assert plain["channel"] == GROUP and collapse == GROUP and urgent is True
        # again while pending: the same invitation, no second push
        again = post(c, ada, "", {"pubkey": pk(bea)})
        assert again.status_code == 200
        assert again.json() == {"invite": inv, "already": True}
        assert len(FakeGateway.sent) == 1
        # Bea sees it; Ada sees who she invited
        me = call(c, bea, "GET", f"/{D}").json()["me"]
        assert me["cohost_invite"]["id"] == inv["id"] and "cohost_invites" not in me
        host = call(c, ada, "GET", f"/{D}").json()["me"]
        assert host["cohost_invite"] is None
        [row] = host["cohost_invites"]
        assert row["state"] == "pending" and row["pubkey"] == pk(bea)
        assert set(row["person"]) == {"name", "slug", "picture"}
        # accepting: the same 9000 as a co-host link (guests._make_host)
        r = post(c, bea, "/answer", {"accept": True})
        assert r.status_code == 200 and r.json() == {"state": "accepted", "host": True}
        last = cw.grp.events[-1]
        assert last["kind"] == 9000 and last["pubkey"] == pk(cw.gkey)
        assert sorted(last["tags"]) == sorted(
            [["h", GROUP], ["p", pk(bea), "admin"], ["invited-by", pk(ada)]]
        )
        me = call(c, bea, "GET", f"/{D}").json()["me"]
        assert me["roles"] == ["cohost"] and me["host"] is True and me["cohost_invite"] is None
        assert call(c, ada, "GET", f"/{D}").json()["me"]["cohost_invites"][0]["state"] == (
            "accepted"
        )
        # a co-host now: inviting again says so, answering again finds nothing
        assert post(c, ada, "", {"pubkey": pk(bea)}).json()["error"] == "already_host"
        r = post(c, bea, "/answer", {"accept": True})
        assert r.status_code == 404 and r.json()["error"] == "no_invite"


def test_the_link_route_publishes_the_same_event(rsvc, cw, keycast, monkeypatch):
    """The co-host link's join and the accepted invitation leave the same tags."""
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    assert guests._make_host(rsvc, GROUP, pk(bea), {"guest_inviter": pk(ada)})
    by_link = cw.grp.events[-1]
    assert sorted(by_link["tags"]) == sorted(
        [["h", GROUP], ["p", pk(bea), "admin"], ["invited-by", pk(ada)]]
    )


def test_decline_and_hosts_see_it_and_a_new_invite(rsvc, cw, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    with TestClient(create_app(rsvc)) as c:
        first = post(c, ada, "", {"pubkey": pk(bea)}).json()["invite"]
        before = len(cw.grp.events)
        r = post(c, bea, "/answer", {"accept": False})
        assert r.status_code == 200 and r.json() == {"state": "declined"}
        assert len(cw.grp.events) == before  # nothing on the relay
        assert call(c, ada, "GET", f"/{D}").json()["me"]["cohost_invites"][0]["state"] == (
            "declined"
        )
        assert call(c, bea, "GET", f"/{D}").json()["me"]["cohost_invite"] is None
        assert post(c, bea, "/answer", {"accept": True}).json()["error"] == "no_invite"
        # the host may ask once more: a new invitation
        again = post(c, ada, "", {"pubkey": pk(bea)}).json()
        assert "already" not in again and again["invite"]["id"] != first["id"]
        assert post(c, bea, "/answer", {"accept": "yes"}).status_code == 422


def test_who_may_invite_and_who_can_be_invited(rsvc, cw, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    gus = member(rsvc, "gus@belfry.test", "Gus")
    bot = member(rsvc, "bot@belfry.test", "Bot")
    boss = member(rsvc, "boss@belfry.test", "Boss")
    tier(rsvc, gus, "guest")
    tier(rsvc, bot, "agent")
    make_event(cw, ada)
    cw.belfry_admins = {pk(boss)}
    with TestClient(create_app(rsvc)) as c:
        # an attendee (not a host) can't; a steward can't either
        r = post(c, bea, "", {"pubkey": pk(cy)})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        cw.put(
            cw.gkey,
            1985,
            [
                ["L", "belfry/group-role"],
                ["l", "steward", "belfry/group-role"],
                ["p", pk(bea)],
                ["h", GROUP],
            ],
        )
        assert post(c, bea, "", {"pubkey": pk(cy)}).status_code == 403
        # a belfry admin who doesn't host it may not (2026-11-26); its host may
        r = post(c, boss, "", {"pubkey": pk(cy)})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        assert post(c, ada, "", {"pubkey": pk(cy)}).status_code == 200
        # nobody hosts twice
        r = post(c, ada, "", {"pubkey": pk(ada)})
        assert r.status_code == 409 and r.json()["error"] == "already_host"
        # guests: the link route instead; agents and strangers: no
        r = post(c, ada, "", {"pubkey": pk(gus)})
        assert r.status_code == 409 and r.json()["error"] == "not_member"
        assert r.json()["guest"] is True
        r = post(c, ada, "", {"pubkey": pk(bot)})
        assert r.status_code == 409 and r.json()["error"] == "not_member"
        assert "guest" not in r.json()
        r = post(c, ada, "", {"pubkey": "ab" * 32})
        assert r.status_code == 409 and r.json()["error"] == "not_member"
        assert post(c, ada, "", {"pubkey": "nope"}).json()["error"] == "invalid"
        assert post(c, ada, "", {}).status_code == 422
        # an unknown event
        r = call(c, ada, "POST", "/2026-zzzzzz/cohost-invites", {"pubkey": pk(cy)})
        assert r.status_code == 404 and r.json()["error"] == "unknown"
        # a co-host (by role) may invite too
        assert post(c, cy, "/answer", {"accept": True}).status_code == 200
        assert post(c, cy, "", {"pubkey": pk(bea)}).status_code == 200


def test_withdraw(rsvc, cw, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    with TestClient(create_app(rsvc)) as c:
        post(c, ada, "", {"pubkey": pk(bea)})
        assert post(c, bea, "/withdraw", {"pubkey": pk(bea)}).status_code == 403
        r = post(c, ada, "/withdraw", {"pubkey": pk(bea)})
        assert r.status_code == 200 and r.json() == {"withdrawn": True}
        assert call(c, ada, "GET", f"/{D}").json()["me"]["cohost_invites"] == []
        assert call(c, bea, "GET", f"/{D}").json()["me"]["cohost_invite"] is None
        r = post(c, ada, "/withdraw", {"pubkey": pk(bea)})
        assert r.status_code == 404 and r.json()["error"] == "no_invite"
        assert post(c, bea, "/answer", {"accept": True}).json()["error"] == "no_invite"


def test_expiry_after_30_days_and_when_the_event_is_over(rsvc, cw, phones, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada, start_in=60 * 86400)
    with TestClient(create_app(rsvc)) as c:
        first = post(c, ada, "", {"pubkey": pk(bea)}).json()["invite"]
        row = rsvc.odoo.cohost_get(D, pk(bea))
        rsvc.odoo.cohost_put({**row, "at": int(time.time()) - 31 * 86400})
        assert call(c, bea, "GET", f"/{D}").json()["me"]["cohost_invite"] is None
        assert call(c, ada, "GET", f"/{D}").json()["me"]["cohost_invites"] == []
        assert post(c, bea, "/answer", {"accept": True}).json()["error"] == "no_invite"
        # a lapsed invitation can be sent again (a new one)
        again = post(c, ada, "", {"pubkey": pk(bea)}).json()
        assert "already" not in again and again["invite"]["id"] != first["id"]
        # the event is over: nothing to answer, nothing new
        monkeypatch.setattr(events.Event, "ended", lambda self, now=None: True)
        r = post(c, bea, "/answer", {"accept": True})
        assert r.status_code == 409 and r.json()["error"] == "event_over"
        r = post(c, ada, "", {"pubkey": pk(bea)})
        assert r.status_code == 409 and r.json()["error"] == "event_over"
        assert cohost.pending(rsvc, events.load(rsvc, D), pk(bea)) is None


def test_archived_and_cancelled(rsvc, cw, phones, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    with TestClient(create_app(rsvc)) as c:
        post(c, ada, "", {"pubkey": pk(bea)})
        monkeypatch.setattr(checkin, "is_archived", lambda svc, g: True)
        r = post(c, ada, "", {"pubkey": pk(member(rsvc, "cy@belfry.test", "Cy"))})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        r = post(c, bea, "/answer", {"accept": True})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        # declining still works in an archived group
        assert post(c, bea, "/answer", {"accept": False}).json() == {"state": "declined"}


def test_a_member_who_turned_the_event_off_gets_no_push(rsvc, cw, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    phones(bea)
    partner = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", pk(bea)]], ["partner_id"]
    )
    push.set_settings(rsvc, partner[0]["partner_id"][0], {"channels": {GROUP: "off"}})
    with TestClient(create_app(rsvc)) as c:
        assert post(c, ada, "", {"pubkey": pk(bea)}).status_code == 200
    assert FakeGateway.sent == []


def test_a_push_failure_never_fails_the_invitation(rsvc, cw, phones, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    phones(bea)

    def boom(self, *a):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(FakeGateway, "send", boom)
    with TestClient(create_app(rsvc)) as c:
        assert post(c, ada, "", {"pubkey": pk(bea)}).status_code == 200


def test_with_odoo_it_is_unsupported(rsvc, cw, monkeypatch):
    from belfry_onboarding import invite

    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(cw, ada)
    with TestClient(create_app(rsvc)) as c:
        monkeypatch.setattr(invite, "own", lambda svc: False)
        r = post(c, ada, "", {"pubkey": pk(bea)})
        assert r.status_code == 409 and r.json()["error"] == "unsupported"
        me = call(c, ada, "GET", f"/{D}").json()["me"]
        assert "cohost_invite" not in me and "cohost_invites" not in me


def test_cors_chat_host_only(rsvc, cw):
    with TestClient(create_app(rsvc)) as c:
        path = f"/api/events/{D}/cohost-invites"
        for suffix in ("", "/answer", "/withdraw"):
            for origin, ok in (
                ("https://chat.belfry.test", True),
                ("https://belfry.test", False),
                ("https://preview.belfry.test", False),
            ):
                r = c.options(
                    path + suffix,
                    headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
                )
                assert (r.status_code == 204) is ok, (suffix, origin, r.status_code)
        # the apex still reads the event (unchanged)
        r = c.options(
            f"/api/events/{D}",
            headers={"Origin": "https://belfry.test", "Access-Control-Request-Method": "GET"},
        )
        assert r.status_code == 204
