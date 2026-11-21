"""In-memory stand-ins for Odoo, Authentik and Keycast. They support only what the code calls."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx
import respx
from coincurve import PrivateKey

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
    "belfry.follower": {"partner_id"},
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
            from belfry_onboarding.nostr import hex_to_npub

            if (
                partner
                and partner.get("belfry_npub")
                and partner["belfry_npub"] != hex_to_npub(pubkey)
            ):
                # like Odoo: the right code, but the contact holds another key (its own)
                return {"state": "other_key", "email": email, "npub": partner["belfry_npub"]}
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
        return {"result": "ok", "authentik_user_id": p["authentik_user_id"], "pass_id": p["id"]}

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
                    partner["belfry_member_number"] = f"{self._seq:04d}"
                out.append(partner["belfry_member_number"])
            return out
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
            by_key = {p.get("keycast_pubkey"): p["id"] for p in self.tables["belfry.pass"].values()}
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
        if op == "=ilike":
            return isinstance(actual, str) and actual.lower() == str(value).lower()
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


class FakeAuthentik:
    """respx routes for the Authentik calls the service makes; counts calls."""

    def __init__(self, router: respx.MockRouter, base_url: str = "http://authentik.test"):
        self.users: list[dict[str, Any]] = []
        self.connections: list[dict[str, Any]] = []
        self.user_creates = 0
        self.connection_creates = 0
        # authentik 2026.8.5 refuses API-created source connections; set to emulate that.
        self.reject_connections = False
        # Groups by name -> {"pk": uuid, "members": set of user pks}; the blueprint creates
        # belfry-participants, so it exists unless a test deletes it.
        self.groups: dict[str, dict[str, Any]] = {
            "belfry-participants": {"pk": "0000-participants", "members": set()}
        }
        self.group_calls: list[tuple[str, str, int]] = []
        # Account-setup (recovery) links: (user pk, token_duration) per call; the link carries
        # this fake's own origin, like authentik builds it from the request host.
        self.recovery_links: list[tuple[int, str]] = []
        # Emulate a brand without a recovery flow (blueprint belfry-account-setup-flow missing).
        self.recovery_flow_missing = False
        base = base_url.rstrip("/")
        self.base = base
        router.post(url__regex=rf"{re.escape(base)}/api/v3/core/users/(?P<pk>\d+)/recovery/").mock(
            side_effect=self._recovery_link
        )
        router.get(f"{base}/api/v3/core/groups/").mock(side_effect=self._list_groups)
        router.post(
            url__regex=rf"{re.escape(base)}/api/v3/core/groups/(?P<gpk>[^/]+)/(?P<op>add_user|remove_user)/"
        ).mock(side_effect=self._group_member)
        router.get(f"{base}/api/v3/core/users/").mock(side_effect=self._list_users)
        router.post(f"{base}/api/v3/core/users/").mock(side_effect=self._create_user)
        router.get(f"{base}/api/v3/sources/user_connections/oauth/").mock(
            side_effect=self._list_connections
        )
        router.post(f"{base}/api/v3/sources/user_connections/oauth/").mock(
            side_effect=self._create_connection
        )

    def _recovery_link(self, request: httpx.Request, pk: str) -> httpx.Response:
        if self.recovery_flow_missing:
            return httpx.Response(400, json={"non_field_errors": "No recovery flow set."})
        if not any(u["pk"] == int(pk) for u in self.users):
            return httpx.Response(404, json={"detail": "Not found."})
        body = json.loads(request.content or b"{}")
        self.recovery_links.append((int(pk), body.get("token_duration", "hours=1")))
        token = f"tok{len(self.recovery_links):03d}"
        return httpx.Response(
            200, json={"link": f"{self.base}/if/flow/belfry-account-setup/?flow_token={token}"}
        )

    def _list_users(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        results = self.users
        if "attributes" in params:
            wanted = json.loads(params["attributes"])
            results = [
                u for u in results if all(u["attributes"].get(k) == v for k, v in wanted.items())
            ]
        if "email" in params:
            results = [u for u in results if u.get("email") == params["email"]]
        if "groups_by_name" in params:
            members = self.groups.get(params["groups_by_name"], {}).get("members", set())
            results = [u for u in results if u["pk"] in members]
        return httpx.Response(
            200, json={"pagination": {"count": len(results), "next": 0}, "results": results}
        )

    def members(self, name: str = "belfry-participants") -> set[int]:
        return set(self.groups[name]["members"])

    def _list_groups(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name")
        results = [{"pk": g["pk"], "name": n} for n, g in self.groups.items() if name in (None, n)]
        return httpx.Response(200, json={"pagination": {"count": len(results)}, "results": results})

    def _group_member(self, request: httpx.Request, gpk: str, op: str) -> httpx.Response:
        group = next((g for g in self.groups.values() if g["pk"] == gpk), None)
        user_pk = json.loads(request.content)["pk"]
        if group is None or not any(u["pk"] == user_pk for u in self.users):
            return httpx.Response(404, json={"detail": "Not found."})
        self.group_calls.append((op, gpk, user_pk))
        if op == "add_user":
            group["members"].add(user_pk)
        else:
            group["members"].discard(user_pk)
        return httpx.Response(204)

    def _create_user(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(u["username"] == body["username"] for u in self.users):
            return httpx.Response(
                400, json={"username": ["user with this username already exists."]}
            )
        user = {"pk": len(self.users) + 1, **body}
        self.users.append(user)
        self.user_creates += 1
        return httpx.Response(201, json=user)

    def _list_connections(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        results = [
            c
            for c in self.connections
            if str(c["user"]) == params.get("user") and c["source"] == params.get("source__slug")
        ]
        return httpx.Response(200, json={"pagination": {"count": len(results)}, "results": results})

    def _create_connection(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if self.reject_connections:
            return httpx.Response(400, json={"source": ["Object with slug= does not exist."]})
        conn = {"pk": len(self.connections) + 1, **body}
        self.connections.append(conn)
        self.connection_creates += 1
        return httpx.Response(201, json=conn)


def nip04_encrypt(secret: bytes, peer: str, text: str) -> str:
    """NIP-04 (AES-256-CBC under the ECDH x coordinate), for the fake signer and test clients."""
    import base64
    import os

    from coincurve import PublicKey
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = PublicKey(b"\x02" + bytes.fromhex(peer)).multiply(secret).format(compressed=True)[1:33]
    iv = os.urandom(16)
    pad = padding.PKCS7(128).padder()
    data = pad.update(text.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(ct).decode() + "?iv=" + base64.b64encode(iv).decode()


def nip04_decrypt(secret: bytes, peer: str, payload: str) -> str:
    import base64

    from coincurve import PublicKey
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    ct, iv = payload.split("?iv=")
    key = PublicKey(b"\x02" + bytes.fromhex(peer)).multiply(secret).format(compressed=True)[1:33]
    dec = Cipher(algorithms.AES(key), modes.CBC(base64.b64decode(iv))).decryptor()
    data = dec.update(base64.b64decode(ct)) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    return (unpad.update(data) + unpad.finalize()).decode()


def real_crypt(secret: bytes, method: str, peer: str, text: str) -> str:
    """What a signer holding SECRET answers to a NIP-46 encryption method."""
    from belfry_onboarding.nip46check import conversation_key, nip44_decrypt, nip44_encrypt

    if method.startswith("nip44"):
        conv = conversation_key(secret, bytes.fromhex(peer))
        if method.endswith("encrypt"):
            return nip44_encrypt(conv, text)
        return nip44_decrypt(conv, text)
    if method.endswith("encrypt"):
        return nip04_encrypt(secret, peer, text)
    return nip04_decrypt(secret, peer, text)


class FakeKeycast:
    """respx routes for preload-user (idempotent on vine_id) and the status endpoint."""

    def __init__(self, router: respx.MockRouter, base_url: str = "http://keycast.test"):
        self.kind = "keycast"  # which signer this fake stands for (FakeSigner: "belfry")
        self.users: dict[str, dict[str, Any]] = {}  # vine_id -> user
        self.status: dict[str, str] = {}  # pubkey -> status
        self.preloads = 0
        self.preload_bodies: list[dict[str, Any]] = []
        # derived=True: pubkeys depend on (vine_id, key_version) like Keycast patch 0014
        self.derived = False
        self.status_calls: list[tuple[str, str]] = []
        self.signed: list[dict[str, Any]] = []
        self.keys: dict[str, bytes] = {}  # pubkey -> secret key, for users with a real key
        self.rpc: list[tuple[str, str]] = []  # (method, acting pubkey) of every /api/nostr call
        self.down = False  # /api/nostr answers 503
        base = base_url.rstrip("/")
        self.preload_route = router.post(f"{base}/api/admin/preload-user").mock(
            side_effect=self._preload
        )
        router.post(f"{base}/api/nostr").mock(side_effect=self._sign)
        router.put(
            url__regex=rf"{re.escape(base)}/api/admin/users/(?P<pubkey>[0-9a-f]+)/status"
        ).mock(side_effect=self._set_status)
        # the user API with the last preloaded user's UCAN (the fake hands out one token)
        self.last_pubkey = ""
        self.binds = True  # Keycast patch 0022 (a session bound to its client key)
        self.bunkers: list[
            dict[str, Any]
        ] = []  # {"pubkey", "app_name", "bunker_url", "secret", "bunker_secret"}
        router.post(f"{base}/api/user/profile").mock(side_effect=self._profile)
        router.post(f"{base}/api/user/bunker/create").mock(side_effect=self._bunker)
        router.get(f"{base}/api/user/profile").mock(side_effect=self._get_profile)
        router.get(f"{base}/api/user/sessions").mock(side_effect=self._sessions)
        router.post(f"{base}/api/user/sessions/revoke").mock(side_effect=self._revoke)
        router.get(f"{base}/.well-known/nostr.json").mock(side_effect=self._nip05)

    def _preload(self, request: httpx.Request) -> httpx.Response:
        self.preloads += 1
        body = json.loads(request.content)
        self.preload_bodies.append(body)
        vine_id = body["vine_id"]
        if vine_id not in self.users:
            if any(u["username"].lower() == body["username"].lower() for u in self.users.values()):
                return httpx.Response(
                    409, json={"error": f"User with username {body['username']} already exists"}
                )
            if body.get("pubkey"):
                pubkey = body["pubkey"]
            elif self.derived:
                seed = f"{vine_id}/{body.get('key_version', 0)}".encode()
                pubkey = hashlib.sha256(seed).hexdigest()
            else:
                pubkey = f"{len(self.users) + 1:064x}"
            self.users[vine_id] = {"pubkey": pubkey, "username": body["username"]}
            self.status[pubkey] = "active"
        self.last_pubkey = self.users[vine_id]["pubkey"]
        # a token per user ("ucan-token/<pubkey>"); the bare "ucan-token" some tests pass
        # stands for the user preloaded last
        return httpx.Response(
            200,
            json={
                "pubkey": self.users[vine_id]["pubkey"],
                "token": "ucan-token/" + self.users[vine_id]["pubkey"],
            },
        )

    def _acting(self, request: httpx.Request) -> str:
        """The pubkey of the user a request's UCAN is for."""
        auth = request.headers.get("Authorization") or ""
        if auth.startswith("Bearer ucan-token/"):
            return auth.removeprefix("Bearer ucan-token/")
        return self.last_pubkey

    def _profile(self, request: httpx.Request) -> httpx.Response:
        me = self._acting(request)
        name = json.loads(request.content)["username"].lower()
        for u in self.users.values():
            if u["username"].lower() == name and u["pubkey"] != me:
                return httpx.Response(409, json={"error": "Username is not available."})
        for u in self.users.values():
            if u["pubkey"] == me:
                u["username"] = name
        return httpx.Response(200, json={"success": True})

    def _get_profile(self, request: httpx.Request) -> httpx.Response:
        me = self._acting(request)
        u = next((u for u in self.users.values() if u["pubkey"] == me), None)
        name = u["username"] if u else None
        return httpx.Response(
            200, json={"username": name, "nip05": f"{name}@belfry.test" if name else None}
        )

    def _sessions(self, request: httpx.Request) -> httpx.Response:
        me = self._acting(request)
        rows = [
            {
                "application_name": b["app_name"],
                "redirect_origin": "",
                "bunker_pubkey": PrivateKey(b["bunker_secret"]).public_key_xonly.format().hex(),
                "client_pubkey": b.get("client_pubkey"),
                "created_at": "2026-11-05T00:00:00Z",
                "last_activity": None,
                "activity_count": 0,
            }
            for b in self.bunkers
            if b["pubkey"] == me and not b.get("revoked")
        ]
        return httpx.Response(200, json={"sessions": rows})

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        bp = json.loads(request.content)["bunker_pubkey"]
        for b in self.bunkers:
            if PrivateKey(b["bunker_secret"]).public_key_xonly.format().hex() == bp:
                b["revoked"] = True
        return httpx.Response(200, json={"success": True, "message": "revoked"})

    def _bunker(self, request: httpx.Request) -> httpx.Response:
        from coincurve import PrivateKey

        body = json.loads(request.content)
        # Keycast patch 0022: a connection bound to a client key; one key, one account.
        # ``binds = False`` stands for a signer built without it (the field is ignored).
        client = (body.get("client_pubkey") or "").lower() if self.binds else ""
        me = self._acting(request)
        if client and any(
            b.get("client_pubkey") == client and b["pubkey"] != me for b in self.bunkers
        ):
            return httpx.Response(
                409,
                json={
                    "error": "This client key is already linked to another account.",
                    "code": "client_key_taken",
                },
            )
        key = PrivateKey()
        bunker_pk = key.public_key_xonly.format().hex()
        secret = f"secret-{len(self.bunkers) + 1}"
        url = f"bunker://{bunker_pk}?relay=wss%3A%2F%2Fbunker.belfry.test&secret={secret}"
        self.bunkers.append(
            {
                "pubkey": me,
                "app_name": body["app_name"],
                "bunker_url": url,
                "secret": secret,
                "bunker_secret": key.secret,
                "client_pubkey": client or None,
            }
        )
        return httpx.Response(
            200,
            json={
                "bunker_url": url,
                "bunker_pubkey": bunker_pk,
                "app_name": body["app_name"],
                "origin": None,
                "created_at": "2026-11-05T00:00:00Z",
                **({"client_pubkey": client} if client else {}),
            },
        )

    def _nip05(self, request: httpx.Request) -> httpx.Response:
        name = (request.url.params.get("name") or "").lower()
        names = {u["username"].lower(): u["pubkey"] for u in self.users.values()}
        return httpx.Response(200, json={"names": {name: names[name]} if name in names else {}})

    def _sign(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        auth = request.headers.get("Authorization") or ""
        # one token for the last preloaded user, or "ucan-token/<pubkey>" for a user of
        # ``self.keys`` (tests with real keys: remote apps)
        acting = self.last_pubkey
        if auth.startswith("Bearer ucan-token/"):
            acting = auth.removeprefix("Bearer ucan-token/")
        elif auth != "Bearer ucan-token":
            return httpx.Response(401, json={"error": "unauthorized"})
        self.rpc.append((body["method"], acting))
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        secret = self.keys.get(acting)
        if body["method"] == "get_public_key":
            return httpx.Response(200, json={"result": acting})
        if body["method"] in ("nip44_encrypt", "nip44_decrypt", "nip04_encrypt", "nip04_decrypt"):
            if secret is None:
                return httpx.Response(200, json={"error": "no key for this user in the fake"})
            peer, text = body["params"]
            try:
                out = real_crypt(secret, body["method"], peer, text)
            except Exception as exc:  # noqa: BLE001 — what the signer answers
                return httpx.Response(200, json={"error": f"{body['method']}: {exc}"})
            return httpx.Response(200, json={"result": out})
        if body["method"] != "sign_event":
            return httpx.Response(400, json={"error": f"unsupported {body['method']}"})
        event = dict(body["params"][0])
        if "pubkey" not in event:
            return httpx.Response(400, json={"error": "event must include pubkey"})
        if event["pubkey"] in self.keys:  # a real key: a real signature
            from belfry_onboarding.nip46check import sign_event as real_sign

            event.pop("id", None)
            event.pop("sig", None)
            done = real_sign(self.keys[event["pubkey"]], event)
            self.signed.append(done)
            return httpx.Response(200, json={"result": done})
        event["id"] = f"{len(self.signed) + 1:064x}"
        event["sig"] = "fake-sig"
        self.signed.append(event)
        return httpx.Response(200, json={"result": event})

    def _set_status(self, request: httpx.Request, pubkey: str) -> httpx.Response:
        if pubkey not in self.status:
            return httpx.Response(404, json={"error": "user not found"})
        body = json.loads(request.content)
        if body["status"] in ("suspended", "banned") and not body.get("reason"):
            # Verified 2026-10-23 against Keycast fe9c6460.
            return httpx.Response(
                400, json={"error": "reason is required when status is suspended or banned"}
            )
        self.status[pubkey] = body["status"]
        self.status_calls.append((pubkey, body["status"]))
        return httpx.Response(200, json={"pubkey": pubkey, "status": body["status"]})


class FakeSigner(FakeKeycast):
    """Belfry's own signer (``services/signer``) as respx routes, for ``SignerClient``.

    It keeps the same books as :class:`FakeKeycast` (``users``, ``status``, ``bunkers``,
    ``signed``, ``keys``, ``rpc``, ``preloads``, ``preload_bodies``, ``status_calls``,
    ``down``, ``derived``), so a test written against one runs against the other. Accounts
    are named in the path: ``acct:<vine_id>`` tokens become ``/accounts/<vine_id>``; the
    Keycast fake's literal tokens still work (``ucan-token``: the account preloaded last,
    ``ucan-token/<pubkey>``: the account with that key)."""

    CREATED = 1793836800  # 2026-11-05T00:00:00Z, the Keycast fake's created_at

    def __init__(
        self,
        router: respx.MockRouter,
        base_url: str = "http://signer.test",
        keycast_url: str = "http://keycast.test",
    ):
        super().__init__(router, keycast_url)
        self.kind = "belfry"
        base = re.escape(base_url.rstrip("/"))
        acct = rf"{base}/accounts/(?P<acct>[^/]+)"
        # no named group here: tests swap this route's side effect for one that takes the
        # request alone, as they do with Keycast's preload-user
        self.preload_route = router.put(url__regex=rf"{base}/accounts/[^/]+$").mock(
            side_effect=self._s_put
        )
        router.get(url__regex=rf"{acct}$").mock(side_effect=self._s_get)
        router.put(url__regex=rf"{acct}/username$").mock(side_effect=self._s_username)
        router.post(url__regex=rf"{acct}/sign$").mock(side_effect=self._s_sign)
        router.post(url__regex=rf"{acct}/(?P<op>encrypt|decrypt)$").mock(side_effect=self._s_crypt)
        router.post(url__regex=rf"{acct}/connections$").mock(side_effect=self._s_connect)
        router.get(url__regex=rf"{acct}/connections$").mock(side_effect=self._s_connections)
        router.post(url__regex=rf"{acct}/connections/revoke$").mock(side_effect=self._s_revoke)
        router.get(url__regex=rf"{base}/names/(?P<name>[^/]+)$").mock(side_effect=self._s_name)
        router.post(url__regex=rf"{base}/pubkeys/(?P<pubkey>[^/]+)/status$").mock(
            side_effect=self._s_status
        )

    @staticmethod
    def _err(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": code, "message": code})

    def _user(self, acct: str) -> dict[str, Any] | None:
        """The account a path segment names: {"pubkey", "username"?}."""
        from urllib.parse import unquote

        name = unquote(acct)
        if name == "ucan-token":
            pubkey = self.last_pubkey
        elif name.startswith("ucan-token/"):
            pubkey = name.removeprefix("ucan-token/")
        else:
            return self.users.get(name)
        found = next((u for u in self.users.values() if u["pubkey"] == pubkey), None)
        return found or ({"pubkey": pubkey} if pubkey else None)

    def _account_json(self, vine_id: str, u: dict[str, Any]) -> dict[str, Any]:
        return {
            "vine_id": vine_id,
            "pubkey": u["pubkey"],
            "custody": "own" if u.get("own") else "derived",
            "key_version": u.get("key_version", 0),
            "status": "suspended" if self.status.get(u["pubkey"]) != "active" else "active",
            "username": u.get("username"),
        }

    def _s_put(self, request: httpx.Request) -> httpx.Response:
        from urllib.parse import unquote

        self.preloads += 1
        body = json.loads(request.content)
        vine_id = unquote(request.url.raw_path.decode().rsplit("/", 1)[-1])
        # the books show what Keycast's preload-user would have been sent
        self.preload_bodies.append(
            {
                "vine_id": vine_id,
                "username": body.get("username"),
                "key_version": body.get("key_version", 0),
                **({"pubkey": body["own_pubkey"]} if body.get("own_pubkey") else {}),
            }
        )
        own = body.get("own_pubkey")
        u = self.users.get(vine_id)
        if u is None:
            name = body.get("username")
            if name and any(
                (x.get("username") or "").lower() == name.lower() for x in self.users.values()
            ):
                return self._err(409, "username_taken")
            if own:
                pubkey = own
            elif self.derived:
                seed = f"{vine_id}/{body.get('key_version', 0)}".encode()
                pubkey = hashlib.sha256(seed).hexdigest()
            else:
                pubkey = f"{len(self.users) + 1:064x}"
            u = {"pubkey": pubkey, "username": name, "own": bool(own)}
            self.users[vine_id] = u
            self.status[pubkey] = "active"
        else:
            for want in (own, body.get("expect_pubkey")):
                if want and want != u["pubkey"]:
                    return self._err(409, "mismatch")
        self.last_pubkey = u["pubkey"]
        return httpx.Response(200, json=self._account_json(vine_id, u))

    def _s_get(self, request: httpx.Request, acct: str) -> httpx.Response:
        u = self._user(acct)
        if u is None:
            return self._err(404, "not_found")
        return httpx.Response(200, json=self._account_json(acct, u))

    def _s_username(self, request: httpx.Request, acct: str) -> httpx.Response:
        u = self._user(acct)
        if u is None:
            return self._err(404, "not_found")
        name = json.loads(request.content)["username"].lower()
        for other in self.users.values():
            if (other.get("username") or "").lower() == name and other["pubkey"] != u["pubkey"]:
                return self._err(409, "username_taken")
        u["username"] = name
        return httpx.Response(200, json=self._account_json(acct, u))

    def _s_name(self, request: httpx.Request, name: str) -> httpx.Response:
        from urllib.parse import unquote

        want = unquote(name).lower()
        for vine_id, u in self.users.items():
            if (u.get("username") or "").lower() == want:
                return httpx.Response(
                    200, json={"name": want, "pubkey": u["pubkey"], "vine_id": vine_id}
                )
        return self._err(404, "not_found")

    def _s_status(self, request: httpx.Request, pubkey: str) -> httpx.Response:
        if pubkey not in self.status:
            return self._err(404, "not_found")
        status = json.loads(request.content)["status"]
        if status not in ("active", "suspended"):
            return self._err(422, "invalid")
        self.status[pubkey] = status
        self.status_calls.append((pubkey, status))
        return httpx.Response(200, json={"updated": 1})

    def _s_sign(self, request: httpx.Request, acct: str) -> httpx.Response:
        u = self._user(acct)
        if u is None:
            return self._err(404, "not_found")
        acting = u["pubkey"]
        self.rpc.append(("sign_event", acting))
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        event = json.loads(request.content).get("event")
        if not isinstance(event, dict):
            return self._err(422, "invalid")
        event = dict(event)
        if event.get("pubkey") and event["pubkey"] != acting:
            return self._err(422, "invalid")
        event["pubkey"] = acting
        if acting in self.keys:  # a real key: a real signature
            from belfry_onboarding.nip46check import sign_event as real_sign

            event.pop("id", None)
            event.pop("sig", None)
            done = real_sign(self.keys[acting], event)
        else:
            done = {**event, "id": f"{len(self.signed) + 1:064x}", "sig": "fake-sig"}
        self.signed.append(done)
        return httpx.Response(200, json={"event": done})

    def _s_crypt(self, request: httpx.Request, acct: str, op: str) -> httpx.Response:
        u = self._user(acct)
        if u is None:
            return self._err(404, "not_found")
        body = json.loads(request.content)
        method = f"{body.get('scheme', 'nip44')}_{op}"
        self.rpc.append((method, u["pubkey"]))
        if self.down:
            return httpx.Response(503, json={"error": "down"})
        secret = self.keys.get(u["pubkey"])
        if secret is None:
            return self._err(422, "invalid")
        try:
            out = real_crypt(secret, method, body["peer"], body["text"])
        except Exception:  # noqa: BLE001 — what the signer answers
            return self._err(422, "invalid")
        return httpx.Response(200, json={"result": out})

    def _s_connect(self, request: httpx.Request, acct: str) -> httpx.Response:
        from coincurve import PrivateKey

        u = self._user(acct)
        if u is None:
            return self._err(404, "not_found")
        body = json.loads(request.content)
        client = (body.get("client_pubkey") or "").lower()
        if client and any(
            b.get("client_pubkey") == client and b["pubkey"] != u["pubkey"] for b in self.bunkers
        ):
            return self._err(409, "client_key_taken")
        key = PrivateKey()
        bunker_pk = key.public_key_xonly.format().hex()
        secret = f"secret-{len(self.bunkers) + 1}"
        url = f"bunker://{bunker_pk}?relay=wss%3A%2F%2Fbunker.belfry.test&secret={secret}"
        self.bunkers.append(
            {
                "pubkey": u["pubkey"],
                "app_name": body["app"],
                "bunker_url": url,
                "secret": secret,
                "bunker_secret": key.secret,
                "client_pubkey": client or None,
            }
        )
        return httpx.Response(
            200,
            json={
                "id": f"{len(self.bunkers):032x}",
                "bunker_url": url,
                "bunker_pubkey": bunker_pk,
                "pubkey": u["pubkey"],
                "app": body["app"],
                "created_at": self.CREATED,
                **({"client_pubkey": client} if client else {"expires_at": self.CREATED + 600}),
            },
        )

    def _s_connections(self, request: httpx.Request, acct: str) -> httpx.Response:
        u = self._user(acct)
        if u is None:
            return self._err(404, "not_found")
        rows = [
            {
                "id": f"{i + 1:032x}",
                "app": b["app_name"],
                "label": b["app_name"],
                "bunker_pubkey": PrivateKey(b["bunker_secret"]).public_key_xonly.format().hex(),
                "client_pubkey": b.get("client_pubkey"),
                "derive": [],
                "created_at": self.CREATED,
                "last_used_at": None,
                "uses": 0,
                "expires_at": None,
            }
            for i, b in enumerate(self.bunkers)
            if b["pubkey"] == u["pubkey"] and not b.get("revoked")
        ]
        return httpx.Response(200, json=rows)

    def _s_revoke(self, request: httpx.Request, acct: str) -> httpx.Response:
        u = self._user(acct)
        if u is None:
            return self._err(404, "not_found")
        bp = json.loads(request.content).get("bunker_pubkey")
        if not bp:
            return self._err(422, "invalid")
        n = 0
        for b in self.bunkers:
            if (
                b["pubkey"] == u["pubkey"]
                and not b.get("revoked")
                and PrivateKey(b["bunker_secret"]).public_key_xonly.format().hex() == bp
            ):
                b["revoked"] = True
                n += 1
        return httpx.Response(200, json={"revoked": n})


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


def flash_of(response) -> str:
    """The one-shot message a redirect carries in its flash cookie (msg and err joined), decoded
    without the key: tests only read it. Messages never travel in the URL."""
    import jwt

    from belfry_onboarding import flash

    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name.strip() == flash.COOKIE:
            token = rest.split(";", 1)[0].strip('"')
            claims = jwt.decode(token, options={"verify_signature": False})
            return f"{claims.get('msg', '')} {claims.get('err', '')}".strip()
    return ""


class FakeSellerOdoo:
    """The seller's Odoo over JSON-2 (salesodoo.py): partners, invoices posted with a number and a
    structured communication, payments, credit notes. ``down``: every call fails as a network
    error; ``refuse``: Odoo answers 422."""

    TAXES = {10: (21.0, True), 11: (21.0, False)}  # id → (rate, price includes it)

    def __init__(self) -> None:
        self.records: dict[str, dict[int, dict[str, Any]]] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.refuse = False
        self.batch_fails = False  # reading several invoices at once fails
        self.unreadable: set[int] = set()  # invoices that can't be read
        self.no_pdf = False
        self.refuse_partner_fields: set[str] = set()  # res.partner fields Odoo refuses (422)
        self.refuse_create: set[str] = set()  # models Odoo refuses to create a record of (403)
        self.add("res.company", name="XL Collective")
        self.add("res.country", code="BE", name="Belgium")
        self.add("res.currency", name="EUR")
        self.add("product.product", display_name="Festival ticket", sale_ok=True)

    def add(self, model: str, **vals: Any) -> int:
        table = self.records.setdefault(model, {})
        rid = max(table, default=0) + 1
        table[rid] = {"id": rid, **vals}
        return rid

    def _fail(self, model: str, method: str) -> None:
        self.calls.append((model, method))
        if self.down:
            raise httpx.ConnectError("odoo down")
        if self.refuse:
            raise OdooError(f"{model}.{method}: refused", {"status": 422})

    @staticmethod
    def _ok(rec: dict[str, Any], domain: list[Any]) -> bool:
        for field, op, value in domain:
            v = rec.get(field, False)
            if isinstance(v, list) and v and isinstance(v[0], int):
                v = v[0]
            if op == "=" and v != value:
                return False
            if op == "!=" and v == value:
                return False
            if op == "=ilike" and str(v).lower() != str(value).lower():
                return False
            if op == "in" and v not in value:
                return False
        return True

    def search_read(self, model, domain, fields, limit=None):
        self._fail(model, "search_read")
        rows = [dict(r) for r in self.records.get(model, {}).values() if self._ok(r, domain)]
        return rows[:limit] if limit else rows

    def read_one(self, model, rec_id, fields):
        self._fail(model, "read")
        r = self.records.get(model, {}).get(rec_id)
        return dict(r) if r else None

    def create(self, model, vals, context=None):
        self._fail(model, "create")
        vals = dict(vals)
        if model in self.refuse_create:
            raise OdooError(f"{model}.create: not allowed", {"status": 403})
        if model == "product.product":
            vals.setdefault("display_name", vals.get("name"))
        if model == "res.partner" and self.refuse_partner_fields & set(vals):
            raise OdooError("res.partner.create: the VAT number is not valid", {"status": 422})
        if model == "account.move":
            total = untaxed = 0.0
            for _, _, line in vals.get("invoice_line_ids", []):
                price = line["price_unit"] * line["quantity"] * (1 - line["discount"] / 100)
                taxes = line["tax_ids"][0][2]
                rate, included = self.TAXES[taxes[0]] if taxes else (0.0, True)
                if included:
                    net = round(price / (1 + rate / 100), 2)
                    untaxed, total = untaxed + net, total + round(price, 2)
                else:
                    gross = round(price * (1 + rate / 100), 2)
                    untaxed, total = untaxed + round(price, 2), total + gross
            vals.update(
                state="draft",
                name="/",
                payment_state="not_paid",
                payment_reference=False,
                amount_untaxed=round(untaxed, 2),
                amount_tax=round(total - untaxed, 2),
                amount_total=round(total, 2),
                journal_id=[1, "Customer Invoices"],
                move_type=vals.get("move_type", "out_invoice"),
            )
        if model in (
            "account.payment.register",
            "account.move.reversal",
            "account.move.send.wizard",
        ):
            vals["context"] = context
        return self.add(model, **vals)

    def call(self, model, method, ids=None, context=None, **params):
        self._fail(model, method)
        moves = self.records.setdefault("account.move", {})
        if model == "account.move" and method == "read":
            if self.batch_fails and len(ids) > 1:
                raise httpx.ReadTimeout("too slow")
            if set(ids) & self.unreadable:
                raise OdooError("account.move.read: gone", {"status": 500})
            return [dict(moves[i]) for i in ids if i in moves]
        if model == "account.move" and method == "action_post":
            for i in ids:
                m = moves[i]
                if m["state"] == "draft":
                    n = sum(1 for x in moves.values() if x["name"] != "/") + 1
                    prefix = "RINV" if m.get("move_type") == "out_refund" else "INV"
                    m.update(state="posted", name=f"{prefix}/2026/{n:04d}")
                    if prefix == "INV":
                        m["payment_reference"] = f"+++000/0000/{n:05d}+++"
            return True
        if model == "account.move" and method == "button_cancel":
            for i in ids:
                moves[i]["state"] = "cancel"
            return True
        if model == "account.move" and method == "get_portal_url":
            return f"/my/invoices/{ids[0]}?access_token=t"
        if model == "account.payment.register" and method == "action_create_payments":
            wiz = self.records[model][ids[0]]
            moves[wiz["context"]["active_id"]]["payment_state"] = "paid"
            moves[wiz["context"]["active_id"]]["paid_journal"] = wiz["journal_id"]
            return True
        if model == "account.move.reversal" and method == "refund_moves":
            wiz = self.records[model][ids[0]]
            orig = moves[wiz["context"]["active_id"]]
            rid = self.add(
                "account.move",
                move_type="out_refund",
                reversed_entry_id=orig["id"],
                state="draft",
                name="/",
                payment_state="not_paid",
                amount_total=orig["amount_total"],
            )
            orig["payment_state"] = "reversed"
            return {"res_id": rid}
        if method == "action_send_and_print":
            wiz = self.records[model][ids[0]]
            if not self.no_pdf:
                import base64

                att = self.add("ir.attachment", datas=base64.b64encode(b"%PDF-1.4 fake").decode())
                moves[wiz["context"]["active_id"]]["invoice_pdf_report_id"] = [att, "inv.pdf"]
            return True
        raise AssertionError(f"unexpected {model}.{method}")
