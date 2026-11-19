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
import html
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
TEST_SETTINGS_PARAM = "belfry.sales.settings.test"
BASE_SECRETS = ("odoo_api_key", "stripe_key", "stripe_webhook_secret")
# the test environment's own secrets (docs/sales.md §9): the same names with "test_" in front
SECRET_KEYS = (*BASE_SECRETS, *(f"test_{k}" for k in BASE_SECRETS))
ENVS = ("live", "test")
# what belongs to one Odoo database: a test database has its own ids, so none of these is
# shared between the two environments
ODOO_KEYS = (
    "odoo_url",
    "odoo_db",
    "company",
    "company_name",
    "odoo_products",
    "tax_included",
    "tax_excluded",
    "card_journal",
    "manual_journal",
    "ticket_product",
)
HOLD_DAYS = 7
REMIND_DAYS = 2  # one reminder this long before the hold ends
CONFIRM_HOURS = 48  # a free order waits this long for its address to be confirmed
OPEN_PER_KEY = 5
OPEN_PER_ADDRESS = 5
ORDERS_PER_ADDRESS_HOUR = 20
ORDERS_PER_IP_HOUR = 30  # orders made from one IP (a /64 for IPv6): shared Wi-Fi at a launch
FAILS_PER_IP_HOUR = 60  # refused checkouts (wrong code, invalid VAT…) from one IP
HOLDS_PER_IP = 10  # unpaid orders of a product with limited places, per IP
MAILS_PER_ADDRESS_DAY = 5
MAIL_TRIES = 8
MAIL_DAYS = 3  # a mail held back by the daily limit gives up after this
STEP_TRIES = 30  # a step retried by the tick (every 15 minutes) stops after this, flagged
CODE_CHECKS_PER_HOUR = 10
OFFERS_PER_DAY = 3
OPEN = ("confirm", "pending", "invoiced", "paid")
STATES = (*OPEN, "active", "cancelled", "expired")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PRODUCT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CODE_RE = re.compile(r"^[A-Z0-9-]{4,40}$")
ORDER_RE = re.compile(r"^o_[a-z0-9]{16}$")
OFFER_RE = re.compile(r"^f_[a-z0-9]{16}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
# Billing countries (ISO 3166 alpha-2) whose organisations' VAT numbers go to VIES. Greece is GR
# here (its VAT numbers start with EL); Northern Ireland's XI numbers are checked for GB.
EU_COUNTRIES = set(
    "AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split()
)
# A tax id outside the EU (EIN, ABN, GSTIN, UID, RFC…): as typed, letters, digits and . - / and
# spaces, 2 to 32 characters with at least 2 letters or digits.
TAX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./-]{0,30}[A-Za-z0-9]$")
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
                created INTEGER NOT NULL, ip TEXT NOT NULL DEFAULT '', data TEXT NOT NULL);
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
                token TEXT PRIMARY KEY, order_id TEXT NOT NULL, partner_id INTEGER NOT NULL,
                address TEXT NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS mails (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT NOT NULL,
                address TEXT NOT NULL, created INTEGER NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'due', sent_at INTEGER, claimed_at INTEGER,
                next_at INTEGER, UNIQUE (kind, ref));
            CREATE TABLE IF NOT EXISTS hits (
                key TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS hits_key ON hits (key, at);
            """
        )
        self._migrate()

    def _migrate(self) -> None:
        """Columns added after a sales.sqlite was first made: an older file never crashes."""
        wanted = {
            "orders": [("ip", "TEXT NOT NULL DEFAULT ''")],
            "confirms": [
                ("partner_id", "INTEGER NOT NULL DEFAULT 0"),
                ("address", "TEXT NOT NULL DEFAULT ''"),
                ("kind", "TEXT NOT NULL DEFAULT 'receipt'"),
            ],
            "mails": [("claimed_at", "INTEGER"), ("next_at", "INTEGER")],
        }
        with self._lock:
            for table, cols in wanted.items():
                have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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
                "INSERT OR REPLACE INTO orders (id, key, email, state, created, ip, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    o["id"],
                    o["key"],
                    o["email"].lower(),
                    o["state"],
                    int(parse_iso(o["created"]).timestamp()),
                    o.get("ip") or "",
                    json.dumps(o),
                ),
            )
        _listing_cache["at"] = 0.0  # places changed

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

    def open_for(
        self, column: str, value: str, product: str | None = None, mailed: bool = False
    ) -> int:
        """Open (unpaid) orders of an address or an IP, for one product or all. MAILED: only
        those whose cancel link was mailed to the address (the per-address limit: an order its
        owner can't see in their inbox doesn't lock them out)."""
        assert column in ("email", "ip")
        with self._lock:
            rows = self._db.execute(
                f"SELECT data FROM orders WHERE {column} = ? AND state IN ('pending', "
                "'invoiced', 'confirm')",
                (value.lower() if column == "email" else value,),
            ).fetchall()
        out = 0
        for r in rows:
            o = json.loads(r[0])
            if product is not None and o["product"] != product:
                continue
            if mailed and not o.get("cancel_mailed"):
                continue
            out += 1
        return out

    def count_ip(self, ip: str, since: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM orders WHERE ip = ? AND created >= ?", (ip, since)
            ).fetchone()[0]

    def hits_count(self, key: str, seconds: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM hits WHERE key = ? AND at >= ?",
                (key, int(time.time()) - seconds),
            ).fetchone()[0]

    def hit(self, key: str, limit: int, seconds: int) -> bool:
        """A rate limit kept in the file (survives restarts): False when KEY is over LIMIT."""
        t = int(time.time())
        with self._lock:
            self._db.execute("DELETE FROM hits WHERE at < ?", (t - 86400 * 2,))
            n = self._db.execute(
                "SELECT COUNT(*) FROM hits WHERE key = ? AND at >= ?", (key, t - seconds)
            ).fetchone()[0]
            if n >= limit:
                return False
            self._db.execute("INSERT INTO hits (key, at) VALUES (?, ?)", (key, t))
            return True

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
        """Whether this event still has to be handled: new, or received but never finished (a
        crash in between: handling again is safe, mark_paid is idempotent)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO stripe_events (id, at, result) VALUES (?, ?, ?)",
                (event_id, int(time.time()), "received"),
            )
            if cur.rowcount == 1:
                return True
            row = self._db.execute(
                "SELECT result FROM stripe_events WHERE id = ?", (event_id,)
            ).fetchone()
        return bool(row) and row[0] == "received"

    def event_result(self, event_id: str, result: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE stripe_events SET result = ? WHERE id = ?", (result[:60], event_id)
            )

    def forget_event(self, event_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM stripe_events WHERE id = ?", (event_id,))

    def confirm_token(self, oid: str, partner_id: int, address: str, kind: str) -> str:
        """The confirmation link's token, bound to the order, the contact and the address it is
        mailed to (only ever to that address). KIND: ``receipt`` (an account's unproven address)
        or ``order`` (a free order waiting for its address)."""
        address = address.strip().lower()
        with self._lock:
            row = self._db.execute(
                "SELECT token FROM confirms WHERE order_id = ? AND partner_id = ? AND address = ? "
                "AND kind = ?",
                (oid, int(partner_id), address, kind),
            ).fetchone()
            if row:
                return row[0]
            token = secrets.token_urlsafe(24)
            self._db.execute(
                "INSERT INTO confirms (token, order_id, partner_id, address, kind, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token, oid, int(partner_id), address, kind, int(time.time())),
            )
            return token

    def confirm_of(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT order_id, partner_id, address, kind FROM confirms WHERE token = ?",
                (token,),
            ).fetchone()
        if not row:
            return None
        return {"order": row[0], "partner": int(row[1]), "address": row[2], "kind": row[3]}

    # the outbox: a mail is marked sent only once the provider took it
    def queue_mail(self, kind: str, ref: str, address: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO mails (kind, ref, address, created) VALUES (?, ?, ?, ?)",
                (kind, ref, address.strip().lower(), int(time.time())),
            )
        return cur.rowcount == 1

    CLAIM_SECONDS = 600

    def due_mails(self) -> list[dict[str, Any]]:
        """Mails to try now: due (and not backing off), or claimed long ago by a sender that
        never finished."""
        t = int(time.time())
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, ref, address, created, tries FROM mails WHERE "
                "(state = 'due' AND (next_at IS NULL OR next_at <= ?)) OR "
                "(state = 'sending' AND claimed_at < ?) ORDER BY id",
                (t, t - self.CLAIM_SECONDS),
            ).fetchall()
        return [
            dict(zip(("id", "kind", "ref", "address", "created", "tries"), r, strict=True))
            for r in rows
        ]

    def claim_mail(self, mid: int) -> bool:
        """One sender at a time: the row goes to ``sending``; False when another has it."""
        t = int(time.time())
        with self._lock:
            cur = self._db.execute(
                "UPDATE mails SET state = 'sending', claimed_at = ? WHERE id = ? AND "
                "((state = 'due' AND (next_at IS NULL OR next_at <= ?)) OR "
                "(state = 'sending' AND claimed_at < ?))",
                (t, mid, t, t - self.CLAIM_SECONDS),
            )
        return cur.rowcount == 1

    def mail_done(
        self, mid: int, state: str, tried: bool = True, next_at: int | None = None
    ) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE mails SET state = ?, tries = tries + ?, sent_at = ?, next_at = ?, "
                "claimed_at = NULL WHERE id = ?",
                (
                    state,
                    1 if tried else 0,
                    int(time.time()) if state == "sent" else None,
                    next_at,
                    mid,
                ),
            )

    def sent_to(self, address: str, seconds: int = 86400) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM mails WHERE address = ? AND state = 'sent' AND sent_at >= ?",
                (address.strip().lower(), int(time.time()) - seconds),
            ).fetchone()[0]

    def mail_state(self, kind: str, ref: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT state FROM mails WHERE kind = ? AND ref = ?", (kind, ref)
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


def env_of(o: dict[str, Any] | None) -> str:
    """An order's environment, fixed when it was made: ``test`` (a tester's order: Stripe's test
    mode and the test Odoo) or ``live``. Orders made before environments existed are live."""
    return "test" if (o or {}).get("env") == "test" else "live"


def _env(env: str) -> str:
    if env not in ENVS:
        raise ValueError("unknown environment")
    return env


def settings(svc: Services, env: str = "live") -> dict[str, Any]:
    """The non-secret sales settings, with the environment's ACCOUNTING_* as the fallback. ENV
    ``test``: the Odoo part (ODOO_KEYS) is the test environment's own, with no fallback; the
    rest (currency, bank, requests channel, invoice ref prefix) is shared."""
    st = _param(svc, SETTINGS_PARAM)
    st = st if isinstance(st, dict) else {}
    cfg = svc.settings
    out = {
        "currency": st.get("currency") or "EUR",
        "odoo_url": st.get("odoo_url") or cfg.accounting_odoo_url,
        "odoo_db": st.get("odoo_db") or cfg.accounting_odoo_db,
        "company": st.get("company") or None,
        "company_name": st.get("company_name") or cfg.accounting_issuer,
        "odoo_products": st.get("odoo_products") or {},
        "fallback_products": _env_products(cfg.accounting_products),
        "tax_included": st.get("tax_included") or _int(cfg.accounting_tax_included),
        "tax_excluded": st.get("tax_excluded") or _int(cfg.accounting_tax_excluded),
        "card_journal": st.get("card_journal") or _int(cfg.accounting_journal),
        "manual_journal": st.get("manual_journal") or _int(cfg.accounting_manual_journal),
        "ref_prefix": st.get("ref_prefix") or cfg.accounting_ref_prefix or "Belfry ",
        "bank_holder": st.get("bank_holder") or "",
        "requests_channel": st.get("requests_channel") or "",
        "bank_iban": st.get("bank_iban") or "",
        "bank_bic": st.get("bank_bic") or "",
        # event tickets through the checkout (ticketsales.py, docs/sales.md §10): the switch,
        # where the public event page sends buyers, and the Odoo product a ticket is invoiced as
        "tickets": bool(st.get("tickets")),
        "checkout_url": st.get("checkout_url") or "",
        "ticket_product": _int(st.get("ticket_product")),
    }
    if _env(env) == "test":
        t = _param(svc, TEST_SETTINGS_PARAM)
        t = t if isinstance(t, dict) else {}
        for k in ODOO_KEYS:
            out[k] = t.get(k) or ({} if k == "odoo_products" else None)
        out["ticket_product"] = _int(out["ticket_product"])
        out["company_name"] = t.get("company_name") or out.get("company_name") or ""
        out["fallback_products"] = {}  # the server's ACCOUNTING_PRODUCTS are production's
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


def save_settings(svc: Services, values: dict[str, Any], env: str = "live") -> None:
    """Save sales settings. ENV ``test`` takes only the Odoo part (ODOO_KEYS), kept apart from
    production's (param ``belfry.sales.settings.test``)."""
    param = SETTINGS_PARAM
    if _env(env) == "test":
        if set(values) - set(ODOO_KEYS):
            raise ValueError("the test environment has only its own Odoo settings")
        param = TEST_SETTINGS_PARAM
    st = _param(svc, param)
    st = st if isinstance(st, dict) else {}
    st.update(values)
    svc.odoo.set_param(param, json.dumps(st))


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


def _secret_name(name: str, env: str) -> str:
    if name not in BASE_SECRETS:
        raise ValueError("unknown secret")
    return f"test_{name}" if _env(env) == "test" else name


def secret(svc: Services, name: str, env: str = "live") -> str:
    """A secret: the console's, else the environment's (production only: the test environment
    has no server-side fallback). Never logged, never in an answer."""
    got = _read_secrets(svc).get(_secret_name(name, env), "")
    if got or env == "test":
        return got
    cfg = svc.settings
    return {
        "odoo_api_key": cfg.accounting_odoo_api_key,
        "stripe_key": getattr(cfg, "stripe_secret_key", "") or "",
        "stripe_webhook_secret": cfg.stripe_webhook_secret,
    }.get(name, "") or ""


def set_secret(svc: Services, name: str, value: str | None, env: str = "live") -> None:
    """Write (or with None clear: back to the environment's) one secret, atomically, mode 600.
    ENV ``test`` writes the test environment's own (``test_<name>``)."""
    name = _secret_name(name, env)
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


def secret_state(svc: Services, name: str, env: str = "live") -> dict[str, Any]:
    """What the console may show of a secret: set or not, where from, and its last 4."""
    mine = _read_secrets(svc).get(_secret_name(name, env), "")
    value = mine or secret(svc, name, env)
    return {
        "set": bool(value),
        "from": "console" if mine else ("environment" if value else ""),
        "last4": value[-4:] if len(value) >= 12 else "",
    }


def odoo_ready(svc: Services, env: str = "live") -> bool:
    st = settings(svc, env)
    return bool(st["odoo_url"] and st["odoo_db"] and secret(svc, "odoo_api_key", env))


def accounting_ready(svc: Services, env: str = "live") -> bool:
    """Invoices can be made: the connection, a product and both taxes picked."""
    st = settings(svc, env)
    return bool(
        odoo_ready(svc, env)
        and (st["odoo_products"] or st["fallback_products"])
        and st["tax_included"]
        and st["tax_excluded"]
    )


def stripe_ready(svc: Services, env: str = "live") -> bool:
    return bool(secret(svc, "stripe_key", env))


def bank_ready(svc: Services) -> bool:
    st = settings(svc)
    return bool(st["bank_holder"] and st["bank_iban"])


def can_sell(svc: Services, env: str = "live") -> bool:
    """Paid products can be sold: invoices, and at least one way to pay them (bank details are
    shared by both environments)."""
    return accounting_ready(svc, env) and (stripe_ready(svc, env) or bank_ready(svc))


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
    head = (
        "Selling passes: paid passes can be sold"
        if s["can_sell"]
        else ("Selling passes: free passes only")
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
    _listing_cache["at"] = 0.0
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
    """Places held: open and active orders of the product. A tester's (test) orders take no
    place from real buyers."""
    return sum(
        1
        for o in store(svc).orders((*OPEN, "active"))
        if o["product"] == pid and env_of(o) == "live"
    )


def taken_by_tests(svc: Services, pid: str) -> int:
    """Test orders of the product (shown apart in the console)."""
    return sum(
        1
        for o in store(svc).orders((*OPEN, "active"))
        if o["product"] == pid and env_of(o) == "test"
    )


def uses_key(o: dict[str, Any]) -> str:
    """Where an order's use of a code is recorded: a test order's never takes a real use."""
    return "test_used" if env_of(o) == "test" else "used"


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


_listing_cache: dict[str, Any] = {"at": 0.0, "path": "", "data": None}
LISTING_SECONDS = 15


def listing(svc: Services) -> dict[str, Any]:
    """GET /products, kept 15 seconds (or until an order changes or a product is saved)."""
    path = svc.settings.register_path
    c = _listing_cache
    if c["data"] is not None and c["path"] == path and time.monotonic() - c["at"] < LISTING_SECONDS:
        return c["data"]
    rows = [
        x for p in products(svc) if p.get("on_sale") and (x := public_product(svc, p)) is not None
    ]
    data = {
        "currency": settings(svc)["currency"],
        "can_sell": can_sell(svc),
        "offers": requests_site(svc) is not None,
        "products": rows,
    }
    c.update(at=time.monotonic(), path=path, data=data)
    return data


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
    bound_key: str = "",
    bound_email: str = "",
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
                "bound_key": bound_key or None,
                "bound_email": bound_email.lower() or None,
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


def _usable(
    c: dict[str, Any] | None, pid: str, key: str = "", verified_email: str = ""
) -> str | None:
    """Why the code can't be used for product PID by KEY (whose account has VERIFIED_EMAIL
    proven, if any), or None. A personal code (an offer's) works only for the requester's key or
    an account proven with the requester's address."""
    if c is None or c.get("revoked"):
        return "unknown_code"
    if c.get("bound_key") or c.get("bound_email"):
        mine = key and key == c.get("bound_key")
        theirs = verified_email and verified_email.lower() == (c.get("bound_email") or "").lower()
        if not (mine or theirs):
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
        for k in ("used", "test_used"):
            if c and o["id"] in (c.get(k) or []):
                c[k].remove(o["id"])
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


def client_ip(request: Request) -> str:
    """The client's address as Caddy forwards it (as events.py reads it)."""
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?"
    )


def ip_tag(svc: Services, ip: str) -> str:
    """An IP kept for the limits only as a keyed hash (never the address itself); an IPv6
    address counts as its /64 (one household or one phone gets many)."""
    import ipaddress

    raw = (ip or "?").strip()
    try:
        addr = ipaddress.ip_address(raw)
        if addr.version == 6:
            raw = str(ipaddress.ip_network(f"{addr}/64", strict=False))
    except ValueError:
        pass
    return hmac_sign(svc, "ip:" + raw)[:16]


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
        "pending": (partner.get("belfry_pending_email") or "").strip().lower(),
    }


