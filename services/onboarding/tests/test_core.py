from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from belfry_onboarding import core
from belfry_onboarding.clients.signer import SignerError
from belfry_onboarding.nostr import hex_to_npub

from .conftest import HOOK_TOKEN, seed_paid_invoice
from .fakes import FakeOdoo


def active_pass(odoo: FakeOdoo, invoice_id: int) -> dict:
    return next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == invoice_id)


# (1) paid invoice -> one signer account, pass active
def test_paid_invoice_activates_everything(services, odoo, signer):
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

    assert signer.creates == 1
    account = signer.accounts[f"odoo-partner-{ids['partner']}"]
    assert account["custody"] == "derived" and account["key_version"] == 0
    assert p["nostr_pubkey"] == account["pubkey"]
    assert p["npub"] == partner["belfry_npub"] == hex_to_npub(account["pubkey"])
    assert p["npub"].startswith("npub1")
    assert account["status"] == "active"


def test_profile_published_with_name_and_member_number_only(services, odoo, signer, relay):
    ids = seed_paid_invoice(odoo, tier="day")
    assert core.activate_pass(services, ids["invoice"]) == "activated"

    by_kind = {e["kind"]: e for e in relay.events}
    assert sorted(by_kind) == [0, 3, 10002, 10050]
    ev = by_kind[0]
    partner = odoo.get("res.partner", ids["partner"])
    assert ev["pubkey"] == active_pass(odoo, ids["invoice"])["nostr_pubkey"]
    assert json.loads(ev["content"]) == {
        "name": partner["name"],
        "display_name": partner["name"],
        "nip05": "osv-0001@belfry.local",
        "picture": f"{services.settings.public_url}/avatar/{ev['pubkey']}.png",
    }
    # empty follow list; both relay lists name the community relay (RELAY_URL without a public one)
    assert by_kind[3]["tags"] == []
    assert by_kind[10002]["tags"] == [["r", relay.url]]
    assert by_kind[10050]["tags"] == [["relay", relay.url]]
    # NIP-42: the challenge was signed with the same key, and every signature went through the
    # signer
    assert len(relay.auth_events) == 4
    assert [e["kind"] for e in signer.signed if e["kind"] != 22242] == [0, 3, 10002, 10050]


def test_starter_events_use_the_public_relay_and_never_overwrite(services, odoo, signer, relay):
    services.settings.community_relay_public_url = "wss://relay.belfry.test"
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    by_kind = {e["kind"]: e for e in relay.events}
    assert by_kind[10002]["tags"] == [["r", "wss://relay.belfry.test"]]
    assert by_kind[10050]["tags"] == [["relay", "wss://relay.belfry.test"]]

    # the participant's own follow list stays; the backfill only adds what is missing
    pubkey = by_kind[0]["pubkey"]
    relay.events = [e for e in relay.events if e["kind"] != 10050]
    relay.events = [
        {**e, "tags": [["p", "ab" * 32]]} if e["kind"] == 3 else e for e in relay.events
    ]
    assert core.publish_starter_events(services) == {"published": {"OSV-0001": [10050]}}
    follows = [e for e in relay.events if e["kind"] == 3]
    assert len(follows) == 1 and follows[0]["tags"] == [["p", "ab" * 32]]
    assert [e["kind"] for e in relay.events if e["pubkey"] == pubkey].count(10050) == 1
    assert core.publish_starter_events(services) == {"published": {"OSV-0001": []}}


def test_profile_carries_the_default_avatar_and_the_backfill_adds_it(services, odoo, relay):
    services.settings.public_url = "https://pass.belfry.test"
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    profile = next(e for e in relay.events if e["kind"] == 0)
    pubkey = profile["pubkey"]
    picture = f"https://pass.belfry.test/avatar/{pubkey}.png"
    assert json.loads(profile["content"])["picture"] == picture

    # a profile the participant wrote without a picture: the backfill adds it, keeps the rest
    own = {"name": "Al", "about": "hi", "website": "https://al.example"}
    relay.events = [e for e in relay.events if e["kind"] != 0] + [
        {**profile, "content": json.dumps(own), "created_at": 2_000_000_000, "tags": [["x", "1"]]}
    ]
    assert core.publish_starter_events(services) == {"published": {"OSV-0001": [0]}}
    latest = max((e for e in relay.events if e["kind"] == 0), key=lambda e: e["created_at"])
    assert json.loads(latest["content"]) == {**own, "picture": picture}
    assert latest["tags"] == [["x", "1"]] and latest["created_at"] == 2_000_000_001
    # with a picture of their own nothing is touched
    assert core.publish_starter_events(services) == {"published": {"OSV-0001": []}}


