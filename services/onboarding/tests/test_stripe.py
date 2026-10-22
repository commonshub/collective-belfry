from __future__ import annotations

import json
import time

import pytest

from belfry_onboarding import core
from belfry_onboarding.stripe_sig import StripeSignatureError, sign_header, verify_signature

from .conftest import STRIPE_SECRET, seed_paid_invoice


def stripe_event(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}}
    ).encode()


def post_event(client, payload: bytes, secret: str = STRIPE_SECRET, header: str | None = None):
    header = header if header is not None else sign_header(secret, payload)
    return client.post(
        "/hooks/stripe",
        content=payload,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )


def active_pass(odoo, invoice_id):
    return next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == invoice_id)


# (7) dispute event delivered twice -> one revoke, second call returns duplicate;
#     bad signature -> 400
def test_dispute_created_twice_revokes_once(client, services, odoo, keycast):
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    payload = stripe_event(
        "evt_1",
        "charge.dispute.created",
        {"id": "dp_1", "payment_intent": "pi_123", "status": "needs_response"},
    )

    r1 = post_event(client, payload)
    assert r1.status_code == 200
    assert r1.json()["result"] == "revoked"
    p = active_pass(odoo, ids["invoice"])
    assert (
        p["state"] == "revoked"
        and p["revoke_reason"] == "chargeback"
        and p["stripe_dispute_id"] == "dp_1"
    )
    assert keycast.status[p["keycast_pubkey"]] == "suspended"
    suspend_calls = len(keycast.status_calls)

    r2 = post_event(client, payload)
    assert r2.status_code == 200
    assert r2.json()["result"] == "duplicate"
    assert len(keycast.status_calls) == suspend_calls
    events = list(odoo.tables["belfry.stripe.event"].values())
    assert len(events) == 1
    assert (
        events[0]["event_id"] == "evt_1"
        and events[0]["result"] == "revoked"
        and events[0]["pass_id"] == p["id"]
    )


def test_invalid_signature_is_rejected(client, odoo):
    payload = stripe_event("evt_bad", "charge.dispute.created", {"payment_intent": "pi_123"})
    assert post_event(client, payload, secret="whsec_other").status_code == 400
    assert post_event(client, payload, header="").status_code == 400
    assert post_event(client, payload, header="t=1,v1=zz").status_code == 400
    stale = sign_header(STRIPE_SECRET, payload, timestamp=int(time.time()) - 3600)
    assert post_event(client, payload, header=stale).status_code == 400
    assert odoo.tables["belfry.stripe.event"] == {}


def test_verify_signature_accepts_multiple_v1_values():
    payload = b'{"id":"evt_x"}'
    ts = int(time.time())
    good = sign_header(STRIPE_SECRET, payload, ts)
    verify_signature(payload, f"t={ts},v1=deadbeef,{good.split(',')[1]}", STRIPE_SECRET)
    with pytest.raises(StripeSignatureError):
        verify_signature(payload, f"t={ts},v1=deadbeef", STRIPE_SECRET)
    with pytest.raises(StripeSignatureError):
        verify_signature(payload, good, "")


def test_dispute_won_reactivates_and_other_events_are_recorded(
    client, services, odoo, keycast, monkeypatch
):
    monkeypatch.setattr(core, "today", lambda: __import__("datetime").date(2027, 1, 26))
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    post_event(
        client,
        stripe_event("evt_c", "charge.dispute.created", {"id": "dp_2", "payment_intent": "pi_123"}),
    )
    assert active_pass(odoo, ids["invoice"])["state"] == "revoked"

    lost = post_event(
        client,
        stripe_event(
            "evt_l",
            "charge.dispute.closed",
            {"id": "dp_2", "payment_intent": "pi_123", "status": "lost"},
        ),
    )
    assert lost.json()["result"] == "ignored_status_lost"
    assert active_pass(odoo, ids["invoice"])["state"] == "revoked"

    won = post_event(
        client,
        stripe_event(
            "evt_w",
            "charge.dispute.closed",
            {"id": "dp_2", "payment_intent": "pi_123", "status": "won"},
        ),
    )
    assert won.json()["result"] == "reactivated"
    p = active_pass(odoo, ids["invoice"])
    assert p["state"] == "active" and p["revoke_reason"] is False
    assert keycast.status[p["keycast_pubkey"]] == "active"

    other = post_event(client, stripe_event("evt_o", "payment_intent.succeeded", {"id": "pi_123"}))
    assert other.json()["result"] == "ignored"
    assert {e["event_id"]: e["result"] for e in odoo.tables["belfry.stripe.event"].values()} == {
        "evt_c": "revoked",
        "evt_l": "ignored_status_lost",
        "evt_w": "reactivated",
        "evt_o": "ignored",
    }
    unknown = post_event(
        client,
        stripe_event(
            "evt_u", "charge.dispute.created", {"id": "dp_9", "payment_intent": "pi_nope"}
        ),
    )
    assert unknown.json()["result"] == "no_pass"
