"""Selling passes on the belfry's own register (docs/sales.md): products with dated prices, codes,
orders, the hold and its expiry, and the account of a buyer who signs with a key made in their
browser.

Where things live:

- products: register param ``belfry.sales.products`` (a JSON list, edited in the console);
- the other non-secret settings (bank, the seller's Odoo picks, currency): register param
  ``belfry.sales.settings``;
- secrets (the seller's Odoo API key, Stripe's restricted key and webhook secret):
  ``sales-secrets.json`` next to the register (mode 600, written atomically by this service;
  never in an answer, a log line, an error or an export). The environment's ``ACCOUNTING_*`` /
  ``STRIPE_*`` values are the fallback when the console left a field empty;
- codes, orders, offers and Stripe's events: ``sales.sqlite`` next to the register.

An order is a small state machine: ``pending`` (made) → ``invoiced`` (the seller's invoice is
posted) → ``paid`` (Stripe, the bank, or an admin) → ``active`` (the pass is open), or
``cancelled`` / ``expired``. Each order records its next ``step``; a step that fails (Odoo or
Stripe down) is retried by the tick (``tick``), so an order is never lost halfway.

The buyer (section 3 of the contract): the key that signs the checkout is a session key. When
the pass opens, an unknown key gets a new contact whose address is *unverified*: it is kept in
``belfry_pending_email``, never in ``email``, so no lookup by e-mail anywhere finds it. The
receipt's confirmation link, or the first sign-in with an e-mail code (connect.py), proves it:
the address moves to ``email`` (``confirm``), or, when it is another account's already, the
pass moves to that account and the temporary one closes. The contact is made only when the pass
opens, so an order nobody pays leaves no account behind.

Amounts are integers of cents inside; the API shows euros as numbers (``200.0``).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id

log = logging.getLogger(__name__)

PRODUCTS_PARAM = "belfry.sales.products"
SETTINGS_PARAM = "belfry.sales.settings"
SECRETS_FILE = "sales-secrets.json"
SECRET_KEYS = ("odoo_api_key", "stripe_key", "stripe_webhook_secret")
HOLD_DAYS = 7
REMIND_DAYS = 2  # one reminder this long before the hold ends
OPEN_PER_KEY = 5
ORDERS_PER_ADDRESS_HOUR = 20
CODE_CHECKS_PER_HOUR = 10
OFFERS_PER_KEY_HOUR = 5
OPEN = ("pending", "invoiced", "paid")
STATES = (*OPEN, "active", "cancelled", "expired")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PRODUCT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CODE_RE = re.compile(r"^[A-Z0-9-]{4,40}$")
ORDER_RE = re.compile(r"^o_[a-z0-9]{16}$")
OFFER_RE = re.compile(r"^f_[a-z0-9]{16}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
API = "/api"


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def today() -> dt.date:
    return now().date()


def mask(email: str) -> str:
    """An address for the logs: never the whole of it."""
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


def euros(cents: int) -> float:
    return round(int(cents) / 100, 2)


def cents(value: Any) -> int:
    """A price typed in euros (12.5, "12.50") as cents; ValueError when it isn't one."""
    if isinstance(value, bool):
        raise ValueError("not an amount")
    d = Decimal(str(value).strip().replace(",", "."))
    if d < 0 or d > 1_000_000 or d != d.quantize(Decimal("0.01")):
        raise ValueError("not an amount")
    return int(d * 100)


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def split(amount: int, vat_rate: Decimal | float | str, percent_off: int, kind: str) -> dict:
    """Net, VAT and gross in cents. AMOUNT is the product's price: VAT included for a person,
    VAT on top for an organisation (the same number, as belfry_pass does)."""
    rate = Decimal(str(vat_rate or 0))
    keep = Decimal(100 - int(percent_off)) / 100
    if kind == "organisation":
        net = _round(Decimal(amount) * keep)
        vat = _round(Decimal(net) * rate / 100)
        gross = net + vat
    else:
        gross = _round(Decimal(amount) * keep)
        net = _round(Decimal(gross) * 100 / (100 + rate))
        vat = gross - net
    return {"net": net, "vat": vat, "gross": gross, "percent_off": int(percent_off)}


