"""``belfry.<domain>/sales``: selling passes, for the belfry's admins (docs/sales.md §2).

Odoo (connection, "Check", then the picks read from Odoo), bank, Stripe, products, codes, offers
and orders. Secrets are write-only: the page says set or not and their last 4 characters, and an
admin can clear one (back to the environment's value, if any). Every form carries the console's
csrf and answers with a fixed message (flash.py); nothing on the page comes from its URL.
"""

from __future__ import annotations

import html
import logging
import re
import secrets as pysecrets
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import console, flash, sales, salesodoo, salespay
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)
E = html.escape
PATH = "/sales"

STYLE = """<style>
.sales h2{margin:28px 0 6px;font-size:1.15rem}
.sales .state{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 4px}
.sales .chip{border:1px solid var(--border);border-radius:999px;padding:3px 10px;font-size:13px}
.sales .chip.ok{color:var(--ok,#15803d);border-color:currentColor}
.sales .card{border:1px solid var(--border);border-radius:12px;padding:14px;margin:10px 0}
.sales form.f{display:grid;gap:10px;max-width:560px}
.sales form.f label{display:grid;gap:4px;font-size:14px;font-weight:500}
.sales form.f .two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sales input[type=text],.sales input[type=password],.sales input[type=date],.sales input[type=number],.sales textarea,.sales select{width:100%;box-sizing:border-box;font:inherit;font-size:16px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg,transparent);color:inherit}
.sales textarea{min-height:70px}
.sales .hint{font-size:13px;color:var(--fg-muted);margin:0}
.sales .actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.sales .ghost{background:transparent;color:inherit;border:1px solid var(--border)}
.sales code{font-size:13px;word-break:break-all}
.sales details{border:1px solid var(--border);border-radius:12px;padding:10px 14px;margin:8px 0}
.sales details summary{font-weight:600}
.sales table td form{display:inline}
.sales .flags{font-size:12px;color:#92400e}
@media (max-width:600px){.sales form.f .two{grid-template-columns:1fr}}
</style>"""

FLAGS = {
    "vies_down": "VAT not checked (VIES down)",
    "odoo_total": "Odoo's invoice total differs from Belfry's (or from what was paid)",
    "card_failed": "no card session",
    "stripe_mismatch": "a Stripe payment that doesn't match",
    "payment_not_booked": "payment not booked in Odoo",
    "paid_late": "paid after it ended",
    "oversold": "no place was left",
    "not_me": "the address's owner said it wasn't them",
    "invoice_pdf_missing": "invoice mailed without its PDF",
    "odoo_read_failed": "Odoo could not read its invoice",
    "stuck": "a step gave up: look at it, then Try again",
    "slow": "its pass is slow to open: still trying every hour",
    "pass_closed": "its pass was closed",
    "paid_twice": "Paid twice: refund one in Stripe",
    "paid_old_amount": "paid through an older card session, for its own amount",
}

MSG = {
    "odoo_ok": "Odoo answers. Now pick the company, products, taxes and journals below.",
    "odoo_bad": "Odoo did not accept the connection. Check the URL, the database and the key.",
    "odoo_need": "Give the URL, the database and the API key.",
    "picks": "Saved.",
    "bank": "Bank details saved.",
    "bank_bad": "That IBAN or BIC is not valid.",
    "stripe": "Stripe saved.",
    "stripe_bad": "The key starts with rk_ or sk_; the webhook secret with whsec_.",
    "cleared": "Cleared.",
    "product": "Product saved.",
    "product_gone": "Product deleted.",
    "codes": "Codes made: they are at the top of the list.",
    "revoked": "Code revoked.",
    "decided": "Answer sent.",
    "cancelled": "Order cancelled.",
    "paid": "Order marked paid.",
    "reopened": "Order reopened and marked paid.",
    "retried": "Tried again: the order shows how it went.",
    "odoo_wait": "Odoo did not answer, so nothing changed. Try again in a moment.",
    "reason": "Give a reason.",
    "not_open": "This order is not open any more.",
    "expired": "The form expired; try again.",
}


