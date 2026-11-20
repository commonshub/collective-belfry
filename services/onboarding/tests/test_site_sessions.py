"""Website accounts (sitesessions.py, docs/website-accounts.md): a browser key becomes a site
session of an account by an e-mail code, never a signer session; the account's own page and
profile; comments and requests posted as the person; the checkout and free tickets for the
account."""

# ruff: noqa: F811
from __future__ import annotations

import hashlib
import itertools
import json
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    applyverified,
    community,
    core,
    events,
    followers,
    guests,
    mail,
    mailer,
    sales,
    sitesessions,
    webrequests,
    webthreads,
)
from belfry_onboarding.app import create_app
from belfry_onboarding.groups_members import signed

from .test_events import D
from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_offers import SITE
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair, tk  # noqa: F401 — fixtures

KIM = "kim@example.org"
LEO = "leo@example.org"
H = {"Host": "belfry.belfry.test"}
_n = itertools.count(1)


def sign_as(pubkey: str):
    """What the signer does for an account in these tests: the event, by that key."""

    def sign(t):
        e = {"created_at": int(time.time()), **t, "pubkey": pubkey}
        e["id"] = hashlib.sha256(json.dumps([next(_n), e], sort_keys=True).encode()).hexdigest()
        e["sig"] = ""
        return e

    return sign


@pytest.fixture
def site(fair, monkeypatch):
    """A belfry with a public requests channel; one groups relay for the events and the
    threads; the signer signs for whoever the register says holds a key."""
    rsvc, w, ada, *_ = fair

    def gsign(t):
        return signed(w.gkey, {"created_at": int(time.time()), **t})

    def member_signer(svc, pid):
        acct = sitesessions.account(svc, pid)
        return (acct["pubkey"], sign_as(acct["pubkey"])) if acct else None

    monkeypatch.setattr(followers, "_relay", lambda svc: (w.grp, gsign))
    monkeypatch.setattr(webthreads, "_groups_pubkey", lambda svc: pk(w.gkey))
    monkeypatch.setattr(webthreads, "sites", lambda svc, fresh=False: {"requests": dict(SITE)})
    monkeypatch.setattr(community, "member_signer", member_signer)
    rsvc.relay = w.comm
    rsvc.settings.pass_page_secret = "pass-page-secret"
    sales.save_settings(rsvc, {"requests_channel": "requests"})
    webrequests.forget()
    webrequests.PER_IP.reset()
    mail._counts.clear()
    mailer.SENT.clear()
    applyverified.PER_IP._seen.clear()
    yield rsvc, w, ada
    webrequests.forget()


def login(c, key, email, **more):
    r = call(c, key, "/login/start", {"email": email, "site": True})
    assert r.status_code == 202 and r.json() == {"sent": True}, r.text
    return call(c, key, "/login/verify", {"email": email, "code": _code(), "site": True, **more})


def guest(c, email=KIM, name="Kim Cook") -> PrivateKey:
    key = PrivateKey()
    r = login(c, key, email, name=name)
    assert r.status_code == 200 and r.json()["state"] == "signed_in", r.text
    return key


def passes(rsvc, pubkey):
    return rsvc.odoo.search_read(
        "belfry.pass",
        [["keycast_pubkey", "=", pubkey]],
        ["state", "tier", "guest_channel", "date_end", "partner_id"],
    )