def is_unverified(svc: Services, partner_id: int) -> bool:
    """A buyer's contact whose address is not proven yet (section 3). Always False on a belfry
    that keeps its register in Odoo (the field doesn't exist there)."""
    if not own(svc):
        return False
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


def _has_pass(svc: Services, partner_id: int) -> bool:
    """Whether the contact holds a membership pass (not only a guest's: the buyer of an event
    ticket is the event's guest, and guests keep their guest name; ticketsales.py)."""
    rows = svc.odoo.search_read(
        "belfry.pass", [["partner_id", "=", partner_id], ["state", "=", "active"]], ["tier"]
    )
    return any(r.get("tier") != "guest" for r in rows)


def confirm(svc: Services, partner_id: int, address: str) -> dict[str, Any]:
    """ADDRESS was proven for the contact (the link mailed to it, or a code mailed to it). It
    becomes the contact's ``email`` only if it is the contact's pending address, nothing else
    ever does; when another account has it already, the passes move there and the temporary
    account closes. ``{"state": "confirmed" | "moved" | "already" | "mismatch", "partner"}``."""
    from . import core, usernames

    address = (address or "").strip().lower()
    with _confirm_lock:
        row = (
            svc.odoo.read_one("res.partner", partner_id, ["name", "email", "belfry_pending_email"])
            or {}
        )
        pending = (row.get("belfry_pending_email") or "").strip()
        if row.get("email"):
            return {"state": "already", "partner": partner_id}
        if not pending or pending.lower() != address:
            return {"state": "mismatch", "partner": partner_id}
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

                found = None
                if _has_pass(svc, partner_id):
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


