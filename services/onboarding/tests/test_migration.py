"""Moving a belfry from Keycast to its own signer (migration.py, docs/own-signer.md §6): the
plan, the import and the verification, against a Keycast database made of rows (the columns of
§6's two queries) and a stand-in for the signer that checks what the real one checks: a sealed
key must open to the public key named, a session's bunker key must be the one Keycast's formula
gives, one client key belongs to one account, a batch is all or nothing."""

from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from coincurve import PrivateKey
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from belfry_onboarding import migration, stewards
from belfry_onboarding.clients.authentik import AuthentikClient

from .conftest import AUTHENTIK_URL
from .fakes import FakeAuthentik

NOW = 1_800_000_000


def pub(secret: bytes) -> str:
    return PrivateKey(secret).public_key_xonly.format().hex()


def secret_of(label: str) -> bytes:
    return hashlib.sha256(f"test key {label}".encode()).digest()


def seal(secret: bytes) -> str:
    """The stand-in's "sealed key": hex the fake signer opens. (Keycast's is AES-GCM under the
    master key; only the signer opens that, and nothing in Python ever does.)"""
    return (b"sealed:" + secret).hex()


def bunker_secret(account_secret: bytes, secret_hash: str) -> bytes:
    """Keycast's formula (core/src/bunker_key.rs): HKDF-SHA256, no salt, the account's key,
    info "keycast-bunker-nip46-v1-" + the stored secret hash."""
    info = b"keycast-bunker-nip46-v1-" + secret_hash.encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(account_secret)


class FakeSignerAdmin:
    """What ``migration.SignerAdmin`` talks to, with the signer's rules."""

    def __init__(self, derivable: dict[str, str]):
        self.derivable = derivable  # vine_id -> the key the custody seed gives
        self.accounts: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, bytes] = {}
        self.conns: list[dict[str, Any]] = []
        self.calls: list[tuple[str, int]] = []
        self.break_bunker_of: str = ""

    def health(self):
        return {"status": "ok", "seed": True, "bunker": {"state": "off"}}

    def account(self, vine_id):
        a = self.accounts.get(vine_id)
        return dict(a, vine_id=vine_id) if a else None

    def import_accounts(self, rows):
        self.calls.append(("import", len(rows)))
        out = []
        for row in rows:
            vine, want = row["vine_id"], row["pubkey"]
            raw = bytes.fromhex(row["sealed"])
            if not raw.startswith(b"sealed:") or pub(raw[7:]) != want:
                out.append(
                    {
                        "vine_id": vine,
                        "result": "error",
                        "error": "mismatch",
                        "message": "the sealed key does not open to that public key",
                    }
                )
                continue
            have = self.accounts.get(vine)
            if have:
                if have["pubkey"] != want:
                    out.append(
                        {
                            "vine_id": vine,
                            "result": "error",
                            "error": "mismatch",
                            "message": "the account has another key",
                        }
                    )
                else:
                    out.append({"vine_id": vine, "result": "exists"})
                continue
            how = "derived" if self.derivable.get(vine) == want else "imported"
            self.accounts[vine] = {
                "pubkey": want,
                "custody": how,
                "key_version": 0,
                "status": "active",
                "username": None,
            }
            self.secrets[vine] = raw[7:]
            out.append({"vine_id": vine, "result": how})
        return out

    def put_own(self, vine_id, pubkey):
        have = self.accounts.get(vine_id)
        if have and have["pubkey"] != pubkey:
            return "mismatch: another key"
        self.accounts.setdefault(
            vine_id,
            {
                "pubkey": pubkey,
                "custody": "own",
                "key_version": 0,
                "status": "active",
                "username": None,
            },
        )
        return ""

    def set_username(self, vine_id, username):
        if any(a["username"] == username for v, a in self.accounts.items() if v != vine_id):
            return "username_taken: "
        self.accounts[vine_id]["username"] = username
        return ""

    def set_status(self, vine_id, status):
        self.accounts[vine_id]["status"] = status
        return ""

    def import_connections(self, vine_id, rows):
        self.calls.append(("connections", len(rows)))
        acct = self.accounts.get(vine_id)
        if acct is None:
            return {"error": "not_found: "}
        if acct["custody"] == "own":
            return {"error": "own_key: "}
        new, counts = [], {"imported": 0, "existing": 0, "revoked": 0}
        for i, row in enumerate(rows):
            derived = pub(bunker_secret(self.secrets[vine_id], row["keycast_secret_hash"]))
            if derived != row["bunker_pubkey"] or row["bunker_pubkey"] == self.break_bunker_of:
                return {"error": f"invalid: connections[{i}]: not the bunker key of this account"}
            client = row.get("client_pubkey")
            revoked = row.get("revoked_at")
            if not revoked and not client:
                return {"error": f"invalid: connections[{i}]: a live row needs a client key"}
            if not 1 <= len(row["app"]) <= 64:
                return {"error": f"invalid: connections[{i}]: app"}
            same = [c for c in self.conns if c["bunker_pubkey"] == row["bunker_pubkey"]]
            if same:
                if same[0]["vine_id"] != vine_id:
                    return {"error": "mismatch: another account's bunker key"}
                counts["existing"] += 1
                continue
            if client and any(
                c["client_pubkey"] == client and c["vine_id"] != vine_id
                for c in [*self.conns, *new]
            ):
                return {"error": "client_key_taken: "}
            new.append(
                {
                    "vine_id": vine_id,
                    "bunker_pubkey": row["bunker_pubkey"],
                    "client_pubkey": client,
                    "revoked_at": revoked,
                    "app": row["app"],
                    "scope": row.get("scope", "own"),
                }
            )
            counts["revoked" if revoked else "imported"] += 1
        self.conns.extend(new)  # all or nothing
        return counts

    def connections(self, vine_id):
        return [dict(c) for c in self.conns if c["vine_id"] == vine_id and not c["revoked_at"]]


