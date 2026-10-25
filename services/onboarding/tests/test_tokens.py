"""Village tokens: the Cashu client (NUT-00 crypto, v4 tokens, minting against a fake mint that
really signs), the allowance per pass, minting at activation and the /tokens page."""

from __future__ import annotations

import datetime as dt
import hashlib

import httpx
import pytest
from coincurve import PrivateKey, PublicKey

from belfry_onboarding import core
from belfry_onboarding.clients.mint import (
    MintClient,
    MintError,
    Proof,
    cbor_decode,
    cbor_encode,
    decode_token,
    encode_token,
    hash_to_curve,
    split_amount,
)

from .conftest import seed_paid_invoice

MINT = "http://mint.test"


# -- NUT-00 and the token format -------------------------------------------------------------------


def test_hash_to_curve_test_vectors():
    # NUT-00 test vectors
    assert (
        hash_to_curve(bytes(32)).format().hex()
        == "024cce997d3b518f739663b757deaec95bcd9473c30a14ac2fd04023a739d1a725"
    )
    assert (
        hash_to_curve(bytes.fromhex("00" * 31 + "01")).format().hex()
        == "022e7158e11c9506f1aa4248bf531298daa7febd6194f003edcd9b93ade6253acf"
    )


def test_split_amount():
    assert split_amount(350) == [2, 4, 8, 16, 64, 256]
    assert sum(split_amount(50)) == 50
    assert split_amount(1) == [1]


def test_cbor_round_trip():
    doc = {
        "m": "https://mint",
        "u": "osv",
        "t": [{"i": b"\x01\x02", "p": [{"a": 300, "s": "x", "c": b"\x02" * 33}]}],
    }
    assert cbor_decode(cbor_encode(doc)) == doc
    assert cbor_encode(0x10000) == b"\x1a\x00\x01\x00\x00"


def test_token_round_trip():
    proofs = [
        Proof(2, "00ad268c4d1f5826", "secret-a", "02" + "11" * 32),
        Proof(8, "00ad268c4d1f5826", "secret-b", "03" + "22" * 32),
    ]
    token = encode_token("https://mint.belfry.test", "osv", proofs)
    assert token.startswith("cashuB") and "=" not in token
    assert decode_token(token) == ("https://mint.belfry.test", "osv", proofs)


# -- a fake mint that signs like a real one: C_ = k * B_ -------------------------------------------


class FakeMint:
    def __init__(self, router, url=MINT, unit="osv"):
        self.keyset_id = "01" + "ab" * 32
        self.privs = {1 << i: PrivateKey(hashlib.sha256(b"k%d" % i).digest()) for i in range(16)}
        self.quotes: dict[str, dict] = {}
        self.spent: set[str] = set()
        self.unit = unit
        router.get(f"{url}/v1/keysets").mock(
            return_value=httpx.Response(
                200, json={"keysets": [{"id": self.keyset_id, "unit": unit, "active": True}]}
            )
        )
        router.get(f"{url}/v1/keys/{self.keyset_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "keysets": [
                        {
                            "id": self.keyset_id,
                            "unit": unit,
                            "keys": {
                                str(a): k.public_key.format().hex() for a, k in self.privs.items()
                            },
                        }
                    ]
                },
            )
        )
        router.post(f"{url}/v1/mint/quote/belfry").mock(side_effect=self._quote)
        router.get(url__regex=rf"{url}/v1/mint/quote/belfry/.*").mock(side_effect=self._quote_state)
        router.post(f"{url}/v1/mint/belfry").mock(side_effect=self._mint)
        router.post(f"{url}/v1/checkstate").mock(side_effect=self._checkstate)

    def _quote(self, request):
        import json

        body = json.loads(request.content)
        assert body["unit"] == self.unit
        qid = f"q{len(self.quotes)}"
        self.quotes[qid] = {
            "quote": qid,
            "amount": body["amount"],
            "amount_paid": 0,
            "amount_issued": 0,
        }
        return httpx.Response(200, json=self.quotes[qid])

    def _quote_state(self, request):
        q = self.quotes[request.url.path.rsplit("/", 1)[1]]
        q["amount_paid"] = q["amount"]  # the fake backend pays at once
        return httpx.Response(200, json=q)

    def _mint(self, request):
        import json

        body = json.loads(request.content)
        sigs = []
        for out in body["outputs"]:
            b_ = PublicKey(bytes.fromhex(out["B_"]))
            c_ = b_.multiply(self.privs[out["amount"]].secret)
            sigs.append({"amount": out["amount"], "id": self.keyset_id, "C_": c_.format().hex()})
        return httpx.Response(200, json={"signatures": sigs})

    def _checkstate(self, request):
        import json

        ys = json.loads(request.content)["Ys"]
        return httpx.Response(
            200,
            json={
                "states": [{"Y": y, "state": "SPENT" if y in self.spent else "UNSPENT"} for y in ys]
            },
        )

    def verify(self, proof: Proof) -> bool:
        """What the real mint checks on redemption: C == k * hash_to_curve(secret)."""
        expected = hash_to_curve(proof.secret.encode()).multiply(self.privs[proof.amount].secret)
        return expected.format().hex() == proof.c


