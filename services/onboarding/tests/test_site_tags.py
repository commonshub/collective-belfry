"""Website accounts: the tags of a profile (sitesessions.py, docs/website-accounts.md §2), kept
where the chat keeps them for a member, on the contact for a guest."""

# ruff: noqa: F811
from __future__ import annotations

import json

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    community,
    mail,
    sitesessions,
)
from belfry_onboarding.app import create_app

from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_site_sessions import (  # noqa: F401 — fixtures
    ASK,
    KIM,
    LEO,
    VILLAGE,
    H,
    admit,
    guest,
    login,
    sign_as,
    site,
    village,
)
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair  # noqa: F401 — fixtures


# ── tags ────────────────────────────────────────────────────────────────────────────────────
def test_a_guests_tags_are_kept_on_the_contact(site):
    rsvc, w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        assert call(c, kim, "/site/me", method="GET").json()["tags"] == []
        for bad in (
            "solar",
            ["#solar"],
            ["so lar"],
            [""],
            ["x" * 31],
            ["café"],
            [1],
            [f"t{i}" for i in range(21)],
        ):
            r = call(c, kim, "/site/profile", {"tags": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        r = call(c, kim, "/site/profile", {"tags": ["Solar", "bread-making", "solar", " 3d "]})
        assert r.status_code == 200 and r.json() == {"ok": True, "changed": True}, r.text
        assert call(c, kim, "/site/me", method="GET").json()["tags"] == [
            "solar",
            "bread-making",
            "3d",
        ]
        row = rsvc.odoo.read_one("res.partner", acct["partner"], ["belfry_tags"])
        assert row["belfry_tags"] == ["solar", "bread-making", "3d"]
        assert not [e for e in w.comm.events + w.grp.events if e["kind"] == 30078]
        r = call(c, kim, "/site/profile", {"tags": ["solar", "bread-making", "3d"]})
        assert r.json() == {"ok": True, "changed": False}
        # with the rest of the profile, in one call
        r = call(c, kim, "/site/profile", {"tags": [], "about": "I bake."})
        assert r.json() == {"ok": True, "changed": True}
        me = call(c, kim, "/site/me", method="GET").json()
        assert (me["tags"], me["about"]) == ([], "I bake.")


def test_a_members_tags_are_the_chats_own_event(site, monkeypatch):
    rsvc, w, _ada = site
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    monkeypatch.setattr(community, "query", lambda svc, f: w.comm.query(f, None))
    # what the chat wrote (people.ts saveExtras), and an older one
    place = {
        "id": "gn:2800866",
        "name": "Brussels",
        "region": "Brussels Capital",
        "country": "BE",
        "lat": 50.85,
        "lon": 4.35,
    }
    content = {"tags": ["facilitation", "café", "solar_power"], "share": "city", "place": place}
    old = {"kind": 30078, "created_at": 1_700_000_000, "tags": [["d", "belfry:profile"]]}
    w.comm.events.append(sign_as(pk(bob))({**old, "content": '{"tags": ["old"]}'}))
    w.comm.events.append(
        sign_as(pk(bob))({**old, "created_at": 1_700_000_500, "content": json.dumps(content)})
    )
    # someone else's, and another app's event of Bob's: never read as his tags
    w.comm.events.append(sign_as("f" * 64)({**old, "created_at": 1_800_000_000, "content": "{}"}))
    w.comm.events.append(
        sign_as(pk(bob))({**old, "created_at": 1_800_000_000, "tags": [["d", "other"]]})
    )
    with TestClient(create_app(rsvc)) as c:
        bk = PrivateKey()
        login(c, bk, "bob@belfry.test")
        me = call(c, bk, "/site/me", method="GET").json()
        assert me["tags"] == ["facilitation", "café", "solar_power"], "as the chat shows them"
        # a tag the chat let them have stays; a new one follows the website's rule
        r = call(c, bk, "/site/profile", {"tags": ["café", "baking", "résumé"]})
        assert r.status_code == 422
        n = len(w.comm.events)
        r = call(c, bk, "/site/profile", {"tags": ["café", "Baking", "facilitation"]})
        assert r.status_code == 200 and r.json() == {"ok": True, "changed": True}, r.text
        [new] = w.comm.events[n:]
        assert new["kind"] == 30078 and new["pubkey"] == pk(bob), "signed as Bob, by the signer"
        assert new["tags"] == [["d", "belfry:profile"]]
        assert new["created_at"] > 1_700_000_500
        # only the tags changed: the place and how it is shared are as the chat left them
        assert json.loads(new["content"]) == {
            "tags": ["café", "baking", "facilitation"],
            "share": "city",
            "place": place,
        }
        assert not [e for e in w.grp.events if e["kind"] == 30078]
        assert call(c, bk, "/site/me", method="GET").json()["tags"] == [
            "café",
            "baking",
            "facilitation",
        ]
        # the same again: no event
        r = call(c, bk, "/site/profile", {"tags": ["café", "baking", "facilitation"]})
        assert r.json()["changed"] is False and len(w.comm.events) == n + 1
        # the relay can't be read: nothing is written over what the chat keeps
        mail._counts.clear()

        def down(svc, f):
            raise RuntimeError("relay down")

        monkeypatch.setattr(community, "query", down)
        r = call(c, bk, "/site/profile", {"tags": ["baking"]})
        assert r.status_code == 503 and len(w.comm.events) == n + 1


def test_a_member_with_no_tags_yet_and_someone_with_their_own_key(site, monkeypatch):
    rsvc, w, _ada = site
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    monkeypatch.setattr(community, "query", lambda svc, f: w.comm.query(f, None))
    with TestClient(create_app(rsvc)) as c:
        bk = PrivateKey()
        login(c, bk, "bob@belfry.test")
        assert call(c, bk, "/site/profile", {"tags": ["solar"]}).json()["changed"] is True
        new = w.comm.events[-1]
        assert new["tags"] == [["d", "belfry:profile"]]
        assert json.loads(new["content"]) == {"tags": ["solar"]}
        monkeypatch.setattr(community, "member_signer", lambda svc, pid: None)
        r = call(c, bk, "/site/profile", {"tags": ["wind"]})
        assert r.status_code == 409 and r.json()["error"] == "own_key"
