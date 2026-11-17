"""The seller's Odoo for selling passes (sales.py): the invoice first, the payment later, a credit
note when an order ends unpaid; read every 15 minutes for transfers and credit notes. Odoo
Online works: only standard records over its JSON-2 API (clients/odoo_json2.py), no module.

Idempotent everywhere: the invoice carries ``<ref prefix><order id>`` in ``ref`` and is looked up
by it before anything is made; a payment is registered only while Odoo says the invoice is not
paid; the credit note is the one reversing the invoice.

Also the VAT check of an organisation on VIES (``Vies``; tests put a fake in ``VIES``).

Errors: Odoo answering 4xx (it refuses the record as it is, or the key) raises
``sales.Refused``; anything else (Odoo down, a timeout) is left for the tick to retry.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .clients.odoo import OdooError
from .clients.odoo_json2 import Json2Client
from .core import Services

log = logging.getLogger(__name__)

PAID = ("paid", "in_payment")
EU = set(
    "AT BE BG CY CZ DE DK EE EL ES FI FR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK XI".split()
)
MOVE_FIELDS = [
    "id",
    "name",
    "state",
    "payment_state",
    "payment_reference",
    "amount_untaxed",
    "amount_tax",
    "amount_total",
    "journal_id",
]


# ── VIES ────────────────────────────────────────────────────────────────────────────────────
class Vies:
    """The EU's VAT number check (REST API). ``check`` → True / False, or None when VIES did not
    answer (the order is taken and flagged for the admins)."""

    URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=10.0)

    def check(self, country: str, number: str) -> bool | None:
        try:
            r = self._client.post(self.URL, json={"countryCode": country, "vatNumber": number})
            if r.status_code != 200:
                return None
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if data.get("userError") not in (None, "VALID", "INVALID"):
            return None  # MS_UNAVAILABLE, TIMEOUT, …
        return bool(data.get("valid"))


VIES: Any = Vies()


def check_vat(vat: str) -> bool | None:
    """An EU VAT number (``BE0123…``) on VIES; True for numbers outside the EU (not checked)."""
    country, number = vat[:2], vat[2:]
    if country not in EU:
        return True
    return VIES.check(country, number)


# ── the client ──────────────────────────────────────────────────────────────────────────────
def client(svc: Services) -> Json2Client:
    from . import sales

    st = sales.settings(svc)
    return Json2Client(st["odoo_url"], st["odoo_db"], sales.secret(svc, "odoo_api_key"))


def _refused(exc: OdooError) -> bool:
    status = int((exc.data or {}).get("status") or 0)
    return 400 <= status < 500 and status not in (408, 429)


def _guard(fn):
    """Odoo refusing (4xx) → sales.Refused, with a short reason and never the key."""
    from . import sales

    def run(*a, **k):
        try:
            return fn(*a, **k)
        except OdooError as exc:
            if _refused(exc):
                raise sales.Refused(str(exc.message or exc)[:200]) from exc
            raise

    run.__name__ = fn.__name__
    return run


def _ctx(move_id: int) -> dict[str, Any]:
    return {"active_model": "account.move", "active_ids": [move_id], "active_id": move_id}


def _cents(x: Any) -> int:
    return int(round(float(x or 0) * 100))


def _move(remote: Json2Client, move_id: int) -> dict[str, Any]:
    move = remote.read_one("account.move", move_id, MOVE_FIELDS)
    if move is None:
        from . import sales

        raise sales.Refused(f"the invoice {move_id} is gone from Odoo")
    return move


def ref_of(svc: Services, oid: str) -> str:
    from . import sales

    return sales.settings(svc)["ref_prefix"] + oid


# ── the steps ───────────────────────────────────────────────────────────────────────────────
def _partner(remote: Json2Client, o: dict[str, Any], company: int | None) -> int:
    b = o.get("billing") or {}
    org = b.get("kind") == "organisation"
    email = b.get("invoice_email") or o["email"]
    if org and b.get("vat"):
        rows = remote.search_read(
            "res.partner", [["vat", "=", b["vat"]], ["is_company", "=", True]], ["id"], limit=1
        )
        if rows:
            return rows[0]["id"]
    elif not org:
        rows = remote.search_read(
            "res.partner",
            [["email", "=ilike", email], ["is_company", "=", False]],
            ["id", "email"],
            limit=5,
        )
        for r in rows:
            if (r.get("email") or "").strip().lower() == email.lower():
                return r["id"]
    vals: dict[str, Any] = {
        "name": (b.get("name") if org else (b.get("name") or o["name"])) or o["name"],
        "email": email,
        "is_company": org,
    }
    for k in ("street", "zip", "city"):
        if b.get(k):
            vals[k] = b[k]
    if org and b.get("vat"):
        vals["vat"] = b["vat"]
    if b.get("country"):
        rows = remote.search_read("res.country", [["code", "=", b["country"]]], ["id"], limit=1)
        if rows:
            vals["country_id"] = rows[0]["id"]
    if company:
        vals["company_id"] = company
    return remote.create("res.partner", vals)


@_guard
def make_invoice(svc: Services, o: dict[str, Any]) -> None:
    """The order's posted invoice: its number, communication and totals read back."""
    from . import sales

    st = sales.settings(svc)
    remote = client(svc)
    ref = ref_of(svc, o["id"])
    rows = remote.search_read(
        "account.move",
        [["move_type", "=", "out_invoice"], ["ref", "=", ref], ["state", "!=", "cancel"]],
        MOVE_FIELDS,
        limit=1,
    )
    if rows:
        move = rows[0]
    else:
        org = (o.get("billing") or {}).get("kind") == "organisation"
        product = (
            st["odoo_products"].get(o["product"])
            or st["fallback_products"].get(o["tier"])
            or st["fallback_products"].get("*")
        )
        tax = st["tax_excluded"] if org else st["tax_included"]
        if not product or not tax:
            raise sales.Refused("no Odoo product or tax picked for this product")
        vals: dict[str, Any] = {
            "move_type": "out_invoice",
            "partner_id": _partner(remote, o, st["company"]),
            "ref": ref,
            "invoice_line_ids": [
                [
                    0,
                    0,
                    {
                        "product_id": int(product),
                        "name": o["product_name"],
                        "quantity": 1,
                        "price_unit": o["unit"] / 100,
                        "discount": float(o["total"]["percent_off"]),
                        "tax_ids": [[6, 0, [int(tax)]]],
                    },
                ]
            ],
        }
        if st["company"]:
            vals["company_id"] = int(st["company"])
        cur = remote.search_read("res.currency", [["name", "=", st["currency"]]], ["id"], limit=1)
        if cur:
            vals["currency_id"] = cur[0]["id"]
        move = _move(
            remote,
            remote.create("account.move", vals, context={"default_move_type": "out_invoice"}),
        )
        sales.store(svc).update(o["id"], invoice={"move_id": move["id"]})
    if move["state"] == "draft":
        remote.call("account.move", "action_post", ids=[move["id"]])
        move = _move(remote, move["id"])
    total = {
        "net": _cents(move["amount_untaxed"]),
        "vat": _cents(move["amount_tax"]),
        "gross": _cents(move["amount_total"]),
        "percent_off": o["total"]["percent_off"],
    }
    flags = list(o.get("flags") or [])
    if total["gross"] != o["total"]["gross"] and "odoo_total" not in flags:
        # Odoo's invoice is the truth (rounding, a fiscal position): the order follows it
        flags.append("odoo_total")
        log.warning(
            "sales: order %s: Odoo's total %s differs from Belfry's %s",
            o["id"],
            total["gross"],
            o["total"]["gross"],
        )
    url = None
    try:
        path = remote.call("account.move", "get_portal_url", ids=[move["id"]])
        if isinstance(path, str) and path.startswith("/"):
            url = st["odoo_url"].rstrip("/") + path
    except (OdooError, httpx.HTTPError):
        url = None
    invoice = {
        "move_id": move["id"],
        "number": move["name"],
        "communication": move.get("payment_reference") or move["name"],
        "url": url,
    }
    sales.store(svc).update(o["id"], invoice=invoice, total=total, flags=flags)
    log.info("sales: order %s invoiced as %s", o["id"], move["name"])
    o = sales.store(svc).order(o["id"]) or o
    if o["pay"] == "invoice":
        from . import salesmail

        if sales.store(svc).once("invoice:" + o["id"]):
            salesmail.invoice(svc, o)
        if st["send_invoice"]:
            _send(remote, move["id"])
    sales.after_invoice(svc, o)