def _opt(value: Any, label: str, current: Any) -> str:
    sel = " selected" if str(value) == str(current or "") else ""
    return f'<option value="{E(str(value))}"{sel}>{E(label)}</option>'


def _select(name: str, rows: list[dict[str, Any]], label_of, current: Any, empty: str) -> str:
    opts = _opt("", empty, current) + "".join(_opt(r["id"], label_of(r), current) for r in rows)
    return f'<select name="{E(name)}">{opts}</select>'


def _secret_line(svc: Services, name: str) -> str:
    st = sales.secret_state(svc, name)
    if not st["set"]:
        return '<span class="chip">not set</span>'
    tail = f" ··· {E(st['last4'])}" if st["last4"] else ""
    where = " (from the server's settings)" if st["from"] == "environment" else ""
    return f'<span class="chip ok">set{tail}</span><span class="hint">{where}</span>'


def _clear(csrf: str, name: str) -> str:
    return (
        f'<form method="post" action="{PATH}/clear" class="inline">'
        f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="secret" value="{E(name)}">'
        '<button class="btn ghost" type="submit">Clear</button></form>'
    )


def _money(c: int) -> str:
    return f"€{c / 100:,.2f}"


def _prices_text(p: dict[str, Any]) -> str:
    return "\n".join(
        f"{pr['amount'] / 100:g}" + (f" until {pr['until']}" if pr.get("until") else "")
        for pr in p.get("prices") or []
    )


def parse_prices(text: str) -> list[dict[str, Any]]:
    """Lines like ``200 until 2026-12-31`` then ``300``."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.fullmatch(r"(\d+(?:[.,]\d{1,2})?)(?:\s+until\s+(\d{4}-\d{2}-\d{2}))?", line)
        if not m:
            raise ValueError("Prices: one per line, like “200 until 2026-12-31”, then “300”.")
        out.append({"amount": m.group(1).replace(",", "."), "until": m.group(2)})
    return out


def product_form(csrf: str, p: dict[str, Any] | None) -> str:
    p = p or {}
    new = not p
    pid = (
        '<label>Id <input type="text" name="id" required pattern="[a-z0-9][a-z0-9-]*" '
        'placeholder="village-2027"></label>'
        if new
        else f'<input type="hidden" name="id" value="{E(p["id"])}">'
    )
    on = " checked" if p.get("on_sale", True) else ""
    qty = "" if p.get("quantity") is None else str(p.get("quantity"))
    return f"""<form method="post" action="{PATH}/product" class="f">