# ── storage ─────────────────────────────────────────────────────────────────────────────────
class Store:
    """sales.sqlite: codes, orders, offers, Stripe events. One JSON document per row, with the
    columns that are searched."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, email TEXT NOT NULL, state TEXT NOT NULL,
                created INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS orders_key ON orders (key);
            CREATE INDEX IF NOT EXISTS orders_state ON orders (state);
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, state TEXT NOT NULL,
                created INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stripe_events (
                id TEXT PRIMARY KEY, at INTEGER NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS confirms (
                token TEXT PRIMARY KEY, order_id TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at INTEGER NOT NULL);
            """
        )

    @contextlib.contextmanager
    def tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # orders
    def order(self, oid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM orders WHERE id = ?", (oid,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_order(self, o: dict[str, Any], db: sqlite3.Connection | None = None) -> None:
        o["updated"] = iso(now())
        with self._lock:
            (db or self._db).execute(
                "INSERT OR REPLACE INTO orders (id, key, email, state, created, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    o["id"],
                    o["key"],
                    o["email"].lower(),
                    o["state"],
                    int(parse_iso(o["created"]).timestamp()),
                    json.dumps(o),
                ),
            )

    def update(self, oid: str, **vals: Any) -> dict[str, Any]:
        with self.tx() as db:
            row = db.execute("SELECT data FROM orders WHERE id = ?", (oid,)).fetchone()
            if row is None:
                raise KeyError(oid)
            o = json.loads(row[0])
            o.update(vals)
            self.put_order(o, db)
        return o

    def orders(
        self, states: tuple[str, ...] | None = None, key: str | None = None, limit: int = 0
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT data FROM orders WHERE 1=1", []
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args += list(states)
        if key:
            sql += " AND key = ?"
            args.append(key)
        sql += " ORDER BY created DESC, id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            return [json.loads(r[0]) for r in self._db.execute(sql, args).fetchall()]

    def count_address(self, email: str, since: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM orders WHERE email = ? AND created >= ?",
                (email.lower(), since),
            ).fetchone()[0]

    # codes
    def code(self, code: str, db: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        with self._lock:
            row = (
                (db or self._db)
                .execute("SELECT data FROM codes WHERE code = ?", (code,))
                .fetchone()
            )
        return json.loads(row[0]) if row else None

    def put_code(self, c: dict[str, Any], db: sqlite3.Connection | None = None) -> None:
        with self._lock:
            (db or self._db).execute(
                "INSERT OR REPLACE INTO codes (code, data) VALUES (?, ?)",
                (c["code"], json.dumps(c)),
            )

    def codes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM codes").fetchall()
        return sorted((json.loads(r[0]) for r in rows), key=lambda c: c["created"], reverse=True)

    # offers
    def offer(self, fid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM offers WHERE id = ?", (fid,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_offer(self, f: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO offers (id, key, state, created, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f["id"],
                    f["key"],
                    f["state"],
                    int(parse_iso(f["created"]).timestamp()),
                    json.dumps(f),
                ),
            )

    def offers(self, key: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT data FROM offers", []
        if key:
            sql += " WHERE key = ?"
            args.append(key)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created DESC", args).fetchall()
        return [json.loads(r[0]) for r in rows]

    # Stripe's events, confirmation links, mails sent once
    def first_event(self, event_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO stripe_events (id, at, result) VALUES (?, ?, ?)",
                (event_id, int(time.time()), "received"),
            )
        return cur.rowcount == 1

    def event_result(self, event_id: str, result: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE stripe_events SET result = ? WHERE id = ?", (result[:60], event_id)
            )

    def forget_event(self, event_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM stripe_events WHERE id = ?", (event_id,))

    def confirm_token(self, oid: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT token FROM confirms WHERE order_id = ?", (oid,)
            ).fetchone()
            if row:
                return row[0]
            token = secrets.token_urlsafe(24)
            self._db.execute(
                "INSERT INTO confirms (token, order_id, at) VALUES (?, ?, ?)",
                (token, oid, int(time.time())),
            )
            return token

    def confirm_order(self, token: str) -> str | None:
        if not token or len(token) < 20:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT order_id FROM confirms WHERE token = ?", (token,)
            ).fetchone()
        return row[0] if row else None

    def once(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO sent (key, at) VALUES (?, ?)", (key, int(time.time()))
            )
        return cur.rowcount == 1


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def store(svc: Services) -> Store:
    path = str(Path(svc.settings.register_path).with_name("sales.sqlite"))
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Store(path)
        return _stores[path]


# ── settings and secrets ────────────────────────────────────────────────────────────────────
def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


def _param(svc: Services, key: str) -> Any:
    try:
        return json.loads(svc.odoo.param(key, "") or "null")
    except ValueError:
        return None


def settings(svc: Services) -> dict[str, Any]:
    """The non-secret sales settings, with the environment's ACCOUNTING_* as the fallback."""
    st = _param(svc, SETTINGS_PARAM)
    st = st if isinstance(st, dict) else {}
    env = svc.settings
    out = {
        "currency": st.get("currency") or "EUR",
        "odoo_url": st.get("odoo_url") or env.accounting_odoo_url,
        "odoo_db": st.get("odoo_db") or env.accounting_odoo_db,
        "company": st.get("company") or None,
        "company_name": st.get("company_name") or env.accounting_issuer,
        "odoo_products": st.get("odoo_products") or {},
        "fallback_products": _env_products(env.accounting_products),
        "tax_included": st.get("tax_included") or _int(env.accounting_tax_included),
        "tax_excluded": st.get("tax_excluded") or _int(env.accounting_tax_excluded),
        "card_journal": st.get("card_journal") or _int(env.accounting_journal),
        "manual_journal": st.get("manual_journal") or _int(env.accounting_manual_journal),
        "ref_prefix": st.get("ref_prefix") or env.accounting_ref_prefix or "Belfry ",
        # Odoo mails its own invoice PDF too (Belfry always mails the transfer details)
        "send_invoice": bool(st.get("send_invoice", False)),
        "bank_holder": st.get("bank_holder") or "",
        "bank_iban": st.get("bank_iban") or "",
        "bank_bic": st.get("bank_bic") or "",
    }
    return out


def _int(v: Any) -> int | None:
    try:
        return int(str(v).strip()) if str(v or "").strip() else None
    except ValueError:
        return None


def _env_products(spec: str) -> dict[str, int]:
    from .accounting import parse_ids

    try:
        return parse_ids(spec or "")
    except ValueError:
        return {}


def save_settings(svc: Services, values: dict[str, Any]) -> None:
    st = _param(svc, SETTINGS_PARAM)
    st = st if isinstance(st, dict) else {}
    st.update(values)
    svc.odoo.set_param(SETTINGS_PARAM, json.dumps(st))


def _secrets_path(svc: Services) -> Path:
    return Path(svc.settings.register_path).with_name(SECRETS_FILE)


_secrets_lock = threading.Lock()


def _read_secrets(svc: Services) -> dict[str, str]:
    try:
        data = json.loads(_secrets_path(svc).read_text())
    except (OSError, ValueError):
        return {}
    return (
        {k: str(v) for k, v in data.items() if k in SECRET_KEYS and v}
        if isinstance(data, dict)
        else {}
    )


def secret(svc: Services, name: str) -> str:
    """A secret: the console's, else the environment's. Never logged, never in an answer."""
    got = _read_secrets(svc).get(name, "")
    if got:
        return got
    env = svc.settings
    return {
        "odoo_api_key": env.accounting_odoo_api_key,
        "stripe_key": getattr(env, "stripe_secret_key", "") or "",
        "stripe_webhook_secret": env.stripe_webhook_secret,
    }.get(name, "") or ""


def set_secret(svc: Services, name: str, value: str | None) -> None:
    """Write (or with None clear: back to the environment's) one secret, atomically, mode 600."""
    if name not in SECRET_KEYS:
        raise ValueError("unknown secret")
    path = _secrets_path(svc)
    with _secrets_lock:
        data = _read_secrets(svc)
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{SECRETS_FILE}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def secret_state(svc: Services, name: str) -> dict[str, Any]:
    """What the console may show of a secret: set or not, where from, and its last 4."""
    mine = _read_secrets(svc).get(name, "")
    value = mine or secret(svc, name)
    return {
        "set": bool(value),
        "from": "console" if mine else ("environment" if value else ""),
        "last4": value[-4:] if len(value) >= 12 else "",
    }


def odoo_ready(svc: Services) -> bool:
    st = settings(svc)
    return bool(st["odoo_url"] and st["odoo_db"] and secret(svc, "odoo_api_key"))


def accounting_ready(svc: Services) -> bool:
    """Invoices can be made: the connection, a product and both taxes picked."""
    st = settings(svc)
    return bool(
        odoo_ready(svc)
        and (st["odoo_products"] or st["fallback_products"])
        and st["tax_included"]
        and st["tax_excluded"]
    )


def stripe_ready(svc: Services) -> bool:
    return bool(secret(svc, "stripe_key"))


def bank_ready(svc: Services) -> bool:
    st = settings(svc)
    return bool(st["bank_holder"] and st["bank_iban"])


def can_sell(svc: Services) -> bool:
    """Paid products can be sold: invoices, and at least one way to pay them."""
    return accounting_ready(svc) and (stripe_ready(svc) or bank_ready(svc))


def summary(svc: Services) -> dict[str, bool]:
    """For the Status page and ``belfry status``: what is set, never a value."""
    return {
        "can_sell": own(svc) and can_sell(svc),
        "odoo": odoo_ready(svc),
        "bank": bank_ready(svc),
        "stripe": stripe_ready(svc),
    }


# ── products ────────────────────────────────────────────────────────────────────────────────
def status_line(svc: Services) -> str:
    """One sentence for the Status page and ``belfry status``: never a value."""
    s = summary(svc)
    parts = ", ".join(
        f"{name} {'set' if s[k] else 'not set'}"
        for k, name in (("odoo", "Odoo"), ("bank", "bank"), ("stripe", "Stripe"))
    )
    head = "Selling passes: paid passes can be sold" if s["can_sell"] else (
        "Selling passes: free passes only"
    )
    return f"{head} ({parts})."


def products(svc: Services) -> list[dict[str, Any]]:
    got = _param(svc, PRODUCTS_PARAM)
    return [p for p in got if isinstance(p, dict) and p.get("id")] if isinstance(got, list) else []


def product(svc: Services, pid: str) -> dict[str, Any] | None:
    return next((p for p in products(svc) if p["id"] == pid), None)


def clean_product(data: dict[str, Any]) -> dict[str, Any]:
    """A product as the console sends it; ValueError with a fixed message when it's wrong."""
    pid = str(data.get("id") or "").strip().lower()
    if not PRODUCT_ID.match(pid):
        raise ValueError("The id: lower-case letters, digits and dashes.")
    name = " ".join(str(data.get("name") or "").split())[:100]
    if not name:
        raise ValueError("A product needs a name.")
    tier = str(data.get("tier") or "member").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", tier) or tier in ("guest", "agent", "steward"):
        raise ValueError("The pass tier: a short word, not guest or agent.")
    role = str(data.get("role") or "").strip().lower()
    if role and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", role):
        raise ValueError("The role: a role's id.")
    dates = {}
    for k in ("valid_from", "valid_until"):
        v = str(data.get(k) or "").strip()
        if v:
            try:
                dt.date.fromisoformat(v)
            except ValueError as exc:
                raise ValueError("Dates as YYYY-MM-DD.") from exc
        dates[k] = v or None
    if dates["valid_from"] and dates["valid_until"] and dates["valid_until"] < dates["valid_from"]:
        raise ValueError("The pass ends before it starts.")
    prices = []
    raw = data.get("prices") or []
    if not isinstance(raw, list) or not raw or len(raw) > 10:
        raise ValueError("Give one to ten prices.")
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            raise ValueError("A price is {amount, until?}.")
        try:
            amount = cents(p.get("amount"))
        except (ValueError, ArithmeticError) as exc:
            raise ValueError("A price is a number of euros, like 200 or 12.50.") from exc
        until = str(p.get("until") or "").strip() or None
        if until:
            try:
                dt.date.fromisoformat(until)
            except ValueError as exc:
                raise ValueError("Dates as YYYY-MM-DD.") from exc
        elif i != len(raw) - 1:
            raise ValueError("Only the last price can have no end date.")
        if prices and until and prices[-1]["until"] and until <= prices[-1]["until"]:
            raise ValueError("Prices go in order of their end dates.")
        prices.append({"until": until, "amount": amount})
    try:
        vat = Decimal(str(data.get("vat_rate", 21)).replace(",", "."))
    except ArithmeticError as exc:
        raise ValueError("The VAT rate is a percentage, like 21.") from exc
    if vat < 0 or vat > 50:
        raise ValueError("The VAT rate is a percentage, like 21.")
    qty = data.get("quantity")
    if qty in (None, ""):
        quantity = None
    else:
        try:
            quantity = int(qty)
        except (TypeError, ValueError) as exc:
            raise ValueError("How many: a whole number, or empty for no limit.") from exc
        if quantity < 0 or quantity > 1_000_000:
            raise ValueError("How many: a whole number, or empty for no limit.")
    return {
        "id": pid,
        "name": name,
        "description": str(data.get("description") or "").strip()[:2000],
        "tier": tier,
        "role": role or None,
        **dates,
        "prices": prices,
        "vat_rate": float(vat),
        "quantity": quantity,
        "on_sale": bool(data.get("on_sale", True)),
    }


def save_product(svc: Services, data: dict[str, Any]) -> dict[str, Any]:
    p = clean_product(data)
    rows = [x for x in products(svc) if x["id"] != p["id"]]
    old = product(svc, p["id"])
    if old is None and len(rows) >= 50:
        raise ValueError("At most 50 products.")
    rows.append(p)
    svc.odoo.set_param(PRODUCTS_PARAM, json.dumps(rows))
    return p


def delete_product(svc: Services, pid: str) -> None:
    if any(o["product"] == pid for o in store(svc).orders()):
        raise ValueError("This product has orders: take it off sale instead.")
    rows = [x for x in products(svc) if x["id"] != pid]
    svc.odoo.set_param(PRODUCTS_PARAM, json.dumps(rows))


def price_now(p: dict[str, Any], on: dt.date | None = None) -> tuple[dict | None, dict | None]:
    """(the price that applies on ON, the next one): the first whose ``until`` hasn't passed."""
    on = on or today()
    prices = p.get("prices") or []
    for i, pr in enumerate(prices):
        if not pr.get("until") or dt.date.fromisoformat(pr["until"]) >= on:
            nxt = prices[i + 1] if i + 1 < len(prices) else None
            return pr, nxt
    return None, None


def taken(svc: Services, pid: str) -> int:
    """Places held: open and active orders of the product."""
    return sum(1 for o in store(svc).orders((*OPEN, "active")) if o["product"] == pid)


def left(svc: Services, p: dict[str, Any]) -> int | None:
    if p.get("quantity") is None:
        return None
    return max(0, int(p["quantity"]) - taken(svc, p["id"]))


def public_product(svc: Services, p: dict[str, Any]) -> dict[str, Any] | None:
    pr, nxt = price_now(p)
    if pr is None:
        return None
    out: dict[str, Any] = {
        "id": p["id"],
        "name": p["name"],
        "description": p.get("description") or "",
        "tier": p["tier"],
        "valid_from": p.get("valid_from"),
        "valid_until": p.get("valid_until"),
        "price": {"amount": euros(pr["amount"]), "vat_rate": p["vat_rate"], "until": pr["until"]},
        "next_price": None,
        "left": left(svc, p),
    }
    if nxt is not None and pr.get("until"):
        start = dt.date.fromisoformat(pr["until"]) + dt.timedelta(days=1)
        out["next_price"] = {"amount": euros(nxt["amount"]), "from": start.isoformat()}
    return out


def listing(svc: Services) -> dict[str, Any]:
    rows = [
        x for p in products(svc) if p.get("on_sale") and (x := public_product(svc, p)) is not None
    ]
    return {"currency": settings(svc)["currency"], "can_sell": can_sell(svc), "products": rows}


# ── codes ───────────────────────────────────────────────────────────────────────────────────
def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def norm_code(code: Any) -> str:
    return re.sub(r"\s", "", str(code or "")).upper()[:40]


def make_codes(
    svc: Services,
    percent: int,
    product_id: str | None = None,
    uses: int = 1,
    count: int = 1,
    expires: str | None = None,
    note: str = "",
    by: str = "",
    offer: str | None = None,
) -> list[dict[str, Any]]:
    from .holder_invites import ApiError

    try:
        percent, uses, count = int(percent), int(uses), int(count)
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid", "percent, uses and count are whole numbers.") from exc
    if not 1 <= percent <= 100:
        raise ApiError(422, "invalid", "percent: 1 to 100.")
    if not 1 <= uses <= 1000 or not 1 <= count <= 500:
        raise ApiError(422, "invalid", "uses: 1 to 1000; count: 1 to 500.")
    if product_id and product(svc, product_id) is None:
        raise ApiError(404, "unknown_product", "No such product.")
    if expires:
        try:
            dt.date.fromisoformat(str(expires))
        except ValueError as exc:
            raise ApiError(422, "invalid", "expires: a date, YYYY-MM-DD.") from exc
    st = store(svc)
    out = []
    with st.tx() as db:
        for _ in range(count):
            code = new_code()
            while st.code(code, db) is not None:
                code = new_code()
            c = {
                "code": code,
                "percent": percent,
                "product": product_id or None,
                "uses": uses,
                "used": [],  # order ids holding a use
                "expires": str(expires) if expires else None,
                "note": " ".join(str(note or "").split())[:200],
                "by": by[:100],
                "offer": offer,
                "revoked": False,
                "created": iso(now()),
            }
            st.put_code(c, db)
            out.append(c)
    return out


def code_row(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": c["code"],
        "percent": c["percent"],
        "product": c["product"],
        "uses": c["uses"],
        "left": max(0, c["uses"] - len(c["used"])),
        "expires": c["expires"],
        "note": c["note"],
        "revoked": c["revoked"],
    }


def _usable(c: dict[str, Any] | None, pid: str) -> str | None:
    """Why the code can't be used for product PID, or None."""
    if c is None or c.get("revoked"):
        return "unknown_code"
    if c.get("product") and c["product"] != pid:
        return "unknown_code"
    if c.get("expires") and dt.date.fromisoformat(c["expires"]) < today():
        return "unknown_code"
    if len(c["used"]) >= int(c["uses"]):
        return "code_used"
    return None


def revoke_code(svc: Services, code: str) -> bool:
    st = store(svc)
    with st.tx() as db:
        c = st.code(norm_code(code), db)
        if c is None:
            return False
        c["revoked"] = True
        st.put_code(c, db)
    return True


def _give_back(svc: Services, o: dict[str, Any]) -> None:
    """An order that ends unpaid gives its code's use back."""
    if not o.get("code"):
        return
    st = store(svc)
    with st.tx() as db:
        c = st.code(o["code"], db)
        if c and o["id"] in c["used"]:
            c["used"].remove(o["id"])
            st.put_code(c, db)


# ── rate limits (in memory) ─────────────────────────────────────────────────────────────────
class Limiter:
    def __init__(self, events: int, seconds: float):
        self.events, self.seconds = events, seconds
        self._seen: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        t = time.monotonic()
        with self._lock:
            q = self._seen[key]
            while q and t - q[0] > self.seconds:
                q.popleft()
            if len(q) >= self.events:
                return False
            q.append(t)
            return True

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


CODE_CHECKS = Limiter(CODE_CHECKS_PER_HOUR, 3600)
OFFERS = Limiter(OFFERS_PER_KEY_HOUR, 3600)


# ── who is buying ───────────────────────────────────────────────────────────────────────────
def account_of(svc: Services, pubkey: str) -> dict[str, Any] | None:
    """The account behind a signed-in key (an active pass on it): partner, and whether its
    address is proven. None: a key nobody here knows (a browser key)."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["partner_id", "tier"],
    )
    rows = [r for r in rows if r.get("tier") != "agent"]
    if not rows:
        return None
    pid = m2o_id(rows[0].get("partner_id"))
    if not pid:
        return None
    partner = svc.odoo.read_one("res.partner", pid, ["name", "email", "belfry_pending_email"]) or {}
    return {
        "partner": pid,
        "name": partner.get("name") or "",
        "email": partner.get("email") or "",
        "verified": bool(partner.get("email")),
    }


def is_unverified(svc: Services, partner_id: int) -> bool:
    """A buyer's contact whose address is not proven yet (section 3)."""
    row = svc.odoo.read_one("res.partner", int(partner_id), ["email", "belfry_pending_email"])
    return bool(row) and bool(row.get("belfry_pending_email")) and not row.get("email")


def unverified_key(svc: Services, pubkey: str) -> bool:
    """Whether PUBKEY's account is a buyer's with an unproven address: no invites, no admin,
    no mailbox, no username of its choice."""
    if not own(svc) or not pubkey:
        return False
    try:
        rows = svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
            ["partner_id"],
        )
    except Exception:  # noqa: BLE001 — refuse nothing on a read error elsewhere
        return False
    pids = {m2o_id(r.get("partner_id")) for r in rows} - {None}
    return bool(pids) and all(is_unverified(svc, p) for p in pids)


def pending_member(svc: Services, email: str) -> dict[str, Any] | None:
    """For the e-mail code sign-in (connect.py): an unverified buyer with this address and an
    active pass. Only the sign-in asks this; every other lookup by e-mail never sees them."""
    email = (email or "").strip().lower()
    if not own(svc) or "@" not in email:
        return None
    for p in svc.odoo.search_read(
        "res.partner", [["belfry_pending_email", "=ilike", email]], ["name", "email"], limit=20
    ):
        full = svc.odoo.read_one("res.partner", p["id"], ["belfry_pending_email", "email"]) or {}
        if full.get("email") or (full.get("belfry_pending_email") or "").lower() != email:
            continue
        rows = svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["partner_id", "=", p["id"]]],
            ["keycast_pubkey"],
            limit=1,
        )
        if rows and rows[0].get("keycast_pubkey"):
            return {
                "partner": p["id"],
                "pubkey": rows[0]["keycast_pubkey"],
                "name": p.get("name") or email.split("@")[0],
                "email": email,
                "unverified": True,
            }
    return None


