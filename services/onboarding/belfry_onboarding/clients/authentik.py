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

    # -- groups -------------------------------------------------------------------------------

    def find_group(self, name: str) -> dict[str, Any] | None:
        data = self._get("/api/v3/core/groups/", {"name": name})
        results = list(data.get("results", []))
        return results[0] if results else None

    def group_member_pks(self, name: str) -> set[int]:
        members: set[int] = set()
        page = 1
        while page:
            data = self._get(
                "/api/v3/core/users/", {"groups_by_name": name, "page": page, "page_size": 500}
            )
            members.update(int(u["pk"]) for u in data.get("results", []))
            page = int((data.get("pagination") or {}).get("next") or 0)
        return members

    def _post_no_content(self, path: str, body: dict[str, Any]) -> None:
        r = self._client.post(f"{self.url}{path}", json=body, headers=self._headers)
        r.raise_for_status()

    def add_user_to_group(self, group_pk: str, user_pk: int) -> None:
        """Idempotent: authentik answers 204 whether or not the user already is a member."""
        self._post_no_content(f"/api/v3/core/groups/{group_pk}/add_user/", {"pk": user_pk})

    def remove_user_from_group(self, group_pk: str, user_pk: int) -> None:
        """Idempotent: 204 also for a non-member."""
        self._post_no_content(f"/api/v3/core/groups/{group_pk}/remove_user/", {"pk": user_pk})

    # -- account setup (recovery) links -------------------------------------------------------

    def create_recovery_link(self, user_pk: int, token_duration: str) -> str:
        """One-time link into the brand's recovery flow with ``user_pk`` as the pending user
        (``POST /api/v3/core/users/<pk>/recovery/``). The token replaces any earlier one for the
        user; authentik builds the link from the request host, so callers rewrite the origin.
        ``token_duration`` uses authentik's timedelta string, e.g. ``days=30``.
        400 "No recovery flow set." when the default brand has no recovery flow."""
        data = self._post(
            f"/api/v3/core/users/{user_pk}/recovery/", {"token_duration": token_duration}
        )
        return str(data["link"])

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
