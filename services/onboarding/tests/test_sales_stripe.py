"""Card payments (salesstripe.py): a Checkout session from the posted invoice, the webhook with
its signature, idempotent on the event and on the order."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse

import httpx
import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import mailer, sales
from belfry_onboarding.app import create_app
from belfry_onboarding.stripe_sig import sign_header

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures

WHSEC = "whsec_" + "s" * 32
H = {"Host": "belfry.belfry.test"}


@pytest.fixture
def stripe(seller, router):
    rsvc = seller[0]
    sales.set_secret(rsvc, "stripe_key", "rk_test_" + "k" * 24)
    sales.set_secret(rsvc, "stripe_webhook_secret", WHSEC)
    made: list[dict] = []
    state = {"fail": 0}

    def create(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(state["fail"], json={"error": {"code": "nope"}})
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        made.append(
            {
                "form": form,
                "idem": request.headers.get("idempotency-key"),
                "auth": request.headers.get("authorization"),
            }
        )
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
    expired = router.post(
        url__regex=r"https://api\.stripe\.com/v1/checkout/sessions/.+/expire"
    ).mock(return_value=httpx.Response(200, json={}))
    return seller, made, state, expired


def event(oid: str, invoice: str, amount: int, eid: str = "evt_1", **obj) -> dict:
    return {
        "id": eid,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "payment_status": "paid",
                "amount_total": amount,
                "currency": "eur",
                "client_reference_id": invoice,
                "payment_intent": "pi_123",
                "metadata": {"belfry_order": oid, "invoice": invoice},
                **obj,
            }
        },
    }


def deliver(c, ev: dict, secret: str = WHSEC):
    body = json.dumps(ev).encode()
    return c.post(
        "/hooks/stripe",
        content=body,
        headers={
            **H,
            "Stripe-Signature": sign_header(secret, body),
            "Content-Type": "application/json",
        },
    )


def test_pay_by_card_end_to_end(stripe, keycast):
    """No invoice until paid (Xavier, 2026-11-17): the session is made from the order; once
    paid, the pass opens, then the invoice is made, the payment goes on it, and the receipt
    carries the PDF."""
    (rsvc, fake, _v), made, _state, _exp = stripe
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, me, pay="card", return_url="https://belfry.test/tickets/thanks")
        assert r.status_code == 201, r.text
        got = r.json()
        oid = got["order"]
        assert got["state"] == "pending" and got["invoice"] is None
        assert got["card_url"].endswith("cs_test_1") and got["pay_url"]
        assert "account.move" not in fake.records or not fake.records["account.move"]
        f = made[0]["form"]
        assert f["line_items[0][price_data][unit_amount]"] == "20000"
        assert f["line_items[0][price_data][product_data][name]"] == "Two-week pass"
        assert f["client_reference_id"] == oid and f["metadata[belfry_order]"] == oid
        assert "metadata[invoice]" not in f
        assert f["success_url"] == f"https://belfry.test/tickets/thanks?order={oid}"
        assert made[0]["idem"] == f"belfry-{oid}-1"
        # a forged signature is refused
        r = deliver(c, event(oid, oid, 20000), secret="whsec_other")
        assert r.status_code == 400
        # a payment that isn't the order's amount: flagged, nothing opens
        r = deliver(c, event(oid, oid, 100, eid="evt_0"))
        assert r.json()["result"] == "mismatch"
        assert "stripe_mismatch" in sales.store(rsvc).order(oid)["flags"]
        # the real one
        r = deliver(c, event(oid, oid, 20000))
        assert r.status_code == 200 and r.json()["result"] == "paid"
        o = sales.store(rsvc).order(oid)
        assert o["state"] == "active" and o["step"] == "done" and o["paid"]["ref"] == "pi_123"
        move = fake.records["account.move"][o["invoice"]["move_id"]]
        assert move["state"] == "posted" and move["payment_state"] == "paid"
        assert move["paid_journal"] == 7 and move["ref"] == "Belfry " + oid
        receipt = [m for m in mailer.SENT if str(m["Subject"]).startswith("Your Two-week pass")]
        assert len(receipt) == 1
        assert [a.get_filename() for a in receipt[0].iter_attachments()] == [
            f"{o['invoice']['number'].replace('/', '-')}.pdf"
        ]
        assert not [m for m in mailer.SENT if str(m["Subject"]).startswith("Invoice")]
        # delivered twice: nothing more
        r = deliver(c, event(oid, oid, 20000))
        assert r.json()["result"] == "duplicate"
        r = deliver(c, event(oid, oid, 20000, eid="evt_2"))
        assert r.json()["result"] == "paid"
        assert len(fake.records["account.payment.register"]) == 1
        assert len(fake.records["account.move"]) == 1
        passes = rsvc.odoo.search_read("belfry.pass", [["sale_order", "=", oid]], ["state"])
        assert [p["state"] for p in passes] == ["active"]
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["state"] == "active" and got["card_url"] is None and got["bunker_url"]
        assert got["invoice"]["number"] == o["invoice"]["number"]


def test_an_unpaid_card_order_leaves_no_invoice(stripe):
    (rsvc, fake, _v), _made, _s, expired = stripe
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="card").json()["order"]
    sales.store(rsvc).update(oid, hold_until=sales.iso(sales.now() - dt.timedelta(minutes=1)))
    sales.tick(rsvc, force=True)
    assert sales.store(rsvc).order(oid)["state"] == "expired"
    assert not fake.records.get("account.move") and expired.called


def test_switching_between_card_and_transfer(stripe, keycast):
    (rsvc, fake, _v), made, _s, _e = stripe
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, me, pay="card").json()["order"]
        # the order's page offers to pay by transfer instead (no bank details yet)
        page = c.get(f"/pay/{oid}", headers=H).text
        assert "Pay by transfer instead" in page and "BE68" not in page
        t = re.search(r'name="t" value="([^"]+)"', page).group(1)
        assert c.post(f"/pay/{oid}/invoice", data={"t": "nope"}, headers=H).status_code == 404
        r = c.post(f"/pay/{oid}/invoice", data={"t": t}, headers=H, follow_redirects=False)
        assert r.status_code == 303
        o = sales.store(rsvc).order(oid)
        assert o["pay"] == "invoice" and o["state"] == "invoiced" and o["invoice"]["number"]
        # a new card session for the invoice's amount, the old one expired
        assert made[-1]["form"]["client_reference_id"] == o["invoice"]["number"]
        assert "BE68 5390 0754 7034" in c.get(f"/pay/{oid}", headers=H).text
        assert any(str(m["Subject"]).startswith("Invoice") for m in mailer.SENT)
        # and back to card over the API: the session for that invoice stays
        r = call(c, me, f"/checkout/{oid}/pay", {"pay": "card"})
        assert r.status_code == 200 and r.json()["card_url"] and r.json()["invoice"]
        assert call(c, PrivateKey(), f"/checkout/{oid}/pay", {"pay": "card"}).status_code == 404
        # paid by card: the payment goes on that invoice
        r = deliver(c, event(oid, o["invoice"]["number"], 20000))
        assert r.json()["result"] == "paid"
        assert len(fake.records["account.move"]) == 1
        assert fake.records["account.move"][1]["paid_journal"] == 7
        # asking POST /checkout again with another pay switches the same open order
        me2 = PrivateKey()
        oid2 = buy(c, me2, pay="card", email="z@example.org").json()["order"]
        r = buy(c, me2, pay="invoice", email="z@example.org")
        assert r.status_code == 200 and r.json()["order"] == oid2 and r.json()["invoice"]


def test_odoo_down_after_a_card_payment_still_opens_the_pass(stripe, keycast):
    (rsvc, fake, _v), _made, _s, _e = stripe
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="card").json()["order"]
        fake.down = True
        r = deliver(c, event(oid, oid, 20000))
        assert r.json()["result"] == "paid"
        o = sales.store(rsvc).order(oid)
        assert o["state"] == "active" and o["step"] == "invoice"
        fake.down = False
        sales.tick(rsvc, force=True)
        o = sales.store(rsvc).order(oid)
        assert o["step"] == "done"
        assert fake.records["account.move"][o["invoice"]["move_id"]]["payment_state"] == "paid"


def test_stripe_down_then_up_and_expired_sessions(stripe):
    (rsvc, _f, _v), made, state, expired = stripe
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        state["fail"] = 503
        got = buy(c, me, pay="card").json()
        assert got["state"] == "pending" and got["card_url"] is None
        oid = got["order"]
        assert sales.store(rsvc).order(oid)["step"] == "card"
        state["fail"] = 0
        sales._polled.clear()
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["card_url"].endswith("cs_test_1")
        # the session expired at Stripe: a new one, with a new idempotency key
        st = sales.store(rsvc)
        o = st.order(oid)
        st.update(oid, stripe={**o["stripe"], "expires_at": int(time.time()) - 10})
        sales._polled.clear()  # a buyer's poll moves an order at most every 10 seconds
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["card_url"].endswith("cs_test_2") and made[-1]["idem"] == f"belfry-{oid}-2"
        # Stripe refusing: pay by transfer instead
        state["fail"] = 400
        oid2 = buy(c, PrivateKey(), pay="card", email="z@example.org").json()["order"]
        o2 = st.order(oid2)
        assert o2["step"] == "wait" and "card_failed" in o2["flags"]
        state["fail"] = 0
    # an order that ends expires its session
    sales.cancel(rsvc, oid, "test", "Ada")
    assert expired.called
