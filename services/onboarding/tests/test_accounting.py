"""Booking tickets at the organisation that sells them (accounting.py), against a fake of its
Odoo's JSON-2 API."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from belfry_onboarding import accounting
from belfry_onboarding.accounting import Accounting


class FakeIssuer:
    """The issuer's Odoo, as far as accounting.py uses it."""

    def __init__(self) -> None:
        self.records: dict[str, dict[int, dict[str, Any]]] = {}
        self.seq = 100
        self.calls: list[tuple[str, str]] = []
        self.sent: list[int] = []
        self.add("res.currency", {"name": "EUR"})
        self.add("res.country", {"code": "BE"})

    def add(self, model: str, vals: dict[str, Any]) -> int:
        self.seq += 1
        self.records.setdefault(model, {})[self.seq] = {"id": self.seq, **vals}
        return self.seq

    def _match(self, row: dict[str, Any], domain: list[Any]) -> bool:
        for field, op, value in domain:
            have = row.get(field)
            if op == "=" and have != value:
                return False
            if op == "!=" and have == value:
                return False
            if op == "=ilike" and str(have or "").lower() != str(value).lower():
                return False
        return True

    def search_read(self, model, domain, fields, limit=None):
        self.calls.append((model, "search_read"))
        rows = [r for r in self.records.get(model, {}).values() if self._match(r, domain)]
        return [dict(r) for r in rows[:limit]]

    def read_one(self, model, rec_id, fields):
        row = self.records.get(model, {}).get(rec_id)
        return dict(row) if row else None

    def create(self, model, vals, context=None):
        self.calls.append((model, "create"))
        if model == "account.move":
            total = 0.0
            for _c, _z, line in vals["invoice_line_ids"]:
                total += line["quantity"] * line["price_unit"] * (1 - line["discount"] / 100)
            vals = {
                **vals,
                "name": False,
                "state": "draft",
                "amount_total": round(total, 2),
                "payment_state": "not_paid",
                "is_move_sent": False,
                "journal_id": [1, "INV"],
            }
        return self.add(model, {**vals, "context": context or {}})

    def call(self, model, method, ids=None, context=None, **params):
        self.calls.append((model, method))
        moves = self.records["account.move"]
        if method == "action_post":
            move = moves[ids[0]]
            prefix = "RINV" if move["move_type"] == "out_refund" else "INV"
            move.update(state="posted", name=f"{prefix}/2027/{ids[0]:05d}")
        elif method == "action_create_payments":
            moves[context["active_ids"][0]]["payment_state"] = "paid"
        elif method == "action_send_and_print":
            moves[context["active_ids"][0]]["is_move_sent"] = True
            self.sent.append(context["active_ids"][0])
        elif method == "refund_moves":
            original = moves[context["active_ids"][0]]
            credit = self.add(
                "account.move",
                {
                    "move_type": "out_refund",
                    "reversed_entry_id": original["id"],
                    "name": False,
                    "state": "draft",
                    "amount_total": original["amount_total"],
                    "payment_state": "not_paid",
                    "is_move_sent": False,
                    "journal_id": [1, "INV"],
                },
            )
            return {"res_id": credit}
        return True


def acc(issuer: FakeIssuer, **kw: Any) -> Accounting:
    return Accounting(
        remote=issuer,
        issuer="XL Collective",
        products={"week": 12, "*": 14},
        tax_included=21,
        tax_excluded=22,
        journal=7,
        manual_journal=None,
        ref_prefix="Belfry ",
        send=True,
        **kw,
    )


def export(
    total: float = 121.0, tax: str = "included", provider: str = "stripe", refunded: float = 0.0
) -> dict[str, Any]:
    return {
        "pass_id": 5,
        "state": "active",
        "invoice": {
            "id": 9,
            "name": "INV/2026/00009",
            "date": "2026-10-31",
            "state": "posted",
            "payment_state": "paid",
            "amount_total": total,
            "currency": "EUR",
        },
        "partner": {
            "name": "Dana",
            "email": "dana@example.org",
            "is_company": False,
            "country_code": "BE",
        },
        "lines": [
            {
                "name": "Week pass",
                "tier": "week",
                "quantity": 1,
                "price_unit": total,
                "discount": 0,
                "price_total": total,
                "tax": tax,
                "tax_amount": 21,
            }
        ],
        "payment": {"date": "2026-10-31", "provider": provider, "reference": "pi_123"},
        "refunded": refunded,
        "refund_date": "2026-11-02" if refunded else False,
    }