def not_me(svc: Services, oid: str, partner_id: int) -> None:
    """ "This wasn't me" on a confirmation page: the unproven account's sessions end, its
    address is dropped (no more links work), the order is flagged for the admins. A free
    order still waiting for its address is cancelled."""
    st = store(svc)
    o = st.order(oid)
    if o is None:
        return
    flags = list(o.get("flags") or [])
    if "not_me" not in flags:
        flags.append("not_me")
        st.update(oid, flags=flags)
    if o["state"] == "confirm":
        cancel(svc, oid, "the address's owner said it wasn't them")
        return
    if not is_unverified(svc, partner_id):
        return  # a proven account is its owner's: nothing to undo from a mail link
    from . import community

    try:
        found = community.member_token(svc, partner_id)
        if found:
            for sess in svc.keycast.list_sessions(found[1]):
                if sess.get("bunker_pubkey"):
                    svc.keycast.revoke_session(found[1], sess["bunker_pubkey"])
    except Exception:  # noqa: BLE001 — the flag tells the admins; they can revoke the pass
        log.exception("sales: sessions of partner %s not revoked", partner_id)
    svc.odoo.write("res.partner", [partner_id], {"belfry_pending_email": False})
    st.update(oid, bunker_url=None)
    log.warning("sales: order %s: the address's owner says it wasn't them", oid)


def cancel_by_owner(svc: Services, oid: str) -> None:
    """The owner of the address cancels an unpaid order someone else made with it (the link in
    every mail about an unpaid order)."""
    st = store(svc)
    o = st.order(oid)
    if o is None:
        return
    st.update(oid, flags=_flag(o, "not_me"))
    cancel(svc, oid, "the address's owner said it wasn't them")


def _move_passes(svc: Services, temp: int, other: int) -> int:
    """The temporary contact's passes open again on OTHER's account; the temporary ones close
    (its key leaves the relays, its signer account is suspended). The tokens the pass came
    with go along (nothing is minted twice)."""
    from . import core

    rows = svc.odoo.search_read(
        "belfry.pass",
        [["partner_id", "=", temp], ["state", "=", "active"]],
        ["tier", "date_start", "date_end", "sale_order", "cashu_token", "cashu_amount"],
    )
    st = store(svc)
    n = 0
    for r in rows:
        sold = st.order(r["sale_order"]) if r.get("sale_order") else None
        if sold and sold.get("ticket"):
            # an event ticket's guest pass: the ticket goes to OTHER's own key, no new pass
            from . import ticketsales

            ticketsales.moved(svc, sold, temp, other)
            core.revoke_pass(svc, r["id"], "moved to the account of the same address")
            n += 1
            continue
        vals: dict[str, Any] = {
            "partner_id": other,
            "tier": r.get("tier") or "member",
            "sold_start": r.get("date_start") or False,
            "sold_end": r.get("date_end") or False,
            "sale_order": r.get("sale_order") or False,
        }
        if r.get("cashu_amount"):
            vals.update(cashu_token=r.get("cashu_token") or False, cashu_amount=r["cashu_amount"])
        new = svc.odoo.create("belfry.pass", vals)
        core.activate_free_pass(svc, new)
        if r.get("sale_order") and st.order(r["sale_order"]):
            # the browser's session was on the temporary account: it ends with it
            moved = st.update(
                r["sale_order"], pass_id=new, partner_id=other, moved_from=temp, bunker_url=None
            )
            mark_erase(svc, other, moved)  # the request goes along with the pass
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
    for k, n in (
        ("name", 120),
        ("street", 200),
        ("street2", 200),
        ("zip", 20),
        ("city", 100),
        ("region", 100),
    ):
        v = " ".join(str(raw.get(k) or "").split())
        if len(v) > n:
            raise bad()
        out[k] = v
    country = str(raw.get("country") or "").strip().upper()
    if country and not COUNTRY_RE.match(country):
        raise bad()
    out["country"] = country
    typed = " ".join(str(raw.get("vat") or "").split())
    if vies_country(kind, country, typed):
        # an EU VAT number: as before (prefix + 2–14, spaces dots and dashes dropped; VIES next)
        vat = re.sub(r"[\s.\-]", "", typed).upper()
        if vat and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{2,14}", vat):
            raise ApiError(422, "invalid_vat", "That VAT number is not valid.")
    else:
        # outside the EU: the organisation's own tax id as it writes it, never sent to VIES
        vat = typed
        if vat and not TAX_ID_RE.match(vat):
            raise ApiError(422, "invalid_vat", "That tax id is not valid.")
    out["vat"] = vat
    inv = str(raw.get("invoice_email") or "").strip()
    if inv and (not EMAIL.match(inv) or len(inv) > 254):
        raise bad()
    out["invoice_email"] = inv
    if kind == "organisation" and not (out["name"] and out["country"] and out["city"]):
        raise bad()
    return out


def vies_country(kind: str, country: str, vat: str = "") -> bool:
    """Whether an organisation's number is an EU VAT number for VIES: its billing country is in
    the EU, or it is a GB organisation with a Northern Ireland number (XI…). An organisation
    with no country (a person's billing) keeps the old rule: the number's own prefix."""
    if kind != "organisation":
        return True  # never checked; the old shape check stays
    if not country:
        return True
    if country in EU_COUNTRIES:
        return True
    return country == "GB" and vat.replace(" ", "").upper().startswith("XI")


URL_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")


def _own_url(svc: Services, url: Any) -> str | None:
    """RETURN_URL rebuilt from its parts when it is https on the belfry's own domain (the apex
    or a subdomain), with no user info, port, query or fragment; else None."""
    from urllib.parse import quote

    if not isinstance(url, str) or len(url) > 500:
        return None
    if any(c in url for c in "\\ \t\r\n") or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    domain = svc.settings.nip05_domain.lower().strip(".")
    if u.scheme != "https" or "@" in u.netloc or port is not None or u.query or u.fragment:
        return None
    if u.params or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    if not (host == domain or host.endswith("." + domain)) or ".." in host:
        return None
    path = quote(u.path or "/", safe="/-._~!$&'()*+,;=:@%")
    if not URL_PATH.match(path) or path.startswith("//"):
        return None
    return f"https://{host}{path}"


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
        "product": None if o.get("ticket") else o["product"],
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
        "erase_after_event": bool(o.get("erase_after_event")),
        # a tester's order (docs/sales.md §9): Stripe's test mode and the test Odoo, no real
        # money; the buying page shows a banner
        "test": env_of(o) == "test",
    }
    if o.get("ticket"):  # an event ticket's order (docs/sales.md §10)
        out["ticket"] = o["ticket"]
    inv = o.get("invoice") or {}
    if inv.get("number"):
        out["invoice"] = {
            "number": inv["number"],
            "communication": inv.get("communication") or inv["number"],
            # Belfry's own page of the invoice (number, amount, communication); the PDF is
            # mailed to the order's address
            "url": pay_url(svc, o["id"]),
        }
    if o["state"] in ("pending", "invoiced") and tot["gross"] > 0:
        if (o.get("stripe") or {}).get("url"):
            out["card_url"] = o["stripe"]["url"]
        if bank_ready(svc):
            # with an invoice: the transfer details; without (a card order): a button there
            # asks for the invoice to pay by transfer instead
            out["pay_url"] = pay_url(svc, o["id"])
    if o["state"] == "active" and o.get("bunker_url") and viewer == o["key"]:
        out["bunker_url"] = o["bunker_url"]
    return out


_checkout_lock = threading.Lock()

# "Send me all my data and erase it all from your server as soon as the event is over": asked
# on POST /checkout or POST /offers, kept on the order (or the offer), copied onto the person's
# contact when the pass opens. Only recorded: the export-and-erase run is not built (sales.md).
ERASE = "belfry_erase_after_event"


def erase_flag(data: dict[str, Any]) -> bool:
    """The body's ``erase_after_event``: absent → False; a JSON boolean; anything else 422."""
    from .holder_invites import ApiError

    if "erase_after_event" not in data:
        return False
    v = data["erase_after_event"]
    if not isinstance(v, bool):
        raise ApiError(422, "invalid", "erase_after_event: true or false.")
    return v


