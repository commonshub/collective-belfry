"""Pass certificates (Phase 3): the claims of a village pass, the Token Status List that makes
revocation visible to verifiers, the SD-JWT VC Type Metadata document, and reading a verified
presentation back out of a Verifier2 session.

The credential itself is issued by walt.id Issuer2 (clients/issuer.py) with Belfry's P-256 key
and x5c chain; the same key signs the status list here, so verifiers accept the list by direct
trust (same x5c leaf as the credential; walt.id checks exactly that).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
import zlib
from typing import Any

import jwt

VCT_PATH = "/vct/village-pass/v1"
STATUS_LIST_TTL = 300  # default seconds a verifier may cache the list (ttl claim, max-age)
STATUS_LIST_MIN_BITS = 8 * 1024


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# -- claims --------------------------------------------------------------------------------------


def pass_credential_data(
    tier: str,
    display_name: str,
    member_number: str,
    npub: str,
    date_start: dt.date,
    date_end: dt.date,
    now: int | None = None,
) -> dict[str, Any]:
    """The claims Issuer2 puts into the SD-JWT VC (credentialData override). Validity is the
    pass period: ``nbf`` at the start of date_start (UTC), ``exp`` at the end of date_end.
    Timestamps are explicit so Issuer2 does not round them."""
    now = int(time.time()) if now is None else now
    nbf = int(dt.datetime.combine(date_start, dt.time.min, tzinfo=dt.UTC).timestamp())
    exp = int(dt.datetime.combine(date_end, dt.time.max, tzinfo=dt.UTC).timestamp())
    return {
        "pass_tier": tier,
        "display_name": display_name,
        "member_number": member_number,
        "npub": npub,
        "iat": now,
        "nbf": min(nbf, now),  # a pass bought during the event is valid at once
        "exp": exp,
    }


def status_claim(uri: str, idx: int) -> dict[str, Any]:
    return {"status_list": {"idx": idx, "uri": uri}}


# -- Token Status List (draft-ietf-oauth-status-list) -------------------------------------------


def encode_status_list(revoked: set[int], bits: int = 1) -> tuple[str, int]:
    """(``lst`` value, number of entries): one bit per index, little-endian within a byte
    (index 0 is the least significant bit of byte 0), zlib-compressed, base64url.
    Returns 1 for every index in ``revoked`` (INVALID), 0 otherwise (VALID)."""
    assert bits == 1
    size = max(STATUS_LIST_MIN_BITS, ((max(revoked, default=0) // 8) + 1) * 8)
    size = ((size + 7) // 8) * 8
    data = bytearray(size // 8)
    for idx in revoked:
        if idx < 0:
            continue
        data[idx // 8] |= 1 << (idx % 8)
    return b64url(zlib.compress(bytes(data), 9)), size


def decode_status_list(lst: str) -> bytes:
    raw = base64.urlsafe_b64decode(lst + "=" * (-len(lst) % 4))
    return zlib.decompress(raw)


def status_at(lst: str, idx: int) -> int:
    data = decode_status_list(lst)
    return (data[idx // 8] >> (idx % 8)) & 1 if idx // 8 < len(data) else 0


def status_list_token(
    issuer: str,
    uri: str,
    revoked: set[int],
    jwk: dict[str, Any],
    x5c: list[str],
    ttl: int = STATUS_LIST_TTL,
) -> str:
    """``application/statuslist+jwt`` signed with the issuer key (ES256) and the x5c chain.
    ``ttl`` tells verifiers how long they may cache the list: how long a revocation may take to
    reach the door."""
    lst, _size = encode_status_list(revoked)
    now = int(time.time())
    payload = {
        "iss": issuer,
        "sub": uri,
        "iat": now,
        "ttl": ttl,
        "exp": now + 24 * 3600,
        "status_list": {"bits": 1, "lst": lst},
    }
    key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    headers: dict[str, Any] = {"typ": "statuslist+jwt", "kid": jwk.get("kid")}
    if x5c:
        headers["x5c"] = x5c
    return jwt.encode(payload, key, algorithm="ES256", headers=headers)


# -- Type Metadata -------------------------------------------------------------------------------


def type_metadata(vct: str) -> dict[str, Any]:
    """SD-JWT VC Type Metadata for the village pass (served at ``vct``). One type for every
    tier; the tier and the validity are claims. Name, member number and npub are selectively
    disclosable; the door asks for the tier only."""
    return {
        "vct": vct,
        "name": "Open Source Village Pass",
        "description": "Pass for Open Source Village, issued by Belfry. One credential type for "
        "day, week and full-access passes; the tier and the validity period are claims.",
        "display": [
            {
                "lang": "en",
                "name": "Open Source Village Pass",
                "description": "Your pass for Open Source Village",
                "rendering": {
                    "simple": {"background_color": "#1F2A44", "text_color": "#FFFFFF"},
                },
            }
        ],
        "claims": [
            {
                "path": ["pass_tier"],
                "display": [{"lang": "en", "label": "Pass tier"}],
                "sd": "never",
            },
            {
                "path": ["display_name"],
                "display": [{"lang": "en", "label": "Name"}],
                "sd": "allowed",
            },
            {
                "path": ["member_number"],
                "display": [{"lang": "en", "label": "Member number"}],
                "sd": "allowed",
            },
            {
                "path": ["npub"],
                "display": [{"lang": "en", "label": "Nostr public key"}],
                "sd": "allowed",
            },
        ],
    }


# -- reading a Verifier2 session -----------------------------------------------------------------


def presented_credential(info: dict[str, Any]) -> dict[str, Any] | None:
    """The first presented village pass of a successful Verifier2 session (walt.id's record
    with ``credentialData`` = claims with disclosures applied, ``signed`` = the issuer JWT)."""
    if info.get("status") != "SUCCESSFUL":
        return None
    presented = info.get("presented_credentials") or {}
    for _query_id, creds in presented.items() if isinstance(presented, dict) else []:
        for cred in creds or []:
            if isinstance(cred, dict) and isinstance(cred.get("credentialData"), dict):
                return cred
    return None


def presented_claims(info: dict[str, Any]) -> dict[str, Any] | None:
    """The disclosed claims of the first presented village pass, or None."""
    cred = presented_credential(info)
    return cred["credentialData"] if cred else None


def issuer_leaf(cred: dict[str, Any]) -> str | None:
    """The base64 DER leaf certificate (``x5c[0]``) the presented credential was signed with."""
    signed = cred.get("signed") or cred.get("signedWithDisclosures") or ""
    try:
        header = json.loads(
            base64.urlsafe_b64decode(signed.split(".")[0] + "=" * (-len(signed.split(".")[0]) % 4))
        )
    except (ValueError, IndexError):
        return None
    x5c = header.get("x5c") or []
    return x5c[0] if x5c else None


def issued_by_belfry(cred: dict[str, Any], accepted_leaves: list[str]) -> bool:
    """Verifier2's ``signature`` policy trusts whatever x5c leaf the credential carries, so the
    door and the bridge pin the issuer: the leaf must be one of Belfry's own (the current
    signer, plus older ones kept during a rotation). Without a configured leaf nothing is
    accepted."""
    leaf = issuer_leaf(cred)
    return bool(leaf) and leaf in accepted_leaves
