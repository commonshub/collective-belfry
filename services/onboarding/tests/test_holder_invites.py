"""Invites by e-mail for a pass holder or their agent: /api/invites, signed with NIP-98."""

from __future__ import annotations

import base64
import hashlib
import json
import time

from coincurve import PrivateKey

from belfry_onboarding import core, steward
from belfry_onboarding.blossom import event_id

from .conftest import seed_paid_invoice

BASE = "https://pass.belfry.test"
URL = BASE + "/api/invites"


def nip98(key: PrivateKey, url: str, method: str = "GET", body: bytes = b"", **over) -> str:
    tags = [["u", url], ["method", method]]
    if body:
        tags.append(["payload", over.pop("payload", hashlib.sha256(body).hexdigest())])
    event = {
        "pubkey": key.public_key_xonly.format().hex(),
        "created_at": over.pop("created_at", int(time.time())),
        "kind": 27235,
        "tags": tags,
        "content": "",
    }
    event["id"] = event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


def holder(services, odoo, authentik=None, steward_group=False) -> tuple[PrivateKey, int]:
    """An active pass with a Keycast key; with ``steward_group`` its holder is a steward."""
    services.settings.public_url = BASE
    ids = seed_paid_invoice(odoo, tier="week")
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == ids["invoice"])
    key = PrivateKey()
    p["keycast_pubkey"] = key.public_key_xonly.format().hex()
    if steward_group:
        user_pk = odoo.tables["res.partner"][ids["partner"]]["belfry_authentik_user_id"]
        authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": {user_pk}}
    return key, ids["partner"]


def post(client, key, path, data, **over):
    body = json.dumps(data).encode()
    auth = nip98(key, BASE + path, "POST", body, **over)
    return client.post(
        path, content=body, headers={"Authorization": auth, "Content-Type": "application/json"}
    )


def get(client, key, path):
    return client.get(path, headers={"Authorization": nip98(key, BASE + path)})


def test_holder_invites_by_mail_and_sees_the_npub(services, odoo, client):
    key, partner = holder(services, odoo)
    r = post(client, key, "/api/invites", {"email": "carol@example.org", "name": "Carol"})
    assert r.status_code == 201, r.text
    invite = r.json()
    assert invite["state"] == "open" and invite["email"] == "carol@example.org"
    assert invite["npub"] is False
    assert odoo.mailed_invites == [invite["id"]]
    stored = odoo.tables["belfry.invite"][invite["id"]]
    assert stored["inviter_partner_id"] == partner  # counts against the limit

    # asking again for the same address returns the same invite, nothing is mailed twice
    again = post(client, key, "/api/invites", {"email": "Carol@example.org"})
    assert again.status_code == 201 and again.json()["id"] == invite["id"]
    assert odoo.mailed_invites == [invite["id"]]

    # Carol pays: the invite shows her pass and npub
    carol = odoo.add(
        "res.partner", name="Carol", email="carol@example.org", belfry_npub="npub1carol"
    )
    pass_id = odoo.add("belfry.pass", partner_id=carol, tier="day", state="active")
    stored.update(state="used", pass_id=pass_id, used_by_partner_id=carol)
    one = get(client, key, f"/api/invites/{invite['id']}")
    assert one.status_code == 200
    assert one.json()["npub"] == "npub1carol" and one.json()["pass_state"] == "active"
    listing = get(client, key, "/api/invites").json()
    assert listing["steward"] is False and [i["id"] for i in listing["invites"]] == [invite["id"]]

    # someone with an active pass is not invited again: their npub comes back
    member = post(client, key, "/api/invites", {"email": "carol@example.org"})
    assert member.status_code == 200 and member.json() == {
        "state": "member",
        "email": "carol@example.org",
        "npub": "npub1carol",
    }


def test_participant_limit_and_revoke(services, odoo, client):
    key, _partner = holder(services, odoo)
    made = [post(client, key, "/api/invites", {"email": f"f{i}@example.org"}) for i in range(3)]
    assert [r.status_code for r in made] == [201, 201, 201]
    over = post(client, key, "/api/invites", {"email": "f4@example.org"})
    assert over.status_code == 409 and over.json()["error"] == "refused"
    assert "no invites left" in over.json()["message"]
    revoked = post(client, key, f"/api/invites/{made[0].json()['id']}/revoke", {})
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert get(client, key, "/api/invites/999").status_code == 404


def test_holder_invite_as_a_link(services, odoo, client):
    key, partner = holder(services, odoo)
    r = post(client, key, "/api/invites", {"note": "for my neighbour"})
    assert r.status_code == 201, r.text
    invite = r.json()
    assert (
        invite["state"] == "open" and invite["email"] == "" and "/belfry/invite/" in invite["url"]
    )
    assert odoo.mailed_invites == []  # nothing mailed: the holder passes the link on
    assert odoo.tables["belfry.invite"][invite["id"]]["inviter_partner_id"] == partner
    second = post(client, key, "/api/invites", {"email": ""})
    assert second.status_code == 201 and second.json()["id"] != invite["id"]
    listed = get(client, key, "/api/invites").json()
    assert {invite["id"], second.json()["id"]} <= {i["id"] for i in listed["invites"]}


def test_steward_invites_without_limit(services, odoo, authentik, client):
    key, partner = holder(services, odoo, authentik, steward_group=True)
    rows = [post(client, key, "/api/invites", {"email": f"s{i}@example.org"}) for i in range(5)]
    assert [r.status_code for r in rows] == [201] * 5
    stored = odoo.tables["belfry.invite"][rows[0].json()["id"]]
    assert stored["steward_email"] and not stored.get("inviter_partner_id")
    assert get(client, key, "/api/invites").json()["steward"] is True


def test_requests_must_be_signed_by_a_pass_holder(services, odoo, client):
    key, _partner = holder(services, odoo)
    body = json.dumps({"email": "x@example.org"}).encode()

    def send(auth):
        return client.post("/api/invites", content=body, headers={"Authorization": auth})

    assert send("").status_code == 401
    assert send(nip98(key, "https://evil.test/api/invites", "POST", body)).status_code == 401
    assert send(nip98(key, URL, "GET", body)).status_code == 401
    assert send(nip98(key, URL, "POST", body, created_at=1_000_000_000)).status_code == 401
    assert send(nip98(key, URL, "POST", body, payload="00" * 32)).status_code == 401  # other body
    stranger = send(nip98(PrivateKey(), URL, "POST", body))
    assert stranger.status_code == 403 and stranger.json()["error"] == "no_pass"
    ok = nip98(key, URL, "POST", body)
    assert send(ok).status_code == 201
    replay = send(ok)
    assert replay.status_code == 401 and "used already" in replay.json()["message"]
    bad = post(client, key, "/api/invites", {"email": "no-address"})
    assert bad.status_code == 422


def test_add_steward_gives_a_free_pass_once(services, odoo, authentik):
    authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": set()}
    first = steward.add_steward(services, "sam@belfry.test", "Steward Sam")
    assert first["pass"]["result"] == "activated"
    p = odoo.tables["belfry.pass"][first["pass"]["pass_id"]]
    assert p["state"] == "active" and p["steward_pass"] is True and p["tier"] == "full"
    again = steward.add_steward(services, "sam@belfry.test")
    assert again["pass"] == {"pass_id": p["id"], "result": "already_active", "state": "active"}
    backfill = steward.steward_passes(services)
    assert backfill == {"stewards": {"sam@belfry.test": again["pass"]}}
