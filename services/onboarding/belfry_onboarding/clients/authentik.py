"""Authentik REST client (bearer token). Thin and typed, no business logic."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class AuthentikClient:
    def __init__(
        self, url: str, token: str, client: httpx.Client | None = None, timeout: float = 30.0
    ):
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._client.get(f"{self.url}{path}", params=params, headers=self._headers)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = self._client.post(f"{self.url}{path}", json=body, headers=self._headers)
        r.raise_for_status()
        return r.json()

    # -- users --------------------------------------------------------------------------------

    def find_users_by_attribute(self, key: str, value: Any) -> list[dict[str, Any]]:
        data = self._get("/api/v3/core/users/", {"attributes": json.dumps({key: value})})
        return list(data.get("results", []))

    def find_users_by_email(self, email: str) -> list[dict[str, Any]]:
        data = self._get("/api/v3/core/users/", {"email": email})
        return list(data.get("results", []))

    def create_user(
        self,
        username: str,
        name: str,
        email: str | None,
        attributes: dict[str, Any],
        path: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "username": username,
            "name": name,
            "is_active": True,
            "attributes": attributes,
            "path": path,
        }
        if email:
            body["email"] = email
        return self._post("/api/v3/core/users/", body)

    # -- OAuth source connections -------------------------------------------------------------

    def find_source_connections(self, user_pk: int, source_slug: str) -> list[dict[str, Any]]:
        data = self._get(
            "/api/v3/sources/user_connections/oauth/",
            {"user": user_pk, "source__slug": source_slug},
        )
        return list(data.get("results", []))

    def create_source_connection(
        self, user_pk: int, source_slug: str, identifier: str
    ) -> dict[str, Any]:
        # UserOAuthSourceConnectionRequest: user (int), source (slug), identifier (str),
        # access_token (optional). To be confirmed against /api/v3/schema/ in the smoke test.
        return self._post(
            "/api/v3/sources/user_connections/oauth/",
            {"user": user_pk, "source": source_slug, "identifier": identifier},
        )
