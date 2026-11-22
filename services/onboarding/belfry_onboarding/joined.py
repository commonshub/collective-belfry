"""Someone joined the belfry: a line in the main channel, so everyone sees who came in.

Xavier, 2026-11-22: "Make sure that when someone joins the belfry there is a message in town
square to make sure everyone can see who entered the room."

The line is a NIP-29 ``9000`` in ``general`` by the groups key (docs/one-shape.md "Joining"):

    ["h", "general"], ["p", <pubkey>, "member"], ["name", <their name>], ["joined", "belfry"]
    and ["agent", "1"] for an agent

Once a person: a second membership, or a guest who becomes a member after having been announced,
adds nothing. Guests of one channel are announced in that channel by guests.admitted, not here.
Best effort: a relay that does not answer costs the line, never the membership.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .core import Services

log = logging.getLogger(__name__)

MAIN = "general"
TAG = ["joined", "belfry"]


def _relay(svc: Services) -> tuple[Any, Any] | None:
    from . import followers

    return followers._relay(svc)


def tags_for(pubkey: str, name: str, agent: bool = False) -> list[list[str]]:
    tags = [["h", MAIN], ["p", pubkey, "member"], list(TAG)]
    name = " ".join((name or "").split())[:100]
    if name:
        tags.append(["name", name])
    if agent:
        tags.append(["agent", "1"])
    return tags


def announce(svc: Services, pubkey: str, name: str, agent: bool = False) -> bool:
    """The join line for PUBKEY, unless the main channel has one already. True when published."""
    if not pubkey:
        return False
    try:
        got = _relay(svc)
        if got is None:
            return False
        relay, sign = got
        seen = relay.query([{"kinds": [9000], "#h": [MAIN], "#p": [pubkey]}], sign) or []
        if any(TAG in e.get("tags", []) for e in seen):
            return False
        relay.publish(
            sign(
                {
                    "kind": 9000,
                    "created_at": int(time.time()),
                    "tags": tags_for(pubkey, name, agent),
                    "content": "",
                }
            ),
            sign,
        )
        log.info("joined: %s… announced in #%s", pubkey[:8], MAIN)
        return True
    except Exception as exc:  # noqa: BLE001 — the membership is open; the line is a courtesy
        log.warning("joined: no line for %s…: %r", pubkey[:8], exc)
        return False