@pytest.fixture
def mint(router) -> FakeMint:
    return FakeMint(router)


@pytest.fixture
def mint_services(services, mint):
    services.mint = MintClient(MINT, "https://mint.belfry.test", "osv")
    return services


def test_minted_token_carries_valid_signatures(mint):
    client = MintClient(MINT, "https://mint.belfry.test", "osv")
    token = client.mint_token(350)
    url, unit, proofs = decode_token(token)
    assert (url, unit) == ("https://mint.belfry.test", "osv")
    assert sorted(p.amount for p in proofs) == [2, 4, 8, 16, 64, 256]
    assert all(mint.verify(p) for p in proofs)
    assert len({p.secret for p in proofs}) == len(proofs)
    assert client.token_state(token) == "unspent"
    mint.spent.add(proofs[0].y)
    assert client.token_state(token) == "partial"
    mint.spent.update(p.y for p in proofs)
    assert client.token_state(token) == "spent"


def test_mint_errors_are_reported(router):
    router.post(f"{MINT}/v1/mint/quote/belfry").mock(
        return_value=httpx.Response(400, json={"detail": "no"})
    )
    with pytest.raises(MintError):
        MintClient(MINT, MINT).mint_token(5)


# -- allowance and activation ----------------------------------------------------------------------


def test_token_allowance(services):
    d = dt.date
    assert core.token_allowance(services, "day", 1, d(2027, 1, 25), d(2027, 1, 26)) == 50
    assert core.token_allowance(services, "week", 7, d(2027, 1, 25), d(2027, 2, 1)) == 350
    # full access: every event day from the start, both ends included (Jan 25..31 = 7)
    assert core.token_allowance(services, "full", None, d(2027, 1, 25), d(2027, 1, 31)) == 350
    assert core.token_allowance(services, "full", None, d(2027, 1, 28), d(2027, 1, 31)) == 200


def test_activation_mints_the_tokens(mint_services, odoo, mint):
    ids = seed_paid_invoice(odoo, tier="week")
    assert core.activate_pass(mint_services, ids["invoice"]) == "activated"
    p = next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == ids["invoice"])
    assert p["cashu_amount"] == 350
    _, _, proofs = decode_token(p["cashu_token"])
    assert sum(x.amount for x in proofs) == 350 and all(mint.verify(x) for x in proofs)


def test_mint_down_does_not_block_activation_and_reconcile_mints_later(
    mint_services, odoo, router, mint
):
    router.post(f"{MINT}/v1/mint/quote/belfry").mock(return_value=httpx.Response(503))
    ids = seed_paid_invoice(odoo, tier="day")
    assert core.activate_pass(mint_services, ids["invoice"]) == "activated"
    p = next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == ids["invoice"])
    assert not p.get("cashu_amount")

    router.post(f"{MINT}/v1/mint/quote/belfry").mock(side_effect=mint._quote)
    assert core.issue_missing_tokens(mint_services) == {p["id"]: 50}
    assert p["cashu_amount"] == 50
    assert core.issue_missing_tokens(mint_services) == {}


