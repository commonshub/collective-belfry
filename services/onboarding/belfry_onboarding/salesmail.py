"""The mails of selling passes (sales.py), through an outbox: a mail is queued once per (kind,
reference), written from the order as it is when it leaves, and marked sent only when the mail
provider took it. The tick retries what failed (MAIL_TRIES at most). At most
MAILS_PER_ADDRESS_DAY sales mails a day go to one address; the others wait (MAIL_DAYS at most).

To an address that is not proven yet only these go: the receipt (with its confirmation link),
the order's confirmation (a free order), the invoice and its reminder, an offer's answer.
A confirmation link is only ever mailed to the address it confirms.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .core import Services

log = logging.getLogger(__name__)

Mail = tuple[str, list[str], tuple[str, str] | None, list[tuple[str, bytes, str]]]


def _money(c: int) -> str:
    return f"€{c / 100:,.2f}"


def _first(name: str) -> str:
    return (name or "").split(" ")[0] or "there"


def _mailer(svc: Services) -> Any:
    from .mailer import Mailer

    return getattr(svc.odoo, "mailer", None) or Mailer(svc.settings)


def confirm_url(svc: Services, oid: str, partner_id: int, address: str, kind: str) -> str:
    from . import console, sales

    token = sales.store(svc).confirm_token(oid, partner_id, address, kind)
    return f"{console.console_url(svc.settings)}/sales/confirm/{token}"


def not_me_line(svc: Services, o: dict[str, Any], address: str) -> str:
    """For every mail about an unpaid order: a link that cancels that order, for the owner of
    an address someone else typed."""
    url = confirm_url(svc, o["id"], 0, address, "cancel")
    return f"If you didn't order this, cancel it here: {url}"


CANCEL_KINDS = ("invoice", "reminder", "order_confirm")  # the mails with the cancel link


def _cancel_mailed(svc: Services, m: dict[str, Any]) -> None:
    """The address got a mail with this order's cancel link: from now on the order counts
    toward that address's limit (its owner can cancel it)."""
    from . import sales

    st = sales.store(svc)
    o = st.order(m["ref"].split(":")[0])
    if o and o["email"].strip().lower() == m["address"] and not o.get("cancel_mailed"):
        st.update(o["id"], cancel_mailed=True)


def queue(svc: Services, kind: str, ref: str, address: str) -> None:
    """Queue a mail (once per kind and reference) and try to send it now."""
    from . import sales

    st = sales.store(svc)
    if not address or not st.queue_mail(kind, ref, address):
        return
    for m in st.due_mails():
        if m["kind"] == kind and m["ref"] == ref:
            _deliver(svc, m)


def deliver_due(svc: Services) -> int:
    """The tick: mails that could not leave yet."""
    from . import sales

    n = 0
    for m in sales.store(svc).due_mails():
        n += 1 if _deliver(svc, m) else 0
    return n


def _deliver(svc: Services, m: dict[str, Any]) -> bool:
    """Send one mail, claimed first (the tick and a request never both send it). Kept due and
    tried again: over the address's daily limit (hourly), with no mail provider (hourly, until
    one is set), after a failure (each tick, then hourly after MAIL_TRIES); given up after
    MAIL_DAYS."""
    from . import sales

    st = sales.store(svc)
    if not st.claim_mail(m["id"]):
        return False
    now = time.time()
    old = now - m["created"] > sales.MAIL_DAYS * 86400
    hourly = int(now) + 3600
    if st.sent_to(m["address"]) >= sales.MAILS_PER_ADDRESS_DAY:
        if old:
            st.mail_done(m["id"], "limited", tried=False)
            log.warning("sales: a %s mail gave up: too many mails to its address", m["kind"])
        else:
            st.mail_done(m["id"], "due", tried=False, next_at=hourly)
        return False
    try:
        built = BUILD[m["kind"]](svc, m["ref"], m["address"])
    except Exception:  # noqa: BLE001 — counted as a try
        log.exception("sales: the %s mail could not be written", m["kind"])
        built = False
    if built is None:  # nothing to say any more (the order moved on)
        st.mail_done(m["id"], "dropped", tried=False)
        return False
    if built:
        built = _mark_test(svc, m, built)
    mailer = _mailer(svc)
    if built and not getattr(mailer.s, "smtp_host", ""):
        # no mail provider yet: kept, tried again hourly until one is set
        subject, lines, button, files = built
        mailer.send(m["address"], subject, lines, button=button, attachments=files)
        st.mail_done(
            m["id"], "no_provider" if old else "due", tried=False, next_at=None if old else hourly
        )
        return False
    ok = False
    if built:
        subject, lines, button, files = built
        ok = mailer.send(m["address"], subject, lines, button=button, attachments=files)
    if ok:
        st.mail_done(m["id"], "sent")
        if m["kind"] in CANCEL_KINDS:
            _cancel_mailed(svc, m)
        return True
    if old:
        st.mail_done(m["id"], "failed")
        log.warning("sales: a %s mail gave up after %s days", m["kind"], sales.MAIL_DAYS)
    elif m["tries"] + 1 >= sales.MAIL_TRIES:
        st.mail_done(m["id"], "due", next_at=hourly)
    else:
        st.mail_done(m["id"], "due")
    return False


