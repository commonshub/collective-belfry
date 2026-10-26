"""Groups relay members: the pubkeys of active passes, kept in step on zooid's member API.

The groups relay (zooid, ``infra/groups-relay``) lets only its members read and write. Its
member list is derived from Odoo like the community relay's whitelist and never edited by
hand: ``sync`` adds every active pubkey that is missing and removes every member that is not
active any more (a removal neither bans the key nor deletes what it wrote, so a pass that is
reactivated picks up where it left off). Calls are NIP-98 signed with the groups API key
(secret ``groups_api_nsec``), the only key zooid's ``API_WHITELIST`` admits; they go to the
relay's internal host name, which the proxy never forwards.

Group apps show a member's name and picture from the kind-0 profile on the group relay itself,
where a participant's profile only arrives once they write there. ``import_profiles`` copies
the members' signed profiles from the community relay (read with NIP-42 AUTH as the groups API
key, which the community relay's whitelist carries) through zooid's import API; the events stay
the participants' own, signed by them.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx
from coincurve import PrivateKey

from .relay_whitelist import normalize_pubkeys

log = logging.getLogger(__name__)

HTTP_AUTH_KIND = 27235
PROFILE_KIND = 0
RELAY_ID = "groups"  # the config file's name in infra/groups-relay/entrypoint.sh


class GroupsMembersError(RuntimeError):
    pass


def api_key(secret_hex: str) -> PrivateKey | None:
    raw = (secret_hex or "").strip()
    if not raw:
        return None
    try:
        return PrivateKey(bytes.fromhex(raw))
    except ValueError as exc:
        raise GroupsMembersError("groups_api_nsec is not a 64-character hex key") from exc


def api_pubkey(secret_hex: str) -> str:
    """The groups API key's pubkey (hex), or "" without a usable key; the community relay's
    whitelist carries it, so the key can read the members' profiles there."""
    try:
        key = api_key(secret_hex)
    except GroupsMembersError:
        return ""
    return key.public_key_xonly.format().hex() if key else ""


def signed(key: PrivateKey, template: dict[str, Any]) -> dict[str, Any]:
    from .blossom import event_id  # blossom imports core, which imports this module

    event = {**template, "pubkey": key.public_key_xonly.format().hex()}
    event["id"] = event_id(event)
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def nip98_header(key: PrivateKey, url: str, method: str, now: int | None = None) -> str:
    event = signed(
        key,
        {
            "created_at": int(now if now is not None else time.time()),
            "kind": HTTP_AUTH_KIND,
            "tags": [["u", url], ["method", method]],
            "content": "",
        },
    )
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


class GroupsMembers:
    def __init__(self, api_url: str, key: PrivateKey, http: httpx.Client | None = None):
        self.api_url = api_url.rstrip("/")
        self.key = key
        self.http = http or httpx.Client(timeout=10)
        # The owner (this key) and the relay's own key are members by construction; zooid
        # refuses to remove them (409), so they are left out of the comparison.
        self.own = key.public_key_xonly.format().hex()

    def _call(self, method: str, path: str) -> httpx.Response:
        url = f"{self.api_url}/relay/{RELAY_ID}{path}"
        return self.http.request(
            method, url, headers={"Authorization": nip98_header(self.key, url, method)}
        )

    def members(self) -> list[str]:
        response = self._call("GET", "/members")
        if response.status_code != 200:
            raise GroupsMembersError(f"GET members: {response.status_code} {response.text[:200]}")
        return normalize_pubkeys(response.json().get("members") or [])

    def sync(self, pubkeys: list[str]) -> dict[str, Any]:
        wanted = set(normalize_pubkeys(pubkeys)) - {self.own}
        current = set(self.members()) - {self.own}
        added, removed, failed = [], [], []
        for pubkey in sorted(wanted - current):
            response = self._call("PUT", f"/members/{pubkey}")
            (added if response.status_code == 200 else failed).append(pubkey)
            if response.status_code != 200:
                log.warning(
                    "groups relay: adding %s failed: %s %s",
                    pubkey,
                    response.status_code,
                    response.text[:200],
                )
        for pubkey in sorted(current - wanted):
            response = self._call("DELETE", f"/members/{pubkey}")
            if response.status_code == 409:  # the relay's own key: a member by construction
                continue
            (removed if response.status_code == 200 else failed).append(pubkey)
            if response.status_code != 200:
                log.warning(
                    "groups relay: removing %s failed: %s %s",
                    pubkey,
                    response.status_code,
                    response.text[:200],
                )
        if added or removed:
            log.info("groups relay members: %d added, %d removed", len(added), len(removed))
        if failed:
            raise GroupsMembersError(f"groups relay: {len(failed)} member changes failed")
        return {"added": added, "removed": removed, "members": len(wanted)}

    def import_profiles(self, relay: Any, pubkeys: list[str], chunk: int = 100) -> int:
        """Copy the newest kind-0 profile of each of ``pubkeys`` from ``relay`` (a
        RelayPublisher on the community relay) into the groups relay; returns how many it
        stored. zooid keeps the newest of a replaceable kind, so a repeat is harmless."""
        wanted = normalize_pubkeys(pubkeys)
        newest: dict[str, dict[str, Any]] = {}
        for i in range(0, len(wanted), chunk):
            authors = wanted[i : i + chunk]
            for event in relay.query(
                [{"kinds": [PROFILE_KIND], "authors": authors, "limit": len(authors)}],
                lambda template: signed(self.key, template),
            ):
                held = newest.get(event.get("pubkey", ""))
                if event.get("pubkey") in authors and (
                    held is None or event["created_at"] > held["created_at"]
                ):
                    newest[event["pubkey"]] = event
        if not newest:
            return 0
        url = f"{self.api_url}/relay/{RELAY_ID}/import"
        response = self.http.post(
            url,
            content="\n".join(json.dumps(e) for e in newest.values()) + "\n",
            headers={
                "Authorization": nip98_header(self.key, url, "POST"),
                "Content-Type": "application/x-ndjson",
            },
        )
        if response.status_code != 200:
            raise GroupsMembersError(f"import: {response.status_code} {response.text[:200]}")
        result = response.json()
        if result.get("invalid"):
            log.warning("groups relay import: %s", result.get("problems"))
        return int(result.get("imported", 0))
