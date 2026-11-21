"""Villagers (villagers.py, docs/website-accounts.md "Villagers: profile pages"): a slug per
account, who sees a profile, the public list, and the website's ``/<folder>/_.html`` page for
the addresses under a folder."""

# ruff: noqa: F811
from __future__ import annotations

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import community, mail, sitesessions, usernames, villagers
from belfry_onboarding.app import create_app

from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_homepage import APEX, apex, nsite  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_site_sessions import (  # noqa: F401 — fixtures
    KIM,
    LEO,
    H,
    admit,
    guest,
    login,
    site,
    village,
)
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair  # noqa: F401 — fixtures

CHANNELS = {"general", "requests", "kitchen"}


@pytest.fixture
def vsite(site, keycast, monkeypatch):
    """The website tests' belfry; only CHANNELS are channels (so other names are free)."""
    rsvc, w, ada = site
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: ch in CHANNELS)
    monkeypatch.setattr(mail, "channel_by_slug", lambda svc, slug: None)
    villagers.forget()
    villagers.forget_names()
    villagers.READS.__init__(10000, 60)
    usernames._taken.clear()
    sitesessions._ticket.clear()
    yield rsvc, w, ada, keycast
    villagers.forget()
    villagers.forget_names()
    usernames._taken.clear()
    sitesessions._ticket.clear()


def named(rsvc, keycast, key: PrivateKey, username: str) -> int:
    """The signer has USERNAME for the member whose key is KEY (as it would for a real one)."""
    [row] = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", pk(key)], ["state", "=", "active"]], ["partner_id"]
    )
    pid = row["partner_id"][0] if isinstance(row["partner_id"], list) else int(row["partner_id"])
    keycast.users[f"odoo-partner-{pid}"] = {"pubkey": pk(key), "username": username}
    villagers.forget_names()
    return pid


def me(c, key):
    r = call(c, key, "/site/me", method="GET")
    assert r.status_code == 200, r.text
    return r.json()


def see(c, slug, key=None):
    if key is None:
        return c.get(f"/api/v1/villagers/{slug}", headers=H)
    return call(c, key, f"/villagers/{slug}", method="GET")


def acct_of(rsvc, key):
    return sitesessions.site_account(rsvc, pk(key))


# ── slugs ───────────────────────────────────────────────────────────────────────────────────
def test_a_guest_gets_a_slug_from_their_name(vsite):
    rsvc, _w, ada, keycast = vsite
    named(rsvc, keycast, ada, "ada")
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        got = me(c, kim)
        assert got["slug"] == "kim-cook" and got["visibility"] == "listed"
        assert villagers.DEFAULT_VISIBILITY == "listed"
        row = rsvc.odoo.read_one("res.partner", acct_of(rsvc, kim)["partner"], ["belfry_slug"])
        assert row["belfry_slug"] == "kim-cook"
        assert me(c, kim)["slug"] == "kim-cook"  # kept, not made again
        # someone else of the same name: -2
        kim2 = guest(c, "kim2@example.org", "Kim Cook")
        assert me(c, kim2)["slug"] == "kim-cook-2"
        # no name: guest-<n>, which becomes their name when they give one
        leo = guest(c, LEO, "Guest")
        pid = acct_of(rsvc, leo)["partner"]
        first = me(c, leo)["slug"]
        assert first.startswith("guest-") and first != got["username"]
        # their own guest-<n>, never guest-<n>-2 (the reserved names are not refused to the
        # one they are meant for)
        assert first == f"guest-{pid}", first
        assert call(c, leo, "/site/profile", {"name": "Leo Lee"}).status_code == 200
        assert me(c, leo)["slug"] == "leo-lee"
        # …but a slug they chose stays when the name changes
        assert call(c, leo, "/site/profile", {"slug": "lion"}).json() == {
            "ok": True,
            "changed": True,
        }
        assert call(c, leo, "/site/profile", {"name": "Leo Leeuw"}).status_code == 200
        assert me(c, leo)["slug"] == "lion"
        assert rsvc.odoo.read_one("res.partner", pid, ["belfry_slug"])["belfry_slug"] == "lion"
        # a name that is a member's username, a channel or reserved is not anyone's default
        for i, (name, want) in enumerate(
            (("Ada", "ada-2"), ("Kitchen", "kitchen-2"), ("Admin", "admin-2"))
        ):
            k = guest(c, f"g{i}@example.org", name)
            assert me(c, k)["slug"] == want, name
        # a member's slug is their username
        k = PrivateKey()
        assert login(c, k, "admin@belfry.test").json()["role"] == "admin"
        got = me(c, k)
        assert got["slug"] == got["username"] == "ada" and got["visibility"] == "listed"


