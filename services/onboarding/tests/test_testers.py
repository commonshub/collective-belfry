"""Plugins (plugins.py), the test environments of Stripe and Odoo, and testers (testers.py):
docs/sales.md §9, docs/roles.md "Tester"."""

# ruff: noqa: F811
from __future__ import annotations

import json

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import console, core, guests, mailer, sales, salesodoo, testers
from belfry_onboarding.app import create_app
from belfry_onboarding.stripe_sig import sign_header

from .fakes import FakeSellerOdoo
from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import _join, call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, shop  # noqa: F401 — fixtures
from .test_sales_console import H, post, signed_in
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import event, stripe  # noqa: F401 — fixtures

LIVE_KEY = "rk_live_" + "L" * 24
TEST_KEY = "rk_test_" + "T" * 24
LIVE_WH = "whsec_" + "l" * 32
TEST_WH = "whsec_" + "t" * 32
TESTER = "tess@example.org"


@pytest.fixture
def envs(stripe, monkeypatch):
    """Production as the sales tests set it up (with a live key now), and a complete test
    environment: its own Stripe key and secret, its own Odoo (another fake) and picks."""
    seller, made, state, expired = stripe
    rsvc, live_odoo, _vies = seller
    sales.set_secret(rsvc, "stripe_key", LIVE_KEY)
    sales.set_secret(rsvc, "stripe_webhook_secret", LIVE_WH)
    test_odoo = FakeSellerOdoo()
    monkeypatch.setattr(salesodoo, "test_client", lambda svc: test_odoo)
    sales.save_settings(
        rsvc,
        {
            "odoo_url": "https://test.odoo.test",
            "odoo_db": "xl-test",
            "company": 1,
            "odoo_products": {"village-2027": 1},
            "tax_included": 10,
            "tax_excluded": 11,
            "card_journal": 7,
            "manual_journal": 8,
        },
        "test",
    )
    sales.set_secret(rsvc, "odoo_api_key", "test-odoo-key-" + "y" * 20, "test")
    sales.set_secret(rsvc, "stripe_key", TEST_KEY, "test")
    sales.set_secret(rsvc, "stripe_webhook_secret", TEST_WH, "test")
    return rsvc, live_odoo, test_odoo, made


def deliver(c, ev: dict, secret: str, path: str):
    body = json.dumps(ev).encode()
    return c.post(
        path,
        content=body,
        headers={
            **H,
            "Stripe-Signature": sign_header(secret, body),
            "Content-Type": "application/json",
        },
    )


def make_tester(rsvc, email=TESTER, name="Tess Tester") -> tuple[PrivateKey, int]:
    testers.add_tester(rsvc, email, name)
    pid = testers.tester_by_email(rsvc, email)
    assert pid
    key = PrivateKey()
    row = rsvc.odoo.search_read(
        "belfry.pass", [["partner_id", "=", pid], ["state", "=", "active"]], ["id"]
    )[0]
    rsvc.odoo.write("belfry.pass", [row["id"]], {"keycast_pubkey": pk(key)})
    return key, pid


# ── settings ────────────────────────────────────────────────────────────────────────────────
def test_test_settings_never_touch_production(seller, monkeypatch):
    rsvc = seller[0]
    before = sales.settings(rsvc)
    live_key = sales.secret(rsvc, "odoo_api_key")
    # the server's own settings are production's fallback only
    monkeypatch.setattr(rsvc.settings, "stripe_webhook_secret", "whsec_from_env", raising=False)
    assert sales.secret(rsvc, "stripe_webhook_secret", "test") == ""
    sales.save_settings(
        rsvc, {"odoo_url": "https://t.odoo.test", "odoo_db": "t", "tax_included": 99}, "test"
    )
    sales.set_secret(rsvc, "odoo_api_key", "other-key-" + "z" * 20, "test")
    assert sales.settings(rsvc) == before and sales.secret(rsvc, "odoo_api_key") == live_key
    t = sales.settings(rsvc, "test")
    assert t["odoo_db"] == "t" and t["tax_included"] == 99 and t["odoo_products"] == {}
    assert t["card_journal"] is None and t["fallback_products"] == {}
    assert t["bank_iban"] == before["bank_iban"]  # the bank is shared
    with pytest.raises(ValueError):
        sales.save_settings(rsvc, {"bank_iban": "X"}, "test")
    with pytest.raises(ValueError):
        sales.settings(rsvc, "staging")
    assert sales.accounting_ready(rsvc) and not sales.accounting_ready(rsvc, "test")
    # the file keeps both, under their own names; the state shows set / not set only
    stored = json.loads(sales._secrets_path(rsvc).read_text())
    assert {"odoo_api_key", "test_odoo_api_key"} <= set(stored)
    assert sales.secret_state(rsvc, "odoo_api_key", "test")["last4"] == "zzzz"


