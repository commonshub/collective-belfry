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

import html
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
    "amount_residual",
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


def ref_of(svc: Services, oid: str, round_: int = 1) -> str:
    """The invoice's ref: the order, and the round when an order was invoiced again after its
    first invoice was credited (a payment after the order ended)."""
    from . import sales

    return sales.settings(svc)["ref_prefix"] + oid + (f"-{round_}" if round_ > 1 else "")


# ── the steps ───────────────────────────────────────────────────────────────────────────────
def _stored_partner(svc: Services, o: dict[str, Any]) -> int | None:
    """The Odoo contact Belfry made for this buyer earlier: on the order (a retry), or on the
    Belfry account (``belfry_odoo_partner``) of a signed-in buyer whose address is proven."""
    if o.get("odoo_partner"):
        return int(o["odoo_partner"])
    pid = o.get("account")
    if pid:
        row = svc.odoo.read_one("res.partner", int(pid), ["email", "belfry_odoo_partner"]) or {}
        if row.get("email") and row.get("belfry_odoo_partner"):
            return int(row["belfry_odoo_partner"])
    return None


def _partner(svc: Services, remote: Json2Client, o: dict[str, Any], company: int | None) -> int:
    """The invoice's contact in Odoo. Never found by an e-mail address (the buyer's isn't
    proven): a person gets the contact Belfry stored for them, else a new one; an organisation
    is found by its VAT number only when VIES said it is valid, else a new one."""
    from . import sales

    b = o.get("billing") or {}
    org = b.get("kind") == "organisation"
    email = b.get("invoice_email") or o["email"]
    found: int | None = None
    if org and b.get("vat") and o.get("vat_valid"):
        rows = remote.search_read(
            "res.partner", [["vat", "=", b["vat"]], ["is_company", "=", True]], ["id"], limit=1
        )
        found = rows[0]["id"] if rows else None
    elif not org:
        found = _stored_partner(svc, o)
    if found is None:
        vals: dict[str, Any] = {
            "name": (b.get("name") if org else (b.get("name") or o["name"])) or o["name"],
            "email": email,
            "is_company": org,
        }
        for k in ("street", "street2", "zip", "city"):
            if b.get(k):
                vals[k] = b[k]
        country_id = None
        if b.get("country"):
            rows = remote.search_read("res.country", [["code", "=", b["country"]]], ["id"], limit=1)
            if rows:
                country_id = vals["country_id"] = rows[0]["id"]
        if b.get("region"):
            state = _state(remote, country_id, b["region"])
            if state:
                vals["state_id"] = state
            else:  # no such state in Odoo: the region goes on the city line
                vals["city"] = ", ".join(x for x in (b.get("city"), b["region"]) if x)
        if company:
            vals["company_id"] = company
        found = _create_partner(svc, remote, o, vals, b.get("vat") if org else "")
    if not org:
        sales.store(svc).update(o["id"], odoo_partner=found)
        if o.get("partner_id"):
            svc.odoo.write("res.partner", [int(o["partner_id"])], {"belfry_odoo_partner": found})
    return found


def _state(remote: Json2Client, country_id: int | None, region: str) -> int | None:
    """The res.country.state of COUNTRY_ID whose code or name is REGION (any case), or None.
    Never fails the order: Odoo refusing or not answering this search → None."""
    if not country_id:
        return None
    for field in ("code", "name"):
        try:
            rows = remote.search_read(
                "res.country.state",
                [["country_id", "=", country_id], [field, "=ilike", region]],
                ["id"],
                limit=1,
            )
        except (OdooError, httpx.HTTPError, OSError):
            log.info("sales: no state looked up for a billing region")
            return None
        if rows:
            return rows[0]["id"]
    return None


def _create_partner(
    svc: Services, remote: Json2Client, o: dict[str, Any], vals: dict[str, Any], vat: str
) -> int:
    """The new contact, with the organisation's VAT number or tax id in ``vat``. Odoo may refuse
    a number its VAT check (base_vat) doesn't know for that country: then the contact is made
    with the number in ``company_registry`` instead, and if Odoo refuses that too, without it.
    Either way the order records where the number went (``tax_id_on``) and the invoice prints it
    in its note (make_invoice) unless Odoo took it as the contact's VAT."""
    from . import sales

    tries: list[tuple[str, dict[str, Any]]] = []
    if vat:
        tries += [
            ("vat", {**vals, "vat": vat}),
            ("company_registry", {**vals, "company_registry": vat}),
        ]
    tries.append(("note" if vat else "", vals))
    for i, (where, v) in enumerate(tries):
        try:
            found = remote.create("res.partner", v)
        except OdooError as exc:
            if i == len(tries) - 1 or not _refused(exc):
                raise
            log.warning(
                "sales: order %s: Odoo refused the tax id in %s; trying another field",
                o["id"],
                where,
            )
            continue
        if vat:
            sales.store(svc).update(o["id"], tax_id_on=where)
            o["tax_id_on"] = where
        return found
    raise AssertionError("unreachable")


