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
        self._admin = {"Authorization": f"Bearer {admin_token}", **common}
        self._service = {"Authorization": f"Bearer {service_token}", **common}

    def preload_user(self, vine_id: str, username: str, display_name: str) -> dict[str, Any]:
        """Returns ``{"pubkey": <hex>, "token": <UCAN>}``. The token is not needed by Belfry."""
        if not vine_id:
            raise ValueError("vine_id must not be empty (Keycast does not check)")
        r = self._client.post(
            f"{self.url}/api/admin/preload-user",
            json={"vine_id": vine_id, "username": username, "display_name": display_name},
            headers=self._admin,
        )
        r.raise_for_status()
        return r.json()

    def set_user_status(self, pubkey: str, status: str) -> None:
        if status not in USER_STATUSES:
            raise ValueError(f"unknown Keycast user status {status!r}")
        r = self._client.put(
            f"{self.url}/api/admin/users/{pubkey}/status",
            json={"status": status},
            headers=self._service,
        )
        r.raise_for_status()
