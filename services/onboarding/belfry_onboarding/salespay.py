"""``belfry.<domain>/pay``: paying by bank transfer (docs/sales.md §6).

The belfry's bank account (holder, IBAN, BIC), the amount and the communication, each with a
copy button, and an EPC (SEPA) QR code for banking apps, drawn here as SVG (segno). A Belgian
structured communication ``+++…+++`` goes in the QR's structured field, anything else in its
message field, exactly as given.

- ``/pay/<order>``: an order's own page; amount and communication come from the order.
- ``/pay?amount=12.50&communication=…``: a free-standing request. ``amount`` must be a number,
  ``communication`` at most 140 characters of letters, digits, spaces and ``+ / - . , : ( )``;
  anything else → the page without them. ``description`` shows only on a link signed by the
  belfry (``&sig=``: ``belfry pay-link``, or ``POST /api/v1/pay-links`` by an admin). Free text
  from an unsigned URL is never shown.
- No bank details set → 404.
"""

from __future__ import annotations

import html
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import segno
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)
E = html.escape

COMMUNICATION = re.compile(r"^[A-Za-z0-9 +/\-.,:()]{1,140}$")
STRUCTURED = re.compile(r"^\+\+\+\d{3}/\d{4}/\d{5}\+\+\+$")
AMOUNT = re.compile(r"^\d{1,7}(?:[.,]\d{1,2})?$")
MAX_DESCRIPTION = 300


# ── IBAN ────────────────────────────────────────────────────────────────────────────────────
def clean_iban(iban: str) -> str:
    return re.sub(r"\s", "", iban or "").upper()


