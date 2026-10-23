"""In-memory stand-ins for Odoo, Authentik and Keycast. They support only what the code calls."""

from __future__ import annotations

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
        vine_id = body["vine_id"]
        if vine_id not in self.users:
            if any(u["username"].lower() == body["username"].lower() for u in self.users.values()):
                return httpx.Response(
                    409, json={"error": f"User with username {body['username']} already exists"}
                )
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
            "grants": {
                "urn:ietf:params:oauth:grant-type:pre-authorized_code": {
                    "pre-authorized_code": f"code-{n:03d}"
                }
            },
        }
        from urllib.parse import quote

        return httpx.Response(
            200,
            json={
                "offerId": f"offer-{n:03d}",
                "profileId": body["profileId"],
                "authMethod": "PRE_AUTHORIZED",
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

    def succeed(self, sid: str, claims: dict[str, Any]) -> None:
        self.sessions[sid].update(
            {
                "status": "SUCCESSFUL",
                "attempted": True,
                "presented_credentials": {
                    "village_pass": [{"type": "vc-sd_jwt", "credentialData": claims}]
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

    def __init__(self, require_auth: bool = False, fail: bool = False):
        self.require_auth = require_auth
        self.fail = fail
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