def test_a_guest_changes_their_slug(vsite):
    rsvc, _w, ada, keycast = vsite
    named(rsvc, keycast, ada, "ada")
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        leo = guest(c, LEO, "Leo Lee")
        assert (me(c, kim)["slug"], me(c, leo)["slug"]) == ("kim-cook", "leo-lee")
        for bad, reason in (
            ("", "short"),
            ("x" * 31, "long"),
            ("-kim", "chars"),
            ("kim cook", "chars"),
            ("kim/cook", "chars"),
            ("hello", "reserved"),
            ("guest-999", "reserved"),
        ):
            r = call(c, kim, "/site/profile", {"slug": bad, "about": "Not saved."})
            assert r.status_code == 422 and r.json()["error"] == "slug_invalid", bad
            assert r.json()["reason"] == reason, bad
        r = call(c, kim, "/site/profile", {"slug": 7})
        assert r.status_code == 422 and r.json()["error"] == "slug_invalid"
        # taken: another guest's, a member's username, a channel's name; nothing else is saved
        for taken in ("leo-lee", "ada", "kitchen", "Leo-Lee"):
            r = call(c, kim, "/site/profile", {"slug": taken, "about": "Not saved."})
            assert r.status_code == 409 and r.json()["error"] == "slug_taken", taken
            low = taken.lower()
            assert r.json()["suggestions"] == [f"{low}-2", f"{low}-3", f"{low}-4"]
        got = me(c, kim)
        assert got["slug"] == "kim-cook" and got["about"] is None
        # a free one, with the rest of the profile in one call
        r = call(c, kim, "/site/profile", {"slug": "@Kim", "about": "I bake."})
        assert r.status_code == 200 and r.json() == {"ok": True, "changed": True}, r.text
        got = me(c, kim)
        assert (got["slug"], got["about"]) == ("kim", "I bake.")
        assert call(c, kim, "/site/profile", {"slug": "kim"}).json() == {
            "ok": True,
            "changed": False,
        }
        # the old one is free again, the new one is not
        assert call(c, leo, "/site/profile", {"slug": "kim"}).json()["error"] == "slug_taken"
        assert call(c, leo, "/site/profile", {"slug": "kim-cook"}).status_code == 200
        # a member's slug is their username: changed in the chat
        k = PrivateKey()
        login(c, k, "admin@belfry.test")
        r = call(c, k, "/site/profile", {"slug": "queen", "about": "Not saved."})
        assert r.status_code == 409 and r.json()["error"] == "use_chat"
        assert me(c, k)["slug"] == "ada" and me(c, k)["about"] is None