def _partner_by_email(svc: Services, email: str, but: int) -> int | None:
    """A proven account with this address (not BUT): the one a confirmation moves onto."""
    from . import apps

    m = apps.member_by_email(svc, email)
    if m and m["partner"] != but:
        return m["partner"]
    rows = svc.odoo.search_read("res.partner", [["email", "=ilike", email]], ["email"], limit=5)
    for r in rows:
        if r["id"] != but and (r.get("email") or "").strip().lower() == email.lower():
            return r["id"]
    return None


_confirm_lock = threading.Lock()


def confirm(svc: Services, partner_id: int) -> dict[str, Any]:
    """The buyer proved their address (the receipt's link, or an e-mail code). It becomes the
    contact's ``email``; when another account has it already, the passes move there and the
    temporary account closes. ``{"state": "confirmed" | "moved" | "already", "partner"}``."""
    from . import core, usernames

    with _confirm_lock:
        row = (
            svc.odoo.read_one("res.partner", partner_id, ["name", "email", "belfry_pending_email"])
            or {}
        )
        pending = (row.get("belfry_pending_email") or "").strip()
        if row.get("email") or not pending:
            return {"state": "already", "partner": partner_id}
        other = _partner_by_email(svc, pending, partner_id)
        if other is None:
            svc.odoo.write(
                "res.partner",
                [partner_id],
                {
                    "email": pending,
                    "belfry_pending_email": False,
                    "belfry_email_verified_at": core.now_utc_str(),
                },
            )
            log.info("sales: partner %s proved their address", partner_id)
            # now it may have a username of its own: its name, as for every member
            try:
                from . import community

                found = community.member_token(svc, partner_id)
                if found:
                    number = (
                        svc.odoo.read_one("res.partner", partner_id, ["belfry_member_number"]) or {}
                    ).get("belfry_member_number") or ""
                    usernames.give_default(svc, found[1], row.get("name") or "", str(number))
            except Exception:  # noqa: BLE001 — the address is proven; the name can come later
                log.exception("sales: no username for partner %s", partner_id)
            return {"state": "confirmed", "partner": partner_id}
        moved = _move_passes(svc, partner_id, other)
        log.info("sales: partner %s's passes moved to an existing account (%d)", partner_id, moved)
        return {"state": "moved", "partner": other}


