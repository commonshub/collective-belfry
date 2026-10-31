"""Bookkeeping at the organisation that sells the tickets, in its own Odoo (Odoo Online too).

Belfry's Odoo keeps the order, the payment link and the pass; its invoice stays internal. For each
paid ticket this module books, over the issuer's JSON-2 API, a customer invoice with the same
lines and VAT, the payment in the issuer's payment journal, and sends that invoice to the buyer.
A full refund becomes a credit note with its refund payment. The result (the issuer's invoice
number, or the error) is kept on the pass (``belfry.pass.accounting_*``, module
``belfry_accounting.py``); the invoice-paid and refund hooks book at once, ``reconcile`` retries.

Idempotent: the issuer's invoice carries ``ACCOUNTING_REF_PREFIX`` + Belfry's invoice number in
``ref`` and is looked up by it before anything is created; the credit note is the one reversing
it. Each step (post, payment, sending) checks the issuer's state first.

Chargebacks and disputes are not booked: the money comes back through Stripe's payouts, which
the issuer's accountant reconciles; the pass shows ``attention``. Partial refunds likewise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .clients.odoo import OdooError
from .clients.odoo_json2 import Json2Client

log = logging.getLogger(__name__)

PAID = ("paid", "in_payment")
REMOTE_PAID = ("paid", "in_payment", "reversed")
MOVE_FIELDS = ["id", "name", "state", "amount_total", "payment_state", "is_move_sent", "journal_id"]


class BookingError(Exception):
    """The issuer's invoice can't be booked as it is; the message says why."""


@dataclass
class Accounting:
    remote: Json2Client
    issuer: str
    products: dict[str, int]  # pass tier or "*" -> the issuer's product id
    tax_included: int | None  # the issuer's VAT with the price including it (individuals)
    tax_excluded: int | None  # ... and on top of the price (organisations)
    journal: int | None  # where payments through Stripe (any provider) are booked
    manual_journal: int | None  # payments a steward marked by hand; None: left open
    ref_prefix: str
    send: bool


def parse_ids(spec: str) -> dict[str, int]:
    """``week=12,day=11,*=14`` -> {"week": 12, "day": 11, "*": 14}."""
    out: dict[str, int] = {}
    for pair in (spec or "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            out[key.strip()] = int(value)
    return out


def _int_or_none(value: str) -> int | None:
    return int(value) if str(value or "").strip() else None


def from_settings(settings: Any) -> Accounting | None:
    if not (settings.accounting_odoo_url and settings.accounting_odoo_api_key):
        return None
    return Accounting(
        remote=Json2Client(
            settings.accounting_odoo_url,
            settings.accounting_odoo_db,
            settings.accounting_odoo_api_key,
        ),
        issuer=settings.accounting_issuer,
        products=parse_ids(settings.accounting_products),
        tax_included=_int_or_none(settings.accounting_tax_included),
        tax_excluded=_int_or_none(settings.accounting_tax_excluded),
        journal=_int_or_none(settings.accounting_journal),
        manual_journal=_int_or_none(settings.accounting_manual_journal),
        ref_prefix=settings.accounting_ref_prefix,
        send=settings.accounting_send,
    )


# -- the issuer's side ----------------------------------------------------------------------------


def _ctx(move_id: int) -> dict[str, Any]:
    return {"active_model": "account.move", "active_ids": [move_id], "active_id": move_id}


def _partner(acc: Accounting, buyer: dict[str, Any]) -> int:
    email = (buyer.get("email") or "").strip()
    if email:
        rows = acc.remote.search_read("res.partner", [["email", "=ilike", email]], ["id"], limit=1)
        if rows:
            return rows[0]["id"]
    vals: dict[str, Any] = {"name": buyer.get("name") or email, "email": email or False}
    for key in ("street", "street2", "zip", "city"):
        if buyer.get(key):
            vals[key] = buyer[key]
    if buyer.get("is_company"):
        vals["is_company"] = True
        if buyer.get("vat"):
            vals["vat"] = buyer["vat"]
    if buyer.get("country_code"):
        countries = acc.remote.search_read(
            "res.country", [["code", "=", buyer["country_code"]]], ["id"], limit=1
        )
        if countries:
            vals["country_id"] = countries[0]["id"]
    return acc.remote.create("res.partner", vals)


def _line(acc: Accounting, line: dict[str, Any]) -> dict[str, Any]:
    product = acc.products.get(line.get("tier") or "") or acc.products.get("*")
    if not product:
        raise BookingError(f"no product of the issuer for {line.get('tier') or 'other lines'}")
    tax = line.get("tax")
    if tax == "included":
        tax_ids = [acc.tax_included] if acc.tax_included else None
    elif tax == "excluded":
        tax_ids = [acc.tax_excluded] if acc.tax_excluded else None
    elif tax == "none":
        tax_ids = []
    else:
        tax_ids = None
    if tax_ids is None:
        raise BookingError(f"no tax of the issuer for a line with VAT {tax!r}")
    return {
        "product_id": product,
        "name": line.get("name") or "",
        "quantity": line.get("quantity") or 1,
        "price_unit": line.get("price_unit") or 0.0,
        "discount": line.get("discount") or 0.0,
        "tax_ids": [[6, 0, tax_ids]],
    }


def _move(acc: Accounting, move_id: int) -> dict[str, Any]:
    move = acc.remote.read_one("account.move", move_id, MOVE_FIELDS)
    if move is None:
        raise BookingError(f"the issuer's move {move_id} is gone")
    return move


def _post_pay_send(
    acc: Accounting,
    move: dict[str, Any],
    expected: float,
    journal: int | None,
    date: str | None,
    memo: str,
) -> dict[str, Any]:
    """Post the issuer's draft (if its total is Belfry's), register the payment, send it."""
    if move["state"] == "draft":
        if abs(float(move["amount_total"]) - float(expected)) > 0.005:
            raise BookingError(
                f"the issuer's {move['name'] or 'draft'} totals {move['amount_total']},"
                f" Belfry's {expected}: left as a draft"
            )
        acc.remote.call("account.move", "action_post", ids=[move["id"]])
        move = _move(acc, move["id"])
    if move["payment_state"] not in REMOTE_PAID and journal and date:
        wizard = acc.remote.create(
            "account.payment.register",
            {"journal_id": journal, "payment_date": date, "communication": memo},
            context=_ctx(move["id"]),
        )
        acc.remote.call(
            "account.payment.register",
            "action_create_payments",
            ids=[wizard],
            context=_ctx(move["id"]),
        )
        move = _move(acc, move["id"])
    if acc.send and not move.get("is_move_sent"):
        wizard = acc.remote.create("account.move.send.wizard", {}, context=_ctx(move["id"]))
        acc.remote.call(
            "account.move.send.wizard",
            "action_send_and_print",
            ids=[wizard],
            context=_ctx(move["id"]),
        )
        move = _move(acc, move["id"])
    return move


def _journal_for(acc: Accounting, data: dict[str, Any]) -> int | None:
    return acc.manual_journal if data["payment"].get("provider") == "manual" else acc.journal


def book_invoice(acc: Accounting, data: dict[str, Any]) -> dict[str, Any]:
    """The issuer's invoice for one paid ticket (``belfry_accounting_export``), booked."""
    invoice = data["invoice"]
    ref = acc.ref_prefix + invoice["name"]
    rows = acc.remote.search_read(
        "account.move",
        [["move_type", "=", "out_invoice"], ["ref", "=", ref], ["state", "!=", "cancel"]],
        MOVE_FIELDS,
        limit=1,
    )
    if rows:
        move = rows[0]
    else:
        vals: dict[str, Any] = {
            "move_type": "out_invoice",
            "partner_id": _partner(acc, data["partner"]),
            "ref": ref,
            "invoice_date": invoice["date"],
            "invoice_line_ids": [[0, 0, _line(acc, line)] for line in data["lines"]],
        }
        currencies = acc.remote.search_read(
            "res.currency", [["name", "=", invoice["currency"]]], ["id"], limit=1
        )
        if currencies:
            vals["currency_id"] = currencies[0]["id"]
        move = _move(
            acc,
            acc.remote.create("account.move", vals, context={"default_move_type": "out_invoice"}),
        )
    payment = data["payment"]
    memo = " ".join(p for p in (ref, payment.get("reference") or "") if p)
    return _post_pay_send(
        acc, move, invoice["amount_total"], _journal_for(acc, data), payment.get("date"), memo
    )