class World:
    """A Keycast database as rows, the register next to it, and a signer to move into."""

    def __init__(self, svc, odoo):
        self.svc, self.odoo = svc, odoo
        self.accounts: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.secrets: dict[str, bytes] = {}
        self.derivable: dict[str, str] = {}
        self.signer = FakeSignerAdmin(self.derivable)
        self.n = 0

    def person(
        self,
        label,
        *,
        tier="steward",
        status="active",
        own=False,
        vine=True,
        derived=True,
        username=None,
        register=True,
        state="active",
        **more,
    ):
        secret = secret_of(label)
        key = pub(secret)
        partner = self.odoo.add(
            "res.partner",
            name=label,
            email=f"{label.lower()}@example.org",
            belfry_key_custody="self" if own else "belfry",
        )
        vine_id = f"odoo-partner-{partner}" if vine else ""
        if register and vine:
            self.odoo.add(
                "belfry.pass",
                partner_id=partner,
                keycast_pubkey=key,
                state=state,
                tier=tier,
                **more,
            )
        self.accounts.append(
            {
                "pubkey": key,
                "vine_id": vine_id or None,
                "username": label.lower() if username is None else username,
                "status": status,
                "sealed": None if own else seal(secret),
            }
        )
        self.secrets[key] = secret
        if derived and vine and not own:
            self.derivable[vine_id] = key
        return SimpleNamespace(pubkey=key, vine_id=vine_id, partner=partner, label=label)

    def session(
        self,
        who,
        kind="served",
        *,
        client=None,
        app="Chat · Firefox on Linux",
        origin="",
        used=None,
    ):
        self.n += 1
        secret_hash = f"$2b$12$hash{self.n:04d}"
        bunker = pub(bunker_secret(self.secrets[who.pubkey], secret_hash))
        client = client or pub(secret_of(f"client {self.n}"))
        row = {
            "user_pubkey": who.pubkey,
            "bunker_public_key": bunker,
            "secret_hash": secret_hash,
            "client_id": app,
            "redirect_origin": origin,
            "client_pubkey": None,
            "connected_client_pubkey": client,
            "verified_client_pubkey": client,
            "created_at": NOW - 5000 + self.n,
            "last_used_at": used if used is not None else NOW - 100 + self.n,
            "uses": 3,
            "revoked_at": None,
            "expires_at": None,
        }
        if kind == "revoked":
            row["revoked_at"] = NOW - 50
        elif kind == "expired":
            row["expires_at"] = NOW - 10
        elif kind == "never":
            row.update(connected_client_pubkey=None, verified_client_pubkey=None)
        elif kind == "unverified":
            row["verified_client_pubkey"] = None
        elif kind == "bound":  # patch 0022: all three client columns equal from the start
            row["client_pubkey"] = client
        self.sessions.append(row)
        return SimpleNamespace(bunker=bunker, client=client, row=row)

    def doc(self):
        return {
            "accounts": [dict(a) for a in self.accounts],
            "sessions": [dict(s) for s in self.sessions],
            "now": NOW,
        }

    def plan(self, **kw):
        return migration.plan(self.svc, self.doc(), **kw)


