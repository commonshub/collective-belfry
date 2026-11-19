"""Applying and logging in from the chat with a proven address and a browser key
(applyverified.py): /api/v1/apply/start|verify|status, /api/v1/login/start|verify."""

# ruff: noqa: F811
from __future__ import annotations

import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import applyverified, mailer
from belfry_onboarding.app import create_app
from belfry_onboarding.places import member_key

from .test_guestapply import agreement, groups  # noqa: F401 — fixtures
from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures

NORA = "nora@example.org"
WHY = "I fix bikes on Saturdays."


@pytest.fixture(autouse=True)
def fresh_limits():
    applyverified.PER_IP._seen.clear()
    yield
    applyverified.PER_IP._seen.clear()


@pytest.fixture
def belfry(world, authentik, groups, monkeypatch):
    """A belfry with its own sign-in, an admin (Ada) and a charter of one agreement."""
    from belfry_onboarding import mail

    rsvc = world[0]
    # the world's relay stand-in calls every word a channel: no name would be free
    monkeypatch.setattr(mail, "channel_id", lambda svc, name: None)
    rsvc.settings.pass_signin = "belfry"
    rsvc.settings.pass_page_secret = "pass-page-secret"
    rule = agreement(groups, pk(world[3]), 1, "We look after each other")
    return rsvc, world[2], world[3], rule, groups


def bound_keys(rsvc, monkeypatch):
    """Every session the signer is asked for: (label, the client key it is bound to)."""
    bound = []
    real = rsvc.keycast.create_bunker

    def create(token, label, client_pubkey=None):
        bound.append((label, client_pubkey))
        return real(token, label, client_pubkey)

    monkeypatch.setattr(rsvc.keycast, "create_bunker", create)
    return bound


def to(address):
    return [m for m in mailer.SENT if m["To"] == address]


def subjects(address):
    return [str(m["Subject"]) for m in to(address)]


def apps_of(rsvc):
    return rsvc.odoo.search_read(
        "belfry.application", [], ["email", "state", "verified", "client", "name", "agreed"]
    )


def apply(c, key, rule, email=NORA, **more):
    """Start and verify: the applicant proves the address with the mailed code."""
    r = call(c, key, "/apply/start", {"email": email, "motivation": WHY, "agreed": [rule], **more})
    assert r.status_code == 202 and r.json() == {"sent": True}, r.text
    return call(c, key, "/apply/verify", {"email": email, "code": _code()})


def test_start_checks_the_form_and_mails_a_code_to_any_address(belfry):
    rsvc, _k, _ada, rule, _g = belfry
    nora = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        mailer.SENT.clear()
        r = call(c, nora, "/apply/start", {"email": NORA, "motivation": " ", "agreed": [rule]})
        assert r.status_code == 400 and r.json()["error"] == "missing"
        r = call(c, nora, "/apply/start", {"motivation": WHY, "agreed": [rule]})
        assert r.status_code == 400 and r.json()["error"] == "missing"
        r = call(c, nora, "/apply/start", {"email": NORA, "motivation": WHY, "agreed": []})
        assert r.status_code == 400 and r.json()["error"] == "unticked"
        r = call(c, nora, "/apply/start", {"email": "nope", "motivation": WHY, "agreed": [rule]})
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        # only bots fill in the hidden field: the same answer, nothing sent
        bot = {"email": NORA, "motivation": WHY, "agreed": [rule], "website": "x"}
        r = call(c, nora, "/apply/start", bot)
        assert r.status_code == 202 and r.json() == {"sent": True}
        assert not mailer.SENT and not apps_of(rsvc)
        # unsigned: nothing
        assert (
            c.post("/api/apply/start", json={"email": NORA, "motivation": WHY}).status_code == 401
        )
        # an address nobody knows and a member's address: the same answer, a code to each
        for address in (NORA, "admin@belfry.test"):
            r = call(
                c, nora, "/apply/start", {"email": address, "motivation": WHY, "agreed": [rule]}
            )
            assert r.status_code == 202 and r.json() == {"sent": True}
            [mail] = to(address)
            text = mail.get_body(preferencelist=("plain",)).get_content()
            assert f"Your code to apply to {rsvc.settings.community_name}: {_code()}" in text
        assert not apps_of(rsvc), "nothing is stored before the address is proven"


