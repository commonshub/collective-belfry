"""The mails of selling passes (sales.py). To an address that is not proven yet only these go:
the receipt (with its confirmation link), the invoice and its reminder, an offer's answer."""

from __future__ import annotations

import logging
from typing import Any

from .core import Services

log = logging.getLogger(__name__)


def _money(c: int) -> str:
    return f"€{c / 100:,.2f}"


def _first(name: str) -> str:
    return (name or "").split(" ")[0] or "there"


def confirm_url(svc: Services, oid: str) -> str:
    from . import console, sales

    token = sales.store(svc).confirm_token(oid)
    return f"{console.console_url(svc.settings)}/sales/confirm/{token}"


def receipt(svc: Services, o: dict[str, Any]) -> None:
    from . import sales

    m = svc.odoo.mailer if hasattr(svc.odoo, "mailer") else None
    if m is None:
        return
    unproven = bool(o.get("partner_id")) and sales.is_unverified(svc, o["partner_id"])
    lines = [
        f"Hello {_first(o['name'])},",
        f"Your {o['product_name']} for {m.name} is ready.",
    ]
    if o["total"]["gross"]:
        inv = (o.get("invoice") or {}).get("number")
        lines.append(
            f"Paid: {_money(o['total']['gross'])}" + (f" (invoice {inv})." if inv else ".")
        )
    if o.get("valid_from") or o.get("valid_until"):
        lines.append(f"Valid {o.get('valid_from') or 'now'} to {o.get('valid_until') or '—'}.")
    if unproven:
        lines.append(
            "One more step: confirm this is your address. Then you can sign in from any "
            "device with it."
        )
        m.send(
            o["email"],
            f"Your {o['product_name']}: confirm your e-mail",
            lines,
            button=(confirm_url(svc, o["id"]), "Confirm your e-mail"),
        )
    else:
        lines.append("Sign in with your address to see your pass in the apps.")
        m.send(o["email"], f"Your {o['product_name']}", lines)
    log.info("sales: receipt of order %s mailed", o["id"])


def invoice(svc: Services, o: dict[str, Any]) -> None:
    """Pay by transfer: the invoice's number, amount and where the bank details are."""
    from . import sales

    m = getattr(svc.odoo, "mailer", None)
    inv = o.get("invoice") or {}
    if m is None or not inv.get("number"):
        return
    lines = [
        f"Hello {_first(o['name'])},",
        f"Thank you for your order of a {o['product_name']} for {m.name}.",
        f"Invoice {inv['number']}: {_money(o['total']['gross'])}.",
        "Please pay it by bank transfer with the communication "
        f"{inv.get('communication') or inv['number']}"
        f" before {o['hold_until'][:10]}. Your place is held until then.",
    ]
    to = (o.get("billing") or {}).get("invoice_email") or o["email"]
    m.send(
        to, f"Invoice {inv['number']}", lines, button=(sales.pay_url(svc, o["id"]), "How to pay")
    )


def reminder(svc: Services, o: dict[str, Any]) -> None:
    from . import sales

    m = getattr(svc.odoo, "mailer", None)
    inv = o.get("invoice") or {}
    if m is None or not inv.get("number"):
        return
    m.send(
        o["email"],
        f"Your place is held until {o['hold_until'][:10]}",
        [
            f"Hello {_first(o['name'])},",
            f"Your {o['product_name']} is not paid yet (invoice {inv['number']}, "
            f"{_money(o['total']['gross'])}).",
            f"Your place is held until {o['hold_until'][:10]}; after that the order ends.",
        ],
        button=(sales.pay_url(svc, o["id"]), "How to pay"),
    )


def _admins(svc: Services) -> list[str]:
    from . import console

    try:
        return sorted(console.admin_emails(svc))
    except Exception:  # noqa: BLE001
        return []


def offer_to_admins(svc: Services, f: dict[str, Any]) -> None:
    from . import console

    m = getattr(svc.odoo, "mailer", None)
    admins = _admins(svc)
    if m is None or not admins:
        return
    m.send(
        admins,
        f"{f['name']} offers something for a pass",
        [
            f"{f['name']} asks for {f['percent']}% off the {f['product']} pass and offers:",
            f["offer"],
            "Decide in the console.",
        ],
        button=(console.console_url(svc.settings) + "/sales#offers", "Open the offers"),
    )


def offer_answer(svc: Services, f: dict[str, Any]) -> None:
    m = getattr(svc.odoo, "mailer", None)
    if m is None:
        return
    if f.get("code"):
        lines = [
            f"Hello {_first(f['name'])},",
            f"Thank you for your offer. {m.name} gives you {f['decided_percent']}% off.",
            f"Your personal code: {f['code']}",
            "Type it when you get your pass. It works once.",
        ]
    else:
        lines = [
            f"Hello {_first(f['name'])},",
            f"Thank you for your offer. {m.name} can't take it this time.",
        ]
    if f.get("note"):
        lines.append(f["note"])
    m.send(f["email"], f"Your offer to {m.name}", lines)