@pytest.fixture
def world(services, odoo):
    services.settings.nip05_domain = "belfry.test"
    return World(services, odoo)


@pytest.fixture
def town(world):
    """Every kind of account and session §6 names."""
    w = world
    w.ana = w.person("Ana")  # a key the custody seed gives
    w.old = w.person("Old", derived=False)  # a random key from before the seed
    w.guest = w.person("Gus", tier="guest", username="guest-7")
    w.agent = w.person("Bot", tier="agent")
    w.own = w.person("Owen", own=True)
    w.gone = w.person("Sue", status="suspended", state="revoked")
    w.banned = w.person("Ban", status="banned", state="revoked")
    w.live = w.session(w.ana)
    w.bound = w.session(w.ana, "bound", app="Belfry for Android")
    w.expired = w.session(w.ana, "expired")
    w.never = w.session(w.ana, "never")
    w.revoked = w.session(w.old, "revoked")
    w.app = w.session(w.old, app="notes.example.org", origin="https://notes.example.org")
    w.guest_live = w.session(w.guest, app="Chat (guest)")
    w.sue_live = w.session(w.gone)  # a suspended account's session is carried
    return w


# ── the plan ────────────────────────────────────────────────────────────────────────────────
def test_the_plan_counts_every_kind(town):
    p = town.plan()
    c = migration.counts(p)
    assert p.problems == [] and p.lost == []
    assert c["accounts"] == 7 and c["excluded"] == 0
    assert c["by_kind"] == {"agent": 1, "guest": 1, "member": 4, "own_key": 1}
    assert c["with_username"] == 7 and c["suspended"] == 2 and c["register_members"] == 7
    assert c["sessions_by_state"] == {
        "expired": 1,
        "never_connected": 1,
        "revoked": 1,
        "served": 5,
    }
    assert c["live_by_scope"] == {"own": 4, "outside": 1}
    by = {s.bunker: s for s in p.sessions}
    assert by[town.app.bunker].scope == "outside" and by[town.live.bunker].scope == "own"
    assert by[town.expired.bunker].revoked_at == NOW - 10  # an expired row goes in revoked
    assert by[town.bound.bunker].client == town.bound.client
    assert all(isinstance(line, str) for line in migration.summary(c))


def test_what_must_stop_a_migration_is_listed_not_skipped(town):
    w = town
    stray = w.person("Stray", vine=False)  # registered on Keycast's own pages: no vine id
    w.person("Odd", username="Name With Spaces")
    w.accounts[0]["status"] = "weird"
    w.sessions.append({**w.live.row, "user_pubkey": "ab" * 32, "bunker_public_key": "cd" * 32})
    lost = w.person("Missing")  # the register expects them; Keycast does not have them
    w.accounts.pop()
    wrong = w.person("Wrong")  # the register has another key for them
    for p in w.odoo.tables["belfry.pass"].values():
        if p["partner_id"] == wrong.partner:
            p["keycast_pubkey"] = "5" * 64
    plan = w.plan()
    kinds = sorted(p["problem"] for p in plan.problems)
    assert kinds == [
        "member_not_in_keycast",
        "no_vine_id",
        "register_has_another_key",
        "session_without_account",
        "unknown_status",
        "username_not_carried",
    ]
    text = json.dumps(plan.problems)
    assert stray.pubkey not in text and wrong.pubkey not in text  # shortened only
    assert stray.pubkey[:8] in text and lost.vine_id in text
    with pytest.raises(migration.MigrationError, match="nothing was imported"):
        migration.run_import(plan, w.signer)
    assert w.signer.accounts == {}
    # named on purpose, the account without a vine id is left out and counted
    plan = w.plan(exclude=[stray.pubkey])
    assert "no_vine_id" not in [p["problem"] for p in plan.problems]
    assert migration.counts(plan)["excluded"] == 1


