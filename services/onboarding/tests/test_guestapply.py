"""A guest asks to become a member from the app (guestapply.py, ``POST /api/applications``
``{"apply"}``), and the web form's charter (apply.py)."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, charter, core, followers, mailer, membership
from belfry_onboarding.app import create_app
from belfry_onboarding.places import member_key

from .fakes import FakeRelay
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures


def today(days: int = 0) -> str:
    return (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=days)).isoformat()


def guest(rsvc, keycast, inviter: str, name="Gus Guest", email="gus@example.org", end=1):
    """A guest of #garden with its own key held by the signer (as join_new makes them)."""
    key = PrivateKey()
    reg = rsvc.odoo
    pid = reg.create("res.partner", {"name": name, "email": email or False})
    reg.create(
        "belfry.pass",
        {
            "partner_id": pid,
            "tier": "guest",
            "state": "active",
            "keycast_pubkey": pk(key),
            "guest_channel": "garden",
            "guest_inviter": inviter,
            "date_start": today(-1),
            "date_end": today(end),
        },
    )
    keycast.users[f"odoo-partner-{pid}"] = {"pubkey": pk(key), "username": f"guest-{pid}"}
    keycast.status[pk(key)] = "active"
    apps.forget()
    return key, pid


@pytest.fixture
def groups(monkeypatch):
    """The groups relay, with a charter of two agreements by the admin."""
    r = FakeRelay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (r, lambda t: {**t, "pubkey": "f" * 64}))
    charter.forget()
    yield r
    charter.forget()


def agreement(relay, admin: str, n: int, title: str, at: int = 100) -> str:
    eid = f"{n:064x}"
    relay.events.append(
        {
            "id": eid,
            "kind": 2100,
            "pubkey": admin,
            "created_at": at,
            "tags": [["t", charter.LIST_ID], ["h", "charter"], ["each", "1"], ["order", str(n)]],
            "content": title,
        }
    )
    return eid