def test_too_many_codes_from_one_place(belfry, monkeypatch):
    rsvc, _k, _ada, rule, _g = belfry
    monkeypatch.setattr(applyverified, "PER_IP", applyverified.Limiter(2, 3600))
    with TestClient(create_app(rsvc)) as c:
        for n in range(2):
            body = {"email": f"n{n}@example.org", "motivation": WHY, "agreed": [rule]}
            assert call(c, PrivateKey(), "/apply/start", body).status_code == 202
        r = call(c, PrivateKey(), "/login/start", {"email": "x@example.org"})
        assert r.status_code == 429 and r.json()["error"] == "too_many"


def test_the_code_stores_the_application_once_and_tells_the_admins_once(belfry):
    rsvc, _k, ada, rule, _g = belfry
    nora = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        mailer.SENT.clear()
        r = call(c, nora, "/apply/verify", {"email": NORA, "code": "123456"})
        assert r.status_code == 410 and r.json()["error"] == "expired"  # no code was asked
        start = {"email": NORA, "motivation": WHY, "agreed": [rule], "name": "Nora New"}
        assert call(c, nora, "/apply/start", start).status_code == 202
        code = _code()
        wrong = "000000" if code != "000000" else "111111"
        r = call(c, nora, "/apply/verify", {"email": NORA, "code": wrong})
        assert r.status_code == 422 and r.json()["error"] == "wrong_code"
        assert r.json()["tries_left"] == 4
        # the code is this key's: another key can't use it
        r = call(c, PrivateKey(), "/apply/verify", {"email": NORA, "code": code})
        assert r.status_code == 410 and not apps_of(rsvc)
        r = call(c, nora, "/apply/verify", {"email": NORA, "code": code})
        assert r.status_code == 200 and r.json() == {"state": "pending"}, r.text
        [row] = apps_of(rsvc)
        assert row["state"] == "new" and row["verified"] is True and row["client"] == pk(nora)
        assert (row["name"], row["email"], row["agreed"]) == ("Nora New", NORA, [rule])
        asked = [s for s in subjects("admin@belfry.test") if "asks to join" in s]
        assert asked == [f"{rsvc.settings.community_name}: Nora New asks to join"]
        # nothing but the code went to the applicant
        assert len(to(NORA)) == 1
        # the code works once
        r = call(c, nora, "/apply/verify", {"email": NORA, "code": code})
        assert r.status_code == 410
        # again from the same key, and from another browser with the same address: the same
        # answer, no second request, the admins not told again
        assert apply(c, nora, rule).json() == {"state": "pending"}
        other = PrivateKey()
        assert apply(c, other, rule).json() == {"state": "pending"}
        [row] = apps_of(rsvc)
        assert row["client"] == pk(other) and row["name"] == "Nora New"
        assert len([s for s in subjects("admin@belfry.test") if "asks to join" in s]) == 1
        # the admins' list says the address is proven
        [item] = call(c, ada, "/applications", {"list": True}).json()["applications"]
        assert item["verified"] is True and item["email"] == NORA and item["kind"] == "visitor"
        assert "client" not in item


def test_an_address_that_has_an_account_is_signed_in_on_the_applicant_key(belfry, monkeypatch):
    rsvc, _k, _ada, rule, _g = belfry
    bound = bound_keys(rsvc, monkeypatch)
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        r = apply(c, me, rule, email="admin@belfry.test")
        got = r.json()
        assert r.status_code == 200 and got["state"] == "signed_in", got
        assert got["bunker_url"].startswith("bunker://") and got["npub"].startswith("npub1")
        assert got["nip05"].endswith("@belfry.test") and len(got["pubkey"]) == 64
        assert bound == [("Chat", pk(me))]
        assert not apps_of(rsvc)


def test_an_account_with_its_own_key_is_told_to_log_in(belfry, monkeypatch):
    rsvc, _k, _ada, rule, _g = belfry
    from belfry_onboarding import guests

    monkeypatch.setattr(guests, "_sign_in_as", lambda *a: None)
    monkeypatch.setattr(guests, "_account_nip05", lambda svc, pid: "ada@belfry.test")
    with TestClient(create_app(rsvc)) as c:
        r = apply(c, PrivateKey(), rule, email="admin@belfry.test")
        assert r.status_code == 409 and r.json()["error"] == "taken"
        assert r.json()["nip05"] == "ada@belfry.test" and "bunker_url" not in r.json()


