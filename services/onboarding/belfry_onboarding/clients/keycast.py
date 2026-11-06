"""Keycast (Divine fork) admin client.

- ``POST /api/admin/preload-user`` with the admin UCAN; idempotent on ``vine_id``.
- ``PUT /api/admin/users/:pubkey/status`` with the service token.
- With a user's UCAN (``preload_user`` returns one): ``POST /api/user/profile`` sets the username
  (the NIP-05 name in Keycast's ``/.well-known/nostr.json``) and ``POST /api/user/bunker/create``
  makes a NIP-46 connection (a ``bunker://`` URL whose secret binds the first client that
  connects with it).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_STATUSES = ("active", "suspended", "banned")


class KeycastClient:
    def __init__(
        self,
        url: str,
        admin_token: str,
        service_token: str,
        tenant_domain: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        # Keycast derives the tenant from the Host header (minus port) and rejects unknown hosts,
        # so inside Compose the service sends the configured tenant domain instead of "keycast".
        common = {"Accept": "application/json"}
        if tenant_domain:
            common["Host"] = tenant_domain
        self._common = common
        self._admin = {"Authorization": f"Bearer {admin_token}", **common}
        self._service = {"Authorization": f"Bearer {service_token}", **common}

    def sign_event(self, token: str, event: dict[str, Any]) -> dict[str, Any]:
        """Sign an unsigned event (must carry ``pubkey``) through ``POST /api/nostr``.

        ``token`` is a user-scoped UCAN, e.g. the one ``preload_user`` returns; it is used right
        away and never stored.
        """
        r = self._client.post(
            f"{self.url}/api/nostr",
            json={"method": "sign_event", "params": [event]},
            headers={"Authorization": f"Bearer {token}", **self._common},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"keycast sign_event failed: {data['error']}")
        return data["result"]

    def preload_user(
        self,
        vine_id: str,
        username: str,
        display_name: str,
        key_version: int = 0,
        pubkey: str | None = None,
    ) -> dict[str, Any]:
        """Returns ``{"pubkey": <hex>, "token": <UCAN>}``. The token is not needed by Belfry.
        ``key_version``: the derived key's version (Keycast patch 0014); ``pubkey``: recreate a
        keyless account for a participant who holds their own key."""
        if not vine_id:
            raise ValueError("vine_id must not be empty (Keycast does not check)")
        body: dict[str, Any] = {
            "vine_id": vine_id,
            "username": username,
            "display_name": display_name,
            "key_version": key_version,
        }
        if pubkey:
            body["pubkey"] = pubkey
        r = self._client.post(
            f"{self.url}/api/admin/preload-user",
            json=body,
            headers=self._admin,
        )
        r.raise_for_status()
        return r.json()

    def set_user_status(self, pubkey: str, status: str, reason: str | None = None) -> None:
        """Keycast answers 400 "reason is required when status is suspended or banned"
        (verified 2026-10-23 against fe9c6460), so a reason is mandatory for those."""
        if status not in USER_STATUSES:
            raise ValueError(f"unknown Keycast user status {status!r}")
        body: dict[str, Any] = {"status": status}
        if status != "active":
            body["reason"] = reason or "belfry"
        r = self._client.put(
            f"{self.url}/api/admin/users/{pubkey}/status",
            json=body,
            headers=self._service,
        )
        r.raise_for_status()

    def set_username(self, token: str, username: str) -> None:
        """The account's username: its NIP-05 name (``<username>@<tenant domain>``)."""
        r = self._client.post(
            f"{self.url}/api/user/profile",
            json={"username": username},
            headers={"Authorization": f"Bearer {token}", **self._common},
        )
        if r.status_code == 409:
            raise ValueError(f"the name {username} is taken")
        r.raise_for_status()

    def create_bunker(self, token: str, app_name: str) -> dict[str, Any]:
        """A new NIP-46 connection for the token's user: ``{"bunker_url", "bunker_pubkey", …}``.
        The URL's secret binds the client key that first sends ``connect`` with it."""
        r = self._client.post(
            f"{self.url}/api/user/bunker/create",
            json={"app_name": app_name[:60] or "App"},
            headers={"Authorization": f"Bearer {token}", **self._common},
        )
        r.raise_for_status()
        return r.json()

    def get_profile(self, token: str) -> dict[str, Any]:
        """The token's user: ``{"username", "nip05"}`` (either may be null)."""
        r = self._client.get(
            f"{self.url}/api/user/profile",
            headers={"Authorization": f"Bearer {token}", **self._common},
        )
        r.raise_for_status()
        return r.json()

    def list_sessions(self, token: str) -> list[dict[str, Any]]:
        """The token's user's NIP-46 connections: application_name, redirect_origin,
        bunker_pubkey, client_pubkey, created_at, last_activity, activity_count."""
        r = self._client.get(
            f"{self.url}/api/user/sessions",
            headers={"Authorization": f"Bearer {token}", **self._common},
        )
        r.raise_for_status()
        return list((r.json() or {}).get("sessions") or [])

    def revoke_session(self, token: str, bunker_pubkey: str) -> None:
        r = self._client.post(
            f"{self.url}/api/user/sessions/revoke",
            json={"bunker_pubkey": bunker_pubkey},
            headers={"Authorization": f"Bearer {token}", **self._common},
        )
        r.raise_for_status()

    def pubkey_for_name(self, name: str) -> str | None:
        """The pubkey of ``<name>@<tenant domain>`` (Keycast's nostr.json), or None."""
        r = self._client.get(
            f"{self.url}/.well-known/nostr.json", params={"name": name}, headers=self._common
        )
        if r.status_code != 200:
            return None
        names = (r.json() or {}).get("names") or {}
        return names.get(name.lower())