def mark_erase(svc: Services, partner_id: int | None, o: dict[str, Any]) -> None:
    """The order asked for its data to be erased after the event: say so on the contact. Once
    there it stays (a later order without it never clears it; only an admin does)."""
    if not partner_id or not o.get("erase_after_event"):
        return
    row = svc.odoo.read_one("res.partner", int(partner_id), [ERASE]) or {}
    if row.get(ERASE):
        return
    svc.odoo.write(
        "res.partner",
        [int(partner_id)],
        {
            ERASE: True,
            ERASE + "_order": o["id"],
            ERASE + "_at": o.get("erase_after_event_at") or iso(now()),
        },
    )
    log.info("sales: partner %s asked to be erased after the event (order %s)", partner_id, o["id"])


def clear_erase(svc: Services, partner_id: int, by: str, reason: str) -> None:
    """An admin clears the request (the person changed their mind, said so to an admin)."""
    svc.odoo.write(
        "res.partner",
        [int(partner_id)],
        {
            ERASE: False,
            ERASE + "_cleared": {"by": by[:100], "reason": reason[:200], "at": iso(now())},
        },
    )
    log.info("sales: partner %s: erase-after-event cleared by an admin", partner_id)


def erase_of(svc: Services, partner_id: int | None) -> bool:
    if not partner_id:
        return False
    try:
        row = svc.odoo.read_one("res.partner", int(partner_id), [ERASE]) or {}
    except Exception:  # noqa: BLE001 — the console still shows the order
        return False
    return bool(row.get(ERASE))


def checkout(
    svc: Services, key: str, data: dict[str, Any], ip: str = ""
) -> tuple[dict[str, Any], int]:
    """POST /checkout. Returns (the order's answer, HTTP status). Refused checkouts count per
    IP on their own (FAILS_PER_IP_HOUR), apart from the orders made."""
    from .holder_invites import ApiError

    st = store(svc)
    fails = "fail-ip:" + ip_tag(svc, ip)
    if st.hits_count(fails, 3600) >= FAILS_PER_IP_HOUR:
        raise ApiError(429, "too_many", "Too many refused tries from here. Try again in an hour.")
    try:
        return _checkout(svc, key, data, ip)
    except ApiError as exc:
        if exc.status != 429 and exc.error not in ("unsupported", "cannot_sell", "accounting"):
            st.hit(fails, 10**9, 3600)  # recorded, never refused here
        raise


def _checkout(
    svc: Services, key: str, data: dict[str, Any], ip: str = ""
) -> tuple[dict[str, Any], int]:
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "Selling needs the belfry's own register.")
    wanted = None  # an event ticket instead of a product (ticketsales.py)
    if data.get("ticket") is not None:
        from . import ticketsales

        if data.get("product") is not None or not isinstance(data["ticket"], str):
            raise ApiError(422, "invalid", 'Send "product" or "ticket", one of them.')
        if data.get("code"):
            raise ApiError(422, "code_not_for_tickets", "Codes are for passes, not for tickets.")
        wanted = ticketsales.find(svc, data["ticket"])
        p = ticketsales.as_product(wanted)
        pid = p["id"]
    else:
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
    erase = erase_flag(data)
    code = norm_code(data.get("code")) if data.get("code") else ""
    return_url = _own_url(svc, data.get("return_url"))
    ipt = ip_tag(svc, ip)
    st = store(svc)
    # the same key, product and address while an order is open (or just became active: a
    # retried request): that order, before any limit counts it again
    recent = iso(now() - dt.timedelta(hours=24))
    for o in st.orders((*OPEN, "active"), key=key):
        if (
            o["product"] == pid
            and o["email"].lower() == email.lower()
            and (o["state"] != "active" or o["created"] >= recent)
        ):
            if (
                o["state"] in ("pending", "invoiced")
                and o.get("pay") != pay
                and o["total"]["gross"]
            ):
                o = switch_pay(svc, o["id"], pay)
            if erase and not o.get("erase_after_event"):
                # asked on the retry: kept (never taken back by one that leaves it out)
                o = st.update(o["id"], erase_after_event=True, erase_after_event_at=iso(now()))
                if o["state"] == "active":
                    mark_erase(svc, o.get("partner_id"), o)
            return answer(svc, o, key), 200
    # an account whose address is not proven has one address: the one it waits to prove
    acct = account_of(svc, key)
    if acct and not acct["verified"] and acct["pending"] and acct["pending"] != email.lower():
        raise ApiError(
            409, "email_mismatch", "Use the address this account was made with, or confirm it."
        )
    # test or live (docs/sales.md §9): only the address decides, never the request. A test
    # order's pass goes on the tester's own account and nowhere else; the key that signs is
    # treated as the tester's only when it already is a session of that account.
    from . import testers

    tester = testers.tester_by_email(svc, email)
    env = "test" if tester is not None else "live"
    owner = acct  # whose account the order is on (None: a browser key)
    if tester is not None and (acct is None or acct["partner"] != tester):
        owner = None
    flags: list[str] = []
    vat_valid = False
    sells = can_sell
    if wanted is not None:
        # the switch, who may have it, and whether tickets can be invoiced at all
        ticketsales.check(svc, wanted, key, env, tester, owner)
        sells = ticketsales.can_sell
    if (
        billing["kind"] == "organisation"
        and billing.get("vat")
        and vies_country(billing["kind"], billing["country"], billing["vat"])
        and sells(svc, env)
    ):
        # checked before the invoice is made (outside the lock: VIES can be slow)
        from . import salesodoo

        valid = salesodoo.check_vat(billing["vat"])
        if valid is False:
            raise ApiError(422, "invalid_vat", "That VAT number is not valid.")
        if valid is None:
            flags.append("vies_down")  # taken; the admins check the number by hand
        vat_valid = valid is True and salesodoo.in_eu(billing["vat"])
    with _checkout_lock:
        if len(st.orders(OPEN, key=key)) >= OPEN_PER_KEY:
            raise ApiError(429, "too_many", "Too many open orders. Pay or wait for one to end.")
        if st.count_ip(ipt, int(time.time()) - 3600) >= ORDERS_PER_IP_HOUR:
            raise ApiError(429, "too_many", "Too many orders from here. Try again in an hour.")
        if st.open_for("email", email, mailed=True) >= OPEN_PER_ADDRESS:
            raise ApiError(
                429,
                "too_many",
                "This address has too many unpaid orders. If some aren't yours, cancel them "
                "with the link in the mails we sent to it.",
            )
        if st.count_address(email, int(time.time()) - 3600) >= ORDERS_PER_ADDRESS_HOUR:
            raise ApiError(429, "too_many", "Too many orders for this address. Try again later.")
        if p.get("quantity") is not None and st.open_for("ip", ipt, pid) >= HOLDS_PER_IP:
            raise ApiError(429, "too_many", "Pay the orders you have before holding more places.")
        if code and not CODE_CHECKS.allow("key:" + key):
            raise ApiError(429, "too_many", "Too many codes tried. Try again in an hour.")
        if wanted is not None:
            # the ticket's price now, as this buyer is invoiced (a person: the amount with its
            # VAT; an organisation: before VAT), and the places left, counted under the lock
            pr = {"until": None, "amount": ticketsales.unit(wanted, billing["kind"])}
            lft = ticketsales.left_now(svc, wanted)
        else:
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
                why = _usable(c, pid, key, acct["email"] if acct and acct["verified"] else "")
                if why:
                    raise ApiError(
                        404 if why == "unknown_code" else 409,
                        why,
                        "That code doesn't work for this."
                        if why == "unknown_code"
                        else "That code is used up.",
                    )
                percent = int(c["percent"])
                if c.get("offer") and not erase:
                    # asked with the offer in kind this code answers
                    erase = bool((st.offer(c["offer"]) or {}).get("erase_after_event"))
                # a test order never takes one of the code's real uses
                c.setdefault(uses_key({"env": env}), []).append(oid)
                st.put_code(c, db)
        total = split(pr["amount"], p["vat_rate"], percent, billing["kind"])
        if total["gross"] > 0 and env == "test" and not sells(svc, "test"):
            # never a fall back to production
            _give_back(svc, {"id": oid, "code": code})
            if wanted is not None:
                raise ApiError(
                    409,
                    "test_not_set_up",
                    "This address is a tester's, and tickets can't be sold in the test "
                    "environment yet. " + ticketsales.missing(svc, "test"),
                )
            raise ApiError(
                409,
                "test_not_set_up",
                "This address is a tester's, and the test environment (Stripe test mode, test "
                "Odoo) is not set up yet.",
            )
        if total["gross"] > 0 and not sells(svc, env):
            _give_back(svc, {"id": oid, "code": code})
            if wanted is not None:
                raise ApiError(
                    409,
                    "cannot_sell",
                    ("Tickets can't be bought here yet. " + ticketsales.missing(svc, env)).strip(),
                )
            raise ApiError(409, "cannot_sell", "Paid passes can't be sold here yet.")
        created = now()
        # a free product with no code makes no account from nothing: the address first
        gate = total["gross"] == 0 and not code and not (owner and owner["verified"])
        hold = None
        if gate:
            hold = iso(created + dt.timedelta(hours=CONFIRM_HOURS))
        elif total["gross"]:
            hold = iso(created + dt.timedelta(days=HOLD_DAYS))
        o = {
            "id": oid,
            "key": key,
            "ip": ipt,
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
            "vat_valid": vat_valid,
            "return_url": return_url,
            "erase_after_event": erase,
            "erase_after_event_at": iso(created) if erase else None,
            "total": total,
            "state": "confirm" if gate else "pending",
            # Xavier, 2026-11-17: an invoice only once paid (card) or once the buyer asked
            # for one to pay by transfer: unpaid card orders leave no invoice behind
            "step": "confirm"
            if gate
            else (
                "activate" if total["gross"] == 0 else ("invoice" if pay == "invoice" else "card")
            ),
            "created": iso(created),
            "hold_until": hold,
            # a signed-in member or guest: the pass goes on their account
            "account": owner["partner"] if owner else None,
            # a test order: always the tester's own account
            "partner_id": tester if tester is not None else (owner["partner"] if owner else None),
            "env": env,
            "pass_id": None,
            "invoice": {},
            "history": [],
            "stripe": {},
            "flags": flags,
            "tries": {},
            "error": None,
        }
        if wanted is not None:
            o.update(ticketsales.order_fields(wanted))
        st.put_order(o)
    log.info(
        "sales: %sorder %s for %s (%s), %s",
        "test " if env == "test" else "",
        oid,
        pid,
        mask(email),
        "to confirm" if gate else ("free" if not total["gross"] else "paid"),
    )
    if gate:
        from . import salesmail

        salesmail.queue(svc, "order_confirm", oid, email)
    else:
        advance(svc, oid, first=True)
    o = st.order(oid) or o
    return answer(svc, o, key), 201