# ── logging in ──────────────────────────────────────────────────────────────────────────────
def test_an_unknown_address_becomes_a_guest_of_the_requests_channel(site, keycast):
    rsvc, w, _ada = site
    k = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        # a name that is no name is refused before the code is tried
        assert call(c, k, "/login/start", {"email": KIM, "site": True}).status_code == 202
        code = _code()
        bad = {"email": KIM, "code": code, "site": True, "name": "x" * 81}
        r = call(c, k, "/login/verify", bad)
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        r = call(c, k, "/login/verify", {**bad, "name": KIM})
        assert r.status_code == 422
        wrong = "000000" if code != "000000" else "111111"
        r = call(c, k, "/login/verify", {**bad, "name": "Kim Cook", "code": wrong})
        assert r.status_code == 422 and r.json()["error"] == "wrong_code"
        assert r.json()["tries_left"] == 4
        r = call(c, k, "/login/verify", {**bad, "name": "Kim Cook"})
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["state"] == "signed_in" and got["site"] is True and got["new"] is True
        assert got["role"] == "guest" and got["name"] == "Kim Cook"
        assert set(got) == {"state", "site", "pubkey", "npub", "nip05", "name", "role", "new"}
        assert "bunker" not in json.dumps(got)
        assert keycast.bunkers == [], "the website never gets a signer session"
        assert got["pubkey"] != pk(k), "the browser key is not the account's key"
        [p] = passes(rsvc, got["pubkey"])
        assert (p["state"], p["tier"], p["guest_channel"]) == ("active", "guest", "requests")
        # they are in the channel, as a requester from the web is
        assert any(
            e["kind"] == 9000 and ["p", got["pubkey"], "member"] in e["tags"] for e in w.grp.events
        )
        # the session is a row in the register, nothing else
        row = rsvc.odoo.site_get(pk(k))
        assert row["partner_id"] == core.m2o_id(p["partner_id"])
        # the same address from another browser: the same account, not new, no name needed
        k2 = PrivateKey()
        r = login(c, k2, KIM)
        assert r.json()["pubkey"] == got["pubkey"] and "new" not in r.json()
        assert r.json()["name"] == "Kim Cook"
        me = call(c, k2, "/site/me", method="GET").json()
        assert me["email"] == KIM and me["role"] == "guest" and me["pubkey"] == got["pubkey"]
        assert sorted(s["current"] for s in me["sessions"]) == [False, True]
        assert me["tickets"] == [] and me["requests"] == [] and me["comments"] == []
        assert "bunker" not in json.dumps(me)