def _send(remote: Json2Client, move_id: int) -> None:
    """Odoo mails its invoice (PDF) too, best effort."""
    try:
        wizard = remote.create("account.move.send.wizard", {}, context=_ctx(move_id))
        remote.call(
            "account.move.send.wizard", "action_send_and_print", ids=[wizard], context=_ctx(move_id)
        )
    except (OdooError, httpx.HTTPError) as exc:
        log.warning("sales: Odoo did not send invoice %s: %s", move_id, type(exc).__name__)


@_guard
def register_payment(svc: Services, o: dict[str, Any]) -> None:
    """The money that came by card (or that an admin marked) on the invoice, in its journal."""
    from . import sales

    st = sales.settings(svc)
    paid = o.get("paid") or {}
    journal = st["card_journal"] if paid.get("how") == "card" else st["manual_journal"]
    move_id = (o.get("invoice") or {}).get("move_id")
    flags = list(o.get("flags") or [])
    if move_id and journal:
        remote = client(svc)
        move = _move(remote, int(move_id))
        if move["payment_state"] not in PAID and move["state"] == "posted":
            memo = " ".join(x for x in (move["name"], paid.get("ref") or "") if x)
            date = (paid.get("at") or sales.iso(sales.now()))[:10]
            wizard = remote.create(
                "account.payment.register",
                {"journal_id": int(journal), "payment_date": date, "communication": memo},
                context=_ctx(int(move_id)),
            )
            remote.call(
                "account.payment.register",
                "action_create_payments",
                ids=[wizard],
                context=_ctx(int(move_id)),
            )
            log.info("sales: order %s: payment registered on %s", o["id"], move["name"])
    elif move_id and "payment_not_booked" not in flags:
        flags.append("payment_not_booked")  # no journal picked: an admin books it in Odoo
    sales.store(svc).update(o["id"], step="done", payment_due=False, flags=flags)