def test_a_guest_asks_and_the_admins_see_who_invited_them(world, groups):
    rsvc, _p, keycast, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    gus, _gid = guest(rsvc, keycast, pk(ada))
    unsaved, _ = guest(rsvc, keycast, pk(ada), "Guest", email="")
    with TestClient(create_app(rsvc)) as c:
        ask = {"apply": {"answer": "I fix bikes on Saturdays."}}
        r = call(c, PrivateKey(), "/applications", ask)
        assert r.status_code == 403 and r.json()["error"] == "not_guest"
        r = call(c, bob, "/applications", ask)
        assert r.status_code == 403 and r.json()["error"] == "not_guest"
        r = call(c, unsaved, "/applications", ask)
        assert r.status_code == 409 and r.json()["error"] == "guest_not_saved"
        for bad in ({"answer": " "}, {"answer": "x" * 2001}, {"answer": "ok", "agreed": ["x"]}):
            r = call(c, gus, "/applications", {"apply": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        st = call(c, gus, "/applications", {"apply": "status"}).json()
        assert st["application"] is None and st["accept"] == "admins" and st["until"]
        mailer.SENT.clear()
        r = call(c, gus, "/applications", ask)
        assert r.status_code == 200, r.text
        got = r.json()
        app = got["application"]
        assert app["state"] == "new" and app["vouches"] == 0 and app["needed"] == 2
        assert app["vouched_by"] == [] and got["accept"] == "admins"
        end = dt.date.fromisoformat(today(2))
        assert got["until"] == int(
            dt.datetime(end.year, end.month, end.day, tzinfo=dt.UTC).timestamp()
        )
        assert any("asks to become a member" in str(m["Subject"]) for m in mailer.SENT)
        again = call(c, gus, "/applications", ask)
        assert again.status_code == 409 and again.json()["error"] == "pending"
        assert again.json()["application"] == app
        # the admins' card
        rows = call(c, ada, "/applications", {"list": True}).json()["applications"]
        row = next(x for x in rows if x["id"] == app["id"])
        assert row["kind"] == "guest" and row["channels"] == ["garden"]
        assert row["inviter"] == "Ada Admin" and row["pubkey"] == pk(gus)
        assert row["email"] == "gus@example.org" and row["since"] == today(-1)
        # a member vouches; the guest sees who
        call(c, bob, "/applications", {"vouch": app["id"]})
        st = call(c, gus, "/applications", {"apply": "status"}).json()
        assert st["application"]["vouched_by"] == [{"pubkey": pk(bob), "name": "Bob"}]
        # declined: they stay a guest, and may ask again
        r = call(c, ada, "/applications", {"decide": app["id"], "approve": False})
        assert r.json()["application"]["state"] == "declined"
        assert (
            call(c, gus, "/applications", {"apply": "status"}).json()["application"]["state"]
            == "declined"
        )
        assert member_key(rsvc, pk(gus)) is None
        assert call(c, gus, "/applications", ask).status_code == 200


def test_accepting_makes_the_guest_a_member_on_the_same_key_and_ticks_the_charter(world, groups):
    rsvc, published, keycast, ada, _ada_id, _s = world
    gus, _gid = guest(rsvc, keycast, pk(ada))
    a1 = agreement(groups, pk(ada), 1, "We look after each other")
    a2 = agreement(groups, pk(ada), 2, "Quiet hours")
    with TestClient(create_app(rsvc)) as c:
        r = call(c, gus, "/applications", {"apply": {"answer": "Hi!", "agreed": [a1, a2, a2]}})
        assert r.status_code == 200, r.text
        aid = r.json()["application"]["id"]
        # an admin changes the second agreement's words after the guest ticked it
        groups.events.append(
            {
                "id": "e" * 64,
                "kind": 2101,
                "pubkey": pk(ada),
                "created_at": int(dt.datetime.now(dt.UTC).timestamp()) + 60,
                "tags": [
                    ["e", a2],
                    ["action", "edit"],
                    ["t", charter.LIST_ID],
                    ["h", "charter"],
                    ["each", "1"],
                ],
                "content": "Quiet hours after 22:00",
            }
        )
        charter.forget()
        mailer.SENT.clear()
        r = call(c, ada, "/applications", {"decide": aid, "approve": True})
        assert r.status_code == 200, r.text
        assert r.json()["application"]["state"] == "accepted"
        assert not [m for m in mailer.SENT if "invite" in str(m["Subject"]).lower()]
        assert member_key(rsvc, pk(gus))["tier"] == "member"
        tiers = [
            p["tier"]
            for p in rsvc.odoo.search_read(
                "belfry.pass",
                [["keycast_pubkey", "=", pk(gus)], ["state", "=", "active"]],
                ["tier"],
            )
        ]
        assert tiers == ["member"]
        assert any(k == 1985 and ["l", "member", "belfry/guest"] in t for k, t in published)
        ticks = [e for e in groups.events if e["kind"] == 2101 and e["pubkey"] == pk(gus)]
        # only the agreement whose words didn't change since they ticked it
        assert [e["tags"] for e in ticks] == [charter.tick_tags(a1)]
        st = call(c, gus, "/applications", {"apply": "status"}).json()
        assert st["application"]["state"] == "accepted" and st["agreed"] == [a1, a2]
        r = call(c, ada, "/applications", {"decide": aid, "approve": True})
        assert r.status_code == 409 and r.json()["error"] == "decided"


def test_vouches_accept_a_guests_request(world, groups):
    rsvc, _p, keycast, ada, _ada_id, _s = world
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    cat, _ = person(rsvc, "cat@belfry.test", "Cat")
    gus, _ = guest(rsvc, keycast, pk(bob))
    with TestClient(create_app(rsvc)) as c:
        call(c, ada, "/membership", {"accept": "vouches", "vouches": 2})
        aid = call(c, gus, "/applications", {"apply": {"answer": "Hi"}}).json()["application"]["id"]
        assert call(c, bob, "/applications", {"vouch": aid}).json()["accepted"] is False
        r = call(c, cat, "/applications", {"vouch": aid})
        assert r.json()["accepted"] is True and r.json()["application"]["state"] == "accepted"
        assert member_key(rsvc, pk(gus)) is not None


def test_an_open_request_keeps_the_guest_in_and_a_decline_gives_a_day(world, groups):
    rsvc, _p, keycast, ada, _ada_id, _s = world
    gus, gid = guest(rsvc, keycast, pk(ada), end=0)
    with TestClient(create_app(rsvc)) as c:
        aid = call(c, gus, "/applications", {"apply": {"answer": "Hi"}}).json()["application"]["id"]
        gpass = rsvc.odoo.search_read("belfry.pass", [["partner_id", "=", gid]], ["id"])[0]["id"]
        rsvc.odoo.write("belfry.pass", [gpass], {"date_end": today(-3)})  # its days ran out
        assert core.expire(rsvc) == {"expired": []}
        call(c, ada, "/applications", {"decide": aid, "approve": False})
        row = rsvc.odoo.read_one("belfry.pass", gpass, ["date_end", "guest_grace_until", "state"])
        assert row["state"] == "active" and row["date_end"] == today(1)
        assert core.expire(rsvc) == {"expired": []}
        st = call(c, gus, "/applications", {"apply": "status"}).json()
        grace = dt.datetime.fromisoformat(row["guest_grace_until"]).replace(tzinfo=dt.UTC)
        assert st["until"] == int(grace.timestamp())
        rsvc.odoo.write("belfry.pass", [gpass], {"guest_grace_until": "2000-01-01 00:00:00"})
        assert core.expire(rsvc) == {"expired": [gpass]}


def test_the_web_form_asks_for_every_agreement_and_signs_them_after_the_invite(world, groups):
    rsvc, _p, keycast, ada, _ada_id, _s = world
    a1 = agreement(groups, pk(ada), 1, "We look after each other")
    a2 = agreement(groups, pk(ada), 2, "Shared things come back")
    form = {"name": "Nora", "email": "nora@example.org", "motivation": "I fix bikes."}
    with TestClient(create_app(rsvc)) as c:
        page = c.get("/apply")
        assert "We look after each other" in page.text and f'value="{a2}"' in page.text
        r = c.post("/apply", data={**form, "agreed": [a1]})
        assert r.status_code == 400 and "tick each agreement" in r.text
        assert f'value="{a1}" required checked' in r.text  # kept as ticked
        r = c.post("/apply", data={**form, "agreed": [a1, a2, "f" * 64]})
        assert r.status_code == 200 and "Thank you" in r.text
        row = rsvc.odoo.search_read("belfry.application", [], ["agreed", "kind"])[0]
        assert row["agreed"] == [a1, a2]
        # accepted: an invite as before; once they hold the membership, their ticks are signed
        call(c, ada, "/applications", {"decide": row["id"], "approve": True})
    inv_id = rsvc.odoo.read_one("belfry.application", row["id"], ["invite_id"])["invite_id"][0]
    nora, nora_pid = person(rsvc, "nora@example.org", "Nora")
    rsvc.odoo.write("belfry.invite", [inv_id], {"used_by_partner_id": nora_pid})
    from belfry_onboarding import guestapply

    done = guestapply.after_invite(rsvc, inv_id)
    assert done == [a1, a2]
    assert [e for e in keycast.signed if e["kind"] == 2101][-1]["tags"] == charter.tick_tags(a2)
    assert guestapply.after_invite(rsvc, inv_id) == []  # once


def test_odoo_belfries_answer_unsupported(services, odoo):
    from belfry_onboarding.holder_invites import ApiError

    with pytest.raises(ApiError) as e:
        membership.applications(services, "a" * 64, {"apply": {"answer": "Hi"}})
    assert e.value.error == "unsupported"
