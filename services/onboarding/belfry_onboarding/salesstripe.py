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
import re
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


def _post(
    svc: Services,
    path: str,
    data: dict[str, Any] | None = None,
    idem: str = "",
    env: str = "live",
) -> dict:
    from . import sales

    headers = {"Authorization": f"Bearer {sales.secret(svc, 'stripe_key', env)}"}
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
    """A Checkout session for the order: from its invoice when it has one (the payment goes on
    that invoice), else from Belfry's own total, with the order id as the reference (the
    invoice is made once paid). Stripe refusing leaves the transfer (flag card_failed); Stripe
    down leaves the step for the tick."""
    from . import sales

    st = sales.store(svc)
    inv = o.get("invoice") or {}
    env = sales.env_of(o)
    if not sales.stripe_ready(svc, env):
        st.update(o["id"], step="wait")
        if inv.get("number"):
            sales.queue_invoice_mail(svc, st.order(o["id"]) or o)
        return
    old = o.get("stripe") or {}
    if old.get("id") and not old.get("expired"):
        expire_session(svc, o)  # a new amount or reference: the old session can't be paid
    attempt = int(old.get("attempt") or 0) + 1
    back = o.get("return_url") or sales.thanks_url(svc)
    ref = inv.get("number") or o["id"]
    name = f"{o['product_name']} ({inv['number']})" if inv.get("number") else o["product_name"]
    data = {
        "mode": "payment",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": sales.settings(svc)["currency"].lower(),
        "line_items[0][price_data][unit_amount]": str(o["total"]["gross"]),
        "line_items[0][price_data][product_data][name]": name,
        "client_reference_id": ref,
        "customer_email": o["email"],
        "metadata[belfry_order]": o["id"],
        "payment_intent_data[metadata][belfry_order]": o["id"],
        "success_url": sales._with_order(back, o["id"]),
        "cancel_url": sales._with_order(back, o["id"]),
    }
    if inv.get("number"):
        data["metadata[invoice]"] = inv["number"]
        data["payment_intent_data[metadata][invoice]"] = inv["number"]
    try:
        got = _post(svc, "/checkout/sessions", data, f"belfry-{o['id']}-{attempt}", env)
    except sales.Refused as exc:
        flags = list(o.get("flags") or [])
        if "card_failed" not in flags:
            flags.append("card_failed")
        log.warning("sales: order %s: no card session: %s", o["id"], exc)
        st.update(o["id"], step="wait", flags=flags, error=str(exc)[:200])
        if inv.get("number"):
            sales.queue_invoice_mail(svc, st.order(o["id"]) or o)
        return
    amounts = {**(o.get("stripe_amounts") or {}), got["id"]: int(o["total"]["gross"])}
    st.update(
        o["id"],
        step="wait",
        stripe={
            "id": got["id"],
            "url": got.get("url"),
            "expires_at": got.get("expires_at"),
            "attempt": attempt,
        },
        stripe_amounts=amounts,
        error=None,
    )
    log.info("sales: order %s: card session made", o["id"])
    if inv.get("number"):
        sales.queue_invoice_mail(svc, st.order(o["id"]) or o)


def expire_session(svc: Services, o: dict[str, Any]) -> None:
    """An order that ended: its open session can't be paid any more (best effort)."""
    from . import sales

    sid = (o.get("stripe") or {}).get("id")
    if not sid:
        return
    try:
        _post(svc, f"/checkout/sessions/{sid}/expire", env=sales.env_of(o))
    except Exception as exc:  # noqa: BLE001 — paid or expired already; a late payment is flagged
        log.info("sales: order %s: session not expired: %s", o["id"], type(exc).__name__)


def livemode_ok(svc: Services, event: dict[str, Any], env: str) -> bool:
    """Stripe's ``livemode`` must match the hook's environment. The test hook takes only test
    events (``livemode`` false). The production hook takes live events, and test events only
    while production's own key is a test key (a belfry that tried Stripe before environments
    existed keeps working)."""
    from . import sales

    live = event.get("livemode")
    if env == "test":
        return live is False
    if live is True:
        return True
    return bool(re.match(r"^(sk|rk)_test_", sales.secret(svc, "stripe_key")))


