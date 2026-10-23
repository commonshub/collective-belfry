"""walt.id Verifier2 session management API (reachable only inside the Compose network)."""

from __future__ import annotations

from typing import Any

import httpx

VP_POLICIES_SD_JWT = [
    "dc+sd-jwt/audience-check",
    "dc+sd-jwt/kb-jwt_signature",
    "dc+sd-jwt/nonce-check",
    "dc+sd-jwt/sd_hash-check",
]


def village_pass_query(vct: str, claims: list[str]) -> dict[str, Any]:
    """DCQL query for one village pass disclosing exactly ``claims``."""
    return {
        "credentials": [
            {
                "id": "village_pass",
                "format": "dc+sd-jwt",
                "meta": {"vct_values": [vct]},
                "claims": [{"path": [c]} for c in claims],
            }
        ]
    }


def village_pass_policies(issuer: str, check_status: bool = True) -> list[Any]:
    """Credential policies for a village pass: Belfry's issuer signature, validity window,
    issuer identifier, and (fail closed) the Token Status List bit."""
    policies: list[Any] = [
        "signature",
        "expiration",
        "not-before",
        {"policy": "allowed-issuer", "allowed_issuer": issuer},
    ]
    if check_status:
        policies.append(
            {"policy": "credential-status", "argument": {"discriminator": "ietf", "value": 0}}
        )
    return policies


class VerifierClient:
    def __init__(self, url: str, client: httpx.Client | None = None, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    def create_session(self, dcql_query: dict[str, Any], vc_policies: list[Any]) -> dict[str, Any]:
        """Cross-device OpenID4VP session. Returns ``sessionId`` and
        ``bootstrapAuthorizationRequestUrl`` (the ``openid4vp://`` URL for the QR code)."""
        body = {
            "flow_type": "cross_device",
            "core_flow": {
                "dcql_query": dcql_query,
                "policies": {
                    "vp_policies": {"dc+sd-jwt": VP_POLICIES_SD_JWT},
                    "vc_policies": vc_policies,
                },
            },
        }
        r = self._client.post(f"{self.url}/verification-session/create", json=body)
        r.raise_for_status()
        return r.json()

    def session_info(self, session_id: str) -> dict[str, Any]:
        r = self._client.get(f"{self.url}/verification-session/{session_id}/info")
        r.raise_for_status()
        return r.json()