def test_plugins_pages_and_key_refusals(envs, world, monkeypatch):
    rsvc = envs[0]
    ada_id = world[4]
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    rsvc.settings.pass_page_secret = "p" * 32
    monkeypatch.setattr(salesodoo, "check_connection", lambda url, db, key: [{"id": 1}])
    bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        assert c.get("/plugins", headers=H, follow_redirects=False).status_code == 303
        signed_in(c, rsvc, bob_id)
        assert c.get("/plugins", headers=H).status_code == 403
        r = post(c, "/plugins/stripe/test", key="rk_test_" + "n" * 20)
        assert sales.secret(rsvc, "stripe_key", "test") == TEST_KEY  # not an admin: unchanged
        signed_in(c, rsvc, ada_id)
        t = c.get("/plugins", headers=H).text
        assert "Stripe" in t and "Odoo" in t and "E-mail" in t
        assert "Live, with a test environment" in t and "The test environment is ready" in t
        assert LIVE_KEY not in t and TEST_KEY not in t
        t = c.get("/plugins/stripe", headers=H).text
        assert "Production" in t and "Test" in t and "/hooks/stripe/test" in t
        assert LIVE_KEY[8:] not in t and TEST_KEY[8:] not in t and TEST_WH not in t
        # production refuses a test key, test refuses a live one: nothing saved
        post(c, "/plugins/stripe/live", key="sk_test_" + "a" * 20)
        assert "That is a test key" in c.get("/plugins/stripe", headers=H).text
        assert sales.secret(rsvc, "stripe_key") == LIVE_KEY
        post(c, "/plugins/stripe/test", key="rk_live_" + "b" * 20)
        assert "That is a live key" in c.get("/plugins/stripe", headers=H).text
        assert sales.secret(rsvc, "stripe_key", "test") == TEST_KEY
        # the right ones go to their own environment
        post(c, "/plugins/stripe/test", key="sk_test_" + "c" * 20 + "TT99")
        assert sales.secret(rsvc, "stripe_key", "test").endswith("TT99")
        assert sales.secret(rsvc, "stripe_key") == LIVE_KEY
        assert "··· TT99" in c.get("/plugins/stripe", headers=H).text
        post(c, "/plugins/clear", secret="stripe_key", env="test", back="stripe")
        assert not sales.secret_state(rsvc, "stripe_key", "test")["set"]
        assert sales.secret(rsvc, "stripe_key") == LIVE_KEY
        assert "Live" in c.get("/plugins", headers=H).text
        # an environment that isn't one of the two: nothing
        r = post(c, "/plugins/stripe/staging", key="sk_test_" + "d" * 20)
        assert r.status_code == 303
        # the test Odoo: its own connection, never production's database
        post(c, "/plugins/odoo/test", url="https://xl.odoo.test", db="xl", key="k" * 20)
        assert "another database" in c.get("/plugins/odoo", headers=H).text
        post(c, "/plugins/odoo/test", url="https://t2.odoo.test", db="xl-t2", key="k" * 20)
        assert sales.settings(rsvc, "test")["odoo_db"] == "xl-t2"
        assert sales.settings(rsvc)["odoo_db"] == "xl"
        post(c, "/plugins/odoo/test/picks", company="2", tax_included="40", tax_excluded="41")
        assert sales.settings(rsvc, "test")["tax_included"] == 40
        assert sales.settings(rsvc)["tax_included"] == 10
        # E-mail: the setup form, coming back here
        t = c.get("/plugins/email", headers=H).text
        assert "E-mail" in t


