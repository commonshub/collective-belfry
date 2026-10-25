"""Proof that a visitor controls a Nostr key: a signed NIP-98 event for the invite page.

When someone joins with a key they already have, the invite page asks their signer (a NIP-07
browser extension) to sign a kind-27235 event naming the invite URL and POST. This module checks
it: the event id, the BIP-340 Schnorr signature, the key, the URL path, the method and the time.
Pure Python (no secp256k1 library in Odoo's image); one check takes a few milliseconds. The
onboarding service checks the same event again before it creates the account.
"""

import hashlib
import json
import time
from urllib.parse import urlparse

PROOF_KIND = 27235
PROOF_MAX_AGE = 600  # seconds the signed event stays usable (it is signed when the form is sent)

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


class ProofError(ValueError):
    """The proof does not hold; the message is shown to the visitor."""


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        lam = 3 * p[0] * p[0] * pow(2 * p[1], _P - 2, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], _P - 2, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(p, n):
    r = None
    while n:
        if n & 1:
            r = _add(r, p)
        p = _add(p, p)
        n >>= 1
    return r


def _lift_x(x):
    if x >= _P:
        return None
    y2 = (pow(x, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if y * y % _P != y2:
        return None
    return x, y if y % 2 == 0 else _P - y


def _tagged_hash(tag, data):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + data).digest()


def schnorr_verify(pubkey, msg, sig):
    """BIP-340 verification; ``pubkey`` 32 bytes (x only), ``msg`` 32 bytes, ``sig`` 64 bytes."""
    if len(pubkey) != 32 or len(msg) != 32 or len(sig) != 64:
        return False
    point = _lift_x(int.from_bytes(pubkey, 'big'))
    r = int.from_bytes(sig[:32], 'big')
    s = int.from_bytes(sig[32:], 'big')
    if point is None or r >= _P or s >= _N:
        return False
    e = int.from_bytes(_tagged_hash("BIP0340/challenge", sig[:32] + pubkey + msg), 'big') % _N
    rp = _add(_mul(_G, s), _mul(point, _N - e))
    return rp is not None and rp[1] % 2 == 0 and rp[0] == r


def event_id(event):
    serialized = json.dumps(
        [0, event['pubkey'], event['created_at'], event['kind'], event['tags'], event['content']],
        separators=(',', ':'),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def npub(pubkey_hex):
    """The npub (NIP-19, bech32) of a 64-hex public key."""
    data = []
    acc = bits = 0
    for byte in bytes.fromhex(pubkey_hex):
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)

    def polymod(values):
        chk = 1
        for value in values:
            top = chk >> 25
            chk = (chk & 0x1FFFFFF) << 5 ^ value
            for i in range(5):
                chk ^= gen[i] if (top >> i) & 1 else 0
        return chk

    hrp = 'npub'
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = polymod(expanded + data + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + '1' + ''.join(_BECH32[d] for d in data + checksum)


def check_proof(proof, pubkey, path, now=None):
    """Check ``proof`` (the signed event as JSON text) for ``pubkey`` (64 hex) and the invite
    page ``path``. Returns the public key in lower case; raises ProofError otherwise."""
    pubkey = (pubkey or '').strip().lower()
    if len(pubkey) != 64 or any(c not in '0123456789abcdef' for c in pubkey):
        raise ProofError("The Nostr key is not a valid public key.")
    try:
        event = json.loads(proof or '')
        tags = {t[0]: t[1] for t in event['tags'] if isinstance(t, list) and len(t) >= 2}
        created_at = int(event['created_at'])
        valid = isinstance(event['sig'], str) and isinstance(event['id'], str)
    except (ValueError, KeyError, TypeError, IndexError):
        raise ProofError("Your signer's answer could not be read. Please try again.") from None
    if not valid or event.get('kind') != PROOF_KIND or event.get('pubkey') != pubkey:
        raise ProofError("Your signer signed something else. Please try again.")
    if urlparse(tags.get('u', '')).path != path or tags.get('method', '').upper() != 'POST':
        raise ProofError("Your signer signed something else. Please try again.")
    if abs((now or time.time()) - created_at) > PROOF_MAX_AGE:
        raise ProofError("The signature is too old. Please send the form again.")
    if event_id(event) != event['id']:
        raise ProofError("The signed event is damaged. Please try again.")
    try:
        ok = schnorr_verify(bytes.fromhex(pubkey), bytes.fromhex(event['id']), bytes.fromhex(event['sig']))
    except ValueError:
        ok = False
    if not ok:
        raise ProofError("The signature does not match the key.")
    return pubkey