def test_books_posts_pays_and_sends_once() -> None:
    issuer = FakeIssuer()
    move = accounting.book_invoice(acc(issuer), export())
    assert move["state"] == "posted" and move["payment_state"] == "paid"
    assert move["is_move_sent"] and issuer.sent == [move["id"]]
    stored = issuer.records["account.move"][move["id"]]
    assert stored["ref"] == "Belfry INV/2026/00009"
    assert stored["invoice_line_ids"][0][2]["tax_ids"] == [[6, 0, [21]]]
    partner = issuer.records["res.partner"][stored["partner_id"]]
    assert partner["email"] == "dana@example.org" and partner["country_id"]
    # again: found by its ref, nothing new is created, paid or sent
    calls = len(issuer.calls)
    again = accounting.book_invoice(acc(issuer), export())
    assert again["id"] == move["id"]
    assert not [c for c in issuer.calls[calls:] if c[1] in ("create", "action_post")]
    assert issuer.sent == [move["id"]]


def test_a_known_buyer_is_reused() -> None:
    issuer = FakeIssuer()
    known = issuer.add("res.partner", {"name": "Dana D.", "email": "DANA@example.org"})
    move = accounting.book_invoice(acc(issuer), export())
    assert issuer.records["account.move"][move["id"]]["partner_id"] == known


def test_a_different_total_stays_a_draft() -> None:
    issuer = FakeIssuer()
    data = export()
    data["lines"][0]["price_unit"] = 100.0  # the issuer's total would differ from Belfry's 121
    try:
        accounting.book_invoice(acc(issuer), data)
    except accounting.BookingError as exc:
        assert "left as a draft" in str(exc)
    else:
        raise AssertionError("no BookingError")
    (move,) = issuer.records["account.move"].values()
    assert move["state"] == "draft"


def test_missing_mapping_is_an_error() -> None:
    issuer = FakeIssuer()
    try:
        accounting.book_invoice(
            acc(
                issuer,
            ),
            export(tax="several"),
        )
    except accounting.BookingError as exc:
        assert "no tax" in str(exc)
    else:
        raise AssertionError("no BookingError")


def test_manual_payment_without_journal_stays_open() -> None:
    issuer = FakeIssuer()
    move = accounting.book_invoice(acc(issuer), export(provider="manual"))
    assert move["state"] == "posted" and move["payment_state"] == "not_paid"


def test_credit_note_for_a_full_refund() -> None:
    issuer = FakeIssuer()
    move = accounting.book_invoice(acc(issuer), export())
    credit = accounting.book_credit_note(acc(issuer), move["id"], export(refunded=121.0))
    assert credit["state"] == "posted" and credit["payment_state"] == "paid"
    assert credit["name"].startswith("RINV/")
    again = accounting.book_credit_note(acc(issuer), move["id"], export(refunded=121.0))
    assert again["id"] == credit["id"]


class PassOdoo:
    """Belfry's Odoo for book_pass: one pass, its export and the written result."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.row: dict[str, Any] = {
            "id": 5,
            "state": "active",
            "invoice_id": [9, "INV"],
            "accounting_state": False,
            "accounting_move_id": 0,
            "accounting_refund_move_id": 0,
        }

    def read_one(self, model, rec_id, fields):
        return dict(self.row)

    def call(self, model, method, args):
        assert method == "belfry_accounting_export"
        return self.data

    def write(self, model, ids, vals):
        self.row.update(vals)
        return True


def test_book_pass_keeps_the_result_on_the_pass() -> None:
    issuer = FakeIssuer()
    odoo = PassOdoo(export())
    svc = SimpleNamespace(accounting=acc(issuer), odoo=odoo)
    assert accounting.book_pass(svc, 5) == "booked"
    assert odoo.row["accounting_state"] == "done"
    assert odoo.row["accounting_ref"].startswith("INV/2027/")
    assert accounting.book_pass(svc, 5) == "already"
    odoo.data = export(refunded=121.0)
    assert accounting.refund_pass(svc, 5) == "credited"
    assert odoo.row["accounting_state"] == "refunded" and odoo.row["accounting_refund_ref"]


def test_book_pass_failure_is_recorded_and_free_tickets_skipped() -> None:
    issuer = FakeIssuer()
    odoo = PassOdoo(export(tax="several"))
    svc = SimpleNamespace(accounting=acc(issuer), odoo=odoo)
    assert accounting.book_pass(svc, 5) == "failed"
    assert odoo.row["accounting_state"] == "failed" and "no tax" in odoo.row["accounting_error"]
    odoo.data = export(total=0.0)
    assert accounting.book_pass(svc, 5) == "skipped"


def test_partial_refund_needs_the_accountant() -> None:
    issuer = FakeIssuer()
    odoo = PassOdoo(export())
    svc = SimpleNamespace(accounting=acc(issuer), odoo=odoo)
    accounting.book_pass(svc, 5)
    odoo.data = export(refunded=50.0)
    assert accounting.refund_pass(svc, 5) == "attention"
    assert "partial refund" in odoo.row["accounting_error"]


def test_parse_ids() -> None:
    assert accounting.parse_ids("day=11, week=12,*=14") == {"day": 11, "week": 12, "*": 14}
    assert accounting.parse_ids("") == {}