# ── testers ─────────────────────────────────────────────────────────────────────────────────
def test_a_tester_is_made_new_and_never_an_admin(envs, world):
    rsvc = envs[0]
    ada = world[3]
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    rsvc.settings.pass_page_secret = "p" * 32
    person(rsvc, "bob@example.org", "Bob")
    # an address with an account can't become a tester, nor a pending one
    with pytest.raises(ValueError, match="account already"):
        console.add_member(rsvc, "Bob@Example.org", "Bob", False, True)
    rsvc.odoo.create("res.partner", {"name": "P", "belfry_pending_email": "pend@example.org"})
    with pytest.raises(ValueError, match="account already"):
        console.add_member(rsvc, "pend@example.org", "P", False, True)
    with pytest.raises(ValueError, match="can't be an admin"):
        console.add_member(rsvc, "new@example.org", "N", True, True)
    tess, tess_id = make_tester(rsvc)
    assert testers.is_tester(rsvc, tess_id) and testers.is_tester_key(rsvc, pk(tess))
    assert rsvc.odoo.read_one("res.partner", tess_id, ["email"])["email"] == TESTER
    with pytest.raises(ValueError, match="can't be an admin"):
        console.edit_member(rsvc, tess_id, "", True)
    with TestClient(create_app(rsvc)) as c:
        r = call(c, ada, "/admins", {"pubkey": pk(tess), "admin": True})
        assert r.status_code == 409 and r.json()["error"] == "tester"
        r = call(c, ada, "/member-roles", {"pubkey": pk(tess), "role": "admin"})
        assert r.status_code == 409 and r.json()["error"] == "tester"
        r = call(c, ada, "/member-roles", {"pubkey": pk(tess), "role": "member"})
        assert r.status_code == 409 and r.json()["error"] == "tester"
        # whois labels them for the apps; belfry.json (public) never lists them
        r = call(c, ada, "/whois", {"pubkeys": [pk(tess), pk(ada)]})
        assert r.json()["testers"] == [pk(tess)] and r.json()["people"][pk(tess)] == "member"
        d = call(c, ada, "/whois", {"directory": True}).json()["members"]
        assert next(m for m in d if m["pubkey"] == pk(tess))["tester"] is True
        assert pk(tess) not in c.get("/.well-known/belfry.json").text
    # the console form: a role choice, tester refused with admin, tag in the list
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, world[4])
        r = post(c, "/members/add", email="x@example.org", role="tester", admin="1")
        assert "can't be an admin" in r.headers.get("set-cookie", "") or r.status_code == 303
        assert testers.tester_by_email(rsvc, "x@example.org") is None
        post(c, "/members/add", email="tim@example.org", name="Tim", role="tester")
        assert testers.tester_by_email(rsvc, "tim@example.org")
        page = c.get("/members", headers=H).text
        assert 'class="tag tester"' in page and 'value="tester"' in page
        assert "A tester is for trying payments" in page


def test_a_tester_link_makes_only_a_new_person_a_tester(envs, world, keycast):
    rsvc = envs[0]
    ada_id = world[4]
    url = console.invite_link(rsvc, ada_id, "tester", 7)
    token = url.rsplit("/", 1)[1]
    assert rsvc.odoo.invite_by_token(token)["member_tester"] is True
    with TestClient(create_app(rsvc)) as c:
        got = _join(c, token, PrivateKey()).json()
        me = PrivateKey()
        p = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", got["pubkey"]]], ["id", "partner_id"]
        )[0]
        rsvc.odoo.write("belfry.pass", [p["id"]], {"keycast_pubkey": pk(me)})
        keycast.status[pk(me)] = "active"
        keycast.users[f"odoo-partner-{core.m2o_id(p['partner_id'])}"]["pubkey"] = pk(me)
        mailer.SENT.clear()
        r = call(c, me, "/guest/save", {"email": "new@example.org", "name": "New"})
        assert r.status_code == 202, r.text
        r = call(
            c, me, "/guest/save/verify", {"email": "new@example.org", "code": _code(), "name": "N"}
        )
        assert r.status_code == 200 and r.json()["member"] is True, r.text
    pid = core.m2o_id(p["partner_id"])
    assert testers.is_tester(rsvc, pid) and testers.tester_by_email(rsvc, "new@example.org") == pid
    # someone who had an account before the link gets nothing from it
    assert not testers.fresh_for_invite(rsvc, world[4], int(rsvc.odoo.invite_by_token(token)["id"]))
    inv = {"member_tester": True, "member_role": "member", "membership": "permanent", "id": 999}
    bob, _ = person(rsvc, "bob@example.org", "Bob")
    assert guests.become_member(rsvc, pk(bob), inv) is False