def _move_passes(svc: Services, temp: int, other: int) -> int:
    """The temporary contact's passes open again on OTHER's account; the temporary ones close
    (its key leaves the relays, its signer account is suspended)."""
    from . import core

    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", temp], ["state", "=", "active"]],
        ["tier", "date_start", "date_end", "sale_order"],
    )
    st = store(svc)
    n = 0
    for r in rows:
        new = svc.odoo.create(
            "belfry.pass",
            {
                "partner_id": other,
                "tier": r.get("tier") or "member",
                "sold_start": r.get("date_start") or False,
                "sold_end": r.get("date_end") or False,
                "sale_order": r.get("sale_order") or False,
            },
        )
        core.activate_free_pass(svc, new)
        if r.get("sale_order") and st.order(r["sale_order"]):
            # the browser's session was on the temporary account: it ends with it
            st.update(
                r["sale_order"], pass_id=new, partner_id=other, moved_from=temp, bunker_url=None
            )
        core.revoke_pass(svc, r["id"], "moved to the account of the same address")
        n += 1
    svc.odoo.write("res.partner", [temp], {"belfry_pending_email": False})
    return n


# ── orders ──────────────────────────────────────────────────────────────────────────────────
def _clean_billing(raw: Any) -> dict[str, Any]:
    from .holder_invites import ApiError

    def bad() -> ApiError:
        return ApiError(422, "invalid_billing", "Check the billing details.")

    if raw in (None, {}):
        return {"kind": "person"}
    if not isinstance(raw, dict):
        raise bad()
    kind = raw.get("kind") or "person"
    if kind not in ("person", "organisation"):
        raise bad()
    out: dict[str, Any] = {"kind": kind}
    for k, n in (("name", 120), ("street", 200), ("zip", 20), ("city", 100)):
        v = " ".join(str(raw.get(k) or "").split())
        if len(v) > n:
            raise bad()
        out[k] = v
    country = str(raw.get("country") or "").strip().upper()
    if country and not COUNTRY_RE.match(country):
        raise bad()
    out["country"] = country
    vat = re.sub(r"[\s.\-]", "", str(raw.get("vat") or "")).upper()
    if vat and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{2,14}", vat):
        raise ApiError(422, "invalid_vat", "That VAT number is not valid.")
    out["vat"] = vat
    inv = str(raw.get("invoice_email") or "").strip()
    if inv and (not EMAIL.match(inv) or len(inv) > 254):
        raise bad()
    out["invoice_email"] = inv
    if kind == "organisation" and not (out["name"] and out["country"] and out["city"]):
        raise bad()
    return out


