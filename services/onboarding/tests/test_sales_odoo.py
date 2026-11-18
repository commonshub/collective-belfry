"""Selling passes with the seller's Odoo (salesodoo.py): the invoice first, transfers read by the
tick, the hold's expiry and its credit note, VIES."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import mailer, sales, salesodoo
from belfry_onboarding.app import create_app

from .fakes import FakeSellerOdoo
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, free_code, shop  # noqa: F401 — fixtures


class FakeVies:
    def __init__(self) -> None:
        self.answer: bool | None = True
        self.asked: list[tuple[str, str]] = []

    def check(self, country: str, number: str) -> bool | None:
        self.asked.append((country, number))
        return self.answer


@pytest.fixture
def seller(shop, monkeypatch):
    rsvc = shop[0]
    fake = FakeSellerOdoo()
    monkeypatch.setattr(salesodoo, "client", lambda svc: fake)
    vies = FakeVies()
    monkeypatch.setattr(salesodoo, "VIES", vies)
    sales.save_settings(
        rsvc,
        {
            "odoo_url": "https://xl.odoo.test",
            "odoo_db": "xl",
            "company": 1,
            "odoo_products": {"village-2027": 1},
            "tax_included": 10,
            "tax_excluded": 11,
            "card_journal": 7,
            "manual_journal": 8,
            "bank_holder": "XL Collective",
            "bank_iban": "BE68539007547034",
            "bank_bic": "GKCCBEBB",
        },
    )
    sales.set_secret(rsvc, "odoo_api_key", "odoo-key-" + "x" * 20)
    return rsvc, fake, vies


def order_of(rsvc, oid):
    return sales.store(rsvc).order(oid)


def test_an_invoice_first_then_a_transfer_opens_the_pass(seller, keycast):
    rsvc, fake, _v = seller
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, me, pay="invoice", return_url="https://belfry.test/tickets/thanks")
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["state"] == "invoiced"
        assert got["total"] == {"net": 165.29, "vat": 34.71, "gross": 200.0, "percent_off": 0}
        assert got["invoice"]["number"] == "INV/2026/0001"
        assert got["invoice"]["communication"] == "+++000/0000/00001+++"
        assert got["invoice"]["url"] == f"https://belfry.belfry.test/pay/{got['order']}"
        assert got["pay_url"] == f"https://belfry.belfry.test/pay/{got['order']}"
        assert got["card_url"] is None  # no Stripe key
        hold = dt.datetime.strptime(got["hold_until"], "%Y-%m-%dT%H:%M:%SZ")
        assert (
            dt.timedelta(days=6, hours=23)
            < hold - dt.datetime.now(dt.UTC).replace(tzinfo=None)
            <= dt.timedelta(days=7)
        )
        oid = got["order"]
        # the seller's records: a person with the address, a posted invoice with our ref
        move = fake.records["account.move"][1]
        assert move["ref"] == "Belfry " + oid and move["state"] == "posted"
        line = move["invoice_line_ids"][0][2]
        assert line["price_unit"] == 200.0 and line["tax_ids"] == [[6, 0, [10]]]
        partner = fake.records["res.partner"][move["partner_id"]]
        assert partner["email"] == "ada@example.org" and partner["is_company"] is False
        # the transfer details mail
        sent = [m for m in mailer.SENT if "Invoice INV/2026/0001" in str(m["Subject"])]
        assert sent and "/pay/" + oid in sent[0].get_body(("plain",)).get_content()
        # asking again: the same order, no second invoice
        assert buy(c, me, pay="invoice").json()["order"] == oid
        assert len(fake.records["account.move"]) == 1
        # nothing paid: the tick changes nothing
        sales.tick(rsvc, force=True)
        assert order_of(rsvc, oid)["state"] == "invoiced"
        # the bank transfer is reconciled in Odoo
        move["payment_state"] = "paid"
        sales.tick(rsvc, force=True)
        o = order_of(rsvc, oid)
        assert o["state"] == "active" and o["pass_id"]
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["state"] == "active" and got["bunker_url"].startswith("bunker://")
        # a second tick doesn't activate it twice
        sales.tick(rsvc, force=True)
        passes = rsvc.odoo.search_read("belfry.pass", [["sale_order", "=", oid]], ["state"])
        assert [p["state"] for p in passes] == ["active"]
        # a full credit note later: the pass closes
        move["payment_state"] = "reversed"
        sales.tick(rsvc, force=True)
        passes = rsvc.odoo.search_read("belfry.pass", [["sale_order", "=", oid]], ["state"])
        assert [p["state"] for p in passes] == ["revoked"]


def test_an_organisation_pays_vat_on_top_after_vies(seller):
    rsvc, fake, vies = seller
    billing = {
        "kind": "organisation",
        "name": "Acme",
        "vat": "BE 0123.456.789",
        "street": "Rue 1",
        "zip": "1000",
        "city": "Brussels",
        "country": "BE",
        "invoice_email": "books@acme.test",
    }
    with TestClient(create_app(rsvc)) as c:
        vies.answer = False
        r = buy(c, PrivateKey(), pay="invoice", billing=billing)
        assert r.status_code == 422 and r.json()["error"] == "invalid_vat"
        vies.answer = None  # VIES down: taken, flagged
        r = buy(c, PrivateKey(), pay="invoice", billing=billing, email="eve@acme.test")
        assert r.status_code == 201
        got = r.json()
        assert got["total"] == {"net": 200.0, "vat": 42.0, "gross": 242.0, "percent_off": 0}
        o = order_of(rsvc, got["order"])
        assert "vies_down" in o["flags"]
    assert vies.asked[-1] == ("BE", "0123456789")
    move = fake.records["account.move"][1]
    partner = fake.records["res.partner"][move["partner_id"]]
    assert partner["is_company"] is True and partner["vat"] == "BE0123456789"
    assert partner["email"] == "books@acme.test" and partner["country_id"] == 1
    assert move["invoice_line_ids"][0][2]["tax_ids"] == [[6, 0, [11]]]


def test_odoo_down_keeps_the_order_and_the_tick_retries(seller):
    rsvc, fake, _v = seller
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fake.down = True
        r = buy(c, me, pay="invoice")
        assert r.status_code == 201 and r.json()["state"] == "pending"
        assert r.json()["invoice"] is None
        oid = r.json()["order"]
        assert order_of(rsvc, oid)["step"] == "invoice"
        fake.down = False
        sales.tick(rsvc, force=True)
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["state"] == "invoiced" and got["invoice"]["number"] == "INV/2026/0001"


def test_odoo_refusing_ends_the_order_and_gives_the_code_back(seller):
    rsvc, fake, _v = seller
    code = free_code(rsvc, uses=1, percent=50)
    with TestClient(create_app(rsvc)) as c:
        fake.refuse = True
        r = buy(c, PrivateKey(), pay="invoice", code=code)
        assert r.status_code == 502 and r.json()["error"] == "accounting"
        fake.refuse = False
        o = sales.store(rsvc).orders()[0]
        assert o["state"] == "cancelled"
        assert sales.store(rsvc).code(code)["used"] == []
        r = buy(c, PrivateKey(), pay="invoice", code=code, email="b@example.org")
        assert r.status_code == 201
        assert r.json()["total"]["gross"] == 100.0 and r.json()["total"]["percent_off"] == 50
        assert fake.records["account.move"][1]["invoice_line_ids"][0][2]["discount"] == 50.0


def test_an_unpaid_order_expires_with_a_credit_note_and_leaves_no_account(seller):
    rsvc, fake, _v = seller
    code = free_code(rsvc, uses=1, percent=10)
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice", code=code).json()["order"]
    st = sales.store(rsvc)
    # two days before the end: one reminder
    st.update(oid, hold_until=sales.iso(sales.now() + dt.timedelta(days=1)))
    sales.tick(rsvc, force=True)
    sales.tick(rsvc, force=True)
    reminders = [m for m in mailer.SENT if "is held until" in str(m["Subject"])]
    assert len(reminders) == 1
    st.update(oid, hold_until=sales.iso(sales.now() - dt.timedelta(minutes=1)))
    partners = len(rsvc.odoo.search_read("res.partner", [], ["id"]))
    sales.tick(rsvc, force=True)
    o = st.order(oid)
    assert o["state"] == "expired" and o["step"] == "done"
    assert st.code(code)["used"] == []
    refunds = [m for m in fake.records["account.move"].values() if m["move_type"] == "out_refund"]
    assert len(refunds) == 1 and refunds[0]["state"] == "posted"
    assert fake.records["account.move"][1]["payment_state"] == "reversed"
    assert len(rsvc.odoo.search_read("res.partner", [], ["id"])) == partners
    sales.tick(rsvc, force=True)  # again: no second credit note
    assert (
        len([m for m in fake.records["account.move"].values() if m["move_type"] == "out_refund"])
        == 1
    )


def test_an_admin_marks_an_order_paid_by_hand(seller):
    rsvc, fake, _v = seller
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
    o = sales.mark_paid(rsvc, oid, "hand", "bank statement 12", "Ada Admin")
    assert o["state"] == "active" and o["paid"]["by"] == "Ada Admin"
    move = fake.records["account.move"][1]
    assert move["payment_state"] == "paid" and move["paid_journal"] == 8
    # twice: nothing more
    assert sales.mark_paid(rsvc, oid, "hand")["state"] == "active"
    assert len(fake.records["account.payment.register"]) == 1


ORG_US = {
    "kind": "organisation",
    "name": "Acme Inc.",
    "vat": "12-3456789",  # an EIN
    "street": "1 Main St",
    "street2": "Suite 400",
    "city": "Springfield",
    "region": "IL",
    "country": "US",
}


def test_an_organisation_outside_the_eu_gets_an_invoice_with_its_tax_id(seller):
    rsvc, fake, vies = seller
    us = fake.add("res.country", code="US", name="United States")
    il = fake.add("res.country.state", country_id=us, code="IL", name="Illinois")
    with TestClient(create_app(rsvc)) as c:
        me = PrivateKey()
        r = buy(c, me, pay="invoice", billing=ORG_US, email="eve@acme.test")
        assert r.status_code == 201, r.text
        got = r.json()
        # no reverse charge: the same tax on top as every organisation
        assert got["total"] == {"net": 200.0, "vat": 42.0, "gross": 242.0, "percent_off": 0}
        assert got["state"] == "invoiced"
        r = call(c, me, "/billing", method="GET")
        assert r.status_code == 403  # an unproven address: as before
    assert vies.asked == []  # never on VIES
    o = order_of(rsvc, got["order"])
    assert o["billing"]["vat"] == "12-3456789" and not o["vat_valid"]
    assert o["billing"]["street2"] == "Suite 400" and o["billing"]["region"] == "IL"
    assert "zip" in o["billing"] and o["billing"]["zip"] == ""
    move = fake.records["account.move"][1]
    partner = fake.records["res.partner"][move["partner_id"]]
    assert partner["vat"] == "12-3456789" and partner["country_id"] == us
    assert partner["state_id"] == il and partner["city"] == "Springfield"
    assert partner["street2"] == "Suite 400" and "zip" not in partner
    assert o["tax_id_on"] == "vat" and "narration" not in move
    assert move["invoice_line_ids"][0][2]["tax_ids"] == [[6, 0, [11]]]


def test_odoo_refusing_the_tax_id_falls_back_and_the_invoice_prints_it(seller):
    rsvc, fake, vies = seller
    fake.refuse_partner_fields = {"vat"}
    billing = {**ORG_US, "country": "IN", "vat": "27AAPFU0939F1ZV", "region": "Kerala"}
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, PrivateKey(), pay="invoice", billing=billing, email="eve@acme.test")
        assert r.status_code == 201, r.text
    o = order_of(rsvc, r.json()["order"])
    move = fake.records["account.move"][1]
    partner = fake.records["res.partner"][move["partner_id"]]
    assert "vat" not in partner and partner["company_registry"] == "27AAPFU0939F1ZV"
    assert o["tax_id_on"] == "company_registry"
    assert move["narration"] == "Tax ID: 27AAPFU0939F1ZV"
    # no Indian state in this Odoo: the region goes on the city line, the order goes on
    assert partner["city"] == "Springfield, Kerala" and "state_id" not in partner
    # and when Odoo refuses company_registry too: the note alone
    fake.refuse_partner_fields = {"vat", "company_registry"}
    with TestClient(create_app(rsvc)) as c:
        r = buy(
            c,
            PrivateKey(),
            pay="invoice",
            billing={**billing, "vat": "51 824 753 556", "country": "AU"},
            email="x@acme.test",
        )
        assert r.status_code == 201, r.text
    o = order_of(rsvc, r.json()["order"])
    move = fake.records["account.move"][max(fake.records["account.move"])]
    partner = fake.records["res.partner"][move["partner_id"]]
    assert "vat" not in partner and "company_registry" not in partner
    assert o["tax_id_on"] == "note" and move["narration"] == "Tax ID: 51 824 753 556"
    assert vies.asked == []


def test_billing_fields_limits_and_countries(seller):
    rsvc, fake, vies = seller
    p = sales.product(rsvc, "village-2027")
    sales.save_product(rsvc, {**p, "quantity": None})  # places aren't what this tests
    seq = iter(range(100))
    with TestClient(create_app(rsvc)) as c:

        def try_(**b):
            return buy(
                c,
                PrivateKey(),
                pay="invoice",
                billing={**ORG_US, **b},
                email=f"buyer{next(seq)}@acme.test",
            )

        for bad in ("x", "12_34", "A" * 33, "12-", "<b>1</b>"):
            r = try_(vat=bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid_vat", bad
        for k, n in (("street2", 200), ("region", 100)):
            r = try_(**{k: "a" * (n + 1)})
            assert r.status_code == 422 and r.json()["error"] == "invalid_billing", k
        r = try_(country="USA")
        assert r.status_code == 422 and r.json()["error"] == "invalid_billing"
        # every ISO 3166 alpha-2 country; zip optional everywhere; no tax id is fine too
        for cc in ("CH", "GB", "NO", "JP", "BR", "ZA", "CA", "AE"):
            r = try_(country=cc, zip="")
            assert r.status_code == 201, (cc, r.text)
        r = try_(country="NZ", vat="")
        assert r.status_code == 201
        # an EU organisation keeps VIES and the VAT number's shape
        r = try_(country="DE", vat="12-3456789")
        assert r.status_code == 422 and r.json()["error"] == "invalid_vat"
        vies.asked.clear()
        r = try_(country="GR", vat="EL 123456789", region="Attica")
        assert r.status_code == 201 and vies.asked == [("EL", "123456789")]
        o = order_of(rsvc, r.json()["order"])
        assert o["billing"]["vat"] == "EL123456789" and o["vat_valid"] is True
        # a Northern Ireland number of a GB organisation is an EU one
        vies.asked.clear()
        r = try_(country="GB", vat="XI 123456789")
        assert r.status_code == 201 and vies.asked == [("XI", "123456789")]
        # a non-EU tax id that happens to start like an EU one is never sent to VIES
        vies.asked.clear()
        r = try_(country="MX", vat="DEMO800101AB1")
        assert r.status_code == 201 and vies.asked == []


def test_a_person_pays_the_price_with_vat_in_it(seller):
    rsvc, fake, _v = seller
    with TestClient(create_app(rsvc)) as c:
        r = buy(
            c,
            PrivateKey(),
            pay="invoice",
            billing={"kind": "person", "country": "BE", "street2": "bte 3", "region": "Brabant"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["total"]["gross"] == 200.0
    move = fake.records["account.move"][1]
    assert move["invoice_line_ids"][0][2]["tax_ids"] == [[6, 0, [10]]]  # tax_included
    partner = fake.records["res.partner"][move["partner_id"]]
    assert partner["street2"] == "bte 3" and partner["city"] == "Brabant"