def advance(svc: Services, oid: str, first: bool = False, count: bool = False) -> dict[str, Any]:
    """Do the order's next steps until one has to wait (for a payment) or fails. FIRST: in the
    checkout request, where Odoo refusing the invoice ends the order. COUNT: only the tick counts
    a failure (a buyer polling their order may make it try, never give up); a step that worked
    has its count reset."""
    from .holder_invites import ApiError

    st = store(svc)
    with _order_lock(oid):
        for _ in range(8):
            o = st.order(oid)
            if o is None or o["state"] in ("cancelled", "expired", "confirm"):
                break
            step = o.get("step")
            if o["state"] == "active" and step not in ("invoice", "register_payment"):
                break
            try:
                if step == "invoice":
                    from . import salesodoo

                    salesodoo.make_invoice(svc, o)
                elif step == "card":
                    from . import salesstripe

                    salesstripe.make_session(svc, o)
                elif step == "register_payment":
                    from . import salesodoo

                    salesodoo.register_payment(svc, o)
                elif step == "activate":
                    activate(svc, o)
                else:
                    break
                _worked(svc, oid, step)
            except Refused as exc:
                o = _tried(svc, oid, step, str(exc), count)
                if first and step == "invoice" and o["state"] == "pending":
                    cancel(svc, oid, "Odoo refused the invoice")
                    raise ApiError(502, "accounting", "The invoice could not be made.") from exc
                log.warning("sales: order %s, %s refused: %s", oid, step, exc)
                break
            except Exception as exc:  # noqa: BLE001 — retried by the tick
                log.warning("sales: order %s, step %s waits: %s", oid, step, type(exc).__name__)
                _tried(svc, oid, step, f"{step}: {type(exc).__name__}", count)
                break
        return st.order(oid) or {}


def _worked(svc: Services, oid: str, step: str) -> None:
    o = store(svc).order(oid) or {}
    tries = dict(o.get("tries") or {})
    if step in tries or o.get("next_try_at"):
        tries.pop(step, None)
        store(svc).update(oid, tries=tries, next_try_at=None)


def _tried(svc: Services, oid: str, step: str, error: str, count: bool = True) -> dict[str, Any]:
    """A failed step. Counted only by the tick (COUNT). Past STEP_TRIES: a PAID order never
    gives up on opening its pass (it tries hourly, flagged ``slow``); any other step stops
    (step ``stuck``), flagged. The console's "Try again" starts it over."""
    st = store(svc)
    o = st.order(oid) or {}
    vals: dict[str, Any] = {"error": error[:200]}
    if not count:
        return st.update(oid, **vals)
    tries = dict(o.get("tries") or {})
    tries[step] = int(tries.get(step) or 0) + 1
    vals["tries"] = tries
    if tries[step] >= STEP_TRIES:
        if step == "activate" and o.get("state") == "paid":
            vals.update(next_try_at=iso(now() + dt.timedelta(hours=1)), flags=_flag(o, "slow"))
        else:
            vals.update(step="stuck", stuck_step=step, flags=_flag(o, "stuck"))
            log.warning("sales: order %s: %s gave up after %d tries", oid, step, tries[step])
    return st.update(oid, **vals)


def try_again(svc: Services, oid: str) -> dict[str, Any]:
    """The console's "Try again": counts reset, a stuck step resumes, then one go now."""
    st = store(svc)
    with _order_lock(oid):
        o = st.order(oid)
        if o is None:
            raise KeyError(oid)
        vals: dict[str, Any] = {
            "tries": {},
            "next_try_at": None,
            "flags": [f for f in o.get("flags") or [] if f not in ("stuck", "slow")],
        }
        if o.get("step") == "stuck":
            vals["step"] = o.get("stuck_step") or "activate"
        o = st.update(oid, **vals)
        if o["state"] in ("cancelled", "expired"):
            if o.get("step") == "credit_note":
                end_unpaid(svc, o)
            return st.order(oid) or o
        return advance(svc, oid)


class Refused(Exception):
    """The seller's Odoo (or Stripe) refused something as it is: not retried by itself."""


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _order_lock(oid: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(oid, threading.RLock())


def _flag(o: dict[str, Any], *names: str) -> list[str]:
    flags = list(o.get("flags") or [])
    for n in names:
        if n not in flags:
            flags.append(n)
    return flags


def next_after_pass(o: dict[str, Any]) -> str:
    """What an active order still owes the books: an invoice (paid by hand before one existed,
    or paid after its first invoice was credited), then the payment on it."""
    if o["total"]["gross"] > 0 and not (o.get("invoice") or {}).get("move_id"):
        return "invoice"
    if o.get("payment_due"):
        return "register_payment"
    return "done"


def after_invoice(svc: Services, o: dict[str, Any]) -> None:
    """The invoice is posted. An unpaid order: a card session next (Stripe set), or waiting.
    A paid or active one: the payment on it."""
    st = store(svc)
    o = st.order(o["id"]) or o
    if o["state"] == "active":
        st.update(o["id"], step="register_payment" if o.get("payment_due") else "done")
        # paid before it had an invoice (a card): the receipt carries the PDF, unless the
        # receipt went already (the invoice was slow), then the paid invoice comes on its own
        from . import salesmail

        if store(svc).mail_state("receipt", o["id"]) is None:
            salesmail.queue(svc, "receipt", o["id"], o["email"])
            return
    elif o["state"] == "paid":
        st.update(o["id"], step="activate")
    else:
        card = stripe_ready(svc, env_of(o))
        st.update(o["id"], state="invoiced", step="card" if card else "wait")
        if card:
            return  # the invoice mail goes once the card session exists (salesstripe)
    queue_invoice_mail(svc, o)


def queue_invoice_mail(svc: Services, o: dict[str, Any]) -> None:
    from . import salesmail

    salesmail.queue(
        svc, "invoice", f"{o['id']}:{int(o.get('invoice_round') or 1)}", invoice_address(o)
    )


def invoice_address(o: dict[str, Any]) -> str:
    return (o.get("billing") or {}).get("invoice_email") or o["email"]


def mark_paid(
    svc: Services,
    oid: str,
    how: str,
    ref: str = "",
    by: str = "",
    session: str = "",
    amount: int | None = None,
) -> dict[str, Any]:
    """Money came (Stripe, the bank seen in Odoo, or an admin): the pass opens, then the books
    follow (an invoice if there is none, the payment on it). Idempotent. A payment on an order
    that ended reopens it (``reopen``): they paid, they get the pass."""
    st = store(svc)
    with _order_lock(oid):
        o = st.order(oid)
        if o is None:
            raise KeyError(oid)
        if o["state"] in ("paid", "active"):
            return o
        if o["state"] in ("cancelled", "expired"):
            o = _reopen(svc, o)
        inv = o.get("invoice") or {}
        due = how in ("card", "hand") and o["total"]["gross"] > 0
        if how == "transfer" and inv.get("move_id"):
            due = False  # Odoo saw it: it is on the invoice already
        st.update(
            oid,
            state="paid",
            step="activate",
            payment_due=due,
            paid={
                "how": how,
                "ref": ref[:100],
                "by": by[:100],
                "at": iso(now()),
                "session": session or None,
                "amount": amount,
            },
        )
        o = st.order(oid) or o
    # any other open card session can't be paid any more (a second payment), best effort
    stripe = o.get("stripe") or {}
    if stripe.get("id") and stripe["id"] != session and not stripe.get("expired"):
        from . import salesstripe

        with contextlib.suppress(Exception):
            salesstripe.expire_session(svc, o)
            st.update(oid, stripe={**stripe, "expired": True})
    return advance(svc, oid)


def _reopen(svc: Services, o: dict[str, Any]) -> dict[str, Any]:
    """An ended order that gets paid: back in, with a new invoice when the old one was
    credited (kept in ``history``), its code's use taken again, flagged ``paid_late`` (and
    ``oversold`` when no place was left)."""
    st = store(svc)
    flags = ["paid_late"]
    p = product(svc, o["product"])
    if p is not None and (lft := left(svc, p)) is not None and lft <= 0:
        flags.append("oversold")
    if o.get("ticket"):
        from . import ticketsales

        if ticketsales.full(svc, o):
            flags.append("oversold")
    inv = o.get("invoice") or {}
    vals: dict[str, Any] = {"flags": _flag(o, *flags), "reopened": iso(now())}
    credited = bool(inv.get("credited"))
    if inv.get("move_id") and not credited:
        # never only the local record (a crash between Odoo's credit note and writing it down):
        # Odoo says whether the invoice still stands. Odoo not answering raises: retried.
        from . import salesodoo

        credited = not salesodoo.invoice_standing(svc, int(inv["move_id"]), env_of(o))
    if inv.get("move_id") and credited:
        vals["history"] = [*(o.get("history") or []), {**inv, "credited": True}]
        vals["invoice"] = {}
        vals["invoice_round"] = int(o.get("invoice_round") or 1) + 1
    else:
        # the credit note was never made (or the invoice was paid): it stands, takes the payment
        vals["step"] = "done"
    if o.get("code"):
        with st.tx() as db:
            c = st.code(o["code"], db)
            if c and o["id"] not in (c.get(uses_key(o)) or []):
                c.setdefault(uses_key(o), []).append(o["id"])
                st.put_code(c, db)
    log.warning("sales: order %s paid after it ended (%s): reopened", o["id"], o["state"])
    return st.update(o["id"], **vals)


def reopen_paid(svc: Services, oid: str, reason: str, by: str) -> dict[str, Any]:
    """The console's "Reopen and mark paid": a transfer that came in time but was reconciled
    after the hold ended (or an order cancelled by mistake)."""
    return mark_paid(svc, oid, "hand", reason, by)


def _existing_pass(svc: Services, oid: str) -> int | None:
    rows = svc.odoo.search_read(
        "belfry.pass", [["sale_order", "=", oid], ["state", "in", ["pending", "active"]]], ["id"]
    )
    return rows[0]["id"] if rows else None


def activate(svc: Services, o: dict[str, Any]) -> None:
    """Open the pass: the contact (made now, for a browser key), the pass with the product's
    tier and dates, the browser key as the account's first session, the receipt."""
    from . import core

    if o.get("ticket"):
        # an event ticket: the buyer is admitted to the event; no membership pass
        from . import ticketsales

        ticketsales.activate(svc, o)
        return
    st = store(svc)
    oid = o["id"]
    pid = o.get("partner_id")
    verified_by_link = bool(o.get("address_confirmed"))
    test = env_of(o) == "test"
    if test:
        # a test order opens a pass only on the account of the tester it was made for, while
        # that person still is a tester with that address (removed since: nothing opens)
        from . import testers

        if not pid or testers.tester_by_email(svc, o["email"]) != int(pid):
            st.update(oid, step="stuck", stuck_step="activate", flags=_flag(o, "tester_gone"))
            log.warning("sales: test order %s: its tester is gone; no pass", oid)
            return
    if not pid:
        vals: dict[str, Any] = {"name": o["name"], "belfry_sale_order": oid}
        if o.get("odoo_partner"):
            vals["belfry_odoo_partner"] = o["odoo_partner"]
        if verified_by_link:  # a free order whose address was confirmed by its link
            vals.update(email=o["email"], belfry_email_verified_at=core.now_utc_str())
        else:
            vals["belfry_pending_email"] = o["email"]
        pid = svc.odoo.create("res.partner", vals)
        o = st.update(oid, partner_id=pid)
    elif o.get("account"):
        acct = svc.odoo.read_one("res.partner", pid, ["email", "belfry_pending_email"]) or {}
        if not acct.get("email") and not acct.get("belfry_pending_email"):
            # a guest who never saved an address: the typed one waits for its proof
            svc.odoo.write("res.partner", [pid], {"belfry_pending_email": o["email"]})
    if not o.get("pass_id"):
        pass_id = _existing_pass(svc, oid)  # made before a crash: the same one
        if pass_id is None:
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
                    **({"belfry_test": True} if test else {}),
                },
            )
        o = st.update(oid, pass_id=pass_id)
    result = core.activate_free_pass(svc, o["pass_id"])
    if result not in ("activated", "already_active"):
        # the pass was closed by someone (revoked, expired): the order doesn't pretend
        st.update(oid, step="stuck", stuck_step="activate", flags=_flag(o, "pass_closed"))
        log.warning("sales: order %s: its pass could not open (%s)", oid, result)
        return
    if o.get("role") and not test:  # a tester's role never changes (testers.py)
        with contextlib.suppress(Exception):
            from . import roles

            if roles.exists(svc, o["role"]):
                svc.odoo.write("res.partner", [pid], {"belfry_role": o["role"]})
    mark_erase(svc, pid, o)
    if not o.get("account") and not o.get("bunker_url") and not verified_by_link and not test:
        # the browser that paid is signed in; never for an address proven by a mail link (the
        # person who clicked may not be the one holding the browser), never for a test order
        # (test cards are public: knowing a tester's address must not sign anyone in)
        o = st.update(oid, bunker_url=_bind_key(svc, pid, o["key"]))
    o = st.update(oid, state="active", activated=iso(now()), error=None)
    o = st.update(oid, step=next_after_pass(o))
    if o["step"] != "invoice":  # else the receipt waits for the invoice (with its PDF)
        from . import salesmail

        salesmail.queue(svc, "receipt", oid, o["email"])
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


