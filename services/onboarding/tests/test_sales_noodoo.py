"""The checkout with no Odoo connected (docs/sales.md §11): an order goes pending → paid →
active with no invoice; the buyer gets Belfry's own receipt; a bank transfer is marked paid by
an admin; nothing is ever marked paid without Stripe's event or an admin."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import time
import urllib.parse

import httpx
import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import events, guests, mailer, plugins, sales, salesodoo, ticketsales
from belfry_onboarding.app import create_app

from .test_events import D, World, make_event
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, free_code, shop  # noqa: F401 — fixtures
from .test_sales_console import H, post, signed_in
from .test_sales_odoo import FakeVies, seller  # noqa: F401 — fixtures
from .test_sales_stripe import WHSEC, deliver, event, stripe  # noqa: F401 — fixtures
from .test_testers import LIVE_WH, TEST_WH, TESTER, envs, make_tester  # noqa: F401
from .test_testers import deliver as deliver_to
from .test_ticket_sales import ORG, admissions, order, tk

BANK = {"bank_holder": "The Village", "bank_iban": "BE68539007547034", "bank_bic": "GKCCBEBB"}


@pytest.fixture
def plain(shop, router, monkeypatch):
    """The sales tests' shop with a Stripe key and NO Odoo: any call to one fails the test."""
    rsvc = shop[0]
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    rsvc.settings.pass_page_secret = "p" * 32

    def no_odoo(*a, **k):
        raise AssertionError("no Odoo is connected: nothing may ask for one")

    monkeypatch.setattr(salesodoo, "client", no_odoo)
    monkeypatch.setattr(salesodoo, "test_client", no_odoo)
    vies = FakeVies()
    monkeypatch.setattr(salesodoo, "VIES", vies)
    sales.set_secret(rsvc, "stripe_key", "rk_test_" + "k" * 24)
    sales.set_secret(rsvc, "stripe_webhook_secret", WHSEC)
    made: list[dict] = []

    def create(request: httpx.Request) -> httpx.Response:
        made.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        n = len(made)
        return httpx.Response(
            200,
            json={
                "id": f"cs_test_{n}",
                "url": f"https://checkout.stripe.com/c/pay/cs_test_{n}",
                "expires_at": int(time.time()) + 86400,
            },
        )

    router.post("https://api.stripe.com/v1/checkout/sessions").mock(side_effect=create)
    router.post(url__regex=r"https://api\.stripe\.com/v1/checkout/sessions/.+/expire").mock(
        return_value=httpx.Response(200, json={})
    )
    return rsvc, made, vies


@pytest.fixture
def fete(plain, world, monkeypatch):
    """``plain`` with an event of Ada's on fake relays, and ticket sales switched on."""
    rsvc, made, vies = plain
    ada = world[3]
    w = World()
    monkeypatch.setattr(events, "community", lambda svc, f: w.comm.query(f, None))
    monkeypatch.setattr(events, "groups", lambda svc, f: w.grp.query(f, None))

    def publish(svc, kind, tags, content=""):
        w.put(w.gkey, kind, tags, content)
        return True

    monkeypatch.setattr(events, "publish", publish)
    monkeypatch.setattr(guests, "_channel_event", lambda svc, kind, tags: publish(svc, kind, tags))
    monkeypatch.setattr(events, "names", lambda svc, pks: {})
    events._stores.clear()
    events.PER_IP.__init__(1000, 60)
    events.PER_EVENT.__init__(1000, 60)
    rsvc.settings.site_url = "https://belfry.test"
    sales.save_settings(rsvc, {"tickets": True})
    make_event(w, ada)
    return rsvc, w, ada, made, vies


def text_of(m) -> str:
    return m.get_body(("plain",)).get_content()


def mails(subject: str) -> list:
    return [m for m in mailer.SENT if subject in str(m["Subject"])]


