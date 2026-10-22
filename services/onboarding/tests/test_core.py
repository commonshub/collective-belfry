from __future__ import annotations

import datetime as dt

import httpx
import pytest

from belfry_onboarding import core
from belfry_onboarding.nostr import hex_to_npub

from .conftest import HOOK_TOKEN, seed_paid_invoice
from .fakes import FakeOdoo


def active_pass(odoo: FakeOdoo, invoice_id: int) -> dict:
    return next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == invoice_id)


# (1) paid invoice -> one Authentik user, one source link, one Keycast preload, pass active
def test_paid_invoice_activates_everything(services, odoo, authentik, keycast):
    ids = seed_paid_invoice(odoo, tier="week")

    assert core.activate_pass(services, ids["invoice"]) == "activated"

    p = active_pass(odoo, ids["invoice"])
    partner = odoo.get("res.partner", ids["partner"])
    assert p["state"] == "active"
    assert p["tier"] == "week"
    assert partner["belfry_member_number"] == "OSV-0001"
    # dates: payment 2027-01-20 < EVENT_START 2027-01-25 -> start at event start, 7 days
    assert p["date_start"] == "2027-01-25"
    assert p["date_end"] == "2027-02-01"
    assert p["transaction_id"] == ids["transaction"]
    assert p["stripe_payment_intent"] == "pi_123"
    assert p["activated_at"]

    assert authentik.user_creates == 1
    assert authentik.connection_creates == 1
    user = authentik.users[0]
    assert user["username"] == "osv-0001"
    assert user["path"] == "belfry/participants"
    assert user["attributes"] == {
        "belfry_member_number": "OSV-0001",
        "odoo_partner_id": ids["partner"],
    }
    assert authentik.connections[0] == {
        "pk": 1,
        "user": 1,
        "source": "pass-bridge",
        "identifier": "OSV-0001",
    }
    assert partner["belfry_authentik_user_id"] == 1
    assert p["authentik_user_id"] == 1

    assert keycast.preloads == 1
    kc_user = keycast.users[f"odoo-partner-{ids['partner']}"]
    assert kc_user["username"] == "osv-0001"
    assert p["keycast_pubkey"] == kc_user["pubkey"]
    assert p["npub"] == partner["belfry_npub"] == hex_to_npub(kc_user["pubkey"])
    assert p["npub"].startswith("npub1")
    assert keycast.status[kc_user["pubkey"]] == "active"


def test_activation_survives_authentik_refusing_the_source_link(services, odoo, authentik, keycast):
    """authentik 2026.8.5 rejects API-created source connections; activation must still complete."""
    authentik.reject_connections = True
    ids = seed_paid_invoice(odoo, tier="day")

    assert core.activate_pass(services, ids["invoice"]) == "activated"

    assert authentik.user_creates == 1
    assert authentik.connection_creates == 0
    assert keycast.preloads == 1
    assert active_pass(odoo, ids["invoice"])["state"] == "active"


def test_full_access_ends_at_event_end_and_late_payment_starts_on_payment_day(services, odoo):
    ids = seed_paid_invoice(odoo, tier="full", paid_on="2027-01-27 09:30:00")
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = active_pass(odoo, ids["invoice"])
    assert p["date_start"] == "2027-01-27"
    assert p["date_end"] == "2027-01-31"


# (2) the same webhook replayed three times -> no additional creates, same npub
def test_webhook_replayed_three_times_creates_nothing_new(client, odoo, authentik, keycast):
    ids = seed_paid_invoice(odoo)
    results = []
    for _ in range(3):
        r = client.post(
            f"/hooks/odoo/invoice-paid?token={HOOK_TOKEN}",
            json={
                "_model": "account.move",
                "_id": ids["invoice"],
                "_action": "Belfry: invoice paid",
            },
        )
        assert r.status_code == 200
        results.append(r.json()["result"])
    assert results == ["activated", "already_active", "already_active"]
    assert len(odoo.tables["belfry.pass"]) == 1
    assert authentik.user_creates == 1
    assert authentik.connection_creates == 1
    assert keycast.preloads == 1
    assert len(keycast.users) == 1
    npub = active_pass(odoo, ids["invoice"])["npub"]
    assert odoo.get("res.partner", ids["partner"])["belfry_npub"] == npub


def test_activation_resumes_after_partial_failure(services, odoo, authentik, keycast):
    """Authentik user created, then Keycast fails: the retry creates no second user or link."""
    ids = seed_paid_invoice(odoo)
    preload = keycast.preload_route
    original = preload.side_effect
    preload.side_effect = lambda request: httpx.Response(500, json={"error": "boom"})
    with pytest.raises(httpx.HTTPStatusError):
        core.activate_pass(services, ids["invoice"])
    assert authentik.user_creates == 1
    assert active_pass(odoo, ids["invoice"])["state"] == "pending"

    preload.side_effect = original
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert authentik.user_creates == 1
    assert authentik.connection_creates == 1
    assert keycast.preloads == 1
    assert active_pass(odoo, ids["invoice"])["state"] == "active"


