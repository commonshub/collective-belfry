"""Card payments for selling passes (sales.py) through Stripe Checkout, the community's own
Stripe account, over Stripe's HTTP API with a restricted key (no SDK).

- A session is made from the posted invoice: its gross amount, the invoice number in
  ``client_reference_id`` and in the metadata with the order id. Stripe keeps a session 24 hours;
  ``GET /checkout/<order>`` makes a new one when it expired (a new idempotency key per attempt).
- ``belfry.<domain>/hooks/stripe`` (``checkout.session.completed`` and
  ``checkout.session.async_payment_succeeded``): signature checked (stripe_sig.py) with the
  console's webhook secret, deduplicated on the event id, the amount, currency and invoice
  checked against the order, then ``sales.mark_paid`` (payment in Odoo, pass active). Idempotent
  on the event id and on the order.

The restricted key needs: Checkout Sessions write. Never logged, never in an answer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services
from .stripe_sig import StripeSignatureError, verify_signature

log = logging.getLogger(__name__)

API = "https://api.stripe.com/v1"
EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
HTTP = httpx.Client(timeout=20.0)


def _post(svc: Services, path: str, data: dict[str, Any] | None = None, idem: str = "") -> dict:
    from . import sales

    headers = {"Authorization": f"Bearer {sales.secret(svc, 'stripe_key')}"}
    if idem:
        headers["Idempotency-Key"] = idem
    r = HTTP.post(API + path, data=data or {}, headers=headers)
    if 400 <= r.status_code < 500 and r.status_code not in (408, 409, 429):
        try:
            code = (r.json().get("error") or {}).get("code") or r.status_code
        except ValueError:
            code = r.status_code
        raise sales.Refused(f"Stripe refused: {code}")
    r.raise_for_status()
    return r.json()


def make_session(svc: Services, o: dict[str, Any]) -> None:
    """A Checkout session for the order's invoice. Stripe refusing leaves the order to be paid
    by transfer (flag card_failed); Stripe down leaves the step for the tick."""
    from . import sales

    st = sales.store(svc)
    inv = o.get("invoice") or {}
    if not inv.get("number") or not sales.stripe_ready(svc):
        st.update(o["id"], step="wait")
        return
    attempt = int((o.get("stripe") or {}).get("attempt") or 0) + 1
    back = o.get("return_url") or sales.thanks_url(svc)
    data = {
        "mode": "payment",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": sales.settings(svc)["currency"].lower(),
        "line_items[0][price_data][unit_amount]": str(o["total"]["gross"]),
        "line_items[0][price_data][product_data][name]": f"{o['product_name']} ({inv['number']})",
        "client_reference_id": inv["number"],
        "customer_email": o["email"],
        "metadata[belfry_order]": o["id"],
        "metadata[invoice]": inv["number"],
        "payment_intent_data[metadata][belfry_order]": o["id"],
        "payment_intent_data[metadata][invoice]": inv["number"],
        "success_url": sales._with_order(back, o["id"]),
        "cancel_url": sales._with_order(back, o["id"]),
    }
    try:
        got = _post(svc, "/checkout/sessions", data, f"belfry-{o['id']}-{attempt}")
    except sales.Refused as exc:
        flags = list(o.get("flags") or [])
        if "card_failed" not in flags:
            flags.append("card_failed")
        log.warning("sales: order %s: no card session: %s", o["id"], exc)
        st.update(o["id"], step="wait", flags=flags, error=str(exc)[:200])
        return
    st.update(
        o["id"],
        step="wait",
        stripe={
            "id": got["id"],
            "url": got.get("url"),
            "expires_at": got.get("expires_at"),
            "attempt": attempt,
        },
        error=None,
    )
    log.info("sales: order %s: card session made", o["id"])


def expire_session(svc: Services, o: dict[str, Any]) -> None:
    """An order that ended: its open session can't be paid any more (best effort)."""
    sid = (o.get("stripe") or {}).get("id")
    if not sid:
        return
    try:
        _post(svc, f"/checkout/sessions/{sid}/expire")
    except Exception as exc:  # noqa: BLE001 — paid or expired already; a late payment is flagged
        log.info("sales: order %s: session not expired: %s", o["id"], type(exc).__name__)


def handle(svc: Services, event: dict[str, Any]) -> str:
    """One verified Stripe event → a short result."""
    from . import sales

    if event.get("type") not in EVENTS:
        return "ignored"
    obj = (event.get("data") or {}).get("object") or {}
    oid = str((obj.get("metadata") or {}).get("belfry_order") or "")
    st = sales.store(svc)
    o = st.order(oid) if sales.ORDER_RE.match(oid) else None
    if o is None:
        return "unknown_order"
    if obj.get("payment_status") != "paid":
        return "not_paid"
    inv = (o.get("invoice") or {}).get("number")
    ok = (
        int(obj.get("amount_total") or -1) == int(o["total"]["gross"])
        and str(obj.get("currency") or "").lower() == sales.settings(svc)["currency"].lower()
        and (not inv or obj.get("client_reference_id") == inv)
    )
    if not ok:
        flags = list(o.get("flags") or [])
        if "stripe_mismatch" not in flags:
            flags.append("stripe_mismatch")
            st.update(oid, flags=flags)
        log.warning("sales: order %s: a Stripe payment that doesn't match it", oid)
        return "mismatch"
    sales.mark_paid(svc, oid, "card", str(obj.get("payment_intent") or obj.get("id") or ""))
    return "paid"


def register(app: FastAPI) -> None:
    from . import sales

    @app.post("/console/hooks/stripe", include_in_schema=False)
    async def stripe_hook(request: Request) -> JSONResponse:
        s: Services = request.app.state.services
        body = await request.body()
        try:
            verify_signature(
                body,
                request.headers.get("stripe-signature"),
                sales.secret(s, "stripe_webhook_secret"),
            )
        except StripeSignatureError as exc:
            log.warning("sales: stripe hook refused: %s", exc)
            return JSONResponse({"error": "invalid signature"}, status_code=400)
        try:
            event = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "not JSON"}, status_code=400)
        if not isinstance(event, dict) or not event.get("id"):
            return JSONResponse({"error": "not a Stripe event"}, status_code=400)
        st = sales.store(s)
        if not st.first_event(str(event["id"])):
            return JSONResponse({"result": "duplicate"})
        try:
            result = await run_in_threadpool(handle, s, event)
        except Exception:  # noqa: BLE001 — Stripe delivers again; handle it then
            st.forget_event(str(event["id"]))
            log.exception("sales: stripe event failed")
            return JSONResponse({"error": "failed"}, status_code=500)
        st.event_result(str(event["id"]), result)
        return JSONResponse({"result": result})