def test_a_pass_paid_by_card_with_no_odoo_gets_a_receipt(plain, keycast):
    rsvc, made, _v = plain
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        assert c.get("/api/v1/products", headers=H).json()["can_sell"] is True
        r = buy(c, me)
        assert r.status_code == 201, r.text
        got = r.json()
        oid = got["order"]
        assert got["state"] == "pending" and got["invoice"] is None and got["card_url"]
        assert got["reference"] == sales.reference_of(oid) and got["pay_url"] is None
        assert got["total"] == {"net": 165.29, "vat": 34.71, "gross": 200.0, "percent_off": 0}
        assert made[-1]["client_reference_id"] == oid
        o = sales.store(rsvc).order(oid)
        assert o["books"] == "none" and o["step"] == "wait" and not mailer.SENT
        # a payment that is not this order's amount opens nothing
        assert deliver(c, event(oid, oid, 100, eid="evt_0")).json()["result"] == "mismatch"
        assert sales.store(rsvc).order(oid)["state"] == "pending"
        # Stripe's event: paid → active, with no invoice step in between
        assert deliver(c, event(oid, oid, 20000)).json()["result"] == "paid"
        o = sales.store(rsvc).order(oid)
        assert o["state"] == "active" and o["step"] == "done" and o["pass_id"]
        assert o["invoice"] == {} and o["payment_due"] is False
        # the same event again, and the same session in another event: nothing twice
        assert deliver(c, event(oid, oid, 20000)).json()["result"] == "duplicate"
        assert deliver(c, event(oid, oid, 20000, eid="evt_2")).json()["result"] == "paid"
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["state"] == "active" and got["bunker_url"].startswith("bunker://")
        # the tick finds nothing to do for the books
        sales.tick(rsvc, force=True)
        assert sales.store(rsvc).order(oid)["step"] == "done"
    (m,) = mails("Two-week pass")
    body = text_of(m)
    assert "receipt" in str(m["Subject"]) and "confirm your e-mail" in str(m["Subject"])
    assert f"Receipt {sales.reference_of(oid)}, from" in body
    assert "For: Two-week pass" in body
    assert "Amount: €165.29 + 21% VAT €34.71 = €200.00" in body
    assert "Paid by card on " in body
    assert "invoice" not in body.lower() and "Billed to" not in body
    assert not list(m.iter_attachments())


def test_a_ticket_by_bank_transfer_marked_paid_by_an_admin(fete, world, keycast):
    rsvc, w, ada, made, vies = fete
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["vat", "21"], ["quantity", "5"])
    sales.save_settings(rsvc, BANK)
    sales.set_secret(rsvc, "stripe_key", None)  # the bank account alone is a way to pay
    assert ticketsales.missing(rsvc) == "" and ticketsales.can_sell(rsvc)
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        got = call(c, PrivateKey(), f"/events/{D}", method="GET").json()
        assert got["tickets"][0]["can_pay"] is True
        r = order(c, me, pay="invoice", billing={**ORG, "street": "Rue 1", "zip": "1000"})
        assert r.status_code == 201, r.text
        got = r.json()
        oid = got["order"]
        ref = sales.reference_of(oid)
        # an organisation: the VAT number is kept as typed, VIES is not asked (nothing is
        # invoiced); the price is before VAT, the VAT on top
        assert vies.asked == []
        assert got["total"] == {"net": 50.0, "vat": 10.5, "gross": 60.5, "percent_off": 0}
        assert got["state"] == "pending" and got["invoice"] is None and got["card_url"] is None
        assert got["reference"] == ref and got["pay_url"].endswith(f"/pay/{oid}")
        # how to pay, by mail and on the order's page
        (m,) = mails("How to pay for your Day ticket")
        assert f"use the communication {ref}" in text_of(m) and "€60.50" in text_of(m)
        assert "cancel it here" in text_of(m) and f"/pay/{oid}" in text_of(m)
        page = c.get(f"/pay/{oid}", headers=H).text
        assert ref in page and "€60.50" in page and "BE68 5390 0754 7034" in page
        assert "<svg" in page and "matched to your order" in page and "nvoice" not in page
        # nothing opens by itself, whatever the tick does
        sales.tick(rsvc, force=True)
        assert sales.store(rsvc).order(oid)["state"] == "pending" and admissions(w) == []
        # the console: waiting for a transfer, with the reference; an admin marks it paid
        signed_in(c, rsvc, world[4])
        t = c.get("/sales", headers=H).text
        head = t[t.index("Waiting for a transfer") : t.index("<h2>Orders</h2>")]
        assert ref in head and "No invoice (no Odoo)" in head and "€60.50" in head
        assert "Day ticket · Garden day" in head and "nothing else does" in head
        assert "No Odoo connected: nothing is invoiced" in t
        post(c, "/sales/order", id=oid, do="paid")  # no reason: refused
        assert sales.store(rsvc).order(oid)["state"] == "pending"
        post(c, "/sales/order", id=oid, reason="statement of 21 Nov", do="paid")
        o = sales.store(rsvc).order(oid)
        assert o["state"] == "active" and o["step"] == "done" and o["invoice"] == {}
        assert o["paid"]["how"] == "hand" and o["paid"]["by"] == "Ada Admin"
        (e,) = admissions(w)
        assert ["paid", "paid"] in e["tags"] and ["by", "checkout"] in e["tags"]
        assert "This order is paid" in c.get(f"/pay/{oid}", headers=H).text
        t = c.get("/sales", headers=H).text
        assert "Waiting for a transfer" not in t
        # the receipt: the event, the ticket, the VAT as the ticket states it, who paid how
        (m,) = mails("Your Day ticket · Garden day: receipt")
        body = text_of(m)
        assert f"Receipt {ref}, from" in body
        assert "Event: Garden day" in body and "Ticket: Day ticket" in body
        assert "Amount: €50.00 + 21% VAT €10.50 = €60.50" in body
        assert "Paid by bank transfer on " in body
        assert "Billed to: Acme, VAT / tax id BE0123456789, Rue 1, 1000 Brussels, BE" in body
        assert "invoice" not in body.lower()
        # refunded by hand: marked here; nobody is taken out of the event by it
        assert "Mark refunded" in t
        post(c, "/sales/order", id=oid, reason="refunded by transfer", do="refunded")
        o = sales.store(rsvc).order(oid)
        assert "refunded_admitted" in o["flags"] and o["closed"]
        assert o["refunded"]["by"] == "Ada Admin"
        assert len(admissions(w)) == 1
        assert "Mark refunded" not in c.get("/sales", headers=H).text


