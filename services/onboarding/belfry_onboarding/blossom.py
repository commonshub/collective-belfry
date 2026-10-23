"""Blossom gate (Phase 2): decides whether a write to the Blossom file server is allowed.

blossom-server verifies the BUD-11 auth event itself, but it cannot know who holds a pass. The
gate in front of it (infra/blossom/Caddyfile) asks ``GET /blossom/auth`` with the original
``Authorization: Nostr <base64 event>`` header; this module checks the event (kind 24242,
Schnorr signature, verb, expiry, server tag) and that its pubkey has an active pass in Odoo.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any

from coincurve import PublicKeyXOnly

from .core import Services

log = logging.getLogger(__name__)

BLOSSOM_AUTH_KIND = 24242
VERBS_BY_METHOD = {"PUT": {"upload"}, "DELETE": {"delete"}, "HEAD": {"upload"}}
MAX_FUTURE_SKEW = 60  # created_at may not lie this far in the future
MAX_TOKEN_LIFETIME = 24 * 3600  # an auth event valid longer than this is refused


class BlossomAuthError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def event_id(event: dict[str, Any]) -> str:
    serialized = json.dumps(
        [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event["tags"],
            event["content"],
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def verify_signature(event: dict[str, Any]) -> bool:
    try:
        return PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
            bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])
        )
    except Exception:
        return False


def parse_authorization(header: str | None) -> dict[str, Any]:
    if not header or not header.startswith("Nostr "):
        raise BlossomAuthError(401, "missing Nostr authorization")
    try:
        event = json.loads(base64.b64decode(header[6:].strip(), validate=True))
    except Exception as exc:
        raise BlossomAuthError(401, "authorization is not a base64 JSON event") from exc
    if not isinstance(event, dict):
        raise BlossomAuthError(401, "authorization is not an event")
    return event


def tag_values(event: dict[str, Any], name: str) -> list[str]:
    return [
        t[1] for t in event.get("tags", []) if isinstance(t, list) and len(t) > 1 and t[0] == name
    ]


def check_event(
    event: dict[str, Any], method: str, hosts: list[str], now: int | None = None
) -> str:
    """Validate a BUD-11 event for an HTTP ``method``; returns the pubkey (hex)."""
    now = int(time.time()) if now is None else now
    for key in ("id", "pubkey", "sig", "kind", "created_at", "tags", "content"):
        if key not in event:
            raise BlossomAuthError(401, f"event lacks {key}")
    if event["kind"] != BLOSSOM_AUTH_KIND:
        raise BlossomAuthError(401, "event is not kind 24242")
    if not isinstance(event["pubkey"], str) or len(event["pubkey"]) != 64:
        raise BlossomAuthError(401, "bad pubkey")
    if event_id(event) != event["id"]:
        raise BlossomAuthError(401, "event id does not match")
    if not verify_signature(event):
        raise BlossomAuthError(401, "bad signature")
    created = int(event["created_at"])
    if created > now + MAX_FUTURE_SKEW:
        raise BlossomAuthError(401, "event created in the future")
    expirations = tag_values(event, "expiration")
    if not expirations:
        raise BlossomAuthError(401, "no expiration tag")
    expiration = int(expirations[0])
    if expiration <= now:
        raise BlossomAuthError(401, "authorization expired")
    if expiration - created > MAX_TOKEN_LIFETIME:
        raise BlossomAuthError(401, "authorization valid for too long")
    verbs = set(tag_values(event, "t"))
    if not verbs & VERBS_BY_METHOD.get(method.upper(), set()):
        raise BlossomAuthError(403, f"verb {sorted(verbs)} does not allow {method}")
    servers = tag_values(event, "server")
    if servers and hosts and not any(s.lower() in hosts for s in servers):
        raise BlossomAuthError(403, "server tag names another server")
    return event["pubkey"]


def pubkey_has_active_pass(svc: Services, pubkey: str) -> bool:
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["id"],
        limit=1,
    )
    return bool(rows)


def authorize(svc: Services, authorization: str | None, method: str) -> str:
    """The pubkey allowed to write, or raises BlossomAuthError (401 bad token, 403 no pass)."""
    hosts = [h.strip().lower() for h in svc.settings.blossom_hosts.split(",") if h.strip()]
    event = parse_authorization(authorization)
    pubkey = check_event(event, method, hosts)
    if not pubkey_has_active_pass(svc, pubkey):
        log.info("blossom: %s refused for %s, no active pass", method, pubkey)
        raise BlossomAuthError(403, "no active pass for this key")
    return pubkey