def confirm_order(svc: Services, oid: str) -> dict[str, Any]:
    """A free order's address was confirmed by its link: the pass opens. If the address is an
    account's already, the pass goes on that account."""
    st = store(svc)
    with _order_lock(oid):
        o = st.order(oid)
        if o is None or o["state"] != "confirm":
            return o or {}
        vals: dict[str, Any] = {"state": "pending", "step": "activate", "address_confirmed": True}
        acct = o.get("account")
        row = (
            svc.odoo.read_one("res.partner", int(acct), ["email", "belfry_pending_email"])
            if acct
            else None
        )
        if row is not None and not row.get("email") and not row.get("belfry_pending_email"):
            # an unsaved guest: the address they typed waits for its proof, which the link is
            svc.odoo.write("res.partner", [int(acct)], {"belfry_pending_email": o["email"]})
        if acct and is_unverified(svc, int(acct)):
            # a signed-in account whose address waited for its proof: the link is that proof
            done = confirm(svc, int(acct), o["email"])
            if done["state"] in ("confirmed", "moved"):
                vals.update(partner_id=done["partner"], account=done["partner"])
        elif not o.get("partner_id"):
            other = _partner_by_email(svc, o["email"], 0)
            if other is not None:
                vals.update(partner_id=other, account=other)
        st.update(oid, **vals)
        return advance(svc, oid)


def cancel(svc: Services, oid: str, reason: str, by: str = "") -> dict[str, Any]:
    """An order that ends unpaid (an admin, or Odoo refused it): the code's use comes back; an
    invoice gets a credit note (the tick retries it). Under the order's lock, state read again."""
    st = store(svc)
    with _order_lock(oid):
        o = st.order(oid)
        if o is None:
            raise KeyError(oid)
        if o["state"] not in ("confirm", "pending", "invoiced"):
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
    """The steps after an unpaid end: Stripe's session expires, the invoice is credited.
    Under the order's lock, only while the order is still ended."""
    st = store(svc)
    with _order_lock(o["id"]):
        o = st.order(o["id"]) or o
        if o["state"] not in ("cancelled", "expired"):
            return
        if (o.get("stripe") or {}).get("id") and not (o.get("stripe") or {}).get("expired"):
            with contextlib.suppress(Exception):
                from . import salesstripe

                salesstripe.expire_session(svc, o)
                st.update(o["id"], stripe={**o["stripe"], "expired": True})
        if o.get("step") == "credit_note":
            try:
                from . import salesodoo

                credited = salesodoo.credit_note(svc, o)
                inv = dict(o.get("invoice") or {})
                inv["credited"] = bool(credited)
                st.update(o["id"], step="done", invoice=inv)
            except Exception as exc:  # noqa: BLE001 — the tick tries again
                log.warning("sales: order %s, credit note waits: %s", o["id"], type(exc).__name__)
                _tried(svc, o["id"], "credit_note", f"credit_note: {type(exc).__name__}")


def expire_holds(svc: Services) -> int:
    """Unpaid orders past their hold expire; a reminder goes 2 days before. Each under its
    order's lock, its state read again."""
    st = store(svc)
    n = 0
    for row in st.orders(("confirm", "pending", "invoiced")):
        oid = row["id"]
        with _order_lock(oid):
            o = st.order(oid)
            t = now()
            if o is None or o["state"] not in ("confirm", "pending", "invoiced"):
                continue
            if not o.get("hold_until"):
                continue
            until = parse_iso(o["hold_until"])
            if until <= t:
                o = st.update(
                    oid,
                    state="expired",
                    step="credit_note" if (o.get("invoice") or {}).get("move_id") else "done",
                    ended={"reason": "hold ended", "by": "", "at": iso(t)},
                )
                _give_back(svc, o)
                end_unpaid(svc, o)
                n += 1
            elif (
                o["state"] != "confirm"
                and until - t <= dt.timedelta(days=REMIND_DAYS)
                and o["total"]["gross"] > 0
                and ((o.get("invoice") or {}).get("number") or o.get("pay") == "card")
            ):
                from . import salesmail

                salesmail.queue(svc, "reminder", oid, invoice_address(o))
    return n


def retry(svc: Services) -> None:
    """Steps that failed earlier: invoices, sessions, payments, activations, credit notes."""
    st = store(svc)
    for o in st.orders():
        if (
            o["state"] in ("pending", "invoiced", "paid")
            and o.get("step")
            in (
                "invoice",
                "card",
                "activate",
            )
        ) or (o["state"] == "active" and o.get("step") in ("invoice", "register_payment")):
            if (
                o["state"] == "active"
                and o.get("step") == "invoice"
                and (o.get("activated") or "9") < iso(now() - dt.timedelta(hours=1))
                and st.mail_state("receipt", o["id"]) is None
            ):
                from . import salesmail  # the invoice is slow: the receipt goes without it

                salesmail.queue(svc, "receipt", o["id"], o["email"])
            if o.get("next_try_at") and o["next_try_at"] > iso(now()):
                continue  # backing off (a paid order that keeps failing to open)
            advance(svc, o["id"], count=True)
        elif o["state"] in ("cancelled", "expired") and o.get("step") == "credit_note":
            end_unpaid(svc, o)


TICK_SECONDS = 900
_last_tick = {"at": 0.0}