def test_a_ticket_without_vat_and_the_ways_to_pay(fete, keycast):
    rsvc, w, ada, made, _v = fete
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"])
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        # Stripe only: no transfer to offer
        r = order(c, PrivateKey(), pay="invoice")
        assert r.status_code == 409 and r.json()["error"] == "no_transfer"
        assert sales.store(rsvc).orders() == []
        me = PrivateKey()
        got = order(c, me).json()
        oid = got["order"]
        assert got["card_url"] and got["pay_url"] is None
        r = call(c, me, f"/checkout/{oid}/pay", {"pay": "invoice"})
        assert r.status_code == 409 and r.json()["error"] == "no_transfer"
        # with a bank account the buyer may change to a transfer, on the page or over the API
        sales.save_settings(rsvc, BANK)
        page = c.get(f"/pay/{oid}", headers=H).text
        assert "Pay by card" in page and "Pay by transfer instead" in page
        assert "BE68 5390" not in page and "get the invoice" not in page
        r = call(c, me, f"/checkout/{oid}/pay", {"pay": "invoice"})
        assert r.status_code == 200 and r.json()["state"] == "pending"
        assert r.json()["pay_url"] and r.json()["card_url"]
        assert len(mails("How to pay for your Day ticket")) == 1
        page = c.get(f"/pay/{oid}", headers=H).text
        assert "BE68 5390 0754 7034" in page and sales.reference_of(oid) in page
        assert "Pay by card" in page  # the card session still works
        assert deliver(c, event(oid, oid, 5000)).json()["result"] == "paid"
        assert sales.store(rsvc).order(oid)["state"] == "active"
    body = text_of(mails("Your Day ticket · Garden day: receipt")[0])
    assert "Amount: €50.00" in body and "VAT" not in body and "Paid by card" in body
    # no way to pay at all: the refusal asks for one, and for nothing of Odoo
    sales.set_secret(rsvc, "stripe_key", None)
    sales.save_settings(rsvc, {"bank_holder": "", "bank_iban": ""})
    with TestClient(create_app(rsvc)) as c:
        r = order(c, PrivateKey(), email="x@example.org")
        assert r.status_code == 409 and r.json()["error"] == "cannot_sell"
        assert "connect Stripe" in r.json()["message"] and "Odoo" not in r.json()["message"]
        got = call(c, PrivateKey(), f"/events/{D}", method="GET").json()
        assert got["tickets"][0]["can_pay"] is False


def test_an_unpaid_order_expires_and_a_late_payment_reopens_it(fete, keycast):
    rsvc, w, ada, _made, _v = fete
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["quantity", "1"])
    sales.save_settings(rsvc, BANK)
    mailer.SENT.clear()
    st = sales.store(rsvc)
    with TestClient(create_app(rsvc)) as c:
        got = order(c, PrivateKey(), pay="invoice").json()
        oid = got["order"]
        r = order(c, PrivateKey(), email="b@example.org")
        assert r.status_code == 409 and r.json()["error"] == "sold_out"
        # one reminder two days before the end, then it expires and frees its place
        st.update(oid, hold_until=sales.iso(sales.now() + dt.timedelta(days=1)))
        sales.tick(rsvc, force=True)
        sales.tick(rsvc, force=True)
        assert len(mails("is held until")) == 1
        st.update(oid, hold_until=sales.iso(sales.now() - dt.timedelta(minutes=1)))
        sales.tick(rsvc, force=True)
        o = st.order(oid)
        assert o["state"] == "expired" and o["step"] == "done" and admissions(w) == []
        assert "This order has ended" in c.get(f"/pay/{oid}", headers=H).text
        other = order(c, PrivateKey(), email="b@example.org").json()
        assert other["state"] == "pending"
        # the transfer was made in time after all: an admin reopens it
        sales.reopen_paid(rsvc, oid, "came on day 8", "Ada Admin")
        o = st.order(oid)
        assert o["state"] == "active" and "paid_late" in o["flags"] and "oversold" in o["flags"]
        assert len(admissions(w)) == 1
        # a card payment that comes after its order was cancelled reopens it too
        sales.cancel(rsvc, other["order"], "by mistake", "Ada Admin")
        assert st.order(other["order"])["state"] == "cancelled"
        ev = event(other["order"], other["order"], 5000, eid="evt_late")
        assert deliver(c, ev).json()["result"] == "paid"
        assert st.order(other["order"])["state"] == "active"


