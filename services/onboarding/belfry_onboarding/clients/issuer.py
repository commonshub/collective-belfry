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
        sign_in: bool = False,
    ) -> dict[str, Any]:
        """``POST /issuer2/credential-offers``: returns ``offerId``, ``expiresAt`` (ms) and
        ``credentialOffer`` (the ``openid-credential-offer://`` URL for the QR code). The
        profile's credentialData is overridden per participant; ``status`` becomes the
        credential's ``status`` claim verbatim. ``sign_in`` mints an authorization-code offer
        instead: the wallet sends the holder through Issuer2's login (Authentik), and the offer's
        ``issuer_state`` ties that login to this offer's data."""
        body: dict[str, Any] = {
            "profileId": profile_id,
            "authMethod": "AUTHORIZED" if sign_in else "PRE_AUTHORIZED",
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

    # -- draft-13 wallets (walt.id's web wallet) ------------------------------------------------
    # Issuer2 1.2.0 speaks OpenID4VCI 1.0 only. Draft-13 wallets take the proof nonce from the
    # token response and send {format, vct, proof} to the credential endpoint, expecting a single
    # `credential` back. The proxy routes both endpoints through the onboarding service, which
    # translates; 1.0 requests pass through unchanged.

    def token(self, form: dict[str, str]) -> httpx.Response:
        """Issuer2's token endpoint; a successful response without ``c_nonce`` gets one from the
        nonce endpoint."""
        r = self._client.post(f"{self.url}/openid4vci/token", data=form)
        if r.status_code != 200:
            return r
        doc = r.json()
        if "c_nonce" not in doc:
            nonce = self._client.post(f"{self.url}/openid4vci/nonce")
            nonce.raise_for_status()
            doc["c_nonce"] = nonce.json()["c_nonce"]
            doc.setdefault("c_nonce_expires_in", 300)
        return httpx.Response(200, json=doc, headers={"Cache-Control": "no-store"})

    def par(self, form: dict[str, str]) -> httpx.Response:
        """Issuer2's pushed authorization request endpoint, unchanged."""
        return self._client.post(f"{self.url}/openid4vci/par", data=form)

    def authorize(self, query: str) -> httpx.Response:
        """Issuer2's authorize endpoint; its redirect is returned, not followed."""
        return self._client.get(f"{self.url}/openid4vci/authorize?{query}", follow_redirects=False)

    def credential(self, body: dict[str, Any], authorization: str) -> httpx.Response:
        """Issuer2's credential endpoint; a draft-13 request is rewritten to 1.0 and the first
        credential of the answer is repeated as ``credential``."""
        draft13 = not body.get("credential_configuration_id") and not body.get(
            "credential_identifier"
        )
        if draft13:
            body = self._draft13_request(body)
        r = self._client.post(
            f"{self.url}/openid4vci/credential",
            json=body,
            headers={"Authorization": authorization},
        )
        if not draft13 or r.status_code != 200:
            return r
        doc = r.json()
        credentials = doc.get("credentials") or []
        if credentials:
            doc["credential"] = credentials[0].get("credential")
            doc["format"] = self._config_for(body["credential_configuration_id"]).get("format")
        return httpx.Response(200, json=doc)

    def _config_for(self, config_id: str) -> dict[str, Any]:
        configs = self.credential_issuer_metadata().get("credential_configurations_supported", {})
        return configs.get(config_id, {})

    def _draft13_request(self, body: dict[str, Any]) -> dict[str, Any]:
        configs = self.credential_issuer_metadata().get("credential_configurations_supported", {})
        config_id = next(
            (cid for cid, c in configs.items() if body.get("vct") and c.get("vct") == body["vct"]),
            next(iter(configs), ""),
        )
        out: dict[str, Any] = {"credential_configuration_id": config_id}
        proof = body.get("proof") or {}
        if proof.get("jwt"):
            out["proofs"] = {"jwt": [proof["jwt"]]}
        elif body.get("proofs"):
            out["proofs"] = body["proofs"]
        return out
