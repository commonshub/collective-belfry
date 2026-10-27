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
    def __init__(
        self,
        url: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        verifier_info: list[dict[str, Any]] | None = None,
    ):
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        # OpenID4VP verifier_info of every request: the EUDI registration certificate, bound to
        # the access certificate that signs the request objects (eudi.py)
        self.verifier_info = verifier_info

    def create_session(
        self, dcql_query: dict[str, Any], vc_policies: list[Any], signed_request: bool = True
    ) -> dict[str, Any]:
        """Cross-device OpenID4VP session. Returns ``sessionId`` and
        ``bootstrapAuthorizationRequestUrl`` (the ``openid4vp://`` URL for the QR code).
        ``signed_request``: the request object is a JWT signed with the verifier's configured
        key and x5c (client id ``x509_san_dns:...`` under Belfry's CA, or ``x509_hash:...`` of
        the EUDI registrar's access certificate), so wallets can authenticate the verifier."""
        core: dict[str, Any] = {
            "dcql_query": dcql_query,
            "signed_request": signed_request,
            "policies": {
                "vp_policies": {"dc+sd-jwt": VP_POLICIES_SD_JWT},
                "vc_policies": vc_policies,
            },
        }
        if self.verifier_info:
            core["verifier_info"] = self.verifier_info
        body = {"flow_type": "cross_device", "core_flow": core}
        r = self._client.post(f"{self.url}/verification-session/create", json=body)
        r.raise_for_status()
        return r.json()

    def session_info(self, session_id: str) -> dict[str, Any]:
        r = self._client.get(f"{self.url}/verification-session/{session_id}/info")
        r.raise_for_status()
        return r.json()
