"""``belfry.<domain>/plugins``: the services a belfry plugs into, for its admins (docs/sales.md §2
and §9). Three plugins, each with its own page:

- **Stripe** (``/plugins/stripe``): Production and Test, each with its own restricted key and
  webhook signing secret, and its own webhook URL (``/hooks/stripe`` and ``/hooks/stripe/test``).
  Production refuses a test key (``sk_test_`` / ``rk_test_``), Test refuses a live one.
- **Odoo** (``/plugins/odoo``): Production and Test, each a complete connection (URL, database,
  API key, "Check") and its own picks read from that database (company, products, taxes,
  journals): a test database has other ids, so nothing Odoo-specific is shared.
- **E-mail** (``/plugins/email``): the mail provider (Lettermint or Resend), the same form as the
  Setup page (setup.mail_row); it saves through ``POST /setup``. No test environment.

The test environments are used only by testers' orders (testers.py). Production keeps the
settings and secrets it had before this page existed (``belfry.sales.settings`` and the
unprefixed names in ``sales-secrets.json``); the test ones are ``belfry.sales.settings.test`` and
``test_<name>``. Secrets are write-only: set or not, and their last 4 characters. Every form
carries the console's csrf and answers with a fixed message (flash.py); nothing on these pages
comes from their URL except the environment, which is one of two fixed words.
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

from . import console, flash, sales, salesconsole, salesodoo, testers
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)
E = html.escape
PATH = "/plugins"
ENV_TITLE = {"live": "Production", "test": "Test"}
STRIPE_KEY = re.compile(r"^(rk|sk)_(test|live)_[A-Za-z0-9]{8,200}$")
WEBHOOK = re.compile(r"^whsec_[A-Za-z0-9]{8,200}$")

MSG = {
    "odoo_ok": "Odoo answers. Now pick the company, products, taxes and journals below.",
    "odoo_ok_no_ticket": "Odoo answers, but it did not let Belfry create the product “Event "
    "ticket” (the API key's user may not create products). Create a service product in Odoo and "
    "pick it below as the product event tickets are invoiced as.",
    "picks_no_ticket": "Saved. Odoo did not let Belfry create the product “Event ticket”: "
    "create a service product in Odoo and pick it as the product event tickets are invoiced as.",
    "odoo_bad": "Odoo did not accept the connection. Check the URL, the database and the key.",
    "odoo_need": "Give the URL, the database and the API key.",
    "odoo_same": "The test Odoo must be another database than production's.",
    "picks": "Saved.",
    "stripe": "Stripe saved.",
    "stripe_bad": "The key starts with rk_ or sk_; the webhook secret with whsec_.",
    "stripe_test_in_live": "That is a test key (…_test_…). Production takes a live key; put "
    "test keys in the Test section.",
    "stripe_live_in_test": "That is a live key (…_live_…). The Test section takes only a test "
    "key, so no real money can move there.",
    "cleared": "Cleared.",
    "expired": "The form expired; try again.",
}

STYLE = """<style>
.plugins .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:16px 0}
.plugins .pcard{display:flex;flex-direction:column;gap:6px;padding:16px;border:1px solid var(--border);border-radius:14px;
background:var(--surface);color:inherit;text-decoration:none}
.plugins .pcard:hover,.plugins .pcard:focus-visible{border-color:var(--accent)}
.plugins .pcard strong{font-size:1.05rem}
.plugins .st{font-size:.9rem;color:var(--fg-muted)} .plugins .st.ok{color:var(--ok,#15803d)} .plugins .st.warn{color:#92400e}
.plugins .env{border:1px solid var(--border);border-radius:14px;padding:4px 16px 16px;margin:16px 0}
.plugins .env.test{border-style:dashed}
.plugins .env h2{display:flex;align-items:center;gap:8px}
.plugins .badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;padding:2px 7px;border-radius:6px}
.plugins .badge.live{background:color-mix(in srgb,var(--ok,#15803d) 16%,transparent);color:var(--ok,#15803d)}
.plugins .badge.test{background:#fef3c7;color:#78350f}
.plugins .missing{margin:6px 0 0;padding-left:18px;font-size:.92rem}
</style>"""


# ── state ───────────────────────────────────────────────────────────────────────────────────
def _test_key(svc: Services, env: str) -> bool:
    return bool(re.match(r"^(sk|rk)_test_", sales.secret(svc, "stripe_key", env)))


def stripe_missing(svc: Services, env: str) -> list[str]:
    out = []
    if not sales.secret(svc, "stripe_key", env):
        out.append("the restricted key")
    if not sales.secret(svc, "stripe_webhook_secret", env):
        out.append("the webhook signing secret")
    return out


def odoo_missing(svc: Services, env: str) -> list[str]:
    st = sales.settings(svc, env)
    out = []
    if not sales.odoo_ready(svc, env):
        out.append("the connection (URL, database, API key)")
        return out
    if not (st["odoo_products"] or st["fallback_products"]):
        out.append("the Odoo product each Belfry product is invoiced as")
    if not st["tax_included"] or not st["tax_excluded"]:
        out.append("both VAT taxes")
    return out


def stripe_status(svc: Services) -> tuple[str, str]:
    """(text, kind) for the Stripe card: kind ok | warn | ""."""
    live, test = not stripe_missing(svc, "live"), not stripe_missing(svc, "test")
    if sales.secret(svc, "stripe_key") and _test_key(svc, "live"):
        return "Needs attention: production has a test key", "warn"
    partly = any(
        sales.secret(svc, k, e) for e in sales.ENVS for k in ("stripe_key", "stripe_webhook_secret")
    )
    if live and test:
        return "Live, with a test environment", "ok"
    if live:
        return "Live", "ok"
    if test:
        return "Test only", "warn"
    return ("Needs attention: half set up", "warn") if partly else ("Not set up", "")


def odoo_status(svc: Services) -> tuple[str, str]:
    live, test = not odoo_missing(svc, "live"), not odoo_missing(svc, "test")
    if live and test:
        return "Live, with a test database", "ok"
    if live:
        return "Live", "ok"
    if sales.odoo_ready(svc, "live") or sales.odoo_ready(svc, "test"):
        return ("Test only", "warn") if test else ("Needs attention: picks missing", "warn")
    return "Not set up", ""


def email_status(svc: Services) -> tuple[str, str]:
    from . import setup

    st = setup.state(svc.settings)
    if not st.get("available"):
        return "Set on the server (belfry config)", ""
    lm = bool(st["set"].get("BELFRY_LETTERMINT_API_KEY"))
    rs = bool(st["set"].get("BELFRY_RESEND_API_KEY"))
    if not (lm or rs):
        return "Not set up", ""
    sender = str(st["values"].get("BELFRY_MAIL_FROM", ""))
    name = "Lettermint" if lm else "Resend"
    return f"Live: {name}" + (f" · sends as {sender}" if sender else ""), "ok"


def tester_note(svc: Services) -> str:
    """What testers need, for the overview: fixed text and counts only."""
    n = len(testers.tester_partners(svc))
    miss = [f"Stripe test: {m}" for m in stripe_missing(svc, "test")] + [
        f"Odoo test: {m}" for m in odoo_missing(svc, "test")
    ]
    who = f"{n} tester{'s' if n != 1 else ''}" if n else "No testers yet"
    head = (
        f"<p>{E(who)}. A tester is a member made for trying payments: what they buy goes through "
        "the Test sections of Stripe and Odoo, never production. Add one from "
        '<a href="/members">Members</a> (+, then Tester).</p>'
    )
    if not miss:
        return head + '<p class="st ok">The test environment is ready.</p>'
    items = "".join(f"<li>{E(m)}</li>" for m in miss)
    return (
        head
        + '<p class="st warn">Testers can buy free passes only until the test environment has:</p>'
        + f'<ul class="missing">{items}</ul>'
    )


# ── pages ───────────────────────────────────────────────────────────────────────────────────
def _note(msg: str, err: str) -> str:
    return (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
        f'<p class="bad">{E(err)}</p>' if err else ""
    )


def _secret_line(svc: Services, name: str, env: str) -> str:
    st = sales.secret_state(svc, name, env)
    if not st["set"]:
        return '<span class="chip">not set</span>'
    tail = f" ··· {E(st['last4'])}" if st["last4"] else ""
    where = " (from the server's settings)" if st["from"] == "environment" else ""
    return f'<span class="chip ok">set{tail}</span><span class="hint">{where}</span>'


def _clear(csrf: str, name: str, env: str, back: str) -> str:
    return (
        f'<form method="post" action="{PATH}/clear" class="inline">'
        f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="secret" value="{E(name)}">'
        f'<input type="hidden" name="env" value="{E(env)}"><input type="hidden" name="back" value="{E(back)}">'
        '<button class="btn ghost" type="submit">Clear</button></form>'
    )


def _clears(svc: Services, csrf: str, names: tuple[str, ...], env: str, back: str) -> str:
    return "".join(
        _clear(csrf, n, env, back)
        for n in names
        if sales.secret_state(svc, n, env)["from"] == "console"
    )


def _env_head(env: str) -> str:
    return f'<h2>{ENV_TITLE[env]} <span class="badge {env}">{"LIVE" if env == "live" else "TEST"}</span></h2>'


def overview_html(svc: Services, who: dict[str, Any], msg: str, err: str) -> str:
    cards = ""
    for path, name, about, (text, kind) in (
        ("stripe", "Stripe", "Card payments", stripe_status(svc)),
        ("odoo", "Odoo", "Invoices, VAT and payments", odoo_status(svc)),
        ("email", "E-mail", "The belfry's mail provider", email_status(svc)),
    ):
        cards += (
            f'<a class="pcard" href="{PATH}/{path}"><strong>{E(name)}</strong>'
            f'<span class="hint">{E(about)}</span><span class="st {kind}">{E(text)}</span></a>'
        )
    body = f"""{salesconsole.STYLE}{STYLE}<div class="sales plugins"><h1>Plugins</h1>
<p class="muted">The services this belfry plugs into. Stripe and Odoo each have a production and a test environment.</p>
{_note(msg, err)}<div class="cards">{cards}</div>
<h2>Testers</h2>{tester_note(svc)}</div>"""
    return console.frame("Plugins", PATH, who, body)


def stripe_html(svc: Services, who: dict[str, Any], msg: str, err: str) -> str:
    csrf = E(who["csrf"])
    base = console.console_url(svc.settings)
    parts = ""
    for env in sales.ENVS:
        hook = base + ("/hooks/stripe" if env == "live" else "/hooks/stripe/test")
        ph = "rk_live_…" if env == "live" else "rk_test_…"
        about = (
            "Real payments. Takes only a live key."
            if env == "live"
            else "Stripe's test mode, for testers only: test cards, no real money. Takes only a "
            "test key."
        )
        warn = (
            '<p class="st warn">This is a test key: production takes real payments only with a live key.</p>'
            if env == "live" and sales.secret(svc, "stripe_key") and _test_key(svc, "live")
            else ""
        )
        parts += f"""<section class="env {env}" id="{env}">{_env_head(env)}<p class="hint">{E(about)}</p>{warn}
<form method="post" action="{PATH}/stripe/{env}" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>Restricted key <input type="password" name="key" autocomplete="off" placeholder="{ph}"></label>
<div class="actions">{_secret_line(svc, "stripe_key", env)}</div>
<label>Webhook signing secret <input type="password" name="webhook" autocomplete="off" placeholder="whsec_…"></label>
<div class="actions">{_secret_line(svc, "stripe_webhook_secret", env)}</div>
<p class="hint">At Stripe{" (in test mode)" if env == "test" else ""}, add the webhook <code>{E(hook)}</code> with the events
<code>checkout.session.completed</code> and <code>checkout.session.async_payment_succeeded</code>.
The restricted key needs Checkout Sessions: write.</p>
<div class="actions"><button class="btn" type="submit">Save</button></div></form>
<div class="actions" style="margin-top:8px">{_clears(svc, csrf, ("stripe_key", "stripe_webhook_secret"), env, "stripe")}</div></section>"""
    body = f"""{salesconsole.STYLE}{STYLE}<div class="sales plugins"><p><a href="{PATH}">‹ Plugins</a></p><h1>Stripe</h1>
<p class="muted">Card payments through your own Stripe account.</p>{_note(msg, err)}{parts}</div>"""
    return console.frame("Stripe", PATH, who, body)


def _picks_form(svc: Services, csrf: str, env: str) -> str:
    st = sales.settings(svc, env)
    try:
        lists = salesodoo.pick_lists(svc, env)
    except (OdooError, httpx.HTTPError, OSError, sales.Refused):
        lists = None
    if lists is None:
        return '<p class="bad">Odoo did not answer just now; the picks show when it does.</p>'
    sel = salesconsole._select

    def tax(t: dict[str, Any]) -> str:
        return f"{t['name']} ({t['amount']:g}%)"

    prod_rows = (
        "".join(
            f"<label>{E(p['name'])} is invoiced as "
            + sel(
                f"product_{p['id']}",
                lists["products"],
                lambda r: r["display_name"],
                st["odoo_products"].get(p["id"]),
                "— pick a product —",
            )
            + "</label>"
            for p in sales.products(svc)
        )
        or '<p class="hint">Add a product on the <a href="/sales">Sales</a> page first.</p>'
    )
    # the product for event tickets: an admin's pick, else the one Belfry found or made in this
    # Odoo ("Event ticket", ticketsales.prepare: when the connection or these picks are saved,
    # and at the first ticket sale); shown selected even when Odoo's list leaves it out
    tickets = list(lists["products"])
    mine = st["ticket_product"]
    if mine and all(r["id"] != mine for r in tickets):
        tickets.append(
            {
                "id": mine,
                "display_name": salesodoo.TICKET_PRODUCT
                if mine == st["ticket_product_auto"]
                else "(the product picked earlier)",
            }
        )
    if mine and mine == st["ticket_product_auto"]:
        ticket_hint = (
            '<p class="hint">Belfry found or made the service product “Event ticket” in this '
            "Odoo for event tickets (the invoice line carries the ticket's own name and price). "
            "Pick another one if you prefer.</p>"
        )
    elif not mine and st["ticket_product_refused"]:
        ticket_hint = (
            '<p class="bad">Odoo did not let Belfry create the product “Event ticket” (the API '
            "key's user may not create products). Create a service product in Odoo and pick it "
            "here.</p>"
        )
    elif not mine:
        ticket_hint = (
            '<p class="hint">Nothing picked: Belfry makes a service product “Event ticket” in '
            "this Odoo when you save, or at the first ticket sale.</p>"
        )
    else:
        ticket_hint = ""
    return f"""<form method="post" action="{PATH}/odoo/{env}/picks" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>Company {sel("company", lists["companies"], lambda r: r["name"], st["company"], "— the key's default —")}</label>
{prod_rows}
<label>Event tickets are invoiced as {sel("ticket_product", tickets, lambda r: r["display_name"], st["ticket_product"], "— Belfry's own “Event ticket” —")}</label>{ticket_hint}
<label>VAT for people (price includes it) {sel("tax_included", lists["taxes"], tax, st["tax_included"], "— pick a tax —")}</label>
<label>VAT for organisations (on top) {sel("tax_excluded", lists["taxes"], tax, st["tax_excluded"], "— pick a tax —")}</label>
<label>Journal for card payments {sel("card_journal", lists["journals"], lambda r: r["name"], st["card_journal"], "— not booked —")}</label>
<label>Journal for payments marked by hand {sel("manual_journal", lists["journals"], lambda r: r["name"], st["manual_journal"], "— not booked —")}</label>
<p class="hint">Belfry mails each invoice with its PDF to the buyer; Odoo doesn't send it.</p>
<div class="actions"><button class="btn" type="submit">Save</button></div></form>"""


def odoo_html(svc: Services, who: dict[str, Any], msg: str, err: str) -> str:
    csrf = E(who["csrf"])
    parts = ""
    for env in sales.ENVS:
        st = sales.settings(svc, env)
        about = (
            "Real invoices, VAT and payments (Odoo Online works)."
            if env == "live"
            else "A separate Odoo database for testers' orders: its own connection and its own "
            "picks, since a test database has other ids."
        )
        picks = _picks_form(svc, csrf, env) if sales.odoo_ready(svc, env) else ""
        parts += f"""<section class="env {env}" id="{env}">{_env_head(env)}<p class="hint">{E(about)}</p>
<form method="post" action="{PATH}/odoo/{env}" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>URL <input type="text" name="url" inputmode="url" placeholder="https://yourorg.odoo.com" value="{E(st["odoo_url"] or "")}"></label>
<label>Database <input type="text" name="db" value="{E(st["odoo_db"] or "")}"></label>
<label>API key <input type="password" name="key" autocomplete="off" placeholder="leave empty to keep the one set"></label>
<div class="actions">{_secret_line(svc, "odoo_api_key", env)}</div>
<div class="actions"><button class="btn" type="submit">Check</button></div></form>
<div class="actions" style="margin-top:8px">{_clears(svc, csrf, ("odoo_api_key",), env, "odoo")}</div>
{picks}</section>"""
    body = f"""{salesconsole.STYLE}{STYLE}<div class="sales plugins"><p><a href="{PATH}">‹ Plugins</a></p><h1>Odoo</h1>
<p class="muted">Invoices, VAT and payments live in your Odoo.</p>{_note(msg, err)}{parts}</div>"""
    return console.frame("Odoo", PATH, who, body)


def email_html(svc: Services, who: dict[str, Any], msg: str, err: str) -> str:
    from . import setup

    st = setup.state(svc.settings)
    if not st.get("available"):
        form = (
            "<p>This belfry has no setup folder: set the mail provider on the server with "
            "<code>belfry config</code>.</p>"
        )
    else:
        _done, row = setup.mail_row(st, who["csrf"], "mail", back=f"{PATH}/email")
        form = f'{setup.SETUP_STYLE}<div class="list">{row}</div>'
        if st["pending"] or st["applying"]:
            form = (
                '<p class="note">Applying the changes: the belfry restarts what changed, which '
                "can take a few minutes.</p>" + form
            )
    body = f"""{salesconsole.STYLE}{STYLE}<div class="sales plugins"><p><a href="{PATH}">‹ Plugins</a></p><h1>E-mail</h1>
<p class="muted">The service that sends the belfry's mail (sign-in codes, invites, receipts). There is no test environment for e-mail.</p>
{_note(msg, err)}{form}</div>"""
    return console.frame("E-mail", PATH, who, body)


# ── saving ──────────────────────────────────────────────────────────────────────────────────
def stripe_refusal(key: str, hook: str, env: str) -> str:
    """The MSG key refusing this Stripe key or secret for ENV, or ""."""
    if (key and not STRIPE_KEY.match(key)) or (hook and not WEBHOOK.match(hook)):
        return "stripe_bad"
    if key and env == "live" and re.match(r"^(sk|rk)_test_", key):
        return "stripe_test_in_live"
    if key and env == "test" and re.match(r"^(sk|rk)_live_", key):
        return "stripe_live_in_test"
    return ""


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    async def who(request: Request) -> dict[str, Any] | None:
        s = svc(request)
        sess = console.read_session(s, request.cookies.get(console.COOKIE))
        return await run_in_threadpool(console.viewer, s, sess) if sess else None

    def back(request: Request, to: str, msg: str = "", err: str = "") -> RedirectResponse:
        return flash.redirect(svc(request), to, MSG.get(msg, ""), MSG.get(err, ""))

    async def page(request: Request, title: str, build) -> HTMLResponse | RedirectResponse:
        s = svc(request)
        me = await who(request)
        if me is None:
            return RedirectResponse(f"/login?to={PATH}", status_code=303)
        if not me["admin"]:
            body = f"<h1>{E(title)}</h1><p>Only the belfry's admins see this page.</p>"
            return HTMLResponse(console.frame(title, PATH, me, body), status_code=403)
        msg, err = flash.peek(request, s)
        out = await run_in_threadpool(build, s, me, msg, err)
        return flash.clear(HTMLResponse(out, headers={"Cache-Control": "no-store"}), request)

    @app.get("/console/plugins", response_class=HTMLResponse)
    async def overview(request: Request):
        return await page(request, "Plugins", overview_html)

    @app.get("/console/plugins/stripe", response_class=HTMLResponse)
    async def stripe_page(request: Request):
        return await page(request, "Stripe", stripe_html)

    @app.get("/console/plugins/odoo", response_class=HTMLResponse)
    async def odoo_page(request: Request):
        return await page(request, "Odoo", odoo_html)

    @app.get("/console/plugins/email", response_class=HTMLResponse)
    async def email_page(request: Request):
        return await page(request, "E-mail", email_html)

    async def admin_form(request: Request) -> tuple[dict[str, Any] | None, Any]:
        form = await request.form()
        me = await who(request)
        ok = (
            me is not None
            and me["admin"]
            and pysecrets.compare_digest(str(form.get("csrf") or ""), me["csrf"] or "x")
        )
        return (me if ok else None), form

    @app.post("/console/plugins/stripe/{env}")
    async def stripe_save(request: Request, env: str):
        to = f"{PATH}/stripe"
        me, form = await admin_form(request)
        if me is None or env not in sales.ENVS:
            return back(request, to, err="expired")
        s = svc(request)
        key = str(form.get("key") or "").strip()
        hook = str(form.get("webhook") or "").strip()
        why = stripe_refusal(key, hook, env)
        if why:
            return back(request, to, err=why)
        if key:
            await run_in_threadpool(sales.set_secret, s, "stripe_key", key, env)
        if hook:
            await run_in_threadpool(sales.set_secret, s, "stripe_webhook_secret", hook, env)
        log.info("plugins: %s saved Stripe (%s)", me["name"] or "an admin", env)
        return back(request, to, msg="stripe")

    async def ticket_product(s: Services, env: str, ok: str) -> str:
        """With no product picked for event tickets, Belfry finds or makes one in ENV's Odoo
        now (ticketsales.prepare). The message to show: OK, or that Odoo refused."""
        from . import ticketsales

        await run_in_threadpool(ticketsales.prepare, s, env, True)
        st = sales.settings(s, env)
        refused = st["ticket_product_refused"] and not ticketsales.odoo_product(st)
        return f"{ok}_no_ticket" if refused else ok

    @app.post("/console/plugins/odoo/{env}")
    async def odoo_save(request: Request, env: str):
        to = f"{PATH}/odoo"
        me, form = await admin_form(request)
        if me is None or env not in sales.ENVS:
            return back(request, to, err="expired")
        s = svc(request)
        url = str(form.get("url") or "").strip().rstrip("/")
        db = str(form.get("db") or "").strip()
        typed = str(form.get("key") or "").strip()
        key = typed or sales.secret(s, "odoo_api_key", env)
        if not (url.startswith("https://") and db and key) or len(url) > 200 or len(db) > 100:
            return back(request, to, err="odoo_need")
        other = sales.settings(s, "test" if env == "live" else "live")
        if (other["odoo_url"] or "").rstrip("/") == url and other["odoo_db"] == db:
            return back(request, to, err="odoo_same")
        try:
            await run_in_threadpool(salesodoo.check_connection, url, db, key)
        except (OdooError, httpx.HTTPError, OSError, ValueError):
            log.info("plugins: an admin's Odoo check failed (%s)", env)
            return back(request, to, err="odoo_bad")
        was = sales.settings(s, env)
        vals: dict[str, Any] = {"odoo_url": url, "odoo_db": db}
        if ((was["odoo_url"] or "").rstrip("/"), was["odoo_db"]) != (url, db):
            # another database has other ids: the product Belfry made in the old one is not here
            vals.update(ticket_product_auto=None, ticket_product_refused=False)
            if was["ticket_product"] and was["ticket_product"] == was["ticket_product_auto"]:
                vals["ticket_product"] = None
        await run_in_threadpool(sales.save_settings, s, vals, env)
        if typed:
            await run_in_threadpool(sales.set_secret, s, "odoo_api_key", key, env)
        log.info("plugins: %s set the Odoo connection (%s)", me["name"] or "an admin", env)
        return back(request, to, msg=await ticket_product(s, env, "odoo_ok"))

    @app.post("/console/plugins/odoo/{env}/picks")
    async def odoo_picks(request: Request, env: str):
        to = f"{PATH}/odoo"
        me, form = await admin_form(request)
        if me is None or env not in sales.ENVS:
            return back(request, to, err="expired")
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
                "ticket_product": num("ticket_product"),
                "ticket_product_refused": False,
                "tax_included": num("tax_included"),
                "tax_excluded": num("tax_excluded"),
                "card_journal": num("card_journal"),
                "manual_journal": num("manual_journal"),
            },
            env,
        )
        return back(request, to, msg=await ticket_product(s, env, "picks"))

    @app.post("/console/plugins/clear")
    async def clear(request: Request):
        me, form = await admin_form(request)
        to = {"stripe": f"{PATH}/stripe", "odoo": f"{PATH}/odoo"}.get(
            str(form.get("back") or ""), PATH
        )
        name, env = str(form.get("secret") or ""), str(form.get("env") or "")
        if me is None or name not in sales.BASE_SECRETS or env not in sales.ENVS:
            return back(request, to, err="expired")
        await run_in_threadpool(sales.set_secret, svc(request), name, None, env)
        return back(request, to, msg="cleared")


__all__ = ["register", "stripe_refusal", "overview_html"]