<input type="hidden" name="csrf" value="{csrf}">{pid}
<label>Name <input type="text" name="name" required value="{E(p.get("name", ""))}"></label>
<label>Description <textarea name="description">{E(p.get("description", ""))}</textarea></label>
<div class="two"><label>Pass tier <input type="text" name="tier" value="{E(p.get("tier", "member"))}"></label>
<label>Role (optional) <input type="text" name="role" value="{E(p.get("role") or "")}"></label></div>
<div class="two"><label>Pass from <input type="date" name="valid_from" value="{E(p.get("valid_from") or "")}"></label>
<label>Pass until <input type="date" name="valid_until" value="{E(p.get("valid_until") or "")}"></label></div>
<label>Prices <textarea name="prices" required placeholder="200 until 2026-12-31&#10;300">{E(_prices_text(p))}</textarea></label>
<p class="hint">One per line, in euros. A person pays this with VAT in it; an organisation pays it plus VAT.</p>
<div class="two"><label>VAT rate (%) <input type="text" inputmode="decimal" name="vat_rate" value="{E(str(p.get("vat_rate", 21)))}"></label>
<label>How many (empty: no limit) <input type="text" inputmode="numeric" name="quantity" value="{E(qty)}"></label></div>
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="on_sale" value="1"{on}> On sale</label>
<div class="actions"><button class="btn" type="submit">{"Add the product" if new else "Save"}</button></div></form>"""


def page_html(svc: Services, who: dict[str, Any], msg: str, err: str) -> str:
    csrf = E(who["csrf"])
    st = sales.settings(svc)
    s = sales.summary(svc)
    note = (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
        f'<p class="bad">{E(err)}</p>' if err else ""
    )

    def chip(ok: bool, text: str) -> str:
        return f'<span class="chip{" ok" if ok else ""}">{E(text)}</span>'

    state = (
        '<div class="state">'
        + chip(s["can_sell"], "Can sell paid passes" if s["can_sell"] else "Free passes only")
        + chip(s["odoo"], "Odoo")
        + chip(s["bank"], "Bank")
        + chip(s["stripe"], "Stripe")
        + "</div>"
    )
    # ── Odoo
    picks = ""
    if sales.odoo_ready(svc):
        try:
            lists = salesodoo.pick_lists(svc)
        except (OdooError, httpx.HTTPError, OSError):
            lists = None
        if lists is None:
            picks = '<p class="bad">Odoo did not answer just now; the picks show when it does.</p>'
        else:

            def tax(t: dict[str, Any]) -> str:
                return f"{t['name']} ({t['amount']:g}%)"

            prod_rows = (
                "".join(
                    f"<label>{E(p['name'])} is invoiced as "
                    + _select(
                        f"product_{p['id']}",
                        lists["products"],
                        lambda r: r["display_name"],
                        st["odoo_products"].get(p["id"]),
                        "— pick a product —",
                    )
                    + "</label>"
                    for p in sales.products(svc)
                )
                or '<p class="hint">Add a product below first.</p>'
            )
            picks = f"""<form method="post" action="{PATH}/picks" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>Company {_select("company", lists["companies"], lambda r: r["name"], st["company"], "— the key's default —")}</label>
{prod_rows}
<label>VAT for people (price includes it) {_select("tax_included", lists["taxes"], tax, st["tax_included"], "— pick a tax —")}</label>
<label>VAT for organisations (on top) {_select("tax_excluded", lists["taxes"], tax, st["tax_excluded"], "— pick a tax —")}</label>
<label>Journal for card payments {_select("card_journal", lists["journals"], lambda r: r["name"], st["card_journal"], "— not booked —")}</label>
<label>Journal for payments marked by hand {_select("manual_journal", lists["journals"], lambda r: r["name"], st["manual_journal"], "— not booked —")}</label>
<p class="hint">Belfry mails each invoice with its PDF to the buyer; Odoo doesn't send it.</p>
<div class="actions"><button class="btn" type="submit">Save</button></div></form>"""
    odoo = f"""<h2>Odoo</h2><p class="hint">Invoices, VAT and payments live in your Odoo (Odoo Online works).</p>
<div class="card"><form method="post" action="{PATH}/odoo" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>URL <input type="text" name="url" inputmode="url" placeholder="https://yourorg.odoo.com" value="{E(st["odoo_url"] or "")}"></label>
<label>Database <input type="text" name="db" value="{E(st["odoo_db"] or "")}"></label>
<label>API key <input type="password" name="key" autocomplete="off" placeholder="leave empty to keep the one set"></label>
<div class="actions">{_secret_line(svc, "odoo_api_key")}</div>
<div class="actions"><button class="btn" type="submit">Check</button></div></form>
<div class="actions" style="margin-top:8px">{_clear(csrf, "odoo_api_key") if sales.secret_state(svc, "odoo_api_key")["from"] == "console" else ""}</div>
{picks}</div>"""
    # ── bank
    bank = f"""<h2>Bank</h2><div class="card"><form method="post" action="{PATH}/bank" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>Account holder <input type="text" name="holder" value="{E(st["bank_holder"])}"></label>
