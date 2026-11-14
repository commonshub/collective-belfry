"""The belfry's Membership settings, requests to join (membership.py) and groups that accept
by vouches (vouches.py)."""

# ruff: noqa: F811
from __future__ import annotations

import itertools
import time

from fastapi.testclient import TestClient

from belfry_onboarding import followers, mailer, membership, vouches
from belfry_onboarding.app import create_app

from .fakes import FakeRelay
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures

GK = "f" * 64  # the groups key (the relay's owner)
_ids = itertools.count(1)


def ev(kind, pubkey, tags, content="", at=None):
    n = next(_ids)
    return {
        "id": f"{n:064x}",
        "kind": kind,
        "pubkey": pubkey,
        "created_at": at if at is not None else int(time.time()) - 1000 + n,
        "tags": tags,
        "content": content,
    }


def sign(template):
    return {**template, "id": f"{next(_ids):064x}", "pubkey": GK, "sig": "0" * 128}


def apply_now(rsvc, name="Nora New", email="nora@example.org"):
    return rsvc.odoo.call(
        "belfry.application",
        "belfry_application_create",
        [{"name": name, "email": email, "motivation": "I fix bikes.", "kind": "visitor"}, []],
    )


# ── settings and the invite rule ────────────────────────────────────────────────────────────
def test_settings_default_to_admins_and_only_admins_change_them(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        got = call(c, bob, "/membership", method="GET").json()
        assert got == {
            "invite": "admins",
            "accept": "admins",
            "vouches": 2,
            "may_invite_members": False,
            "admin": False,
        }
        assert call(c, ada, "/membership", method="GET").json()["may_invite_members"] is True
        r = call(c, bob, "/membership", {"invite": "members"})
        assert r.status_code == 403 and r.json()["error"] == "not_admin"
        assert call(c, ada, "/membership", {"invite": "everyone"}).status_code == 422
        assert call(c, ada, "/membership", {"vouches": 6}).json()["error"] == "invalid"
        assert call(c, ada, "/membership", {"vouches": True}).status_code == 422
        r = call(c, ada, "/membership", {"invite": "members", "accept": "vouches", "vouches": 3})
        assert r.status_code == 200, r.text
        assert r.json()["invite"] == "members" and r.json()["vouches"] == 3
        assert call(c, bob, "/membership", method="GET").json()["may_invite_members"] is True


def test_member_links_follow_the_setting(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        link = {"channel": "garden", "as": "member"}
        r = call(c, bob, "/channel-invites", link)
        assert r.status_code == 403 and r.json()["error"] == "not_admin"
        call(c, ada, "/roles", {"label": "Host"})
        call(c, ada, "/membership", {"invite": "members"})
        r = call(c, bob, "/channel-invites", link)
        assert r.status_code == 200, r.text
        assert r.json()["as"] == "member" and r.json()["member_role"] == "member"
        # a member gives the default role only; admins any
        r = call(c, bob, "/channel-invites", {**link, "role": "host"})
        assert r.status_code == 403 and r.json()["error"] == "not_admin"
        r = call(c, bob, "/channel-invites", {**link, "role": "admin"})
        assert r.status_code == 403
        assert call(c, ada, "/channel-invites", {**link, "role": "host"}).status_code == 200
        # guest links stay as they were for members
        assert call(c, bob, "/channel-invites", {"channel": "garden"}).status_code == 200


# ── requests to join the belfry ─────────────────────────────────────────────────────────────
def test_admins_list_accept_and_decline_requests(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    nora = apply_now(rsvc)
    omar = apply_now(rsvc, "Omar", "omar@example.org")
    with TestClient(create_app(rsvc)) as c:
        r = call(c, bob, "/applications", {"list": True})
        assert r.status_code == 403 and r.json()["error"] == "not_admin"
        got = call(c, ada, "/applications", {"list": True}).json()
        assert [a["id"] for a in got["applications"]] == [omar, nora]
        row = got["applications"][1]
        assert row["name"] == "Nora New" and row["email"] == "nora@example.org"
        assert row["motivation"] == "I fix bikes." and row["vouches"] == 0
        assert got["accept"] == "admins"
        r = call(c, bob, "/applications", {"decide": nora, "approve": True})
        assert r.status_code == 403
        mailer.SENT.clear()
        r = call(c, ada, "/applications", {"decide": nora, "approve": True})
        assert r.status_code == 200, r.text
        assert r.json()["application"]["state"] == "invited"
        assert any("nora@example.org" in str(m["To"]) for m in mailer.SENT)  # the invite
        r = call(c, ada, "/applications", {"decide": nora, "approve": False})
        assert r.status_code == 409 and r.json()["error"] == "decided"
        r = call(c, ada, "/applications", {"decide": omar, "approve": False})
        assert r.json()["application"]["state"] == "declined"
        assert call(c, ada, "/applications", {"list": True}).json()["applications"] == []
        assert call(c, ada, "/applications", {"decide": 999, "approve": True}).status_code == 404


def test_vouches_accept_a_request_by_themselves(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    cat, _ = person(rsvc, "cat@belfry.test", "Cat")
    nora = apply_now(rsvc)
    with TestClient(create_app(rsvc)) as c:
        r = call(c, bob, "/applications", {"vouch": nora})
        assert r.status_code == 403 and r.json()["error"] == "not_vouching"
        call(c, ada, "/membership", {"accept": "vouches", "vouches": 2})
        # members see waiting requests now, without the e-mail address
        got = call(c, bob, "/applications", {"list": True}).json()
        assert got["needed"] == 2 and "email" not in got["applications"][0]
        r = call(c, bob, "/applications", {"vouch": nora})
        assert r.status_code == 200, r.text
        assert r.json()["application"]["vouches"] == 1 and r.json()["application"]["vouched"]
        assert r.json()["accepted"] is False
        # the same person twice counts once; taking it back works
        assert call(c, bob, "/applications", {"vouch": nora}).json()["application"]["vouches"] == 1
        r = call(c, bob, "/applications", {"vouch": nora, "on": False})
        assert r.json()["application"]["vouches"] == 0
        call(c, bob, "/applications", {"vouch": nora})
        mailer.SENT.clear()
        r = call(c, cat, "/applications", {"vouch": nora})
        assert r.json()["accepted"] is True and r.json()["application"]["state"] == "invited"
        assert any("nora@example.org" in str(m["To"]) for m in mailer.SENT)
        row = rsvc.odoo.read_one("belfry.application", nora, ["handled_by"])
        assert row["handled_by"] == membership.BY_VOUCHES
        r = call(c, cat, "/applications", {"vouch": nora})
        assert r.status_code == 409


def test_lowering_the_number_accepts_and_leavers_stop_counting(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    nora = apply_now(rsvc)
    with TestClient(create_app(rsvc)) as c:
        call(c, ada, "/membership", {"accept": "vouches", "vouches": 3})
        call(c, bob, "/applications", {"vouch": nora})
        # a key that is no member any more doesn't count
        rsvc.odoo.write("belfry.application", [nora], {"vouched_by": [pk(bob), "e" * 64]})
        assert call(c, ada, "/applications", {"list": True}).json()["applications"][0][
            "vouches"
        ] == 1
        assert membership.settle(rsvc) == []
        call(c, ada, "/membership", {"vouches": 1})
        row = rsvc.odoo.read_one("belfry.application", nora, ["state"])
        assert row["state"] == "invited"


# ── groups that accept by vouches ───────────────────────────────────────────────────────────
def group_world(monkeypatch, members, admins, n):
    relay = FakeRelay()
    ch = "pod-garden-ab"
    relay.events += [
        ev(39000, GK, [["d", ch], ["name", "Garden"], ["closed"]]),
        ev(9002, admins[0], [["h", ch], ["name", "Garden"], ["closed"], ["vouches", str(n)]]),
        ev(39001, GK, [["d", ch]] + [["p", a, "admin"] for a in admins]),
        ev(39002, GK, [["d", ch]] + [["p", m] for m in members]),
    ]
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, sign))
    return relay, ch


def vouch(relay, ch, author, req, value="vouch"):
    relay.events.append(
        ev(
            1985,
            author,
            [
                ["h", ch],
                ["L", "belfry/vouch"],
                ["l", value, "belfry/vouch"],
                ["e", req["id"]],
                ["p", req["pubkey"]],
            ],
        )
    )


def test_a_group_admits_by_vouches(world, monkeypatch):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    cat, _ = person(rsvc, "cat@belfry.test", "Cat")
    dan, _ = person(rsvc, "dan@belfry.test", "Dan")
    nora = "a" * 64  # the applicant
    relay, ch = group_world(monkeypatch, [pk(ada), pk(bob), pk(cat)], [pk(ada)], 2)
    req = ev(9021, nora, [["h", ch]], "I grow tomatoes.")
    relay.events.append(req)
    vouch(relay, ch, pk(bob), req)
    vouch(relay, ch, nora, req)  # the applicant's own: no
    vouch(relay, ch, pk(dan), req)  # not in the group: no
    assert vouches.watch(rsvc) == []
    vouch(relay, ch, pk(cat), req, "withdrawn")
    assert vouches.check(rsvc, ch) == []
    vouch(relay, ch, pk(cat), req)  # the latest label counts
    assert vouches.watch(rsvc) == [nora]
    admitted = [e for e in relay.events if e["kind"] == 9000]
    assert len(admitted) == 1 and admitted[0]["pubkey"] == GK
    tags = admitted[0]["tags"]
    assert ["p", nora, "member"] in tags and ["by", "vouches"] in tags
    assert ["vouches", "2"] in tags and ["e", req["id"]] in tags
    # idempotent: answered now
    assert vouches.watch(rsvc) == [] and vouches.check(rsvc, ch) == []


def test_groups_without_the_rule_and_answered_requests_are_left_alone(world, monkeypatch):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    relay, ch = group_world(monkeypatch, [pk(ada), pk(bob)], [pk(ada)], 0)
    req = ev(9021, "b" * 64, [["h", ch]])
    relay.events.append(req)
    vouch(relay, ch, pk(bob), req)
    vouch(relay, ch, pk(ada), req)
    assert vouches.check(rsvc, ch) == []  # admins accept here
    # the rule turned on, but an admin declined already
    relay.events.append(ev(9002, pk(ada), [["h", ch], ["closed"], ["vouches", "1"]]))
    relay.events.append(ev(9001, pk(ada), [["h", ch], ["p", "b" * 64]]))
    assert vouches.check(rsvc, ch) == []
    # asking again opens a new request: old vouches don't carry over
    req2 = ev(9021, "b" * 64, [["h", ch]])
    relay.events.append(req2)
    assert vouches.check(rsvc, ch) == []
    vouch(relay, ch, pk(bob), req2)
    assert vouches.check(rsvc, ch) == ["b" * 64]


def test_the_check_endpoint(world, monkeypatch):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    relay, ch = group_world(monkeypatch, [pk(ada), pk(bob)], [pk(ada)], 1)
    req = ev(9021, "c" * 64, [["h", ch]])
    relay.events.append(req)
    vouch(relay, ch, pk(bob), req)
    with TestClient(create_app(rsvc)) as c:
        assert call(c, bob, "/group-requests/check", {"channel": "../x"}).status_code == 422
        r = call(c, bob, "/group-requests/check", {"channel": ch})
        assert r.status_code == 200
        # the service's own round may have got there first: one 9000 either way
        assert r.json()["admitted"] in ([], ["c" * 64])
        assert len([e for e in relay.events if e["kind"] == 9000]) == 1
