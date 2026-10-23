"""HTTP endpoints (see the contract table in docs/slice-1-onboarding.md)."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import blossom, bridge, claim, core, steward
from .clients.authentik import AuthentikClient
from .clients.issuer import IssuerClient
from .clients.keycast import KeycastClient
from .clients.odoo import OdooClient, OdooError
from .clients.verifier import VerifierClient
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


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text()) if path else None


def build_services(settings: Settings) -> Services:
    return Services(
        issuer=IssuerClient(settings.issuer_url) if settings.issuer_url else None,
        verifier=VerifierClient(settings.verifier_url) if settings.verifier_url else None,
        issuer_jwk=_load_json(settings.issuer_jwk_path) or {},
        issuer_x5c=_load_json(settings.issuer_x5c_path) or [],
        bridge_jwk=_load_json(settings.bridge_jwk_path) or {},
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
        # Bring the relay whitelist and the Authentik participants group up to date with Odoo
        # on every start (best effort).
        await run_in_threadpool(core.sync_relay_whitelist, app.state.services)
        await run_in_threadpool(core.sync_participants, app.state.services)
        yield

    app = FastAPI(title="Belfry onboarding", version="0.2.0", lifespan=lifespan)
    app.state.bridge = bridge.BridgeStore()
    steward.register(app)  # /steward: the steward pages (steward.py)
    # The repo's brand/ folder (Open Source Village logos, Geist fonts, tokens.css) at /brand:
    # read by the pages here, authentik's brand blueprint, the issuer metadata and wallets.
    # BRAND_DIR in Compose (../brand mounted at /brand); a local checkout finds the folder itself.
    brand_dir = Path(os.environ.get("BRAND_DIR") or Path(__file__).resolve().parents[3] / "brand")
    if brand_dir.is_dir():
        mimetypes.add_type("font/woff2", ".woff2")  # python-slim knows no woff2
        app.mount("/brand", StaticFiles(directory=str(brand_dir)), name="brand")

    @app.middleware("http")
    async def brand_cors(request: Request, call_next):
        """Authentik (login.<domain>) and Odoo (odoo.<domain>) load the fonts from here; font
        files are CORS-checked, so /brand answers every origin and may be cached for a day."""
        response = await call_next(request)
        if request.url.path.startswith("/brand/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response

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

    @app.post("/admin/passes/{pass_id}/login-link")
    async def admin_login_link(
        pass_id: int, request: Request, token: str | None = Query(default=None)
    ) -> dict[str, Any]:
        """A new one-time sign-in link for an active pass (Odoo's "Send new login link" button).
        Odoo mails it when the pass is written. Errors from Authentik or Odoo are 500."""
        check_token(request, token)
        result = await run_in_threadpool(core.send_login_link, svc(request), pass_id)
        return {"result": result, "pass_id": pass_id}

    @app.post("/admin/passes/{pass_id}/revoke")
    async def admin_revoke(
        pass_id: int, request: Request, token: str | None = Query(default=None)
    ) -> dict[str, Any]:
        """Steward decision (Odoo's "Revoke pass" button): relay, signer, Authentik group and
        the status list bit (served live from Odoo's state) all follow."""
        check_token(request, token)
        result = await run_in_threadpool(core.revoke_pass, svc(request), pass_id, "steward")
        return {"result": result, "pass_id": pass_id}

    @app.get("/admin/partners/{partner_id}/pass")
    async def admin_partner_pass(partner_id: int, request: Request) -> dict[str, Any]:
        """The partner's pass for the community apps (Keycast's "Your pass" card), server to
        server with PASS_INFO_TOKEN as bearer token; read only, unlike the hook token."""
        expected = svc(request).settings.pass_info_token
        if not expected:
            raise HTTPException(status_code=503, detail="pass info not configured")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, expected):
            raise HTTPException(status_code=401, detail="invalid token")
        info = await run_in_threadpool(claim.pass_info, svc(request), partner_id)
        if info is None:
            raise HTTPException(status_code=404, detail="no pass")
        return info

    # -- Phase 3: claim page, status list, type metadata, door ------------------------------------

    def claim_error(exc: claim.ClaimError) -> HTMLResponse:
        body = (
            f'<div class="card"><h1>Pass not available</h1><p class="bad">{exc.message}</p></div>'
        )
        return HTMLResponse(claim.page("Pass not available", body), status_code=exc.status)

    @app.get("/claim/{token}", response_class=HTMLResponse)
    async def claim_page(token: str, request: Request):
        """The participant's claim page: a fresh credential offer as QR code and link."""
        services = svc(request)
        try:
            pass_row, partner = await run_in_threadpool(claim.pass_for_claim, services, token)
            offer = await run_in_threadpool(claim.create_claim_offer, services, pass_row, partner)
        except claim.ClaimError as exc:
            return claim_error(exc)
        return HTMLResponse(claim.claim_page(services, pass_row, partner, offer))

    @app.get("/claim/{token}/offer")
    async def claim_offer(token: str, request: Request) -> JSONResponse:
        """The same offer as JSON (for wallets and the smoke test)."""
        services = svc(request)
        try:
            pass_row, partner = await run_in_threadpool(claim.pass_for_claim, services, token)
            offer = await run_in_threadpool(claim.create_claim_offer, services, pass_row, partner)
        except claim.ClaimError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        return JSONResponse(
            {
                "credential_offer": offer["credentialOffer"],
                "offer_id": offer.get("offerId"),
                "expires_at": offer.get("expiresAt"),
                "pass_id": offer["pass_id"],
                "tier": offer["tier"],
                "date_start": offer["date_start"],
                "date_end": offer["date_end"],
            }
        )

    @app.get("/status/1")
    async def status_list(request: Request) -> Response:
        """Token Status List of every pass certificate (bit = pass id): 1 for revoked and
        expired passes. Derived from Odoo on every request, cacheable for a few minutes."""
        services = svc(request)
        try:
            token = await run_in_threadpool(claim.status_list_jwt, services)
        except claim.ClaimError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        return Response(
            token,
            media_type="application/statuslist+jwt",
            headers={"Cache-Control": f"max-age={services.settings.status_list_ttl}"},
        )

    @app.get("/vct/village-pass/v1")
    # walt.id's wallet looks the type up at <authority>/.well-known/vct<path of the vct>
    # (an earlier SD-JWT VC draft), not at the vct URL itself.
    @app.get("/.well-known/vct/vct/village-pass/v1")
    async def vct(request: Request) -> JSONResponse:
        return JSONResponse(claim.type_metadata(svc(request)))

    @app.get("/door", response_class=HTMLResponse)
    async def door(request: Request):
        try:
            session = await run_in_threadpool(claim.door_session, svc(request))
        except claim.ClaimError as exc:
            return claim_error(exc)
        return HTMLResponse(claim.door_page(session))

    @app.post("/door/sessions")
    async def door_create(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool(claim.door_session, svc(request)))
        except claim.ClaimError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.get("/door/sessions/{session_id}")
    async def door_poll(session_id: str, request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                await run_in_threadpool(claim.door_result, svc(request), session_id)
            )
        except claim.ClaimError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)

    # -- Phase 3: OpenID4VP-to-OIDC bridge for Authentik ------------------------------------------

    def store(request: Request) -> bridge.BridgeStore:
        return request.app.state.bridge

    def bridge_json_error(exc: bridge.BridgeError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description}, status_code=exc.status
        )

    @app.get("/bridge/.well-known/openid-configuration")
    async def bridge_discovery(request: Request) -> JSONResponse:
        return JSONResponse(bridge.discovery(svc(request).settings.public_url))

    @app.get("/bridge/jwks")
    async def bridge_jwks(request: Request) -> JSONResponse:
        key = svc(request).bridge_jwk
        return JSONResponse({"keys": [bridge.public_jwk(key)] if key else []})

    @app.get("/bridge/authorize", response_class=HTMLResponse)
    async def bridge_authorize(request: Request):
        params = dict(request.query_params)
        try:
            auth = await run_in_threadpool(
                bridge.start_authorization, svc(request), store(request), params
            )
        except bridge.BridgeError as exc:
            if exc.error in ("invalid_client", "invalid_request") or not params.get("redirect_uri"):
                return HTMLResponse(bridge.error_page(exc), status_code=exc.status)
            sep = "&" if "?" in params["redirect_uri"] else "?"
            return RedirectResponse(
                f"{params['redirect_uri']}{sep}error={exc.error}"
                + (f"&state={params['state']}" if params.get("state") else ""),
                status_code=302,
            )
        return HTMLResponse(bridge.authorize_page(auth))

    @app.get("/bridge/poll/{sid}")
    async def bridge_poll(sid: str, request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                await run_in_threadpool(bridge.poll, svc(request), store(request), sid)
            )
        except bridge.BridgeError as exc:
            return bridge_json_error(exc)

    @app.post("/bridge/token")
    async def bridge_token(request: Request) -> JSONResponse:
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            tokens = await run_in_threadpool(
                bridge.exchange_code,
                svc(request),
                store(request),
                form,
                request.headers.get("authorization"),
            )
        except bridge.BridgeError as exc:
            return bridge_json_error(exc)
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

    @app.get("/bridge/userinfo")
    async def bridge_userinfo(request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                bridge.userinfo(store(request), request.headers.get("authorization"))
            )
        except bridge.BridgeError as exc:
            return bridge_json_error(exc)

    @app.api_route("/blossom/auth", methods=["GET", "HEAD", "PUT", "DELETE"])
    async def blossom_auth(request: Request) -> Response:
        """Forward-auth target of the Blossom gate: 200 when the Authorization: Nostr event is a
        valid BUD-11 token for the original method and its pubkey holds an active pass."""
        method = request.headers.get("x-forwarded-method", request.method)
        try:
            pubkey = await run_in_threadpool(
                blossom.authorize, svc(request), request.headers.get("authorization"), method
            )
        except blossom.BlossomAuthError as exc:
            return JSONResponse({"error": exc.reason}, status_code=exc.status)
        return Response(status_code=200, headers={"X-Belfry-Pubkey": pubkey})

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
