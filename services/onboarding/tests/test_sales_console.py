"""belfry.<domain>/sales (salesconsole.py): admins only, csrf, fixed messages, write-only
secrets, products, codes, offers and orders."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import console, mypass, sales, salesconsole
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures

H = {"Host": "belfry.belfry.test"}
CSRF = "c" * 16


@pytest.fixture
def admin(seller, world):
    rsvc = seller[0]
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    rsvc.settings.pass_page_secret = "p" * 32
    ada_id = world[4]
    return rsvc, ada_id


def signed_in(c, rsvc, partner):
    c.cookies.set(
        console.COOKIE,
        mypass._sign(rsvc, {"kind": "console", "partner": partner, "csrf": CSRF}, 3600),
    )


def post(c, path, **data):
    return c.post(path, data={"csrf": CSRF, **data}, headers=H, follow_redirects=False)


def test_only_admins_see_the_page(admin, world):
    rsvc, ada_id = admin
    _bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        r = c.get("/sales", headers=H, follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]
        signed_in(c, rsvc, bob_id)
        assert c.get("/sales", headers=H).status_code == 403
        r = post(c, "/sales/bank", holder="X", iban="BE68539007547034")
        assert r.status_code == 303
        assert sales.settings(rsvc)["bank_holder"] == "XL Collective"  # unchanged
        signed_in(c, rsvc, ada_id)
        r = c.get("/sales", headers=H)
        assert r.status_code == 200
        t = r.text
        assert "Can sell paid passes" in t and "Two-week pass" in t
        assert 'href="/sales"' in t  # in the side menu
        # the Odoo picks come from Odoo; the key is never on the page
        assert "Festival ticket" in t and "XL Collective" in t
        assert "odoo-key-" not in t and "x" * 20 not in t
        assert "https://belfry.belfry.test/hooks/stripe" in t
        # a form without the csrf does nothing
        r = c.post(
            "/sales/bank",
            data={"holder": "Y", "iban": "BE68539007547034"},
            headers=H,
            follow_redirects=False,
        )
        assert sales.settings(rsvc)["bank_holder"] == "XL Collective"


def test_settings_secrets_products_and_codes(admin, monkeypatch):
    rsvc, ada_id = admin
    from belfry_onboarding import salesodoo

    checked = []

    def check(url, db, key):
        checked.append((url, db, len(key)))
        if key == "bad":
            raise ValueError("refused")
        return [{"id": 1, "name": "XL"}]

    monkeypatch.setattr(salesodoo, "check_connection", check)
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, ada_id)
        post(c, "/sales/odoo", url="https://xl.odoo.test", db="xl", key="bad")
        assert "did not accept" in c.get("/sales", headers=H).text
        assert sales.secret(rsvc, "odoo_api_key") != "bad"
        post(c, "/sales/odoo", url="https://xl.odoo.test", db="xl2", key="k" * 30 + "LAST")
        t = c.get("/sales", headers=H).text
        assert "Odoo answers" in t and "··· LAST" in t and "k" * 30 not in t
        assert sales.settings(rsvc)["odoo_db"] == "xl2"
        # the key kept when left empty
        post(c, "/sales/odoo", url="https://xl.odoo.test", db="xl", key="")
        assert checked[-1] == ("https://xl.odoo.test", "xl", 34)
        # bank: IBAN checked
        post(c, "/sales/bank", holder="Hub", iban="BE68 5390 0754 7035")
        assert "not valid" in c.get("/sales", headers=H).text
        post(c, "/sales/bank", holder="Hub", iban="BE68 5390 0754 7034", bic="gkccbebb")
        assert sales.settings(rsvc)["bank_bic"] == "GKCCBEBB"
        # Stripe: write-only, cleared back to nothing
        post(c, "/sales/stripe", key="rk_live_" + "a" * 20 + "ZZ99", webhook="whsec_" + "b" * 20)
        t = c.get("/sales", headers=H).text
        assert "··· ZZ99" in t and "a" * 20 not in t
        post(c, "/sales/clear", secret="stripe_key")
        assert not sales.secret_state(rsvc, "stripe_key")["set"]
        post(c, "/sales/stripe", key="pk_live_nope")
        assert "starts with rk_" in c.get("/sales", headers=H).text
        # picks
        post(
            c,
            "/sales/picks",
            company="1",
            **{"product_village-2027": "1"},
            tax_included="10",
            tax_excluded="11",
            card_journal="7",
            manual_journal="",
        )
        st = sales.settings(rsvc)
        assert st["odoo_products"] == {"village-2027": 1} and st["manual_journal"] is None
        # products
        post(
            c,
            "/sales/product",
            id="day",
            name="Day pass",
            tier="day",
            prices="12.50 until 2099-01-01\n15",
            vat_rate="6",
            quantity="",
            on_sale="1",
        )
        p = sales.product(rsvc, "day")
        assert p["prices"] == [
            {"until": "2099-01-01", "amount": 1250},
            {"until": None, "amount": 1500},
        ]
        assert p["vat_rate"] == 6.0 and p["quantity"] is None
        post(c, "/sales/product", id="day2", name="X", prices="twelve")
        assert "one per line" in c.get("/sales", headers=H).text
        post(c, "/sales/product/delete", id="day")
        assert sales.product(rsvc, "day") is None
        # codes
        post(
            c,
            "/sales/codes",
            percent="50",
            product="village-2027",
            uses="2",
            count="3",
            note="crew",
        )
        codes = sales.store(rsvc).codes()
        assert len(codes) == 3 and codes[0]["note"] == "crew" and codes[0]["by"] == "Ada Admin"
        t = c.get("/sales", headers=H).text
        assert codes[0]["code"] in t
        post(c, "/sales/code/revoke", code=codes[0]["code"])
        assert sales.store(rsvc).code(codes[0]["code"])["revoked"]


def test_orders_cancel_and_mark_paid_need_a_reason(admin):
    rsvc, ada_id = admin
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
        oid2 = buy(c, PrivateKey(), pay="invoice", email="b@example.org").json()["order"]
        signed_in(c, rsvc, ada_id)
        t = c.get("/sales", headers=H).text
        assert "INV/2026/0001" in t and "ada@example.org" in t
        post(c, "/sales/order", id=oid, reason="", do="paid")
        assert "Give a reason" in c.get("/sales", headers=H).text
        post(c, "/sales/order", id=oid, reason="bank statement 3", do="paid")
        assert sales.store(rsvc).order(oid)["state"] == "active"
        post(c, "/sales/order", id=oid2, reason="asked by phone", do="cancel")
        o = sales.store(rsvc).order(oid2)
        assert o["state"] == "cancelled" and o["ended"]["by"] == "Ada Admin"
        post(c, "/sales/order", id=oid2, reason="again", do="paid")
        assert "not open any more" in c.get("/sales", headers=H).text


def test_prices_text_parses():
    assert salesconsole.parse_prices("200 until 2026-12-31\n\n300,50") == [
        {"amount": "200", "until": "2026-12-31"},
        {"amount": "300.50", "until": None},
    ]
    with pytest.raises(ValueError):
        salesconsole.parse_prices("200 till 2026")


def test_waiting_for_a_transfer_comes_first_soonest_hold_first(admin):
    rsvc, ada_id = admin
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        late = buy(c, PrivateKey(), pay="invoice", email="late@example.org").json()["order"]
        soon = buy(c, PrivateKey(), pay="invoice", email="soon@example.org").json()["order"]
        st = sales.store(rsvc)
        st.update(
            soon,
            name="Soon Person",
            hold_until=sales.iso(sales.now() + dt.timedelta(days=2, hours=1)),
        )
        st.update(
            late, name="Late Person", hold_until=sales.iso(sales.now() + dt.timedelta(days=6))
        )
        card = buy(c, PrivateKey(), email="card@example.org").json()["order"]  # no invoice
        signed_in(c, rsvc, ada_id)
        t = c.get("/sales", headers=H).text
        head = t[t.index("Waiting for a transfer") : t.index("<h2>Orders</h2>")]
        assert head.index("Soon Person") < head.index("Late Person")
        assert "3 days left" in head and "6 days left" in head
        assert "INV/2026/" in head and "€200.00" in head
        assert st.order(card)["state"] != "invoiced" and "card@example.org" not in head
        # the Mark paid there is the same action: a reason, and who did it
        post(c, "/sales/order", id=soon, reason="bank statement 7", do="paid")
        o = st.order(soon)
        assert o["state"] == "active" and o["paid"]["by"] == "Ada Admin"
        t = c.get("/sales", headers=H).text
        head = t[t.index("Waiting for a transfer") : t.index("<h2>Orders</h2>")]
        assert "Soon Person" not in head and "Late Person" in head


def test_days_left_wording():
    now = sales.now()
    assert salesconsole.days_left(None) == "no end set"
    assert (
        salesconsole.days_left(sales.iso(now - dt.timedelta(hours=1)), now) == "the hold has ended"
    )
    assert (
        salesconsole.days_left(sales.iso(now + dt.timedelta(hours=5)), now)
        == "less than a day left"
    )
    assert (
        salesconsole.days_left(sales.iso(now + dt.timedelta(days=1, hours=1)), now) == "2 days left"
    )


def test_the_erase_request_shows_on_the_order_and_an_admin_clears_it(admin, keycast):
    rsvc, ada_id = admin
    code = sales.make_codes(rsvc, 100, "village-2027")[0]["code"]
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        oid = buy(c, PrivateKey(), code=code, erase_after_event=True).json()["order"]
        signed_in(c, rsvc, ada_id)
        t = c.get("/sales", headers=H).text
        assert "Erase their data after the event (asked)" in t and "Clear the erase request" in t
        pid = sales.store(rsvc).order(oid)["partner_id"]
        post(c, "/sales/order", id=oid, reason="", do="keep_data")
        assert sales.erase_of(rsvc, pid)
        post(c, "/sales/order", id=oid, reason="they asked by phone", do="keep_data")
        assert not sales.erase_of(rsvc, pid)
        t = c.get("/sales", headers=H).text
        assert "Clear the erase request" not in t and "(asked)" in t