<label>IBAN <input type="text" name="iban" value="{E(salespay.iban_spaced(st["bank_iban"]))}"></label>
<label>BIC <input type="text" name="bic" value="{E(st["bank_bic"])}"></label>
<div class="actions"><button class="btn" type="submit">Save</button>{'<a href="/pay">See the pay page</a>' if s["bank"] else ""}</div></form></div>"""
    # ── Stripe
    hook = console.console_url(svc.settings) + "/hooks/stripe"
    stripe = f"""<h2>Stripe</h2><div class="card"><form method="post" action="{PATH}/stripe" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>Restricted key <input type="password" name="key" autocomplete="off" placeholder="rk_live_…"></label>
<div class="actions">{_secret_line(svc, "stripe_key")}</div>
<label>Webhook signing secret <input type="password" name="webhook" autocomplete="off" placeholder="whsec_…"></label>
<div class="actions">{_secret_line(svc, "stripe_webhook_secret")}</div>
<p class="hint">At Stripe, add the webhook <code>{E(hook)}</code> with the events
<code>checkout.session.completed</code> and <code>checkout.session.async_payment_succeeded</code>.
The restricted key needs Checkout Sessions: write.</p>
<div class="actions"><button class="btn" type="submit">Save</button></div></form>
<div class="actions" style="margin-top:8px">{_clear(csrf, "stripe_key") if sales.secret_state(svc, "stripe_key")["from"] == "console" else ""}{_clear(csrf, "stripe_webhook_secret") if sales.secret_state(svc, "stripe_webhook_secret")["from"] == "console" else ""}</div></div>"""
    # ── products
    prods = ""
    for p in sales.products(svc):
        pub = sales.public_product(svc, p)
        price = _money(sales.cents(pub["price"]["amount"])) if pub else "no price now"
        left = sales.left(svc, p)
        sold = sales.taken(svc, p["id"])
        status = "on sale" if p.get("on_sale") else "off sale"
        prods += f"""<details><summary>{E(p["name"])} · {E(price)} · {sold} taken{f", {left} left" if left is not None else ""} · {status}</summary>
{product_form(csrf, p)}
<form method="post" action="{PATH}/product/delete" onsubmit="return confirm('Delete this product?')"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="id" value="{E(p["id"])}"><p><button class="btn ghost" type="submit">Delete</button></p></form></details>"""
    products = f"""<h2>Products</h2>{prods or '<p class="hint">No products yet.</p>'}
<details><summary>Add a product</summary>{product_form(csrf, None)}</details>"""
    # ── codes
    popts = _opt("", "any product", "") + "".join(
        _opt(p["id"], p["name"], "") for p in sales.products(svc)
    )
    crow = ""
    for c in sales.store(svc).codes()[:200]:
        r = sales.code_row(c)
        users = len(c["used"])
        act = (
            "revoked"
            if r["revoked"]
            else f"""<form method="post" action="{PATH}/code/revoke"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="code" value="{E(r["code"])}"><button class="btn ghost" type="submit">Revoke</button></form>"""
        )
        crow += (
            f"<tr><td><code>{E(r['code'])}</code></td><td>{r['percent']}% off</td>"
            f"<td>{E(r['product'] or 'any product')}</td><td>used {users} of {r['uses']}</td>"
            f"<td>{'until ' + E(r['expires']) if r['expires'] else ''}</td><td>{E(r['note'])}</td><td>{act}</td></tr>"
        )
    codes = f"""<h2>Codes</h2><details><summary>Make codes</summary>