TEST_LINE = (
    "TEST ORDER: made through the test environment (Stripe test mode, test Odoo). No real money."
)


def _mark_test(svc: Services, m: dict[str, Any], built: Mail) -> Mail:
    """A test order's mails say so, in their subject and first line (fixed text)."""
    from . import sales

    if m["kind"] not in ("receipt", "order_confirm", "invoice", "reminder"):
        return built
    o = _order(svc, str(m["ref"]).split(":", 1)[0])
    if sales.env_of(o) != "test":
        return built
    subject, lines, button, files = built
    return f"[TEST] {subject}", [TEST_LINE, *lines], button, files


# ── what each mail says ─────────────────────────────────────────────────────────────────────
def _order(svc: Services, oid: str) -> dict[str, Any] | None:
    from . import sales

    return sales.store(svc).order(oid.split(":")[0])


def _receipt(svc: Services, oid: str, address: str) -> Mail | None:
    from . import sales

    o = _order(svc, oid)
    if o is None or o["state"] != "active":
        return None
    name = _mailer(svc).name
    lines = [f"Hello {_first(o['name'])},", f"Your {o['product_name']} for {name} is ready."]
    if o["total"]["gross"]:
        inv = (o.get("invoice") or {}).get("number")
        lines.append(
            f"Paid: {_money(o['total']['gross'])}" + (f" (invoice {inv})." if inv else ".")
        )
    if o.get("valid_from") or o.get("valid_until"):
        lines.append(f"Valid {o.get('valid_from') or 'now'} to {o.get('valid_until') or '—'}.")
    files = _pdf(svc, o)
    pid = o.get("partner_id")
    if pid and sales.is_unverified(svc, pid):
        row = svc.odoo.read_one("res.partner", pid, ["belfry_pending_email"]) or {}
        pending = (row.get("belfry_pending_email") or "").strip().lower()
        if pending and pending == address == o["email"].strip().lower():
            lines.append(
                "One more step: confirm this is your address. Then you can sign in from any "
                "device with it. If you didn't order this, the same link lets you say so."
            )
            url = confirm_url(svc, o["id"], pid, pending, "receipt")
            return (
                f"Your {o['product_name']}: confirm your e-mail",
                lines,
                (url, "Confirm your e-mail"),
                files,
            )
    if o.get("ticket"):  # an event ticket: no pass, the event itself
        lines.append("Sign in with your address to see the event and your ticket.")
        return f"Your {o['product_name']}", lines, None, files
    lines.append("Sign in with your address to see your pass in the apps.")
    return f"Your {o['product_name']}", lines, None, files


def _pdf(svc: Services, o: dict[str, Any]) -> list[tuple[str, bytes, str]]:
    """The order's invoice PDF from Odoo, when it has an invoice (else nothing; flagged when it
    has one but Odoo gave no PDF)."""
    from . import sales, salesodoo

    inv = o.get("invoice") or {}
    if not inv.get("move_id"):
        return []
    pdf = salesodoo.invoice_pdf(svc, int(inv["move_id"]), sales.env_of(o))
    if pdf:
        return [(f"{inv['number'].replace('/', '-')}.pdf", pdf, "application/pdf")]
    st = sales.store(svc)
    o = st.order(o["id"]) or o
    st.update(o["id"], flags=sales._flag(o, "invoice_pdf_missing"))
    return []