def test_relay_failure_does_not_block_activation(services, odoo, relay):
    relay.fail = True
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert relay.events == []


def test_no_relay_configured_skips_profile(services, odoo, signer):
    services.relay = None
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert signer.signed == []


def test_a_locked_seed_holds_the_activation_until_unlocked(services, odoo, signer):
    """No key is made that could not be recovered: the pass stays pending, the retry works."""
    signer.seed_locked = True
    ids = seed_paid_invoice(odoo, tier="day")
    with pytest.raises(SignerError) as exc:
        core.activate_pass(services, ids["invoice"])
    assert exc.value.code == "seed_locked"
    assert active_pass(odoo, ids["invoice"])["state"] == "pending"
    signer.seed_locked = False
    assert core.activate_pass(services, ids["invoice"]) == "activated"


def test_full_access_ends_at_event_end_and_late_payment_starts_on_payment_day(services, odoo):
    ids = seed_paid_invoice(odoo, tier="full", paid_on="2027-01-27 09:30:00")
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = active_pass(odoo, ids["invoice"])
    assert p["date_start"] == "2027-01-27"
    assert p["date_end"] == "2027-01-31"


# (2) the same webhook replayed three times -> no additional creates, same npub
def test_webhook_replayed_three_times_creates_nothing_new(client, odoo, signer):
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
    assert signer.creates == 1
    assert len(signer.accounts) == 1
    npub = active_pass(odoo, ids["invoice"])["npub"]
    assert odoo.get("res.partner", ids["partner"])["belfry_npub"] == npub


def test_activation_resumes_after_partial_failure(services, odoo, signer):
    """The member number is assigned, then the signer fails: the retry makes one account."""
    ids = seed_paid_invoice(odoo)
    signer.fail = True
    with pytest.raises(SignerError):
        core.activate_pass(services, ids["invoice"])
    assert active_pass(odoo, ids["invoice"])["state"] == "pending"
    assert odoo.get("res.partner", ids["partner"])["belfry_member_number"] == "OSV-0001"

    signer.fail = False
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert signer.creates == 1
    assert odoo.get("res.partner", ids["partner"])["belfry_member_number"] == "OSV-0001"
    assert active_pass(odoo, ids["invoice"])["state"] == "active"


def test_hook_rejects_bad_token_and_tolerates_errors(client, odoo):
    r = client.post("/hooks/odoo/invoice-paid?token=wrong", json={"_id": 1})
    assert r.status_code == 401
    r = client.post(f"/hooks/odoo/invoice-paid?token={HOOK_TOKEN}", json={"_id": 999})
    assert r.status_code == 200 and r.json()["result"] == "not_found"
    r = client.post(f"/hooks/odoo/invoice-paid?token={HOOK_TOKEN}", json={"_model": "account.move"})
    assert r.status_code == 200 and r.json()["result"] == "ignored"


# (3) unpaid or pending transaction -> nothing created
def test_unpaid_invoice_creates_nothing(services, odoo, signer):
    ids = seed_paid_invoice(odoo, payment_state="not_paid", tx_state="pending")
    assert core.activate_pass(services, ids["invoice"]) == "not_paid"
    ids2 = seed_paid_invoice(odoo, payment_state="paid", tx_state="pending")
    assert core.activate_pass(services, ids2["invoice"]) == "not_paid"
    ids3 = seed_paid_invoice(odoo, payment_state="paid", provider_reference=None)
    assert core.activate_pass(services, ids3["invoice"]) == "not_paid"
    ids4 = seed_paid_invoice(odoo, payment_state="in_payment", with_transaction=False)
    assert core.activate_pass(services, ids4["invoice"]) == "not_paid"
    assert odoo.tables["belfry.pass"] == {}
    assert signer.creates == 0