def test_a_register_that_does_not_answer_stops_the_plan(town, monkeypatch):
    monkeypatch.setattr(
        town.odoo, "search_read", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(migration.MigrationError, match="register"):
        town.plan()


# ── the import ──────────────────────────────────────────────────────────────────────────────
def test_the_import_carries_everything_and_is_safe_to_repeat(town):
    w = town
    res = migration.run_import(w.plan(), w.signer)
    assert res["errors"] == []
    # the key from before the seed comes in as it is; the others are the seed's
    assert res["accounts"] == {"derived": 5, "imported": 1, "exists": 0, "own_key": 1}
    assert res["usernames"] == 7 and res["suspended"] == 2  # banned is kept as suspended
    assert res["sessions"] == {"imported": 5, "existing": 0, "revoked": 2}
    s = w.signer
    assert s.accounts[w.old.vine_id]["custody"] == "imported"
    assert s.accounts[w.own.vine_id] == {
        "pubkey": w.own.pubkey,
        "custody": "own",
        "key_version": 0,
        "status": "active",
        "username": "owen",
    }
    assert s.accounts[w.banned.vine_id]["status"] == "suspended"
    live = {c["bunker_pubkey"]: c for c in s.connections(w.ana.vine_id)}
    assert set(live) == {w.live.bunker, w.bound.bunker}
    assert live[w.live.bunker]["client_pubkey"] == w.live.client
    assert all(c["bunker_pubkey"] != w.never.bunker for c in s.conns)  # nothing to carry
    assert [c["scope"] for c in s.connections(w.old.vine_id)] == ["outside"]
    # again, and again after Keycast moved on: only what is new is written
    again = migration.run_import(w.plan(), s)
    assert again["errors"] == [] and again["usernames"] == 0 and again["suspended"] == 0
    assert again["accounts"]["exists"] == 6 and again["accounts"]["derived"] == 0
    assert again["sessions"] == {"imported": 0, "existing": 7, "revoked": 0}
    new = w.person("New")
    late = w.session(new)
    w.sessions[0]["revoked_at"] = NOW - 1  # (the signer keeps the row it has: verify says so)
    third = migration.run_import(w.plan(), s)
    assert third["accounts"]["derived"] == 1 and third["sessions"]["imported"] == 1
    assert late.bunker in {c["bunker_pubkey"] for c in s.connections(new.vine_id)}


def test_a_key_that_does_not_open_to_its_public_key_is_refused(town):
    w = town
    w.accounts[0]["sealed"] = seal(secret_of("somebody else"))
    res = migration.run_import(w.plan(), w.signer)
    assert [e["step"] for e in res["errors"]] == ["key"]
    assert res["errors"][0]["vine_id"] == w.ana.vine_id
    assert w.ana.vine_id not in w.signer.accounts
    assert not w.signer.connections(w.ana.vine_id)  # and none of its sessions
    report = migration.verify(w.plan(), w.signer, w.svc)
    assert not report["ok"]
    assert "account_not_imported" in [m["mismatch"] for m in report["mismatches"]]


def test_a_bunker_key_the_formula_does_not_give_stops_that_accounts_sessions(town):
    w = town
    w.signer.break_bunker_of = w.bound.bunker
    res = migration.run_import(w.plan(), w.signer)
    assert [(e["step"], e["vine_id"]) for e in res["errors"]] == [("sessions", w.ana.vine_id)]
    assert "connections[" in res["errors"][0]["error"]
    assert w.signer.connections(w.ana.vine_id) == []  # all or nothing
    assert w.signer.connections(w.guest.vine_id)  # the others went in
    report = migration.verify(w.plan(), w.signer, w.svc)
    assert sorted({m["mismatch"] for m in report["mismatches"]}) == ["session_missing"]


def test_big_tables_go_in_batches_under_the_body_limit(world):
    w = world
    people = [w.person(f"P{i}") for i in range(230)]
    for _ in range(950):
        w.session(people[0])
    res = migration.run_import(w.plan(), w.signer)
    assert res["errors"] == [] and res["sessions"]["imported"] == 950
    sizes = [n for kind, n in w.signer.calls if kind == "import"]
    assert sizes == [100, 100, 30]
    assert [n for kind, n in w.signer.calls if kind == "connections"] == [400, 400, 150]
    rows = [{"x": "y" * 1000} for _ in range(600)]
    assert max(len(json.dumps(b)) for b in migration.batches(rows, 10_000)) < 2 * 256_000


# ── one client key, one identity ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("first", ["suspended", "active"])
def test_a_client_key_two_accounts_share_stays_with_the_one_in_use(world, first):
    """Eight guests made by one phone before patch 0023, seven of them closed: whichever row
    Keycast lists first, the active account keeps the device's key and its session."""
    w = world
    shared = pub(secret_of("the phone"))
    order = ["Closed", "Marcel"] if first == "suspended" else ["Marcel", "Closed"]
    made = {}
    for label in order:
        closed = label == "Closed"
        made[label] = w.person(
            label,
            tier="guest",
            status="suspended" if closed else "active",
            state="revoked" if closed else "active",
        )
        w.session(made[label], client=shared, app="Chat (guest)", used=NOW - (5 if closed else 500))
    p = w.plan()
    assert p.problems == [] and p.lost == []  # nobody in use is signed out
    res = migration.run_import(p, w.signer)
    assert res["errors"] == [] and res["sessions"] == {"imported": 1, "existing": 0, "revoked": 1}
    marcel = w.signer.connections(made["Marcel"].vine_id)
    assert len(marcel) == 1 and marcel[0]["client_pubkey"] == shared
    assert w.signer.connections(made["Closed"].vine_id) == []
    stale = [c for c in w.signer.conns if c["vine_id"] == made["Closed"].vine_id]
    assert stale[0]["revoked_at"] and stale[0]["client_pubkey"] is None
    assert migration.verify(p, w.signer, w.svc)["ok"]


def test_two_accounts_in_use_with_one_client_key_is_a_loss_to_accept(world):
    w = world
    shared = pub(secret_of("a shared laptop"))
    a, b = w.person("Aa"), w.person("Bb")
    w.session(a, client=shared, used=NOW - 500)
    w.session(b, client=shared, used=NOW - 5)  # used last: keeps the key
    p = w.plan()
    assert [(x["vine_id"], x["kept_by"]) for x in p.lost] == [(a.vine_id, b.vine_id)]
    assert shared not in json.dumps(p.lost)
    migration.run_import(p, w.signer)
    assert len(w.signer.connections(b.vine_id)) == 1 and not w.signer.connections(a.vine_id)
    report = migration.verify(p, w.signer, w.svc)
    assert not report["ok"] and report["mismatches"][0]["mismatch"] == "session_lost"
    assert migration.verify(p, w.signer, w.svc, accept_lost=1)["ok"]
    assert not migration.verify(p, w.signer, w.svc, accept_lost=2)["ok"]


# ── verify ──────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def moved(town):
    assert migration.run_import(town.plan(), town.signer)["errors"] == []
    return town


def kinds(report):
    return sorted({m["mismatch"] for m in report["mismatches"]})


def test_a_clean_move_verifies(moved):
    w = moved
    report = migration.verify(w.plan(), w.signer, w.svc)
    assert report["ok"] and report["mismatches"] == []
    assert report["checked"] == {
        "accounts": 7,
        "keys_equal": 7,
        "usernames": 7,
        "live_sessions": 5,
    }
    assert report["not_verifiable"]["revoked_sessions"] == 2
    assert report["digest"] == migration.source_digest(w.doc())
    text = json.dumps(report)
    for a in w.accounts:
        assert a["pubkey"] not in text and (a["sealed"] or "no-seal") not in text
    assert "$2b$" not in text and "@example.org" not in text


@pytest.mark.parametrize(
    "damage, expected",
    [
        (
            lambda w: w.signer.accounts.pop(w.guest.vine_id),
            ["account_not_imported", "member_not_in_signer"],
        ),
        (
            lambda w: w.signer.accounts[w.ana.vine_id].update(pubkey="9" * 64),
            ["member_key", "signer_key"],
        ),
        (lambda w: w.signer.accounts[w.ana.vine_id].update(username="anna"), ["username"]),
        (lambda w: w.signer.accounts[w.ana.vine_id].update(username=None), ["username"]),
        (lambda w: w.signer.accounts[w.gone.vine_id].update(status="active"), ["status"]),
        (lambda w: w.signer.accounts[w.own.vine_id].update(custody="derived"), ["custody"]),
        (
            lambda w: w.signer.conns.remove(
                next(c for c in w.signer.conns if c["bunker_pubkey"] == w.live.bunker)
            ),
            ["session_missing"],
        ),
        (
            lambda w: next(c for c in w.signer.conns if c["bunker_pubkey"] == w.live.bunker).update(
                client_pubkey="8" * 64
            ),
            ["session_client_key"],
        ),
        (
            lambda w: next(
                c for c in w.signer.conns if c["bunker_pubkey"] == w.revoked.bunker
            ).update(revoked_at=None),
            ["session_unexpected"],
        ),
        (
            lambda w: w.signer.conns.append(
                {
                    "vine_id": w.ana.vine_id,
                    "bunker_pubkey": "7" * 64,
                    "client_pubkey": "6" * 64,
                    "revoked_at": None,
                    "app": "Made meanwhile",
                    "scope": "own",
                }
            ),
            ["session_unexpected"],
        ),
    ],
)
def test_verify_fails_on_every_kind_of_difference(moved, damage, expected):
    damage(moved)
    report = migration.verify(moved.plan(), moved.signer, moved.svc)
    assert not report["ok"] and kinds(report) == expected


def test_verify_fails_when_keycast_or_the_register_moved_on(moved):
    w = moved
    w.session(w.guest)  # a session opened after the import
    assert kinds(migration.verify(w.plan(), w.signer, w.svc)) == ["session_missing"]
    w.sessions.pop()
    w.person("Late")  # an account made after the import
    report = migration.verify(w.plan(), w.signer, w.svc)
    assert kinds(report) == ["account_not_imported", "member_not_in_signer"]
    w.accounts.pop()
    assert kinds(migration.verify(w.plan(), w.signer, w.svc)) == [
        "member_not_in_signer",
        "problem",
    ]  # the register still expects Late: Keycast lost them
    for p in list(w.odoo.tables["belfry.pass"].values()):
        if p["keycast_pubkey"] == w.accounts[0]["pubkey"]:
            p["keycast_pubkey"] = "4" * 64  # the register says another key than Keycast
    assert "register_key" in kinds(migration.verify(w.plan(), w.signer, w.svc))


def test_after_the_switch_only_the_keys_are_compared(moved):
    """People sign in and out, join and rename on the signer: Keycast's rows are the past."""
    w = moved
    s = w.signer
    s.conns.append(
        {
            "vine_id": w.ana.vine_id,
            "bunker_pubkey": "7" * 64,
            "client_pubkey": "6" * 64,
            "revoked_at": None,
            "app": "Chat",
            "scope": "own",
        }
    )  # a new sign-in
    s.conns.remove(next(c for c in s.conns if c["bunker_pubkey"] == w.live.bunker))  # signed out
    s.accounts[w.ana.vine_id]["username"] = "ana-maria"
    s.accounts[w.guest.vine_id]["status"] = "suspended"
    late = w.person("Late")  # joined after the switch: never in Keycast
    w.accounts.pop()
    s.accounts[late.vine_id] = {
        "pubkey": late.pubkey,
        "custody": "derived",
        "key_version": 0,
        "status": "active",
        "username": "late",
    }
    assert not migration.verify(w.plan(), s, w.svc)["ok"]
    report = migration.verify(w.plan(), s, w.svc, keys_only=True)
    assert report["ok"] and report["keys_only"] is True and report["checked"]["keys_equal"] == 7
    # a key is a key, for ever
    s.accounts[w.old.vine_id]["pubkey"] = "9" * 64
    assert kinds(migration.verify(w.plan(), s, w.svc, keys_only=True)) == [
        "member_key",
        "signer_key",
    ]
    s.accounts[w.old.vine_id]["pubkey"] = w.old.pubkey
    del s.accounts[late.vine_id]  # and nobody of the register may be missing
    assert kinds(migration.verify(w.plan(), s, w.svc, keys_only=True)) == ["member_not_in_signer"]


def test_the_digest_follows_what_is_carried_and_not_the_counters(town):
    w = town
    before = migration.source_digest(w.doc())
    w.sessions[0].update(last_used_at=NOW + 99, uses=9999)
    assert migration.source_digest(w.doc()) == before
    w.sessions[0]["revoked_at"] = NOW
    changed = migration.source_digest(w.doc())
    assert changed != before
    w.accounts[0]["username"] = "renamed"
    assert migration.source_digest(w.doc()) != changed


# ── the admins ──────────────────────────────────────────────────────────────────────────────
def test_admins_are_copied_and_compared_with_masked_addresses(moved, router):
    w = moved
    ak = FakeAuthentik(router, AUTHENTIK_URL)
    for pk, email in ((7, "ana@example.org"), (8, "old@example.org"), (9, "ghost@example.org")):
        ak.users.append({"pk": pk, "username": email, "email": email, "attributes": {}})
    ak.groups["belfry-stewards"] = {"pk": "g", "members": {7, 8, 9}}
    client = AuthentikClient(AUTHENTIK_URL, "ak-token")
    report = migration.verify(w.plan(), w.signer, w.svc, client)
    assert kinds(report) == ["steward_not_marked", "steward_without_person"]
    got = migration.import_stewards(w.svc, client)
    assert got == {
        "marked": ["a•••@example.org", "o•••@example.org"],
        "already": [],
        "missing": ["g•••@example.org"],
    }
    report = migration.verify(w.plan(), w.signer, w.svc, client)
    assert kinds(report) == ["steward_without_person"]  # an admin with nobody to carry
    assert "ghost@example.org" not in json.dumps(report)
    report = migration.verify(w.plan(), w.signer, w.svc, client, accept_missing_stewards=1)
    assert report["ok"] and report["stewards"] == {
        "authentik": 3,
        "register": 2,
        "without_person": 1,
    }
    w.odoo.write("res.partner", [w.guest.partner], {stewards.FIELD: True})
    report = migration.verify(w.plan(), w.signer, w.svc, client, accept_missing_stewards=1)
    assert kinds(report) == ["steward_only_in_register"]


# ── the signer's API and the command line ───────────────────────────────────────────────────
def test_the_api_client_calls_the_signers_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body, request.headers.get("authorization")))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "seed": True})
        if path == "/import":
            return httpx.Response(200, json={"results": [{"vine_id": "v", "result": "derived"}]})
        if path == "/import/connections":
            if body["vine_id"] == "bad":
                return httpx.Response(
                    422, json={"error": "invalid", "message": "connections[0]: x"}
                )
            return httpx.Response(200, json={"imported": 1, "existing": 0, "revoked": 0})
        if path.endswith("/connections"):
            return httpx.Response(200, json=[{"bunker_pubkey": "b"}])
        if path == "/accounts/none":
            return httpx.Response(404, json={"error": "not_found"})
        if path.endswith("/username"):
            return httpx.Response(409, json={"error": "username_taken", "message": ""})
        return httpx.Response(200, json={"vine_id": "v", "pubkey": "p"})

    api = migration.SignerAdmin(
        "http://signer:8200/", "t" * 32, httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert api.health()["seed"] is True
    assert api.account("odoo-partner-1") == {"vine_id": "v", "pubkey": "p"}
    assert api.account("none") is None
    assert api.import_accounts([{"vine_id": "v", "pubkey": "p", "sealed": "00"}])[0]["result"]
    assert api.put_own("odoo-partner-2", "p") == ""
    assert api.set_username("odoo-partner-2", "lina").startswith("username_taken")
    assert api.set_status("odoo-partner-2", "suspended") == ""
    assert api.import_connections("v", [{}]) == {"imported": 1, "existing": 0, "revoked": 0}
    assert api.import_connections("bad", [{}]) == {"error": "invalid: connections[0]: x"}
    assert api.connections("odoo-partner-1") == [{"bunker_pubkey": "b"}]
    assert [(m, p) for m, p, _, _ in seen] == [
        ("GET", "/health"),
        ("GET", "/accounts/odoo-partner-1"),
        ("GET", "/accounts/none"),
        ("POST", "/import"),
        ("PUT", "/accounts/odoo-partner-2"),
        ("PUT", "/accounts/odoo-partner-2/username"),
        ("POST", "/accounts/odoo-partner-2/status"),
        ("POST", "/import/connections"),
        ("POST", "/import/connections"),
        ("GET", "/accounts/odoo-partner-1/connections"),
    ]
    assert seen[4][2] == {"own_pubkey": "p"} and seen[6][2] == {"status": "suspended"}
    assert all(auth == "Bearer " + "t" * 32 for _, path, _, auth in seen if path != "/health")
    with pytest.raises(migration.MigrationError, match="token"):
        migration.SignerAdmin("http://signer:8200", "")


def run_cli(w, phase, monkeypatch, **more):
    args = SimpleNamespace(
        phase=phase,
        exclude=more.get("exclude"),
        no_stewards=True,
        accept_lost_sessions=more.get("accept_lost", 0),
        accept_missing_stewards=0,
    )
    monkeypatch.setattr(migration, "SignerAdmin", lambda url, token: w.signer)
    out, err = io.StringIO(), io.StringIO()
    code = migration.cli(args, w.svc, w.svc.settings, io.StringIO(json.dumps(w.doc())), out, err)
    return code, out.getvalue(), err.getvalue()


def test_the_command_prints_counts_and_never_a_key_or_an_address(town, monkeypatch):
    w = town
    code, out, err = run_cli(w, "plan", monkeypatch)
    assert code == 0 and "accounts in Keycast: 7" in err and "problems: 0" in err
    assert w.signer.accounts == {}  # the plan writes nothing
    code, out, err = run_cli(w, "verify", monkeypatch)
    assert code == 1 and "MISMATCH" in err and "account_not_imported" in err
    code, out, err = run_cli(w, "import", monkeypatch)
    assert code == 0, err
    assert "keys: " in err and "1 imported as they are" in err and "1 members' own" in err
    code, out, err = run_cli(w, "verify", monkeypatch)
    assert code == 0 and "verify: clean" in err
    report = json.loads(out)
    assert report["ok"] is True and report["checked"]["accounts"] == 7
    everything = out + err
    for a in w.accounts:
        assert a["pubkey"] not in everything
        assert (a["sealed"] or "no-seal") not in everything
    for s in w.sessions:
        assert s["secret_hash"] not in everything
        assert s["bunker_public_key"] not in everything
    assert "@example.org" not in everything
    # an account without a vine id: plan and import stop; named, it is left out
    stray = w.person("Stray", vine=False)
    code, out, err = run_cli(w, "plan", monkeypatch)
    assert code == 1 and "no_vine_id" in err and stray.pubkey not in err
    code, out, err = run_cli(w, "import", monkeypatch)
    assert code == 1 and "nothing was imported" in err
    code, out, err = run_cli(w, "verify", monkeypatch, exclude=[stray.pubkey])
    assert code == 0, err
    # rows that are not rows
    args = SimpleNamespace(
        phase="plan",
        exclude=None,
        no_stewards=True,
        accept_lost_sessions=0,
        accept_missing_stewards=0,
    )
    assert (
        migration.cli(
            args, w.svc, w.svc.settings, io.StringIO("not json"), io.StringIO(), io.StringIO()
        )
        == 2
    )


# ── the script ──────────────────────────────────────────────────────────────────────────────
SCRIPTS = __import__("pathlib").Path(__file__).resolve().parents[3] / "infra" / "scripts"


def functions(text: str) -> dict[str, str]:
    """name -> body of every top-level ``name() {`` … ``}`` of a bash script (one-line
    functions are left out)."""
    import re

    out: dict[str, str] = {}
    for m in re.finditer(r"^([a-z_]+)\(\) \{(?:[^\n]*[^}\n])?\n(.*?)^\}", text, re.S | re.M):
        out[m.group(1)] = m.group(2)
    return out


def test_the_script_parses_and_only_cleanup_deletes():
    import subprocess

    script = SCRIPTS / "migrate-to-own-signer.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    fns = functions(script.read_text())
    assert {
        "cmd_check",
        "cmd_backup",
        "cmd_import",
        "cmd_verify",
        "cmd_switch",
        "cmd_rollback",
        "cmd_cleanup",
        "rows",
        "py",
    } <= set(fns)
    destructive = ("DROP ", "volume rm", "rm -sf", "down -v", "TRUNCATE", "DELETE FROM")
    for name, body in fns.items():
        if name == "cmd_cleanup":
            continue
        for word in destructive:
            assert word not in body, f"{name} must not delete ({word})"
    # Keycast's database is only ever read outside cleanup: one query, in a read-only transaction
    assert "READ ONLY" in fns["rows"] and "encrypted_secret_key" in fns["rows"]
    for name, body in fns.items():
        if name not in ("rows", "cmd_cleanup", "cmd_check"):
            assert "psql" not in body, name
    assert "select count(*) from users" in fns["cmd_check"]
    # cleanup: a typed sentence at a terminal, the two reports, seven days, a fresh backup
    c = fns["cmd_cleanup"]
    for needed in (
        "-t 0",
        "switch_report",
        "after_report",
        "CLEANUP_DAYS",
        "fresh_backup",
        "delete keycast and authentik of",
    ):
        assert needed in c, needed
    assert c.index("read -r -p") < c.index("DROP SCHEMA") and c.index("fresh_backup") < c.index(
        "DROP SCHEMA"
    )
    assert "YES" not in c  # --yes never answers for it
    # the switch decides on a verify made with the writers stopped, and backs up first
    s = fns["cmd_switch"]
    assert s.index("fresh_backup") < s.index("stop onboarding keycast") < s.index("py import")
    assert s.index("py verify") < s.index("BELFRY_SIGNER=belfry BELFRY_SIGNER_MIGRATED=1")
    # rows never land in a file
    assert "rows |" in fns["py"] and "rows >" not in script.read_text()


def test_apply_never_cleans_up_and_only_the_migration_starts_the_import_signer():
    belfry = (SCRIPTS / "belfry").read_text()
    assert "cleanup" not in belfry and "migrate-to-own-signer.sh cleanup" not in belfry
    fns = functions(belfry)
    assert "BELFRY_SIGNER_IMPORT" in fns["signer_files"]
    for name, body in fns.items():
        if name != "signer_files":
            assert "BELFRY_SIGNER_IMPORT" not in body, name
    # the switch of the signer asks the data, not one file
    assert "ever_had_people" in fns["cmd_config"]
    assert 'first-admin.json" &&' not in fns["cmd_config"]