def _own_url(svc: Services, url: Any) -> str | None:
    """RETURN_URL when it is on the belfry's own domain (apex or a subdomain), else None."""
    if not isinstance(url, str) or len(url) > 500:
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    domain = svc.settings.nip05_domain.lower()
    if u.scheme != "https" or not (host == domain or host.endswith("." + domain)):
        return None
    if u.username or u.password or "?" in url or "#" in url:
        return None
    return url


def thanks_url(svc: Services) -> str:
    from . import console

    return console.console_url(svc.settings) + "/sales/thanks"


def pay_url(svc: Services, oid: str) -> str:
    from . import console

    return f"{console.console_url(svc.settings)}/pay/{oid}"


def _with_order(url: str, oid: str) -> str:
    return f"{url}?order={oid}"


def answer(svc: Services, o: dict[str, Any], viewer: str | None = None) -> dict[str, Any]:
    """The order as the API shows it (section 4)."""
    tot = o["total"]
    out: dict[str, Any] = {
        "order": o["id"],
        "state": o["state"],
        "product": o["product"],
        "total": {
            "net": euros(tot["net"]),
            "vat": euros(tot["vat"]),
            "gross": euros(tot["gross"]),
            "percent_off": tot["percent_off"],
        },
        "invoice": None,
        "card_url": None,
        "pay_url": None,
        "hold_until": o.get("hold_until"),
    }
    inv = o.get("invoice") or {}
    if inv.get("number"):
        out["invoice"] = {
            "number": inv["number"],
            "communication": inv.get("communication") or inv["number"],
            "url": inv.get("url"),
        }
    if o["state"] in ("pending", "invoiced") and tot["gross"] > 0:
        if (o.get("stripe") or {}).get("url"):
            out["card_url"] = o["stripe"]["url"]
        if inv.get("number") and bank_ready(svc):
            out["pay_url"] = pay_url(svc, o["id"])
    if o["state"] == "active" and o.get("bunker_url") and viewer == o["key"]:
        out["bunker_url"] = o["bunker_url"]
    return out


_checkout_lock = threading.Lock()


def checkout(svc: Services, key: str, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """POST /checkout. Returns (the order's answer, HTTP status)."""
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "Selling needs the belfry's own register.")
    pid = str(data.get("product") or "")
    p = product(svc, pid)
    if p is None:
        raise ApiError(404, "unknown_product", "No such product.")
    if not p.get("on_sale"):
        raise ApiError(409, "not_on_sale", "This product is not on sale.")
    name = " ".join(str(data.get("name") or "").split())[:100]
    email = str(data.get("email") or "").strip()
    if not EMAIL.match(email) or len(email) > 254:
        raise ApiError(422, "invalid_email", "That is not an e-mail address.")
    billing = _clean_billing(data.get("billing"))
    pay = data.get("pay") or "card"
    if pay not in ("card", "invoice"):
        raise ApiError(422, "invalid", 'pay: "card" or "invoice".')
    code = norm_code(data.get("code")) if data.get("code") else ""
    return_url = _own_url(svc, data.get("return_url"))
    flags: list[str] = []
    if billing["kind"] == "organisation" and billing.get("vat") and can_sell(svc):
        # checked before the invoice is made (outside the lock: VIES can be slow)
        from . import salesodoo

        valid = salesodoo.check_vat(billing["vat"])
        if valid is False:
            raise ApiError(422, "invalid_vat", "That VAT number is not valid.")
        if valid is None:
            flags.append("vies_down")  # taken; the admins check the number by hand
    st = store(svc)
    with _checkout_lock:
        # the same key, product and address while an order is open (or just became active: a
        # retried request): that order
        recent = iso(now() - dt.timedelta(hours=24))
        for o in st.orders((*OPEN, "active"), key=key):
            if (
                o["product"] == pid
                and o["email"].lower() == email.lower()
                and (o["state"] != "active" or o["created"] >= recent)
            ):
                return answer(svc, o, key), 200
        if len(st.orders(OPEN, key=key)) >= OPEN_PER_KEY:
            raise ApiError(429, "too_many", "Too many open orders. Pay or wait for one to end.")
        if st.count_address(email, int(time.time()) - 3600) >= ORDERS_PER_ADDRESS_HOUR:
            raise ApiError(429, "too_many", "Too many orders for this address. Try again later.")
        if code and not CODE_CHECKS.allow("key:" + key):
            raise ApiError(429, "too_many", "Too many codes tried. Try again in an hour.")
        pr, _ = price_now(p)
        if pr is None:
            raise ApiError(409, "not_on_sale", "This product is not on sale.")
        lft = left(svc, p)
        if lft is not None and lft <= 0:
            raise ApiError(409, "sold_out", "Sold out.")
        oid = "o_" + "".join(
            secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16)
        )
        percent = 0
        with st.tx() as db:
            if code:
                c = st.code(code, db)
                why = _usable(c, pid)
                if why:
                    raise ApiError(
                        404 if why == "unknown_code" else 409,
                        why,
                        "That code doesn't work for this."
                        if why == "unknown_code"
                        else "That code is used up.",
                    )
                percent = int(c["percent"])
                c["used"].append(oid)
                st.put_code(c, db)
        total = split(pr["amount"], p["vat_rate"], percent, billing["kind"])
        if total["gross"] > 0 and not can_sell(svc):
            _give_back(svc, {"id": oid, "code": code})
            raise ApiError(409, "cannot_sell", "Paid passes can't be sold here yet.")

        acct = account_of(svc, key)
        created = now()
        o = {
            "id": oid,
            "key": key,
            "product": pid,
            "tier": p["tier"],
            "role": p.get("role"),
            "valid_from": p.get("valid_from"),
            "valid_until": p.get("valid_until"),
            "product_name": p["name"],
            "unit": pr["amount"],
            "vat_rate": p["vat_rate"],
            "name": name or billing.get("name") or email.split("@")[0],
            "email": email,
            "code": code or None,
            "pay": pay,
            "billing": billing,
            "return_url": return_url,
            "total": total,
            "state": "pending",
            "step": "activate" if total["gross"] == 0 else "invoice",
            "created": iso(created),
            "hold_until": iso(created + dt.timedelta(days=HOLD_DAYS)) if total["gross"] else None,
            # a signed-in member or guest: the pass goes on their account
            "account": acct["partner"] if acct else None,
            "partner_id": acct["partner"] if acct else None,
            "pass_id": None,
            "invoice": {},
            "stripe": {},
            "flags": flags,
            "error": None,
        }
        st.put_order(o)
    log.info(
        "sales: order %s for %s (%s), %s",
        oid,
        pid,
        mask(email),
        "free" if not total["gross"] else "paid",
    )
    advance(svc, oid, first=True)
    o = st.order(oid) or o
    return answer(svc, o, key), 201


