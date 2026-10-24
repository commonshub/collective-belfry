"""Cashu client for Belfry's token mint (cdk-mintd, fake payment backend, unit ``osv``).

The onboarding service is the only party that mints (the proxy keeps ``/v1/mint/*`` internal):
at activation it mints the pass holder's tokens (50 per pass day) and keeps the resulting
``cashuB`` token on the pass; the participant redeems it into a wallet (wallet.<domain>,
cashu.me) from the link on the claim page.

Implements what that needs and nothing more:

- NUT-00 blind Diffie-Hellman key exchange (``hash_to_curve``, blinding, unblinding) and the
  v4 token serialisation (``cashuB`` + base64url of CBOR, with a minimal CBOR codec),
- NUT-01/02 active keyset and keys for the unit, NUT-04 mint quote and mint with the mint's
  custom method ``belfry`` (the fake backend marks quotes paid at once; its ``bolt11`` cannot
  price a custom unit), NUT-07 ``checkstate`` (has the token been redeemed?).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Any

import httpx
from coincurve import PrivateKey, PublicKey

DOMAIN_SEPARATOR = b"Secp256k1_HashToCurve_Cashu_"
CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


class MintError(RuntimeError):
    pass


# -- NUT-00 ----------------------------------------------------------------------------------------


def hash_to_curve(message: bytes) -> PublicKey:
    msg_hash = hashlib.sha256(DOMAIN_SEPARATOR + message).digest()
    for counter in range(2**16):
        candidate = hashlib.sha256(msg_hash + counter.to_bytes(4, "little")).digest()
        try:
            return PublicKey(b"\x02" + candidate)
        except ValueError:
            continue
    raise MintError("hash_to_curve found no point")


def blind(secret: str, r: bytes | None = None) -> tuple[PublicKey, bytes]:
    """B_ = Y + rG for the secret's Y; returns (B_, r)."""
    r = r or PrivateKey().secret
    y = hash_to_curve(secret.encode())
    return PublicKey.combine_keys([y, PrivateKey(r).public_key]), r


def unblind(c_: PublicKey, r: bytes, mint_key: PublicKey) -> PublicKey:
    """C = C_ - rK."""
    neg_r = (CURVE_ORDER - int.from_bytes(r, "big")) % CURVE_ORDER
    return PublicKey.combine_keys([c_, mint_key.multiply(neg_r.to_bytes(32, "big"))])


def split_amount(amount: int) -> list[int]:
    """Powers of two that add up to ``amount`` (the keyset's denominations), smallest first."""
    return [1 << i for i in range(amount.bit_length()) if amount >> i & 1]


# -- minimal CBOR (RFC 8949) for v4 tokens: maps, arrays, text, bytes, unsigned ints ------------


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    for info, fmt in ((24, ">B"), (25, ">H"), (26, ">I"), (27, ">Q")):
        if n < 1 << (8 * struct.calcsize(fmt)):
            return bytes([major << 5 | info]) + struct.pack(fmt, n)
    raise ValueError("integer too large")


