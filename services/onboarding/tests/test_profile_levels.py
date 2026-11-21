"""Website profiles: a headline and links (sitesessions.py), three levels of who sees a profile
and the one-off move of everyone already here to ``private`` (villagers.py, register.py;
docs/website-accounts.md)."""

# ruff: noqa: F811
from __future__ import annotations

import json

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import community, mail, register_import, roles, sitesessions
from belfry_onboarding.app import create_app
from belfry_onboarding.register import Register

from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_site_sessions import (  # noqa: F401 — fixtures
    LEO,
    H,
    guest,
    login,
    sign_as,
    site,
)
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair  # noqa: F401 — fixtures
from .test_villagers import see, vsite  # noqa: F401 — fixtures

LINK = {"url": "https://kim.example/bread", "label": "My bread"}


def me(c, key):
    r = call(c, key, "/site/me", method="GET")
    assert r.status_code == 200, r.text
    return r.json()


def refused(c, key, body, message):
    r = call(c, key, "/site/profile", body)
    assert r.status_code == 422, (body, r.text)
    assert r.json() == {"error": "invalid", "message": message}, body


# ── the fields ──────────────────────────────────────────────────────────────────────────────
HEADLINE_BAD = "headline: one line of at most 120 characters."
LINKS_LIST = "links: a list of at most 8 links."
LINKS_SHAPE = 'links: each link is {"url", "label"?}.'
LINKS_URL = (
    "links: each url is an https:// address of at most 500 characters, with a host, "
    "without spaces or user@."
)
LINKS_LABEL = "links: a label is one line of at most 40 characters."
LINKS_TWICE = "links: the same url twice."


