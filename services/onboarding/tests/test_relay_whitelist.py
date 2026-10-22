"""Relay whitelist: rendering of the rnostr template and the sync points in core."""

from __future__ import annotations

import datetime as dt
import re

import pytest

from belfry_onboarding import core
from belfry_onboarding.relay_whitelist import (
    RelayWhitelist,
    WhitelistTemplateError,
    normalize_pubkeys,
    render,
    write_in_place,
)

from .conftest import HOOK_TOKEN, TEMPLATE, seed_paid_invoice

A = "a" * 64
B = "b" * 64


def whitelists(text: str) -> list[list[str]]:
    """Every marked pubkey_whitelist line of a rendered config, as lists of pubkeys."""
    out = []
    for m in re.finditer(r"^\s*pubkey_whitelist\s*=\s*\[(.*)\]\s*#\s*belfry:whitelist", text, re.M):
        out.append(re.findall(r'"([0-9a-f]{64})"', m.group(1)))
    return out


def test_template_has_a_req_and_an_event_whitelist():
    text = TEMPLATE.read_text()
    assert len(whitelists(text)) == 2
    assert "[auth.req]" in text and "[auth.event]" in text
    assert re.search(r"^\[auth\]\s*\nenabled = true", text, re.M)


def test_render_fills_every_marked_list_and_nothing_else():
    template = (
        "[auth]\nenabled = true\n[auth.req]\npubkey_whitelist = []  # belfry:whitelist\n"
        '[auth.event]\npubkey_whitelist = ["old"]  # belfry:whitelist\n'
        'event_pubkey_whitelist = ["keep"]\n'
    )
    out = render(template, [A, B])
    assert whitelists(out) == [[A, B], [A, B]]
    assert 'event_pubkey_whitelist = ["keep"]' in out
    assert render(template, []).count("pubkey_whitelist = []  # belfry:whitelist") == 2
    with pytest.raises(WhitelistTemplateError):
        render("[auth]\nenabled = true\n", [A])


def test_normalize_pubkeys_dedupes_lowercases_and_drops_garbage():
    assert normalize_pubkeys([B, A.upper(), A, "", None, "npub1xyz", "zz" * 32]) == [A, B]


def test_write_in_place_keeps_the_inode_and_reports_changes(tmp_path):
    path = tmp_path / "rnostr.toml"
    path.write_text("x" * 100)
    inode = path.stat().st_ino
    assert write_in_place(path, "short") is True
    assert path.read_text() == "short"
    assert path.stat().st_ino == inode  # rnostr --watch only sees in-place modifications
    assert write_in_place(path, "short") is False


def test_sync_adds_extra_pubkeys(tmp_path):
    wl = RelayWhitelist(str(TEMPLATE), str(tmp_path / "out.toml"), extra_pubkeys=f"{B}, ")
    result = wl.sync([A])
    assert result == {"pubkeys": 2, "changed": True, "path": str(tmp_path / "out.toml")}
    assert whitelists((tmp_path / "out.toml").read_text()) == [[A, B], [A, B]]
    assert wl.sync([A])["changed"] is False


def rendered(services) -> list[str]:
    lists = whitelists(services.whitelist.config_path.read_text())
    assert len(lists) == 2 and lists[0] == lists[1]
    return lists[0]


def test_activation_whitelists_the_pubkey_before_the_profile_is_published(
    services, odoo, relay, monkeypatch
):
    ids = seed_paid_invoice(odoo)
    seen: list[list[str]] = []
    original = relay.publish

    def publish(event, sign):
        seen.append(rendered(services))  # the whitelist as it was when the profile went out
        original(event, sign)

    monkeypatch.setattr(relay, "publish", publish)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    pubkey = next(iter(odoo.tables["belfry.pass"].values()))["keycast_pubkey"]
    assert seen == [[pubkey]]
    assert rendered(services) == [pubkey]


def test_revoke_and_expiry_remove_the_pubkey_but_a_second_active_pass_keeps_it(
    services, odoo, monkeypatch
):
    monkeypatch.setattr(core, "today", lambda: dt.date(2027, 1, 28))
    first = seed_paid_invoice(odoo, tier="day")  # ends 2027-01-26
    core.activate_pass(services, first["invoice"])
    second = seed_paid_invoice(odoo, tier="full", partner_id=first["partner"])
    odoo.get("payment.transaction", second["transaction"])["provider_reference"] = "pi_2"
    core.activate_pass(services, second["invoice"])
    other = seed_paid_invoice(odoo, tier="week")
    odoo.get("payment.transaction", other["transaction"])["provider_reference"] = "pi_3"
    core.activate_pass(services, other["invoice"])
    passes = {p["invoice_id"]: p for p in odoo.tables["belfry.pass"].values()}
    same_partner_key = passes[first["invoice"]]["keycast_pubkey"]
    other_key = passes[other["invoice"]]["keycast_pubkey"]
    assert passes[second["invoice"]]["keycast_pubkey"] == same_partner_key
    assert rendered(services) == sorted([same_partner_key, other_key])

    # expiry of the day pass: the partner still holds the full pass, key stays
    assert core.expire(services)["expired"] == [passes[first["invoice"]]["id"]]
    assert rendered(services) == sorted([same_partner_key, other_key])

    # steward revocation of the other participant removes their key
    assert core.revoke_pass(services, passes[other["invoice"]]["id"], "steward") == "revoked"
    assert rendered(services) == [same_partner_key]

    # a won dispute brings it back
    assert core.reactivate_pass(services, passes[other["invoice"]]["id"]) == "reactivated"
    assert rendered(services) == sorted([same_partner_key, other_key])


def test_whitelist_failure_does_not_block_activation_or_revocation(services, odoo, caplog):
    services.whitelist.template_path = services.whitelist.template_path / "missing"
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    pass_id = next(iter(odoo.tables["belfry.pass"].values()))["id"]
    assert core.revoke_pass(services, pass_id, "steward") == "revoked"
    assert "relay whitelist not updated" in caplog.text


def test_no_whitelist_configured_is_a_no_op(services, odoo):
    services.whitelist = None
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    assert core.sync_relay_whitelist(services) is None


def test_sync_endpoint_and_startup_sync(client, services, odoo):
    # the app's lifespan synced on start: the file exists with an empty whitelist
    assert rendered(services) == []
    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    # somebody edits the rendered file by hand; the endpoint restores the derived state
    services.whitelist.config_path.write_text("broken")
    r = client.post(f"/admin/sync-relay?token={HOOK_TOKEN}")
    assert r.status_code == 200
    assert r.json()["result"] == "synced" and r.json()["pubkeys"] == 1
    assert len(rendered(services)) == 1
    assert client.post("/admin/sync-relay?token=wrong").status_code == 401
