"""HTTP endpoints (see the contract table in docs/slice-1-onboarding.md)."""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from . import core
from .clients.authentik import AuthentikClient
from .clients.keycast import KeycastClient
from .clients.odoo import OdooClient, OdooError
from .core import Services
from .relay import RelayPublisher
from .relay_whitelist import RelayWhitelist
from .settings import Settings
from .stripe_sig import StripeSignatureError, verify_signature

log = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_services(settings: Settings) -> Services:
    return Services(
        odoo=OdooClient(
            settings.odoo_url, settings.odoo_db, settings.odoo_user, settings.odoo_api_key
        ),
        authentik=AuthentikClient(settings.authentik_url, settings.authentik_token),
        keycast=KeycastClient(
            settings.keycast_url,
            settings.keycast_admin_token,
            settings.keycast_service_token,
            tenant_domain=settings.keycast_tenant_domain,
        ),
        settings=settings,
        relay=RelayPublisher(settings.relay_url) if settings.relay_url else None,
        whitelist=(
            RelayWhitelist(
                settings.relay_config_template,
                settings.relay_config_path,
                settings.relay_extra_pubkeys,
            )
            if settings.relay_config_path
            else None
        ),
    )


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is None:
            settings = Settings()
            configure_logging(settings.log_level)
            app.state.services = build_services(settings)
        else:
            app.state.services = services
        # Bring the relay whitelist up to date with Odoo on every start (best effort).
        await run_in_threadpool(core.sync_relay_whitelist, app.state.services)
        yield

    app = FastAPI(title="Belfry onboarding", version="0.1.0", lifespan=lifespan)

    def svc(request: Request) -> Services:
        return request.app.state.services

    def check_token(request: Request, token: str | None) -> None:
        expected = svc(request).settings.hook_token
        if not token or not expected or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="invalid token")

    async def read_record_id(request: Request) -> int | None:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            log.error("odoo hook: body is not JSON")
            return None
        if not isinstance(payload, dict):
            return None
        rec_id = payload.get("_id", payload.get("id"))
        try:
            return int(rec_id)
        except (TypeError, ValueError):
            log.error("odoo hook: payload has no usable _id: %r", payload)
            return None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/hooks/odoo/invoice-paid")
    async def invoice_paid(
        request: Request, token: str | None = Query(default=None)
    ) -> dict[str, Any]:
        check_token(request, token)
        invoice_id = await read_record_id(request)
        if invoice_id is None:
            return {"result": "ignored", "error": "missing _id"}
        try:
            result = await run_in_threadpool(core.activate_pass, svc(request), invoice_id)
        except Exception as exc:  # Odoo does not retry: log, answer 200, reconcile picks it up
            log.exception("invoice-paid hook failed for invoice %s", invoice_id)
            return {"result": "error", "invoice_id": invoice_id, "error": str(exc)}
        return {"result": result, "invoice_id": invoice_id}

    @app.post("/hooks/odoo/refund")
    async def refund(request: Request, token: str | None = Query(default=None)) -> dict[str, Any]:
        check_token(request, token)
        transaction_id = await read_record_id(request)
        if transaction_id is None:
            return {"result": "ignored", "error": "missing _id"}
        try:
            result = await run_in_threadpool(core.revoke_for_refund, svc(request), transaction_id)
        except Exception as exc:
            log.exception("refund hook failed for transaction %s", transaction_id)
            return {"result": "error", "transaction_id": transaction_id, "error": str(exc)}
        return {"result": result, "transaction_id": transaction_id}

    @app.post("/admin/reconcile")
    async def admin_reconcile(
        request: Request, token: str | None = Query(default=None)
    ) -> dict[str, Any]:
        check_token(request, token)
        return await run_in_threadpool(core.reconcile, svc(request))

    @app.post("/admin/sync-relay")
    async def admin_sync_relay(
        request: Request, token: str | None = Query(default=None)
    ) -> dict[str, Any]:
        check_token(request, token)
        if svc(request).whitelist is None:
            return {"result": "disabled"}
        result = await run_in_threadpool(core.sync_relay_whitelist, svc(request), [], True)
        return {"result": "synced", **result}

    @app.post("/hooks/stripe")
    async def stripe_hook(request: Request) -> dict[str, Any]:
        services = svc(request)
        body = await request.body()
        try:
            verify_signature(
                body,
                request.headers.get("stripe-signature"),
                services.settings.stripe_webhook_secret,
            )
        except StripeSignatureError as exc:
            log.warning("stripe hook: %s", exc)
            raise HTTPException(status_code=400, detail=f"invalid signature: {exc}") from exc
        try:
            event = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="body is not JSON") from exc
        event_id = event.get("id")
        if not isinstance(event, dict) or not event_id:
            raise HTTPException(status_code=400, detail="not a Stripe event")
        return await run_in_threadpool(handle_stripe_event, services, event)

    return app


def handle_stripe_event(services: Services, event: dict[str, Any]) -> dict[str, Any]:
    """Record the event in belfry.stripe.event, then act. Replays answer ``duplicate``."""
    odoo = services.odoo
    event_id = event["id"]
    event_type = event.get("type", "")
    existing = odoo.search_read(
        "belfry.stripe.event", [["event_id", "=", event_id]], ["id", "result"], limit=1
    )
    if existing and not str(existing[0].get("result") or "").startswith("error"):
        return {"result": "duplicate", "event_id": event_id, "previous": existing[0].get("result")}
    if existing:  # a previous delivery failed while handling: Stripe retried, process it again
        record_id = existing[0]["id"]
        odoo.write("belfry.stripe.event", [record_id], {"result": "received"})
    else:
        try:
            record_id = odoo.create(
                "belfry.stripe.event",
                {
                    "event_id": event_id,
                    "type": event_type,
                    "received_at": core.now_utc_str(),
                    "result": "received",
                },
            )
        except OdooError as exc:
            if exc.is_constraint_violation:  # concurrent delivery lost the race
                return {"result": "duplicate", "event_id": event_id}
            raise

    obj = (event.get("data") or {}).get("object") or {}
    pass_id: int | None = None
    try:
        if event_type == "charge.dispute.created":
            result, pass_id = core.handle_dispute_created(services, obj)
        elif event_type == "charge.dispute.closed":
            result, pass_id = core.handle_dispute_closed(services, obj)
        else:
            result = "ignored"
    except Exception as exc:
        log.exception("stripe hook: handling %s %s failed", event_type, event_id)
        result = f"error: {exc}"[:200]
        odoo.write("belfry.stripe.event", [record_id], {"result": result})
        raise HTTPException(status_code=500, detail="event handling failed") from exc

    vals: dict[str, Any] = {"result": result}
    if pass_id is not None:
        vals["pass_id"] = pass_id
    odoo.write("belfry.stripe.event", [record_id], vals)
    return {"result": result, "event_id": event_id, "pass_id": pass_id}


app = create_app()
