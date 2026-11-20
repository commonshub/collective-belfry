"""Client of Belfry's signer (``services/signer``), the only holder of the members' keys.

Internal network only, bearer token ``SIGNER_TOKEN``. Accounts are keyed by
``vine_id = odoo-partner-<partner id>``; the signer derives the key from the custody seed
(same formula as Keycast's patch 0014, so npubs did not change with the move). See
``services/signer/README.md`` for the API.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


def vine_id(partner_id: int) -> str:
    return f"odoo-partner-{int(partner_id)}"


class SignerError(Exception):
    """An answer of the signer other than 2xx: ``status``, ``code`` (``not_found``,
    ``seed_locked``, ``own_key``, ``suspended``, ``forbidden``, ``mismatch``, ``invalid``)."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"signer {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class SignerClient:
    def __init__(
        self, url: str, token: str, client: httpx.Client | None = None, timeout: float = 30.0
    ):
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        r = self._client.request(method, f"{self.url}{path}", json=body, headers=self._headers)
        if r.status_code >= 400:
            try:
                data = r.json()
            except ValueError:
                data = {}
            raise SignerError(
                r.status_code, str(data.get("error") or "error"), str(data.get("message") or "")
            )
        return r.json()

    # -- accounts -------------------------------------------------------------------------------

    def ensure_account(
        self,
        vine_id: str,
        key_version: int = 0,
        own_pubkey: str | None = None,
        expect_pubkey: str | None = None,
    ) -> dict[str, Any]:
        """Creates the account if missing (idempotent): the derived key at ``key_version``, or
        none with ``own_pubkey`` (an existing account moves to it, revoking its connections).
        ``expect_pubkey``: refuse (409 mismatch) unless the derived key is that one."""
        body: dict[str, Any] = {"key_version": int(key_version or 0)}
        if own_pubkey:
            body["own_pubkey"] = own_pubkey
        if expect_pubkey:
            body["expect_pubkey"] = expect_pubkey
        return self._call("PUT", f"/accounts/{vine_id}", body)

    def account(self, vine_id: str) -> dict[str, Any] | None:
        try:
            return self._call("GET", f"/accounts/{vine_id}")
        except SignerError as exc:
            if exc.status == 404:
                return None
            raise

    def set_status(self, vine_id: str, status: str) -> None:
        """``active`` or ``suspended`` (suspended: nothing is signed, also not over NIP-46)."""
        self._call("POST", f"/accounts/{vine_id}/status", {"status": status})

    def sign(
        self, vine_id: str, event: dict[str, Any], connection: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"event": event}
        if connection:
            body["connection"] = connection
        return self._call("POST", f"/accounts/{vine_id}/sign", body)["event"]

    def crypt(
        self,
        vine_id: str,
        op: str,
        scheme: str,
        peer: str,
        text: str,
        connection: str | None = None,
    ) -> str:
        """``op``: encrypt or decrypt; ``scheme``: nip44 or nip04."""
        if op not in ("encrypt", "decrypt"):
            raise ValueError(op)
        body: dict[str, Any] = {"scheme": scheme, "peer": peer, "text": text}
        if connection:
            body["connection"] = connection
        return self._call("POST", f"/accounts/{vine_id}/{op}", body)["result"]

    def derive(self, vine_id: str, purpose: str, connection: str | None = None) -> str:
        body: dict[str, Any] = {"purpose": purpose}
        if connection:
            body["connection"] = connection
        return self._call("POST", f"/accounts/{vine_id}/derive", body)["secret"]

    def export(self, vine_id: str) -> str:
        return self._call("POST", f"/accounts/{vine_id}/export")["nsec"]

    # -- bunker connections ---------------------------------------------------------------------

    def create_connection(
        self,
        vine_id: str,
        app: str,
        label: str = "",
        derive: list[str] | None = None,
        keep: int | None = None,
    ) -> dict[str, Any]:
        """``{id, bunker_url, bunker_pubkey, pubkey}``; the URL's secret is shown only now."""
        body: dict[str, Any] = {"app": app, "label": label or app, "derive": derive or []}
        if keep:
            body["keep"] = keep
        return self._call("POST", f"/accounts/{vine_id}/connections", body)

    def connections(self, vine_id: str) -> list[dict[str, Any]]:
        return list(self._call("GET", f"/accounts/{vine_id}/connections"))

    def revoke(self, vine_id: str, connection: str | None = None, app: str | None = None) -> int:
        body: dict[str, Any] = {}
        if connection:
            body["id"] = connection
        if app:
            body["app"] = app
        return int(self._call("POST", f"/accounts/{vine_id}/connections/revoke", body)["revoked"])

    # -- migration and health -------------------------------------------------------------------

    def import_sealed(self, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Keys sealed by Keycast under the same master key: ``[{vine_id, pubkey, sealed}]``."""
        return list(self._call("POST", "/import", {"accounts": rows})["results"])

    def health(self) -> dict[str, Any]:
        r = self._client.get(f"{self.url}/health")
        r.raise_for_status()
        return r.json()