def cbor_encode(value: Any) -> bytes:
    if isinstance(value, bool) or value is None:
        return bytes([0xF5 if value is True else 0xF4 if value is False else 0xF6])
    if isinstance(value, int):
        if value < 0:
            raise ValueError("negative integers are not used in tokens")
        return _head(0, value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return _head(3, len(raw)) + raw
    if isinstance(value, list):
        return _head(4, len(value)) + b"".join(cbor_encode(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(
            cbor_encode(k) + cbor_encode(v) for k, v in value.items()
        )
    raise TypeError(f"cannot encode {type(value).__name__}")


def cbor_decode(data: bytes) -> Any:
    value, end = _decode(data, 0)
    if end != len(data):
        raise ValueError("trailing bytes")
    return value


def _decode(data: bytes, i: int) -> tuple[Any, int]:
    first = data[i]
    major, info = first >> 5, first & 0x1F
    i += 1
    if major == 7:
        return {20: False, 21: True, 22: None}[info], i
    if info < 24:
        n = info
    else:
        size = {24: 1, 25: 2, 26: 4, 27: 8}[info]
        n = int.from_bytes(data[i : i + size], "big")
        i += size
    if major == 0:
        return n, i
    if major == 2:
        return data[i : i + n], i + n
    if major == 3:
        return data[i : i + n].decode(), i + n
    if major == 4:
        items = []
        for _ in range(n):
            item, i = _decode(data, i)
            items.append(item)
        return items, i
    if major == 5:
        out = {}
        for _ in range(n):
            key, i = _decode(data, i)
            out[key], i = _decode(data, i)
        return out, i
    raise ValueError(f"unsupported CBOR major type {major}")


# -- tokens ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proof:
    amount: int
    keyset_id: str
    secret: str
    c: str  # compressed point, hex

    @property
    def y(self) -> str:
        return hash_to_curve(self.secret.encode()).format().hex()


def encode_token(mint_url: str, unit: str, proofs: list[Proof]) -> str:
    """v4 token: ``cashuB`` + base64url(CBOR), proofs grouped by keyset."""
    by_keyset: dict[str, list[Proof]] = {}
    for p in proofs:
        by_keyset.setdefault(p.keyset_id, []).append(p)
    doc = {
        "m": mint_url,
        "u": unit,
        "t": [
            {
                "i": bytes.fromhex(kid),
                "p": [{"a": p.amount, "s": p.secret, "c": bytes.fromhex(p.c)} for p in ps],
            }
            for kid, ps in by_keyset.items()
        ],
    }
    return "cashuB" + base64.urlsafe_b64encode(cbor_encode(doc)).decode().rstrip("=")


def decode_token(token: str) -> tuple[str, str, list[Proof]]:
    if not token.startswith("cashuB"):
        raise ValueError("not a v4 token")
    raw = token[6:]
    doc = cbor_decode(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    proofs = [
        Proof(amount=p["a"], keyset_id=t["i"].hex(), secret=p["s"], c=p["c"].hex())
        for t in doc["t"]
        for p in t["p"]
    ]
    return doc["m"], doc["u"], proofs


# -- the mint --------------------------------------------------------------------------------------


class MintClient:
    """``url``: the mint as this service reaches it; ``public_url``: the mint URL written into
    tokens (the one wallets use)."""

    def __init__(
        self,
        url: str,
        public_url: str,
        unit: str = "osv",
        method: str = "belfry",
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ):
        self.url = url.rstrip("/")
        self.public_url = public_url.rstrip("/")
        self.unit = unit
        self.method = method
        self._client = client or httpx.Client(timeout=timeout)

    def _get(self, path: str) -> Any:
        r = self._client.get(self.url + path)
        if r.status_code != 200:
            raise MintError(f"GET {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = self._client.post(self.url + path, json=body)
        if r.status_code != 200:
            raise MintError(f"POST {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def active_keys(self) -> tuple[str, dict[int, PublicKey]]:
        keysets = [
            k
            for k in self._get("/v1/keysets")["keysets"]
            if k.get("unit") == self.unit and k.get("active")
        ]
        if not keysets:
            raise MintError(f"the mint has no active keyset for unit {self.unit!r}")
        kid = keysets[0]["id"]
        keyset = self._get(f"/v1/keys/{kid}")["keysets"][0]
        return kid, {int(a): PublicKey(bytes.fromhex(k)) for a, k in keyset["keys"].items()}

    def mint_token(self, amount: int, wait_s: float = 10.0) -> str:
        """Mint ``amount`` tokens and return them as one ``cashuB`` token."""
        if amount <= 0:
            raise ValueError("amount must be positive")
        quote = self._post(f"/v1/mint/quote/{self.method}", {"amount": amount, "unit": self.unit})
        quote_id = quote["quote"]
        deadline = time.monotonic() + wait_s
        # Custom-method quotes report amount_paid/amount_issued instead of a state.
        while (
            quote.get("state") != "PAID"
            and quote.get("amount_paid", 0) - quote.get("amount_issued", 0) < amount
        ):
            if time.monotonic() > deadline:
                raise MintError(f"mint quote {quote_id} not paid: {quote}")
            time.sleep(0.2)
            quote = self._get(f"/v1/mint/quote/{self.method}/{quote_id}")
        kid, keys = self.active_keys()
        pending = []
        for part in split_amount(amount):
            secret = secrets.token_hex(32)
            b_, r = blind(secret)
            pending.append((part, secret, b_, r))
        outputs = [{"amount": a, "id": kid, "B_": b_.format().hex()} for a, _, b_, _ in pending]
        sigs = self._post(f"/v1/mint/{self.method}", {"quote": quote_id, "outputs": outputs})[
            "signatures"
        ]
        if len(sigs) != len(pending):
            raise MintError("the mint returned a different number of signatures")
        proofs = []
        for (part, secret, _, r), sig in zip(pending, sigs, strict=True):
            c = unblind(PublicKey(bytes.fromhex(sig["C_"])), r, keys[part])
            proofs.append(
                Proof(amount=part, keyset_id=sig["id"], secret=secret, c=c.format().hex())
            )
        return encode_token(self.public_url, self.unit, proofs)

    def swap_token(self, token: str) -> str:
        """Redeem a token into fresh proofs (what a wallet does on receive); returns the new token.
        Used by checks; the service itself never takes participants' tokens."""
        _, _, proofs = decode_token(token)
        kid, keys = self.active_keys()
        pending = []
        for p in proofs:
            secret = secrets.token_hex(32)
            b_, r = blind(secret)
            pending.append((p.amount, secret, b_, r))
        body = {
            "inputs": [
                {"amount": p.amount, "id": p.keyset_id, "secret": p.secret, "C": p.c}
                for p in proofs
            ],
            "outputs": [
                {"amount": a, "id": kid, "B_": b_.format().hex()} for a, _, b_, _ in pending
            ],
        }
        sigs = self._post("/v1/swap", body)["signatures"]
        fresh = [
            Proof(
                a,
                sig["id"],
                secret,
                unblind(PublicKey(bytes.fromhex(sig["C_"])), r, keys[a]).format().hex(),
            )
            for (a, secret, _, r), sig in zip(pending, sigs, strict=True)
        ]
        return encode_token(self.public_url, self.unit, fresh)

    def token_state(self, token: str) -> str:
        """``unspent`` (still in the token), ``spent`` (redeemed into a wallet) or ``partial``."""
        _, _, proofs = decode_token(token)
        states = {
            s["state"]
            for s in self._post("/v1/checkstate", {"Ys": [p.y for p in proofs]})["states"]
        }
        if states == {"UNSPENT"}:
            return "unspent"
        if states == {"SPENT"}:
            return "spent"
        return "partial"