<form method="post" action="{PATH}/codes" class="f"><input type="hidden" name="csrf" value="{csrf}">
<div class="two"><label>Percent off <input type="text" inputmode="numeric" name="percent" value="100"></label>
<label>For <select name="product">{popts}</select></label></div>
<div class="two"><label>Uses each <input type="text" inputmode="numeric" name="uses" value="1"></label>
<label>How many codes <input type="text" inputmode="numeric" name="count" value="1"></label></div>
<div class="two"><label>Expires (optional) <input type="date" name="expires"></label>
<label>Note <input type="text" name="note" placeholder="who it's for"></label></div>
<div class="actions"><button class="btn" type="submit">Make</button></div></form></details>
{f'<table class="list"><tr><th>Code</th><th>Off</th><th>For</th><th>Used</th><th>Expires</th><th>Note</th><th></th></tr>{crow}</table>' if crow else '<p class="hint">No codes yet.</p>'}"""
    # ── offers
    orow = ""
    for f in sales.store(svc).offers()[:100]:
        if f["state"] == "open":
            choices = "".join(
                _opt(v, "no" if v == 0 else f"{v}% off", f["percent"])
                for v in (0, 50, 60, 70, 75, 80, 90, 100)
            )
            act = f"""<form method="post" action="{PATH}/offer" class="f"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="id" value="{E(f["id"])}"><label>Answer <select name="percent">{choices}</select></label>
<label>Note (optional; public in the thread) <input type="text" name="note"></label><div class="actions"><button class="btn" type="submit">Send the answer</button></div></form>"""
        elif f["state"] == "confirm":
            act = '<p class="hint">Waiting for them to confirm their address: not public yet.</p>'
        else:
            act = (
                f"<p>{E(f['state'])}"
                + (f", {f.get('decided_percent')}% off" if f.get("decided_percent") else "")
                + "</p>"
            )
        chat = sales.offer_chat_link(svc, f)
        link = f' · <a href="{E(chat)}">the thread in the chat</a>' if chat else ""
        orow += f"""<div class="card"><p><b>{E(f["name"])}</b> · <span class="hint">{E(f["email"])} · {E(f["product"])} · asks {f["percent"]}% off · {E(f["created"][:10])}</span>{link}</p>
<p>{E(f["offer"])}</p>{act}</div>"""
    chans = sales.public_channels(svc)
    current = st.get("requests_channel") or ""
    if chans:
        pick = _select(
            "channel",
            [{"id": c["channel"], "label": f"{c['name']} (/{c['slug']})"} for c in chans],
            lambda r: r["label"],
            current,
            "— none: no offers in kind —",
        )
        chooser = f"""<form method="post" action="{PATH}/requests" class="f"><input type="hidden" name="csrf" value="{csrf}">
<label>Requests channel {pick}</label>
<p class="hint">Offers in kind are public there, like issues: the request and the person's name; their e-mail address is never shown on the public page. Members answer in the thread; only admins decide.</p>
<div class="actions"><button class="btn" type="submit">Save</button></div></form>"""
    else:
        chooser = (
            '<p class="hint">To take offers in kind, make a channel named <b>requests</b>, turn on '
            "its web page with the slug <b>requests</b> and make it public (the channel's settings "
            "in the chat). It then shows here.</p>"
        )
    offers = (
        f'<h2 id="offers">Offers in kind</h2>{chooser}{orow or "<p class=hint>No offers yet.</p>"}'
    )
    # ── orders
    rrow = ""
    for o in sales.store(svc).orders(limit=300):
        flags = ", ".join(FLAGS.get(x, x) for x in o.get("flags") or [])
        inv = (o.get("invoice") or {}).get("number") or ""
        act = ""
        if o["state"] in ("pending", "invoiced"):
            act = f"""<details><summary>Change</summary><form method="post" action="{PATH}/order" class="f">
<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{E(o["id"])}">
<label>Reason <input type="text" name="reason" required></label>
<div class="actions"><button class="btn" type="submit" name="do" value="paid">Mark paid</button>
<button class="btn ghost" type="submit" name="do" value="cancel">Cancel the order</button></div></form></details>"""
        elif o["state"] in ("cancelled", "expired") and o["total"]["gross"] > 0:
            # a transfer made in time but seen after the hold ended (or a cancel by mistake)
            act = f"""<details><summary>Change</summary><form method="post" action="{PATH}/order" class="f">