def test_a_headline_and_links_are_checked(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        for bad in ("x" * 121, "a\nb", "a\tb", "a\x00b", "a‎b", 12, ["a"]):
            refused(c, kim, {"headline": bad}, HEADLINE_BAD)
        assert call(c, kim, "/site/profile", {"headline": "  " + "x" * 120 + " "}).json() == {
            "ok": True,
            "changed": True,
        }
        assert me(c, kim)["headline"] == "x" * 120  # trimmed
        for bad in ("https://a.example", {"url": "https://a.example"}, None, [LINK] * 9):
            refused(c, kim, {"links": bad}, LINKS_LIST)
        nine = [{"url": f"https://a.example/{i}"} for i in range(9)]
        refused(c, kim, {"links": nine}, LINKS_LIST)
        for bad in (
            ["https://a.example"],
            [{"label": "x"}],
            [{"url": "https://a.example", "title": "x"}],
            [{"url": "https://a.example", "label": "x", "icon": "y"}],
        ):
            refused(c, kim, {"links": bad}, LINKS_SHAPE)
        for url in (
            "http://a.example",
            "https://",
            "https:///path",
            "https://user@a.example/",
            "https://user:pw@a.example/",
            "https://a.example/a b",
            "https://a.example/\n",
            "https://a.example/\x7f",
            "https://a.example:99999/",
            "https://a_b.example/",
            "https://a.example\\@b.example/",
            "ftp://a.example",
            "https://a.example/" + "x" * 483,  # 501 characters
            12,
            None,
        ):
            refused(c, kim, {"links": [{"url": url}]}, LINKS_URL)
        for label in ("x" * 41, "a\nb", 3):
            refused(c, kim, {"links": [{"url": "https://a.example", "label": label}]}, LINKS_LABEL)
        refused(c, kim, {"links": [LINK, {"url": LINK["url"]}]}, LINKS_TWICE)
        # a refused field refuses the whole call: nothing else is saved
        refused(c, kim, {"about": "I bake.", "links": [{"url": "http://a.example"}]}, LINKS_URL)
        assert me(c, kim)["about"] is None
        # the edges that are right
        eight = [{"url": "https://a.example/" + "x" * 482}] + [
            {"url": f"https://a.example/{i}", "label": "y" * 40 if i == 0 else ""} for i in range(7)
        ]
        assert len(eight[0]["url"]) == 500
        r = call(c, kim, "/site/profile", {"links": eight})
        assert r.status_code == 200, r.text
        got = me(c, kim)["links"]
        assert len(got) == 8 and got[0] == {"url": eight[0]["url"], "label": None}
        assert got[1] == {"url": "https://a.example/0", "label": "y" * 40}
        assert got[2] == {"url": "https://a.example/1", "label": None}  # "" is no label
        r = call(c, kim, "/site/profile", {"links": [{"url": "https://a.example:8443/x?y=1#z"}]})
        assert r.status_code == 200, r.text
        assert me(c, kim)["links"] == [{"url": "https://a.example:8443/x?y=1#z", "label": None}]


def test_a_guests_headline_and_links_are_kept_on_the_contact(site):
    rsvc, w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        got = me(c, kim)
        assert (got["headline"], got["links"]) == (None, [])
        body = {"headline": "Baker", "links": [LINK]}
        assert call(c, kim, "/site/profile", body).json() == {"ok": True, "changed": True}
        assert call(c, kim, "/site/profile", body).json() == {"ok": True, "changed": False}
        got = me(c, kim)
        assert (got["headline"], got["links"]) == ("Baker", [LINK])
        row = rsvc.odoo.read_one("res.partner", acct["partner"], sitesessions.GUEST_FIELDS)
        assert row["belfry_headline"] == "Baker"
        assert json.loads(row["belfry_links"]) == [LINK]  # JSON text
        assert not [e for e in w.comm.events + w.grp.events if e["kind"] == 30078]
        # "" / null / [] clear them
        r = call(c, kim, "/site/profile", {"headline": "", "links": []})
        assert r.json()["changed"] is True
        got = me(c, kim)
        assert (got["headline"], got["links"]) == (None, [])
        assert call(c, kim, "/site/profile", {"headline": None}).json()["changed"] is False
        row = rsvc.odoo.read_one("res.partner", acct["partner"], sitesessions.GUEST_FIELDS)
        assert not row["belfry_headline"] and not row["belfry_links"]


def test_a_members_headline_and_links_go_into_their_profile_event(site, monkeypatch):
    rsvc, w, _ada = site
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    monkeypatch.setattr(community, "query", lambda svc, f: w.comm.query(f, None))
    place = {"name": "Brussels", "country": "BE"}
    content = {"tags": ["solar"], "share": "city", "place": place}
    old = {"kind": 30078, "created_at": 1_700_000_000, "tags": [["d", "belfry:profile"]]}
    w.comm.events.append(sign_as(pk(bob))({**old, "content": json.dumps(content)}))
    with TestClient(create_app(rsvc)) as c:
        bk = PrivateKey()
        login(c, bk, "bob@belfry.test")
        body = {"headline": "Builds roofs", "links": [LINK]}
        assert call(c, bk, "/site/profile", body).json() == {"ok": True, "changed": True}
        new = w.comm.events[-1]
        assert new["kind"] == 30078 and new["pubkey"] == pk(bob)
        assert new["tags"] == [["d", "belfry:profile"]]
        assert json.loads(new["content"]) == {
            **content,
            "headline": "Builds roofs",
            "links": [LINK],
        }
        got = me(c, bk)
        assert (got["headline"], got["links"], got["tags"]) == ("Builds roofs", [LINK], ["solar"])
        n = len(w.comm.events)
        assert call(c, bk, "/site/profile", body).json()["changed"] is False
        assert len(w.comm.events) == n  # nothing signed for nothing
        # tags with them, one event
        r = call(c, bk, "/site/profile", {"tags": ["solar", "roofs"], "headline": "Roofer"})
        assert r.json()["changed"] is True and len(w.comm.events) == n + 1
        assert json.loads(w.comm.events[-1]["content"]) == {
            **content,
            "tags": ["solar", "roofs"],
            "headline": "Roofer",
            "links": [LINK],
        }
        # cleared: out of the event, the rest kept
        assert call(c, bk, "/site/profile", {"headline": "", "links": []}).json()["changed"]
        assert json.loads(w.comm.events[-1]["content"]) == {**content, "tags": ["solar", "roofs"]}
        # what the chat may have written that isn't right is not shown
        w.comm.events.append(
            sign_as(pk(bob))(
                {
                    **old,
                    "created_at": 1_900_000_000,
                    "content": json.dumps(
                        {"headline": "a\nb", "links": [{"url": "http://x.example"}, LINK, LINK]}
                    ),
                }
            )
        )
        got = me(c, bk)
        assert (got["headline"], got["links"]) == (None, [LINK])
        # the signer can't sign for someone with their own key
        monkeypatch.setattr(community, "member_signer", lambda svc, pid: None)
        r = call(c, bk, "/site/profile", {"headline": "x"})
        assert r.status_code == 409 and r.json()["error"] == "own_key"


def test_the_hourly_limit_covers_them(site):
    rsvc, _w, _ada = site
    mail._counts.clear()
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        codes = []
        for i in range(sitesessions.PROFILE_EDITS_PER_HOUR + 1):
            body = {"headline": f"h{i}"} if i % 2 else {"links": [{"url": f"https://a.ex/{i}"}]}
            codes.append(call(c, kim, "/site/profile", body).status_code)
        assert codes[-1] == 429 and set(codes[:-1]) == {200}, codes
    mail._counts.clear()


def test_they_follow_a_guest_into_membership(site, monkeypatch):
    rsvc, w, _ada = site
    monkeypatch.setattr(community, "query", lambda svc, f: w.comm.query(f, None))
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        body = {"tags": ["bread"], "headline": "Baker", "links": [LINK]}
        assert call(c, kim, "/site/profile", body).status_code == 200
    n = len(w.comm.events)
    assert roles.make_member(rsvc, acct["pubkey"], "member") is True
    [new] = [e for e in w.comm.events[n:] if e["kind"] == 30078]
    assert json.loads(new["content"]) == {"tags": ["bread"], "headline": "Baker", "links": [LINK]}
    row = rsvc.odoo.read_one("res.partner", acct["partner"], sitesessions.GUEST_FIELDS)
    assert row["belfry_tags"] == [] and not row["belfry_headline"] and not row["belfry_links"]


# ── the three levels on a member's page ─────────────────────────────────────────────────────
def test_a_listed_members_page_shows_only_its_public_part(vsite, monkeypatch):
    from .test_villagers import named

    rsvc, w, _ada, keycast = vsite
    monkeypatch.setattr(community, "query", lambda svc, f: w.comm.query(f, None))
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    named(rsvc, keycast, bob, "bob")
    with TestClient(create_app(rsvc)) as c:
        bk = PrivateKey()
        login(c, bk, "bob@belfry.test")
        body = {"headline": "Roofer", "links": [LINK], "tags": ["roofs"], "about": "Roofs."}
        assert call(c, bk, "/site/profile", body).status_code == 200
        r = see(c, "bob")
        assert r.json() == {
            "slug": "bob",
            "name": "Bob",
            "picture": None,
            "headline": "Roofer",
            "role": "member",
            "visibility": "listed",
            "partial": True,
        }
        for word in ("Roofs.", "roofs", "kim.example", pk(bob), "npub1"):
            assert word not in r.text.replace("Roofer", "")
        full = see(c, "bob", bob).json()
        assert full["partial"] is False and full["links"] == [LINK] and full["tags"] == ["roofs"]
        assert full["about"] == "Roofs." and full["headline"] == "Roofer"
        assert call(c, bk, "/site/profile", {"visibility": "public"}).status_code == 200
        r = see(c, "bob")
        assert r.json()["partial"] is False and r.json()["links"] == [LINK]


def test_the_website_and_a_cache_keep_signed_and_unsigned_answers_apart(vsite):
    rsvc, *_ = vsite
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        assert me(c, kim)["slug"] == "kim-cook"
        for path in ("/api/v1/villagers/kim-cook", "/api/v1/villagers"):
            r = c.get(path, headers={**H, "Origin": "https://belfry.test"})
            assert r.status_code == 200, r.text
            assert r.headers["access-control-allow-origin"] == "https://belfry.test"
            vary = [v.strip() for v in r.headers["vary"].split(",")]
            assert vary == ["Authorization", "Origin"], path


# ── everyone who was here before ────────────────────────────────────────────────────────────
def test_everyone_already_here_becomes_private_once(tmp_path, settings):
    path = tmp_path / "register.sqlite"
    reg = Register(path, settings)
    # what an older version left: contacts with nothing stored, and choices made
    old = reg._db  # the param is there already (an empty register): take it out, as before
    old.execute("DELETE FROM params WHERE key = ?", (Register.VISIBILITY_MIGRATED,))
    ids = {
        "none": reg.execute_kw("res.partner", "create", [{"name": "Ann", "email": "a@x.test"}]),
        "public": reg.execute_kw(
            "res.partner", "create", [{"name": "Pat", "belfry_profile_visibility": "public"}]
        ),
        "villagers": reg.execute_kw(
            "res.partner", "create", [{"name": "Vic", "belfry_profile_visibility": "villagers"}]
        ),
    }

    def vis(r, pid):
        return r.read_one("res.partner", pid, ["belfry_profile_visibility"])[
            "belfry_profile_visibility"
        ]

    again = Register(path, settings)  # this version's first run
    assert vis(again, ids["none"]) == "private"
    assert vis(again, ids["public"]) == "public"
    assert vis(again, ids["villagers"]) == "villagers"  # read as private
    assert again.param(Register.VISIBILITY_MIGRATED)
    # someone new after that has nothing stored: listed; and a later start changes nothing
    new = again.execute_kw("res.partner", "create", [{"name": "Neo", "email": "n@x.test"}])
    third = Register(path, settings)
    assert not vis(third, new)
    assert vis(third, ids["none"]) == "private"
    from belfry_onboarding import villagers

    assert villagers.visibility_of({"belfry_profile_visibility": vis(third, new)}) == "listed"
    assert villagers.visibility_of({"belfry_profile_visibility": "villagers"}) == "private"
    # a brand new register: nothing to move, marked done
    fresh = Register(tmp_path / "fresh.sqlite", settings)
    assert fresh.param(Register.VISIBILITY_MIGRATED)
    pid = fresh.execute_kw("res.partner", "create", [{"name": "Zoe"}])
    assert not vis(fresh, pid)


class _FakeOdoo:
    """Just enough of Odoo for register-import: one contact with a member number."""

    def search_read(self, model, domain, fields, **kw):
        if model != "res.partner":
            return []
        if domain and domain[0][0] == "id":
            return [{"id": 7, "name": "Ann", "email": "a@x.test", "belfry_member_number": "1"}]
        if domain and domain[0][0] == "belfry_member_number":
            return [{"id": 7}]
        return []


def test_people_brought_over_from_odoo_are_private(tmp_path, settings, monkeypatch):
    reg = Register(tmp_path / "register.sqlite", settings)
    fields = {"name": {"type": "char"}, "email": {"type": "char"}}
    monkeypatch.setattr(
        register_import,
        "_fields",
        lambda o, model: (
            {**fields, "belfry_member_number": {"type": "char"}} if model == "res.partner" else {}
        ),
    )
    counts = register_import.run(_FakeOdoo(), reg)
    assert counts["res.partner"] == 1
    row = reg.read_one("res.partner", 7, ["belfry_profile_visibility"])
    assert row["belfry_profile_visibility"] == "private"
    assert reg.param(Register.VISIBILITY_MIGRATED)


def test_a_guest_who_says_nothing_is_listed(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        leo = guest(c, LEO, "Leo Lee")
        assert me(c, kim)["visibility"] == me(c, leo)["visibility"] == "listed"
        row = rsvc.odoo.read_one(
            "res.partner",
            sitesessions.site_account(rsvc, pk(kim))["partner"],
            ["belfry_profile_visibility"],
        )
        assert not row["belfry_profile_visibility"]