def test_a_code_at_100_and_a_free_product_need_nothing_set(shop, keycast):
    rsvc = shop[0]
    code = free_code(rsvc)
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, PrivateKey(), code=code)
        assert r.status_code == 201 and r.json()["state"] == "active"
        assert "reference" not in r.json()
    o = sales.store(rsvc).orders()[0]
    assert o["books"] == "none" and o["step"] == "done"


def test_a_pass_refunded_by_hand_closes(plain, keycast):
    rsvc, _made, _v = plain
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey()).json()["order"]
        assert sales.mark_refunded(rsvc, oid, "x", "Ada")["state"] == "pending"  # not paid: no
        deliver(c, event(oid, oid, 20000))
    o = sales.mark_refunded(rsvc, oid, "refunded in Stripe", "Ada Admin")
    assert o["closed"] and "refunded" in o["flags"]
    passes = rsvc.odoo.search_read("belfry.pass", [["sale_order", "=", oid]], ["state"])
    assert [p["state"] for p in passes] == ["revoked"]
    assert sales.mark_refunded(rsvc, oid, "again", "Ada Admin")["refunded"]["by"] == "Ada Admin"


def test_a_tester_pays_in_stripes_test_mode_with_no_test_odoo(envs, keycast):
    """Production keeps its Odoo; the test environment has Stripe's test mode only."""
    rsvc, live_odoo, test_odoo, made = envs
    sales.save_settings(rsvc, {"odoo_url": None, "odoo_db": None}, "test")
    sales.set_secret(rsvc, "odoo_api_key", None, "test")
    assert not sales.odoo_ready(rsvc, "test") and sales.can_sell(rsvc, "test")
    assert "receipt and no invoice" in plugins.tester_note(rsvc)
    tess, _pid = make_tester(rsvc)
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        got = buy(c, tess, email=TESTER).json()
        oid = got["order"]
        assert got["test"] is True and got["card_url"] and got["invoice"] is None
        assert sales.store(rsvc).order(oid)["books"] == "none"
        ev = {**event(oid, oid, 20000, eid="evt_t"), "livemode": False}
        # the production hook never settles a test order
        live = {**ev, "livemode": True}
        assert deliver_to(c, live, LIVE_WH, "/hooks/stripe").json()["result"] == "wrong_env"
        assert deliver_to(c, ev, TEST_WH, "/hooks/stripe/test").json()["result"] == "paid"
        o = sales.store(rsvc).order(oid)
        assert o["state"] == "active" and o["step"] == "done" and o["invoice"] == {}
        # a normal buyer's order is still invoiced in production's Odoo
        got = buy(c, PrivateKey(), email="ada@example.org", pay="invoice").json()
        assert got["state"] == "invoiced" and got["invoice"]["number"] and "reference" not in got
        assert sales.store(rsvc).order(got["order"])["books"] == "odoo"
    assert len(live_odoo.records["account.move"]) == 1
    assert not test_odoo.records.get("account.move")
    (m,) = mails("Two-week pass: receipt")
    assert str(m["Subject"]).startswith("[TEST]") and "Paid by card" in text_of(m)


def test_odoo_connected_later_leaves_older_orders_with_their_receipt(plain, keycast, monkeypatch):
    rsvc, _made, _v = plain
    from .fakes import FakeSellerOdoo

    with TestClient(create_app(rsvc)) as c:
        old = buy(c, PrivateKey()).json()["order"]
        fake = FakeSellerOdoo()
        monkeypatch.setattr(salesodoo, "client", lambda svc: fake)
        sales.save_settings(
            rsvc,
            {
                "odoo_url": "https://xl.odoo.test",
                "odoo_db": "xl",
                "odoo_products": {"village-2027": 1},
                "tax_included": 10,
                "tax_excluded": 11,
            },
        )
        sales.set_secret(rsvc, "odoo_api_key", "odoo-key-" + "x" * 20)
        new = buy(c, PrivateKey(), email="b@example.org").json()["order"]
        assert deliver(c, event(old, old, 20000, eid="evt_a")).json()["result"] == "paid"
        assert deliver(c, event(new, new, 20000, eid="evt_b")).json()["result"] == "paid"
    st = sales.store(rsvc)
    assert st.order(old)["state"] == "active" and st.order(old)["invoice"] == {}
    assert st.order(new)["invoice"]["number"] and len(fake.records["account.move"]) == 1
