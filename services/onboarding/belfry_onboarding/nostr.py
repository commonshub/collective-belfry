"""Bech32 (BIP-173) encoding of Nostr public keys: hex pubkey -> ``npub1...`` (NIP-19)."""

from __future__ import annotations

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _create_checksum(hrp: str, data: list[int]) -> list[int]:
    polymod = _polymod(_hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def bech32_encode(hrp: str, payload: bytes) -> str:
    data = _convertbits(payload, 8, 5)
    return hrp + "1" + "".join(_CHARSET[d] for d in data + _create_checksum(hrp, data))


def hex_to_npub(pubkey_hex: str) -> str:
    raw = bytes.fromhex(pubkey_hex)
    if len(raw) != 32:
        raise ValueError(f"Nostr pubkey must be 32 bytes, got {len(raw)}")
    return bech32_encode("npub", raw)


def bech32_decode(text: str) -> tuple[str, bytes]:
    """``npub1…`` and friends -> (hrp, payload); raises ValueError on a bad checksum."""
    text = text.strip().lower()
    pos = text.rfind("1")
    if pos < 1 or len(text) - pos < 7:
        raise ValueError("not bech32")
    hrp, rest = text[:pos], text[pos + 1 :]
    try:
        data = [_CHARSET.index(c) for c in rest]
    except ValueError as exc:
        raise ValueError("not bech32") from exc
    if _polymod(_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    values = data[:-6]
    acc = bits = 0
    out = bytearray()
    for v in values:
        acc = (acc << 5) | v
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return hrp, bytes(out)


def to_hex_pubkey(value: str) -> str:
    """A pubkey given as ``npub1…`` or 64 hex characters -> hex; raises ValueError."""
    value = value.strip()
    if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    hrp, raw = bech32_decode(value)
    if hrp != "npub" or len(raw) != 32:
        raise ValueError("not an npub")
    return raw.hex()
