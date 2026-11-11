"""NIP-98 HTTP authorization: ``Authorization: Nostr <base64 kind-27235 event>``.

Any key may sign (there is no list of belfries): the event must be valid and signed, at most 60
seconds off, name ``POST`` and this gateway's URL, carry the body's SHA-256 in ``payload``, and be
used once (its id is remembered while it could still be replayed).
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any

from coincurve import PublicKeyXOnly

KIND = 27235
MAX_AGE = 60


class AuthError(Exception):
    pass


def event_id(e: dict[str, Any]) -> str:
    raw = json.dumps(
        [0, e["pubkey"], e["created_at"], e["kind"], e["tags"], e["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _tag(e: dict[str, Any], name: str) -> list[str]:
    return [t[1] for t in e.get("tags", []) if isinstance(t, list) and len(t) > 1 and t[0] == name]


class Replays:
    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_use(self, event_id: str, now: float) -> bool:
        with self._lock:
            for k in [k for k, t in self._seen.items() if t < now]:
                del self._seen[k]
            if event_id in self._seen:
                return False
            self._seen[event_id] = now + 2 * MAX_AGE + 5
            return True


def check(
    header: str | None,
    url: str,
    body: bytes,
    replays: Replays,
    now: float | None = None,
) -> str:
    """The signer's pubkey (hex), or AuthError."""
    now = time.time() if now is None else now
    if not header or not header.startswith("Nostr "):
        raise AuthError("missing Nostr authorization")
    try:
        e = json.loads(base64.b64decode(header[6:].strip(), validate=True))
    except Exception as exc:
        raise AuthError("authorization is not a base64 JSON event") from exc
    if not isinstance(e, dict):
        raise AuthError("authorization is not an event")
    for k in ("id", "pubkey", "sig", "kind", "created_at", "tags", "content"):
        if k not in e:
            raise AuthError(f"event lacks {k}")
    if e["kind"] != KIND:
        raise AuthError("event is not kind 27235")
    if not isinstance(e["created_at"], int) or isinstance(e["created_at"], bool):
        raise AuthError("created_at is not a number")
    if not isinstance(e["pubkey"], str) or len(e["pubkey"]) != 64:
        raise AuthError("bad pubkey")
    try:
        ok = event_id(e) == e["id"] and PublicKeyXOnly(bytes.fromhex(e["pubkey"])).verify(
            bytes.fromhex(e["sig"]), bytes.fromhex(e["id"])
        )
    except Exception:
        ok = False
    if not ok:
        raise AuthError("bad signature")
    if abs(now - e["created_at"]) > MAX_AGE:
        raise AuthError("authorization too old or in the future")
    if [m.upper() for m in _tag(e, "method")] != ["POST"]:
        raise AuthError("method tag does not match")
    if not any(u.rstrip("/") == url.rstrip("/") for u in _tag(e, "u")):
        raise AuthError("u tag names another URL")
    if _tag(e, "payload")[:1] != [hashlib.sha256(body).hexdigest()]:
        raise AuthError("payload tag is not the body's SHA-256")
    if not replays.first_use(e["id"], now):
        raise AuthError("this authorization was used already")
    return e["pubkey"]