@_guard
def make_invoice(svc: Services, o: dict[str, Any]) -> None:
    """The order's posted invoice: its number, communication and totals read back."""
    from . import sales

    st = sales.settings(svc)
    remote = client(svc)
    ref = ref_of(svc, o["id"], int(o.get("invoice_round") or 1))
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
            "partner_id": _partner(svc, remote, o, st["company"]),
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
        b = o.get("billing") or {}
        if org and b.get("vat") and o.get("tax_id_on") in ("company_registry", "note"):
            # Odoo didn't take it as the contact's VAT: the invoice says it in its note
            label = (
                "VAT"
                if sales.vies_country("organisation", b.get("country", ""), b["vat"])
                else "Tax ID"
            )
            vals["narration"] = f"{label}: {html.escape(b['vat'])}"
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
    # no link into Odoo's portal (it would show the contact's other documents): the invoice's
    # page is Belfry's /pay/<order>, and the PDF goes with Belfry's own mail
    invoice = {
        "move_id": move["id"],
        "number": move["name"],
        "communication": move.get("payment_reference") or move["name"],
    }
    if o["state"] in ("paid", "active"):
        # the amount was paid: keep it; say so when this invoice (a second round) differs
        if total["gross"] != o["total"]["gross"]:
            o = sales.store(svc).order(o["id"]) or o
            # what was paid stands; never asked again of the buyer
            sales.store(svc).update(o["id"], flags=sales._flag(o, "odoo_total"))
        sales.store(svc).update(o["id"], invoice=invoice)
    else:
        sales.store(svc).update(o["id"], invoice=invoice, total=total, flags=flags)
    log.info("sales: order %s invoiced as %s", o["id"], move["name"])
    sales.after_invoice(svc, sales.store(svc).order(o["id"]) or o)


@_guard
def invoice_standing(svc: Services, move_id: int) -> bool:
    """Whether the invoice can still take a payment: posted, not reversed, no credit note made
    against it. Raises when Odoo doesn't answer (the caller tries again later)."""
    remote = client(svc)
    move = remote.read_one("account.move", move_id, ["state", "payment_state"])
    if not move or move.get("state") != "posted" or move.get("payment_state") == "reversed":
        return False
    refunds = remote.search_read(
        "account.move",
        [["move_type", "=", "out_refund"], ["reversed_entry_id", "=", move_id]],
        ["id", "state"],
        limit=5,
    )
    return not any(r.get("state") != "cancel" for r in refunds)