def handle(svc: Services, event: dict[str, Any], env: str = "live") -> str:
    """One verified Stripe event of ENV's hook → a short result. An order of the other
    environment is never settled by it."""
    from . import sales

    if event.get("type") not in EVENTS:
        return "ignored"
    if not livemode_ok(svc, event, env):
        log.warning("sales: a Stripe event of the wrong mode on the %s hook", env)
        return "wrong_mode"
    obj = (event.get("data") or {}).get("object") or {}
    oid = str((obj.get("metadata") or {}).get("belfry_order") or "")
    st = sales.store(svc)
    o = st.order(oid) if sales.ORDER_RE.match(oid) else None
    if o is None:
        return "unknown_order"
    if sales.env_of(o) != env:
        log.warning(
            "sales: order %s is %s; an event on the %s hook ignored", oid, sales.env_of(o), env
        )
        return "wrong_env"
    if obj.get("payment_status") != "paid":
        return "not_paid"
    inv = (o.get("invoice") or {}).get("number")
    sid = str(obj.get("id") or "")
    paid_amount = int(obj.get("amount_total") or -1)
    # the amount the session was made for (an older session still open after the total changed
    # to Odoo's is paid for its own amount: accepted, flagged)
    made_for = int((o.get("stripe_amounts") or {}).get(sid, o["total"]["gross"]))
    ref = obj.get("client_reference_id")
    ok = (
        paid_amount == made_for
        and str(obj.get("currency") or "").lower() == sales.settings(svc)["currency"].lower()
        and bool(ref)
        and ref in (oid, inv)
    )
    if ok and o["state"] in ("paid", "active"):
        if (o.get("paid") or {}).get("session") != sid:
            # a second payment of an order already paid: nothing changes, the admins refund one
            twice = [*(o.get("paid_twice") or []), sid][-10:]
            st.update(oid, flags=sales._flag(o, "paid_twice"), paid_twice=twice)
            log.warning("sales: order %s was paid twice (a second Stripe session)", oid)
            return "paid_twice"
        return "paid"
    if not ok:
        flags = list(o.get("flags") or [])
        if "stripe_mismatch" not in flags:
            flags.append("stripe_mismatch")
            st.update(oid, flags=flags)
        log.warning("sales: order %s: a Stripe payment that doesn't match it", oid)
        return "mismatch"
    if made_for != int(o["total"]["gross"]):
        st.update(oid, flags=sales._flag(o, "paid_old_amount"))
    sales.mark_paid(
        svc,
        oid,
        "card",
        str(obj.get("payment_intent") or sid),
        session=sid,
        amount=paid_amount,
    )
    return "paid"


def register(app: FastAPI) -> None:
    from . import sales

    @app.post("/console/hooks/stripe", include_in_schema=False)
    async def stripe_hook(request: Request) -> JSONResponse:
        return await hook(request, "live")

    @app.post("/console/hooks/stripe/test", include_in_schema=False)
    async def stripe_hook_test(request: Request) -> JSONResponse:
        """The test environment's hook (Stripe's test mode): its own signing secret; settles
        only test orders."""
        return await hook(request, "test")

    async def hook(request: Request, env: str) -> JSONResponse:
        s: Services = request.app.state.services
        body = await request.body()
        whsec = sales.secret(s, "stripe_webhook_secret", env)
        if not whsec:
            return JSONResponse({"error": "not set up"}, status_code=400)
        try:
            verify_signature(body, request.headers.get("stripe-signature"), whsec)
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
        # Stripe's ids are unique per account and mode; the environment keeps the two apart
        eid = str(event["id"]) if env == "live" else f"test:{event['id']}"
        if not st.first_event(eid):
            return JSONResponse({"result": "duplicate"})
        try:
            result = await run_in_threadpool(handle, s, event, env)
        except Exception:  # noqa: BLE001 — Stripe delivers again; handle it then
            st.forget_event(eid)
            log.exception("sales: stripe event failed")
            return JSONResponse({"error": "failed"}, status_code=500)
        st.event_result(eid, result)
        return JSONResponse({"result": result})
