#!/usr/bin/env python3
"""Generate a Nostr keypair (secp256k1, x-only public key) without dependencies.

Usage:
  nostr_keys.py            -> JSON {"secret_hex": ..., "pubkey_hex": ..., "npub": ...}
  nostr_keys.py <secret>   -> derive the public key for an existing 64-hex secret
  nostr_keys.py --selftest -> check against known vectors

Pure-Python point multiplication; fine for generating a handful of admin/server keys,
not for anything performance-critical.
"""
import json
import secrets
import sys

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1) * pow(2 * y1, P - 2, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, P - 2, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _mul(k, point=G):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def pubkey_hex(secret_hex):
    d = int(secret_hex, 16)
    if not 0 < d < N:
        raise ValueError("secret out of range")
    x, _ = _mul(d)
    return "%064x" % x


# bech32 (BIP-173) for the npub, so the output can be pasted into Nostr clients.
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def bech32_encode(hrp, data_bytes):
    acc, bits, data = 0, 0, []
    for b in data_bytes:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def selftest():
    assert pubkey_hex("%064x" % 1) == "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    # pair from the Keycast spike (server key)
    assert pubkey_hex("4a04d17606671e18e96a2f3b03005b156f00aec8e3449bec7510586e6f1119a3") == \
        "60bb19f17b390e5ec895277e557e32c93090c89703e03f143d06c8465a179efd"
    assert bech32_encode("npub", bytes.fromhex("3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d")) == \
        "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
    print("ok")


def main(argv):
    if argv and argv[0] == "--selftest":
        return selftest()
    secret = argv[0] if argv else secrets.token_hex(32)
    pub = pubkey_hex(secret)
    print(json.dumps({"secret_hex": secret, "pubkey_hex": pub, "npub": bech32_encode("npub", bytes.fromhex(pub))}))


if __name__ == "__main__":
    main(sys.argv[1:])