def test_no_name_is_guest_and_no_requests_channel_is_unknown(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        r = login(c, PrivateKey(), KIM)
        assert r.json()["name"] == "Guest" and r.json()["new"] is True
        # never a name from the address
        pid = rsvc.odoo.search_read("res.partner", [["email", "=", KIM]], ["name"])[0]
        assert pid["name"] == "Guest"
        sales.save_settings(rsvc, {"requests_channel": ""})
        k = PrivateKey()
        r = login(c, k, LEO, name="Leo")
        assert r.status_code == 200 and r.json()["state"] == "unknown" and r.json()["proof"]
        assert rsvc.odoo.site_get(pk(k)) is None
        assert not rsvc.odoo.search_read("res.partner", [["email", "=", LEO]], ["id"])


def test_a_member_and_an_admin_log_in_as_themselves(site, keycast):
    rsvc, _w, ada = site
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        r = login(c, PrivateKey(), "admin@belfry.test", name="Somebody Else")
        got = r.json()
        assert got["state"] == "signed_in" and got["role"] == "admin" and "new" not in got
        assert got["pubkey"] == pk(ada) and got["name"] == "Ada Admin"  # the name is not taken
        k = PrivateKey()
        r = login(c, k, "bob@belfry.test")
        assert r.json()["role"] == "member" and r.json()["pubkey"] == pk(bob)
        assert keycast.bunkers == []
        assert len(passes(rsvc, pk(bob))) == 1, "a member gets no guest pass"
        # an account's own key never becomes a site session
        assert call(c, bob, "/login/start", {"email": KIM, "site": True}).status_code == 202
        r = call(c, bob, "/login/verify", {"email": KIM, "code": _code(), "site": True})
        assert r.status_code == 409 and r.json()["error"] == "key_taken"
        # without "site" the chat's log-in is what it was: a signer session for this key
        k3 = PrivateKey()
        assert call(c, k3, "/login/start", {"email": "bob@belfry.test"}).status_code == 202
        r = call(c, k3, "/login/verify", {"email": "bob@belfry.test", "code": _code()})
        assert "site" not in r.json() and rsvc.odoo.site_get(pk(k3)) is None


def test_a_session_ends(site, monkeypatch):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        k = guest(c)
        assert call(c, k, "/site/me", method="GET").status_code == 200
        # a key that is no session: 401 no_session, on every /site/ path
        other = PrivateKey()
        for path, method in (
            ("/site/me", "GET"),
            ("/site/logout", "POST"),
            ("/site/profile", "POST"),
        ):
            r = call(c, other, path, {"name": "X"}, method=method)
            assert r.status_code == 401 and r.json()["error"] == "no_session", path
        assert c.get("/api/v1/site/me", headers=H).status_code == 401
        # logging out
        assert call(c, k, "/site/logout").json() == {"ok": True}
        assert call(c, k, "/site/me", method="GET").json()["error"] == "no_session"
        # 30 days without use
        k = PrivateKey()
        login(c, k, KIM)
        rsvc.odoo.site_touch(pk(k), int(time.time()) - 31 * 86400)
        assert call(c, k, "/site/me", method="GET").status_code == 401
        assert rsvc.odoo.site_get(pk(k)) is None
        # at most ten per account: the least recently used go
        keys = []
        for n in range(11):
            keys.append(PrivateKey())
            applyverified.PER_IP._seen.clear()
            mail._counts.clear()
            rsvc.odoo._db.execute("DELETE FROM records WHERE model = 'belfry.key.code'")
            login(c, keys[-1], KIM)
            rsvc.odoo.site_touch(pk(keys[-1]), int(time.time()) - 100 + n)
        assert rsvc.odoo.site_get(pk(keys[0])) is None
        assert len(rsvc.odoo.site_list(rsvc.odoo.site_get(pk(keys[-1]))["partner_id"])) == 10
        # the account loses its last pass: every session ends
        acct = sitesessions.site_account(rsvc, pk(keys[-1]))
        for p in acct["passes"]:
            core.revoke_pass(rsvc, p["id"], "test")
        assert rsvc.odoo.site_list(acct["partner"]) == []
        assert call(c, keys[-1], "/site/me", method="GET").status_code == 401


def test_a_use_renews_the_guest_pass(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        k = guest(c)
        acct = sitesessions.site_account(rsvc, pk(k))
        [p] = acct["passes"]
        full = p["date_end"]
        rsvc.odoo.write("belfry.pass", [p["id"]], {"date_end": "2026-01-01"})
        # (an expired date on an active pass: the hourly expiry has not run yet)
        rsvc.odoo.site_touch(pk(k), int(time.time()) - 120)
        assert call(c, k, "/site/me", method="GET").status_code == 200
        assert passes(rsvc, acct["pubkey"])[0]["date_end"] == full


def test_a_guest_who_becomes_a_member_keeps_the_session(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        k = guest(c)
        acct = sitesessions.site_account(rsvc, pk(k))
        new = rsvc.odoo.create("belfry.pass", {"partner_id": acct["partner"], "tier": "member"})
        rsvc.odoo.write("belfry.pass", [new], {"state": "active", "keycast_pubkey": acct["pubkey"]})
        me = call(c, k, "/site/me", method="GET").json()
        assert me["role"] == "member" and me["pubkey"] == acct["pubkey"]


# ── the profile ─────────────────────────────────────────────────────────────────────────────
def test_the_profile_is_signed_by_the_signer_and_merged(site):
    rsvc, w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        k = guest(c, name="Guest")
        acct = sitesessions.site_account(rsvc, pk(k))
        # a profile made elsewhere, with a field this API doesn't know
        w.grp.events.append(
            sign_as(acct["pubkey"])(
                {
                    "kind": 0,
                    "created_at": 1_700_000_000,
                    "tags": [],
                    "content": json.dumps(
                        {"name": "Guest", "nip05": "x@belfry.test", "lud16": "a@b.c"}
                    ),
                }
            )
        )
        for bad in (
            {},
            {"name": ""},
            {"name": "n" * 81},
            {"about": "a" * 1001},
            {"picture": "http://example.org/a.png"},
            {"picture": "javascript:alert(1)"},
            {"nip05": "admin@belfry.test"},
        ):
            r = call(c, k, "/site/profile", bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        r = call(
            c,
            k,
            "/site/profile",
            {"name": "Kim Cook", "about": "I cook.", "picture": "https://example.org/kim.png"},
        )
        assert r.status_code == 200 and r.json() == {"ok": True, "changed": True}, r.text
        latest = max(
            (e for e in w.grp.events if e["kind"] == 0 and e["pubkey"] == acct["pubkey"]),
            key=lambda e: e["created_at"],
        )
        assert json.loads(latest["content"]) == {
            "name": "Kim Cook",
            "nip05": "x@belfry.test",
            "lud16": "a@b.c",
            "about": "I cook.",
            "picture": "https://example.org/kim.png",
        }
        # the contact has the name too
        row = rsvc.odoo.read_one("res.partner", acct["partner"], ["name"])
        assert row["name"] == "Kim Cook"
        me = call(c, k, "/site/me", method="GET").json()
        assert (me["name"], me["about"]) == ("Kim Cook", "I cook.")
        assert me["picture"] == "https://example.org/kim.png"
        # the same again changes nothing; null takes a field out
        assert call(c, k, "/site/profile", {"name": "Kim Cook"}).json()["changed"] is False
        assert call(c, k, "/site/profile", {"picture": None}).json()["changed"] is True
        assert call(c, k, "/site/me", method="GET").json()["picture"] is None


def test_someone_with_their_own_key_cannot_be_signed_for(site, monkeypatch):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        k = guest(c)
        monkeypatch.setattr(community, "member_signer", lambda svc, pid: None)
        r = call(c, k, "/site/profile", {"name": "Kim"})
        assert r.status_code == 409 and r.json()["error"] == "own_key"


# ── requests and comments ───────────────────────────────────────────────────────────────────
ASK = {"product": "village-2027", "offer": "I cook for 40 people on two evenings."}


def test_a_request_is_posted_at_once_by_the_persons_own_key(site, keycast):
    rsvc, w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        mailer.SENT.clear()
        # another address than the session's
        r = call(c, kim, "/offers", {**ASK, "email": LEO})
        assert r.status_code == 409 and r.json()["error"] == "email_mismatch"
        r = call(c, kim, "/offers", {**ASK, "email": KIM.upper(), "title": "Dinner for forty"})
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["state"] == "open" and got["title"] == "Dinner for forty"
        assert got["url"] == "https://belfry.test/requests/" + got["url"].rsplit("/", 1)[1]
        assert got["my_url"].startswith(got["url"] + "?k=")
        card = next(e for e in w.grp.events if e["kind"] == 9)
        assert card["pubkey"] == acct["pubkey"], "signed as the person, not by the belfry"
        assert ["offer", got["offer"]] in card["tags"] and ["p", acct["pubkey"]] in card["tags"]
        assert not any("Confirm" in str(m["Subject"]) for m in mailer.SENT)
        f = sales.store(rsvc).offer(got["offer"])
        assert f["key"] == acct["pubkey"] and f["email"] == KIM and f["name"] == "Kim Cook"
        # theirs from any of their sessions, with no thread key
        kim2 = PrivateKey()
        login(c, kim2, KIM)
        one = call(c, kim2, "/requests/1", method="GET").json()
        assert one["me"]["author"] is True and one["author"] == {
            "name": "Kim Cook",
            "role": "guest",
        }
        assert one["viewer"] == {"role": "guest", "author": True, "can_comment": True}
        lst = call(c, kim2, "/requests", method="GET").json()
        assert lst["items"][0]["mine"] is True
        assert call(c, kim2, f"/offers/{got['offer']}", method="GET").json()["state"] == "open"
        # nobody else's
        leo = guest(c, LEO, "Leo")
        one = call(c, leo, "/requests/1", method="GET").json()
        assert "me" not in one and one["viewer"]["author"] is False
        assert "mine" not in call(c, leo, "/requests", method="GET").json()["items"][0]
        assert call(c, leo, f"/offers/{got['offer']}", method="GET").status_code == 404
        # the author edits title and text through the session
        r = call(c, kim2, "/requests/1/edit", {"title": "Dinner for fifty"})
        assert r.json() == {"ok": True, "changed": True}
        r = call(c, leo, "/requests/1/edit", {"title": "Mine now"})
        assert r.status_code == 403 and r.json()["error"] == "not_author"
        me = call(c, kim, "/site/me", method="GET").json()
        [mine] = me["requests"]
        assert (mine["number"], mine["title"], mine["state"]) == (1, "Dinner for fifty", "open")
        assert mine["url"] == got["url"] and mine["id"] == card["id"] and mine["created_at"]
        assert keycast.bunkers == []


def test_anyone_who_has_the_channel_comments_as_themselves(site, monkeypatch):
    rsvc, w, ada = site
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    # a member's name is their profile's (without one a member is "Somebody")
    profile = {"kind": 0, "created_at": 1, "tags": [], "content": '{"name": "Bob"}'}
    w.grp.events.append(sign_as(pk(bob))(profile))
    at = itertools.count(int(time.time()) - 500)

    def say(c, key, text):
        """A comment, stamped one second after the last (they are read in that order)."""
        r = call(c, key, "/requests/1/comments", {"text": text})
        if r.status_code == 201:
            w.grp.events[-1]["created_at"] = next(at)
            webrequests.forget()
        return r

    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        leo = guest(c, LEO, "Leo")
        assert call(c, kim, "/offers", ASK).status_code == 201
        leo_pk = sitesessions.site_account(rsvc, pk(leo))["pubkey"]
        r = say(c, leo, " ")
        assert r.status_code == 422
        r = say(c, leo, "Which evenings?")
        assert r.status_code == 201 and r.json() == {"ok": True}, r.text
        reply = next(e for e in w.grp.events if e["kind"] == 1111)
        assert reply["pubkey"] == leo_pk and reply["content"] == "Which evenings?"
        assert ["h", "requests"] in reply["tags"]
        # a member (their site session), and the author
        bk = PrivateKey()
        login(c, bk, "bob@belfry.test")
        assert say(c, bk, "Welcome!").status_code == 201
        assert say(c, kim, "Tuesday.").status_code == 201
        # an account's own key that is no site session is who it was: not the author
        r = say(c, bob, "As myself")
        assert r.status_code == 403 and r.json()["error"] == "not_author"
        one = call(c, leo, "/requests/1", method="GET").json()
        comments = [x for x in one["timeline"] if x["type"] == "comment"]
        assert [(x["author"]["name"], x["author"]["role"]) for x in comments] == [
            ("Leo", "guest"),
            ("Bob", "member"),
            ("Kim Cook", "author"),
        ]
        assert [x.get("mine", False) for x in comments] == [True, False, False]
        # unsigned: the same names, nothing marked
        pub = c.get("/api/v1/requests/1", headers=H).json()
        assert "viewer" not in pub and not any("mine" in x for x in pub["timeline"])
        # Leo edits his own comment, nobody else's
        mine_id, bobs_id = comments[0]["id"], comments[1]["id"]
        r = call(c, leo, "/requests/1/edit", {"comment": mine_id, "text": "Which two evenings?"})
        assert r.status_code == 200 and r.json()["changed"] is True, r.text
        x = [e for e in w.grp.events if e["kind"] == 1010][-1]
        assert x["pubkey"] == leo_pk and ["e", mine_id] in x["tags"]
        r = call(c, leo, "/requests/1/edit", {"comment": bobs_id, "text": "Hacked"})
        assert r.status_code == 403 and r.json()["error"] == "not_yours"
        me = call(c, leo, "/site/me", method="GET").json()
        [mine] = me["comments"]
        assert mine["request"]["number"] == 1 and mine["text"] == "Which two evenings?"
        assert mine["id"] == mine_id and me["requests"] == []
        # a new name shows on what they wrote before
        assert call(c, leo, "/site/profile", {"name": "Leo Lion"}).status_code == 200
        pub = c.get("/api/v1/requests/1", headers=H).json()
        assert pub["timeline"][0]["author"]["name"] == "Leo Lion"
        # 20 an hour per account
        monkeypatch.setattr(webrequests, "COMMENTS_PER_HOUR", 1)
        mail._counts.clear()
        assert say(c, leo, "One").status_code == 201
        r = say(c, leo, "Two")
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        # a member who can't read the channel (a closed one they are not in) may not
        monkeypatch.setattr(guests, "_closed", lambda svc, ch: True)
        r = say(c, bk, "Still here?")
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        # the admin always may
        ak = PrivateKey()
        login(c, ak, "admin@belfry.test")
        assert say(c, ak, "Noted.").status_code == 201
        assert [e for e in w.grp.events if e["kind"] == 1111][-1]["pubkey"] == pk(ada)


# ── the checkout ────────────────────────────────────────────────────────────────────────────
def test_a_free_order_opens_at_once_on_the_account(site, keycast):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        mailer.SENT.clear()
        r = call(c, kim, "/checkout", {"product": "friend", "email": LEO})
        assert r.status_code == 409 and r.json()["error"] == "email_mismatch"
        r = call(c, kim, "/checkout", {"product": "friend"})
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["state"] == "active" and "bunker_url" not in got
        o = sales.store(rsvc).order(got["order"])
        assert o["key"] == acct["pubkey"] and o["email"] == KIM and o["name"] == "Kim Cook"
        assert o["partner_id"] == acct["partner"] and not o.get("bunker_url")
        assert not any("Confirm" in str(m["Subject"]) for m in mailer.SENT)
        tiers = sorted(p["tier"] for p in passes(rsvc, acct["pubkey"]) if p["state"] == "active")
        assert tiers == ["guest", "member"]
        # the order from any session of the account; not from anyone else's
        k2 = PrivateKey()
        login(c, k2, KIM)
        r = call(c, k2, f"/checkout/{got['order']}", method="GET")
        assert r.status_code == 200 and "bunker_url" not in r.json()
        leo = guest(c, LEO, "Leo")
        assert call(c, leo, f"/checkout/{got['order']}", method="GET").status_code == 404
        assert keycast.bunkers == []
        # a browser key with no session: the confirmation by mail, as before
        r = call(c, PrivateKey(), "/checkout", {"product": "friend", "email": "new@example.org"})
        assert r.status_code == 201 and r.json()["state"] == "confirm"


def test_the_admins_endpoints_know_no_site_session(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        ak = PrivateKey()
        assert login(c, ak, "admin@belfry.test").json()["role"] == "admin"
        r = call(c, ak, "/codes", {"percent": 100, "product": "friend"})
        assert r.status_code == 403 and r.json()["error"] == "not_admin"
        r = call(c, ak, "/offers/f_0123456789abcdef/decide", {"percent": 100})
        assert r.status_code == 403
        assert call(c, ak, "/me", method="GET").json()["member"] is False
        assert call(c, ak, "/whois", {"pubkeys": []}).status_code == 403
        assert call(c, ak, "/roles", {"label": "Host"}).status_code in (401, 403)
        r = call(c, ak, "/me/sessions", method="GET")
        assert r.status_code == 403 and r.json()["error"] == "no_pass"


# ── tickets ─────────────────────────────────────────────────────────────────────────────────
VILLAGE = "2026-vill4g"


def village(w, host):
    """A second event (the one a ticket needs) with a pass and a kids' ticket."""
    start = int(time.time()) + 20 * 86400
    w.put(host, 9007, [["h", "ev-" + VILLAGE]])
    w.admins("ev-" + VILLAGE, [pk(host)])
    w.put(
        host,
        31923,
        [
            ["d", VILLAGE],
            ["title", "The village"],
            ["start", str(start)],
            ["end", str(start + 86400)],
            ["visibility", "public"],
        ],
    )
    for tid, title in (("pass", "Village pass"), ("kids", "Kids pass")):
        w.put(
            host,
            30402,
            [["d", f"{VILLAGE}:{tid}"], ["a", f"31923:{pk(host)}:{VILLAGE}"], ["title", title]],
        )


def admit(w, pubkey, ticket=f"{VILLAGE}:pass", *more):
    w.put(
        w.gkey,
        9000,
        [["h", "ev-" + VILLAGE], ["p", pubkey, "member"], ["ticket", ticket], *more],
    )


def test_a_ticket_that_needs_another_ticket(site):
    rsvc, w, ada = site
    village(w, ada)
    tk(w, ada, "day", "Day ticket", ["requires", f"31923:{pk(ada)}:{VILLAGE}", f"{VILLAGE}:pass"])
    tk(w, ada, "open", "Open ticket")
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        need = {
            "event_link": VILLAGE,
            "event_title": "The village",
            "ticket": f"{VILLAGE}:pass",
            "ticket_title": "Village pass",
        }
        got = call(c, kim, f"/events/{D}", method="GET").json()
        day, opn = (next(t for t in got["tickets"] if t["id"] == i) for i in ("day", "open"))
        assert day["requires"] == need and day["eligible"] is False
        assert opn["requires"] is None and opn["eligible"] is True
        pub = c.get(f"/api/v1/events/{D}", headers=H).json()["tickets"]
        assert pub[0]["requires"] == need and "eligible" not in pub[0]
        # not without the pass: the site session's account, a member, a stranger
        r = call(c, kim, f"/events/{D}/join", {"ticket": "day"})
        assert r.status_code == 409 and r.json()["error"] == "requires", r.text
        assert {k: r.json().get(k) for k in ("event_link", "event_title", "ticket_title")} == {
            "event_link": VILLAGE,
            "event_title": "The village",
            "ticket_title": "Village pass",
        }
        r = call(c, PrivateKey(), f"/events/{D}/join", {"ticket": "day", "name": "Sam"})
        assert r.status_code == 409 and r.json()["error"] == "requires"
        assert call(c, ada, f"/events/{D}/join", {"ticket": "day"}).json()["error"] == "requires"
        # another ticket of the village is not the one
        admit(w, acct["pubkey"], f"{VILLAGE}:kids")
        assert call(c, kim, f"/events/{D}/join", {"ticket": "day"}).status_code == 409
        # a test admission counts only for a tester
        admit(w, acct["pubkey"], f"{VILLAGE}:pass", ["test", "1"])
        w.grp.events[-1]["created_at"] += 1
        assert call(c, kim, f"/events/{D}/join", {"ticket": "day"}).status_code == 409
        # with the pass: the account takes it (no new guest, no session)
        admit(w, acct["pubkey"])
        w.grp.events[-1]["created_at"] += 2
        got = call(c, kim, f"/events/{D}", method="GET").json()
        assert next(t for t in got["tickets"] if t["id"] == "day")["eligible"] is True
        r = call(c, kim, f"/events/{D}/join", {"ticket": "day"})
        assert r.status_code == 200, r.text
        assert r.json() == {"ticket": f"{D}:day", "pubkey": acct["pubkey"], "new": False}
        ev = events.load(rsvc, D)
        assert events.people(rsvc, ev).holders == {acct["pubkey"]: f"{D}:day"}
        # taken out of the village since: no longer
        w.put(w.gkey, 9001, [["h", "ev-" + VILLAGE], ["p", acct["pubkey"]]])
        w.grp.events[-1]["created_at"] += 3
        t = next(t for t in events.tickets(rsvc, ev) if t.id == "day")
        assert events.required(rsvc, t, acct["pubkey"])[1] is False
        # their page lists the ticket they hold
        me = call(c, kim, "/site/me", method="GET").json()
        [mine] = me["tickets"]
        assert (mine["event_link"], mine["event_title"]) == (D, "Garden day")
        assert (mine["ticket"], mine["ticket_title"]) == (f"{D}:day", "Day ticket")
        assert mine["ticket_url"].startswith(f"https://chat.belfry.test/events/{D}/t/")
        assert mine["checked_in"] is None and mine["test"] is False
        # an application is the person's own signed request: not from a site session
        r = call(c, kim, f"/events/{D}/apply", {"ticket": "open"})
        assert r.status_code == 409 and r.json()["error"] == "use_chat"


def test_an_unreadable_requires_tag_admits_nobody(site):
    rsvc, w, ada = site
    village(w, ada)
    tk(w, ada, "day", "Day ticket", ["requires", "the village"])
    tk(w, ada, "two", "Two", ["requires", VILLAGE], ["requires", VILLAGE])
    tk(w, ada, "any", "Any village ticket", ["requires", VILLAGE])
    tk(w, ada, "gone", "Gone", ["requires", VILLAGE, "nope"])
    admit(w, pk(ada), f"{VILLAGE}:kids")
    with TestClient(create_app(rsvc)) as c:
        for tid in ("day", "two", "gone"):
            r = call(c, ada, f"/events/{D}/join", {"ticket": tid})
            assert r.status_code == 409 and r.json()["error"] == "requires", tid
        got = call(c, ada, f"/events/{D}", method="GET").json()
        day = next(t for t in got["tickets"] if t["id"] == "day")
        assert day["requires"] == {"event_link": None, "event_title": None}
        assert day["eligible"] is False
        # the event alone (its d): any ticket of it
        r = call(c, ada, f"/events/{D}/join", {"ticket": "any"})
        assert r.status_code == 200 and r.json()["ticket"] == f"{D}:any"


def test_the_checkout_and_a_request_ask_for_the_needed_ticket_too(site):
    rsvc, w, ada = site
    village(w, ada)
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["requires", VILLAGE])
    sales.save_settings(rsvc, {"tickets": True})
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        body = {"ticket": f"{D}:day"}
        r = call(c, kim, "/checkout", body)
        assert r.status_code == 409 and r.json()["error"] == "requires", r.text
        assert r.json()["event_title"] == "The village"
        # a browser key nobody knows holds nothing
        r = call(c, PrivateKey(), "/checkout", {**body, "name": "Sam", "email": "sam@example.org"})
        assert r.status_code == 409 and r.json()["error"] == "requires"
        r = call(c, kim, "/offers", {**body, "offer": "I bake."})
        assert r.status_code == 409 and r.json()["error"] == "requires"
        got = call(c, kim, f"/events/{D}", method="GET").json()["tickets"][0]
        assert got["can_pay"] is False and got["eligible"] is False
        admit(w, acct["pubkey"])
        got = call(c, kim, f"/events/{D}", method="GET").json()["tickets"][0]
        assert got["can_pay"] is True and got["eligible"] is True
        r = call(c, kim, "/checkout", body)
        assert r.status_code == 201 and r.json()["state"] == "pending", r.text
        assert "bunker_url" not in r.json()
        r = call(c, kim, "/offers", {**body, "offer": "I bake."})
        assert r.status_code == 201 and r.json()["state"] == "open"


# ── CORS ────────────────────────────────────────────────────────────────────────────────────
def test_the_website_and_its_previews_reach_the_account_endpoints(site):
    rsvc, _w, _ada = site

    def preflight(c, path, origin):
        return c.options(
            "/api/v1" + path,
            headers={**H, "Origin": origin, "Access-Control-Request-Method": "POST"},
        )

    with TestClient(create_app(rsvc)) as c:
        for path in ("/login/start", "/login/verify", "/site/me", "/site/profile", "/site/logout"):
            for origin in ("https://belfry.test", "https://preview.belfry.test"):
                r = preflight(c, path, origin)
                assert r.status_code == 204, (path, origin)
                assert r.headers["access-control-allow-origin"] == origin
                assert "access-control-allow-credentials" not in r.headers
            assert preflight(c, path, "https://evil.example").status_code == 403
        # a preview never comments, edits, or reaches anything else
        for path in ("/requests/1/comments", "/requests/1/edit", "/site/other", "/roles"):
            assert preflight(c, path, "https://preview.belfry.test").status_code == 403, path
        assert preflight(c, "/requests/1/comments", "https://belfry.test").status_code == 204