<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{E(o["id"])}">
<label>Reason <input type="text" name="reason" required></label>
<p class="hint">The pass opens; a new invoice is made if the first was credited.</p>
<div class="actions"><button class="btn" type="submit" name="do" value="reopen">Reopen and mark paid</button></div></form></details>"""
        if o.get("flags") or o.get("step") == "stuck" or o.get("next_try_at"):
            act += f"""<form method="post" action="{PATH}/order"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="id" value="{E(o["id"])}"><button class="btn ghost" type="submit" name="do" value="retry">Try again</button></form>"""
        rrow += (
            f"<tr><td>{E(o['created'][:10])}</td><td>{E(o['name'])}<br><span class='hint'>{E(o['email'])}</span></td>"
            f"<td>{E(o['product'])}</td><td>{_money(o['total']['gross'])}</td><td>{'invoice ' + E(inv) if inv else ''}</td>"
            f"<td>{E(o['state'])}{f'<br><span class=flags>{E(flags)}</span>' if flags else ''}</td><td>{act}</td></tr>"
        )
    orders = f"""<h2>Orders</h2>{f'<table class="list"><tr><th>Date</th><th>Who</th><th>Product</th><th>Total</th><th>Invoice</th><th>State</th><th></th></tr>{rrow}</table>' if rrow else '<p class="hint">No orders yet.</p>'}"""
    body = f"""{STYLE}<div class="sales"><h1>Sales</h1>