def test_manual_payment_accepted_only_when_enabled(services, odoo):
    services.settings.allow_manual_payments = True
    ids = seed_paid_invoice(odoo, payment_state="paid", with_transaction=False)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = active_pass(odoo, ids["invoice"])
    assert p["state"] == "active"
    assert p.get("transaction_id", False) is False
    assert p["date_start"] == "2027-01-25"  # invoice_date 2027-01-19 < event start


# (4) refund -> pass revoked, the signer account suspended
def test_refund_revokes_pass_and_suspends_the_signer(client, services, odoo, signer):
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
    assert signer.accounts[f"odoo-partner-{p['partner_id']}"]["status"] == "suspended"
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


# (5) a second active pass for the same partner keeps the signer active on refund of the first
def test_refund_keeps_the_signer_active_with_second_active_pass(services, odoo, signer):
    first = seed_paid_invoice(odoo, tier="day")
    assert core.activate_pass(services, first["invoice"]) == "activated"
    second = seed_paid_invoice(odoo, tier="full", partner_id=first["partner"])
    odoo.get("payment.transaction", second["transaction"])["provider_reference"] = "pi_456"
    assert core.activate_pass(services, second["invoice"]) == "activated"
    assert signer.creates == 1 and len(signer.accounts) == 1  # same partner, same key

    refund_id = odoo.add(
        "payment.transaction",
        state="done",
        operation="refund",
        source_transaction_id=first["transaction"],
    )
    assert core.revoke_for_refund(services, refund_id) == "revoked"
    assert active_pass(odoo, first["invoice"])["state"] == "revoked"
    assert active_pass(odoo, second["invoice"])["state"] == "active"
    vid = f"odoo-partner-{first['partner']}"
    assert signer.accounts[vid]["status"] == "active"
    assert (vid, "suspended") not in signer.status_calls


# (6) expiry job expires only passes with date_end < today
def test_expire_only_passes_past_date_end(services, odoo, signer, monkeypatch):
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
    assert signer.accounts[f"odoo-partner-{a['partner']}"]["status"] == "suspended"
    assert signer.accounts[f"odoo-partner-{b['partner']}"]["status"] == "active"
    assert all(
        p["invoice_id"] != c["invoice"] or p["state"] == "pending"
        for p in odoo.tables["belfry.pass"].values()
    )
    # running it again changes nothing
    assert core.expire(services) == {"expired": []}