def iban_ok(iban: str) -> bool:
    """The ISO 13616 check: mod 97 of the rearranged number is 1."""
    s = clean_iban(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def bic_ok(bic: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", (bic or "").strip().upper()))


def iban_spaced(iban: str) -> str:
    s = clean_iban(iban)
    return " ".join(s[i : i + 4] for i in range(0, len(s), 4))


# ── EPC QR ──────────────────────────────────────────────────────────────────────────────────
def epc_payload(holder: str, iban: str, bic: str, cents: int | None, communication: str) -> str:
    """EPC069-12 "SEPA credit transfer" (version 002, UTF-8). A Belgian structured
    communication goes in the structured field (Febelfin's QR on invoices)."""
    structured = communication if STRUCTURED.match(communication or "") else ""
    return "\n".join(
        [
            "BCD",
            "002",
            "1",
            "SCT",
            (bic or "").strip().upper(),
            holder[:70],
            clean_iban(iban),
            f"EUR{cents / 100:.2f}" if cents else "",
            "",
            structured,
            "" if structured else (communication or "")[:140],
        ]
    )


def qr_svg(payload: str) -> str:
    qr = segno.make(payload, error="m", encoding="utf-8")
    return qr.svg_inline(scale=4, border=2, dark="#000", light="#fff", omitsize=True)


# ── signed links ────────────────────────────────────────────────────────────────────────────
def parse_amount(raw: Any) -> int | None:
    s = str(raw or "").strip()
    if not AMOUNT.match(s):
        return None
    try:
        d = Decimal(s.replace(",", "."))
    except InvalidOperation:
        return None
    c = int(d * 100)
    return c if 0 < c <= 100_000_000 else None


def _signed_text(cents: int | None, communication: str, description: str) -> str:
    return json.dumps([cents or 0, communication, description], separators=(",", ":"))


def sign(svc: Services, cents: int | None, communication: str, description: str) -> str:
    from . import sales

    return sales.hmac_sign(svc, _signed_text(cents, communication, description))


def make_link(svc: Services, amount: Any, communication: str, description: str) -> str:
    """A /pay link the belfry signs, so its description shows (ValueError when a part is
    not acceptable)."""
    from . import console

    cents = parse_amount(amount) if amount not in (None, "") else None
    if amount not in (None, "") and cents is None:
        raise ValueError("amount: a number, like 12.50")
    communication = (communication or "").strip()
    if communication and not COMMUNICATION.match(communication):
        raise ValueError("communication: up to 140 letters, digits, spaces and + / - . , : ( )")
    description = " ".join((description or "").split())
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"description: at most {MAX_DESCRIPTION} characters")
    q: dict[str, str] = {}
    if cents:
        q["amount"] = f"{cents / 100:.2f}"
    if communication:
        q["communication"] = communication
    if description:
        q["description"] = description
        q["sig"] = sign(svc, cents, communication, description)
    base = console.console_url(svc.settings) + "/pay"
    return base + ("?" + urlencode(q) if q else "")


# ── the page ────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
.pay{max-width:520px;margin:0 auto}
.pay h1{font-size:26px;margin:0 0 6px}
.pay .lead{color:var(--fg-muted);margin:0 0 18px}
.pay .box{border:1px solid var(--border);border-radius:16px;padding:18px;display:flex;flex-direction:column;align-items:center;gap:18px}
.pay .qr{background:#fff;border-radius:12px;padding:10px;width:200px;max-width:70vw}
.pay .qr svg{width:100%;height:auto;display:block}
.pay dl{margin:0;width:100%;display:grid;gap:10px}
.pay .row{display:flex;align-items:center;gap:10px;border-top:1px solid var(--border);padding-top:10px}
.pay .row:first-child{border-top:0;padding-top:0}
.pay dt{font-size:13px;color:var(--fg-muted);margin:0}
.pay dd{margin:2px 0 0;font-weight:600;word-break:break-all;font-variant-numeric:tabular-nums}
.pay .row>div{flex:1;min-width:0}
.pay .copy{flex:none;font:inherit;font-size:13px;border:1px solid var(--border);background:transparent;color:inherit;border-radius:8px;padding:6px 10px;cursor:pointer}
.pay .note{font-size:14px;color:var(--fg-muted);margin-top:14px}
.pay .desc{margin:0 0 14px}
</style>"""

SCRIPT = """<script>
document.addEventListener('click', function (e) {
  var b = e.target.closest('button.copy'); if (!b) return;
  var v = b.getAttribute('data-copy');
  var done = function () { var t = b.textContent; b.textContent = 'Copied'; setTimeout(function () { b.textContent = t; }, 1500); };
  if (navigator.clipboard) navigator.clipboard.writeText(v).then(done, function () {});
});
</script>"""

TEXT = {
    "lead_amount": "to {holder}. Scan the code with your banking app, or copy each detail.",
    "lead": "A transfer to {holder}. Scan the code with your banking app, or copy each detail.",
    "keep": "Please keep the communication exactly as it is: it is how your transfer is matched "
    "to your invoice.",
    "slow": "Transfers take one or two working days.",
    "paid": "This order is paid. Thank you.",
    "ended": "This order has ended. If you paid by bank transfer before your place's date, "
    "tell the belfry's admins: they reopen it. Otherwise, make a new order.",
    "waiting": "This order has no invoice yet. Try again in a few minutes.",
}


def _row(label: str, shown: str, copy: str) -> str:
    return (
        f'<div class="row"><div><dt>{E(label)}</dt><dd>{E(shown)}</dd></div>'
        f'<button type="button" class="copy" data-copy="{E(copy)}" '
        f'aria-label="Copy the {E(label.lower())}">Copy</button></div>'
    )


def page_html(
    st: dict[str, Any], cents: int | None, communication: str, description: str = ""
) -> str:
    holder, iban, bic = st["bank_holder"], st["bank_iban"], st["bank_bic"]
    qr = qr_svg(epc_payload(holder, iban, bic, cents, communication))
    if cents:
        money = f"€{cents / 100:,.2f}"
        lead = f"<b>{E(money)}</b> " + E(TEXT["lead_amount"].format(holder=holder))
    else:
        lead = E(TEXT["lead"].format(holder=holder))
    rows = _row("Account holder", holder, holder) + _row(
        "IBAN", iban_spaced(iban), clean_iban(iban)
    )
    if bic:
        rows += _row("BIC", bic.upper(), bic.upper())
    if cents:
        rows += _row("Amount", f"€{cents / 100:,.2f}", f"{cents / 100:.2f}")
    if communication:
        rows += _row("Communication", communication, communication)
    desc = f'<p class="desc">{E(description)}</p>' if description else ""
    note = (TEXT["keep"] + " " if communication else "") + TEXT["slow"]
    return f"""{STYLE}<div class="pay"><h1>Pay by bank transfer</h1>{desc}<p class="lead">{lead}</p>
<div class="box"><div class="qr" role="img" aria-label="QR code for your banking app, with every detail filled in">{qr}</div>
<dl>{rows}</dl></div><p class="note">{E(note)}</p></div>{SCRIPT}"""


def page_token(svc: Services, oid: str) -> str:
    """The order page's token for its own form (the page's address is the order's secret)."""
    from . import sales

    return sales.hmac_sign(svc, "pay-page:" + oid)


def order_html(svc: Services, o: dict[str, Any]) -> str:
    """An order's own page (the API's ``invoice.url``): its invoice's number and amount; while
    it is open, until when the place is held and how to pay (bank transfer when bank details
    are set, the card link when there is one)."""
    from . import sales

    inv = o.get("invoice") or {}
    money = f"€{o['total']['gross'] / 100:,.2f}"
    head = (
        f'<p class="lead"><b>Invoice {E(inv["number"])}</b> · {E(money)} · '
        f"{E(o['product_name'])}</p>"
        if inv.get("number")
        else ""
    )
    if o["state"] in ("paid", "active"):
        return f'{STYLE}<div class="pay"><h1>Your order</h1>{head}<p>{E(TEXT["paid"])}</p></div>'
    if o["state"] in ("cancelled", "expired"):
        return f'{STYLE}<div class="pay"><h1>Your order</h1>{head}<p>{E(TEXT["ended"])}</p></div>'
    if not inv.get("number"):
        if o.get("pay") == "card" and sales.bank_ready(svc):
            # a card order has no invoice (it is made once paid): ask for one to pay by transfer
            card = (o.get("stripe") or {}).get("url")
            card_btn = f'<p><a class="btn" href="{E(card)}">Pay by card</a></p>' if card else ""
            money = f"€{o['total']['gross'] / 100:,.2f}"
            return (
                f'{STYLE}<div class="pay"><h1>Your order</h1><p class="lead">{E(o["product_name"])}'
                f" · {E(money)}</p>{card_btn}"
                f'<form method="post" action="/pay/{E(o["id"])}/invoice">'
                f'<input type="hidden" name="t" value="{E(page_token(svc, o["id"]))}">'
                '<button class="btn ghost" type="submit">Pay by transfer instead: get the '
                "invoice</button></form></div>"
            )
        return f'{STYLE}<div class="pay"><h1>Your order</h1><p>{E(TEXT["waiting"])}</p></div>'
    held = f"Your place is held until {o['hold_until'][:10]}."
    card = (o.get("stripe") or {}).get("url")
    card_btn = f'<p><a class="btn" href="{E(card)}">Pay by card</a></p>' if card else ""
    if sales.bank_ready(svc):
        comm = inv.get("communication") or inv["number"]
        body = page_html(sales.settings(svc), o["total"]["gross"], comm)
        return body.replace(
            '<div class="box">',
            f'{head}<p class="lead">{E(held)}</p>{card_btn}<div class="box">',
            1,
        )
    return f'{STYLE}<div class="pay"><h1>Your order</h1>{head}<p>{E(held)}</p>{card_btn}</div>'


def _frame(body: str, status: int = 200) -> HTMLResponse:
    from . import theme

    return HTMLResponse(
        theme.frame("Pay by bank transfer", body),
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex",
        },
    )


def _message(key: str, status: int = 200) -> HTMLResponse:
    return _frame(
        f'{STYLE}<div class="pay"><h1>Pay by bank transfer</h1><p>{E(TEXT[key])}</p></div>', status
    )


def free_request(svc: Services, q: dict[str, str]) -> tuple[int | None, str, str]:
    """(cents, communication, description) of /pay?…: each part only when it is acceptable,
    the description only when the link is signed."""
    import hmac

    cents = parse_amount(q.get("amount"))
    communication = (q.get("communication") or "").strip()
    if not COMMUNICATION.match(communication):
        communication = ""
    description = ""
    raw = " ".join((q.get("description") or "").split())
    if raw and len(raw) <= MAX_DESCRIPTION and q.get("sig"):
        # the signature covers the amount and the communication as given
        given_cents = parse_amount(q.get("amount")) if q.get("amount") else None
        given_comm = (q.get("communication") or "").strip()
        want = sign(svc, given_cents, given_comm, raw).encode()
        if hmac.compare_digest(str(q["sig"]).encode("utf-8", "replace"), want):
            description = raw
    return cents, communication, description


def register(app: FastAPI) -> None:
    from . import sales
    from .holder_invites import ApiError

    @app.get("/console/pay", include_in_schema=False)
    async def pay(request: Request):
        s = request.app.state.services
        if not sales.own(s) or not sales.bank_ready(s):
            return _frame("<p>Not found.</p>", 404)
        cents, comm, desc = free_request(s, dict(request.query_params))
        return _frame(page_html(sales.settings(s), cents, comm, desc))

    @app.get("/console/pay/{oid}", include_in_schema=False)
    async def pay_order(oid: str, request: Request):
        s = request.app.state.services
        if not sales.own(s):
            return _frame("<p>Not found.</p>", 404)
        o = sales.store(s).order(oid) if sales.ORDER_RE.match(oid) else None
        if o is None or not o["total"]["gross"]:
            return _frame("<p>Not found.</p>", 404)
        return _frame(order_html(s, o))

    @app.post("/console/pay/{oid}/invoice", include_in_schema=False)
    async def pay_by_transfer(oid: str, request: Request):
        """A card order's buyer asks for the invoice to pay by transfer instead."""
        import hmac

        from fastapi.responses import RedirectResponse

        s = request.app.state.services
        form = await request.form()
        given = str(form.get("t") or "").encode("utf-8", "replace")
        if not sales.own(s) or not sales.ORDER_RE.match(oid):
            return _frame("<p>Not found.</p>", 404)
        if not hmac.compare_digest(given, page_token(s, oid).encode()):
            return _frame("<p>Not found.</p>", 404)
        try:
            await run_in_threadpool(sales.switch_pay, s, oid, "invoice")
        except ApiError:
            pass  # the page shows the order as it is
        return RedirectResponse(f"/pay/{oid}", status_code=303)

    @app.post("/api/pay-links")
    async def api_pay_link(request: Request) -> JSONResponse:
        s, key, data = await sales._signed(request)
        await run_in_threadpool(sales._admin, s, key)
        if not sales.bank_ready(s):
            raise ApiError(409, "no_bank", "Set the bank details first.")
        try:
            url = make_link(
                s,
                data.get("amount"),
                str(data.get("communication") or ""),
                str(data.get("description") or ""),
            )
        except ValueError as exc:
            raise ApiError(422, "invalid", str(exc)) from exc
        return JSONResponse({"url": url}, status_code=201, headers={"Cache-Control": "no-store"})
