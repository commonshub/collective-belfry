"""Publish events to a Nostr relay, answering a NIP-42 AUTH challenge through a signer callback.

The community relay requires AUTH for writing, so the publisher signs the kind-22242 challenge
event with the same signer (Keycast, on behalf of the participant) that signed the event itself.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from websockets.sync.client import connect

log = logging.getLogger(__name__)

# Signs an unsigned event (without ``pubkey``/``id``/``sig``) and returns the signed event.
Signer = Callable[[dict[str, Any]], dict[str, Any]]

AUTH_KIND = 22242


class RelayError(RuntimeError):
    pass


class RelayPublisher:
    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def publish(self, event: dict[str, Any], sign: Signer) -> None:
        """Send ``event`` and wait for its OK; answer an AUTH challenge once if the relay asks."""
        with connect(self.url, open_timeout=self.timeout, close_timeout=self.timeout) as ws:
            ws.send(json.dumps(["EVENT", event]))
            auth_id: str | None = None
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                kind = msg[0]
                if kind == "AUTH" and auth_id is None:
                    auth = sign(
                        {
                            "kind": AUTH_KIND,
                            "created_at": int(time.time()),
                            "tags": [["relay", self.url], ["challenge", msg[1]]],
                            "content": "",
                        }
                    )
                    auth_id = auth["id"]
                    ws.send(json.dumps(["AUTH", auth]))
                elif kind == "OK" and auth_id is not None and msg[1] == auth_id:
                    if not msg[2]:
                        raise RelayError(f"AUTH rejected: {msg[3] if len(msg) > 3 else ''}")
                    ws.send(json.dumps(["EVENT", event]))
                elif kind == "OK" and msg[1] == event["id"]:
                    reason = msg[3] if len(msg) > 3 else ""
                    if msg[2]:
                        return
                    if reason.startswith("auth-required"):
                        # The challenge arrives separately; keep waiting for it or for the resend.
                        continue
                    raise RelayError(f"event rejected: {reason}")
                elif kind == "NOTICE":
                    log.info("relay notice: %s", msg[1] if len(msg) > 1 else "")
            raise RelayError("timed out waiting for the relay's OK")

    def query(self, filters: list[dict[str, Any]], sign: Signer) -> list[dict[str, Any]]:
        """Events matching ``filters`` up to EOSE; answers an AUTH challenge once (the community
        relay serves reads only to whitelisted, authenticated pubkeys)."""
        sub_id = "belfry"
        req = json.dumps(["REQ", sub_id, *filters])
        events: list[dict[str, Any]] = []
        with connect(self.url, open_timeout=self.timeout, close_timeout=self.timeout) as ws:
            ws.send(req)
            auth_id: str | None = None
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                kind = msg[0]
                if kind == "AUTH" and auth_id is None:
                    auth = sign(
                        {
                            "kind": AUTH_KIND,
                            "created_at": int(time.time()),
                            "tags": [["relay", self.url], ["challenge", msg[1]]],
                            "content": "",
                        }
                    )
                    auth_id = auth["id"]
                    ws.send(json.dumps(["AUTH", auth]))
                elif kind == "OK" and auth_id is not None and msg[1] == auth_id:
                    if not msg[2]:
                        raise RelayError(f"AUTH rejected: {msg[3] if len(msg) > 3 else ''}")
                    events.clear()
                    ws.send(req)
                elif kind == "EVENT" and msg[1] == sub_id:
                    events.append(msg[2])
                elif kind == "EOSE" and msg[1] == sub_id:
                    return events
                elif kind == "CLOSED" and msg[1] == sub_id:
                    reason = msg[2] if len(msg) > 2 else ""
                    if reason.startswith("auth-required"):
                        continue
                    raise RelayError(f"query closed: {reason}")
                elif kind == "NOTICE":
                    log.info("relay notice: %s", msg[1] if len(msg) > 1 else "")
            raise RelayError("timed out waiting for the relay's EOSE")


# The community relay answers at most this many events per filter (rnostr `max_limit`).
PAGE_LIMIT = 300


def query_all(
    relay: Any, flt: dict[str, Any], sign: Signer, page: int = PAGE_LIMIT, max_pages: int = 100
) -> list[dict[str, Any]]:
    """Every event matching one filter, newest first, page by page with ``until``. The relay caps
    each answer (``PAGE_LIMIT``), so a single REQ silently drops the oldest events. ``until`` is
    inclusive, so pages overlap at their oldest second; paging ends when a page brings nothing
    new (also when more than a page shares one second, which the caller avoids by spreading
    ``created_at``)."""
    seen: dict[str, dict[str, Any]] = {}
    until: int | None = None
    for _ in range(max_pages):
        q = {**flt, "limit": page} | ({"until": until} if until is not None else {})
        events = relay.query([q], sign)
        new = [e for e in events if e["id"] not in seen]
        if not new:
            break
        for e in new:
            seen[e["id"]] = e
        until = min(e["created_at"] for e in events)
    return sorted(seen.values(), key=lambda e: e["created_at"], reverse=True)
