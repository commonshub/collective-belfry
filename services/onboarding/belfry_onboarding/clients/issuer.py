"""walt.id Issuer2 management API (community edition, unauthenticated: reachable only inside the
Compose network). One call: create a pre-authorized credential offer for the village pass."""

from __future__ import annotations

from typing import Any

import httpx


class IssuerClient:
    def __init__(self, url: str, client: httpx.Client | None = None, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    def create_offer(
        self,
        profile_id: str,
        credential_data: dict[str, Any],
        status: dict[str, Any] | None,
        expires_in_seconds: int,
    ) -> dict[str, Any]:
        """``POST /issuer2/credential-offers``: returns ``offerId``, ``expiresAt`` (ms) and
        ``credentialOffer`` (the ``openid-credential-offer://`` URL for the QR code). The
        profile's credentialData is overridden per participant; ``status`` becomes the
        credential's ``status`` claim verbatim."""
        body: dict[str, Any] = {
            "profileId": profile_id,
            "authMethod": "PRE_AUTHORIZED",
            "valueMode": "BY_VALUE",
            "expiresInSeconds": expires_in_seconds,
            "runtimeOverrides": {"credentialData": credential_data},
        }
        if status is not None:
            body["runtimeOverrides"]["credentialStatus"] = status
        r = self._client.post(f"{self.url}/issuer2/credential-offers", json=body)
        r.raise_for_status()
        return r.json()

    def credential_issuer_metadata(self) -> dict[str, Any]:
        """The issuer's OpenID4VCI metadata (``/.well-known/openid-credential-issuer/openid4vci``)
        with ``authorization_servers`` filled in: Issuer2 omits it (it is its own authorization
        server, which the spec allows), and walt.id's web wallet then has no token endpoint."""
        r = self._client.get(f"{self.url}/.well-known/openid-credential-issuer/openid4vci")
        r.raise_for_status()
        doc = r.json()
        if not doc.get("authorization_servers") and not doc.get("token_endpoint"):
            doc["authorization_servers"] = [doc["credential_issuer"]]
        return doc