def advance(svc: Services, oid: str, first: bool = False) -> dict[str, Any]:
    """Do the order's next steps until one has to wait (for a payment) or fails (retried by the
    tick). FIRST: in the checkout request, where Odoo refusing the invoice ends the order."""
    from .holder_invites import ApiError

    st = store(svc)
    with _order_lock(oid):
        for _ in range(6):
            o = st.order(oid)
            if o is None or o["state"] in ("cancelled", "expired"):
                break
            if o["state"] == "active" and o.get("step") != "register_payment":
                break
            step = o.get("step")
            try:
                if step == "invoice":
                    from . import salesodoo

                    salesodoo.make_invoice(svc, o)
                elif step == "card":
                    from . import salesstripe

                    salesstripe.make_session(svc, o)
                elif step == "wait":
                    break
                elif step == "register_payment":
                    from . import salesodoo

                    salesodoo.register_payment(svc, o)
                elif step == "activate":
                    activate(svc, o)
                else:
                    break
            except Refused as exc:
                st.update(oid, error=str(exc)[:200])
                if first:
                    cancel(svc, oid, "Odoo refused the invoice")
                    raise ApiError(502, "accounting", "The invoice could not be made.") from exc
                log.warning("sales: order %s, %s refused: %s", oid, step, exc)
                break
            except Exception as exc:  # noqa: BLE001 — retried by the tick
                log.warning("sales: order %s, step %s waits: %s", oid, step, type(exc).__name__)
                st.update(oid, error=f"{step}: {type(exc).__name__}"[:200])
                break
        return st.order(oid) or {}