def test_status_and_an_approval_that_makes_a_member_at_once(belfry, monkeypatch):
    rsvc, _k, ada, rule, groups = belfry
    bound = bound_keys(rsvc, monkeypatch)
    nora, stranger = PrivateKey(), PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        assert call(c, nora, "/apply/status", {}).json() == {"state": "none"}
        assert apply(c, nora, rule, name="Nora New").json() == {"state": "pending"}
        assert call(c, nora, "/apply/status", {}).json() == {"state": "pending"}
        assert call(c, stranger, "/apply/status", {}).json() == {"state": "none"}
        assert bound == [], "no signer call while the request waits"
        [row] = apps_of(rsvc)
        mailer.SENT.clear()
        r = call(c, ada, "/applications", {"decide": row["id"], "approve": True})
        assert r.status_code == 200, r.text
        assert r.json()["application"]["state"] == "accepted"
        # a member at once: no invite, one mail (the welcome), the default role, a username
        assert rsvc.odoo.search_read("belfry.invite", [], ["id"]) == []
        [welcome] = to(NORA)
        text = welcome.get_body(preferencelist=("plain",)).get_content()
        assert welcome["Subject"] == f"Welcome to {rsvc.settings.community_name}"
        assert "chat.belfry.test" in text and "/signin/" not in text and "/invite/" not in text
        assert "nora-new@belfry.test" in text
        [p] = rsvc.odoo.search_read(
            "belfry.pass", [["partner_id.email", "=", NORA]], ["state", "tier", "keycast_pubkey"]
        )
        assert p["state"] == "active" and p["tier"] == "member"
        assert member_key(rsvc, p["keycast_pubkey"]) is not None
        # the agreement they ticked is signed for them
        ticks = [
            e for e in groups.events if e["kind"] == 2101 and e["pubkey"] == p["keycast_pubkey"]
        ]
        assert [["e", rule] in e["tags"] for e in ticks] == [True]
        # answered once
        r = call(c, ada, "/applications", {"decide": row["id"], "approve": True})
        assert r.status_code == 409 and r.json()["error"] == "decided"
        # the browser that waits gets its session, bound to its own key; nobody else does
        assert call(c, stranger, "/apply/status", {}).json() == {"state": "none"}
        got = call(c, nora, "/apply/status", {}).json()
        assert got["state"] == "approved" and got["signed_in"] is True, got
        assert got["bunker_url"].startswith("bunker://") and got["pubkey"] == p["keycast_pubkey"]
        assert got["nip05"] == "nora-new@belfry.test" and got["npub"].startswith("npub1")
        assert bound == [("Chat", pk(nora))]
        # asked again (the answer was lost, or it was fetched before): they log in
        again = call(c, nora, "/apply/status", {})
        assert again.status_code == 200
        assert again.json() == {
            "state": "approved",
            "signed_in": False,
            "nip05": "nora-new@belfry.test",
        }
        assert len(bound) == 1
        assert len(to(NORA)) == 1, "one mail to the applicant"


def test_a_key_the_signer_knows_already_gets_no_second_session(belfry, monkeypatch):
    from belfry_onboarding.clients.keycast import ClientKeyTaken

    rsvc, _k, ada, rule, _g = belfry
    nora = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        apply(c, nora, rule)
        [row] = apps_of(rsvc)
        call(c, ada, "/applications", {"decide": row["id"], "approve": True})

        def taken(token, label, client_pubkey=None):
            raise ClientKeyTaken(client_pubkey)

        monkeypatch.setattr(rsvc.keycast, "create_bunker", taken)
        r = call(c, nora, "/apply/status", {})
        assert r.status_code == 200 and r.json()["state"] == "approved"
        assert r.json()["signed_in"] is False and r.json()["nip05"].endswith("@belfry.test")