<p class="muted">Passes people buy: free with a code, or paid by card or bank transfer against an invoice in your Odoo.</p>
{note}{state}{products}{codes}{offers}{orders}{odoo}{bank}{stripe}</div>"""
    return console.frame("Sales", PATH, who, body)


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    async def who(request: Request) -> dict[str, Any] | None:
        s = svc(request)
        sess = console.read_session(s, request.cookies.get(console.COOKIE))
        return await run_in_threadpool(console.viewer, s, sess) if sess else None

    async def admin_form(request: Request) -> tuple[dict[str, Any] | None, Any]:
        form = await request.form()
        me = await who(request)
        ok = (
            me is not None
            and me["admin"]
            and pysecrets.compare_digest(str(form.get("csrf") or ""), me["csrf"] or "x")
        )
        return (me if ok else None), form

    def back(request: Request, msg: str = "", err: str = "", at: str = "") -> RedirectResponse:
        return flash.redirect(
            svc(request), PATH + (f"#{at}" if at else ""), MSG.get(msg, ""), MSG.get(err, err)
        )

    @app.get("/console/sales", response_class=HTMLResponse)
    async def page(request: Request):
        s = svc(request)
        me = await who(request)
        if me is None:
            return RedirectResponse("/login?to=/sales", status_code=303)
        if not me["admin"]:
            body = "<h1>Sales</h1><p>Only the belfry's admins see this page.</p>"
            return HTMLResponse(console.frame("Sales", PATH, me, body), status_code=403)
        if not sales.own(s):
            body = (
                "<h1>Sales</h1><p>This belfry keeps its register in Odoo: it sells through "
                "Odoo, not here.</p>"
            )
            return HTMLResponse(console.frame("Sales", PATH, me, body))
        msg, err = flash.peek(request, s)
        html_ = await run_in_threadpool(page_html, s, me, msg, err)
        return flash.clear(HTMLResponse(html_, headers={"Cache-Control": "no-store"}), request)

    def admin_only(fn):
        async def run(request: Request):
            me, form = await admin_form(request)
            if me is None:
                return back(request, err="expired")
            return await fn(request, me, form)

        run.__name__ = fn.__name__
        return run

    @app.post("/console/sales/odoo")
    @admin_only
    async def odoo(request: Request, me: dict[str, Any], form: Any):
        s = svc(request)
        url = str(form.get("url") or "").strip().rstrip("/")
        db = str(form.get("db") or "").strip()
        key = str(form.get("key") or "").strip()
        key = key or sales.secret(s, "odoo_api_key")
        if not (url.startswith("https://") and db and key) or len(url) > 200 or len(db) > 100:
            return back(request, err="odoo_need")
        try:
            await run_in_threadpool(salesodoo.check_connection, url, db, key)
        except (OdooError, httpx.HTTPError, OSError, ValueError):
            log.info("sales: an admin's Odoo check failed")
            return back(request, err="odoo_bad")
        await run_in_threadpool(sales.save_settings, s, {"odoo_url": url, "odoo_db": db})
        if str(form.get("key") or "").strip():
            await run_in_threadpool(sales.set_secret, s, "odoo_api_key", key)
        log.info("sales: %s set the Odoo connection", me["name"] or "an admin")
        return back(request, msg="odoo_ok")

    @app.post("/console/sales/picks")
    @admin_only
    async def picks(request: Request, me: dict[str, Any], form: Any):
        s = svc(request)

        def num(k: str) -> int | None:
            v = str(form.get(k) or "").strip()
            return int(v) if v.isdigit() else None

        products = {
            p["id"]: n for p in sales.products(s) if (n := num(f"product_{p['id']}")) is not None
        }
        await run_in_threadpool(
            sales.save_settings,
            s,
            {
                "company": num("company"),
                "odoo_products": products,
                "tax_included": num("tax_included"),
                "tax_excluded": num("tax_excluded"),
                "card_journal": num("card_journal"),
                "manual_journal": num("manual_journal"),
            },
        )
        return back(request, msg="picks")

    @app.post("/console/sales/bank")
    @admin_only
    async def bank(request: Request, me: dict[str, Any], form: Any):
        holder = " ".join(str(form.get("holder") or "").split())[:70]
        iban = salespay.clean_iban(str(form.get("iban") or ""))
        bic = str(form.get("bic") or "").strip().upper()
        if not holder or not salespay.iban_ok(iban) or (bic and not salespay.bic_ok(bic)):
            return back(request, err="bank_bad")
        await run_in_threadpool(
            sales.save_settings,
            svc(request),
            {"bank_holder": holder, "bank_iban": iban, "bank_bic": bic},
        )
        return back(request, msg="bank")

    @app.post("/console/sales/stripe")
    @admin_only
    async def stripe(request: Request, me: dict[str, Any], form: Any):
        s = svc(request)
        key = str(form.get("key") or "").strip()
        hook = str(form.get("webhook") or "").strip()
        if (key and not re.fullmatch(r"(rk|sk)_(test|live)_[A-Za-z0-9]{8,200}", key)) or (
            hook and not re.fullmatch(r"whsec_[A-Za-z0-9]{8,200}", hook)
        ):
            return back(request, err="stripe_bad")
        if key:
            await run_in_threadpool(sales.set_secret, s, "stripe_key", key)
        if hook:
            await run_in_threadpool(sales.set_secret, s, "stripe_webhook_secret", hook)
        return back(request, msg="stripe")

    @app.post("/console/sales/clear")
    @admin_only
    async def clear(request: Request, me: dict[str, Any], form: Any):
        name = str(form.get("secret") or "")
        if name not in sales.SECRET_KEYS:
            return back(request, err="expired")
        await run_in_threadpool(sales.set_secret, svc(request), name, None)
        return back(request, msg="cleared")

    @app.post("/console/sales/product")
    @admin_only
    async def product(request: Request, me: dict[str, Any], form: Any):
        try:
            data = {
                "id": form.get("id"),
                "name": form.get("name"),
                "description": form.get("description"),
                "tier": form.get("tier"),
                "role": form.get("role"),
                "valid_from": form.get("valid_from"),
                "valid_until": form.get("valid_until"),
                "prices": parse_prices(str(form.get("prices") or "")),
                "vat_rate": form.get("vat_rate") or 21,
                "quantity": form.get("quantity"),
                "on_sale": form.get("on_sale") == "1",
            }
            await run_in_threadpool(sales.save_product, svc(request), data)
        except ValueError as exc:
            # the message is one of clean_product's fixed sentences
            return back(request, err=str(exc))
        return back(request, msg="product")

    @app.post("/console/sales/product/delete")
    @admin_only
    async def product_delete(request: Request, me: dict[str, Any], form: Any):
        try:
            await run_in_threadpool(sales.delete_product, svc(request), str(form.get("id") or ""))
        except ValueError as exc:
            return back(request, err=str(exc))
        return back(request, msg="product_gone")

    @app.post("/console/sales/codes")
    @admin_only
    async def codes(request: Request, me: dict[str, Any], form: Any):
        from .holder_invites import ApiError

        try:
            await run_in_threadpool(
                sales.make_codes,
                svc(request),
                form.get("percent"),
                str(form.get("product") or "") or None,
                form.get("uses") or 1,
                form.get("count") or 1,
                str(form.get("expires") or "") or None,
                str(form.get("note") or ""),
                me["name"] or "an admin",
            )
        except ApiError as exc:
            return back(request, err=exc.message)
        return back(request, msg="codes")

    @app.post("/console/sales/code/revoke")
    @admin_only
    async def code_revoke(request: Request, me: dict[str, Any], form: Any):
        await run_in_threadpool(sales.revoke_code, svc(request), str(form.get("code") or ""))
        return back(request, msg="revoked")

    @app.post("/console/sales/requests")
    @admin_only
    async def requests_channel(request: Request, me: dict[str, Any], form: Any):
        s = svc(request)
        channel = str(form.get("channel") or "")
        ok = {c["channel"] for c in await run_in_threadpool(sales.public_channels, s)}
        if channel and channel not in ok:
            return back(request, err="expired")
        await run_in_threadpool(sales.save_settings, s, {"requests_channel": channel})
        return back(request, msg="picks", at="offers")

    @app.post("/console/sales/offer")
    @admin_only
    async def offer(request: Request, me: dict[str, Any], form: Any):
        from .holder_invites import ApiError

        try:
            await run_in_threadpool(
                sales.decide_offer,
                svc(request),
                str(form.get("id") or ""),
                form.get("percent"),
                str(form.get("note") or ""),
                me["name"] or "an admin",
            )
        except ApiError as exc:
            return back(request, err=exc.message, at="offers")
        return back(request, msg="decided", at="offers")

    @app.post("/console/sales/order")
    @admin_only
    async def order(request: Request, me: dict[str, Any], form: Any):
        s = svc(request)
        oid = str(form.get("id") or "")
        reason = " ".join(str(form.get("reason") or "").split())[:200]
        o = sales.store(s).order(oid) if sales.ORDER_RE.match(oid) else None
        if o is not None and form.get("do") == "retry":
            try:
                await run_in_threadpool(sales.try_again, s, oid)
            except Exception:  # noqa: BLE001 — the order says what is still wrong
                log.exception("sales: order %s: try again failed", oid)
            log.info("sales: order %s tried again by an admin", oid)
            return back(request, msg="retried")
        if not reason:
            return back(request, err="reason")
        by = me["name"] or "an admin"
        if o is not None and form.get("do") == "reopen" and o["state"] in ("cancelled", "expired"):
            try:
                await run_in_threadpool(sales.reopen_paid, s, oid, reason, by)
            except Exception:  # noqa: BLE001 — Odoo didn't say whether the invoice stands
                log.warning("sales: order %s: reopening waits for Odoo", oid)
                return back(request, err="odoo_wait")
            log.info("sales: order %s reopened and marked paid by hand", oid)
            return back(request, msg="reopened")
        if o is None or o["state"] not in ("pending", "invoiced"):
            return back(request, err="not_open")
        if form.get("do") == "paid":
            await run_in_threadpool(sales.mark_paid, s, oid, "hand", reason, by)
            log.info("sales: order %s marked paid by hand", oid)
            return back(request, msg="paid")
        await run_in_threadpool(sales.cancel, s, oid, reason, by)
        log.info("sales: order %s cancelled by an admin", oid)
        return back(request, msg="cancelled")


__all__ = ["register", "page_html", "parse_prices"]
