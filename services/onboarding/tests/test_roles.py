"""Members' roles (roles.py) and links that make someone a member (guests.py, "as": "member")."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import itertools
import json
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, console, core, guests, mail, mailer, roles, steward
from belfry_onboarding.app import create_app
from belfry_onboarding.places import member_key

from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import BASE, nip98

API = "https://belfry.belfry.test/api/v1"
_tick = itertools.count(1)


def pk(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


def call(c, key, path, data=None, method="POST"):
    """A signed request on the belfry host; each one a second apart (no replays)."""
    at = int(time.time()) - 50 + next(_tick) % 50
    body = b"" if method == "GET" else json.dumps(data or {}).encode()
    headers = {
        "Host": "belfry.belfry.test",
        "Authorization": nip98(key, API + path, method, body, created_at=at),
    }
    if method == "GET":
        return c.get("/api/v1" + path, headers=headers)
    headers["Content-Type"] = "application/json"
    return c.post("/api/v1" + path, content=body, headers=headers)


@pytest.fixture
def world(rsvc, published, keycast, monkeypatch):  # noqa: F811
    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    rsvc.settings.nip05_domain = "belfry.test"
    stewards = {"admin@belfry.test"}
    monkeypatch.setattr(console, "admin_emails", lambda svc: set(stewards))

    def set_admin(svc, email, admin):
        if admin:
            stewards.add(email)
            steward.steward_pass(svc, email)
        else:
            stewards.discard(email)

    monkeypatch.setattr(console, "set_admin", set_admin)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: True)
    monkeypatch.setattr(guests, "_in_channel", lambda *a: False)
    apps.forget()
    ada, ada_id = person(rsvc, "admin@belfry.test", "Ada Admin")
    return rsvc, published, keycast, ada, ada_id, stewards


def person(rsvc, email, name):
    made = steward.steward_pass(rsvc, email, name)
    key = PrivateKey()
    rsvc.odoo.write(
        "belfry.pass", [made["pass_id"]], {"keycast_pubkey": pk(key), "state": "active"}
    )
    row = rsvc.odoo.read_one("belfry.pass", made["pass_id"], ["partner_id"])
    apps.forget()
    return key, core.m2o_id(row["partner_id"])


def test_admins_keep_a_list_of_roles(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        got = call(c, bob, "/roles", method="GET").json()
        assert [r["id"] for r in got["roles"]] == ["member", "admin"]
        assert got["default"] == "member" and got["members"] == {
            pk(ada): "admin",
            pk(bob): "member",
        }
        assert "others" not in got  # counts of guests and agents: admins only
        # only admins change roles
        r = call(c, bob, "/roles", {"label": "Host"})
        assert r.status_code == 403 and r.json()["error"] == "not_admin"
        r = call(c, ada, "/roles", {"label": "Host", "description": "Runs events at the Hub."})
        assert r.status_code == 201, r.text
        assert r.json()["role"] == {
            "id": "host",
            "label": "Host",
            "description": "Runs events at the Hub.",
            "builtin": False,
            "powers": False,
            "default": False,
        }
        assert [x["id"] for x in r.json()["roles"]] == ["member", "host", "admin"]
        assert call(c, ada, "/roles", {"label": "host"}).json()["error"] == "taken"
        assert call(c, ada, "/roles", {"label": ""}).json()["error"] == "invalid"
        assert call(c, ada, "/roles", {"label": "x" * 41}).status_code == 422
        # rename a built-in; Admin can't be the default
        r = call(c, ada, "/roles/member", {"label": "Neighbour"})
        assert r.status_code == 200 and r.json()["role"]["label"] == "Neighbour"
        assert call(c, ada, "/roles/admin", {"default": True}).json()["error"] == "admin_default"
        r = call(c, ada, "/roles/host", {"default": True})
        assert r.json()["default"] == "host"
        assert call(c, ada, "/roles/nope", {"label": "X"}).json()["error"] == "unknown_role"
        assert call(c, ada, "/roles/member/delete").json()["error"] == "builtin"
        others = call(c, ada, "/roles", method="GET").json()["others"]
        assert others == {"guest": 0, "agent": 0}


def test_a_role_per_member_and_deleting_one_moves_them_to_the_default(world):
    rsvc, _p, _k, ada, _ada_id, stewards = world
    bob, bob_id = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        call(c, ada, "/roles", {"label": "Host"})
        call(c, ada, "/roles", {"label": "Steward of money"})
        r = call(c, ada, "/member-roles", {"pubkey": pk(bob), "role": "host"})
        assert r.status_code == 200 and r.json()["role"] == "host"
        assert rsvc.odoo.read_one("res.partner", bob_id, ["belfry_role"])["belfry_role"] == "host"
        assert call(c, bob, "/member-roles", {"pubkey": pk(ada), "role": "host"}).status_code == 403
        assert (
            call(c, ada, "/member-roles", {"pubkey": pk(bob), "role": "nope"}).json()["error"]
            == "unknown_role"
        )
        # whois carries it, apart from "people"
        r = call(c, bob, "/whois", {"pubkeys": [pk(bob), pk(ada)]}).json()
        assert r["people"] == {pk(bob): "member", pk(ada): "member"}
        assert r["roles"] == {pk(bob): "host", pk(ada): "admin"}
        # the admin role is the stewards group: given and taken through admins.py
        r = call(c, ada, "/member-roles", {"pubkey": pk(bob), "role": "admin"})
        assert r.json()["role"] == "admin" and "bob@belfry.test" in stewards
        assert sorted(r.json()["admins"]) == sorted([pk(ada), pk(bob)])
        r = call(c, ada, "/member-roles", {"pubkey": pk(bob), "role": "steward-of-money"})
        assert r.json()["role"] == "steward-of-money" and "bob@belfry.test" not in stewards
        # the last admin stays one
        r = call(c, ada, "/member-roles", {"pubkey": pk(ada), "role": "member"})
        assert r.status_code == 400 and r.json()["error"] == "last_admin"
        # removed from the admins elsewhere (the console): back to the default, not "admin"
        call(c, ada, "/member-roles", {"pubkey": pk(bob), "role": "admin"})
        stewards.discard("bob@belfry.test")
        apps.forget()
        assert roles.role_of(rsvc, pk(bob)) == "member"
        # deleting a role: who had it gets the default
        call(c, ada, "/member-roles", {"pubkey": pk(bob), "role": "steward-of-money"})
        call(c, ada, "/roles/host", {"default": True})
        r = call(c, ada, "/roles/steward-of-money/delete")
        assert r.status_code == 200 and r.json()["moved"] == [pk(bob)]
        assert roles.role_of(rsvc, pk(bob)) == "host"
        assert [x["id"] for x in r.json()["roles"]] == ["member", "host", "admin"]


def test_a_new_member_gets_the_default_role_and_old_ones_keep_theirs(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        call(c, ada, "/roles", {"label": "Neighbour", "default": True})
    assert roles.role_of(rsvc, pk(bob)) == "member"  # no stored role: a member, not the default
    made = rsvc.odoo.call("belfry.pass", "belfry_steward_pass", ["cy@belfry.test", "Cy"])
    core.activate_free_pass(rsvc, made["pass_id"])
    row = rsvc.odoo.read_one("belfry.pass", made["pass_id"], ["partner_id", "keycast_pubkey"])
    assert roles.role_of(rsvc, row["keycast_pubkey"]) == "neighbour"


def _join(c, token, key, data=None):
    return call(c, key, f"/channel-invites/{token}/join", data or {})


def test_only_admins_make_member_links(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        r = call(c, bob, "/channel-invites", {"channel": "garden", "as": "member"})
        assert r.status_code == 403 and r.json()["error"] == "not_admin"
        r = call(c, ada, "/channel-invites", {"channel": "garden", "as": "member", "role": "x"})
        assert r.status_code == 404 and r.json()["error"] == "unknown_role"
        bad = {"channel": "garden", "as": "member", "membership": "week"}
        assert call(c, ada, "/channel-invites", bad).status_code == 422
        r = call(c, ada, "/channel-invites", {"channel": "garden", "as": "member"})
        assert r.status_code == 200, r.text
        assert r.json()["as"] == "member" and r.json()["member_role"] == "member"
        assert r.json()["membership"] == "permanent"
        # an admin link is for one person
        r = call(
            c,
            ada,
            "/channel-invites",
            {"channel": "garden", "as": "member", "role": "admin", "people": 50},
        )
        assert r.json()["people"] == 1


def test_guest_links_last_a_day_a_week_or_a_month(world):
    rsvc, _p, _k, ada, _ada_id, _s = world
    with TestClient(create_app(rsvc)) as c:
        r = call(c, ada, "/channel-invites", {"channel": "garden", "guest_days": 7})
        assert r.json()["days"] == 7 and r.json()["as"] == "guest"
        token = r.json()["url"].rsplit("/", 1)[1]
        got = _join(c, token, PrivateKey())
        assert got.status_code == 200, got.text
        p = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", got.json()["pubkey"]]], ["date_end"]
        )[0]
        assert p["date_end"] == (dt.date.today() + dt.timedelta(days=7)).isoformat()
        assert (
            call(c, ada, "/channel-invites", {"channel": "garden", "guest_days": 3}).status_code
            == 422
        )
        # a request that doesn't say (older clients): 30 days, as always
        assert call(c, ada, "/channel-invites", {"channel": "garden"}).json()["days"] == 30


def test_a_member_link_makes_a_member_once_they_join_with_an_e_mail(world):
    rsvc, published, keycast, ada, _ada_id, _s = world
    with TestClient(create_app(rsvc)) as c:
        call(c, ada, "/roles", {"label": "Host", "description": "Runs events."})
        r = call(
            c,
            ada,
            "/channel-invites",
            {"channel": "garden", "as": "member", "role": "host", "membership": "year"},
        )
        token = r.json()["url"].rsplit("/", 1)[1]
        info = c.get(f"/api/channel-invites/{token}").json()
        assert info["as"] == "member" and info["member_role"] == "host"
        assert info["member_role_label"] == "Host" and info["membership"] == "year"
        # opening it: guest mode, reading, as with a guest link
        got = _join(c, token, PrivateKey())
        assert got.status_code == 200, got.text
        assert got.json()["reading"] is True and got.json()["email_needed"] is True
        assert got.json()["as"] == "member" and got.json()["member_role_label"] == "Host"
        assert got.json()["membership"] == "year"
        # our own key stands in for the identity the signer holds
        me = PrivateKey()
        p = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", got.json()["pubkey"]]], ["id", "partner_id"]
        )[0]
        rsvc.odoo.write("belfry.pass", [p["id"]], {"keycast_pubkey": pk(me)})
        keycast.status[pk(me)] = "active"
        keycast.users[f"odoo-partner-{core.m2o_id(p['partner_id'])}"]["pubkey"] = pk(me)
        mailer.SENT.clear()
        r = call(c, me, "/guest/save", {"email": "gus@example.org", "name": "Gus"})
        assert r.status_code == 202, r.text
        r = call(
            c,
            me,
            "/guest/save/verify",
            {"email": "gus@example.org", "code": _code(), "name": "Gus"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"saved": True, "joined": True, "member": True, "role": "host"}
    row = member_key(rsvc, pk(me))
    assert row is not None and row["tier"] == "member"
    full = rsvc.odoo.search_read(
        "belfry.pass",
        [["keycast_pubkey", "=", pk(me)], ["state", "=", "active"]],
        ["tier", "date_end"],
    )
    assert [x["tier"] for x in full] == ["member"]  # the guest pass closed
    assert full[0]["date_end"] == (dt.date.today() + dt.timedelta(days=365)).isoformat()
    assert pk(me) in core.active_pubkeys(rsvc)
    assert roles.role_of(rsvc, pk(me)) == "host"
    told = [t for k, t in published if k == 1985]
    assert any(["l", "member", "belfry/guest"] in t for t in told)
    assert not any(k == 9001 for k, _t in published)  # still in the channel


def test_a_member_link_used_by_someone_who_has_an_account(world):
    rsvc, published, _k, ada, ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    bot = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        call(c, ada, "/roles", {"label": "Host"})
        call(c, ada, "/member-roles", {"pubkey": pk(bob), "role": "host"})
        body = {"channel": "garden", "as": "member", "role": "admin"}
        token = call(c, ada, "/channel-invites", {**body, "role": "member", "people": 50}).json()
        token = token["url"].rsplit("/", 1)[1]
        # a member: the channel, their role unchanged
        r = _join(c, token, bob)
        assert r.status_code == 200 and r.json()["guest"] is False and r.json()["as"] == "member"
        assert roles.role_of(rsvc, pk(bob)) == "host"
        # an agent is refused
        row = rsvc.odoo.create_guest_invite(ada_id, pk(ada), "kitchen", 30)
        call(c, bot, f"/guest-invites/{row['url'].rsplit('/', 1)[1]}/accept", {"name": "Bot"})
        r = _join(c, token, bot)
        assert r.status_code == 403 and r.json()["error"] == "agent"
        # a guest who saved their account: a member at once
        gtoken = call(c, ada, "/channel-invites", {"channel": "kitchen", "people": 50}).json()
        gtoken = gtoken["url"].rsplit("/", 1)[1]
        gus = guests.join_new(rsvc, gtoken, "Gus", pk(PrivateKey()))
        gus_pk = gus["pubkey"]
        r = _join_as(c, rsvc, token, gus_pk)
        assert r["email_needed"] is True and member_key(rsvc, gus_pk) is None
        partner = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", gus_pk]], ["partner_id"]
        )
        rsvc.odoo.write(
            "res.partner", [core.m2o_id(partner[0]["partner_id"])], {"email": "gus@x.org"}
        )
        r = _join_as(c, rsvc, token, gus_pk)
        assert r["member"] is True and r["role"] == "member"
        assert member_key(rsvc, gus_pk)["tier"] == "member"


def _join_as(c, rsvc, token, pubkey):
    """join_account for a key the signer holds (no private key here)."""
    return guests.join_account(rsvc, token, pubkey)


def test_a_membership_with_an_end_closes_like_a_guest_pass(world, monkeypatch):
    """Passes with a last day close in the service's hourly round, whatever their tier."""
    from belfry_onboarding import followers

    rsvc, _p, _k, _ada, _ada_id, _s = world
    # no account in the (fake) signer to suspend
    monkeypatch.setattr(core, "_keycast_pubkey_for", lambda svc, row: None)
    pid = rsvc.odoo.create("res.partner", {"name": "Old Member", "email": "old@x.org"})
    pass_id = rsvc.odoo.create(
        "belfry.pass",
        {
            "partner_id": pid,
            "tier": "member",
            "state": "active",
            "keycast_pubkey": "d" * 64,
            "date_end": (dt.date.today() - dt.timedelta(days=1)).isoformat(),
        },
    )
    followers._expired_at = 0.0
    assert pass_id in followers.expire_passes(rsvc)["expired"]
    assert rsvc.odoo.read_one("belfry.pass", pass_id, ["state"])["state"] == "expired"