def test_hook_rejects_bad_token_and_tolerates_errors(client, odoo):
    r = client.post("/hooks/odoo/invoice-paid?token=wrong", json={"_id": 1})
    assert r.status_code == 401
    r = client.post(f"/hooks/odoo/invoice-paid?token={HOOK_TOKEN}", json={"_id": 999})
    assert r.status_code == 200 and r.json()["result"] == "not_found"
    r = client.post(f"/hooks/odoo/invoice-paid?token={HOOK_TOKEN}", json={"_model": "account.move"})
    assert r.status_code == 200 and r.json()["result"] == "ignored"


# (3) unpaid or pending transaction -> nothing created
def test_unpaid_invoice_creates_nothing(services, odoo, authentik, keycast):
    ids = seed_paid_invoice(odoo, payment_state="not_paid", tx_state="pending")
    assert core.activate_pass(services, ids["invoice"]) == "not_paid"
    ids2 = seed_paid_invoice(odoo, payment_state="paid", tx_state="pending")
    assert core.activate_pass(services, ids2["invoice"]) == "not_paid"
    ids3 = seed_paid_invoice(odoo, payment_state="paid", provider_reference=None)
    assert core.activate_pass(services, ids3["invoice"]) == "not_paid"
    ids4 = seed_paid_invoice(odoo, payment_state="in_payment", with_transaction=False)
    assert core.activate_pass(services, ids4["invoice"]) == "not_paid"
    assert odoo.tables["belfry.pass"] == {}
    assert authentik.user_creates == 0
    assert keycast.preloads == 0


def test_manual_payment_accepted_only_when_enabled(services, odoo):
    services.settings.allow_manual_payments = True
    ids = seed_paid_invoice(odoo, payment_state="paid", with_transaction=False)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = active_pass(odoo, ids["invoice"])
    assert p["state"] == "active"
    assert p.get("transaction_id", False) is False
    assert p["date_start"] == "2027-01-25"  # invoice_date 2027-01-19 < event start


# (4) refund -> pass revoked, Keycast suspended
def test_refund_revokes_pass_and_suspends_keycast(client, services, odoo, keycast):
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    refund_id = odoo.add(
        "payment.transaction",
        reference="R-INV/2027/00001",
        state="done",
        operation="refund",
        provider_reference="re_1",
        source_transaction_id=ids["transaction"],
    )
    r = client.post(
        f"/hooks/odoo/refund?token={HOOK_TOKEN}",
        json={"_model": "payment.transaction", "_id": refund_id},
    )
    assert r.status_code == 200 and r.json()["result"] == "revoked"
    p = active_pass(odoo, ids["invoice"])
    assert p["state"] == "revoked"
    assert p["revoke_reason"] == "refund"
    assert p["revoked_at"]
    assert keycast.status[p["keycast_pubkey"]] == "suspended"
    # replay
    r = client.post(f"/hooks/odoo/refund?token={HOOK_TOKEN}", json={"_id": refund_id})
    assert r.json()["result"] == "already_revoked"
    assert p["state"] == "revoked"
    # a pending refund does nothing
    pending = odoo.add(
        "payment.transaction",
        state="pending",
        operation="refund",
        source_transaction_id=ids["transaction"],
    )
    assert core.revoke_for_refund(services, pending) == "not_refund"


# (5) a second active pass for the same partner keeps Keycast active on refund of the first
def test_refund_keeps_keycast_active_with_second_active_pass(services, odoo, keycast):
    first = seed_paid_invoice(odoo, tier="day")
    assert core.activate_pass(services, first["invoice"]) == "activated"
    second = seed_paid_invoice(odoo, tier="full", partner_id=first["partner"])
    odoo.get("payment.transaction", second["transaction"])["provider_reference"] = "pi_456"
    assert core.activate_pass(services, second["invoice"]) == "activated"
    assert keycast.preloads == 2 and len(keycast.users) == 1  # same partner, same key

    refund_id = odoo.add(
        "payment.transaction",
        state="done",
        operation="refund",
        source_transaction_id=first["transaction"],
    )
    assert core.revoke_for_refund(services, refund_id) == "revoked"
    assert active_pass(odoo, first["invoice"])["state"] == "revoked"
    assert active_pass(odoo, second["invoice"])["state"] == "active"
    pubkey = active_pass(odoo, first["invoice"])["keycast_pubkey"]
    assert keycast.status[pubkey] == "active"
    assert (pubkey, "suspended") not in keycast.status_calls