def tick(svc: Services, force: bool = False) -> None:
    """Every 15 minutes (followers' loop runs every 5): holds, retries, the open invoices read
    from Odoo (paid by transfer → active; fully credited → the pass closes), the mails due."""
    if not own(svc):
        return
    if not force and time.monotonic() - _last_tick["at"] < TICK_SECONDS - 5:
        return
    _last_tick["at"] = time.monotonic()
    from . import salesmail

    for job in (expire_holds, retry, _read_invoices, salesmail.deliver_due):
        try:
            job(svc)
        except Exception as exc:  # noqa: BLE001 — next round
            log.warning("sales: %s failed: %s", job.__name__, type(exc).__name__)


def _read_invoices(svc: Services) -> None:
    """Each environment's open invoices from its own Odoo."""
    from . import salesodoo

    for env in ENVS:
        if odoo_ready(svc, env):
            try:
                salesodoo.read_open_invoices(svc, env)
            except Exception as exc:  # noqa: BLE001 — the other environment still goes
                log.warning("sales: %s invoices not read: %s", env, type(exc).__name__)


# ── offers (in kind): public requests in the requests channel ───────────────────────────────
# Xavier, 2026-11-17: in-kind requests are public, like GitHub issues, with back and forth in
# the thread. An offer is a thread from outside in the "requests channel" (a channel whose web
# page is on and public, webthreads.py): the same confirmation of the address before anything
# is public, then a card in the channel tagged ["offer", id] with buttons for the belfry's
# admins (docs/actions.md, belfry:offer-decide). A decision is a public reply in the thread;
# the personal code goes only in the private mail to the requester.
OFFER_PERCENTS = (50, 75, 100)


def requests_site(svc: Services) -> tuple[str, dict[str, Any]] | None:
    """(slug, site) of the requests channel when its page is on and public, else None."""
    from . import webthreads

    channel = settings(svc).get("requests_channel") or ""
    if not channel:
        return None
    try:
        sites = webthreads.sites(svc)
    except Exception:  # noqa: BLE001
        return None
    for slug, site in sites.items():
        if site["channel"] == channel and site.get("list") == "public":
            return slug, site
    return None


def public_channels(svc: Services) -> list[dict[str, str]]:
    """The channels that can take offers: page on and public (the console's pick-list)."""
    from . import webthreads

    try:
        sites = webthreads.sites(svc)
    except Exception:  # noqa: BLE001
        return []
    return [
        {"channel": site["channel"], "slug": slug, "name": site.get("name") or slug}
        for slug, site in sorted(sites.items())
        if site.get("list") == "public"
    ]


def offer_answer(f: dict[str, Any], mine: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "offer": f["id"],
        "state": f["state"],
        "product": f["product"],
        "erase_after_event": bool(f.get("erase_after_event")),
    }
    if f.get("url"):
        out["url"] = f["url"]
    if mine and f.get("my_url"):
        out["my_url"] = f["my_url"]
    if f["state"] in ("accepted", "declined"):
        out["percent"] = f.get("decided_percent") or 0
    return out


def offer_buttons() -> list[list[str]]:
    return [
        ["button", "offer-50", "50% off", "", "belfry:offer-decide"],
        ["button", "offer-75", "75% off", "", "belfry:offer-decide"],
        ["button", "offer-100", "100% off", "primary", "belfry:offer-decide"],
        ["button", "offer-no", "Decline", "danger", "belfry:offer-decide"],
    ]


def make_offer(svc: Services, key: str, data: dict[str, Any], ip: str = "") -> dict[str, Any]:
    """POST /offers: a public request, posted once its author confirmed the mailed link."""
    from . import webthreads
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "Selling needs the belfry's own register.")
    found = requests_site(svc)
    if found is None:
        raise ApiError(409, "no_requests_channel", "This belfry takes no offers in kind yet.")
    slug, site = found
    p = product(svc, str(data.get("product") or ""))
    if p is None:
        raise ApiError(404, "unknown_product", "No such product.")
    if not p.get("on_sale"):
        raise ApiError(409, "not_on_sale", "This product is not on sale.")
    name = " ".join(str(data.get("name") or "").split())[:100]
    email = str(data.get("email") or "").strip()
    if not webthreads.EMAIL.match(email) or len(email) > 254:  # the requests page's own rule
        raise ApiError(422, "invalid_email", "That is not an e-mail address.")
    text = str(data.get("offer") or "").strip()
    if not name or not text or len(text) > 2000:
        raise ApiError(422, "invalid", "Say who you are and what you offer (2000 characters).")
    try:
        percent = int(data.get("percent") if data.get("percent") not in (None, "") else 50)
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "invalid", "percent: 50 to 100.") from exc
    if not 50 <= percent <= 100:
        raise ApiError(422, "invalid", "percent: 50 to 100.")
    erase = erase_flag(data)
    st = store(svc)
    mine = [f for f in st.offers(key) if f["state"] in ("confirm", "open")]
    if (
        len(mine) >= 3
        or not st.hit("offer-key:" + key, OFFERS_PER_DAY, 86400)
        or not st.hit("offer-ip:" + ip_tag(svc, ip), OFFERS_PER_DAY, 86400)
    ):
        raise ApiError(429, "too_many", "Too many offers for now.")
    fid = "f_" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16))
    title = f"{name}: {percent}% off {p['name']} in kind"
    extra = [
        ["offer", fid],
        ["product", p["id"]],
        ["percent", str(percent)],
        ["p", key],
        *offer_buttons(),
    ]
    token = webthreads.start_thread(
        svc,
        slug,
        site,
        title,
        text,
        name,
        email,
        ip,
        extra=extra,
        hook={"kind": "offer", "id": fid},
        public_note=True,
    )
    if token is None:
        raise ApiError(429, "too_many", "Too many requests in a short time: try in an hour.")
    f = {
        "id": fid,
        "key": key,
        "product": p["id"],
        "name": name,
        "email": email,
        "offer": text,
        "percent": percent,
        "erase_after_event": erase,
        "erase_after_event_at": iso(now()) if erase else None,
        "state": "confirm",
        "channel": site["channel"],
        "slug": slug,
        "created": iso(now()),
    }
    st.put_offer(f)
    log.info("sales: offer %s for %s (%s), waiting for its link", fid, p["id"], mask(email))
    return f


def offer_posted(svc: Services, fid: str, event: dict[str, Any], url: str, my_url: str) -> None:
    """webthreads: the requester pressed the link; the request is public."""
    st = store(svc)
    f = st.offer(fid)
    if f is None or f["state"] != "confirm":
        return
    f.update(state="open", card=event["id"], url=url, my_url=my_url)
    st.put_offer(f)


def offer_chat_link(svc: Services, f: dict[str, Any]) -> str:
    from urllib.parse import quote

    if not f.get("card") or not f.get("channel"):
        return ""
    return (
        f"https://chat.{svc.settings.nip05_domain}/#/c/{quote(f['channel'], safe='')}"
        f"/thread/{f['card']}"
    )


_decide_lock = threading.Lock()


def decide_offer(svc: Services, fid: str, percent: Any, note: str, by: str) -> dict[str, Any]:
    """An admin's answer: a public reply in the thread ("Approved: 50% off." / "Not this
    time.", with the note), and the personal code only in the private mail. One decision, one
    code: two admins at once get one answer and one "has an answer already"."""
    with _decide_lock:
        return _decide_offer(svc, fid, percent, note, by)


def _decide_offer(svc: Services, fid: str, percent: Any, note: str, by: str) -> dict[str, Any]:
    from .holder_invites import ApiError

    st = store(svc)
    f = st.offer(fid)
    if f is None:
        raise ApiError(404, "not_found", "No such offer.")
    if f["state"] != "open":
        raise ApiError(
            409,
            "decided" if f["state"] != "confirm" else "not_open",
            "This offer has an answer already."
            if f["state"] != "confirm"
            else "This offer is not public yet.",
        )
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
            svc,
            percent,
            f["product"],
            1,
            1,
            None,
            f"Offer from {f['name']}",
            by,
            fid,
            bound_key=f["key"],
            bound_email=f["email"],
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
    _reply_in_thread(
        svc,
        f,
        (f"Approved: {percent}% off." if percent else "Not this time.")
        + (f" {note}" if note else ""),
    )
    from . import salesmail

    salesmail.offer_answer(svc, f)
    return f


def _reply_in_thread(svc: Services, f: dict[str, Any], text: str) -> None:
    """The decision, public, as a reply under the request (by the groups key)."""
    from . import followers

    got = followers._relay(svc)
    if got is None or not f.get("card"):
        return
    relay, sign = got
    root = f["card"]
    found = relay.query([{"ids": [root]}], sign) or []
    author = (found[0] if found else {}).get("pubkey") or ""
    tags = [
        ["h", f["channel"]],
        ["E", root, "", author],
        ["K", "9"],
        ["P", author],
        ["e", root, "", author],
        ["k", "9"],
        ["p", author],
        ["client", "belfry-sales"],
    ]
    try:
        event = sign({"kind": 1111, "created_at": int(time.time()), "content": text, "tags": tags})
        relay.publish(event, sign)
    except Exception:  # noqa: BLE001 — the decision stands; the mail tells the requester
        log.exception("sales: offer %s: the decision was not posted in its thread", f["id"])


