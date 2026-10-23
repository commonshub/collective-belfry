"""NIP-46 client check: connect to a ``bunker://`` URL the way a Nostr app does.

Keycast hands out ``bunker://<bunker pubkey>?relay=<url>&secret=<secret>`` (OAuth token response,
"create bunker", nostr-login). This module is the smoke test's remote-signing client: it opens the
relay named in the URL, sends ``connect`` with the secret, asks ``get_public_key``, has a kind-1
event signed and checks that a kind outside the policy is refused. Requests and responses are
NIP-44 v2 encrypted (the signer mirrors the request's encryption); the client key is throwaway.

    python -m belfry_onboarding.nip46check "<bunker url>" --expect-pubkey <hex>
        [--refused-kind 9735] [--map localhost:3080=keycast-relay:8080] [--timeout 20]

``--map`` rewrites the relay host (the public relay URL in the bunker URL is not reachable from
inside the Compose network). Exit code 0 when every step passed; the steps are printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import sys
import time
import urllib.parse
from typing import Any

from coincurve import PrivateKey, PublicKey
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from .blossom import event_id

NOSTR_CONNECT_KIND = 24133


# ---------------------------------------------------------------------------------------------
# NIP-44 v2 (https://github.com/nostr-protocol/nips/blob/master/44.md)


def conversation_key(secret_key: bytes, their_xonly_pubkey: bytes) -> bytes:
    """HKDF-extract(salt="nip44-v2", ikm=x coordinate of the ECDH point)."""
    shared = (
        PublicKey(b"\x02" + their_xonly_pubkey).multiply(secret_key).format(compressed=True)[1:33]
    )
    return hmac.new(b"nip44-v2", shared, hashlib.sha256).digest()


def _message_keys(conv_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    keys = HKDFExpand(algorithm=hashes.SHA256(), length=76, info=nonce).derive(conv_key)
    return keys[0:32], keys[32:44], keys[44:76]


def padded_len(unpadded_len: int) -> int:
    if not 1 <= unpadded_len <= 65535:
        raise ValueError("invalid plaintext length")
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (math.floor(math.log2(unpadded_len - 1)) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded_len - 1) // chunk + 1)


def _pad(plaintext: bytes) -> bytes:
    return len(plaintext).to_bytes(2, "big") + plaintext.ljust(padded_len(len(plaintext)), b"\x00")


def _unpad(padded: bytes) -> bytes:
    unpadded_len = int.from_bytes(padded[:2], "big")
    if unpadded_len == 0 or len(padded) != 2 + padded_len(unpadded_len):
        raise ValueError("invalid padding")
    return padded[2 : 2 + unpadded_len]


def _chacha(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    # cryptography's ChaCha20 takes a 16-byte nonce: 4-byte little-endian counter + 12-byte nonce.
    return (
        Cipher(algorithms.ChaCha20(key, b"\x00" * 4 + nonce12), mode=None).encryptor().update(data)
    )


def nip44_encrypt(conv_key: bytes, plaintext: str, nonce: bytes | None = None) -> str:
    nonce = nonce or os.urandom(32)
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    ciphertext = _chacha(chacha_key, chacha_nonce, _pad(plaintext.encode()))
    mac = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ciphertext + mac).decode()


def nip44_decrypt(conv_key: bytes, payload: str) -> str:
    if payload.startswith("#"):
        raise ValueError("unsupported NIP-44 version")
    data = base64.b64decode(payload, validate=True)
    if len(data) < 99 or data[0] != 2:
        raise ValueError("invalid NIP-44 payload")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    if not hmac.compare_digest(
        mac, hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    ):
        raise ValueError("invalid NIP-44 MAC")
    return _unpad(_chacha(chacha_key, chacha_nonce, ciphertext)).decode()


# ---------------------------------------------------------------------------------------------
# Events and the bunker URL


def sign_event(secret_key: bytes, event: dict[str, Any]) -> dict[str, Any]:
    key = PrivateKey(secret_key)
    event = {**event, "pubkey": key.public_key_xonly.format().hex()}
    event["id"] = event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def parse_bunker_url(url: str) -> tuple[str, list[str], str | None]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "bunker" or len(parts.netloc) != 64:
        raise ValueError(f"not a bunker URL: {url!r}")
    query = urllib.parse.parse_qs(parts.query)
    relays = query.get("relay") or []
    if not relays:
        raise ValueError("bunker URL names no relay")
    return parts.netloc, relays, (query.get("secret") or [None])[0]


class HostMap:
    def __init__(self, spec: str | None):
        self.map: dict[str, str] = {}
        for pair in (spec or "").split(","):
            if "=" in pair:
                src, dst = pair.split("=", 1)
                self.map[src.strip()] = dst.strip()

    def __call__(self, url: str) -> str:
        for src, dst in self.map.items():  # whole-prefix entries may change the scheme too
            if "://" in src and url.startswith(src):
                return dst + url[len(src) :]
        parts = urllib.parse.urlsplit(url)
        if parts.netloc in self.map:
            parts = parts._replace(netloc=self.map[parts.netloc])
        return urllib.parse.urlunsplit(parts)


# ---------------------------------------------------------------------------------------------
# The client


class Nip46Error(Exception):
    pass


class BunkerClient:
    """One relay connection, one throwaway client key, NIP-44 both ways."""

    def __init__(
        self,
        bunker_url: str,
        hostmap: HostMap,
        timeout: float = 20.0,
        debug: bool = False,
        insecure_tls: bool = False,
    ):
        import ssl

        from websockets.sync.client import connect  # imported here: the module is also unit-tested

        self.bunker_pubkey, relays, self.secret = parse_bunker_url(bunker_url)
        self.relay = hostmap(relays[0])
        self.secret_key = PrivateKey().secret
        self.pubkey = PrivateKey(self.secret_key).public_key_xonly.format().hex()
        self.conv_key = conversation_key(self.secret_key, bytes.fromhex(self.bunker_pubkey))
        self.timeout = timeout
        self.debug = debug
        ssl_context = None
        if insecure_tls and self.relay.startswith("wss://"):  # a local proxy with its own CA
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        self._connection = connect(
            self.relay, open_timeout=timeout, close_timeout=5, ssl=ssl_context
        )
        self.ws = self._connection.__enter__()
        self.sub_id = secrets.token_hex(8)
        self.ws.send(
            json.dumps(
                [
                    "REQ",
                    self.sub_id,
                    {
                        "kinds": [NOSTR_CONNECT_KIND],
                        "#p": [self.pubkey],
                        "since": int(time.time()) - 60,
                    },
                ]
            )
        )

    def close(self) -> None:
        try:
            self.ws.send(json.dumps(["CLOSE", self.sub_id]))
        except Exception:
            pass
        self._connection.__exit__(None, None, None)

    def call(self, method: str, params: list[Any]) -> dict[str, Any]:
        request_id = secrets.token_hex(8)
        event = sign_event(
            self.secret_key,
            {
                "created_at": int(time.time()),
                "kind": NOSTR_CONNECT_KIND,
                "tags": [["p", self.bunker_pubkey]],
                "content": nip44_encrypt(
                    self.conv_key,
                    json.dumps({"id": request_id, "method": method, "params": params}),
                ),
            },
        )
        self.ws.send(json.dumps(["EVENT", event]))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Nip46Error(f"no response to {method} within {self.timeout:.0f}s")
            try:
                raw = self.ws.recv(timeout=remaining)
            except TimeoutError as exc:
                raise Nip46Error(f"no response to {method} within {self.timeout:.0f}s") from exc
            if self.debug:
                print(f"  relay -> {str(raw)[:160]}", file=sys.stderr)
            message = json.loads(raw)
            if message[0] == "OK" and message[1] == event["id"] and not message[2]:
                raise Nip46Error(f"relay refused the request: {message[3]}")
            if message[0] == "NOTICE":
                print(f"  relay notice: {message[1]}", file=sys.stderr)
            if message[0] != "EVENT" or message[1] != self.sub_id:
                continue
            reply = message[2]
            try:
                body = json.loads(nip44_decrypt(self.conv_key, reply["content"]))
            except Exception as exc:  # not for us or not NIP-44
                print(
                    f"  undecryptable reply from {reply.get('pubkey', '?')[:12]} skipped: {exc}",
                    file=sys.stderr,
                )
                continue
            if body.get("id") == request_id:
                return body


def run_check(
    bunker_url: str,
    expect_pubkey: str,
    refused_kind: int | None,
    hostmap: HostMap,
    timeout: float,
    debug: bool = False,
    insecure_tls: bool = False,
) -> int:
    client = BunkerClient(bunker_url, hostmap, timeout, debug, insecure_tls)
    print(
        f"relay {client.relay} (from the bunker URL), bunker pubkey {client.bunker_pubkey[:12]}..."
    )
    failures = 0

    def check(cond: bool, text: str) -> None:
        nonlocal failures
        print(("  ok   " if cond else "  FAIL ") + text)
        if not cond:
            failures += 1

    try:
        # Like real clients, retry the connect: for 30 s after a Keycast instance died without
        # leaving the signer hashring, requests hashed to it are dropped without an answer.
        for attempt in range(1, 4):
            try:
                reply = client.call("connect", [client.bunker_pubkey, client.secret or "", ""])
                break
            except Nip46Error as exc:
                if attempt == 3:
                    raise
                print(f"  connect attempt {attempt}: {exc}; retrying", file=sys.stderr)
        outcome = reply.get("result") or reply.get("error")
        check(reply.get("result") == "ack", f"connect with the secret -> {outcome}")
        reply = client.call("get_public_key", [])
        check(
            reply.get("result") == expect_pubkey,
            f"get_public_key -> {str(reply.get('result') or reply.get('error'))[:70]}",
        )
        unsigned = {
            "created_at": int(time.time()),
            "kind": 1,
            "tags": [],
            "content": "hello from a NIP-46 client",
            "pubkey": expect_pubkey,
        }
        reply = client.call("sign_event", [json.dumps(unsigned)])
        signed: dict[str, Any] | None = None
        if isinstance(reply.get("result"), str):
            try:
                signed = json.loads(reply["result"])
            except ValueError:
                signed = None
        from .blossom import verify_signature

        check(
            bool(signed)
            and signed.get("pubkey") == expect_pubkey
            and signed.get("id") == event_id(signed)
            and verify_signature(signed),
            "sign_event kind 1 -> "
            + (
                "valid signature by the participant's key"
                if signed
                else str(reply.get("error"))[:120]
            ),
        )
        if refused_kind is not None:
            reply = client.call("sign_event", [json.dumps({**unsigned, "kind": refused_kind})])
            outcome = str(reply.get("error") or reply.get("result"))[:80]
            check("error" in reply, f"sign_event kind {refused_kind} refused: {outcome}")
    finally:
        client.close()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("bunker_url")
    ap.add_argument(
        "--expect-pubkey",
        required=True,
        help="the participant's pubkey (hex) the signer must report",
    )
    ap.add_argument(
        "--refused-kind",
        type=int,
        default=None,
        help="a kind outside the policy that must be refused",
    )
    ap.add_argument(
        "--map", default=None, help="host rewrites for the relay URL, src=dst[,src=dst]"
    )
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--debug", action="store_true", help="print every relay message")
    ap.add_argument(
        "--insecure-tls", action="store_true", help="do not verify the relay's certificate"
    )
    args = ap.parse_args(argv)
    try:
        return run_check(
            args.bunker_url,
            args.expect_pubkey,
            args.refused_kind,
            HostMap(args.map),
            args.timeout,
            args.debug,
            args.insecure_tls,
        )
    except (Nip46Error, ValueError, OSError) as exc:
        print(f"  FAIL {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
