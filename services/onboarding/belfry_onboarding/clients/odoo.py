"""Odoo JSON-RPC client (``/jsonrpc``, service ``object``, method ``execute_kw``).

Authentication: ``common.authenticate`` once with the API key as password, the uid is cached.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class OdooError(Exception):
    """An error returned by Odoo over JSON-RPC."""

    def __init__(self, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.data = data or {}

    @property
    def is_missing_method(self) -> bool:
        """True when Odoo reports that a model has no such method (older module version)."""
        text = f"{self.message} {self.data.get('message', '')} {self.data.get('name', '')}"
        return "AttributeError" in text or "has no attribute" in text or "Invalid method" in text

    @property
    def is_constraint_violation(self) -> bool:
        text = f"{self.message} {self.data.get('message', '')} {self.data.get('name', '')}"
        return (
            "IntegrityError" in text
            or "UniqueViolation" in text
            or "duplicate key" in text
            or "already exists" in text
        )


class OdooApi:
    """Helpers built on ``execute_kw``. Subclasses (and test fakes) implement ``execute_kw``."""

    def execute_kw(
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None
    ) -> Any:
        raise NotImplementedError

    def call(
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None
    ) -> Any:
        return self.execute_kw(model, method, args, kwargs or {})

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields}
        if limit is not None:
            kwargs["limit"] = limit
        if order is not None:
            kwargs["order"] = order
        return self.execute_kw(model, "search_read", [domain], kwargs)

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        return self.execute_kw(model, "read", [ids], {"fields": fields})

    def read_one(self, model: str, rec_id: int, fields: list[str]) -> dict[str, Any] | None:
        rows = self.search_read(model, [["id", "=", rec_id]], fields, limit=1)
        return rows[0] if rows else None

    def write(self, model: str, ids: list[int], vals: dict[str, Any]) -> bool:
        return self.execute_kw(model, "write", [ids, vals], {})

    def create(self, model: str, vals: dict[str, Any]) -> int:
        result = self.execute_kw(model, "create", [vals], {})
        if isinstance(result, list):
            return result[0]
        return result


class OdooClient(OdooApi):
    def __init__(
        self,
        url: str,
        db: str,
        user: str,
        api_key: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)
        self._uid: int | None = None
        self._ids = itertools.count(1)

    def _rpc(self, service: str, method: str, args: list[Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": next(self._ids),
        }
        response = self._client.post(f"{self.url}/jsonrpc", json=payload)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            err = body["error"]
            data = err.get("data") or {}
            message = data.get("message") or err.get("message") or "Odoo RPC error"
            raise OdooError(message, data)
        return body.get("result")

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self._rpc("common", "authenticate", [self.db, self.user, self.api_key, {}])
            if not uid:
                raise OdooError(f"Odoo authentication failed for {self.user!r} on db {self.db!r}")
            self._uid = int(uid)
        return self._uid

    def execute_kw(
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None
    ) -> Any:
        return self._rpc(
            "object",
            "execute_kw",
            [self.db, self.uid, self.api_key, model, method, args, kwargs or {}],
        )
