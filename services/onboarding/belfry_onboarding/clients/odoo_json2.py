"""Odoo's JSON-2 API (``POST /json/2/<model>/<method>``, Odoo 19 and Odoo Online), for the Odoo
of the organisation that invoices the tickets (accounting.py).

Named arguments only: ``ids`` and ``context`` for the records, the method's own parameters by
name. Authentication: a user's API key as a bearer token, the database in ``X-Odoo-Database``.
JSON-RPC (``/jsonrpc``, clients/odoo.py) is deprecated from Odoo 19 on, and Odoo Online moves
first, so the remote side speaks JSON-2 from the start.
"""

from __future__ import annotations

from typing import Any

import httpx

from .odoo import OdooError


class Json2Client:
    def __init__(
        self,
        url: str,
        db: str,
        api_key: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        self.url = url.rstrip("/")
        self.db = db
        self.api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        context: dict[str, Any] | None = None,
        **params: Any,
    ) -> Any:
        body: dict[str, Any] = dict(params)
        if ids:
            body["ids"] = ids
        if context:
            body["context"] = context
        headers = {"Authorization": f"bearer {self.api_key}"}
        if self.db:
            headers["X-Odoo-Database"] = self.db
        response = self._client.post(
            f"{self.url}/json/2/{model}/{method}", json=body, headers=headers
        )
        if response.is_success:
            return response.json()
        try:
            err = response.json()
        except ValueError:
            err = {"message": response.text[:300]}
        message = err.get("message") or f"HTTP {response.status_code}"
        raise OdooError(f"{model}.{method}: {message}", {"status": response.status_code, **err})

    def search_read(
        self, model: str, domain: list[Any], fields: list[str], limit: int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"domain": domain, "fields": fields}
        if limit is not None:
            params["limit"] = limit
        return self.call(model, "search_read", **params)

    def create(
        self, model: str, vals: dict[str, Any], context: dict[str, Any] | None = None
    ) -> int:
        return self.call(model, "create", context=context, vals_list=[vals])[0]

    def read_one(self, model: str, rec_id: int, fields: list[str]) -> dict[str, Any] | None:
        rows = self.call(model, "read", ids=[rec_id], fields=fields)
        return rows[0] if rows else None