def test_usernames_and_slugs_are_one_set_of_names(vsite):
    rsvc, _w, ada, keycast = vsite
    named(rsvc, keycast, ada, "ada")
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    bob_id = named(rsvc, keycast, bob, "bob")
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = acct_of(rsvc, kim)
        assert me(c, kim)["slug"] == "kim-cook"
        # nobody else gets it as a username: the check, and the change in the chat
        r = call(c, bob, "/username/check", {"username": "kim-cook"})
        assert r.json()["state"] == "taken"
        usernames._taken.clear()
        r = call(c, bob, "/username", {"username": "kim-cook"})
        assert r.status_code == 409 and r.json()["error"] == "taken"
        assert not usernames._free(rsvc, "kim-cook")
        assert not usernames._free(rsvc, "kim-cook", pk(bob))
        token = community.member_token(rsvc, bob_id)[1]
        usernames._taken.clear()
        assert usernames.give_default(rsvc, token, "Kim Cook", "0002", "kim-cook") == "bob"
        keycast.users[f"odoo-partner-{bob_id}"]["username"] = "0002"  # no name yet
        got = usernames.give_default(rsvc, token, "Kim Cook", "0002", mine=pk(bob))
        assert got == "kim-cook-2"
        # the guest's own slug is no obstacle for the guest: asked with their own key…
        usernames._taken.clear()
        assert usernames._free(rsvc, "kim-cook", acct["pubkey"])
        assert usernames.check(rsvc, acct["pubkey"], {"username": "kim-cook"})["state"] == (
            "available"
        )
        # …and when they become a member, their name is theirs to have
        new = rsvc.odoo.create("belfry.pass", {"partner_id": acct["partner"], "tier": "member"})
        token = community.member_token(rsvc, acct["partner"])[1]
        got = usernames.give_default(rsvc, token, "Kim Cook", "0003", mine=acct["pubkey"])
        assert got == "kim-cook"
        rsvc.odoo.write("belfry.pass", [new], {"state": "active", "keycast_pubkey": acct["pubkey"]})
        villagers.forget_names()
        got = me(c, kim)
        assert got["role"] == "member" and got["slug"] == got["username"] == "kim-cook"
        r = see(c, "kim-cook", kim)
        assert r.status_code == 200 and r.json()["role"] == "member" and r.json()["self"] is True
        # the contact's old slug counts for nothing now, and is taken off when it is met
        assert villagers.guest_holder(rsvc, "kim-cook") == ""
        row = rsvc.odoo.read_one("res.partner", acct["partner"], ["belfry_slug"])
        assert not row["belfry_slug"]


def test_a_guest_who_left_keeps_their_name(vsite):
    rsvc, _w, _ada, _keycast = vsite
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = acct_of(rsvc, kim)
        assert me(c, kim)["slug"] == "kim-cook"
        for p in acct["passes"]:
            rsvc.odoo.write("belfry.pass", [p["id"]], {"state": "revoked"})
        assert villagers.find(rsvc, "kim-cook") is None, "no pass, no page"
        assert villagers.guest_holder(rsvc, "kim-cook") == acct["pubkey"]
        assert not usernames._free(rsvc, "kim-cook")
        assert usernames._free(rsvc, "kim-cook", acct["pubkey"])


# ── who sees a profile ──────────────────────────────────────────────────────────────────────
PROFILE = {
    "slug",
    "name",
    "picture",
    "headline",
    "about",
    "tags",
    "links",
    "role",
    "visibility",
    "self",
    "partial",
}
PARTIAL = {"slug", "name", "picture", "headline", "role", "visibility", "partial"}
LINKS = [{"url": "https://kim.example/bread", "label": "My bread"}]


