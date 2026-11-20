"""NIP-05 names: ``<member number>@<domain>`` (lower case) for every active pass, from Odoo
(Keycast served these until 2026-11-19). ``GET /.well-known/nostr.json?name=osv-0042`` on the
apex (the proxy sends it here) answers that one name with the member's key and the community
relay; without a name, or for an unknown one, the list is empty, so the members cannot be
listed. Cached for a minute; a revoked pass drops out with the cache.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id

CACHE_SECONDS = 60
PATH = "/.well-known/nostr.json"


class Names:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.loaded = 0.0
        self.lock = threading.Lock()

    def get(self, svc: Services) -> dict[str, str]:
        with self.lock:
            if time.time() - self.loaded > CACHE_SECONDS:
                self.names = load(svc)
                self.loaded = time.time()
            return self.names


def load(svc: Services) -> dict[str, str]:
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["nostr_pubkey", "!=", False]],
        ["partner_id", "nostr_pubkey"],
    )
    by_partner = {
        m2o_id(r.get("partner_id")): r["nostr_pubkey"] for r in rows if m2o_id(r.get("partner_id"))
    }
    if not by_partner:
        return {}
    partners = svc.odoo.read("res.partner", sorted(by_partner), ["belfry_member_number"])
    return {
        p["belfry_member_number"].lower(): by_partner[p["id"]]
        for p in partners
        if p.get("belfry_member_number")
    }


def answer(svc: Services, names: dict[str, str], name: str) -> dict[str, Any]:
    pubkey = names.get((name or "").strip().lower())
    if not pubkey:
        return {"names": {}}
    out: dict[str, Any] = {"names": {name.strip().lower(): pubkey}}
    relay = svc.settings.community_relay_public_url or svc.settings.relay_url
    if relay:
        out["relays"] = {pubkey: [relay]}
    return out


def register(app: FastAPI) -> None:
    app.state.nip05 = Names()

    @app.get(PATH)
    async def nostr_json(request: Request, name: str = "") -> JSONResponse:
        services: Services = request.app.state.services
        names = await run_in_threadpool(request.app.state.nip05.get, services) if name else {}
        return JSONResponse(
            answer(services, names, name),
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"},
        )
