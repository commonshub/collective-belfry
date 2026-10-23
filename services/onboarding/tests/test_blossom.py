"""Blossom gate: BUD-11 event checks and the active-pass requirement."""

from __future__ import annotations

import base64
import json

import pytest
from coincurve import PrivateKey

from belfry_onboarding import blossom, core

from .conftest import seed_paid_invoice

NOW = 1_800_000_000


def signed_event(key: PrivateKey, verb: str = "upload", server: str | None = None, **over):
    pubkey = key.public_key_xonly.format().hex()
    tags = [["t", verb], ["expiration", str(over.pop("expiration", NOW + 300))]]
    if server:
        tags.append(["server", server])
    event = {
        "pubkey": pubkey,
        "created_at": over.pop("created_at", NOW),
        "kind": over.pop("kind", 24242),
        "tags": tags,
        "content": "Authorize upload",
    }
    event["id"] = blossom.event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    event.update(over)
    return event


def header(event) -> str:
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


def test_valid_upload_event_passes():
    key = PrivateKey()
    ev = signed_event(key, server="localhost:3100")
    assert blossom.check_event(ev, "PUT", ["localhost:3100"], now=NOW) == ev["pubkey"]
    assert blossom.check_event(ev, "HEAD", [], now=NOW) == ev["pubkey"]


@pytest.mark.parametrize(
    "mutate,status,reason",
    [
        (lambda k: signed_event(k, kind=1), 401, "kind 24242"),
        (lambda k: {**signed_event(k), "sig": "00" * 64}, 401, "bad signature"),
        (lambda k: {**signed_event(k), "content": "tampered"}, 401, "id does not match"),
        (lambda k: signed_event(k, expiration=NOW - 1), 401, "expired"),
        (lambda k: signed_event(k, expiration=NOW + 2 * 24 * 3600), 401, "too long"),
        (lambda k: signed_event(k, created_at=NOW + 3600), 401, "future"),
        (lambda k: signed_event(k, verb="delete"), 403, "does not allow PUT"),
        (lambda k: signed_event(k, server="evil.example"), 403, "another server"),
    ],
)
def test_bad_events_are_refused(mutate, status, reason):
    ev = mutate(PrivateKey())
    with pytest.raises(blossom.BlossomAuthError) as exc:
        blossom.check_event(ev, "PUT", ["localhost:3100"], now=NOW)
    assert exc.value.status == status and reason in exc.value.reason


def test_delete_needs_the_delete_verb():
    key = PrivateKey()
    assert blossom.check_event(signed_event(key, verb="delete"), "DELETE", [], now=NOW)
    with pytest.raises(blossom.BlossomAuthError):
        blossom.check_event(signed_event(key, verb="upload"), "DELETE", [], now=NOW)


def test_gate_endpoint_requires_an_active_pass(client, services, odoo, keycast, monkeypatch):
    monkeypatch.setattr(blossom.time, "time", lambda: NOW)
    key = PrivateKey()
    pubkey = key.public_key_xonly.format().hex()
    put = {"X-Forwarded-Method": "PUT"}  # what Caddy's forward_auth sends
    r = client.get("/blossom/auth", headers={"Authorization": header(signed_event(key)), **put})
    assert r.status_code == 403 and "no active pass" in r.json()["error"]

    ids = seed_paid_invoice(odoo)
    core.activate_pass(services, ids["invoice"])
    p = next(
        p for p in odoo.tables["belfry.pass"].values() if p.get("invoice_id") == ids["invoice"]
    )
    p["keycast_pubkey"] = pubkey  # the fake Keycast key becomes this test key
    keycast.status[pubkey] = "active"  # so the later revoke can suspend it
    r = client.get(
        "/blossom/auth",
        headers={"Authorization": header(signed_event(key)), "X-Forwarded-Method": "PUT"},
    )
    assert r.status_code == 200 and r.headers["x-belfry-pubkey"] == pubkey
    # the forwarded method decides the verb
    r = client.get(
        "/blossom/auth",
        headers={"Authorization": header(signed_event(key)), "X-Forwarded-Method": "DELETE"},
    )
    assert r.status_code == 403
    # revoked: refused
    core.revoke_pass(services, p["id"], "steward")
    r = client.get("/blossom/auth", headers={"Authorization": header(signed_event(key)), **put})
    assert r.status_code == 403 and "no active pass" in r.json()["error"]
    # without the forwarded method the plain GET is refused by the verb check
    r = client.get("/blossom/auth", headers={"Authorization": header(signed_event(key))})
    assert r.status_code == 403
    assert client.get("/blossom/auth").status_code == 401
    assert client.get("/blossom/auth", headers={"Authorization": "Nostr !!!"}).status_code == 401