def test_who_sees_a_profile(vsite):
    rsvc, w, ada, keycast = vsite
    named(rsvc, keycast, ada, "ada")
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    named(rsvc, keycast, bob, "bob")
    village(w, ada)
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        r = call(
            c,
            kim,
            "/site/profile",
            {
                "about": "I bake.",
                "picture": "https://x.test/k.jpg",
                "tags": ["bread"],
                "headline": "Baker at the village",
                "links": LINKS,
            },
        )
        assert r.status_code == 200, r.text
        assert me(c, kim)["slug"] == "kim-cook"
        leo = guest(c, LEO, "Leo Lee")  # a guest without a ticket
        tia = guest(c, "tia@example.org", "Tia")  # a guest with one
        admit(w, acct_of(rsvc, tia)["pubkey"])
        bob_site, ada_site = PrivateKey(), PrivateKey()
        assert login(c, bob_site, "bob@belfry.test").json()["role"] == "member"
        assert login(c, ada_site, "admin@belfry.test").json()["role"] == "admin"
        stranger = PrivateKey()
        secrets = (KIM, acct_of(rsvc, kim)["pubkey"], "npub1", "nip05", "pubkey", "email")
        hidden = ("I bake.", "bread", "kim.example")  # about, tags, links

        def allowed(key, mine=False):
            r = see(c, "kim-cook", key)
            assert r.status_code == 200, r.text
            got = r.json()
            assert set(got) == PROFILE
            assert got == {
                "slug": "kim-cook",
                "name": "Kim Cook",
                "picture": "https://x.test/k.jpg",
                "headline": "Baker at the village",
                "about": "I bake.",
                "tags": ["bread"],
                "links": LINKS,
                "role": "guest",
                "visibility": got["visibility"],
                "self": mine,
                "partial": False,
            }
            assert not any(s in r.text for s in secrets)
            assert r.headers["vary"].startswith("Authorization")
            return r

        def partial(key):
            r = see(c, "kim-cook", key)
            assert r.status_code == 200, r.text
            assert r.json() == {
                "slug": "kim-cook",
                "name": "Kim Cook",
                "picture": "https://x.test/k.jpg",
                "headline": "Baker at the village",
                "role": "guest",
                "visibility": "listed",
                "partial": True,
            }
            assert not any(s in r.text for s in secrets + hidden)
            assert r.headers["vary"].startswith("Authorization")
            return r

        def refused(slug, key):
            r = see(c, slug, key)
            assert r.status_code == 403, (slug, r.text)
            assert set(r.json()) == {"error", "message"} and r.json()["error"] == "villagers_only"
            assert r.headers["cache-control"] == "no-store"
            return r

        # listed (the default): someone unsigned, a guest without a ticket, a key nobody knows
        # see the public part; the unsigned answer may be kept a moment, a signed one never
        assert partial(None).headers["cache-control"] == "public, max-age=60"
        for key in (leo, stranger):
            assert partial(key).headers["cache-control"] == "no-store"
        # …and a name nobody has is refused to them as before
        for key in (None, leo, stranger):
            refused("nobody-here", key)
            refused("not a slug!", key)
        # villagers: a guest with a ticket, a member (their own key, or a site session), an
        # admin see everything; and the owner
        for key in (tia, bob, bob_site, ada, ada_site):
            r = allowed(key)
            assert r.json()["visibility"] == "listed"
            assert r.headers["cache-control"] == "no-store"
            r = see(c, "nobody-here", key)
            assert r.status_code == 404 and r.json()["error"] == "unknown"
        allowed(kim, mine=True)
        # a signature that is sent must be right
        r = c.get("/api/v1/villagers/kim-cook", headers={**H, "Authorization": "Nostr nope"})
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"
        # private: villagers only; the same 403 as for a name nobody has
        for bad in ("friends", "", None, True, "Private"):
            r = call(c, kim, "/site/profile", {"visibility": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
            assert r.json()["message"] == "visibility: listed, private or public."
        assert call(c, kim, "/site/profile", {"visibility": "listed"}).json()["changed"] is False
        r = call(c, kim, "/site/profile", {"visibility": "private"})
        assert r.json() == {"ok": True, "changed": True}
        assert me(c, kim)["visibility"] == "private"
        for key in (None, leo, stranger):
            a, b = refused("kim-cook", key), refused("nobody-here", key)
            assert a.json() == b.json()
        for key in (tia, bob, ada_site):
            assert allowed(key).json()["visibility"] == "private"
        allowed(kim, mine=True)
        # public: anyone; only the unsigned answer may be kept a moment
        r = call(c, kim, "/site/profile", {"visibility": "public"})
        assert r.json() == {"ok": True, "changed": True}
        assert call(c, kim, "/site/profile", {"visibility": "public"}).json()["changed"] is False
        assert me(c, kim)["visibility"] == "public"
        r = allowed(None)
        assert r.json()["visibility"] == "public"
        assert r.headers["cache-control"] == "public, max-age=60"
        for key in (leo, stranger, tia, bob, ada_site):
            assert allowed(key).headers["cache-control"] == "no-store"
        allowed(kim, mine=True)
        assert see(c, "KIM-COOK").status_code == 200  # as typed
        # an unknown name still says nothing to someone who is not a villager
        for key in (None, leo, stranger):
            refused("nobody-here", key)
        # a member's page, by their username; listed by default
        r = see(c, "bob", None)
        assert r.status_code == 200 and set(r.json()) == PARTIAL
        assert (r.json()["name"], r.json()["role"]) == ("Bob", "member")
        assert set(see(c, "bob", leo).json()) == PARTIAL
        r = see(c, "bob", tia)
        assert r.status_code == 200 and set(r.json()) == PROFILE
        assert (r.json()["name"], r.json()["role"], r.json()["self"]) == ("Bob", "member", False)
        assert pk(bob) not in r.text and "bob@belfry.test" not in r.text
        assert see(c, "bob", bob_site).json()["self"] is True
        assert see(c, "ada", bob).json()["role"] == "admin"
        # a guest's name in the signer (guest-<n>) is not their page
        refused(me(c, kim)["username"], None)
        assert see(c, me(c, kim)["username"], bob).status_code == 404
        # "villagers" (older site code) is private, and reads as private
        assert call(c, kim, "/site/profile", {"visibility": "villagers"}).json()["changed"]
        assert me(c, kim)["visibility"] == "private"
        row = rsvc.odoo.read_one(
            "res.partner", acct_of(rsvc, kim)["partner"], ["belfry_profile_visibility"]
        )
        assert row["belfry_profile_visibility"] == "private"
        assert call(c, kim, "/site/profile", {"visibility": "villagers"}).json()["changed"] is False
        refused("kim-cook", None)
        # a stored "villagers" (written before the three levels) reads as private
        rsvc.odoo.write(
            "res.partner",
            [acct_of(rsvc, kim)["partner"]],
            {"belfry_profile_visibility": "villagers"},
        )
        assert me(c, kim)["visibility"] == "private"
        assert see(c, "kim-cook", bob).json()["visibility"] == "private"
        refused("kim-cook", None)
        # no pass any more: no page
        for p in acct_of(rsvc, kim)["passes"]:
            rsvc.odoo.write("belfry.pass", [p["id"]], {"state": "revoked"})
        assert see(c, "kim-cook", bob).status_code == 404


def test_a_ticket_makes_a_guest_a_villager(vsite):
    rsvc, w, ada, _keycast = vsite
    village(w, ada)
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        leo = guest(c, LEO, "Leo Lee")
        assert me(c, kim)["slug"] == "kim-cook"
        assert see(c, "kim-cook", leo).json()["partial"] is True  # listed: the public part
        assert call(c, kim, "/site/profile", {"visibility": "private"}).status_code == 200
        assert see(c, "kim-cook", leo).status_code == 403
        admit(w, acct_of(rsvc, leo)["pubkey"])
        sitesessions._ticket.clear()  # believed for two minutes otherwise
        assert see(c, "kim-cook", leo).status_code == 200
        assert see(c, "kim-cook", leo).json()["partial"] is False
        # a guest's own key (the chat's) reads as the account too
        assert villagers.is_villager(rsvc, villagers.caller(rsvc, acct_of(rsvc, leo)["pubkey"]))
        assert not villagers.is_villager(rsvc, villagers.caller(rsvc, pk(PrivateKey())))


def test_needs_the_own_register(vsite, monkeypatch):
    rsvc, *_ = vsite
    monkeypatch.setattr(sitesessions, "own", lambda svc: False)
    with TestClient(create_app(rsvc)) as c:
        for path in ("/api/v1/villagers", "/api/v1/villagers/kim"):
            r = c.get(path, headers=H)
            assert r.status_code == 409 and r.json()["error"] == "unsupported"


# ── the list ────────────────────────────────────────────────────────────────────────────────
def test_the_list_has_the_public_and_listed_profiles(vsite, monkeypatch):
    rsvc, w, ada, keycast = vsite
    monkeypatch.setattr(community, "query", lambda svc, f: w.comm.query(f, None))
    ada_pid = named(rsvc, keycast, ada, "ada")
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    named(rsvc, keycast, bob, "bob")
    with TestClient(create_app(rsvc)) as c:
        # the members are listed by default (their username is their page)
        r = c.get("/api/v1/villagers", headers=H)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=60"
        assert r.headers["vary"].startswith("Authorization")
        assert r.json() == {
            "villagers": [
                {
                    "slug": "ada",
                    "name": "Ada Admin",
                    "picture": None,
                    "headline": None,
                    "role": "admin",
                    "partial": True,
                },
                {
                    "slug": "bob",
                    "name": "Bob",
                    "picture": None,
                    "headline": None,
                    "role": "member",
                    "partial": True,
                },
            ]
        }
        rsvc.odoo.write("res.partner", [ada_pid], {"belfry_profile_visibility": "private"})
        kim = guest(c)
        leo = guest(c, LEO, "Leo Lee")
        nameless = guest(c, "noname@example.org", "Guest")
        assert me(c, leo)["slug"] == "leo-lee"
        assert me(c, nameless)["slug"].startswith("guest-")  # never in the list
        guest(c, "tom@example.org", "Tom")  # no slug made yet: not in the list either
        r = call(
            c,
            kim,
            "/site/profile",
            {
                "visibility": "public",
                "tags": ["bread"],
                "headline": "Baker",
                "links": LINKS,
                "about": "I bake.",
            },
        )
        assert r.status_code == 200, r.text
        bob_site = PrivateKey()
        login(c, bob_site, "bob@belfry.test")
        r = call(c, bob_site, "/site/profile", {"headline": "Builds roofs", "tags": ["roofs"]})
        assert r.status_code == 200, r.text
        villagers.forget()
        r = c.get("/api/v1/villagers", headers=H)
        rows = {v["slug"]: v for v in r.json()["villagers"]}
        assert rows["kim-cook"] == {
            "slug": "kim-cook",
            "name": "Kim Cook",
            "picture": None,
            "headline": "Baker",
            "tags": ["bread"],
            "links": LINKS,
            "role": "guest",
            "partial": False,
        }
        # listed: only the public part (no tags, no links)
        assert rows["bob"] == {
            "slug": "bob",
            "name": "Bob",
            "picture": None,
            "headline": "Builds roofs",
            "role": "member",
            "partial": True,
        }
        assert rows["leo-lee"]["partial"] is True
        assert not any(v.startswith("guest-") or v == "tom" for v in rows)
        assert "I bake." not in r.text  # about is never in the list
        for s in (KIM, "bob@belfry.test", pk(bob), acct_of(rsvc, kim)["pubkey"], "npub1"):
            assert s not in r.text
        # the same for someone signed (a villager too): private profiles never appear
        r2 = call(c, bob, "/villagers", method="GET")
        assert r2.json() == r.json() and r2.headers["cache-control"] == "no-store"
        # a change through this API empties the list at once
        assert call(c, kim, "/site/profile", {"slug": "baker"}).status_code == 200
        assert call(c, kim, "/site/profile", {"name": "Kim the Baker"}).status_code == 200
        got = c.get("/api/v1/villagers", headers=H).json()["villagers"]
        assert ("baker", "Kim the Baker") in [(v["slug"], v["name"]) for v in got]
        assert call(c, bob_site, "/site/profile", {"visibility": "private"}).status_code == 200
        assert call(c, leo, "/site/profile", {"visibility": "villagers"}).status_code == 200
        got = c.get("/api/v1/villagers", headers=H).json()["villagers"]
        assert "bob" not in [v["slug"] for v in got] and "leo-lee" not in [v["slug"] for v in got]
        assert "baker" in [v["slug"] for v in got]


# ── CORS ────────────────────────────────────────────────────────────────────────────────────
def test_the_website_and_its_previews_read_profiles(vsite):
    rsvc, *_ = vsite
    with TestClient(create_app(rsvc)) as c:
        for path in ("/villagers", "/villagers/kim-cook"):
            for origin in ("https://belfry.test", "https://preview.belfry.test"):
                r = c.options(
                    "/api/v1" + path,
                    headers={**H, "Origin": origin, "Access-Control-Request-Method": "GET"},
                )
                assert r.status_code == 204, (path, origin)
                assert r.headers["access-control-allow-origin"] == origin
                assert "authorization" in r.headers["access-control-allow-headers"].lower()
                assert "access-control-allow-credentials" not in r.headers
                r = c.get("/api/v1" + path, headers={**H, "Origin": origin})
                assert r.headers["access-control-allow-origin"] == origin
                assert "access-control-allow-credentials" not in r.headers
            r = c.options(
                "/api/v1" + path,
                headers={
                    **H,
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert r.status_code == 403
        r = c.options(
            "/api/v1/villagers/kim/more",
            headers={
                **H,
                "Origin": "https://preview.belfry.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 403


# ── the website's page for the addresses under a folder ─────────────────────────────────────
def test_fallback_path():
    from belfry_onboarding.homepage import fallback_path

    paths = {"/index.html": "a", "/villagers/_.html": "b", "/a/b/_.html": "c", "/logo.png": "d"}
    assert fallback_path("/villagers/kim-cook", paths) == "/villagers/_.html"
    assert fallback_path("/villagers/kim-cook/", paths) == "/villagers/_.html"
    assert fallback_path("/villagers/kim.cook", paths) == "/villagers/_.html"
    assert fallback_path("/a/b/c", paths) == "/a/b/_.html"
    for no in (
        "/villagers",
        "/villagers/",
        "/villagers/kim/more",
        "/villagers/kim//",
        "/villagers//kim",
        "/villagers/..",
        "/villagers/kim cook",
        "/villagers/" + "x" * 81,
        "/villagers/kim.png",  # a file of a kind the site has
        "/villagers/kim.PNG",
        "/a/kim",
        "/kim",  # never the root
        "/others/kim",
    ):
        assert fallback_path(no, paths) is None, no
    assert fallback_path("/kim", {"/_.html": "a"}) is None


def test_a_folders_page_answers_the_addresses_under_it(apex, client, router):
    nsite(
        client,
        apex,
        router,
        {
            "/index.html": b"<h1>Home</h1>",
            "/404.html": b"gone",
            "/logo.png": b"png",
            "/villagers/index.html": b"<h1>Everyone</h1>",
            "/villagers/_.html": b"<h1>One of us</h1>",
            "/villagers/map.html": b"<h1>Map</h1>",
            "/villagers/ada/index.html": b"<h1>Ada herself</h1>",
        },
    )
    for path in ("/villagers/kim-cook", "/villagers/kim-cook/", "/villagers/kim.cook"):
        r = client.get(path, headers=APEX)
        assert r.status_code == 200 and r.text == "<h1>One of us</h1>", path
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "public, max-age=60"
    # a file that exists wins
    assert client.get("/villagers", headers=APEX).text == "<h1>Everyone</h1>"
    assert client.get("/villagers/", headers=APEX).text == "<h1>Everyone</h1>"
    assert client.get("/villagers/map", headers=APEX).text == "<h1>Map</h1>"
    assert client.get("/villagers/ada", headers=APEX).text == "<h1>Ada herself</h1>"
    # deeper, a missing file of a kind the site has, another folder: as before
    for path in ("/villagers/kim/photos", "/villagers/kim.png", "/people/kim", "/kim"):
        r = client.get(path, headers=APEX)
        assert r.status_code == 404 and r.text == "gone", path


def test_without_the_page_nothing_changes(apex, client, router):
    nsite(
        client,
        apex,
        router,
        {"/index.html": b"<h1>Home</h1>", "/villagers/index.html": b"<h1>Everyone</h1>"},
    )
    r = client.get("/villagers/kim-cook", headers=APEX)
    assert r.status_code == 404 and r.text == "Not found"


def test_a_preview_has_the_same_page(rsvc, monkeypatch):
    from belfry_onboarding import homepage, nostr, website

    author = "11" * 32
    hp = homepage.Homepage(rsvc.settings, homepage.Config("nsite", pubkey=author))
    files = {"/index.html": b"<h1>Home</h1>", "/villagers/_.html": b"<h1>One of us</h1>"}
    hp._site = homepage.Site(
        {p: f"{i:064x}" for i, p in enumerate(files)}, ["http://b"], fetched_at=1e18, event_id="e"
    )

    async def blob(sha, servers):
        return list(files.values())[int(sha, 16)], "text/html"

    monkeypatch.setattr(hp, "blob", blob)
    monkeypatch.setattr(website, "site_of", lambda svc, pk, name: hp)
    monkeypatch.setattr(website, "_previews", {})
    rsvc.settings.nip05_domain = "belfry.test"
    with TestClient(create_app(rsvc)) as c:
        host = {"Host": "preview.belfry.test"}
        c.cookies.set(website.PREVIEW_COOKIE, nostr.hex_to_npub(author))
        r = c.get("/villagers/kim-cook", headers=host)
        assert r.status_code == 200 and r.text == "<h1>One of us</h1>"
        assert c.get("/villagers/kim/more", headers=host).status_code == 404
