"""In-memory stand-ins for Odoo, Authentik and Keycast. They support only what the code calls."""

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
    "belfry.report": {"reporter_pass_id", "target_pass_id"},
    "product.product": set(),
    "res.partner": set(),
}
UNIQUE: dict[str, list[str]] = {
    "belfry.pass": ["invoice_id", "stripe_payment_intent", "claim_token"],
    "belfry.stripe.event": ["event_id"],
    "res.partner": ["belfry_member_number"],
}


class FakeOdoo(OdooApi):
    def __init__(self, legacy_module: bool = False):
        self.tables: dict[str, dict[int, dict[str, Any]]] = {m: {} for m in M2O}
        self.legacy_module = legacy_module  # simulate a module without the public RPC methods
        self.calls: list[tuple[str, str]] = []
        self._seq = 0

    # -- seeding ------------------------------------------------------------------------------

    invites_per_participant = 3

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
        used = "signin_used_at" if kind == "signin" else "welcome_used_at"
        if method == "belfry_welcome_info":
            return {
                "result": "ok",
                "state": p.get("state"),
                "first_name": "Alice",
                "payment": "paid" if p.get("state") == "active" else "unpaid",
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
                    partner["belfry_member_number"] = f"OSV-{self._seq:04d}"
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


class FakeKeycast:
    """respx routes for preload-user (idempotent on vine_id) and the status endpoint."""

    def __init__(self, router: respx.MockRouter, base_url: str = "http://keycast.test"):
        self.users: dict[str, dict[str, Any]] = {}  # vine_id -> user
        self.status: dict[str, str] = {}  # pubkey -> status
        self.preloads = 0
        self.preload_bodies: list[dict[str, Any]] = []
        # derived=True: pubkeys depend on (vine_id, key_version) like Keycast patch 0014
        self.derived = False
        self.status_calls: list[tuple[str, str]] = []
        self.signed: list[dict[str, Any]] = []
        base = base_url.rstrip("/")
        self.preload_route = router.post(f"{base}/api/admin/preload-user").mock(
            side_effect=self._preload
        )
        router.post(f"{base}/api/nostr").mock(side_effect=self._sign)
        router.put(
            url__regex=rf"{re.escape(base)}/api/admin/users/(?P<pubkey>[0-9a-f]+)/status"
        ).mock(side_effect=self._set_status)

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
        return httpx.Response(
            200, json={"pubkey": self.users[vine_id]["pubkey"], "token": "ucan-token"}
        )

    def _sign(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.headers.get("Authorization") != "Bearer ucan-token":
            return httpx.Response(401, json={"error": "unauthorized"})
        if body["method"] != "sign_event":
            return httpx.Response(400, json={"error": f"unsupported {body['method']}"})
        event = dict(body["params"][0])
        if "pubkey" not in event:
            return httpx.Response(400, json={"error": "event must include pubkey"})
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