@_guard
def credit_note(svc: Services, o: dict[str, Any]) -> None:
    """An order that ended unpaid: its invoice is credited in full (a draft is cancelled)."""
    move_id = (o.get("invoice") or {}).get("move_id")
    if not move_id:
        return
    remote = client(svc)
    move = _move(remote, int(move_id))
    if move["state"] == "draft":
        remote.call("account.move", "button_cancel", ids=[move["id"]])
        return
    if move["state"] != "posted" or move["payment_state"] in (*PAID, "reversed"):
        return  # paid after all (the tick will see it), cancelled, or credited already
    rows = remote.search_read(
        "account.move",
        [["move_type", "=", "out_refund"], ["reversed_entry_id", "=", move["id"]]],
        ["id", "state"],
        limit=1,
    )
    if rows:
        credit_id = rows[0]["id"]
        state = rows[0]["state"]
    else:
        wizard = remote.create(
            "account.move.reversal",
            {
                "reason": f"Order {o['id']} not paid in time",
                "journal_id": (move.get("journal_id") or [False])[0],
            },
            context=_ctx(move["id"]),
        )
        action = remote.call(
            "account.move.reversal", "refund_moves", ids=[wizard], context=_ctx(move["id"])
        )
        credit_id = action.get("res_id") if isinstance(action, dict) else None
        if not credit_id:
            found = remote.search_read(
                "account.move",
                [["move_type", "=", "out_refund"], ["reversed_entry_id", "=", move["id"]]],
                ["id"],
                limit=1,
            )
            if not found:
                raise OdooError("no credit note made", {"status": 500})
            credit_id = found[0]["id"]
        state = (remote.read_one("account.move", credit_id, ["state"]) or {}).get("state")
    if state == "draft":
        remote.call("account.move", "action_post", ids=[credit_id])
    log.info("sales: order %s: invoice %s credited", o["id"], move["name"])


def read_open_invoices(svc: Services) -> dict[str, int]:
    """The tick: invoices of open orders paid in Odoo (a transfer reconciled) → active; the
    invoice of an active order credited in full → its pass closes."""
    from . import core, sales

    st = sales.store(svc)
    watch = {
        int(o["invoice"]["move_id"]): o
        for o in st.orders(("invoiced", "active"))
        if (o.get("invoice") or {}).get("move_id") and not o.get("closed")
    }
    if not watch:
        return {"paid": 0, "closed": 0}
    remote = client(svc)
    rows = remote.call(
        "account.move", "read", ids=sorted(watch), fields=["id", "payment_state", "state"]
    )
    paid = closed = 0
    for r in rows or []:
        o = watch.get(r["id"])
        if o is None:
            continue
        if o["state"] == "invoiced" and r.get("payment_state") in PAID:
            sales.mark_paid(svc, o["id"], "transfer")
            paid += 1
        elif o["state"] == "active" and r.get("payment_state") == "reversed" and o.get("pass_id"):
            core.revoke_pass(svc, int(o["pass_id"]), "credit note")
            st.update(o["id"], closed=sales.iso(sales.now()))
            log.info("sales: order %s: the invoice was credited, its pass closed", o["id"])
            closed += 1
    return {"paid": paid, "closed": closed}


# ── the console's pick-lists ────────────────────────────────────────────────────────────────
def check_connection(url: str, db: str, key: str) -> list[dict[str, Any]]:
    """The companies KEY sees (the console's "Check"); OdooError / httpx.HTTPError when not."""
    remote = Json2Client(url, db, key)
    return remote.search_read("res.company", [], ["id", "name"])


def pick_lists(svc: Services) -> dict[str, list[dict[str, Any]]]:
    """Companies, products, sale taxes and bank/cash journals, read from Odoo."""
    from . import sales

    remote = client(svc)
    company = sales.settings(svc)["company"]
    scope = [["company_id", "in", [int(company), False]]] if company else []
    return {
        "companies": remote.search_read("res.company", [], ["id", "name"]),
        "products": remote.search_read(
            "product.product", [["sale_ok", "=", True], *scope], ["id", "display_name"], limit=100
        ),
        "taxes": remote.search_read(
            "account.tax",
            [
                ["type_tax_use", "=", "sale"],
                *([["company_id", "=", int(company)]] if company else []),
            ],
            ["id", "name", "amount", "price_include"],
        ),
        "journals": remote.search_read(
            "account.journal",
            [
                ["type", "in", ["bank", "cash"]],
                *([["company_id", "=", int(company)]] if company else []),
            ],
            ["id", "name", "code"],
        ),
    }