def book_credit_note(acc: Accounting, move_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """The credit note reversing the issuer's invoice ``move_id``, with the refund paid out."""
    rows = acc.remote.search_read(
        "account.move",
        [
            ["move_type", "=", "out_refund"],
            ["reversed_entry_id", "=", move_id],
            ["state", "!=", "cancel"],
        ],
        MOVE_FIELDS,
        limit=1,
    )
    if rows:
        credit = rows[0]
    else:
        original = _move(acc, move_id)
        wizard = acc.remote.create(
            "account.move.reversal",
            {
                "reason": f"Refund of {original['name']}",
                "journal_id": (original.get("journal_id") or [False])[0],
            },
            context=_ctx(move_id),
        )
        action = acc.remote.call(
            "account.move.reversal", "refund_moves", ids=[wizard], context=_ctx(move_id)
        )
        credit_id = action.get("res_id") if isinstance(action, dict) else None
        if not credit_id:
            found = acc.remote.search_read(
                "account.move",
                [["move_type", "=", "out_refund"], ["reversed_entry_id", "=", move_id]],
                ["id"],
                limit=1,
            )
            if not found:
                raise BookingError(f"no credit note for the issuer's move {move_id}")
            credit_id = found[0]["id"]
        credit = _move(acc, credit_id)
    return _post_pay_send(
        acc,
        credit,
        data["refunded"],
        _journal_for(acc, data),
        data.get("refund_date"),
        f"Refund {acc.ref_prefix}{data['invoice']['name']}",
    )


# -- Belfry's side: the pass keeps the result -----------------------------------------------------

PASS_FIELDS = [
    "id",
    "state",
    "invoice_id",
    "accounting_state",
    "accounting_move_id",
    "accounting_refund_move_id",
]


def _record(svc: Any, pass_id: int, vals: dict[str, Any]) -> None:
    from .core import now_utc_str

    svc.odoo.write("belfry.pass", [pass_id], {"accounting_synced_at": now_utc_str(), **vals})


def _export(svc: Any, pass_id: int) -> dict[str, Any]:
    return svc.odoo.call("belfry.pass", "belfry_accounting_export", [[pass_id]])


def book_pass(svc: Any, pass_id: int) -> str:
    """Book the ticket of a paid pass at the issuer. Returns a short result code."""
    acc: Accounting | None = svc.accounting
    if acc is None:
        return "disabled"
    row = svc.odoo.read_one("belfry.pass", pass_id, PASS_FIELDS)
    if row is None:
        return "not_found"
    if row.get("accounting_state") in ("done", "refunded"):
        return "already"
    data = _export(svc, pass_id)
    invoice = data.get("invoice")
    if not invoice or invoice.get("payment_state") not in PAID:
        return "not_paid"
    if not invoice.get("amount_total"):
        _record(svc, pass_id, {"accounting_state": "skipped", "accounting_error": False})
        return "skipped"
    try:
        move = book_invoice(acc, data)
    except (BookingError, OdooError, OSError) as exc:
        log.warning("accounting: pass %s, invoice %s not booked: %s", pass_id, invoice["name"], exc)
        _record(svc, pass_id, {"accounting_state": "failed", "accounting_error": str(exc)[:250]})
        return "failed"
    _record(
        svc,
        pass_id,
        {
            "accounting_state": "done",
            "accounting_ref": move["name"],
            "accounting_move_id": move["id"],
            "accounting_error": False,
        },
    )
    log.info("accounting: pass %s booked as %s at %s", pass_id, move["name"], acc.issuer)
    return "booked"


def refund_pass(svc: Any, pass_id: int) -> str:
    """Book the credit note of a refunded pass at the issuer."""
    acc: Accounting | None = svc.accounting
    if acc is None:
        return "disabled"
    row = svc.odoo.read_one("belfry.pass", pass_id, PASS_FIELDS)
    if row is None:
        return "not_found"
    if row.get("accounting_refund_move_id"):
        return "already"
    if row.get("accounting_state") != "done":
        result = book_pass(svc, pass_id)
        if result not in ("booked", "already"):
            return result
        row = svc.odoo.read_one("belfry.pass", pass_id, PASS_FIELDS) or row
    data = _export(svc, pass_id)
    refunded = float(data.get("refunded") or 0)
    if refunded <= 0:
        return "no_refund"
    total = float(data["invoice"]["amount_total"])
    if refunded < total - 0.005:
        _record(
            svc,
            pass_id,
            {
                "accounting_state": "attention",
                "accounting_error": f"partial refund of {refunded} of {total}: book it by hand",
            },
        )
        return "attention"
    try:
        credit = book_credit_note(acc, int(row["accounting_move_id"]), data)
    except (BookingError, OdooError, OSError) as exc:
        log.warning("accounting: pass %s, credit note not booked: %s", pass_id, exc)
        _record(svc, pass_id, {"accounting_error": f"credit note: {exc}"[:250]})
        return "failed"
    _record(
        svc,
        pass_id,
        {
            "accounting_state": "refunded",
            "accounting_refund_ref": credit["name"],
            "accounting_refund_move_id": credit["id"],
            "accounting_error": False,
        },
    )
    log.info("accounting: pass %s credited as %s at %s", pass_id, credit["name"], acc.issuer)
    return "credited"


def flag_chargeback(svc: Any, pass_id: int) -> str:
    """A chargeback or dispute: nothing is booked, the accountant decides."""
    if svc.accounting is None:
        return "disabled"
    row = svc.odoo.read_one("belfry.pass", pass_id, PASS_FIELDS)
    if row is None or row.get("accounting_state") not in ("done", "failed", "todo", False, None):
        return "unchanged"
    _record(
        svc,
        pass_id,
        {
            "accounting_state": "attention",
            "accounting_error": "chargeback or dispute: check Stripe's payout",
        },
    )
    return "attention"


def book_invoice_id(svc: Any, invoice_id: int) -> str:
    """The invoice-paid hook's entry: the pass of ``invoice_id``, booked."""
    if svc.accounting is None:
        return "disabled"
    rows = svc.odoo.search_read("belfry.pass", [["invoice_id", "=", invoice_id]], ["id"], limit=1)
    return book_pass(svc, rows[0]["id"]) if rows else "no_pass"


def refund_transaction(svc: Any, transaction_id: int) -> str:
    """The refund hook's entry: the credit note for the pass a done refund belongs to."""
    if svc.accounting is None:
        return "disabled"
    tx = svc.odoo.read_one("payment.transaction", transaction_id, ["source_transaction_id"])
    source = (tx or {}).get("source_transaction_id")
    source_id = source[0] if isinstance(source, list | tuple) and source else None
    if not source_id:
        return "no_source"
    rows = svc.odoo.search_read(
        "belfry.pass", [["transaction_id", "=", source_id]], ["id"], limit=1
    )
    return refund_pass(svc, rows[0]["id"]) if rows else "no_pass"


def after(svc: Any, step: str, entry: Any, *args: Any) -> None:
    """Run a booking after a hook did its own work; a failure is logged and kept on the pass,
    never passed on (reconcile tries again)."""
    if svc.accounting is None:
        return
    try:
        log.info("accounting after %s: %s", step, entry(svc, *args))
    except Exception:
        log.exception("accounting after %s failed", step)


def reconcile(svc: Any) -> dict[str, Any] | None:
    """Book what the hooks missed: paid passes not booked yet, refunded ones without a credit
    note. Failed bookings are tried again."""
    if svc.accounting is None:
        return None
    booked: dict[int, str] = {}
    for row in svc.odoo.search_read(
        "belfry.pass",
        [
            ["invoice_id", "!=", False],
            ["state", "in", ["active", "expired", "revoked"]],
            ["accounting_state", "in", [False, "todo", "failed"]],
            ["payment_state", "in", list(PAID) + ["reversed"]],
        ],
        ["id"],
    ):
        try:
            booked[row["id"]] = book_pass(svc, row["id"])
        except Exception:
            log.exception("accounting reconcile: pass %s", row["id"])
            booked[row["id"]] = "error"
    credited: dict[int, str] = {}
    for row in svc.odoo.search_read(
        "belfry.pass",
        [
            ["state", "=", "revoked"],
            ["revoke_reason", "=", "refund"],
            ["accounting_state", "=", "done"],
            ["accounting_refund_move_id", "=", 0],
        ],
        ["id"],
    ):
        try:
            credited[row["id"]] = refund_pass(svc, row["id"])
        except Exception:
            log.exception("accounting reconcile: refund of pass %s", row["id"])
            credited[row["id"]] = "error"
    return {"booked": booked, "credited": credited}


def check(acc: Accounting) -> dict[str, Any]:
    """What the configured ids are at the issuer, and the candidates to choose from."""

    def names(model: str, ids: list[int], fields: list[str]) -> dict[int, Any]:
        if not ids:
            return {}
        rows = acc.remote.call(model, "read", ids=ids, fields=fields)
        return {r["id"]: r for r in rows}

    taxes = [t for t in (acc.tax_included, acc.tax_excluded) if t]
    journals = [j for j in (acc.journal, acc.manual_journal) if j]
    return {
        "issuer": acc.issuer,
        "products": names("product.product", list(acc.products.values()), ["display_name"]),
        "taxes": names("account.tax", taxes, ["name", "amount", "price_include", "type_tax_use"]),
        "journals": names("account.journal", journals, ["name", "type", "code"]),
        "candidates": {
            "taxes": acc.remote.search_read(
                "account.tax",
                [["type_tax_use", "=", "sale"]],
                ["id", "name", "amount", "price_include"],
            ),
            "journals": acc.remote.search_read(
                "account.journal", [["type", "in", ["bank", "cash"]]], ["id", "name", "code"]
            ),
            "products": acc.remote.search_read(
                "product.product", [["sale_ok", "=", True]], ["id", "display_name"], limit=50
            ),
        },
    }