# ── orders in the test environment ──────────────────────────────────────────────────────────
def test_a_testers_order_uses_the_test_environment_and_a_buyers_production(envs, keycast):
    rsvc, live_odoo, test_odoo, made = envs
    tess, tess_id = make_tester(rsvc)
    buyer = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        # the tester, signed in with their own session: test mode
        r = buy(c, tess, email=TESTER, pay="card")
        assert r.status_code == 201, r.text
        t = r.json()
        assert t["test"] is True
        # a buyer at the same time: production
        r = buy(c, buyer, email="ada@example.org", pay="card")
        b = r.json()
        assert b["test"] is False
        assert made[0]["auth"] == f"Bearer {TEST_KEY}" and made[1]["auth"] == f"Bearer {LIVE_KEY}"
        # the test order takes no place from real buyers
        assert sales.left(rsvc, sales.product(rsvc, "village-2027")) == 2
        assert sales.taken_by_tests(rsvc, "village-2027") == 1
        # a test event on the production hook can't settle anything, nor the reverse
        ev_t = {**event(t["order"], t["order"], 20000, eid="evt_t"), "livemode": False}
        ev_b = {**event(b["order"], b["order"], 20000, eid="evt_b"), "livemode": True}
        assert deliver(c, ev_t, TEST_WH, "/hooks/stripe").status_code == 400  # wrong secret
        r = deliver(c, ev_t, LIVE_WH, "/hooks/stripe")
        assert r.json()["result"] == "wrong_mode"
        r = deliver(c, {**ev_t, "id": "evt_t2", "livemode": True}, LIVE_WH, "/hooks/stripe")
        assert r.json()["result"] == "wrong_env"
        r = deliver(c, {**ev_b, "id": "evt_b2", "livemode": False}, TEST_WH, "/hooks/stripe/test")
        assert r.json()["result"] == "wrong_env"
        r = deliver(c, {**ev_b, "id": "evt_b3"}, TEST_WH, "/hooks/stripe/test")
        assert r.json()["result"] == "wrong_mode"
        st = sales.store(rsvc)
        assert st.order(t["order"])["state"] == "pending"
        assert st.order(b["order"])["state"] == "pending"
        # each on its own hook
        assert deliver(c, ev_t, TEST_WH, "/hooks/stripe/test").json()["result"] == "paid"
        assert deliver(c, ev_b, LIVE_WH, "/hooks/stripe").json()["result"] == "paid"
    ot, ob = st.order(t["order"]), st.order(b["order"])
    assert ot["state"] == "active" and ob["state"] == "active"
    # each invoice in its own Odoo
    assert ot["invoice"]["move_id"] in test_odoo.records["account.move"]
    assert ot["invoice"]["move_id"] not in live_odoo.records.get("account.move", {}) or (
        live_odoo.records["account.move"][ot["invoice"]["move_id"]]["ref"] != "Belfry " + ot["id"]
    )
    refs = {m["ref"] for m in live_odoo.records["account.move"].values()}
    assert "Belfry " + ob["id"] in refs and "Belfry " + ot["id"] not in refs
    assert {m["ref"] for m in test_odoo.records["account.move"].values()} == {"Belfry " + ot["id"]}
    # the tester's pass is on the tester's account and marked test
    p = rsvc.odoo.read_one("belfry.pass", ot["pass_id"], ["partner_id", "belfry_test", "state"])
    assert core.m2o_id(p["partner_id"]) == tess_id and p["belfry_test"] is True
    pb = rsvc.odoo.read_one("belfry.pass", ob["pass_id"], ["belfry_test"])
    assert not pb["belfry_test"]
    # the test receipt says so; the tester's live Odoo contact was never used or stored
    subjects = [
        str(m["Subject"])
        for m in mailer.SENT
        if m["To"] == TESTER and "Welcome" not in str(m["Subject"])
    ]
    assert subjects and all(s.startswith("[TEST]") for s in subjects)
    assert not rsvc.odoo.read_one("res.partner", tess_id, ["belfry_odoo_partner"])[
        "belfry_odoo_partner"
    ]
    # the tick reads each environment's invoices in its own Odoo
    test_odoo.calls.clear()
    live_odoo.calls.clear()
    sales.tick(rsvc, force=True)
    assert ("account.move", "read") in test_odoo.calls
    assert ("account.move", "read") in live_odoo.calls