def _order_confirm(svc: Services, oid: str, address: str) -> Mail | None:
    o = _order(svc, oid)
    if o is None or o["state"] != "confirm" or o["email"].strip().lower() != address:
        return None
    name = _mailer(svc).name
    url = confirm_url(svc, o["id"], 0, address, "order")
    return (
        f"Confirm your {o['product_name']}",
        [
            f"Hello {_first(o['name'])},",
            f"Someone asked for a {o['product_name']} for {name} with this address. "
            "Confirm it to get your pass.",
            f"The link works until {o['hold_until'][:16].replace('T', ' ')} (UTC). If it "
            "wasn't you, the same link lets you say so, or ignore this mail.",
            not_me_line(svc, o, address),
        ],
        (url, "Confirm"),
        [],
    )


def _held(o: dict[str, Any]) -> str:
    return f"Your place is held until {o['hold_until'][:10]}."


def _invoice(svc: Services, ref: str, address: str) -> Mail | None:
    """The invoice: its number, amount, how to pay (the bank page when bank details are set,
    else the card link), the PDF from Odoo when it could be fetched."""
    from . import sales

    o = _order(svc, ref)
    inv = (o or {}).get("invoice") or {}
    if o is None or not inv.get("number"):
        return None
    name = _mailer(svc).name
    paid = o["state"] in ("paid", "active")
    lines = [
        f"Hello {_first(o['name'])},",
        f"Thank you for your order of a {o['product_name']} for {name}.",
        f"Invoice {inv['number']}: {_money(o['total']['gross'])}" + (" (paid)." if paid else "."),
    ]
    button = None
    if not paid:
        if sales.bank_ready(svc):
            lines.append(
                "To pay by bank transfer, use the communication "
                f"{inv.get('communication') or inv['number']}. " + _held(o)
            )
            button = (sales.pay_url(svc, o["id"]), "How to pay")
        elif (o.get("stripe") or {}).get("url"):
            lines.append(_held(o))
            button = (o["stripe"]["url"], "Pay by card")
        else:
            lines.append(_held(o))
        if address == o["email"].strip().lower():
            lines.append(not_me_line(svc, o, address))
    return f"Invoice {inv['number']}", lines, button, _pdf(svc, o)


def _reminder(svc: Services, oid: str, address: str) -> Mail | None:
    """The one reminder, 2 days before the hold ends: for an invoice, and for a card order
    (no invoice yet: its card link, and the transfer instead), with the cancel link."""
    from . import sales

    o = _order(svc, oid)
    inv = (o or {}).get("invoice") or {}
    if o is None or o["state"] not in ("pending", "invoiced") or not o["total"]["gross"]:
        return None
    if not inv.get("number") and o.get("pay") != "card":
        return None
    card = (o.get("stripe") or {}).get("url")
    if sales.bank_ready(svc):
        button = (sales.pay_url(svc, o["id"]), "How to pay")
    elif card:
        button = (card, "Pay by card")
    else:
        button = None
    what = (
        f"(invoice {inv['number']}, {_money(o['total']['gross'])})"
        if inv.get("number")
        else f"({_money(o['total']['gross'])})"
    )
    return (
        f"Your place is held until {o['hold_until'][:10]}",
        [
            f"Hello {_first(o['name'])},",
            f"Your {o['product_name']} is not paid yet {what}.",
            f"{_held(o)} After that the order ends. A transfer made before then still counts: "
            "it can take a few days to show.",
            *([not_me_line(svc, o, address)] if address == o["email"].strip().lower() else []),
        ],
        button,
        [],
    )


def _offer_answer(svc: Services, ref: str, address: str) -> Mail | None:
    from . import sales

    f = sales.store(svc).offer(ref)
    if f is None or f["state"] not in ("accepted", "declined"):
        return None
    name = _mailer(svc).name
    if f.get("code"):
        lines = [
            f"Hello {_first(f['name'])},",
            f"Thank you for your offer. {name} gives you {f['decided_percent']}% off.",
            f"Your personal code: {f['code']}",
            "Type it when you get your pass. It works once.",
        ]
    else:
        lines = [
            f"Hello {_first(f['name'])},",
            f"Thank you for your offer. {name} can't take it this time.",
        ]
    if f.get("note"):
        lines.append(f["note"])
    lines.append(
        "The answer is in your request's thread too, without the code: the code is only in this "
        "mail, and works only for you."
    )
    button = (f["my_url"], "Open your request") if f.get("my_url") else None
    return f"Your offer to {name}", lines, button, []


BUILD = {
    "receipt": _receipt,
    "order_confirm": _order_confirm,
    "invoice": _invoice,
    "reminder": _reminder,
    "offer_answer": _offer_answer,
}


def offer_answer(svc: Services, f: dict[str, Any]) -> None:
    queue(svc, "offer_answer", f["id"], f["email"])