# (6) expiry job expires only passes with date_end < today
def test_expire_only_passes_past_date_end(services, odoo, keycast, monkeypatch):
    fake_today = dt.date(2027, 1, 28)
    monkeypatch.setattr(core, "today", lambda: fake_today)
    a = seed_paid_invoice(odoo, tier="day")  # ends 2027-01-26
    core.activate_pass(services, a["invoice"])
    b = seed_paid_invoice(odoo, tier="full")  # ends 2027-01-31
    odoo.get("payment.transaction", b["transaction"])["provider_reference"] = "pi_b"
    core.activate_pass(services, b["invoice"])
    c = seed_paid_invoice(odoo, tier="week")  # ends 2027-02-01, still pending (never activated)
    assert active_pass(odoo, a["invoice"])["date_end"] == "2027-01-26"

    result = core.expire(services)
    assert result["expired"] == [active_pass(odoo, a["invoice"])["id"]]
    pa, pb = active_pass(odoo, a["invoice"]), active_pass(odoo, b["invoice"])
    assert pa["state"] == "expired" and pa["revoke_reason"] == "expiry" and pa["revoked_at"]
    assert pb["state"] == "active"
    assert keycast.status[pa["keycast_pubkey"]] == "suspended"
    assert keycast.status[pb["keycast_pubkey"]] == "active"
    assert all(
        p["invoice_id"] != c["invoice"] or p["state"] == "pending"
        for p in odoo.tables["belfry.pass"].values()
    )
    # running it again changes nothing
    assert core.expire(services) == {"expired": []}


def test_reconcile_activates_missed_invoices_and_revokes_refunds(
    services, odoo, authentik, keycast, monkeypatch
):
    monkeypatch.setattr(core, "today", lambda: dt.date(2027, 1, 21))
    missed = seed_paid_invoice(odoo)
    done = seed_paid_invoice(odoo)
    odoo.get("payment.transaction", done["transaction"])["provider_reference"] = "pi_done"
    core.activate_pass(services, done["invoice"])
    odoo.add(
        "payment.transaction",
        state="done",
        operation="refund",
        source_transaction_id=done["transaction"],
    )
    unpaid = seed_paid_invoice(odoo, payment_state="not_paid", tx_state="pending")

    result = core.reconcile(services)
    assert result["activated"] == {missed["invoice"]: "activated"}
    assert active_pass(odoo, missed["invoice"])["state"] == "active"
    assert list(result["revoked"].values()) == ["revoked"]
    assert active_pass(odoo, done["invoice"])["state"] == "revoked"
    assert not any(
        p["invoice_id"] == unpaid["invoice"] for p in odoo.tables["belfry.pass"].values()
    )
    # second run is a no-op
    assert core.reconcile(services) == {"activated": {}, "revoked": {}}


def test_reconcile_endpoint_requires_token(client):
    assert client.post("/admin/reconcile").status_code == 401
    r = client.post(f"/admin/reconcile?token={HOOK_TOKEN}")
    assert r.status_code == 200 and r.json() == {"activated": {}, "revoked": {}}


def test_legacy_module_fallbacks(settings, authentik, keycast):
    """Without the module's RPC methods the service numbers members and creates passes itself."""
    from belfry_onboarding.clients.authentik import AuthentikClient
    from belfry_onboarding.clients.keycast import KeycastClient

    odoo = FakeOdoo(legacy_module=True)
    services = core.Services(
        odoo=odoo,
        authentik=AuthentikClient("http://authentik.test", "t"),
        keycast=KeycastClient("http://keycast.test", "a", "s"),
        settings=settings,
    )
    ids = seed_paid_invoice(odoo, tier="day")
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = active_pass(odoo, ids["invoice"])
    assert p["tier"] == "day"
    assert (
        odoo.get("res.partner", ids["partner"])["belfry_member_number"]
        == f"OSV-{ids['partner']:04d}"
    )
    assert p["date_end"] == "2027-01-26"


def test_existing_authentik_user_found_by_email_is_reused(services, odoo, authentik):
    authentik.users.append(
        {"pk": 7, "username": "alice", "email": "alice@example.com", "attributes": {}}
    )
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert authentik.user_creates == 0
    assert authentik.connection_creates == 1
    assert authentik.connections[0]["user"] == 7
    assert active_pass(odoo, ids["invoice"])["authentik_user_id"] == 7


def test_keycast_client_sends_tenant_host_header(router):
    from belfry_onboarding.clients.keycast import KeycastClient

    route = router.post("http://keycast.test/api/admin/preload-user").mock(
        return_value=httpx.Response(200, json={"pubkey": "ab" * 32, "token": "t"})
    )
    KeycastClient("http://keycast.test", "a", "s", tenant_domain="belfry.example").preload_user(
        "odoo-partner-1", "osv-0001", "Alice"
    )
    assert route.calls.last.request.headers["host"] == "belfry.example"
    assert route.calls.last.request.headers["authorization"] == "Bearer a"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_npub_encoding_known_vector():
    # Test vector from NIP-19
    hex_pk = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
    assert hex_to_npub(hex_pk) == "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"