def test_a_declined_request_says_so_and_they_may_ask_again(belfry):
    rsvc, _k, ada, rule, _g = belfry
    nora = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        apply(c, nora, rule)
        [row] = apps_of(rsvc)
        mailer.SENT.clear()
        r = call(c, ada, "/applications", {"decide": row["id"], "approve": False})
        assert r.json()["application"]["state"] == "declined"
        assert subjects(NORA) == [f"Your request to join {rsvc.settings.community_name}"]
        assert call(c, nora, "/apply/status", {}).json() == {"state": "declined"}
        assert not rsvc.odoo.search_read("belfry.pass", [["partner_id.email", "=", NORA]], ["id"])
        assert apply(c, nora, rule).json() == {"state": "pending"}
        assert call(c, nora, "/apply/status", {}).json() == {"state": "pending"}
        assert [a["state"] for a in apps_of(rsvc)] == ["declined", "new"]


def test_vouches_accept_a_proven_request_the_same_way(belfry):
    rsvc, _k, ada, rule, _g = belfry
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    nora = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        call(c, ada, "/membership", {"accept": "vouches", "vouches": 1})
        apply(c, nora, rule)
        [row] = apps_of(rsvc)
        r = call(c, bob, "/applications", {"vouch": row["id"]})
        assert r.json()["accepted"] is True and r.json()["application"]["state"] == "accepted"
        assert rsvc.odoo.search_read("belfry.invite", [], ["id"]) == []
        assert call(c, nora, "/apply/status", {}).json()["state"] == "approved"


def test_the_old_form_still_ends_in_an_invite(belfry):
    rsvc, _k, ada, rule, _g = belfry
    with TestClient(create_app(rsvc)) as c:
        body = {"email": "olga@example.org", "motivation": WHY, "agreed": [rule]}
        assert c.post("/api/apply", json=body).json() == {"ok": True}
        [row] = apps_of(rsvc)
        assert not row.get("verified") and not row.get("client")
        [item] = call(c, ada, "/applications", {"list": True}).json()["applications"]
        assert item["verified"] is False
        mailer.SENT.clear()
        r = call(c, ada, "/applications", {"decide": row["id"], "approve": True})
        assert r.json()["application"]["state"] == "invited"
        assert len(rsvc.odoo.search_read("belfry.invite", [], ["id"])) == 1
        assert any("invites you" in s for s in subjects("olga@example.org"))
        assert not rsvc.odoo.search_read(
            "belfry.pass", [["partner_id.email", "=", "olga@example.org"]], ["id"]
        )


# ── logging in on the chat ──────────────────────────────────────────────────────────────────
def login(c, key, email):
    r = call(c, key, "/login/start", {"email": email})
    assert r.status_code == 202 and r.json() == {"sent": True}, r.text
    return call(c, key, "/login/verify", {"email": email, "code": _code()})


def test_login_mails_a_code_to_any_address_and_an_unknown_one_gets_a_proof(belfry):
    rsvc, _k, _ada, rule, _g = belfry
    nora = PrivateKey()
    form = {"email": NORA, "motivation": WHY, "agreed": [rule]}
    with TestClient(create_app(rsvc)) as c:
        mailer.SENT.clear()
        r = call(c, nora, "/login/start", {"email": "nope"})
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        r = call(c, nora, "/login/start", {"email": NORA})
        assert r.status_code == 202 and r.json() == {"sent": True}
        [mail] = to(NORA)
        text = mail.get_body(preferencelist=("plain",)).get_content()
        code = _code()
        assert f"Your code for {rsvc.settings.community_name}: {code}" in text
        wrong = "000000" if code != "000000" else "111111"
        r = call(c, nora, "/login/verify", {"email": NORA, "code": wrong})
        assert r.status_code == 422 and r.json()["tries_left"] == 4
        # a log-in code is no application code
        r = call(c, nora, "/apply/verify", {"email": NORA, "code": code})
        assert r.status_code == 410
        r = call(c, nora, "/login/verify", {"email": NORA, "code": code})
        got = r.json()
        assert r.status_code == 200 and got["state"] == "unknown" and got["proof"], got
        assert call(c, nora, "/login/verify", {"email": NORA, "code": code}).status_code == 410
        proof = got["proof"]
        # the proof is this key's, for this address, for a quarter of an hour
        r = call(c, PrivateKey(), "/apply/start", {**form, "proof": proof})
        assert r.status_code == 422 and r.json()["error"] == "proof"
        r = call(c, nora, "/apply/start", {**form, "email": "mallory@example.org", "proof": proof})
        assert r.status_code == 422 and r.json()["error"] == "proof"
        old = applyverified.make_proof(rsvc, NORA, pk(nora), now=time.time() - 901)
        r = call(c, nora, "/apply/start", {**form, "proof": old})
        assert r.status_code == 422 and r.json()["error"] == "proof"
        for junk in ("", "x", "1.2", 7, f"{int(time.time()) + 60}.{'A' * 43}"):
            r = call(c, nora, "/apply/start", {**form, "proof": junk})
            assert r.status_code == 422 and r.json()["error"] == "proof", junk
        assert not apps_of(rsvc)
        # the form is still checked first
        r = call(c, nora, "/apply/start", {**form, "agreed": [], "proof": proof})
        assert r.status_code == 400 and r.json()["error"] == "unticked"
        # with the proof: no second code, the application waits at once
        mailer.SENT.clear()
        r = call(c, nora, "/apply/start", {**form, "proof": proof})
        assert r.status_code == 200 and r.json() == {"state": "pending"}, r.text
        assert not to(NORA)
        [row] = apps_of(rsvc)
        assert row["verified"] is True and row["client"] == pk(nora) and row["name"] == "nora"
        assert call(c, nora, "/apply/status", {}).json() == {"state": "pending"}
        # again: the same answer, nothing new
        r = call(c, nora, "/apply/start", {**form, "proof": proof})
        assert r.json() == {"state": "pending"} and len(apps_of(rsvc)) == 1


