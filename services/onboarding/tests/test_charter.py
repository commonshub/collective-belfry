"""The charter (charter.py, iOS SPEC 19.32): the parser against the app's vector, the public
read and belfry.json's ``charter``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import apps, charter, followers
from belfry_onboarding.app import create_app

from .fakes import FakeRelay

VECTOR = Path(__file__).parent / "data" / "charter.json"
ADMIN = "a" * 64


def test_the_list_id_is_the_apps_one():
    assert charter.LIST_ID == "d93ecde52eff27c7"


def test_the_parser_matches_the_vector():
    v = json.loads(VECTOR.read_text())
    got = charter.parse(v["events"], set(v["admins"]))
    want = v["expected"]
    assert [r["id"] for r in got] == [r["id"] for r in want]
    for g, w in zip(got, want, strict=True):
        assert g["title"] == w["title"] and g["about"] == w["about"]
        assert g["order"] == w["order"] and g["text_at"] == w["textAt"]
        assert g["agreed"] == w["agreed"]
        assert g["stale"] == sorted(w["stale"])
    me = v["me"]
    assert [r["id"] for r in got if me not in r["agreed"]] == v["me_to_agree"]
    assert max(r["agreed"][me] for r in got) == v["me_agreed_at"]


def ev(i, kind, pubkey, at, tags, content=""):
    return {
        "id": f"{i:064x}",
        "kind": kind,
        "pubkey": pubkey,
        "created_at": at,
        "tags": [["t", charter.LIST_ID], ["h", "charter"], *tags],
        "content": content,
    }


def test_order_none_last_and_long_titles_cut():
    got = charter.parse(
        [
            ev(1, 2100, ADMIN, 10, [["each", "1"]], "No order"),
            ev(2, 2100, ADMIN, 20, [["each", "1"], ["order", "3"]], "x" * 400),
            ev(3, 2100, ADMIN, 30, [["each", "1"], ["order", "nope"]], "   "),  # empty: ignored
        ],
        {ADMIN},
    )
    assert [r["title"][:2] for r in got] == ["xx", "No"]
    assert len(got[0]["title"]) == 300 and got[1]["order"] is None


@pytest.fixture
def relay(monkeypatch):
    r = FakeRelay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (r, lambda t: {**t, "pubkey": "f" * 64}))
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {ADMIN})
    charter.forget()
    yield r
    charter.forget()


def test_anyone_reads_the_charter_and_belfry_json_says_it_exists(services, relay):
    with TestClient(create_app(services)) as c:
        assert c.get("/api/charter").json() == {"agreements": []}
        charter.forget()
        assert c.get("/.well-known/belfry.json").json()["charter"] is False
        relay.events += [
            ev(1, 2100, ADMIN, 10, [["each", "1"], ["order", "1"], ["about", "Be kind."]], "Care"),
            ev(2, 2100, "b" * 64, 11, [["each", "1"]], "Not an admin's"),
            ev(
                3,
                2101,
                ADMIN,
                12,
                [["e", f"{1:064x}"], ["action", "edit"], ["order", "1"]],
                "Care!",
            ),
        ]
        charter.forget()
        r = c.get("/api/charter")
        assert r.status_code == 200 and "max-age=60" in r.headers["cache-control"]
        assert r.json() == {
            "agreements": [
                {
                    "id": f"{1:064x}",
                    "title": "Care!",
                    "about": "",
                    "order": 1,
                    "added": 10,
                    "text_at": 12,
                }
            ]
        }
        assert c.get("/.well-known/belfry.json").json()["charter"] is True


def test_a_relay_that_is_down_never_blocks_belfry_json(services, relay):
    relay.fail = True
    with TestClient(create_app(services)) as c:
        r = c.get("/api/charter")
        assert r.status_code == 503 and r.json()["error"] == "unavailable"
        doc = c.get("/.well-known/belfry.json")
        assert doc.status_code == 200 and "charter" not in doc.json()
