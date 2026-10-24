"""Reports of content in the community apps (directory, marketplace, photos).

The village server (services/village) checks the reporter's NIP-98 signature and forwards the
report to ``POST /admin/reports`` with the pass-info bearer token. The report is stored in Odoo
(``belfry.report``) and mailed to every member of the stewards group in Authentik; stewards
handle it on the steward pages (steward.py). ``GET /admin/moderation/hidden`` lists what the
apps must not show: the references of reports whose content a steward hid.

Reporters need an active pass. Only stewards see reports and who sent them.
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import core
from .core import Services

log = logging.getLogger(__name__)

APPS = {"people", "market", "photos"}
KINDS = {"profile", "listing", "photo", "comment"}
REASONS = {"spam", "harassment", "illegal", "scam", "other"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
# an event id, a pubkey, or an addressable coordinate <kind>:<pubkey>:<d>
REF = re.compile(r"^([0-9a-f]{64}|\d{1,5}:[0-9a-f]{64}:[^\s]{1,200})$")


class ReportError(Exception):
    pass


def validate(body: dict[str, Any]) -> dict[str, Any]:
    """The fields Odoo stores, checked; ReportError with a short reason otherwise."""

    def text(key: str, limit: int) -> str:
        value = body.get(key) or ""
        if not isinstance(value, str):
            raise ReportError(f"{key} must be text")
        return value.strip()[:limit]

    vals = {
        "reporter_pubkey": text("reporter_pubkey", 64).lower(),
        "app": text("app", 10),
        "target_kind": text("target_kind", 10),
        "target_ref": text("target_ref", 300),
        "target_pubkey": text("target_pubkey", 64).lower(),
        "reason": text("reason", 20),
        "note": text("note", 2000),
        "target_url": text("target_url", 500),
        "target_excerpt": text("target_excerpt", 300),
    }
    if not HEX64.match(vals["reporter_pubkey"]) or not HEX64.match(vals["target_pubkey"]):
        raise ReportError("reporter_pubkey and target_pubkey are 64 hex characters")
    if vals["app"] not in APPS or vals["target_kind"] not in KINDS:
        raise ReportError("unknown app or target_kind")
    if vals["reason"] not in REASONS:
        raise ReportError("unknown reason")
    if not REF.match(vals["target_ref"]):
        raise ReportError("target_ref is an event id, a pubkey or a kind:pubkey:d coordinate")
    if vals["target_url"] and not vals["target_url"].startswith("https://"):
        if not vals["target_url"].startswith("http://localhost"):
            vals["target_url"] = ""
    if vals["reporter_pubkey"] == vals["target_pubkey"]:
        raise ReportError("you cannot report your own content")
    return {k: v for k, v in vals.items() if v}


def create_report(svc: Services, body: dict[str, Any]) -> int:
    vals = validate(body)
    if vals["reporter_pubkey"] not in set(core.active_pubkeys(svc)):
        raise PermissionError("the reporter has no active pass")
    try:
        stewards = svc.authentik.group_member_emails(svc.settings.steward_group)
    except Exception:  # noqa: BLE001 - the report counts even when the mail cannot be addressed
        log.exception("reports: could not list the stewards' e-mail addresses")
        stewards = []
    report_id = svc.odoo.call("belfry.report", "belfry_report_create", [vals, stewards])
    log.info(
        "report %s: %s %s, mailed to %d stewards",
        report_id,
        vals["app"],
        vals["reason"],
        len(stewards),
    )
    return int(report_id)


def hidden_refs(svc: Services) -> list[str]:
    return list(svc.odoo.call("belfry.report", "belfry_hidden_refs", []))


def register(app: FastAPI) -> None:
    """Mount /admin/reports and /admin/moderation/hidden (create_app calls this). Both take the
    pass-info bearer token; the proxy keeps /admin/* off the internet."""

    def guard(request: Request) -> Services:
        services: Services = request.app.state.services
        expected = services.settings.pass_info_token
        if not expected:
            raise HTTPException(status_code=503, detail="pass info token not configured")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, expected):
            raise HTTPException(status_code=401, detail="invalid token")
        return services

    @app.post("/admin/reports")
    async def admin_create_report(request: Request) -> dict[str, Any]:
        services = guard(request)
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="body is not JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body is not an object")
        try:
            report_id = await run_in_threadpool(create_report, services, body)
        except ReportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"id": report_id}

    @app.get("/admin/moderation/hidden")
    async def admin_hidden(request: Request) -> dict[str, Any]:
        services = guard(request)
        return {"hidden": await run_in_threadpool(hidden_refs, services)}