async def offer_decide_press(p: Any) -> Any:
    """docs/actions.md: belfry:offer-decide, pressed on an offer's card in the chat."""
    from . import actions, apps

    svc = p.svc
    admins = await run_in_threadpool(apps.admin_pubkeys, svc)
    if p.press["pubkey"] not in admins:
        return actions.Result.error("not_allowed", "Only admins of this belfry decide offers.")
    fid = next((t[1] for t in p.message.get("tags", []) if t[:1] == ["offer"] and len(t) > 1), "")
    f = store(svc).offer(fid) if OFFER_RE.match(fid or "") else None
    if f is None or f.get("card") != p.message.get("id"):
        return actions.Result.error("no_target", "This message is not an offer here.")
    bid = p.button["id"]
    percent = 0 if bid == "offer-no" else int(bid.removeprefix("offer-") or 0)
    if percent and percent not in OFFER_PERCENTS:
        return actions.Result.error("no_target")
    name = await run_in_threadpool(actions._name, svc, p.press["pubkey"])
    from .holder_invites import ApiError

    try:
        await run_in_threadpool(decide_offer, svc, fid, percent, "", name)
    except ApiError as exc:
        return actions.Result.error("gone" if exc.error == "decided" else "failed", exc.message)
    if percent:
        return actions.Result("ok", f"Approved by {name}: {percent}% off.", closed=True)
    return actions.Result("ok", f"Declined by {name}.", closed=True)


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
        acct = await run_in_threadpool(account_of, s, key)
        mail_ok = acct["email"] if acct and acct["verified"] else ""
        why = _usable(c, pid, key, mail_ok) if product(s, pid) else "unknown_code"
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
        out, status = await run_in_threadpool(checkout, s, key, data, client_ip(request))
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

    @app.post(API + "/checkout/{oid}/pay")
    async def api_checkout_pay(oid: str, request: Request) -> JSONResponse:
        s, key, data = await _signed(request)
        o = store(s).order(oid) if ORDER_RE.match(oid) else None
        if o is None or o["key"] != key:
            raise ApiError(404, "not_found", "No such order.")
        o = await run_in_threadpool(switch_pay, s, oid, str(data.get("pay") or ""))
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
        f = await run_in_threadpool(make_offer, s, key, data, client_ip(request))
        return JSONResponse(offer_answer(f), status_code=201, headers=NO_STORE)

    @app.get(API + "/offers/{fid}")
    async def api_offer_get(fid: str, request: Request) -> JSONResponse:
        s, key, _ = await _signed(request, "GET")
        f = store(s).offer(fid) if OFFER_RE.match(fid) else None
        if f is None or f["key"] != key:
            raise ApiError(404, "not_found", "No such offer.")
        return JSONResponse(offer_answer(f, mine=True), headers=NO_STORE)

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


# ── the buyer's pages on belfry.<domain> (fixed text and the order's own details only) ──────
PAGE_TEXT = {
    "thanks": "Thank you. Your order is in: the next steps come by e-mail.",
    "gone": "This link doesn't work (any more).",
    "ask": "Confirm that this address is yours. Then you can sign in with it from any device.",
    "ask_order": "Confirm this order with your address. Your pass opens when you do.",
    "confirmed": "Done: your address is confirmed. You can sign in with it from any device.",
    "order_confirmed": "Done: your pass is ready. Sign in with your address to see it.",
    "moved": "This address had an account here already: your pass is on that account now. "
    "Sign in with your address.",
    "already": "This address is confirmed already.",
    "not_me": "Thank you for telling us. Nothing more happens with your address, and the "
    "belfry's admins have been told.",
    "ask_cancel": "Someone ordered this with your address. If it wasn't you, cancel it.",
    "cancelled": "Cancelled. Thank you for telling us; the belfry's admins have been told.",
}


def _page(key: str, status: int = 200, extra: str = "") -> Any:
    from fastapi.responses import HTMLResponse

    from . import theme

    body = f'<div class="card"><p>{html.escape(PAGE_TEXT[key])}</p>{extra}</div>'
    return HTMLResponse(
        theme.frame("Your pass", body),
        status_code=status,
        headers={**NO_STORE, "Referrer-Policy": "no-referrer"},
    )


def _order_summary(o: dict[str, Any]) -> str:
    gross = o["total"]["gross"]
    price = f"€{gross / 100:,.2f}" if gross else "free"
    return (
        "<dl>"
        f"<dt>What</dt><dd>{html.escape(o['product_name'])}</dd>"
        f"<dt>For</dt><dd>{html.escape(price)}</dd>"
        f"<dt>Ordered</dt><dd>{html.escape(o['created'][:16].replace('T', ' '))} (UTC)</dd>"
        f"<dt>Address</dt><dd>{html.escape(o['email'])}</dd>"
        "</dl>"
    )


def register_pages(app: FastAPI) -> None:
    @app.get("/console/sales/thanks", include_in_schema=False)
    async def thanks(request: Request):
        return _page("thanks")

    def _found(s: Services, token: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        c = store(s).confirm_of(token)
        o = store(s).order(c["order"]) if c else None
        if c is None or o is None or o["email"].strip().lower() != c["address"]:
            return None
        return c, o

    @app.get("/console/sales/confirm/{token}", include_in_schema=False)
    async def confirm_page(token: str, request: Request):
        s = request.app.state.services
        found = await run_in_threadpool(_found, s, token)
        if not found:
            return _page("gone", 404)
        c, o = found
        if c["kind"] == "order" and o["state"] != "confirm":
            return _page("gone", 404)
        if c["kind"] == "cancel":
            if o["state"] not in ("confirm", "pending", "invoiced"):
                return _page("gone", 404)
            form = (
                _order_summary(o)
                + f'<form method="post" action="/sales/confirm/{html.escape(token)}">'
                '<input type="hidden" name="answer" value="cancel">'
                '<button class="btn" type="submit">Cancel this order</button></form>'
            )
            return _page("ask_cancel", extra=form)
        # buttons: mail scanners open links, only a person presses one
        forms = (
            _order_summary(o) + f'<form method="post" action="/sales/confirm/{html.escape(token)}">'
            '<input type="hidden" name="answer" value="yes">'
            '<button class="btn" type="submit">Yes, this was me</button></form>'
            f'<form method="post" action="/sales/confirm/{html.escape(token)}">'
            '<input type="hidden" name="answer" value="no">'
            '<button class="btn ghost" type="submit">This wasn\'t me</button></form>'
        )
        return _page("ask_order" if c["kind"] == "order" else "ask", extra=forms)

    @app.post("/console/sales/confirm/{token}", include_in_schema=False)
    async def confirm_post(token: str, request: Request):
        s = request.app.state.services
        found = await run_in_threadpool(_found, s, token)
        if not found:
            return _page("gone", 404)
        c, o = found
        form = await request.form()
        if c["kind"] == "cancel":
            if form.get("answer") != "cancel" or o["state"] not in (
                "confirm",
                "pending",
                "invoiced",
            ):
                return _page("gone", 404)
            await run_in_threadpool(cancel_by_owner, s, o["id"])
            return _page("cancelled")
        if form.get("answer") == "no":
            await run_in_threadpool(not_me, s, o["id"], c["partner"])
            return _page("not_me")
        if c["kind"] == "order":
            if o["state"] != "confirm":
                return _page("gone", 404)
            await run_in_threadpool(confirm_order, s, o["id"])
            return _page("order_confirmed")
        if o.get("moved_from") == c["partner"]:
            return _page("already")
        out = await run_in_threadpool(confirm, s, c["partner"], c["address"])
        if out["state"] == "mismatch":
            return _page("gone", 404)
        return _page(out["state"])


def switch_pay(svc: Services, oid: str, pay: str) -> dict[str, Any]:
    """The buyer changes how they pay an open order. card → invoice: the invoice is made now
    (and mailed). invoice → card: a card session for that invoice's amount (the payment goes on
    it)."""
    from .holder_invites import ApiError

    if pay not in ("card", "invoice"):
        raise ApiError(422, "invalid", 'pay: "card" or "invoice".')
    st = store(svc)
    with _order_lock(oid):
        o = st.order(oid)
        if o is None or o["state"] not in ("pending", "invoiced") or not o["total"]["gross"]:
            raise ApiError(409, "not_open", "This order can't change how it is paid now.")
        if pay == "invoice" and not bank_ready(svc):
            raise ApiError(409, "no_transfer", "Paying by transfer is not set up here.")
        if pay == "card" and not stripe_ready(svc, env_of(o)):
            raise ApiError(409, "no_card", "Paying by card is not set up here.")
        vals: dict[str, Any] = {"pay": pay}
        has_invoice = bool((o.get("invoice") or {}).get("move_id"))
        if pay == "invoice" and not has_invoice:
            vals["step"] = "invoice"
        elif pay == "card":
            stripe = o.get("stripe") or {}
            fresh = stripe.get("url") and int(stripe.get("expires_at") or 0) > time.time() + 60
            if not fresh:
                vals["step"] = "card"
        st.update(oid, **vals)
        return advance(svc, oid)


_polled: dict[str, float] = {}
POLL_SECONDS = 10


def _refresh(svc: Services, o: dict[str, Any]) -> dict[str, Any]:
    """A buyer looking at their order: a waiting step may be tried again (at most once every
    10 seconds per order, never counted as a failure), and a card session that expired (Stripe
    keeps them 24 hours) is made anew."""
    t = time.monotonic()
    if t - _polled.get(o["id"], -1e9) < POLL_SECONDS:
        return o
    _polled[o["id"]] = t
    if len(_polled) > 10000:
        _polled.clear()
    if o.get("step") in ("invoice", "card", "register_payment", "activate"):
        o = advance(svc, o["id"]) or o
    if o["state"] in ("pending", "invoiced") and (o.get("stripe") or {}).get("expires_at"):
        if int(o["stripe"]["expires_at"]) < time.time() + 60 and stripe_ready(svc, env_of(o)):
            store(svc).update(o["id"], step="card")
            o = advance(svc, o["id"]) or o
    return o


def hmac_sign(svc: Services, text: str) -> str:
    """The belfry's signature of TEXT (signed /pay links)."""
    key = (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()
    return hmac.new(key, b"belfry-pay:" + text.encode(), hashlib.sha256).hexdigest()[:32]
