"""In-memory stand-ins for Odoo and Belfry's signer. They support only what the code calls."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx
import respx

from belfry_onboarding.clients.odoo import OdooApi, OdooError

# Many2one fields per model: ``read``/``search_read`` return them as ``[id, name]`` like Odoo.
M2O: dict[str, set[str]] = {
    "account.move": {"partner_id"},
    "account.move.line": {"move_id", "product_id"},
    "payment.transaction": {"source_transaction_id"},
    "belfry.pass": {"partner_id", "product_id", "invoice_id", "transaction_id", "invite_id"},
    "belfry.stripe.event": {"pass_id"},
    "belfry.invite": {"inviter_partner_id", "used_by_partner_id"},
    "belfry.feedback": {"partner_id"},
    "belfry.application": {"invite_id"},
    "belfry.shift": set(),
    "belfry.report": {"reporter_pass_id", "target_pass_id"},
    "product.product": set(),
    "res.partner": set(),
    "belfry.key.code": set(),
    "belfry.login.code": {"partner_id"},
}
UNIQUE: dict[str, list[str]] = {
    "belfry.pass": ["invoice_id", "stripe_payment_intent", "claim_token"],
    "belfry.stripe.event": ["event_id"],
    "res.partner": ["belfry_member_number"],
}


SHIFT_METHODS = {
    "belfry_shift_board",
    "belfry_shift_signup",
    "belfry_shift_leave",
    "belfry_steward_shift_create",
    "belfry_steward_shift_cancel",
    "belfry_steward_volunteers",
    "belfry_steward_volunteer_add",
    "belfry_application_volunteer",
    "belfry_volunteer_by_token",
    "belfry_volunteer_of_partner",
}


class FakeOdoo(OdooApi):
    def __init__(self, legacy_module: bool = False):
        self.tables: dict[str, dict[int, dict[str, Any]]] = {m: {} for m in M2O}
        self.legacy_module = legacy_module  # simulate a module without the public RPC methods
        self.calls: list[tuple[str, str]] = []
        self._seq = 0
        self._login_seq = 0
        # sign-in mails: (model, address, code, link secret)
        self.mails: list[tuple[str, str, str, str]] = []

    # -- seeding ------------------------------------------------------------------------------

    invites_per_participant = 3

    mailed_invites: list[int] = []

    def _key_codes(self, method: str, args: list[Any]) -> Any:
        """belfry_key_code.py: e-mail codes for an app's key. The mailed code lands in
        ``mailed_codes[email]``; ``key_code_answer`` overrides the next verify answer."""
        codes = self.tables["belfry.key.code"]
        email, pubkey = args[0], args[1]
        if "@" not in email:
            raise OdooError("A valid e-mail address is required.")
        if method == "belfry_key_code_send":
            if sum(1 for c in codes.values() if c["email"] == email) >= 5:
                return {"state": "too_many"}
            code = f"{len(codes) + 123456}"
            self.add("belfry.key.code", email=email, pubkey=pubkey, code=code, tries=0, app=args[2])
            self.mailed_codes = {**self.mailed_codes, email: code}
            return {"state": "sent", "expires_at": "2027-01-25 10:10:00"}
        if method == "belfry_key_code_verify":
            if self.key_code_answer:
                answer, self.key_code_answer = self.key_code_answer, None
                return answer
            live = [c for c in codes.values() if c["email"] == email and c["pubkey"] == pubkey]
            if not live or live[-1]["tries"] >= 5 or live[-1].get("used"):
                return {"state": "expired"}
            rec = live[-1]
            rec["tries"] += 1
            if rec["code"] != args[2]:
                return {"state": "wrong_code", "tries_left": 5 - rec["tries"]}
            rec["used"] = True
            partner = next(
                (p for p in self.tables["res.partner"].values() if p.get("email") == email), None
            )
            pid = partner["id"] if partner else self.add("res.partner", name=email, email=email)
            self.tables["res.partner"][pid]["belfry_verified_pubkey"] = pubkey
            return {"state": "bound", "email": email, "npub": "npub-of-" + pubkey, "member": False}
        raise NotImplementedError(method)

    mailed_codes: dict[str, str] = {}
    key_code_answer: dict[str, Any] | None = None

    def _holder_invites(self, method: str, args: list[Any]) -> Any:
        """belfry_holder_invites.py: invites by e-mail for a pass holder."""
        partner_id = args[0]
        invites = self.tables["belfry.invite"]

        def row(i: dict[str, Any]) -> dict[str, Any]:
            p = self.tables["belfry.pass"].get(i.get("pass_id") or 0, {})
            active = p.get("state") == "active"
            npub = self.tables["res.partner"].get(p.get("partner_id"), {}).get("belfry_npub")
            return {
                "id": i["id"],
                "state": i["state"],
                "email": i.get("invitee_email") or "",
                "name": i.get("invitee_name") or "",
                "note": i.get("note") or "",
                "url": i["url"],
                "expires_at": i["expires_at"],
                "pass_state": p.get("state") or False,
                "npub": (npub or False) if active else False,
            }

        mine = [i for i in invites.values() if i.get("requested_by_partner_id") == partner_id]
        if method == "belfry_holder_invites":
            return [row(i) for i in sorted(mine, key=lambda i: -i["id"])]
        if method == "belfry_holder_invite_get":
            found = [i for i in mine if i["id"] == args[1]]
            return row(found[0]) if found else False
        if method == "belfry_holder_invite_revoke":
            found = [i for i in mine if i["id"] == args[1]]
            if not found:
                raise OdooError("UserError", {"message": "No such invite."})
            if found[0]["state"] == "open":
                found[0]["state"] = "revoked"
            return row(found[0])
        # belfry_holder_invite(partner_id, email, name, note, steward_email)
        email, name, note, steward_email = args[1], args[2], args[3], args[4]
        if not any(
            p.get("partner_id") == partner_id and p.get("state") == "active"
            for p in self.tables["belfry.pass"].values()
        ):
            raise OdooError(
                "UserError", {"message": "Only participants with an active pass can invite."}
            )
        email = email or ""  # no e-mail: an invite link the holder passes on, nothing mailed
        for p in self.tables["belfry.pass"].values():
            holder = self.tables["res.partner"].get(p.get("partner_id"), {})
            if (
                email
                and p.get("state") == "active"
                and (holder.get("email") or "").lower() == email.lower()
            ):
                return {
                    "state": "member",
                    "email": email,
                    "npub": holder.get("belfry_npub") or False,
                }
        for i in mine:
            if (
                email
                and (i.get("invitee_email") or "").lower() == email.lower()
                and i["state"] in ("open", "reserved")
            ):
                return row(i)
        if steward_email:
            made = self.execute_kw("belfry.invite", "belfry_steward_create", [steward_email, note])
        else:
            made = self._participant_invites("belfry_participant_create", [partner_id, note])
        inv = invites[made["id"]]
        inv.update(requested_by_partner_id=partner_id, invitee_email=email, invitee_name=name)
        if email:
            self.mailed_invites = [*self.mailed_invites, inv["id"]]
        return row(inv)

    def _steward_pass(self, email: str, name: Any) -> dict[str, Any]:
        """belfry_steward.py: the steward's free pass on a zero-amount, paid invoice."""
        partners = self.tables["res.partner"]
        person = next(
            (pid for pid, p in partners.items() if (p.get("email") or "").lower() == email.lower()),
            None,
        ) or self.add("res.partner", name=name or email, email=email)
        for p in self.tables["belfry.pass"].values():
            if p.get("partner_id") == person and p.get("state") in ("pending", "active"):
                return {
                    "pass_id": p["id"],
                    "invoice_id": p.get("invoice_id") or False,
                    "state": p["state"],
                    "created": False,
                }
        if not self.tables["product.product"]:
            self.add(
                "product.product", name="Full access", belfry_tier="full", belfry_duration_days=0
            )
        product = next(
            pid for pid, p in self.tables["product.product"].items() if p["belfry_tier"] == "full"
        )
        invoice = self.add(
            "account.move",
            name=f"INV/STEWARD/{person}",
            move_type="out_invoice",
            state="posted",
            payment_state="paid",
            partner_id=person,
            invoice_date="2027-01-19",
            transaction_ids=[],
        )
        self.add("account.move.line", move_id=invoice, product_id=product)
        pass_id = self.add(
            "belfry.pass",
            partner_id=person,
            tier="full",
            product_id=product,
            invoice_id=invoice,
            state="pending",
            steward_pass=True,
            manual_paid_by=f"steward pass ({email})",
        )
        return {"pass_id": pass_id, "invoice_id": invoice, "state": "pending", "created": True}

    def _participant_invites(self, method: str, args: list[Any]) -> Any:
        """belfry_friends.py: a participant's own invites, within the limit."""
        partner_id = args[0]
        mine = [
            i
            for i in self.tables["belfry.invite"].values()
            if i.get("inviter_partner_id") == partner_id
        ]

        def row(i: dict[str, Any]) -> dict[str, Any]:
            used = i.get("used_by_partner_id")
            return {
                "id": i["id"],
                "url": i["url"],
                "state": i["state"],
                "note": i.get("note") or "",
                "expires_at": i["expires_at"],
                "used_by": self.tables["res.partner"].get(used, {}).get("name", "") if used else "",
            }

        taken = sum(1 for i in mine if i["state"] in ("open", "reserved", "used"))
        if method == "belfry_participant_invites":
            rows = [row(i) for i in sorted(mine, key=lambda i: -i["id"])]
            return {
                "limit": self.invites_per_participant,
                "left": max(0, self.invites_per_participant - taken),
                "invites": rows,
            }
        if method == "belfry_participant_create":
            active = any(
                p.get("partner_id") == partner_id and p.get("state") == "active"
                for p in self.tables["belfry.pass"].values()
            )
            if not active:
                raise OdooError(
                    "UserError",
                    {"message": "Only participants with an active pass can invite friends."},
                )
            if taken >= self.invites_per_participant:
                raise OdooError(
                    "UserError",
                    {
                        "message": "You have no invites left. "
                        "Revoke an unused one, or ask a steward."
                    },
                )
            inv_id = self.add(
                "belfry.invite",
                inviter_partner_id=partner_id,
                inviter_name=self.tables["res.partner"].get(partner_id, {}).get("name"),
                note=args[1] if len(args) > 1 else False,
                state="open",
                token=f"ptok{len(self.tables['belfry.invite']) + 1}",
                expires_at="2026-11-08 10:00:00",
                create_date="2026-10-25 10:00:00",
            )
            inv = self.tables["belfry.invite"][inv_id]
            inv["url"] = f"http://odoo.test/belfry/invite/{inv['token']}"
            return row(inv)
        if method == "belfry_participant_revoke":
            inv = next((i for i in mine if i["id"] == args[1]), None)
            if inv is None:
                raise OdooError("UserError", {"message": "No such invite."})
            if inv["state"] == "open":
                inv["state"] = "revoked"
            return row(inv)
        raise OdooError(f"unknown method {method}")

    def add(self, model: str, **vals: Any) -> int:
        self._check_unique(model, vals, None)
        rec_id = vals.pop("id", None) or 1 + max(self.tables[model].keys(), default=0)
        self.tables[model][rec_id] = {"id": rec_id, **vals}
        if model == "payment.transaction":
            self._link_transaction(rec_id)
        return rec_id

    def get(self, model: str, rec_id: int) -> dict[str, Any]:
        return self.tables[model][rec_id]

    def _link_transaction(self, tx_id: int) -> None:
        tx = self.tables["payment.transaction"][tx_id]
        for inv_id in tx.get("invoice_ids", []):
            inv = self.tables["account.move"][inv_id]
            inv.setdefault("transaction_ids", [])
            if tx_id not in inv["transaction_ids"]:
                inv["transaction_ids"].append(tx_id)

    def _volunteer_info(self, p: dict[str, Any]) -> dict[str, Any]:
        count = sum(
            1 for sh in self.tables["belfry.shift"].values() if p["partner_id"] in sh["signups"]
        )
        return {
            "pass_id": p["id"],
            "partner_id": p["partner_id"],
            "name": "Vic",
            "state": p["state"],
            "count": count,
            "min_shifts": 3,
            "change_hours": 48,
            "date_start": p.get("date_start"),
            "date_end": p.get("date_end"),
        }

    def _volunteer_pass(self, partner_id: int) -> dict[str, Any] | None:
        return next(
            (
                p
                for p in self.tables["belfry.pass"].values()
                if p.get("tier") == "volunteer"
                and p.get("partner_id") == partner_id
                and p.get("state") in ("pending", "active")
            ),
            None,
        )

    def _shifts(self, method: str, args: list[Any]) -> Any:
        """Odoo's models/belfry_volunteer.py, without overlaps and the leave deadline."""
        shifts = self.tables["belfry.shift"]
        if method == "belfry_shift_board":
            me = args[0] if args else None
            return [
                {
                    "id": i,
                    "title": sh["name"],
                    "description": "",
                    "start": sh["start"],
                    "end": sh["end"],
                    "place": sh.get("place") or "",
                    "capacity": sh["capacity"],
                    "taken": len(sh["signups"]),
                    "people": ["Vic"] * len(sh["signups"]),
                    "mine": me in sh["signups"],
                }
                for i, sh in shifts.items()
            ]
        if method == "belfry_steward_shift_create":
            return self.add("belfry.shift", **args[0], signups=[])
        if method == "belfry_steward_shift_cancel":
            for i in args[0]:
                shifts.pop(i, None)
            return True
        if method == "belfry_steward_volunteers":
            return [
                self._volunteer_info(p) | {"email": "vic@example.org"}
                for p in self.tables["belfry.pass"].values()
                if p.get("tier") == "volunteer"
            ]
        if method in ("belfry_steward_volunteer_add", "belfry_application_volunteer"):
            if method == "belfry_application_volunteer":
                self.tables["belfry.application"][args[0][0]].update(
                    state="invited", handled_by=args[1]
                )
                partner = self.add("res.partner", name="Vic", email="vic@example.org")
            else:
                partner = self.add("res.partner", name=args[0], email=args[1])
            return self.add(
                "belfry.pass",
                partner_id=partner,
                tier="volunteer",
                state="pending",
                volunteer_token=f"vol-{partner:04d}" + "x" * 30,
            )
        if method == "belfry_volunteer_by_token":
            p = next(
                (
                    p
                    for p in self.tables["belfry.pass"].values()
                    if p.get("volunteer_token") == args[0]
                ),
                None,
            )
            return self._volunteer_info(p) if p else False
        if method == "belfry_volunteer_of_partner":
            p = self._volunteer_pass(args[0])
            return self._volunteer_info(p) if p else False
        p = self._volunteer_pass(args[0])
        if p is None:
            raise OdooError("Only volunteers can sign up for shifts.")
        sh = shifts[args[1]]
        if method == "belfry_shift_signup":
            if len(sh["signups"]) >= sh["capacity"]:
                raise OdooError("This shift is full.")
            sh["signups"].append(args[0])
            info = self._volunteer_info(p)
            info["ready"] = p["state"] == "pending" and info["count"] >= 3
            return info
        sh["signups"].remove(args[0])
        info = self._volunteer_info(p)
        info["below_minimum"] = p["state"] == "active" and info["count"] < 3
        return info

    def _signin(self, method: str, *args: Any) -> dict[str, Any]:
        """Odoo's models/belfry_welcome.py, without the expiry of the welcome page."""
        kind, token = args if method == "belfry_use_signin" else ("welcome", args[0])
        field = {"signin": "signin_token", "welcome": "welcome_token"}[kind]
        p = next(
            (p for p in self.tables["belfry.pass"].values() if token and p.get(field) == token),
            None,
        )
        if p is None:
            return {"result": "not_found"}
        result = self._use_signin(method, kind, p)
        if kind == "welcome":
            result["return_url"] = p.get("return_url") or False
        return result

    def _use_signin(self, method: str, kind: str, p: dict[str, Any]) -> dict[str, Any]:
        used = "signin_used_at" if kind == "signin" else "welcome_used_at"
        if method == "belfry_welcome_info":
            return {
                "result": "ok",
                "state": p.get("state"),
                "first_name": "Alice",
                "payment": "paid" if p.get("state") == "active" or p.get("paid") else "unpaid",
                "tier_name": "Day pass",
                "date_start": p.get("date_start"),
                "date_end": p.get("date_end"),
                "claim_token": p.get("claim_token"),
                "signin_available": p.get("state") == "active" and not p.get(used),
            }
        if p.get("state") != "active":
            return {"result": "not_active"}
        if p.get(used):
            return {"result": "used"}
        if kind == "signin" and (p.get("login_setup_expires_at") or "") < "2000":
            return {"result": "expired"}
        p[used] = "2026-10-26 12:00:00"
        return {"result": "ok", "partner_id": p["partner_id"], "pass_id": p["id"]}

    # -- sign-in by e-mail (models/belfry_login.py) and stewards ------------------------------

    def may_sign_in(self, partner_id: int) -> bool:
        partner = self.tables["res.partner"].get(int(partner_id))
        if partner is None:
            return False
        return bool(partner.get("belfry_steward")) or any(
            p.get("partner_id") == int(partner_id) and p.get("state") == "active"
            for p in self.tables["belfry.pass"].values()
        )

    def _login(self, method: str, args: list[Any]) -> Any:
        codes = self.tables["belfry.login.code"]
        if method == "belfry_may_sign_in":
            return self.may_sign_in(args[0])
        if method == "belfry_login_send":
            email = (args[0] or "").strip()
            if "@" not in email:
                raise OdooError("A valid e-mail address is required.")
            partner = next(
                (
                    p
                    for p in self.tables["res.partner"].values()
                    if (p.get("email") or "").lower() == email.lower() and self.may_sign_in(p["id"])
                ),
                None,
            )
            if partner is None:
                return {"state": "sent"}
            self._login_seq += 1
            n = self._login_seq
            code, secret = f"{100000 + n}", f"link-secret-{n:04d}-xxxxxxxxxxxxxxxx"
            self.add(
                "belfry.login.code",
                email=email,
                partner_id=partner["id"],
                code=code,
                secret=secret,
                next_url=args[1] if len(args) > 1 else False,
                used=False,
                tries=0,
            )
            self.mails.append(("belfry.login.code", email, code, secret))
            return {"state": "sent"}
        if method == "belfry_login_verify":
            email, code = args
            rec = next(
                (
                    r
                    for r in reversed(list(codes.values()))
                    if r["email"].lower() == email.lower() and not r["used"]
                ),
                None,
            )
            if rec is None or rec["tries"] >= 5:
                return {"state": "expired"}
            rec["tries"] += 1
            if str(code).replace(" ", "") != rec["code"]:
                return {"state": "wrong_code", "tries_left": 5 - rec["tries"]}
            rec["used"] = True
            if not self.may_sign_in(rec["partner_id"]):
                return {"state": "not_active"}
            return {"state": "ok", "partner_id": rec["partner_id"], "next_url": rec["next_url"]}
        if method == "belfry_login_use_link":
            rec = next((r for r in codes.values() if r["secret"] == args[0]), None)
            if rec is None:
                return {"state": "not_found"}
            if rec["used"]:
                return {"state": "used"}
            rec["used"] = True
            if not self.may_sign_in(rec["partner_id"]):
                return {"state": "not_active"}
            return {"state": "ok", "partner_id": rec["partner_id"], "next_url": rec["next_url"]}
        raise OdooError(f"belfry.login.code has no method {method}")

    def _stewards(self, method: str, args: list[Any]) -> Any:
        partners = self.tables["res.partner"]
        if method == "belfry_steward_emails":
            return sorted(
                {
                    p["email"]
                    for p in partners.values()
                    if p.get("belfry_steward") and p.get("email")
                }
            )
        email = args[0].strip()
        partner = next(
            (p for p in partners.values() if (p.get("email") or "").lower() == email.lower()), None
        )
        created = partner is None
        if created:
            pid = self.add("res.partner", name=(len(args) > 1 and args[1]) or email, email=email)
            partner = partners[pid]
        partner["belfry_steward"] = True
        return {"partner_id": partner["id"], "created": created}

    # -- execute_kw ---------------------------------------------------------------------------

    def execute_kw(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.calls.append((model, method))
        if model not in self.tables:
            raise OdooError(f"Object {model} doesn't exist")
        if method == "search_read":
            return self._search_read(
                model, args[0], kwargs.get("fields"), kwargs.get("limit"), kwargs.get("order")
            )
        if method == "read":
            return [
                self._format(
                    model, self.tables[model][i], args[1] if len(args) > 1 else kwargs.get("fields")
                )
                for i in args[0]
                if i in self.tables[model]
            ]
        if method == "write":
            ids, vals = args
            for i in ids:
                self._check_unique(model, vals, i)
                self.tables[model][i].update(vals)
            return True
        if method == "create":
            vals = dict(args[0])
            return self.add(model, **vals)
        if model == "res.partner" and method == "belfry_ensure_member_number":
            if self.legacy_module:
                raise OdooError(
                    "AttributeError: type object 'res.partner' has no attribute "
                    "'belfry_ensure_member_number'"
                )
            out = []
            for pid in args[0]:
                partner = self.tables[model][pid]
                if not partner.get("belfry_member_number"):
                    self._seq += 1
                    partner["belfry_member_number"] = f"OSV-{self._seq:04d}"
                out.append(partner["belfry_member_number"])
            return out
        if model == "belfry.login.code":
            return self._login(method, args)
        if model == "res.partner" and method in ("belfry_steward_emails", "belfry_make_steward"):
            return self._stewards(method, args)
        if model == "belfry.pass" and method in ("belfry_use_signin", "belfry_welcome_info"):
            return self._signin(method, *args)
        if model == "belfry.pass" and method == "belfry_find_or_create_for_invoice":
            if self.legacy_module:
                raise OdooError(
                    "AttributeError: type object 'belfry.pass' has no attribute "
                    "'belfry_find_or_create_for_invoice'"
                )
            invoice_id = args[0]
            for p in self.tables[model].values():
                if p.get("invoice_id") == invoice_id:
                    return p["id"]
            invoice = self.tables["account.move"][invoice_id]
            product = None
            for line in self.tables["account.move.line"].values():
                if line.get("move_id") == invoice_id and line.get("product_id"):
                    product = self.tables["product.product"][line["product_id"]]
                    if product.get("belfry_tier"):
                        break
            if not product or not product.get("belfry_tier"):
                raise OdooError("ValidationError: invoice has no pass product")
            return self.add(
                "belfry.pass",
                partner_id=invoice["partner_id"],
                tier=product["belfry_tier"],
                product_id=product["id"],
                invoice_id=invoice_id,
                state="pending",
            )
        if model == "belfry.report" and method == "belfry_report_create":
            vals, emails = args[0], args[1] if len(args) > 1 else []
            by_key = {p.get("nostr_pubkey"): p["id"] for p in self.tables["belfry.pass"].values()}
            rid = self.add(
                "belfry.report",
                **vals,
                state="open",
                reporter_pass_id=by_key.get(vals.get("reporter_pubkey")) or False,
                target_pass_id=by_key.get(vals.get("target_pubkey")) or False,
                mailed_to=list(emails),
                create_date="2026-10-25 10:00:00",
                handled_by=False,
            )
            return rid
        if model == "belfry.report" and method == "belfry_report_handle":
            (rid,), action, email = args[0], args[1], args[2]
            states = {"hide": "hidden", "dismiss": "dismissed", "reopen": "open"}
            if action not in states:
                raise OdooError(f"UserError: Unknown action {action}.")
            self.tables[model][rid].update(state=states[action], handled_by=email)
            return states[action]
        if model == "belfry.report" and method == "belfry_hidden_refs":
            return sorted(
                {r["target_ref"] for r in self.tables[model].values() if r.get("state") == "hidden"}
            )
        if model == "belfry.invite" and method == "belfry_steward_create":
            email, note = args[0], args[1] if len(args) > 1 else False
            inv_id = self.add(
                "belfry.invite",
                steward_email=email,
                inviter_name=args[2] if len(args) > 2 else False,
                note=note,
                state="open",
                token=f"tok{len(self.tables[model]) + 1}",
                expires_at="2026-11-06 10:00:00",
                create_date="2026-10-23 10:00:00",
                steward_id=False,
            )
            inv = self.tables[model][inv_id]
            inv["url"] = f"http://odoo.test/belfry/invite/{inv['token']}"
            return {"id": inv_id, "url": inv["url"], "token": inv["token"]}
        if model == "belfry.key.code":
            return self._key_codes(method, args)
        if model == "belfry.invite" and method.startswith("belfry_holder_"):
            return self._holder_invites(method, args)
        if model == "belfry.pass" and method == "belfry_steward_pass":
            return self._steward_pass(args[0], args[1] if len(args) > 1 else False)
        if model == "belfry.invite" and method.startswith("belfry_participant_"):
            return self._participant_invites(method, args)
        if model == "belfry.feedback" and method == "belfry_feedback_create":
            vals, emails = args[0], args[1] if len(args) > 1 else []
            return self.add(
                "belfry.feedback",
                **vals,
                state="new",
                mailed_to=list(emails),
                screenshot=args[2] if len(args) > 2 else False,
                screenshot_name=f"screenshot.{(args[3] or 'image/png').split('/')[1]}"
                if len(args) > 3 and args[2]
                else False,
                create_date="2026-10-25 10:00:00",
                handled_by=False,
            )
        if model == "belfry.application" and method == "belfry_application_create":
            vals, emails = args[0], args[1] if len(args) > 1 else []
            if "@" not in (vals.get("email") or ""):
                raise OdooError("Please enter a valid e-mail address.")
            return self.add(
                "belfry.application",
                **vals,
                state="new",
                mailed_to=list(emails),
                create_date="2026-10-26 10:00:00",
                handled_by=False,
                duplicate_note=False,
            )
        if model == "belfry.application" and method in (
            "belfry_application_invite",
            "belfry_application_decline",
        ):
            (aid,), email = args[0], args[1]
            row = self.tables[model][aid]
            if row["state"] != "new":
                raise OdooError(f"This application was answered already ({row['state']}).")
            if method == "belfry_application_decline":
                row.update(state="declined", handled_by=email)
                return "declined"
            created = self.execute_kw("belfry.invite", "belfry_steward_create", [email, "app"])
            row.update(state="invited", handled_by=email, invite_id=created["id"])
            return created
        if (
            model in ("belfry.shift", "belfry.pass", "belfry.application")
            and method in SHIFT_METHODS
        ):
            return self._shifts(method, args)
        if model == "belfry.feedback" and method == "belfry_feedback_handle":
            (fid,), action, email = args[0], args[1], args[2]
            state = {"done": "done", "reopen": "new"}[action]
            self.tables[model][fid].update(state=state, handled_by=email)
            return state
        if model == "belfry.invite" and method == "action_revoke":
            for i in args[0]:
                if self.tables[model][i]["state"] == "open":
                    self.tables[model][i]["state"] = "revoked"
            return True
        if model == "belfry.pass" and method == "belfry_steward_mark_paid":
            (pass_id,), email = args[0], args[1]
            p = self.tables[model][pass_id]
            invoice = self.tables["account.move"][p["invoice_id"]]
            if p["state"] != "pending" or invoice["payment_state"] in ("paid", "in_payment"):
                raise OdooError("UserError: only a pending pass with an unpaid invoice")
            p["manual_paid_by"] = email
            invoice["payment_state"] = "paid"
            return "paid"
        raise OdooError(f"AttributeError: type object '{model}' has no attribute '{method}'")

    # -- internals ----------------------------------------------------------------------------

    def _check_unique(self, model: str, vals: dict[str, Any], rec_id: int | None) -> None:
        for field in UNIQUE.get(model, []):
            value = vals.get(field)
            if not value:
                continue
            for other in self.tables[model].values():
                if other["id"] != rec_id and other.get(field) == value:
                    raise OdooError(
                        "IntegrityError: duplicate key value violates unique constraint "
                        f'"{model}_{field}_uniq"'
                    )

    @staticmethod
    def _match(rec: dict[str, Any], field: str, op: str, value: Any) -> bool:
        actual = rec.get(field, False)
        if op == "=":
            return actual == value or (not actual and not value)
        if op == "!=":
            if value is False:
                return bool(actual)
            return actual != value
        if op == "in":
            return actual in value
        if op == "not in":
            return actual not in value
        if actual in (None, False):
            return False
        if op == "<":
            return actual < value
        if op == "<=":
            return actual <= value
        if op == ">":
            return actual > value
        if op == ">=":
            return actual >= value
        raise NotImplementedError(f"domain operator {op!r}")

    def _search_read(self, model, domain, fields, limit, order):
        rows = list(self.tables[model].values())
        for clause in domain:
            if clause in ("&", "|", "!"):
                raise NotImplementedError("FakeOdoo supports only AND domains")
            field, op, value = clause
            rows = [r for r in rows if self._match(r, field, op, value)]
        if order:
            key, _, direction = order.partition(" ")
            rows.sort(key=lambda r: r.get(key) or 0, reverse=direction.strip() == "desc")
        else:
            rows.sort(key=lambda r: r["id"])
        if limit:
            rows = rows[:limit]
        return [self._format(model, r, fields) for r in rows]

    def _format(self, model: str, rec: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
        fields = fields or [f for f in rec if f != "id"]
        out: dict[str, Any] = {"id": rec["id"]}
        for f in fields:
            v = rec.get(f, False)
            if f in M2O[model]:
                v = [v, f"{model},{v}"] if v else False
            out[f] = v
        return out


class FakeSigner:
    """respx stand-in for Belfry's signer (services/signer): accounts keyed by vine_id, status,
    signing, derived secrets, bunker connections. ``derived=True`` makes pubkeys depend on
    (vine_id, key_version) like the custody seed; ``seed_locked`` refuses new accounts."""

    def __init__(self, router: respx.MockRouter, base_url: str = "http://signer.test"):
        self.accounts: dict[str, dict[str, Any]] = {}
        self.connections: dict[str, dict[str, Any]] = {}
        self.derived = False
        self.seed_locked = False
        self.fail = False  # every account creation answers 500
        self.creates = 0
        self.status_calls: list[tuple[str, str]] = []
        self.signed: list[dict[str, Any]] = []
        self.imported: list[dict[str, Any]] = []
        base = re.escape(base_url.rstrip("/"))
        acc = rf"{base}/accounts/(?P<vid>[A-Za-z0-9_-]+)"
        router.put(url__regex=acc + "$").mock(side_effect=self._ensure)
        router.get(url__regex=acc + "$").mock(side_effect=self._get)
        router.post(url__regex=acc + "/status$").mock(side_effect=self._status)
        router.post(url__regex=acc + "/sign$").mock(side_effect=self._sign)
        router.post(url__regex=acc + "/(?P<op>encrypt|decrypt)$").mock(side_effect=self._crypt)
        router.post(url__regex=acc + "/derive$").mock(side_effect=self._derive)
        router.post(url__regex=acc + "/export$").mock(side_effect=self._export)
        router.post(url__regex=acc + "/connections$").mock(side_effect=self._connect)
        router.get(url__regex=acc + "/connections$").mock(side_effect=self._list)
        router.post(url__regex=acc + "/connections/revoke$").mock(side_effect=self._revoke)
        router.post(url__regex=rf"{base}/import$").mock(side_effect=self._import)

    @staticmethod
    def _err(status: int, code: str, message: str = "") -> httpx.Response:
        return httpx.Response(status, json={"error": code, "message": message or code})

    def _ensure(self, request: httpx.Request, vid: str) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        own = body.get("own_pubkey")
        acc = self.accounts.get(vid)
        if own:
            if acc is None or acc["pubkey"] != own:
                self.accounts[vid] = {
                    **(acc or {"status": "active", "key_version": 0}),
                    "pubkey": own,
                    "custody": "own",
                }
                for c in self.connections.values():
                    if c["vine_id"] == vid:
                        c["revoked"] = True
            return httpx.Response(200, json={"vine_id": vid, **self.accounts[vid]})
        if acc is not None:
            return httpx.Response(200, json={"vine_id": vid, **acc})
        if self.fail:
            return self._err(500, "internal", "boom")
        if self.seed_locked:
            return self._err(503, "seed_locked")
        version = int(body.get("key_version") or 0)
        if self.derived:
            pubkey = hashlib.sha256(f"{vid}/{version}".encode()).hexdigest()
        else:
            pubkey = f"{len(self.accounts) + 1:064x}"
        if body.get("expect_pubkey") and body["expect_pubkey"] != pubkey:
            return self._err(409, "mismatch")
        self.creates += 1
        self.accounts[vid] = {
            "pubkey": pubkey,
            "custody": "derived",
            "key_version": version,
            "status": "active",
        }
        return httpx.Response(200, json={"vine_id": vid, **self.accounts[vid]})

    def _get(self, request: httpx.Request, vid: str) -> httpx.Response:
        if vid not in self.accounts:
            return self._err(404, "not_found")
        return httpx.Response(200, json={"vine_id": vid, **self.accounts[vid]})

    def _status(self, request: httpx.Request, vid: str) -> httpx.Response:
        if vid not in self.accounts:
            return self._err(404, "not_found")
        status = json.loads(request.content)["status"]
        self.accounts[vid]["status"] = status
        self.status_calls.append((vid, status))
        return httpx.Response(200, json={"ok": True})

    def _usable(self, vid: str, body: dict[str, Any]) -> httpx.Response | None:
        acc = self.accounts.get(vid)
        if acc is None:
            return self._err(404, "not_found")
        if acc["status"] != "active":
            return self._err(403, "suspended")
        if acc["custody"] == "own":
            return self._err(409, "own_key")
        conn = body.get("connection")
        if conn and (conn not in self.connections or self.connections[conn]["revoked"]):
            return self._err(403, "forbidden", "this connection has been revoked")
        return None

    def _sign(self, request: httpx.Request, vid: str) -> httpx.Response:
        body = json.loads(request.content)
        refused = self._usable(vid, body)
        if refused is not None:
            return refused
        event = dict(body["event"])
        event["pubkey"] = self.accounts[vid]["pubkey"]
        event.setdefault("created_at", 1700000000)
        event.setdefault("tags", [])
        event.setdefault("content", "")
        event["id"] = f"{len(self.signed) + 1:064x}"
        event["sig"] = "fake-sig"
        self.signed.append(event)
        return httpx.Response(200, json={"event": event})

    def _crypt(self, request: httpx.Request, vid: str, op: str) -> httpx.Response:
        body = json.loads(request.content)
        refused = self._usable(vid, body)
        if refused is not None:
            return refused
        text = body["text"]
        result = f"enc:{text}" if op == "encrypt" else text.removeprefix("enc:")
        return httpx.Response(200, json={"result": result})

    def _derive(self, request: httpx.Request, vid: str) -> httpx.Response:
        body = json.loads(request.content)
        refused = self._usable(vid, body)
        if refused is not None:
            return refused
        conn = self.connections.get(body.get("connection") or "")
        if conn and body["purpose"] not in conn["derive"]:
            return self._err(403, "forbidden", "this app may not derive that")
        secret = hashlib.sha256(f"{vid}/{body['purpose']}".encode()).hexdigest()
        return httpx.Response(200, json={"secret": secret})

    def _export(self, request: httpx.Request, vid: str) -> httpx.Response:
        refused = self._usable(vid, {})
        if refused is not None:
            return refused
        return httpx.Response(200, json={"nsec": "nsec1fake" + self.accounts[vid]["pubkey"][:8]})

    def _connect(self, request: httpx.Request, vid: str) -> httpx.Response:
        body = json.loads(request.content)
        refused = self._usable(vid, {})
        if refused is not None:
            return refused
        cid = f"conn{len(self.connections) + 1:03d}"
        self.connections[cid] = {
            "id": cid,
            "vine_id": vid,
            "app": body["app"],
            "label": body.get("label") or body["app"],
            "derive": body.get("derive") or [],
            "keep": body.get("keep"),
            "created_at": 1700000000 + len(self.connections),
            "last_used_at": None,
            "revoked": False,
        }
        return httpx.Response(
            200,
            json={
                "id": cid,
                "bunker_url": f"bunker://{'b' * 64}?relay=wss%3A%2F%2Fbunker.test&secret=s{cid}",
                "bunker_pubkey": "b" * 64,
                "pubkey": self.accounts[vid]["pubkey"],
            },
        )

    def live(self, vid: str) -> list[dict[str, Any]]:
        return [c for c in self.connections.values() if c["vine_id"] == vid and not c["revoked"]]

    def _list(self, request: httpx.Request, vid: str) -> httpx.Response:
        if vid not in self.accounts:
            return self._err(404, "not_found")
        rows = [{k: v for k, v in c.items() if k != "revoked"} for c in self.live(vid)]
        return httpx.Response(200, json=rows)

    def _revoke(self, request: httpx.Request, vid: str) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        n = 0
        for c in self.live(vid):
            if (body.get("id") and c["id"] == body["id"]) or (
                not body.get("id") and (not body.get("app") or c["app"] == body["app"])
            ):
                c["revoked"] = True
                n += 1
        return httpx.Response(200, json={"revoked": n})

    def _import(self, request: httpx.Request) -> httpx.Response:
        rows = json.loads(request.content)["accounts"]
        self.imported.extend(rows)
        results = []
        for row in rows:
            if row["vine_id"] in self.accounts:
                results.append({"vine_id": row["vine_id"], "result": "exists"})
                continue
            self.accounts[row["vine_id"]] = {
                "pubkey": row["pubkey"],
                "custody": "imported",
                "key_version": 0,
                "status": "active",
            }
            results.append({"vine_id": row["vine_id"], "result": "imported"})
        return httpx.Response(200, json={"results": results})


class FakeIssuer:
    """respx stand-in for walt.id Issuer2's offer endpoint: records the credentialData and
    status overrides, answers with an offer URL carrying a pre-authorized code."""

    def __init__(self, router: respx.MockRouter, base_url: str = "http://issuer.test"):
        self.offers: list[dict[str, Any]] = []
        base = base_url.rstrip("/")
        router.post(f"{base}/issuer2/credential-offers").mock(side_effect=self._create)

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.offers.append(body)
        n = len(self.offers)
        offer = {
            "credential_issuer": "http://localhost:7005/openid4vci",
            "credential_configuration_ids": ["village_pass"],
            "grants": (
                {"authorization_code": {"issuer_state": f"session-{n:03d}"}}
                if body.get("authMethod") == "AUTHORIZED"
                else {
                    "urn:ietf:params:oauth:grant-type:pre-authorized_code": {
                        "pre-authorized_code": f"code-{n:03d}"
                    }
                }
            ),
        }
        from urllib.parse import quote

        return httpx.Response(
            200,
            json={
                "offerId": f"offer-{n:03d}",
                "profileId": body["profileId"],
                "authMethod": body.get("authMethod"),
                "expiresAt": 1_900_000_000_000,
                "credentialOffer": "openid-credential-offer://?credential_offer="
                + quote(json.dumps(offer, separators=(",", ":")), safe=""),
            },
        )


class FakeVerifier:
    """respx stand-in for walt.id Verifier2: sessions are created pending; a test marks one as
    presented (``succeed``) or failed and the info endpoint reflects it."""

    def __init__(self, router: respx.MockRouter, base_url: str = "http://verifier.test"):
        self.sessions: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        base = base_url.rstrip("/")
        router.post(f"{base}/verification-session/create").mock(side_effect=self._create)
        router.get(url__regex=rf"{re.escape(base)}/verification-session/(?P<sid>[^/]+)/info").mock(
            side_effect=self._info
        )

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.created.append(body)
        sid = f"vs-{len(self.created):03d}"
        self.sessions[sid] = {"id": sid, "status": "ACTIVE", "attempted": False}
        return httpx.Response(
            200,
            json={
                "sessionId": sid,
                "bootstrapAuthorizationRequestUrl": f"openid4vp://authorize?request_uri="
                f"http%3A%2F%2Flocalhost%3A7004%2Fverification-session%2F{sid}%2Frequest",
            },
        )

    def _info(self, request: httpx.Request, sid: str) -> httpx.Response:
        if sid not in self.sessions:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=self.sessions[sid])

    def succeed(self, sid: str, claims: dict[str, Any], leaf: str = "MIIBfake") -> None:
        """Mark the session as presented; ``leaf`` is the x5c[0] of the issuer JWT (the fixture
        issuer leaf by default, so the door and bridge accept it)."""
        import base64

        header = (
            base64.urlsafe_b64encode(
                json.dumps({"alg": "ES256", "typ": "dc+sd-jwt", "x5c": [leaf, "MIIBca"]}).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        self.sessions[sid].update(
            {
                "status": "SUCCESSFUL",
                "attempted": True,
                "presented_credentials": {
                    "village_pass": [
                        {
                            "type": "vc-sd_jwt",
                            "credentialData": claims,
                            "signed": f"{header}.e30.c2ln",
                        }
                    ]
                },
                "policy_results": {
                    "vc_policies": [{"policy": {"id": "signature"}, "success": True}]
                },
            }
        )

    def fail(self, sid: str, policy: str = "credential-status") -> None:
        self.sessions[sid].update(
            {
                "status": "FAILED",
                "attempted": True,
                "policy_results": {
                    "vc_policies": [
                        {"policy": {"id": "signature"}, "success": True},
                        {"policy": {"id": policy}, "success": False},
                    ]
                },
            }
        )


class FakeRelay:
    """Stands in for RelayPublisher: records published events, optionally demands AUTH or fails."""

    url = "ws://relay.test"

    def __init__(
        self, require_auth: bool = False, fail: bool = False, max_limit: int | None = None
    ):
        self.require_auth = require_auth
        self.fail = fail
        # like rnostr's `max_limit`: at most this many events per filter, newest first
        self.max_limit = max_limit
        self.events: list[dict[str, Any]] = []
        self.auth_events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any], sign) -> None:
        if self.fail:
            raise RuntimeError("relay down")
        if self.require_auth:
            auth = sign(
                {
                    "kind": 22242,
                    "created_at": 0,
                    "tags": [["relay", self.url], ["challenge", "c1"]],
                    "content": "",
                }
            )
            assert auth["kind"] == 22242 and auth["pubkey"] == event["pubkey"]
            self.auth_events.append(auth)
        self.events.append(event)

    def query(self, filters: list[dict[str, Any]], sign) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("relay down")
        out: list[dict[str, Any]] = []
        for f in filters:
            hits = [
                e
                for e in self.events
                if e["kind"] in f.get("kinds", [e["kind"]])
                and e["pubkey"] in f.get("authors", [e["pubkey"]])
                and e["created_at"] <= f.get("until", e["created_at"])
                and all(
                    any(t[0] == name[1:] and t[1] in values for t in e["tags"])
                    for name, values in f.items()
                    if name.startswith("#")
                )
            ]
            caps = [n for n in (f.get("limit"), self.max_limit) if n is not None]
            if caps:
                hits = sorted(hits, key=lambda e: e["created_at"], reverse=True)[: min(caps)]
            out += [e for e in hits if e not in out]
        return out