class Refused(Exception):
    """The seller's Odoo (or Stripe) refused something as it is: not retried by itself."""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _order_lock(oid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(oid, threading.Lock())


def after_invoice(svc: Services, o: dict[str, Any]) -> None:
    """The invoice is posted: the next step is a card session (Stripe set) or waiting."""
    store(svc).update(o["id"], state="invoiced", step="card" if stripe_ready(svc) else "wait")


def mark_paid(svc: Services, oid: str, how: str, ref: str = "", by: str = "") -> dict[str, Any]:
    """Money came (Stripe, the bank seen in Odoo, or an admin): register it in Odoo when it
    didn't come through Odoo, then open the pass. Idempotent."""
    st = store(svc)
    with _order_lock(oid):
        o = st.order(oid)
        if o is None:
            raise KeyError(oid)
        if o["state"] in ("paid", "active"):
            return o
        flags = list(o.get("flags") or [])
        if o["state"] in ("cancelled", "expired"):
            flags.append("paid_after_" + o["state"])
            log.warning("sales: order %s was paid after it ended (%s)", oid, o["state"])
        # the pass first (the person paid); the payment is booked in Odoo after, retried alone
        due = how in ("card", "hand") and bool((o.get("invoice") or {}).get("move_id"))
        st.update(
            oid,
            state="paid",
            step="activate",
            payment_due=due,
            paid={"how": how, "ref": ref[:100], "by": by[:100], "at": iso(now())},
            flags=flags,
        )
    return advance(svc, oid)


def activate(svc: Services, o: dict[str, Any]) -> None:
    """Open the pass: the contact (made now, for a browser key), the pass with the product's
    tier and dates, the browser key as the account's first session, the receipt."""
    from . import core

    st = store(svc)
    oid = o["id"]
    pid = o.get("partner_id")
    if not pid:
        pid = svc.odoo.create(
            "res.partner",
            {"name": o["name"], "belfry_pending_email": o["email"], "belfry_sale_order": oid},
        )
        o = st.update(oid, partner_id=pid)
    elif o.get("account"):
        acct = svc.odoo.read_one("res.partner", pid, ["email", "belfry_pending_email"]) or {}
        if not acct.get("email") and not acct.get("belfry_pending_email"):
            # a guest who never saved an address: the typed one waits for its proof
            svc.odoo.write("res.partner", [pid], {"belfry_pending_email": o["email"]})
    if not o.get("pass_id"):
        start = o.get("valid_from") or today().isoformat()
        end = o.get("valid_until") or core.MEMBERSHIP_END.isoformat()
        if end < today().isoformat():
            end = today().isoformat()
        pass_id = svc.odoo.create(
            "belfry.pass",
            {
                "partner_id": pid,
                "tier": o["tier"],
                "sale_order": oid,
                "sold_start": start,
                "sold_end": end,
            },
        )
        o = st.update(oid, pass_id=pass_id)
    core.activate_free_pass(svc, o["pass_id"])
    if o.get("role"):
        with contextlib.suppress(Exception):
            from . import roles

            if roles.exists(svc, o["role"]):
                svc.odoo.write("res.partner", [pid], {"belfry_role": o["role"]})
    if not o.get("account") and not o.get("bunker_url"):
        o = st.update(oid, bunker_url=_bind_key(svc, pid, o["key"]))
    o = st.update(
        oid,
        state="active",
        step="register_payment" if o.get("payment_due") else "done",
        activated=iso(now()),
        error=None,
    )
    if st.once("receipt:" + oid):
        from . import salesmail

        salesmail.receipt(svc, st.order(oid) or o)
    log.info("sales: order %s active (pass %s)", oid, o["pass_id"])


def _bind_key(svc: Services, partner_id: int, key: str) -> str | None:
    """The browser key as the new account's first session (as guest links do)."""
    from . import community
    from .clients.keycast import ClientKeyTaken

    found = community.member_token(svc, partner_id)
    if not found:
        return None
    try:
        bunker = svc.keycast.create_bunker(found[1], "Tickets", key)
    except ClientKeyTaken:
        log.warning("sales: partner %s: the browser key belongs to another account", partner_id)
        return None
    return bunker.get("bunker_url") or None


def cancel(svc: Services, oid: str, reason: str, by: str = "") -> dict[str, Any]:
    """An order that ends unpaid (an admin, or Odoo refused it): the code's use comes back; an
    invoice gets a credit note (the tick retries it)."""
    st = store(svc)
    o = st.order(oid)
    if o is None:
        raise KeyError(oid)
    if o["state"] not in ("pending", "invoiced"):
        return o
    o = st.update(
        oid,
        state="cancelled",
        step="credit_note" if (o.get("invoice") or {}).get("move_id") else "done",
        ended={"reason": reason[:200], "by": by[:100], "at": iso(now())},
    )
    _give_back(svc, o)
    end_unpaid(svc, o)
    return st.order(oid) or o


def end_unpaid(svc: Services, o: dict[str, Any]) -> None:
    """The steps after an unpaid end: Stripe's session expires, the invoice is credited."""
    st = store(svc)
    if (o.get("stripe") or {}).get("id"):
        with contextlib.suppress(Exception):
            from . import salesstripe

            salesstripe.expire_session(svc, o)
    if o.get("step") == "credit_note":
        try:
            from . import salesodoo

            salesodoo.credit_note(svc, o)
            st.update(o["id"], step="done")
        except Exception as exc:  # noqa: BLE001 — the tick tries again
            log.warning("sales: order %s, credit note waits: %s", o["id"], type(exc).__name__)


def expire_holds(svc: Services) -> int:
    """Unpaid orders past their hold expire; a reminder goes 2 days before."""
    st = store(svc)
    t = now()
    n = 0
    for o in st.orders(("pending", "invoiced")):
        if not o.get("hold_until"):
            continue
        until = parse_iso(o["hold_until"])
        if until <= t:
            o = st.update(
                o["id"],
                state="expired",
                step="credit_note" if (o.get("invoice") or {}).get("move_id") else "done",
                ended={"reason": "hold ended", "by": "", "at": iso(t)},
            )
            _give_back(svc, o)
            end_unpaid(svc, o)
            n += 1
        elif until - t <= dt.timedelta(days=REMIND_DAYS) and (o.get("invoice") or {}).get("number"):
            if st.once("remind:" + o["id"]):
                from . import salesmail

                salesmail.reminder(svc, o)
    return n


def retry(svc: Services) -> None:
    """Steps that failed earlier: invoices, sessions, payments, activations, credit notes."""
    st = store(svc)
    for o in st.orders():
        if (o["state"] in OPEN and o.get("step") in ("invoice", "card", "activate")) or (
            o["state"] == "active" and o.get("step") == "register_payment"
        ):
            advance(svc, o["id"])
        elif o["state"] in ("cancelled", "expired") and o.get("step") == "credit_note":
            end_unpaid(svc, o)


TICK_SECONDS = 900
_last_tick = {"at": 0.0}


def tick(svc: Services, force: bool = False) -> None:
    """Every 15 minutes (followers' loop runs every 5): holds, retries, and the open invoices
    read from Odoo (paid by transfer → active; fully credited → the pass closes)."""
    if not own(svc):
        return
    if not force and time.monotonic() - _last_tick["at"] < TICK_SECONDS - 5:
        return
    _last_tick["at"] = time.monotonic()
    for job in (expire_holds, retry, _read_invoices):
        try:
            job(svc)
        except Exception as exc:  # noqa: BLE001 — next round
            log.warning("sales: %s failed: %s", job.__name__, type(exc).__name__)


def _read_invoices(svc: Services) -> None:
    if not odoo_ready(svc):
        return
    from . import salesodoo

    salesodoo.read_open_invoices(svc)


# ── offers (in kind) ────────────────────────────────────────────────────────────────────────
def offer_answer(f: dict[str, Any]) -> dict[str, Any]:
    out = {"offer": f["id"], "state": f["state"], "product": f["product"]}
    if f["state"] in ("accepted", "declined"):
        out["percent"] = f.get("decided_percent") or 0
    return out


def make_offer(svc: Services, key: str, data: dict[str, Any]) -> dict[str, Any]:
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "Selling needs the belfry's own register.")
    p = product(svc, str(data.get("product") or ""))
    if p is None:
        raise ApiError(404, "unknown_product", "No such product.")
    if not p.get("on_sale"):
        raise ApiError(409, "not_on_sale", "This product is not on sale.")
    name = " ".join(str(data.get("name") or "").split())[:100]
    email = str(data.get("email") or "").strip()
    if not EMAIL.match(email) or len(email) > 254:
        raise ApiError(422, "invalid_email", "That is not an e-mail address.")
    text = str(data.get("offer") or "").strip()
    if not name or not text or len(text) > 2000:
        raise ApiError(422, "invalid", "Say who you are and what you offer (2000 characters).")
    try:
        percent = int(data.get("percent"))
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid", "percent: 50 to 100.") from exc
    if not 50 <= percent <= 100:
        raise ApiError(422, "invalid", "percent: 50 to 100.")
    st = store(svc)
    mine = [f for f in st.offers(key) if f["state"] == "open"]
    if len(mine) >= 3 or not OFFERS.allow("key:" + key):
        raise ApiError(429, "too_many", "Too many offers for now.")
    f = {
        "id": "f_"
        + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16)),
        "key": key,
        "product": p["id"],
        "name": name,
        "email": email,
        "offer": text,
        "percent": percent,
        "state": "open",
        "created": iso(now()),
    }
    st.put_offer(f)
    from . import salesmail

    salesmail.offer_to_admins(svc, f)
    log.info("sales: offer %s for %s (%s)", f["id"], p["id"], mask(email))
    return f


def decide_offer(svc: Services, fid: str, percent: Any, note: str, by: str) -> dict[str, Any]:
    from .holder_invites import ApiError

    st = store(svc)
    f = st.offer(fid)
    if f is None:
        raise ApiError(404, "not_found", "No such offer.")
    if f["state"] != "open":
        raise ApiError(409, "decided", "This offer has an answer already.")
    try:
        percent = int(percent)
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid", "percent: 0, or 50 to 100.") from exc
    if percent != 0 and not 50 <= percent <= 100:
        raise ApiError(422, "invalid", "percent: 0, or 50 to 100.")
    note = " ".join(str(note or "").split())[:500]
    code = None
    if percent:
        code = make_codes(
            svc, percent, f["product"], 1, 1, None, f"Offer from {f['name']}", by, fid
        )[0]["code"]
    f.update(
        state="accepted" if percent else "declined",
        decided_percent=percent,
        note=note,
        decided_by=by[:100],
        decided_at=iso(now()),
        code=code,
    )
    st.put_offer(f)
    from . import salesmail

    salesmail.offer_answer(svc, f)
    return f


# ── the API ─────────────────────────────────────────────────────────────────────────────────
async def _signed(request: Request, method: str = "POST") -> tuple[Services, str, dict[str, Any]]:
    from .holder_invites import ApiError, authenticate

    s = request.app.state.services
    body = await request.body() if method == "POST" else b""
    pubkey = authenticate(
        s,
        request.app.state.api_seen,
        request.headers.get("authorization"),
        method,
        request.url.path,
        body,
    )
    if method != "POST":
        return s, pubkey, {}
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "Send a JSON object.")
    return s, pubkey, data


def _admin(svc: Services, pubkey: str) -> str:
    """The admin's name for the records; ApiError 403 for anyone else."""
    from . import apps
    from .holder_invites import ApiError

    if pubkey not in apps.admin_pubkeys(svc) or unverified_key(svc, pubkey):
        raise ApiError(403, "not_admin", "Only the belfry's admins do this.")
    acct = account_of(svc, pubkey) or {}
    return acct.get("name") or "an admin"