def test_reconcile_activates_missed_invoices_and_revokes_refunds(
    services, odoo, signer, monkeypatch
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
    # the refunded holder's signer account is suspended
    assert signer.accounts[f"odoo-partner-{done['partner']}"]["status"] == "suspended"
    # second run is a no-op
    assert core.reconcile(services) == {"activated": {}, "revoked": {}, "tokens": {}}


def test_reconcile_endpoint_requires_token(client):
    assert client.post("/admin/reconcile").status_code == 401
    r = client.post(f"/admin/reconcile?token={HOOK_TOKEN}")
    assert r.status_code == 200 and r.json() == {
        "activated": {},
        "revoked": {},
        "tokens": {},
    }


def test_legacy_module_fallbacks(settings, signer):
    """Without the module's RPC methods the service numbers members and creates passes itself."""
    from belfry_onboarding.clients.signer import SignerClient

    odoo = FakeOdoo(legacy_module=True)
    services = core.Services(
        odoo=odoo,
        signer=SignerClient("http://signer.test", "t"),
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


def test_signer_client_sends_the_token_and_reads_errors(router):
    from belfry_onboarding.clients.signer import SignerClient

    route = router.put("http://signer.example/accounts/odoo-partner-1").mock(
        return_value=httpx.Response(503, json={"error": "seed_locked", "message": "locked"})
    )
    with pytest.raises(SignerError) as exc:
        SignerClient("http://signer.example", "tok").ensure_account("odoo-partner-1", 3)
    assert (exc.value.status, exc.value.code) == (503, "seed_locked")
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"
    assert json.loads(route.calls.last.request.content) == {"key_version": 3}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_npub_encoding_known_vector():
    # Test vector from NIP-19
    hex_pk = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
    assert hex_to_npub(hex_pk) == "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"


# -- self-custody (the account page moves the key, account.py) ----------------------------------


def test_key_changed_moves_the_pass_and_the_whitelist(services, odoo, signer, whitelist):
    ids = seed_paid_invoice(odoo, tier="week")
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = active_pass(odoo, ids["invoice"])
    old, new = p["nostr_pubkey"], "ab" * 32
    vine = f"odoo-partner-{ids['partner']}"

    assert core.key_changed(services, vine, old, old) == "took_custody"
    assert p["nostr_pubkey"] == old
    assert core.key_changed(services, vine, old, new) == "moved"
    assert p["nostr_pubkey"] == new and p["npub"] == hex_to_npub(new)
    assert odoo.get("res.partner", ids["partner"])["belfry_npub"] == hex_to_npub(new)
    rendered = whitelist.config_path.read_text()
    assert new in rendered and old not in rendered
    with pytest.raises(ValueError):
        core.key_changed(services, "vine-1", old, new)


# -- derived keys and recovery (the custody seed) -------------------------------------------------


def test_activation_passes_the_key_version(services, odoo, signer):
    ids = seed_paid_invoice(odoo, tier="day")
    odoo.get("res.partner", ids["partner"])["belfry_key_version"] = 2
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert signer.accounts[f"odoo-partner-{ids['partner']}"]["key_version"] == 2


def test_restore_keys_recreates_accounts_with_the_same_npubs(services, odoo, signer):
    signer.derived = True
    a = seed_paid_invoice(odoo, tier="week")
    assert core.activate_pass(services, a["invoice"]) == "activated"
    pa = active_pass(odoo, a["invoice"])
    odoo.get("res.partner", a["partner"])["belfry_key_custody"] = "belfry"

    # a second participant moved to their own key
    bob = odoo.add("res.partner", name="Bob Example", email="bob@example.com")
    b = seed_paid_invoice(odoo, tier="day", partner_id=bob, provider_reference="pi_b")
    assert core.activate_pass(services, b["invoice"]) == "activated"
    pb = active_pass(odoo, b["invoice"])
    own = "cd" * 32
    core.key_changed(services, f"odoo-partner-{b['partner']}", pb["nostr_pubkey"], own)
    assert odoo.get("res.partner", b["partner"])["belfry_key_custody"] == "self"

    # the signer's data is lost
    signer.accounts.clear()
    report = core.restore_keys(services)
    assert report["partners"] == {
        odoo.get("res.partner", a["partner"])["belfry_member_number"]: "ok",
        odoo.get("res.partner", b["partner"])["belfry_member_number"]: "own_key",
    }
    assert signer.accounts[f"odoo-partner-{a['partner']}"]["pubkey"] == pa["nostr_pubkey"]
    assert signer.accounts[f"odoo-partner-{b['partner']}"]["pubkey"] == own
    assert signer.accounts[f"odoo-partner-{b['partner']}"]["custody"] == "own"
    # running it again changes nothing
    assert core.restore_keys(services) == report


def test_restore_keys_reports_a_random_key_that_cannot_come_back(services, odoo, signer):
    ids = seed_paid_invoice(odoo, tier="day")
    assert core.activate_pass(services, ids["invoice"]) == "activated"  # sequential = "random"
    signer.accounts.clear()
    signer.derived = True
    report = core.restore_keys(services)
    assert list(report["partners"].values()) == ["mismatch"]
    assert signer.accounts == {}, "nothing is written for a key the seed does not give"
    signer.seed_locked = True
    assert list(core.restore_keys(services)["partners"].values()) == ["locked"]


def test_mollie_chargeback_revokes_and_reversal_gives_the_pass_back(client, services, odoo, signer):
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    tx = odoo.get("payment.transaction", ids["transaction"])

    def hook():
        r = client.post(
            f"/hooks/odoo/chargeback?token={HOOK_TOKEN}",
            json={"_model": "payment.transaction", "_id": ids["transaction"]},
        )
        assert r.status_code == 200
        return r.json()["result"]

    assert hook() == "no_chargeback"
    tx["belfry_chargeback_state"] = "open"
    assert hook() == "revoked"
    p = active_pass(odoo, ids["invoice"])
    assert (p["state"], p["revoke_reason"]) == ("revoked", "chargeback")
    vid = f"odoo-partner-{ids['partner']}"
    assert signer.accounts[vid]["status"] == "suspended"
    assert hook() == "already_revoked"
    tx["belfry_chargeback_state"] = "reversed"
    assert hook() == "reactivated"
    assert p["state"] == "active" and signer.accounts[vid]["status"] == "active"
    assert hook() == "unchanged"
    # a pass a steward revoked stays revoked when a chargeback is reversed
    core.revoke_pass(services, p["id"], "steward")
    assert hook() == "unchanged" and p["state"] == "revoked"
    assert client.post("/hooks/odoo/chargeback?token=wrong", json={"_id": 1}).status_code == 401


def test_reconcile_revokes_open_chargebacks(services, odoo, monkeypatch):
    monkeypatch.setattr(core, "today", lambda: dt.date(2027, 1, 21))
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    odoo.get("payment.transaction", ids["transaction"])["belfry_chargeback_state"] = "open"
    result = core.reconcile(services)
    assert list(result["revoked"].values()) == ["revoked"]
    assert active_pass(odoo, ids["invoice"])["revoke_reason"] == "chargeback"


# -- joining with an own key (invite page) ---------------------------------------------------------


def own_key_proof(path: str = "/belfry/invite/tok") -> tuple[str, str]:
    """A key of the participant's own and the NIP-98 event the invite page has them sign."""
    from coincurve import PrivateKey

    from belfry_onboarding.blossom import event_id

    key = PrivateKey()
    pubkey = key.public_key_xonly.format().hex()
    event = {
        "pubkey": pubkey,
        "created_at": 1800000000,
        "kind": 27235,
        "tags": [["u", f"https://shop.example{path}"], ["method", "POST"]],
        "content": "Join Open Source Village with this key",
    }
    event["id"] = event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return pubkey, json.dumps(event)


def seed_own_key_pass(odoo: FakeOdoo, ids: dict, pubkey: str, proof: str) -> int:
    return odoo.add(
        "belfry.pass",
        partner_id=ids["partner"],
        invoice_id=ids["invoice"],
        tier="week",
        state="pending",
        own_pubkey=pubkey,
        own_pubkey_proof=proof,
    )


def test_own_key_gets_a_keyless_account_and_no_profile(services, odoo, signer, relay, whitelist):
    ids = seed_paid_invoice(odoo, tier="week")
    pubkey, proof = own_key_proof()
    seed_own_key_pass(odoo, ids, pubkey, proof)

    assert core.activate_pass(services, ids["invoice"]) == "activated"

    p = active_pass(odoo, ids["invoice"])
    partner = odoo.get("res.partner", ids["partner"])
    account = signer.accounts[f"odoo-partner-{ids['partner']}"]
    assert (account["pubkey"], account["custody"]) == (pubkey, "own")
    assert p["nostr_pubkey"] == pubkey
    assert p["npub"] == partner["belfry_npub"] == hex_to_npub(pubkey)
    assert partner["belfry_key_custody"] == "self"
    assert account["status"] == "active"
    # Belfry cannot sign for the key: no profile, no relay or follow lists
    assert relay.events == []
    assert signer.signed == []
    assert pubkey in whitelist.config_path.read_text()


def test_own_key_with_a_tampered_proof_stops_the_activation(services, odoo, signer):
    ids = seed_paid_invoice(odoo, tier="week")
    pubkey, proof = own_key_proof()
    other, _ = own_key_proof()
    seed_own_key_pass(odoo, ids, other, proof)  # the proof is for another key

    with pytest.raises(RuntimeError, match="does not hold"):
        core.activate_pass(services, ids["invoice"])
    assert signer.accounts == {}
    assert active_pass(odoo, ids["invoice"])["state"] == "pending"


def test_starter_events_skip_participants_with_their_own_key(services, odoo, signer, relay):
    ids = seed_paid_invoice(odoo, tier="week")
    pubkey, proof = own_key_proof()
    seed_own_key_pass(odoo, ids, pubkey, proof)
    assert core.activate_pass(services, ids["invoice"]) == "activated"

    assert core.publish_starter_events(services) == {"published": {}}
    assert relay.events == []