def test_login_with_a_request_that_waits_moves_it_to_this_browser(belfry, monkeypatch):
    rsvc, _k, ada, rule, _g = belfry
    bound = bound_keys(rsvc, monkeypatch)
    first, second, third = PrivateKey(), PrivateKey(), PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        apply(c, first, rule)
        r = login(c, second, NORA)
        assert r.status_code == 200 and r.json() == {"state": "pending"}
        assert call(c, second, "/apply/status", {}).json() == {"state": "pending"}
        assert call(c, first, "/apply/status", {}).json() == {"state": "none"}
        [row] = apps_of(rsvc)
        call(c, ada, "/applications", {"decide": row["id"], "approve": True})
        assert call(c, first, "/apply/status", {}).json() == {"state": "none"}
        # approved, and a browser without a session logs in: the address has an account now
        r = login(c, third, NORA)
        got = r.json()
        assert r.status_code == 200 and got["state"] == "signed_in", got
        assert got["nip05"] == "nora@belfry.test" and got["bunker_url"].startswith("bunker://")
        assert bound == [("Chat", pk(third))]
        got = call(c, second, "/apply/status", {}).json()
        assert got["state"] == "approved" and got["signed_in"] is True
        assert bound == [("Chat", pk(third)), ("Chat", pk(second))]


def test_login_signs_a_member_in_and_never_adopts_a_request_nobody_proved(belfry, monkeypatch):
    rsvc, _k, _ada, rule, _g = belfry
    bound = bound_keys(rsvc, monkeypatch)
    me, olga = PrivateKey(), PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        r = login(c, me, "admin@belfry.test")
        assert r.status_code == 200 and r.json()["state"] == "signed_in"
        assert bound == [("Chat", pk(me))] and "proof" not in r.json()
        body = {"email": "olga@example.org", "motivation": WHY, "agreed": [rule]}
        assert c.post("/api/apply", json=body).json() == {"ok": True}
        # anyone may have typed that address and those words: logging in does not adopt it
        got = login(c, olga, "olga@example.org").json()
        assert got["state"] == "unknown" and got["proof"]
        [row] = apps_of(rsvc)
        assert not row.get("verified") and row.get("client") != pk(olga)


def test_without_the_own_register_the_endpoints_say_so(services, monkeypatch):
    services.settings.public_url = "https://pass.belfry.test"
    services.settings.console_url = "https://belfry.belfry.test"
    with TestClient(create_app(services)) as c:
        for path in ("/apply/start", "/apply/verify", "/apply/status", "/login/start"):
            r = call(c, PrivateKey(), path, {})
            assert r.status_code == 409 and r.json()["error"] == "unsupported", path