NO_STORE = {"Cache-Control": "no-store"}


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError

    @app.get(API + "/products")
    async def api_products(request: Request) -> JSONResponse:
        s = request.app.state.services
        if not own(s):
            raise ApiError(409, "unsupported", "Selling needs the belfry's own register.")
        out = await run_in_threadpool(listing, s)
        return JSONResponse(out, headers={"Cache-Control": "public, max-age=30"})

    @app.post(API + "/codes/check")
    async def api_code_check(request: Request) -> JSONResponse:
        s, key, data = await _signed(request)
        if not CODE_CHECKS.allow("key:" + key):
            raise ApiError(429, "too_many", "Too many codes tried. Try again in an hour.")
        pid = str(data.get("product") or "")
        c = store(s).code(norm_code(data.get("code")))
        why = _usable(c, pid) if product(s, pid) else "unknown_code"
        if why == "unknown_code":
            raise ApiError(404, "unknown_code", "That code doesn't work for this.")
        if why == "code_used":
            raise ApiError(409, "code_used", "That code is used up.")
        return JSONResponse(
            {"percent": c["percent"], "left": max(0, c["uses"] - len(c["used"]))},
            headers=NO_STORE,
        )

    @app.post(API + "/codes")
    async def api_codes(request: Request) -> JSONResponse:
        s, key, data = await _signed(request)
        by = await run_in_threadpool(_admin, s, key)
        made = await run_in_threadpool(
            lambda: make_codes(
                s,
                data.get("percent"),
                data.get("product") or None,
                data.get("uses", 1),
                data.get("count", 1),
                data.get("expires") or None,
                str(data.get("note") or ""),
                by,
            )
        )
        return JSONResponse(
            {"codes": [code_row(c) for c in made]}, status_code=201, headers=NO_STORE
        )

    @app.post(API + "/checkout")
    async def api_checkout(request: Request) -> JSONResponse:
        s, key, data = await _signed(request)
        out, status = await run_in_threadpool(checkout, s, key, data)
        return JSONResponse(out, status_code=status, headers=NO_STORE)

    @app.get(API + "/checkout/{oid}")
    async def api_checkout_get(oid: str, request: Request) -> JSONResponse:
        s, key, _ = await _signed(request, "GET")
        o = store(s).order(oid) if ORDER_RE.match(oid) else None
        if o is None or o["key"] != key:
            raise ApiError(404, "not_found", "No such order.")
        if o["state"] in OPEN:
            o = await run_in_threadpool(_refresh, s, o)
        return JSONResponse(answer(s, o, key), headers=NO_STORE)

    @app.get(API + "/billing")
    async def api_billing(request: Request) -> JSONResponse:
        s, key, _ = await _signed(request, "GET")
        acct = await run_in_threadpool(account_of, s, key)
        if not acct or not acct["verified"]:
            raise ApiError(403, "not_verified", "Sign in with a proven address first.")
        rows = [
            o
            for o in store(s).orders()
            if o.get("partner_id") == acct["partner"] and o.get("billing")
        ]
        return JSONResponse({"billing": rows[0]["billing"] if rows else None}, headers=NO_STORE)

    @app.post(API + "/offers")
    async def api_offer(request: Request) -> JSONResponse:
        s, key, data = await _signed(request)
        f = await run_in_threadpool(make_offer, s, key, data)
        return JSONResponse(offer_answer(f), status_code=201, headers=NO_STORE)

    @app.get(API + "/offers/{fid}")
    async def api_offer_get(fid: str, request: Request) -> JSONResponse:
        s, key, _ = await _signed(request, "GET")
        f = store(s).offer(fid) if OFFER_RE.match(fid) else None
        if f is None or f["key"] != key:
            raise ApiError(404, "not_found", "No such offer.")
        return JSONResponse(offer_answer(f), headers=NO_STORE)

    @app.post(API + "/offers/{fid}/decide")
    async def api_offer_decide(fid: str, request: Request) -> JSONResponse:
        s, key, data = await _signed(request)
        by = await run_in_threadpool(_admin, s, key)
        if not OFFER_RE.match(fid):
            raise ApiError(404, "not_found", "No such offer.")
        f = await run_in_threadpool(
            decide_offer, s, fid, data.get("percent"), str(data.get("note") or ""), by
        )
        return JSONResponse(offer_answer(f), headers=NO_STORE)


# ── the buyer's pages on belfry.<domain> (fixed text only) ────────────────────────────────────
PAGE_TEXT = {
    "thanks": "Thank you. Your order is in: the next steps come by e-mail.",
    "gone": "This link doesn't work (any more).",
    "ask": "Confirm that this address is yours. Then you can sign in with it from any device.",
    "confirmed": "Done: your address is confirmed. You can sign in with it from any device.",
    "moved": "This address had an account here already: your pass is on that account now. "
    "Sign in with your address.",
    "already": "This address is confirmed already.",
}


def _page(key: str, status: int = 200, form: str = "") -> Any:
    from fastapi.responses import HTMLResponse

    from . import theme

    body = f'<div class="card"><p>{PAGE_TEXT[key]}</p>{form}</div>'
    return HTMLResponse(
        theme.frame("Your pass", body),
        status_code=status,
        headers={**NO_STORE, "Referrer-Policy": "no-referrer"},
    )


def register_pages(app: FastAPI) -> None:
    @app.get("/console/sales/thanks", include_in_schema=False)
    async def thanks(request: Request):
        return _page("thanks")

    @app.get("/console/sales/confirm/{token}", include_in_schema=False)
    async def confirm_page(token: str, request: Request):
        s = request.app.state.services
        if not store(s).confirm_order(token):
            return _page("gone", 404)
        # a button: mail scanners open links, only a person presses it
        form = (
            f'<form method="post" action="/sales/confirm/{token}">'
            '<button class="btn" type="submit">Confirm my e-mail</button></form>'
        )
        return _page("ask", form=form)

    @app.post("/console/sales/confirm/{token}", include_in_schema=False)
    async def confirm_post(token: str, request: Request):
        s = request.app.state.services
        oid = store(s).confirm_order(token)
        o = store(s).order(oid) if oid else None
        if o is None or not o.get("partner_id"):
            return _page("gone", 404)
        partner = o.get("moved_from") or o["partner_id"]
        out = await run_in_threadpool(confirm, s, int(partner))
        return _page(out["state"] if not o.get("moved_from") else "already")


def _refresh(svc: Services, o: dict[str, Any]) -> dict[str, Any]:
    """A buyer looking at their order: an unpaid step is tried again, and a card session that
    expired (Stripe keeps them 24 hours) is made anew."""
    if o.get("step") in ("invoice", "card", "register_payment", "activate"):
        o = advance(svc, o["id"]) or o
    if o["state"] == "invoiced" and (o.get("stripe") or {}).get("expires_at"):
        if int(o["stripe"]["expires_at"]) < time.time() + 60 and stripe_ready(svc):
            store(svc).update(o["id"], step="card")
            o = advance(svc, o["id"]) or o
    return o


def hmac_sign(svc: Services, text: str) -> str:
    """The belfry's signature of TEXT (signed /pay links)."""
    key = (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()
    return hmac.new(key, b"belfry-pay:" + text.encode(), hashlib.sha256).hexdigest()[:32]