def invoice_pdf(svc: Services, move_id: int) -> bytes | None:
    """The invoice's PDF from Odoo (its ``invoice_pdf_report_id`` attachment; Odoo makes it
    when the invoice is "sent" without e-mail), or None. Odoo never mails it itself here."""
    import base64

    try:
        remote = client(svc)
        row = remote.read_one("account.move", move_id, ["invoice_pdf_report_id"]) or {}
        att = row.get("invoice_pdf_report_id")
        if not att:
            wizard = remote.create(
                "account.move.send.wizard", {"sending_methods": []}, context=_ctx(move_id)
            )
            remote.call(
                "account.move.send.wizard",
                "action_send_and_print",
                ids=[wizard],
                context=_ctx(move_id),
            )
            row = remote.read_one("account.move", move_id, ["invoice_pdf_report_id"]) or {}
            att = row.get("invoice_pdf_report_id")
        att_id = att[0] if isinstance(att, list | tuple) else att
        if not att_id:
            return None
        data = (remote.read_one("ir.attachment", int(att_id), ["datas"]) or {}).get("datas")
        pdf = base64.b64decode(data) if data else b""
        return pdf if pdf.startswith(b"%PDF") else None
    except (OdooError, httpx.HTTPError, OSError, ValueError, TypeError) as exc:
        log.warning("sales: no PDF of invoice %s: %s", move_id, type(exc).__name__)
        return None


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
        if move["state"] != "posted" or move["payment_state"] == "reversed":
            # credited or cancelled under us: nothing to pay on; the admins book it by hand
            if "payment_not_booked" not in flags:
                flags.append("payment_not_booked")
        elif move["payment_state"] in PAID:
            if paid.get("how") == "card" and "paid_twice" not in flags:
                # Odoo has it paid already by another payment (a transfer seen first): this card
                # payment is a second one
                flags.append("paid_twice")
        else:
            memo = " ".join(x for x in (move["name"], paid.get("ref") or "") if x)
            date = (paid.get("at") or sales.iso(sales.now()))[:10]
            vals: dict[str, Any] = {
                "journal_id": int(journal),
                "payment_date": date,
                "communication": memo,
            }
            if paid.get("amount") is not None:
                # the amount Stripe actually took, when it isn't what the invoice still asks
                took = int(paid["amount"])
                if took != _cents(move.get("amount_residual", move["amount_total"])):
                    vals["amount"] = took / 100
            wizard = remote.create("account.payment.register", vals, context=_ctx(int(move_id)))
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
def credit_note(svc: Services, o: dict[str, Any]) -> bool:
    """An order that ended unpaid: its invoice is credited in full (a draft is cancelled).
    False when the invoice was paid after all: it stands (the tick sees it and reopens)."""
    move_id = (o.get("invoice") or {}).get("move_id")
    if not move_id:
        return True
    remote = client(svc)
    move = _move(remote, int(move_id))
    if move["state"] == "draft":
        remote.call("account.move", "button_cancel", ids=[move["id"]])
        return True
    if move["payment_state"] in PAID:
        return False  # paid after all: not credited
    if move["state"] != "posted" or move["payment_state"] == "reversed":
        return True  # cancelled, or credited already
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
    return True


def read_open_invoices(svc: Services) -> dict[str, int]:
    """The tick: an invoice paid in Odoo (a transfer reconciled) → the order is paid (an ended
    order whose invoice was never credited reopens); the CURRENT invoice of an active order
    credited in full → its pass closes. Read in one call; when that fails, one by one, and an
    invoice that can't be read is flagged without holding the others up."""
    from . import core, sales

    st = sales.store(svc)
    watch: dict[int, dict[str, Any]] = {}
    for o in st.orders(("invoiced", "active", "cancelled", "expired")):
        inv = o.get("invoice") or {}
        if not inv.get("move_id") or o.get("closed"):
            continue
        if o["state"] in ("cancelled", "expired") and (inv.get("credited") or o.get("reopened")):
            continue
        watch[int(inv["move_id"])] = o
    if not watch:
        return {"paid": 0, "closed": 0, "unread": 0}
    remote = client(svc)
    fields = ["id", "payment_state", "state"]
    try:
        rows = remote.call("account.move", "read", ids=sorted(watch), fields=fields) or []
    except (OdooError, httpx.HTTPError, OSError):
        rows = []
        for mid, o in watch.items():
            try:
                got = remote.call("account.move", "read", ids=[mid], fields=fields) or []
            except (OdooError, httpx.HTTPError, OSError):
                got = []
            if not got and "odoo_read_failed" not in (o.get("flags") or []):
                st.update(o["id"], flags=sales._flag(o, "odoo_read_failed"))
            rows += got
    paid = closed = 0
    for r in rows:
        o = watch.get(r["id"])
        if o is None:
            continue
        try:
            if o["state"] != "active" and r.get("payment_state") in PAID:
                sales.mark_paid(svc, o["id"], "transfer")
                paid += 1
            elif (
                o["state"] == "active" and r.get("payment_state") == "reversed" and o.get("pass_id")
            ):
                core.revoke_pass(svc, int(o["pass_id"]), "credit note")
                st.update(o["id"], closed=sales.iso(sales.now()))
                log.info("sales: order %s: the invoice was credited, its pass closed", o["id"])
                closed += 1
        except Exception:  # noqa: BLE001 — the others go on; this one comes again
            log.exception("sales: order %s: its invoice's news not handled", o["id"])
    return {"paid": paid, "closed": closed, "unread": len(watch) - len(rows)}


def in_eu(vat: str) -> bool:
    return vat[:2] in EU


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
