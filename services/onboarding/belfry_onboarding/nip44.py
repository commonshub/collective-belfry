"""NIP-44 v2 encryption and NIP-59 gift wraps (NIP-17 private messages).

Access requests reach Belfry as gift-wrapped events addressed to its service key, and Belfry
writes to admins the same way (access.py). Checked against nostr-tools' implementation
(tests/test_nip44.py carries vectors from it).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from coincurve import PrivateKey, PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

GIFT_WRAP_KIND = 1059
SEAL_KIND = 13
TWO_DAYS = 2 * 24 * 3600


class Nip44Error(ValueError):
    pass


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def conversation_key(secret: bytes, pubkey_hex: str) -> bytes:
    """HKDF-extract of the unhashed ECDH x coordinate, salt ``nip44-v2``."""
    shared = PublicKey(b"\x02" + bytes.fromhex(pubkey_hex)).multiply(secret).format()[1:]
    return _hkdf_extract(b"nip44-v2", shared)


def _padded_len(n: int) -> int:
    if n <= 32:
        return 32
    next_power = 1 << ((n - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((n - 1) // chunk + 1)


def _chacha(key: bytes, nonce: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.ChaCha20(key, b"\x00\x00\x00\x00" + nonce), mode=None)
    return cipher.encryptor().update(data)


def _keys(conv: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    k = _hkdf_expand(conv, nonce, 76)
    return k[:32], k[32:44], k[44:]


def encrypt(plaintext: str, conv: bytes, nonce: bytes | None = None) -> str:
    raw = plaintext.encode("utf-8")
    if not 1 <= len(raw) <= 65535:
        raise Nip44Error("plaintext must be 1 to 65535 bytes")
    nonce = nonce or os.urandom(32)
    key, chacha_nonce, hmac_key = _keys(conv, nonce)
    padded = len(raw).to_bytes(2, "big") + raw + b"\x00" * (_padded_len(len(raw)) - len(raw))
    ciphertext = _chacha(key, chacha_nonce, padded)
    mac = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ciphertext + mac).decode()


def decrypt(payload: str, conv: bytes) -> str:
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise Nip44Error("payload is not base64") from exc
    if len(data) < 99 or data[0] != 2:
        raise Nip44Error("unknown version or payload too short")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    key, chacha_nonce, hmac_key = _keys(conv, nonce)
    if not hmac.compare_digest(
        mac, hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    ):
        raise Nip44Error("invalid MAC")
    padded = _chacha(key, chacha_nonce, ciphertext)
    n = int.from_bytes(padded[:2], "big")
    if n < 1 or len(padded) != 2 + _padded_len(n):
        raise Nip44Error("invalid padding")
    return padded[2 : 2 + n].decode("utf-8")


# -- NIP-59 ------------------------------------------------------------------------------------


def _event_id(event: dict[str, Any]) -> str:
    serial = [
        0,
        event["pubkey"],
        event["created_at"],
        event["kind"],
        event["tags"],
        event["content"],
    ]
    raw = json.dumps(serial, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sign_event(key: PrivateKey, template: dict[str, Any]) -> dict[str, Any]:
    event = {**template, "pubkey": key.public_key_xonly.format().hex()}
    event["id"] = _event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def verify(event: dict[str, Any]) -> bool:
    """A well-formed event whose id and Schnorr signature check out."""
    try:
        if _event_id(event) != event["id"]:
            return False
        from coincurve import PublicKeyXOnly

        return PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
            bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _past(now: int) -> int:
    return now - secrets.randbelow(TWO_DAYS)


def wrap(
    key: PrivateKey, rumor: dict[str, Any], recipient: str, now: int | None = None
) -> dict[str, Any]:
    """Seal ``rumor`` (unsigned: kind, content, tags, created_at) from ``key`` to ``recipient``
    and wrap it with a one-time key. Returns the kind-1059 event."""
    now = int(now if now is not None else time.time())
    rumor = {**rumor, "pubkey": key.public_key_xonly.format().hex()}
    rumor["id"] = _event_id(rumor)
    seal = sign_event(
        key,
        {
            "kind": SEAL_KIND,
            "created_at": _past(now),
            "tags": [],
            "content": encrypt(json.dumps(rumor), conversation_key(key.secret, recipient)),
        },
    )
    one_time = PrivateKey()
    return sign_event(
        one_time,
        {
            "kind": GIFT_WRAP_KIND,
            "created_at": _past(now),
            "tags": [["p", recipient]],
            "content": encrypt(json.dumps(seal), conversation_key(one_time.secret, recipient)),
        },
    )


def unwrap(key: PrivateKey, wrapped: dict[str, Any]) -> dict[str, Any]:
    """The rumor inside a gift wrap addressed to ``key``. Its ``pubkey`` is the seal's signer,
    checked: a rumor claiming another author is refused."""
    if wrapped.get("kind") != GIFT_WRAP_KIND or not verify(wrapped):
        raise Nip44Error("not a valid gift wrap")
    seal = json.loads(decrypt(wrapped["content"], conversation_key(key.secret, wrapped["pubkey"])))
    if seal.get("kind") != SEAL_KIND or not verify(seal):
        raise Nip44Error("not a valid seal")
    rumor = json.loads(decrypt(seal["content"], conversation_key(key.secret, seal["pubkey"])))
    if rumor.get("pubkey") != seal["pubkey"]:
        raise Nip44Error("the rumor's author is not the seal's signer")
    if not isinstance(rumor.get("tags"), list) or not isinstance(rumor.get("content"), str):
        raise Nip44Error("malformed rumor")
    rumor["id"] = _event_id(rumor)
    return rumor
