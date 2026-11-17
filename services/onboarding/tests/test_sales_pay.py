"""belfry.<domain>/pay (salespay.py): the EPC QR, an order's page, free requests and signed
descriptions."""

# ruff: noqa: F811
from __future__ import annotations

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import sales, salespay
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures

H = {"Host": "belfry.belfry.test"}


def test_iban_and_bic_checks():
    assert salespay.iban_ok("BE68 5390 0754 7034")
    assert not salespay.iban_ok("BE68 5390 0754 7035")
    assert salespay.bic_ok("GKCCBEBB") and salespay.bic_ok("gkccbebbxxx")
    assert not salespay.bic_ok("GKCC")
    assert salespay.iban_spaced("be68539007547034") == "BE68 5390 0754 7034"


def test_epc_payload_puts_a_structured_communication_in_its_field():
    p = salespay.epc_payload("XL", "BE68 5390 0754 7034", "gkccbebb", 20000, "+++000/0000/00001+++")
    assert p.split("\n") == [
        "BCD",
        "002",
        "1",
        "SCT",
        "GKCCBEBB",
        "XL",
        "BE68539007547034",
        "EUR200.00",
        "",
        "+++000/0000/00001+++",
        "",
    ]
    p = salespay.epc_payload("XL", "BE68539007547034", "", None, "Invoice 42")
    assert p.split("\n")[7:] == ["", "", "", "Invoice 42"]
    assert salespay.qr_svg(p).startswith("<svg")


def test_no_bank_details_no_page(shop):
    with TestClient(create_app(shop[0])) as c:
        assert c.get("/pay", headers=H).status_code == 404


def test_an_orders_page_and_free_requests(seller):
    rsvc = seller[0]
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
        r = c.get(f"/pay/{oid}", headers=H)
        assert r.status_code == 200
        t = r.text
        assert "€200.00" in t and "+++000/0000/00001+++" in t and "BE68 5390 0754 7034" in t
        assert 'data-copy="BE68539007547034"' in t and "<svg" in t
        assert c.get("/pay/o_" + "a" * 16, headers=H).status_code == 404
        # free-standing: only acceptable parts show; never unsigned free text
        r = c.get("/pay?amount=12.50&communication=Room+B+(May)", headers=H)
        assert "€12.50" in r.text and "Room B (May)" in r.text
        r = c.get(
            "/pay?amount=abc&communication=<script>alert(1)</script>&description=Pay+now", headers=H
        )
        assert r.status_code == 200
        assert (
            "alert" not in r.text and "Pay now" not in r.text and "€" not in r.text.split("<dl>")[1]
        )
        # signed by the belfry: the description shows
        link = salespay.make_link(rsvc, "30", "Coffee 7", "Coffee beans for the Hub")
        assert "sig=" in link
        r = c.get(link.split("belfry.belfry.test", 1)[1], headers=H)
        assert "Coffee beans for the Hub" in r.text and "€30.00" in r.text
        # the same with another amount: the signature doesn't hold
        tampered = link.replace("amount=30.00", "amount=31.00")
        r = c.get(tampered.split("belfry.belfry.test", 1)[1], headers=H)
        assert "Coffee beans" not in r.text
        # paid orders show no bank details
        sales.mark_paid(rsvc, oid, "hand")
        assert "This order is paid" in c.get(f"/pay/{oid}", headers=H).text


def test_admins_make_pay_links_over_the_api(seller, world):
    rsvc = seller[0]
    ada = world[3]
    with TestClient(create_app(rsvc)) as c:
        r = call(c, PrivateKey(), "/pay-links", {"amount": "5"})
        assert r.status_code == 403
        r = call(c, ada, "/pay-links", {"amount": "5", "description": "Locker"})
        assert r.status_code == 201 and "description=Locker" in r.json()["url"]
        r = call(c, ada, "/pay-links", {"communication": "x" * 141})
        assert r.status_code == 422
