"""Keycast (Divine fork) admin client.

- ``POST /api/admin/preload-user`` with the admin UCAN; idempotent on ``vine_id``.
- ``PUT /api/admin/users/:pubkey/status`` with the service token.
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