def test_no_mint_configured_no_tokens(services, odoo):
    ids = seed_paid_invoice(odoo)
    assert core.activate_pass(services, ids["invoice"]) == "activated"
    p = next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == ids["invoice"])
    assert not p.get("cashu_token")
    assert core.issue_missing_tokens(services) == {}


# -- the /tokens page ------------------------------------------------------------------------------


def test_tokens_page(mint_services, odoo, mint, client):
    mint_services.settings.wallet_public_url = "https://wallet.belfry.test"
    ids = seed_paid_invoice(odoo, tier="day")
    core.activate_pass(mint_services, ids["invoice"])
    p = next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == ids["invoice"])

    r = client.get(f"/tokens/{p['claim_token']}")
    assert r.status_code == 200
    assert "Your 50 village tokens" in r.text
    assert f'href="https://wallet.belfry.test/#token={p["cashu_token"]}"' in r.text
    assert r.headers["referrer-policy"] == "no-referrer"

    claim = client.get(f"/claim/{p['claim_token']}")
    assert f"/tokens/{p['claim_token']}" in claim.text or claim.status_code != 200

    mint.spent.update(x.y for x in decode_token(p["cashu_token"])[2])
    r = client.get(f"/tokens/{p['claim_token']}")
    assert "are in a wallet" in r.text and p["cashu_token"] not in r.text

    assert client.get("/tokens/not-a-real-claim-token").status_code == 404


# -- the wallet fetches the pass's tokens (/wallet/pass-tokens) -----------------------------------


def nip98(key: PrivateKey, url: str, method: str = "GET", **over) -> str:
    import base64
    import json
    import time

    from belfry_onboarding.blossom import event_id

    event = {
        "pubkey": key.public_key_xonly.format().hex(),
        "created_at": over.pop("created_at", int(time.time())),
        "kind": over.pop("kind", 27235),
        "tags": [["u", url], ["method", method]],
        "content": "",
    }
    event["id"] = event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    event.update(over)
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


def test_wallet_gets_the_pass_tokens_once(mint_services, odoo, mint, client):
    mint_services.settings.public_url = "https://pass.belfry.test"
    mint_services.settings.wallet_public_url = "https://wallet.belfry.test"
    ids = seed_paid_invoice(odoo, tier="day")
    core.activate_pass(mint_services, ids["invoice"])
    p = next(p for p in odoo.tables["belfry.pass"].values() if p["invoice_id"] == ids["invoice"])
    key = PrivateKey()
    p["keycast_pubkey"] = key.public_key_xonly.format().hex()
    url = "https://pass.belfry.test/wallet/pass-tokens"

    r = client.get("/wallet/pass-tokens", headers={"Authorization": nip98(key, url)})
    assert r.status_code == 200
    assert r.json() == {"tokens": [{"amount": 50, "token": p["cashu_token"]}]}
    assert r.headers["access-control-allow-origin"] == "https://wallet.belfry.test"
    pre = client.options("/wallet/pass-tokens")
    assert pre.status_code == 204 and "Authorization" in pre.headers["access-control-allow-headers"]

    # someone else's key gets nothing
    other = client.get("/wallet/pass-tokens", headers={"Authorization": nip98(PrivateKey(), url)})
    assert other.json() == {"tokens": []}

    # once redeemed, nothing comes back
    mint.spent.update(x.y for x in decode_token(p["cashu_token"])[2])
    r = client.get("/wallet/pass-tokens", headers={"Authorization": nip98(key, url)})
    assert r.json() == {"tokens": []}


def test_wallet_pass_tokens_needs_a_fresh_signed_request(mint_services, odoo, mint, client):
    mint_services.settings.public_url = "https://pass.belfry.test"
    key = PrivateKey()
    url = "https://pass.belfry.test/wallet/pass-tokens"
    assert client.get("/wallet/pass-tokens").status_code == 401
    for auth in (
        nip98(key, "https://evil.test/wallet/pass-tokens"),  # another URL
        nip98(key, url, method="POST"),  # another method
        nip98(key, url, created_at=1_000_000_000),  # replayed
        nip98(key, url, kind=1),  # not NIP-98
        nip98(key, url, sig="00" * 64),  # forged
    ):
        assert client.get("/wallet/pass-tokens", headers={"Authorization": auth}).status_code == 401