def test_a_stranger_with_a_testers_address_gets_nothing(envs, keycast):
    rsvc, _live, _test, made = envs
    _tess, tess_id = make_tester(rsvc)
    stranger = PrivateKey()
    partners_before = len(rsvc.odoo.search_read("res.partner", [], ["id"]))
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, stranger, email=TESTER.upper(), pay="card")
        assert r.status_code == 201 and r.json()["test"] is True
        oid = r.json()["order"]
        assert made[-1]["auth"] == f"Bearer {TEST_KEY}"
        ev = {**event(oid, oid, 20000, eid="evt_s"), "livemode": False}
        assert deliver(c, ev, TEST_WH, "/hooks/stripe/test").json()["result"] == "paid"
        got = call(c, stranger, f"/checkout/{oid}", method="GET").json()
        assert got["state"] == "active" and "bunker_url" not in got
    o = sales.store(rsvc).order(oid)
    assert o["account"] is None and o["partner_id"] == tess_id and not o.get("bunker_url")
    # no account of their own, no session for their key
    assert len(rsvc.odoo.search_read("res.partner", [], ["id"])) == partners_before
    assert not any(b.get("client_pubkey") == pk(stranger) for b in keycast.bunkers)
    p = rsvc.odoo.read_one("belfry.pass", o["pass_id"], ["partner_id", "keycast_pubkey"])
    assert core.m2o_id(p["partner_id"]) == tess_id and p["keycast_pubkey"] != pk(stranger)
    # a free product with a 100% code: the same, the pass on the tester's account
    code = sales.make_codes(rsvc, 100, "village-2027", 1)[0]["code"]
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, PrivateKey(), email=TESTER, code=code)
        assert r.json()["state"] == "active" and "bunker_url" not in r.json()
    # a test order never takes one of the code's real uses
    c_ = sales.store(rsvc).code(code)
    assert c_["used"] == [] and len(c_["test_used"]) == 1
    # a tester removed since: their pending test order opens nothing
    with TestClient(create_app(rsvc)) as c:
        oid2 = buy(c, PrivateKey(), email=TESTER, pay="card", name="x").json()["order"]
    console.remove_member(rsvc, tess_id)
    sales.mark_paid(rsvc, oid2, "hand", "test", "admin")
    o2 = sales.store(rsvc).order(oid2)
    assert o2["state"] == "paid" and "tester_gone" in o2["flags"] and not o2.get("pass_id")


def test_no_test_environment_means_409_never_production(stripe, keycast):
    (rsvc, live_odoo, _v), made, _s, _e = stripe
    make_tester(rsvc)
    bank = {k: sales.settings(rsvc)[k] for k in ("bank_holder", "bank_iban", "bank_bic")}
    with TestClient(create_app(rsvc)) as c:
        # no test Stripe key and no bank account: no way to pay in the test environment
        sales.save_settings(rsvc, {"bank_holder": "", "bank_iban": ""})
        r = buy(c, PrivateKey(), email=TESTER, pay="card")
        assert r.status_code == 409 and r.json()["error"] == "test_not_set_up"
        assert made == [] and not live_odoo.records.get("account.move")
        assert sales.store(rsvc).orders() == []
        # the bank account alone (shared) is a way to pay; no test Odoo: a receipt, no invoice.
        # Still a test order, and production's Stripe and Odoo are never used for it
        sales.save_settings(rsvc, bank)
        r = buy(c, PrivateKey(), email=TESTER, pay="card")
        assert r.status_code == 201 and r.json()["test"] is True
        assert r.json()["card_url"] is None and r.json()["invoice"] is None
        assert r.json()["reference"].startswith("ORDER ")
        assert made == [] and not live_odoo.records.get("account.move")
        # a normal buyer is not affected
        r = buy(c, PrivateKey(), email="ada@example.org", pay="card")
        assert r.status_code == 201 and r.json()["test"] is False
    assert sales.store(rsvc).code  # the store still works


def test_a_test_invoice_order_and_mark_paid(envs, keycast):
    rsvc, live_odoo, test_odoo, _made = envs
    tess, _ = make_tester(rsvc)
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, tess, email=TESTER, pay="invoice")
        assert r.status_code == 201, r.text
        oid = r.json()["order"]
        assert r.json()["invoice"]["number"]
        page = c.get(f"/pay/{oid}", headers=H).text
        assert "Test order: no real money." in page
    o = sales.store(rsvc).order(oid)
    assert o["invoice"]["move_id"] in test_odoo.records["account.move"]
    assert not live_odoo.records.get("account.move")
    sales.mark_paid(rsvc, oid, "hand", "seen", "admin")
    o = sales.store(rsvc).order(oid)
    assert o["state"] == "active"
    move = test_odoo.records["account.move"][o["invoice"]["move_id"]]
    assert move["payment_state"] == "paid"
    assert not live_odoo.records.get("account.payment.register")


def test_the_sales_page_lists_test_orders_apart(envs, world, keycast):
    rsvc = envs[0]
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    rsvc.settings.pass_page_secret = "p" * 32
    tess, _ = make_tester(rsvc)
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        buy(c, tess, email=TESTER, pay="card", name="Tess Tester")
        signed_in(c, rsvc, world[4])
        t = c.get("/sales", headers=H).text
        assert "Test orders" in t and "test-mark" in t
        orders, tests = t.split('<h2 id="test-orders">', 1)
        assert TESTER in tests and TESTER not in orders.split("<h2>Orders</h2>", 1)[1]
        assert "/plugins" in t and "Restricted key" not in t
