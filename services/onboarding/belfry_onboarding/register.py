"""The belfry's own register: who is who and who holds a pass, in one SQLite file, for a belfry
that runs without Odoo (REGISTER=belfry; decision 2026-11-08, Odoo becomes an optional add-on).

It speaks the small part of Odoo's RPC that the onboarding service uses (``OdooApi``:
search_read / read / write / create and the ``belfry_*`` methods of the belfry_pass addon), so the
service's code is the same with either. What it keeps:

- people (``res.partner``: name, e-mail, member number, key custody, verified key),
- passes (``belfry.pass``: state, dates, keys, wallet claim, one-time sign-in links),
- invites (from admins, from members, by e-mail), applications, e-mail codes,
- reports, feedback and followers.

What it does not do: invoices, payments, tiers and prices, volunteer shifts. Those are the Odoo
add-on's; asking for them here says so. A membership is free: an accepted invite or an admin
opens it (core.activate_free_pass), it has no invoice.

Every record is a JSON document in one table, keyed by model and id; ids grow per model and are
never reused, so ``odoo-partner-<id>`` (the bunker derives keys from it) stays unique for good.
Mails go through mailer.py (the belfry's mail provider)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .clients.odoo import OdooApi, OdooError
from .mailer import Mailer

log = logging.getLogger(__name__)

# Many2one fields: read/search_read return them as [id, name], like Odoo.
M2O: dict[str, dict[str, str]] = {
    "belfry.pass": {"partner_id": "res.partner", "invite_id": "belfry.invite"},
    "belfry.invite": {
        "inviter_partner_id": "res.partner",
        "used_by_partner_id": "res.partner",
        "requested_by_partner_id": "res.partner",
        "pass_id": "belfry.pass",
    },
    "belfry.application": {"invite_id": "belfry.invite"},
    "belfry.report": {"reporter_pass_id": "belfry.pass", "target_pass_id": "belfry.pass"},
    "belfry.feedback": {"partner_id": "res.partner"},
    "belfry.follower": {"partner_id": "res.partner"},
    "belfry.key.code": {"partner_id": "res.partner"},
    "res.partner": {},
}
MODELS = set(M2O)
UNIQUE: dict[str, list[str]] = {
    "belfry.pass": ["claim_token", "signin_token", "welcome_token"],
    "belfry.invite": ["token"],
    "belfry.follower": ["token"],
    "res.partner": ["belfry_member_number"],
}
# Odoo-only models (billing, shifts): searching them finds nothing here.
BILLING = {
    "account.move",
    "account.move.line",
    "payment.transaction",
    "product.product",
    "product.template",
    "sale.order",
    "belfry.shift",
    "belfry.stripe.event",
}
NEEDS_ODOO = "This needs the Odoo add-on (billing and shifts are not in the belfry's own register)."

INVITE_DAYS = 14
INVITES_PER_MEMBER = 3
CODE_MINUTES, CODE_TRIES, CODES_PER_EMAIL, CODES_PER_KEY, CODE_WINDOW = 10, 5, 5, 10, 30
WELCOME_HOURS = 24
FOLLOW_ASKS_PER_DAY = 3
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
FREQUENCIES = ("asap", "monthly")
TIER_NAMES = {"member": "Membership", "steward": "Membership (admin)"}


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)


def stamp(t: dt.datetime | None = None) -> str:
    return (t or now()).strftime("%Y-%m-%d %H:%M:%S")


def _user_error(message: str) -> OdooError:
    return OdooError(message, {"message": message, "name": "odoo.exceptions.UserError"})


def _npub(pubkey: str) -> str:
    from .nostr import hex_to_npub

    return hex_to_npub(pubkey)


class Register(OdooApi):
    def __init__(self, path: str | Path, settings: Any, mailer: Mailer | None = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.mailer = mailer or Mailer(settings)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                model TEXT NOT NULL, id INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (model, id));
            CREATE TABLE IF NOT EXISTS sequences (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS params (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    # ── storage ──────────────────────────────────────────────────────────────────────────────
    def _all(self, model: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, data FROM records WHERE model = ? ORDER BY id", (model,)
        ).fetchall()
        return [{**json.loads(d), "id": i} for i, d in rows]

    def _get(self, model: str, rec_id: int) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT data FROM records WHERE model = ? AND id = ?", (model, int(rec_id))
        ).fetchone()
        return {**json.loads(row[0]), "id": int(rec_id)} if row else None

    def _put(self, model: str, rec: dict[str, Any]) -> None:
        data = {k: v for k, v in rec.items() if k != "id"}
        self._db.execute(
            "INSERT OR REPLACE INTO records (model, id, data) VALUES (?, ?, ?)",
            (model, rec["id"], json.dumps(data, default=str)),
        )

    def _next(self, name: str, start: int = 0) -> int:
        row = self._db.execute("SELECT value FROM sequences WHERE name = ?", (name,)).fetchone()
        value = max(row[0] if row else 0, start) + 1
        self._db.execute(
            "INSERT OR REPLACE INTO sequences (name, value) VALUES (?, ?)", (name, value)
        )
        return value

    def param(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM params WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_param(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO params (key, value) VALUES (?, ?)", (key, value))

    def _create(self, model: str, vals: dict[str, Any]) -> int:
        vals = dict(vals)
        self._defaults(model, vals)
        self._check_unique(model, vals, None)
        rec_id = int(vals.pop("id", 0) or 0)
        top = self._db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM records WHERE model = ?", (model,)
        ).fetchone()[0]
        rec_id = rec_id or self._next("id:" + model, top)
        self._put(model, {**vals, "id": rec_id, "create_date": vals.get("create_date") or stamp()})
        return rec_id

    def _write(self, model: str, ids: list[int], vals: dict[str, Any]) -> None:
        for i in ids:
            rec = self._get(model, i)
            if rec is None:
                continue
            self._check_unique(model, vals, i)
            before = dict(rec)
            rec.update(vals)
            self._put(model, rec)
            if model == "belfry.pass":
                self._after_pass_write(before, rec, vals)

    def _defaults(self, model: str, vals: dict[str, Any]) -> None:
        if model == "belfry.pass":
            vals.setdefault("state", "pending")
            vals.setdefault("tier", "member")
            vals.setdefault("welcome_token", secrets.token_urlsafe(32))
        elif model == "belfry.invite":
            vals.setdefault("token", secrets.token_urlsafe(32))
            vals.setdefault("state", "open")
            vals.setdefault("expires_at", stamp(now() + dt.timedelta(days=INVITE_DAYS)))
        elif model == "belfry.follower":
            vals.setdefault("token", secrets.token_urlsafe(24))
            vals.setdefault("state", "pending")
            vals.setdefault("frequency", "monthly")
            vals.setdefault("channel", "general")
        elif model == "res.partner":
            vals.setdefault("belfry_key_custody", "belfry")
            vals.setdefault("belfry_key_version", 0)

    def _check_unique(self, model: str, vals: dict[str, Any], rec_id: int | None) -> None:
        for field in UNIQUE.get(model, []):
            value = vals.get(field)
            if not value:
                continue
            for other in self._all(model):
                if other["id"] != rec_id and other.get(field) == value:
                    raise OdooError(f'duplicate key value violates unique constraint "{field}"')

    # ── Odoo's RPC ───────────────────────────────────────────────────────────────────────────
    def execute_kw(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = self._dispatch(model, method, list(args), kwargs)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return result

    def _dispatch(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        if model in BILLING:
            if method in ("search_read", "read"):
                return []
            if method == "search_count":
                return 0
            raise OdooError(NEEDS_ODOO)
        if model not in MODELS:
            raise OdooError(f"Object {model} doesn't exist")
        if method == "search_read":
            domain = args[0] if args else kwargs.get("domain", [])
            return self._search_read(
                model, domain, kwargs.get("fields"), kwargs.get("limit"), kwargs.get("order")
            )
        if method == "search_count":
            return len(self._search_read(model, args[0] if args else [], ["id"], None, None))
        if method == "read":
            fields = args[1] if len(args) > 1 else kwargs.get("fields")
            return [self._format(model, r, fields) for i in args[0] if (r := self._get(model, i))]
        if method == "write":
            self._write(model, list(args[0]), dict(args[1]))
            return True
        if method == "create":
            return self._create(model, dict(args[0]))
        if method in (
            "belfry_shift_board",
            "belfry_steward_volunteers",
            "belfry_volunteer_of_partner",
        ):
            return [] if method != "belfry_volunteer_of_partner" else False  # lists stay empty
        handler = getattr(self, "_m_" + method, None)
        if handler is None:
            raise OdooError(
                NEEDS_ODOO
                if "shift" in method or "paid" in method
                else f"AttributeError: type object '{model}' has no attribute '{method}'"
            )
        return handler(model, *args, **kwargs)

    @staticmethod
    def _match(rec: dict[str, Any], field: str, op: str, value: Any) -> bool:
        actual = rec.get(field, False)
        if isinstance(actual, list) and len(actual) == 2 and isinstance(actual[0], int):
            actual = actual[0]
        if op == "=":
            return actual == value or (not actual and not value)
        if op == "!=":
            return bool(actual) if value is False else actual != value
        if op == "in":
            return actual in value
        if op == "not in":
            return actual not in value
        if op == "=ilike":
            return isinstance(actual, str) and actual.lower() == str(value).lower()
        if op == "ilike":
            return isinstance(actual, str) and str(value).lower() in actual.lower()
        if actual in (None, False):
            return False
        return {
            "<": actual < value,
            "<=": actual <= value,
            ">": actual > value,
            ">=": actual >= value,
        }[op]

    def _search_read(self, model, domain, fields, limit, order):
        rows = self._all(model)
        for clause in domain or []:
            if clause in ("&", "|", "!"):
                raise OdooError("The register supports AND domains only.")
            field, op, value = clause
            if field.startswith("partner_id.") and model in ("belfry.pass", "belfry.follower"):
                sub = field.split(".", 1)[1]
                ok = {p["id"] for p in self._all("res.partner") if self._match(p, sub, op, value)}
                rows = [r for r in rows if r.get("partner_id") in ok]
                continue
            rows = [r for r in rows if self._match(self._computed(model, r), field, op, value)]
        for part in reversed([p.strip() for p in (order or "id").split(",") if p.strip()]):
            key, _, direction = part.partition(" ")
            rows.sort(
                key=lambda r: (r.get(key) is None, r.get(key) or 0),
                reverse=direction.strip().lower() == "desc",
            )
        if limit:
            rows = rows[: int(limit)]
        return [self._format(model, r, fields) for r in rows]

    def _computed(self, model: str, rec: dict[str, Any]) -> dict[str, Any]:
        """Related and computed fields the service reads, as Odoo computes them."""
        out = dict(rec)
        if model == "belfry.pass":
            partner = self._get("res.partner", rec.get("partner_id") or 0) or {}
            out["member_number"] = partner.get("belfry_member_number") or False
            out["email"] = partner.get("email") or False
            out["tier_name"] = TIER_NAMES.get(rec.get("tier") or "", rec.get("tier") or "")
            out.setdefault("invoice_id", False)
        elif model == "belfry.invite":
            out["url"] = self._invite_url(rec)
        elif model == "belfry.report":
            for side in ("reporter", "target"):
                p = self._get("belfry.pass", rec.get(f"{side}_pass_id") or 0) or {}
                out[f"{side}_partner_id"] = p.get("partner_id") or False
        elif model == "belfry.follower":
            partner = self._get("res.partner", rec.get("partner_id") or 0) or {}
            out["email"] = partner.get("email") or False
        return out

    def _format(self, model: str, rec: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
        full = self._computed(model, rec)
        fields = fields or [f for f in full if f != "id"]
        out: dict[str, Any] = {"id": rec["id"]}
        m2o = dict(M2O.get(model, {}))
        if model == "belfry.report":
            m2o.update(reporter_partner_id="res.partner", target_partner_id="res.partner")
        for f in fields:
            v = full.get(f, False)
            if f in m2o:
                target = self._get(m2o[f], v) if v else None
                v = [v, (target or {}).get("name") or f"{m2o[f]},{v}"] if v else False
            out[f] = v if v is not None else False
        return out

    # ── people and passes ────────────────────────────────────────────────────────────────────
    def _m_belfry_ensure_member_number(self, model, ids, *_a, **_k):
        out = []
        for pid in ids:
            partner = self._get("res.partner", pid)
            if partner is None:
                continue
            if not partner.get("belfry_member_number"):
                top = max(
                    (
                        int(p["belfry_member_number"])
                        for p in self._all("res.partner")
                        if str(p.get("belfry_member_number") or "").isdigit()
                    ),
                    default=0,
                )
                number = f"{self._next('belfry.member', top):04d}"
                self._write("res.partner", [pid], {"belfry_member_number": number})
                partner["belfry_member_number"] = number
            out.append(partner["belfry_member_number"])
        return out

    def _after_pass_write(self, before: dict[str, Any], rec: dict[str, Any], vals: dict[str, Any]):
        """Odoo's belfry.pass.write: an active pass uses up its invite; a new sign-in link at
        activation (or asked for again) is mailed to the holder."""
        if vals.get("state") == "active" and before.get("state") != "active":
            if rec.get("invite_id"):
                inv = self._get("belfry.invite", rec["invite_id"])
                if inv and inv.get("state") in ("open", "reserved"):
                    self._write(
                        "belfry.invite",
                        [inv["id"]],
                        {"state": "used", "used_by_partner_id": rec.get("partner_id")},
                    )
        if vals.get("login_setup_url") and rec.get("state") == "active":
            self._mail_signin(rec)

    def _mail_signin(self, rec: dict[str, Any]) -> None:
        partner = self._get("res.partner", rec.get("partner_id") or 0) or {}
        if not partner.get("email"):
            log.warning("register: pass %s has no e-mail; sign-in link not mailed", rec["id"])
            return
        first = (partner.get("name") or "").split(" ")[0] or "there"
        until = (rec.get("login_setup_expires_at") or "")[:16]
        sent = self.mailer.send(
            partner["email"],
            f"Welcome to {self.mailer.name}",
            [
                f"Hello {first},",
                f"You are a member of {self.mailer.name} now (member number "
                f"{partner.get('belfry_member_number') or '—'}).",
                "The button signs you in, without a password, and takes you to the "
                "community's apps." + (f" It works once, until {until} (UTC)." if until else ""),
                "Afterwards you sign in with your e-mail address: you get a code by mail "
                "each time.",
            ],
            button=(rec["login_setup_url"], f"Sign in to {self.mailer.name}"),
        )
        if sent:
            self._write("belfry.pass", [rec["id"]], {"login_mail_sent_at": stamp()})

    def _by_token(self, field: str, token: str) -> dict[str, Any] | None:
        if not token or len(token) < 20:
            return None
        return next((p for p in self._all("belfry.pass") if p.get(field) == token), None)

    def _m_belfry_welcome_info(self, model, token, *_a, **_k):
        rec = self._by_token("welcome_token", token)
        if rec is None:
            return {"result": "not_found"}
        partner = self._get("res.partner", rec.get("partner_id") or 0) or {}
        fresh = bool(rec.get("activated_at")) and rec["activated_at"] > stamp(
            now() - dt.timedelta(hours=WELCOME_HOURS)
        )
        return {
            "result": "ok",
            "state": rec.get("state"),
            "payment": "paid",  # memberships here are free
            "first_name": (partner.get("name") or "").split(" ")[0],
            "tier_name": TIER_NAMES.get(rec.get("tier") or "", rec.get("tier") or ""),
            "date_start": rec.get("date_start") or False,
            "date_end": rec.get("date_end") or False,
            "claim_token": rec.get("claim_token") or False,
            "signin_available": rec.get("state") == "active"
            and not rec.get("welcome_used_at")
            and fresh,
            "return_url": rec.get("return_url") or False,
        }

    def _m_belfry_use_signin(self, model, kind, token, *_a, **_k):
        field = {"signin": "signin_token", "welcome": "welcome_token"}.get(kind)
        rec = self._by_token(field, token) if field else None
        if rec is None:
            return {"result": "not_found"}
        result = self._use(rec, kind)
        if kind == "welcome":
            result["return_url"] = rec.get("return_url") or False
        return result

    def _use(self, rec: dict[str, Any], kind: str) -> dict[str, Any]:
        if rec.get("state") != "active":
            return {"result": "not_active"}
        used = "signin_used_at" if kind == "signin" else "welcome_used_at"
        if rec.get(used):
            return {"result": "used"}
        if kind == "signin":
            exp = rec.get("login_setup_expires_at")
            expired = bool(exp) and exp < stamp()
        else:
            expired = not (
                rec.get("activated_at")
                and rec["activated_at"] > stamp(now() - dt.timedelta(hours=WELCOME_HOURS))
            )
        if expired:
            return {"result": "expired"}
        if not rec.get("authentik_user_id"):
            return {"result": "no_user"}
        self._write("belfry.pass", [rec["id"]], {used: stamp()})
        return {"result": "ok", "authentik_user_id": rec["authentik_user_id"], "pass_id": rec["id"]}

    def _active_pass(self, partner_id: int) -> dict[str, Any] | None:
        return next(
            (
                p
                for p in self._all("belfry.pass")
                if p.get("partner_id") == partner_id and p.get("state") == "active"
            ),
            None,
        )

    def _partner_by_email(self, email: str) -> dict[str, Any] | None:
        found = [
            p for p in self._all("res.partner") if (p.get("email") or "").lower() == email.lower()
        ]
        with_pass = [
            p
            for p in found
            if any(x.get("partner_id") == p["id"] for x in self._all("belfry.pass"))
        ]
        return (with_pass or found or [None])[0]

    def _m_belfry_steward_pass(self, model, email, name=False, *_a, **_k):
        """An admin's membership: free, no invoice. Returns the pass (still pending: the service
        opens it with core.activate_free_pass)."""
        email = (email or "").strip()
        if not EMAIL.match(email):
            raise _user_error("A valid e-mail address is required.")
        person = self._partner_by_email(email)
        pid = (
            person["id"]
            if person
            else self._create(
                "res.partner", {"name": (name or "").strip() or email, "email": email}
            )
        )
        for p in self._all("belfry.pass"):
            if p.get("partner_id") == pid and p.get("state") in ("pending", "active"):
                return {
                    "pass_id": p["id"],
                    "invoice_id": False,
                    "state": p["state"],
                    "created": False,
                }
        pass_id = self._create(
            "belfry.pass",
            {
                "partner_id": pid,
                "tier": "member",
                "steward_pass": True,
                "manual_paid_by": f"admin ({email})",
            },
        )
        return {"pass_id": pass_id, "invoice_id": False, "state": "pending", "created": True}

    # ── invites ──────────────────────────────────────────────────────────────────────────────
    def _invite_url(self, rec: dict[str, Any]) -> str:
        return f"{self.settings.public_url.rstrip('/')}/invite/{rec.get('token', '')}"

    def _usable(self, inv: dict[str, Any]) -> dict[str, Any]:
        if inv.get("state") == "open" and (inv.get("expires_at") or "9") < stamp():
            self._write("belfry.invite", [inv["id"]], {"state": "expired"})
            inv["state"] = "expired"
        return inv

    def _m_belfry_steward_create(
        self, model, steward_email, note=False, steward_name=False, *_a, **_k
    ):
        steward_email = (steward_email or "").strip()
        if not steward_email:
            raise _user_error("A steward e-mail is required.")
        inv_id = self._create(
            "belfry.invite",
            {
                "steward_email": steward_email,
                "note": note or False,
                "steward_id": False,
                "inviter_name": (steward_name or "").strip()[:100] or False,
            },
        )
        inv = self._get("belfry.invite", inv_id)
        return {"id": inv_id, "url": self._invite_url(inv), "token": inv["token"]}

    def _m_action_revoke(self, model, ids, *_a, **_k):
        for i in ids:
            inv = self._get("belfry.invite", i)
            if inv and inv.get("state") == "open":
                self._write("belfry.invite", [i], {"state": "revoked"})
        return True

    def _invite_row(self, inv: dict[str, Any]) -> dict[str, Any]:
        inv = self._usable(inv)
        used = self._get("res.partner", inv.get("used_by_partner_id") or 0) or {}
        return {
            "id": inv["id"],
            "url": self._invite_url(inv),
            "state": inv["state"],
            "note": inv.get("note") or "",
            "expires_at": inv.get("expires_at") or False,
            "used_by": used.get("name") or "",
        }

    def _member(self, partner_id: int, message: str) -> dict[str, Any]:
        partner = self._get("res.partner", int(partner_id))
        if not partner or not self._active_pass(partner["id"]):
            raise _user_error(message)
        return partner

    def _m_belfry_participant_invites(self, model, partner_id, *_a, **_k):
        mine = [
            i for i in self._all("belfry.invite") if i.get("inviter_partner_id") == int(partner_id)
        ]
        rows = [self._invite_row(i) for i in sorted(mine, key=lambda i: -i["id"])]
        limit = int(self.param("belfry.invites_per_participant", str(INVITES_PER_MEMBER)))
        taken = sum(1 for r in rows if r["state"] in ("open", "reserved", "used"))
        return {"limit": limit, "left": max(0, limit - taken), "invites": rows}

    def _m_belfry_participant_create(self, model, partner_id, note=False, *_a, **_k):
        partner = self._member(partner_id, "Only members can invite friends.")
        if not self._m_belfry_participant_invites(model, partner["id"])["left"]:
            raise _user_error("You have no invites left. Revoke an unused one, or ask an admin.")
        inv_id = self._create(
            "belfry.invite",
            {
                "inviter_partner_id": partner["id"],
                "inviter_name": partner.get("name"),
                "note": (note or "").strip()[:200] or False,
            },
        )
        return self._invite_row(self._get("belfry.invite", inv_id))

    def _m_belfry_participant_revoke(self, model, partner_id, invite_id, *_a, **_k):
        inv = self._get("belfry.invite", int(invite_id))
        if not inv or inv.get("inviter_partner_id") != int(partner_id):
            raise _user_error("No such invite.")
        self._m_action_revoke(model, [inv["id"]])
        return self._invite_row(self._get("belfry.invite", inv["id"]))

    def _holder_row(self, inv: dict[str, Any]) -> dict[str, Any]:
        inv = self._usable(inv)
        p = self._get("belfry.pass", inv.get("pass_id") or 0) or {}
        active = p.get("state") == "active"
        partner = self._get("res.partner", p.get("partner_id") or 0) or {}
        return {
            "id": inv["id"],
            "state": inv["state"],
            "email": inv.get("invitee_email") or "",
            "name": inv.get("invitee_name") or "",
            "note": inv.get("note") or "",
            "url": self._invite_url(inv),
            "expires_at": inv.get("expires_at") or False,
            "pass_state": p.get("state") or False,
            "npub": (partner.get("belfry_npub") or p.get("npub") or False) if active else False,
        }

    def _m_belfry_holder_invite(
        self, model, partner_id, email, name=False, note=False, steward_email=False, *_a, **_k
    ):
        holder = self._member(partner_id, "Only members can invite.")
        email = (email or "").strip()
        if email and not EMAIL.match(email):
            raise _user_error("A valid e-mail address is required.")
        name = (name or "").strip()[:100] or False
        if email:
            person = self._partner_by_email(email)
            if person and self._active_pass(person["id"]):
                return {
                    "state": "member",
                    "email": email,
                    "npub": person.get("belfry_npub") or False,
                }
            for i in self._all("belfry.invite"):
                if (
                    i.get("requested_by_partner_id") == holder["id"]
                    and (i.get("invitee_email") or "").lower() == email.lower()
                    and self._usable(i)["state"] in ("open", "reserved")
                ):
                    return self._holder_row(i)
        if steward_email:
            made = self._m_belfry_steward_create(model, steward_email, note, holder.get("name"))
        else:
            made = self._m_belfry_participant_create(model, holder["id"], note)
        self._write(
            "belfry.invite",
            [made["id"]],
            {
                "requested_by_partner_id": holder["id"],
                "invitee_email": email or False,
                "invitee_name": name,
                "inviter_name": holder.get("name"),
            },
        )
        inv = self._get("belfry.invite", made["id"])
        if email:
            self._mail_invite(inv)
        return self._holder_row(inv)

    def _mail_invite(self, inv: dict[str, Any]) -> None:
        who = inv.get("inviter_name") or "Someone"
        sent = self.mailer.send(
            inv["invitee_email"],
            f"{who} invites you to {self.mailer.name}",
            [
                f"Hello {inv.get('invitee_name') or ''}".strip() + ",",
                f"{who} invites you to join {self.mailer.name}."
                + (f' "{inv["note"]}"' if inv.get("note") else ""),
                f"The link is for you only and works until {(inv.get('expires_at') or '')[:10]}.",
            ],
            button=(self._invite_url(inv), "Accept the invite"),
        )
        if sent:
            self._write("belfry.invite", [inv["id"]], {"invite_mail_sent_at": stamp()})

    def _m_belfry_holder_invites(self, model, partner_id, *_a, **_k):
        mine = [
            i
            for i in self._all("belfry.invite")
            if i.get("requested_by_partner_id") == int(partner_id)
        ]
        return [self._holder_row(i) for i in sorted(mine, key=lambda i: -i["id"])]

    def _m_belfry_holder_invite_get(self, model, partner_id, invite_id, *_a, **_k):
        inv = self._get("belfry.invite", int(invite_id))
        if not inv or inv.get("requested_by_partner_id") != int(partner_id):
            return False
        return self._holder_row(inv)

    def _m_belfry_holder_invite_revoke(self, model, partner_id, invite_id, *_a, **_k):
        inv = self._get("belfry.invite", int(invite_id))
        if not inv or inv.get("requested_by_partner_id") != int(partner_id):
            raise _user_error("No such invite.")
        self._m_action_revoke(model, [inv["id"]])
        return self._holder_row(self._get("belfry.invite", inv["id"]))

    # invite page (invite.py): who may join with this link, then the pass
    def invite_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            inv = next(
                (
                    i
                    for i in self._all("belfry.invite")
                    if token and len(token) >= 20 and i.get("token") == token
                ),
                None,
            )
            return self._usable(inv) if inv else None

    def accept_invite(self, token: str, name: str, email: str) -> dict[str, Any]:
        """The invitee confirmed their address: their person and a pending pass, the invite
        reserved for it. Returns {"pass_id", "partner_id"} or raises OdooError."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = self._accept(token, name, email)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return out

    def _accept(self, token: str, name: str, email: str) -> dict[str, Any]:
        inv = next((i for i in self._all("belfry.invite") if i.get("token") == token), None)
        if inv is None:
            raise _user_error("This invite does not exist.")
        inv = self._usable(inv)
        if inv["state"] == "reserved" and inv.get("pass_id"):
            p = self._get("belfry.pass", inv["pass_id"]) or {}
            return {"pass_id": inv["pass_id"], "partner_id": p.get("partner_id")}
        if inv["state"] != "open":
            raise _user_error(f"This invite is {inv['state']}.")
        person = self._partner_by_email(email)
        if person and self._active_pass(person["id"]):
            raise _user_error("This address belongs to a member already: sign in instead.")
        pid = (
            person["id"]
            if person
            else self._create("res.partner", {"name": name.strip()[:100] or email, "email": email})
        )
        if person and name.strip() and person.get("name") in (None, "", email):
            self._write("res.partner", [pid], {"name": name.strip()[:100]})
        pass_id = self._create(
            "belfry.pass", {"partner_id": pid, "tier": "member", "invite_id": inv["id"]}
        )
        self._write(
            "belfry.invite",
            [inv["id"]],
            {"state": "reserved", "pass_id": pass_id, "used_by_partner_id": pid},
        )
        return {"pass_id": pass_id, "partner_id": pid}

    # ── applications ─────────────────────────────────────────────────────────────────────────
    def _m_belfry_application_create(self, model, vals, steward_emails=None, *_a, **_k):
        data = {
            k: str((vals or {}).get(k) or "").strip()
            for k in ("name", "email", "motivation", "kind")
        }
        if not data["name"] or not data["motivation"]:
            raise _user_error("Please enter your name and a few words about why you want to join.")
        if not EMAIL.match(data["email"]):
            raise _user_error("Please enter a valid e-mail address.")
        app_id = self._create(
            "belfry.application",
            {
                "name": data["name"][:200],
                "email": data["email"][:200],
                "motivation": data["motivation"][:4000],
                "kind": "volunteer" if data["kind"] == "volunteer" else "visitor",
                "state": "new",
                "handled_by": False,
                "duplicate_note": False,
            },
        )
        self.mailer.send(
            data["email"],
            f"We received your request to join {self.mailer.name}",
            [
                f"Hello {data['name']},",
                "Thank you. The admins read every request and answer by mail.",
            ],
        )
        admins = [e for e in (steward_emails or []) if e and "@" in e]
        if admins:
            self.mailer.send(
                sorted(set(admins)),
                f"{self.mailer.name}: {data['name']} asks to join",
                [
                    f"{data['name']} ({data['email']}) asks to join:",
                    data["motivation"],
                    "Answer in the admin console.",
                ],
            )
        return app_id

    def _application(self, ids: list[int]) -> dict[str, Any]:
        row = self._get("belfry.application", ids[0])
        if row is None:
            raise _user_error("No such application.")
        if row.get("state") != "new":
            raise _user_error(f"This application was answered already ({row['state']}).")
        return row

    def _m_belfry_application_invite(
        self, model, ids, steward_email, steward_name=False, note=False, *_a, **_k
    ):
        row = self._application(ids)
        created = self._m_belfry_steward_create(
            model, steward_email, note or f"Application: {row['name']}"[:200], steward_name
        )
        self._write(
            "belfry.application",
            [row["id"]],
            {
                "state": "invited",
                "invite_id": created["id"],
                "handled_by": steward_email,
                "handled_at": stamp(),
            },
        )
        self._write(
            "belfry.invite",
            [created["id"]],
            {"invitee_email": row["email"], "invitee_name": row["name"]},
        )
        self._mail_invite(self._get("belfry.invite", created["id"]))
        return created

    def _m_belfry_application_decline(self, model, ids, steward_email, *_a, **_k):
        row = self._application(ids)
        self._write(
            "belfry.application",
            [row["id"]],
            {"state": "declined", "handled_by": steward_email, "handled_at": stamp()},
        )
        self.mailer.send(
            row["email"],
            f"Your request to join {self.mailer.name}",
            [
                f"Hello {row['name']},",
                f"Thank you for your interest in {self.mailer.name}. We cannot offer "
                "you a place this time. If you know a member, they can invite you.",
            ],
        )
        return "declined"

    # ── e-mail codes for an app's key ────────────────────────────────────────────────────────
    @staticmethod
    def _clean(email: str, pubkey: str) -> tuple[str, str]:
        email = (email or "").strip()
        if not EMAIL.match(email) or len(email) > 254:
            raise _user_error("A valid e-mail address is required.")
        pubkey = (pubkey or "").strip().lower()
        if not HEX64.match(pubkey):
            raise _user_error("The key must be 64 hex characters.")
        return email, pubkey

    @staticmethod
    def _hash(code_id: int, code: str) -> str:
        return hashlib.sha256(f"{code_id}:{code}".encode()).hexdigest()

    def _m_belfry_key_code_send(self, model, email, pubkey, app_name=False, *_a, **_k):
        email, pubkey = self._clean(email, pubkey)
        since = stamp(now() - dt.timedelta(minutes=CODE_WINDOW))
        codes = [c for c in self._all("belfry.key.code") if c.get("create_date", "") >= since]
        if (
            sum(1 for c in codes if c["email"].lower() == email.lower()) >= CODES_PER_EMAIL
            or sum(1 for c in codes if c["pubkey"] == pubkey) >= CODES_PER_KEY
        ):
            return {"state": "too_many", "retry_after": CODE_WINDOW * 60}
        for c in self._all("belfry.key.code"):  # only the newest code works
            if (
                c["email"].lower() == email.lower()
                and c["pubkey"] == pubkey
                and not c.get("used_at")
                and c["expires_at"] > stamp()
            ):
                self._write("belfry.key.code", [c["id"]], {"expires_at": stamp()})
        code = f"{secrets.randbelow(10**6):06d}"
        expires = stamp(now() + dt.timedelta(minutes=CODE_MINUTES))
        cid = self._create(
            "belfry.key.code",
            {
                "email": email,
                "pubkey": pubkey,
                "app_name": (app_name or "").strip()[:60] or False,
                "code_hash": "-",
                "expires_at": expires,
                "tries": 0,
                "used_at": False,
            },
        )
        self._write("belfry.key.code", [cid], {"code_hash": self._hash(cid, code)})
        app = (app_name or "").strip()[:60]
        self.mailer.send(
            email,
            f"{code} is your {self.mailer.name} code",
            [
                (f"{app} asks" if app else "An app asks")
                + f" to connect to {self.mailer.name} with this address. Your code:",
                code,
                f"It works for {CODE_MINUTES} minutes. If you did not ask for it, "
                "ignore this mail.",
            ],
        )
        return {"state": "sent", "expires_at": expires}

    def _m_belfry_key_code_verify(self, model, email, pubkey, code, name=False, *_a, **_k):
        email, pubkey = self._clean(email, pubkey)
        live = [
            c
            for c in self._all("belfry.key.code")
            if c["email"].lower() == email.lower()
            and c["pubkey"] == pubkey
            and not c.get("used_at")
        ]
        rec = live[-1] if live else None
        if rec is None or rec["expires_at"] <= stamp() or rec.get("tries", 0) >= CODE_TRIES:
            return {"state": "expired"}
        tries = rec.get("tries", 0) + 1
        self._write("belfry.key.code", [rec["id"]], {"tries": tries})
        if not hmac.compare_digest(
            rec["code_hash"], self._hash(rec["id"], re.sub(r"\s", "", str(code or "")))
        ):
            return {"state": "wrong_code", "tries_left": CODE_TRIES - tries}
        self._write("belfry.key.code", [rec["id"]], {"used_at": stamp()})
        return self._bind(rec, email, pubkey, name)

    def _bind(self, rec: dict[str, Any], email: str, pubkey: str, name: Any) -> dict[str, Any]:
        key_npub = _npub(pubkey)
        partner = self._partner_by_email(email)
        if partner and partner.get("belfry_npub") and partner["belfry_npub"] != key_npub:
            return {"state": "other_key", "email": partner["email"], "npub": partner["belfry_npub"]}
        mine = partner["id"] if partner else None
        taken = any(
            p["id"] != mine
            and (p.get("belfry_npub") == key_npub or p.get("belfry_verified_pubkey") == pubkey)
            for p in self._all("res.partner")
        ) or any(
            p.get("partner_id") != mine
            and p.get("state") in ("pending", "active")
            and pubkey in (p.get("keycast_pubkey"), p.get("own_pubkey"))
            for p in self._all("belfry.pass")
        )
        if taken:
            return {"state": "key_taken"}
        if partner is None:
            mine = self._create(
                "res.partner", {"name": (name or "").strip()[:100] or email, "email": email}
            )
        self._write(
            "res.partner",
            [mine],
            {"belfry_verified_pubkey": pubkey, "belfry_email_verified_at": stamp()},
        )
        self._write("belfry.key.code", [rec["id"]], {"partner_id": mine})
        partner = self._get("res.partner", mine) or {}
        return {
            "state": "bound",
            "email": partner.get("email"),
            "npub": key_npub,
            "member": bool(self._active_pass(mine)),
        }

    # ── reports and feedback ─────────────────────────────────────────────────────────────────
    def _pass_for_pubkey(self, pubkey: str) -> int | bool:
        passes = [p for p in self._all("belfry.pass") if p.get("keycast_pubkey") == pubkey]
        active = [p for p in passes if p.get("state") == "active"]
        return (active or passes[::-1] or [{"id": False}])[0]["id"]

    def _m_belfry_report_create(self, model, vals, steward_emails=None, *_a, **_k):
        allowed = {
            "reporter_pubkey",
            "app",
            "target_kind",
            "target_ref",
            "target_pubkey",
            "target_url",
            "target_excerpt",
            "reason",
            "note",
        }
        data = {k: v for k, v in (vals or {}).items() if k in allowed}
        for key in ("reporter_pubkey", "target_pubkey", "target_ref"):
            if not data.get(key):
                raise _user_error(f"A report needs {key}.")
        data["note"] = (data.get("note") or "")[:2000] or False
        data["target_excerpt"] = (data.get("target_excerpt") or "")[:300] or False
        rid = self._create(
            "belfry.report",
            {
                **data,
                "state": "open",
                "handled_by": False,
                "reporter_pass_id": self._pass_for_pubkey(data["reporter_pubkey"]),
                "target_pass_id": self._pass_for_pubkey(data["target_pubkey"]),
            },
        )
        admins = [e for e in (steward_emails or []) if e and "@" in e]
        if admins:
            lines = [
                f"A member reported {data.get('target_kind') or 'something'} in "
                f"{data.get('app') or 'an app'} ({data.get('reason') or 'no reason'})."
            ]
            if data.get("target_excerpt"):
                lines.append(f"Excerpt: {data['target_excerpt']}")
            if data.get("note"):
                lines.append(f"Note: {data['note']}")
            lines.append("Handle it in the admin console.")
            self.mailer.send(sorted(set(admins)), f"{self.mailer.name}: new report", lines)
        return rid

    def _m_belfry_report_handle(
        self, model, ids, action, steward_email, steward_note=False, *_a, **_k
    ):
        states = {"hide": "hidden", "dismiss": "dismissed", "reopen": "open"}
        if action not in states:
            raise _user_error(f"Unknown action {action}.")
        if not (steward_email or "").strip():
            raise _user_error("A steward e-mail is required.")
        vals = {"state": states[action], "handled_by": steward_email.strip(), "handled_at": stamp()}
        if steward_note:
            vals["steward_note"] = steward_note[:2000]
        self._write("belfry.report", list(ids), vals)
        return states[action]

    def _m_belfry_hidden_refs(self, model, *_a, **_k):
        return sorted(
            {r["target_ref"] for r in self._all("belfry.report") if r.get("state") == "hidden"}
        )

    def _m_belfry_feedback_create(
        self,
        model,
        vals,
        steward_emails=None,
        screenshot_b64=False,
        screenshot_type=False,
        *_a,
        **_k,
    ):
        allowed = {"partner_id", "email", "page_url", "happened", "expected", "user_agent"}
        data = {k: v for k, v in (vals or {}).items() if k in allowed}
        for key, size in (
            ("happened", 4000),
            ("expected", 4000),
            ("page_url", 500),
            ("user_agent", 300),
            ("email", 200),
        ):
            if data.get(key):
                data[key] = str(data[key])[:size]
        ext = (screenshot_type or "image/png").split("/")[-1][:5]
        fid = self._create(
            "belfry.feedback",
            {
                **data,
                "state": "new",
                "handled_by": False,
                "screenshot": screenshot_b64 or False,
                "screenshot_name": f"screenshot.{ext}" if screenshot_b64 else False,
            },
        )
        admins = [e for e in (steward_emails or []) if e and "@" in e]
        if admins:
            partner = self._get("res.partner", data.get("partner_id") or 0) or {}
            who = partner.get("name") or data.get("email") or "someone not signed in"
            self.mailer.send(
                sorted(set(admins)),
                f"{self.mailer.name}: feedback from {who}",
                [
                    f"What happened: {data.get('happened') or '—'}",
                    f"What they expected: {data.get('expected') or '—'}",
                    f"Page: {data.get('page_url') or '—'}",
                ],
            )
        return fid

    def _m_belfry_feedback_handle(self, model, ids, action, steward_email, *_a, **_k):
        states = {"done": "done", "reopen": "new"}
        if action not in states or not (steward_email or "").strip():
            raise _user_error("Unknown action.")
        self._write(
            "belfry.feedback",
            list(ids),
            {"state": states[action], "handled_by": steward_email.strip(), "handled_at": stamp()},
        )
        return states[action]

    # ── followers ────────────────────────────────────────────────────────────────────────────
    def _channel(self, channel: str) -> str:
        channel = (channel or "").strip()
        if not CHANNEL.match(channel):
            raise _user_error("Unknown channel.")
        return channel

    def _follower(self, partner_id: int, channel: str) -> dict[str, Any] | None:
        return next(
            (
                f
                for f in self._all("belfry.follower")
                if f.get("partner_id") == partner_id and f.get("channel") == channel
            ),
            None,
        )

    def _public(self, f: dict[str, Any]) -> dict[str, Any]:
        email = (self._get("res.partner", f.get("partner_id") or 0) or {}).get("email") or ""
        return {
            "id": f["id"],
            "state": f["state"],
            "frequency": f["frequency"],
            "email": email,
            "channel": f["channel"],
        }

    def _m_belfry_follow_request(self, model, email, channel="general", *_a, **_k):
        email = (email or "").strip()
        if len(email) > 254 or not EMAIL.match(email):
            raise _user_error("A valid e-mail address is required.")
        partner = self._partner_by_email(email)
        pid = (
            partner["id"]
            if partner
            else self._create("res.partner", {"name": email, "email": email})
        )
        channel = self._channel(channel)
        f = self._follower(pid, channel)
        if f is None:
            f = self._get(
                "belfry.follower",
                self._create("belfry.follower", {"partner_id": pid, "channel": channel}),
            )
        if f["state"] == "active":
            return {"state": "following", "id": f["id"], "token": f["token"]}
        if f.get("asked_at") and f["asked_at"] > stamp(now() - dt.timedelta(days=1)):
            if (f.get("asks") or 0) >= FOLLOW_ASKS_PER_DAY:
                return {"state": "too_many"}
            self._write("belfry.follower", [f["id"]], {"asks": (f.get("asks") or 0) + 1})
        else:
            self._write("belfry.follower", [f["id"]], {"asks": 1, "asked_at": stamp()})
        return {"state": "send", "id": f["id"], "token": f["token"]}

    def _by_follow_token(self, token: str) -> dict[str, Any] | None:
        token = (token or "").strip()
        if not token or len(token) > 100:
            return None
        return next((f for f in self._all("belfry.follower") if f.get("token") == token), None)

    def _m_belfry_follow_get(self, model, token, *_a, **_k):
        f = self._by_follow_token(token)
        return self._public(f) if f else {"state": "unknown"}

    def _m_belfry_follow_set(self, model, token, choice, *_a, **_k):
        f = self._by_follow_token(token)
        if f is None:
            return {"state": "unknown"}
        if choice == "stop":
            if f["state"] != "stopped":
                self._write(
                    "belfry.follower", [f["id"]], {"state": "stopped", "stopped_at": stamp()}
                )
        elif choice in FREQUENCIES:
            vals: dict[str, Any] = {"frequency": choice}
            if f["state"] != "active":
                vals.update(
                    state="active",
                    confirmed_at=stamp(),
                    stopped_at=False,
                    sent_until=int(dt.datetime.now(dt.UTC).timestamp()),
                )
            self._write("belfry.follower", [f["id"]], vals)
        else:
            raise _user_error("Unknown choice.")
        return self._public(self._get("belfry.follower", f["id"]))

    def _m_belfry_follow_due(self, model, frequency, *_a, **_k):
        if frequency not in FREQUENCIES:
            raise _user_error("Unknown frequency.")
        out = []
        for f in self._all("belfry.follower"):
            if f.get("state") == "active" and f.get("frequency") == frequency:
                email = self._public(f)["email"]
                if email:
                    out.append(
                        {
                            "id": f["id"],
                            "email": email,
                            "frequency": frequency,
                            "token": f["token"],
                            "channel": f["channel"],
                            "since": f.get("sent_until") or 0,
                        }
                    )
        return out

    def _m_belfry_follow_member_list(self, model, partner_id, *_a, **_k):
        return [
            {"channel": f["channel"], "frequency": f["frequency"], "state": f["state"]}
            for f in self._all("belfry.follower")
            if f.get("partner_id") == int(partner_id)
        ]

    def _m_belfry_follow_member_set(self, model, partner_id, channel, choice, *_a, **_k):
        partner = self._get("res.partner", int(partner_id))
        if not partner or not partner.get("email"):
            raise _user_error("This member has no e-mail address.")
        channel = self._channel(channel)
        f = self._follower(partner["id"], channel)
        if f is None:
            if choice == "stop":
                return {"channel": channel, "state": "stopped", "frequency": "monthly"}
            f = self._get(
                "belfry.follower",
                self._create("belfry.follower", {"partner_id": partner["id"], "channel": channel}),
            )
        result = self._m_belfry_follow_set(model, f["token"], choice)
        return {"channel": channel, "state": result["state"], "frequency": result["frequency"]}

    def _m_belfry_follow_claim_day(self, model, day, *_a, **_k):
        if self.param("belfry.announcements_digest_day") >= day:
            return False
        self.set_param("belfry.announcements_digest_day", day)
        return True

    def _m_belfry_follow_mail(
        self, model, follower_id, subject, body_html, until=0, headers=None, *_a, **_k
    ):
        f = self._get("belfry.follower", int(follower_id))
        email = self._public(f)["email"] if f else ""
        if not email:
            return False
        sent = self.mailer.send_html(email, subject or "", body_html or "", headers or {})
        if until:
            self._write(
                "belfry.follower",
                [f["id"]],
                {"sent_until": max(f.get("sent_until") or 0, int(until)), "last_sent_at": stamp()},
            )
        return f["id"] if sent else False
