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
SESSIONS0 = {
    "imported": 0,
    "existing": 0,
    "revoked": 0,
    "updated": 0,
    "released": 0,
    "revoked_gone": 0,
}


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
        self.closed: bool | None = False  # the "no new accounts" switch (None: none)
        self.skipped: dict[str, str] = {}  # bunker -> vine_id (GET /import/skipped)
        self.archived: list[tuple[str, str, Any]] = []  # held keys replaced by an own key
        self.window = False  # the migration's window (POST /settings {migration})

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

    def import_connections(self, vine_id, rows, sync=False):
        """The signer's rules: all or nothing; a bunker key the formula does not give skips
        that row only (and lists it); with SYNC, honoured only inside the migration's window,
        a row that is there is brought to what the import says, except that an outside row
        never becomes own."""
        self.calls.append(("connections", len(rows)))
        acct = self.accounts.get(vine_id)
        if acct is None:
            return {"error": "not_found: "}
        if acct["custody"] == "own":
            return {"error": "own_key: "}
        if sync and not self.window:
            return {"error": "invalid: sync is for the migration's window only"}
        new, changes, skipped = [], [], []
        counts = {"imported": 0, "existing": 0, "revoked": 0, "updated": 0, "released": 0}
        conns = [*self.conns]
        for i, row in enumerate(rows):
            client = row.get("client_pubkey")
            revoked = row.get("revoked_at")
            if not revoked and not client:
                return {"error": f"invalid: connections[{i}]: a live row needs a client key"}
            if not 1 <= len(row["app"]) <= 64:
                return {"error": f"invalid: connections[{i}]: app"}
            derived = pub(bunker_secret(self.secrets[vine_id], row["keycast_secret_hash"]))
            if derived != row["bunker_pubkey"] or row["bunker_pubkey"] == self.break_bunker_of:
                skipped.append(i)
                continue
            same = [c for c in conns if c["bunker_pubkey"] == row["bunker_pubkey"]]
            want = {
                "client_pubkey": client,
                "revoked_at": revoked,
                "app": row["app"],
                "scope": row.get("scope", "own"),
                "decrypt": bool(row.get("decrypt")),
                "origin": (row.get("origin") or "").strip(),
                "kind": "nip46",
            }
            if same:
                c = same[0]
                if c["vine_id"] != vine_id:
                    return {"error": "mismatch: another account's bunker key"}
                if c.get("scope") == "outside" and want["scope"] != "outside":
                    want["scope"], want["decrypt"] = "outside", c.get("decrypt", False)
                now = {k: c.get(k) for k in want}
                if not sync or (
                    now["client_pubkey"] == client
                    and bool(now["revoked_at"]) == bool(revoked)
                    and now["app"] == want["app"]
                    and now["scope"] == want["scope"]
                    and now["decrypt"] == want["decrypt"]
                    and now["origin"] == want["origin"]
                ):
                    counts["existing"] += 1
                    continue
                if (
                    client
                    and client != now["client_pubkey"]
                    and any(
                        x["client_pubkey"] == client and x["vine_id"] != vine_id
                        for x in [*conns, *new]
                    )
                ):
                    return {"error": "client_key_taken: "}
                if revoked and now["revoked_at"]:
                    want["revoked_at"] = now["revoked_at"]
                changes.append((c, want))
                counts["updated"] += 1
                if now["client_pubkey"] and not client:
                    counts["released"] += 1
                continue
            if client and any(
                c["client_pubkey"] == client and c["vine_id"] != vine_id for c in [*conns, *new]
            ):
                return {"error": "client_key_taken: "}
            new.append({"vine_id": vine_id, "bunker_pubkey": row["bunker_pubkey"], **want})
            counts["revoked" if revoked else "imported"] += 1
        for c, want in changes:  # all or nothing
            c.update(want)
        self.conns.extend(new)
        for row in rows:
            self.skipped.pop(row["bunker_pubkey"], None)
        for i in skipped:
            self.skipped[rows[i]["bunker_pubkey"]] = vine_id
        return {**counts, "skipped": skipped}

    def move_own(self, vine_id, pubkey):
        have = self.accounts[vine_id]
        if have["custody"] != "own" and self.window:
            self.archived.append((vine_id, have["pubkey"], self.secrets.get(vine_id)))
        have.update(pubkey=pubkey, custody="own")
        for c in self.conns:
            if c["vine_id"] == vine_id and not c["revoked_at"]:
                c["revoked_at"] = NOW
        return dict(have, vine_id=vine_id, archived_keys=len(self.archived))

    def revoke(self, vine_id, bunker_pubkey):
        for c in self.conns:
            if c["vine_id"] == vine_id and c["bunker_pubkey"] == bunker_pubkey:
                c["revoked_at"] = c["revoked_at"] or NOW
        return ""

    def import_skipped(self):
        return [{"vine_id": v, "bunker_pubkey": b, "at": NOW} for b, v in self.skipped.items()]

    def connections(self, vine_id, all=False):
        return [
            dict(c) for c in self.conns if c["vine_id"] == vine_id and (all or not c["revoked_at"])
        ]

    def list_accounts(self):
        return [dict(a, vine_id=v) for v, a in sorted(self.accounts.items())]

    def close_accounts(self, closed):
        if self.closed is None:
            return None
        self.closed = closed
        return closed

    def set_window(self, on):
        self.window = bool(on)
        return self.window

    def delete_archived_keys(self):
        if self.window:
            raise migration.MigrationError("the signer kept its archived keys: invalid: window")
        n = len(self.archived)
        self.archived.clear()
        return n


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

    def make_agent(self, label, *, keycast=False):
        """An agent as the register makes one (register._agent_pass): its OWN key on the pass
        (keycast_pubkey = own_pubkey), and no account in Keycast. KEYCAST: Keycast has one all
        the same, with another (derived) key: what an old token request for the agent's
        partner left behind (a "phantom")."""
        key = pub(secret_of(label))
        partner = self.odoo.add("res.partner", name=label, belfry_key_custody="belfry")
        owner = self.odoo.add("res.partner", name=f"owner of {label}")
        self.odoo.add(
            "belfry.pass",
            partner_id=partner,
            tier="agent",
            agent_owner=owner,
            keycast_pubkey=key,
            own_pubkey=key,
            state="active",
        )
        vine_id = f"odoo-partner-{partner}"
        phantom = None
        if keycast:
            secret = secret_of(f"phantom {label}")
            phantom = pub(secret)
            self.accounts.append(
                {
                    "pubkey": phantom,
                    "vine_id": vine_id,
                    "username": None,
                    "status": "active",
                    "sealed": seal(secret),
                }
            )
            self.secrets[phantom] = secret
        return SimpleNamespace(pubkey=key, vine_id=vine_id, partner=partner, phantom=phantom)

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
def world(services, odoo, monkeypatch):
    services.settings.nip05_domain = "belfry.test"
    w = World(services, odoo)
    # the outside apps an admin approved (apps.state reads them from the community relay)
    w.approved = {"https://notes.example.org"}
    monkeypatch.setattr(migration, "outside_origins", lambda svc: w.approved)
    return w


@pytest.fixture
def town(world):
    """Every kind of account and session §6 names."""
    w = world
    w.ana = w.person("Ana")  # a key the custody seed gives
    w.old = w.person("Old", derived=False)  # a random key from before the seed
    w.guest = w.person("Gus", tier="guest", username="guest-7")
    w.agent = w.make_agent("Bot")  # its own key, no Keycast account (register._agent_pass)
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
    assert c["accounts"] == 6 and c["excluded"] == 0
    assert c["by_kind"] == {"guest": 1, "member": 4, "own_key": 1}
    assert c["with_username"] == 6 and c["suspended"] == 2 and c["register_members"] == 7
    # the agent: its own key in the register and no Keycast account, counted, not a problem
    assert c["own_key_no_account"] == 1 and p.no_account == {town.agent.vine_id: town.agent.pubkey}
    assert c["scope_guessed"] == 0
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
    assert res["accounts"] == {"derived": 4, "imported": 1, "exists": 0, "own_key": 1}
    assert res["usernames"] == 6 and res["suspended"] == 2  # banned is kept as suspended
    assert res["sessions"] == dict(SESSIONS0, imported=5, revoked=2)
    assert w.agent.vine_id not in w.signer.accounts  # nothing to carry for the agent
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
    assert again["accounts"]["exists"] == 5 and again["accounts"]["derived"] == 0
    assert again["sessions"] == dict(SESSIONS0, existing=7)
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


def test_a_bunker_key_the_formula_does_not_give_skips_that_row_only(town):
    w = town
    w.signer.break_bunker_of = w.bound.bunker
    res = migration.run_import(w.plan(), w.signer)
    assert res["errors"] == []
    assert res["skipped"] == [
        {"vine_id": w.ana.vine_id, "bunker": w.bound.bunker[:8] + "…", "app": "Belfry for Android"}
    ]
    # the account and its other rows went in
    assert {c["bunker_pubkey"] for c in w.signer.connections(w.ana.vine_id)} == {w.live.bunker}
    report = migration.verify(w.plan(), w.signer, w.svc)
    # listed, and blocking until accepted by its exact number
    assert kinds(report) == ["session_skipped"] and report["skipped"] == res["skipped"]
    assert migration.verify(w.plan(), w.signer, w.svc, accept_skipped=1)["ok"]
    assert kinds(migration.verify(w.plan(), w.signer, w.svc, accept_skipped=2)) == [
        "session_skipped"
    ]
    # a later import that carries it takes it off the list
    w.signer.break_bunker_of = ""
    migration.run_import(w.plan(), w.signer)
    assert w.signer.skipped == {} and migration.verify(w.plan(), w.signer, w.svc)["ok"]
    assert kinds(migration.verify(w.plan(), w.signer, w.svc, accept_skipped=1)) == [
        "accept_skipped_does_not_match"
    ]


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
    assert res["errors"] == [] and res["sessions"] == dict(SESSIONS0, imported=1, revoked=1)
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
        "accounts": 6,
        "keys_equal": 6,
        "usernames": 6,
        "live_sessions": 5,
        "revoked_sessions": 2,
        "own_key_no_account": 1,
        "kept_outside": 0,
        "signer_accounts": 6,
    }
    # the revoked rows are read back from the signer (GET …/connections?all=true)
    assert report["not_verifiable"] == {
        "revoked_sessions": 0,
        "accounts_list": False,
        "skipped_list": False,
    }
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
            ["account_count", "account_not_imported", "member_not_in_signer"],
        ),
        (
            lambda w: w.signer.accounts[w.ana.vine_id].update(pubkey="9" * 64),
            ["member_key", "signer_key"],
        ),
        (lambda w: w.signer.accounts[w.ana.vine_id].update(username="anna"), ["username"]),
        (lambda w: w.signer.accounts[w.ana.vine_id].update(username=None), ["username"]),
        (lambda w: w.signer.accounts[w.gone.vine_id].update(status="active"), ["status"]),
        (
            lambda w: w.signer.accounts[w.own.vine_id].update(custody="derived"),
            ["custody", "register_custody"],
        ),
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
            ["revoked_session_live", "session_unexpected"],
        ),
        (
            lambda w: w.signer.conns.remove(
                next(c for c in w.signer.conns if c["bunker_pubkey"] == w.revoked.bunker)
            ),
            ["revoked_session_missing"],
        ),
        (
            lambda w: next(
                c for c in w.signer.conns if c["bunker_pubkey"] == w.revoked.bunker
            ).update(client_pubkey="5" * 64),
            ["session_client_key"],
        ),
        (
            # an account nobody imported (made on the signer between import and switch)
            lambda w: w.signer.accounts.update(
                {
                    "odoo-partner-999": {
                        "pubkey": "3" * 64,
                        "custody": "derived",
                        "key_version": 0,
                        "status": "active",
                        "username": None,
                    }
                }
            ),
            ["account_unexpected"],
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
    assert kinds(report) == ["account_count", "account_not_imported", "member_not_in_signer"]
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
    assert report["ok"] and report["keys_only"] is True and report["checked"]["keys_equal"] == 6
    assert report["checked"]["signer_accounts"] == 7  # Late is no surprise after the switch
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
            if request.url.params.get("all") == "true":
                return httpx.Response(200, json=[{"bunker_pubkey": "b", "revoked_at": 5}])
            return httpx.Response(200, json=[{"bunker_pubkey": "b"}])
        if path == "/accounts":
            if request.url.params.get("after") == "v1":
                return httpx.Response(200, json={"accounts": [{"vine_id": "v2"}], "next": None})
            return httpx.Response(200, json={"accounts": [{"vine_id": "v1"}], "next": "v1"})
        if path == "/settings":
            return httpx.Response(200, json=body)
        if path == "/archived-keys":
            return httpx.Response(200, json={"deleted": 3})
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
    assert api.connections("odoo-partner-1", all=True) == [{"bunker_pubkey": "b", "revoked_at": 5}]
    assert [a["vine_id"] for a in api.list_accounts()] == ["v1", "v2"]
    assert api.close_accounts(True) is True and api.close_accounts(False) is False
    assert api.set_window(True) is True and seen[-1][2] == {"migration": True}
    assert api.delete_archived_keys() == 3 and seen[-1][:2] == ("DELETE", "/archived-keys")
    api.import_connections("v", [{}], sync=True)
    assert seen[-1][2] == {"vine_id": "v", "connections": [{}], "sync": True}
    assert seen[8][2] == {"vine_id": "bad", "connections": [{}]}, "no sync unless asked"
    del seen[-3:]
    assert [(m, p) for m, p, _, _ in seen][:10] == [
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
    assert [(m, p) for m, p, _, _ in seen][10:] == [
        ("GET", "/accounts/odoo-partner-1/connections"),
        ("GET", "/accounts"),
        ("GET", "/accounts"),
        ("POST", "/settings"),
        ("POST", "/settings"),
    ]
    assert seen[-2][2] == {"no_new_accounts": True}
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
    monkeypatch.setattr(w.svc.settings, "signer", "keycast")  # import runs before the switch
    code, out, err = run_cli(w, "plan", monkeypatch)
    assert code == 0 and "accounts in Keycast: 6" in err and "problems: 0" in err
    assert w.signer.accounts == {}  # the plan writes nothing
    code, out, err = run_cli(w, "verify", monkeypatch)
    assert code == 1 and "MISMATCH" in err and "account_not_imported" in err
    code, out, err = run_cli(w, "import", monkeypatch)
    assert code == 0, err
    assert "keys: " in err and "1 imported as they are" in err and "1 members' own" in err
    # the import closes the making of accounts until the switch opens it
    assert w.signer.closed is True and "closed until the switch" in err
    code, out, err = run_cli(w, "verify", monkeypatch)
    assert code == 0 and "verify: clean" in err and "2 revoked ones" in err
    assert w.signer.closed is True, "a verify changes nothing"
    code, out, err = run_cli(w, "open", monkeypatch)
    assert code == 0 and w.signer.closed is False and json.loads(out) == {"no_new_accounts": False}
    assert w.signer.window is False, "open ends the migration's window too"
    code, out, err = run_cli(w, "verify", monkeypatch)
    report = json.loads(out)
    assert report["ok"] is True and report["checked"]["accounts"] == 6
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
        if name not in ("rows", "cmd_cleanup", "cmd_check", "box_id", "keycast_carry_back"):
            assert "psql" not in body, name
    # the box's fingerprint reads; the rollback writes only revocations and suspensions
    assert "select system_identifier" in fns["box_id"]
    carry = fns["keycast_carry_back"]
    assert "UPDATE oauth_authorizations SET revoked_at" in carry
    assert "UPDATE users SET status" in carry and "ON_ERROR_STOP=1" in carry
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
    # the switch holds the apply lock from before anything stops to the end, and the apply it
    # runs knows the lock is held
    assert s.index("take_apply_lock") < s.index("stop onboarding keycast")
    assert "BELFRY_APPLY_LOCKED=1" in fns["take_apply_lock"]
    assert "flock -w" in fns["take_apply_lock"]
    # switch, rollback and cleanup refuse to run outside tmux/screen (unless --no-tmux)
    for name in ("cmd_switch", "cmd_rollback", "cmd_cleanup"):
        assert "need_tmux" in fns[name] and "need_this_box" in fns[name], name
    assert "take_apply_lock" in fns["cmd_rollback"] and "take_apply_lock" in fns["cmd_cleanup"]
    # rollback writes the signer's sign-outs and suspensions into Keycast before it starts
    r = fns["cmd_rollback"]
    assert r.index("keycast_carry_back") < r.index("BELFRY_SIGNER=keycast")
    # cleanup stops at the first failed deletion, and asks for a backup of the signer's keys
    assert "|| die" in c[c.index("DROP SCHEMA") :][:300]
    assert "backup_holds_signer" in c and "report_of_this_box" in c
    # the backup's time comes from the backup file, never from the environment
    text = script.read_text()
    assert "BELFRY_BACKUP_STAMP" not in text and "last-success" not in text
    assert "+ 300" in fns["backup_age"]


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


def test_a_signer_that_cannot_list_says_so_instead_of_passing(moved):
    """An older signer: no GET /accounts, and its connection list has no revoked rows. The
    report names what it could not read; nothing is taken as checked."""
    w = moved

    class Older:
        def __init__(self, inner):
            self.inner = inner

        def account(self, vine_id):
            return self.inner.account(vine_id)

        def connections(self, vine_id, all=False):
            rows = self.inner.connections(vine_id)  # live rows only, whatever is asked
            return [{k: v for k, v in r.items() if k != "revoked_at"} for r in rows]

    report = migration.verify(w.plan(), Older(w.signer), w.svc)
    assert report["ok"], report["mismatches"]
    assert report["not_verifiable"] == {
        "revoked_sessions": 2,
        "accounts_list": True,
        "skipped_list": True,
    }
    assert report["checked"]["revoked_sessions"] == 0 and "signer_accounts" not in report["checked"]


def test_the_import_carries_keycasts_origin():
    s = migration.Session(
        user="a" * 64,
        bunker="b" * 64,
        secret_hash="$2b$x",
        app="Chat",
        origin="https://chat.belfry.test",
        client="c" * 64,
        state="served",
        created_at=1,
        last_used_at=None,
        uses=0,
        revoked_at=None,
    )
    assert migration.connection_row(s)["origin"] == "https://chat.belfry.test"
    s.origin = ""
    assert "origin" not in migration.connection_row(s)


# ── agents: their own key, no Keycast account ──────────────────────────────────────────────
def test_agents_with_their_own_key_are_counted_not_carried_and_checked(town):
    w = town
    phantom = w.make_agent("Old bot", keycast=True)  # Keycast minted a key for its partner once
    p = w.plan()
    assert p.problems == []
    assert set(p.no_account) == {w.agent.vine_id, phantom.vine_id}
    assert [a.kind for a in p.accounts if a.vine_id == phantom.vine_id] == ["phantom"]
    assert migration.counts(p)["by_kind"]["phantom"] == 1
    res = migration.run_import(p, w.signer)
    assert res["errors"] == []
    assert w.agent.vine_id not in w.signer.accounts and phantom.vine_id not in w.signer.accounts
    report = migration.verify(w.plan(), w.signer, w.svc)
    assert report["ok"], report["mismatches"]
    assert report["checked"]["own_key_no_account"] == 2
    # a keyless account with the agent's own key is consistent too
    w.signer.accounts[w.agent.vine_id] = {
        "pubkey": w.agent.pubkey,
        "custody": "own",
        "key_version": 0,
        "status": "active",
        "username": None,
    }
    assert migration.verify(w.plan(), w.signer, w.svc)["ok"]
    # a key held for an agent is never right (the phantom's, or any other)
    w.signer.accounts[phantom.vine_id] = {
        "pubkey": phantom.phantom,
        "custody": "derived",
        "key_version": 0,
        "status": "active",
        "username": None,
    }
    assert kinds(migration.verify(w.plan(), w.signer, w.svc)) == ["own_key_account"]
    # also after the switch
    assert kinds(migration.verify(w.plan(), w.signer, w.svc, keys_only=True)) == ["own_key_account"]
    # a phantom somebody signs in on stops the plan: it must be understood
    w.signer.accounts.pop(phantom.vine_id)
    w.secrets[phantom.phantom] = secret_of(f"phantom {'Old bot'}")
    w.session(SimpleNamespace(pubkey=phantom.phantom))
    assert [x["problem"] for x in w.plan().problems] == ["phantom_with_sessions"]


def test_the_signer_is_never_asked_for_an_agents_account(services, odoo):
    from belfry_onboarding import community, signerguard

    key = "ab" * 32
    partner = odoo.add("res.partner", name="Bot", belfry_key_custody="belfry")
    odoo.add(
        "belfry.pass",
        partner_id=partner,
        tier="agent",
        keycast_pubkey=key,
        own_pubkey=key,
        state="active",
    )
    assert signerguard.own_key_person(services, partner)
    asked = []
    services.keycast.preload_user = lambda *a, **k: asked.append(a) or {}
    assert community.member_token(services, partner) is None and asked == []
    assert [m["own"] for m in signerguard.members(services) if m["partner"] == partner] == [True]


# ── import is a sync: run again, it brings the signer to Keycast's state ───────────────────
def test_running_import_again_follows_keycast_between_import_and_switch(world):
    w = world
    w.signer.closed = True  # the import closed new accounts: the migration's window
    a, b, c = w.person("Aa"), w.person("Bb"), w.person("Cc", derived=False)
    signed_out = w.session(a)
    deleted = w.session(a)
    shared = pub(secret_of("a shared tablet"))
    a_tab = w.session(a, client=shared, used=NOW - 500)
    b_tab = w.session(b, client=shared, used=NOW - 900)  # a uses it more lately: a keeps it
    w.approved.add("https://late.example.org")
    scoped = w.session(b, app="late.example.org", origin="https://late.example.org")
    w.approved.discard("https://late.example.org")  # (not approved at the first import)
    c_live = w.session(c)
    p = w.plan()
    assert len(p.lost) == 1
    first = migration.run_import(p, w.signer)
    assert first["errors"] == []
    # (the outside app not approved yet: its scope is guessed, and accepted by number)
    assert migration.verify(w.plan(), w.signer, w.svc, accept_lost=1, accept_guessed=1)["ok"]
    # since the first import, on Keycast: a sign-out, a row deleted, b used the tablet last,
    # the outside app approved, and c took their own key
    signed_out.row["revoked_at"] = NOW - 5
    w.sessions.remove(deleted.row)
    b_tab.row["last_used_at"] = NOW - 1
    w.approved.add("https://late.example.org")
    new_own = pub(secret_of("Cc's own key"))
    for acct in w.accounts:
        if acct["pubkey"] == c.pubkey:
            acct.update(pubkey=new_own, sealed=None)
    w.sessions.remove(c_live.row)  # Keycast deletes the sessions of a key taken over
    for row in w.odoo.tables["belfry.pass"].values():
        if row["partner_id"] == c.partner:
            row["keycast_pubkey"] = new_own
    w.odoo.write("res.partner", [c.partner], {"belfry_key_custody": "self"})
    p = w.plan()
    assert p.problems == [] and [x["kept_by"] for x in p.lost] == [b.vine_id]
    second = migration.run_import(p, w.signer)
    assert second["errors"] == [], second["errors"]
    assert second["sessions"]["released"] == 1 and second["sessions"]["revoked_gone"] == 1
    assert second["custody_moved"] == [
        {
            "vine_id": c.vine_id,
            "from": "imported",
            "from_key": c.pubkey[:8] + "…",
            "to_key": new_own[:8] + "…",
            "archived_keys": 1,
        }
    ]
    assert w.signer.archived[0][1] == c.pubkey  # the held key is kept, not dropped
    live_a = {x["bunker_pubkey"] for x in w.signer.connections(a.vine_id)}
    assert live_a == set()  # signed out, deleted, and its tablet row went to b
    live_b = {x["bunker_pubkey"]: x for x in w.signer.connections(b.vine_id)}
    assert set(live_b) == {b_tab.bunker, scoped.bunker}
    assert live_b[b_tab.bunker]["client_pubkey"] == shared
    assert live_b[scoped.bunker]["scope"] == "outside"
    report = migration.verify(w.plan(), w.signer, w.svc, accept_lost=1)
    assert report["ok"], report["mismatches"]
    # and once more: nothing left to do
    third = migration.run_import(w.plan(), w.signer)
    assert third["sessions"]["updated"] == 0 and third["sessions"]["revoked_gone"] == 0
    assert third["custody_moved"] == []
    del a_tab


def test_a_session_signed_out_in_keycast_is_revoked_in_the_signer_by_the_next_import(world):
    """The finding: an import that left a revoked session live made the switch abort."""
    w = world
    a = w.person("Aa")
    live = w.session(a)
    migration.run_import(w.plan(), w.signer)
    assert w.signer.window is True, "the import opens the migration's window"
    live.row["revoked_at"] = NOW - 1
    assert "session_unexpected" in kinds(migration.verify(w.plan(), w.signer, w.svc))
    # outside the window (closed by the switch) the signer refuses a sync: nothing changes
    w.signer.window = False
    assert "error" in w.signer.import_connections(
        a.vine_id, [migration.connection_row(s) for s in w.plan().sessions], True
    )
    # "no new accounts" alone is not the window: the next import opens it again and syncs
    w.signer.closed = True
    migration.run_import(w.plan(), w.signer)
    assert migration.verify(w.plan(), w.signer, w.svc)["ok"]


# ── custody against the register, scope, unverified, empty Keycast ─────────────────────────
def test_custody_must_agree_with_the_register(town):
    w = town
    w.odoo.write("res.partner", [w.ana.partner], {"belfry_key_custody": "self"})
    assert [x["problem"] for x in w.plan().problems] == ["register_custody"]
    w.odoo.write("res.partner", [w.ana.partner], {"belfry_key_custody": "belfry"})
    w.odoo.write("res.partner", [w.own.partner], {"belfry_key_custody": "belfry"})
    assert [x["problem"] for x in w.plan().problems] == ["register_custody"]


@pytest.mark.parametrize(
    "origin, scope, guessed",
    [
        ("", "own", False),  # create_bunker: the onboarding service's sessions
        ("https://chat.belfry.test", "own", False),
        ("https://belfry.test", "own", False),  # the chat on the apex, earlier
        ("https://calendar.belfry.test", "own", False),
        ("sh.belfry.app://connect", "own", False),
        ("https://notes.example.org", "outside", False),  # approved
        ("https://unknown.example.org", "own", True),
        ("nostrconnect://" + "ab" * 32, "own", True),
        ("https://belfry.test.evil.org", "own", True),
    ],
)
def test_scope_comes_from_keycasts_origin(world, origin, scope, guessed):
    assert migration.scope_of(world.svc, origin, world.approved) == (scope, guessed)


def test_guessed_scopes_block_until_accepted_and_the_scope_is_verified(world):
    w = world
    a = w.person("Aa")
    w.session(a, app="Who knows", origin="https://who.example.org")
    p = w.plan()
    assert migration.counts(p)["scope_guessed"] == 1
    migration.run_import(p, w.signer)
    report = migration.verify(w.plan(), w.signer, w.svc)
    assert kinds(report) == ["scope_guessed"]
    assert [x["app"] for x in report["scope_guessed"]] == ["Who knows"]
    assert not migration.verify(w.plan(), w.signer, w.svc, accept_guessed=2)["ok"]
    report = migration.verify(w.plan(), w.signer, w.svc, accept_guessed=1)
    assert report["ok"], report["mismatches"]
    # a scope the plan knows (not guessed) that differs is a mismatch
    own = w.session(a, app="Chat", origin="https://chat.belfry.test")
    migration.run_import(w.plan(), w.signer)
    row = next(c for c in w.signer.conns if c["bunker_pubkey"] == own.bunker)
    row.update(scope="outside", decrypt=True)
    assert kinds(migration.verify(w.plan(), w.signer, w.svc, accept_guessed=1)) == ["session_scope"]
    row.update(scope="own", decrypt=False)


def test_the_sync_never_turns_an_outside_app_into_an_own_one(world):
    """The finding: the approvals changed (or a guess) and the sync rewrote outside to own."""
    w = world
    a = w.person("Aa")
    s = w.session(a, app="notes.example.org", origin="https://notes.example.org")
    migration.run_import(w.plan(), w.signer)
    row = next(c for c in w.signer.conns if c["bunker_pubkey"] == s.bunker)
    assert (row["scope"], row["decrypt"]) == ("outside", True)
    # the app is no longer approved: the plan guesses own; the sync keeps it outside
    w.approved.discard("https://notes.example.org")
    p = w.plan()
    assert [x.guessed for x in p.sessions] == [True]
    migration.run_import(p, w.signer)
    assert (row["scope"], row["decrypt"]) == ("outside", True)
    report = migration.verify(w.plan(), w.signer, w.svc, accept_guessed=1)
    assert report["ok"], report["mismatches"]
    assert report["checked"]["kept_outside"] == 1


def test_approvals_that_cannot_be_read_stop_the_plan(services, odoo, monkeypatch):
    from belfry_onboarding import apps

    def down(svc):
        raise ConnectionError("relay down")

    monkeypatch.setattr(apps, "state", down)
    services.settings.nip05_domain = "belfry.test"
    w = World(services, odoo)
    w.person("Aa")
    with pytest.raises(migration.MigrationError, match="approvals"):
        w.plan()
    args = SimpleNamespace(
        phase="plan",
        exclude=None,
        no_stewards=True,
        accept_lost_sessions=0,
        accept_missing_stewards=0,
    )
    err = io.StringIO()
    code = migration.cli(
        args, services, services.settings, io.StringIO(json.dumps(w.doc())), io.StringIO(), err
    )
    assert code == 2 and "approvals" in err.getvalue()


def test_imported_outside_sessions_keep_decrypt_and_origin_kind_decrypt_are_verified(moved):
    w = moved
    app_row = next(c for c in w.signer.conns if c["bunker_pubkey"] == w.app.bunker)
    assert (app_row["scope"], app_row["decrypt"], app_row["origin"]) == (
        "outside",
        True,
        "https://notes.example.org",
    )
    live = next(c for c in w.signer.conns if c["bunker_pubkey"] == w.live.bunker)
    assert live["decrypt"] is False
    revoked = next(c for c in w.signer.conns if c["bunker_pubkey"] == w.revoked.bunker)
    for row, field, value, kind in [
        (app_row, "decrypt", False, "session_decrypt"),
        (live, "decrypt", True, "session_decrypt"),
        (live, "origin", "https://elsewhere.example.org", "session_origin"),
        (live, "kind", "http", "session_kind"),
        (revoked, "origin", "https://elsewhere.example.org", "session_origin"),
        (revoked, "kind", "http", "session_kind"),
    ]:
        old = row[field]
        row[field] = value
        assert kinds(migration.verify(w.plan(), w.signer, w.svc)) == [kind], (field, value)
        row[field] = old
    assert migration.verify(w.plan(), w.signer, w.svc)["ok"]


def test_import_refuses_after_the_switch_and_carry_back_before_it(town, monkeypatch):
    w = town
    monkeypatch.setattr(w.svc.settings, "signer", "belfry")
    code, out, err = run_cli(w, "import", monkeypatch)
    assert code == 2 and "own signer" in err and w.signer.accounts == {}
    monkeypatch.setattr(w.svc.settings, "signer", "keycast")
    code, out, err = run_cli(w, "carry-back", monkeypatch)
    assert code == 2 and "after the switch" in err
    code, out, err = run_cli(w, "import", monkeypatch)
    assert code == 0 and w.signer.window is True
    code, out, err = run_cli(w, "close-window", monkeypatch)
    assert code == 0 and w.signer.window is False and json.loads(out) == {"migration": False}
    # the archive is deleted only by the cleanup, after the switch
    w.signer.archived.append(("v", "k", b"s"))
    code, out, err = run_cli(w, "drop-archive", monkeypatch)
    assert code == 2 and w.signer.archived
    monkeypatch.setattr(w.svc.settings, "signer", "belfry")
    code, out, err = run_cli(w, "drop-archive", monkeypatch)
    assert code == 0 and json.loads(out) == {"deleted": 1} and w.signer.archived == []


def test_a_held_key_is_archived_only_inside_the_window(world):
    w = world
    a = w.person("Aa")
    migration.run_import(w.plan(), w.signer)
    w.signer.window = False
    w.signer.move_own(a.vine_id, "7" * 64)
    assert w.signer.archived == []


def test_unverified_sessions_block_until_accepted_by_number(town):
    w = town
    w.session(w.ana, "unverified")
    migration.run_import(w.plan(), w.signer)
    assert kinds(migration.verify(w.plan(), w.signer, w.svc)) == ["unverified_sessions"]
    assert migration.verify(w.plan(), w.signer, w.svc, accept_unverified=1)["ok"]
    assert not migration.verify(w.plan(), w.signer, w.svc, accept_unverified=2)["ok"]


def test_no_keycast_rows_is_never_a_clean_verify(moved):
    w = moved
    w.accounts.clear()
    w.sessions.clear()
    for mode in (False, True):
        report = migration.verify(w.plan(), w.signer, w.svc, keys_only=mode)
        assert "keycast_empty" in kinds(report), mode


def test_after_the_switch_a_member_may_take_their_own_key(moved):
    w = moved
    new = "7" * 64
    w.signer.accounts[w.ana.vine_id].update(pubkey=new, custody="own")
    for row in w.odoo.tables["belfry.pass"].values():
        if row["partner_id"] == w.ana.partner:
            row["keycast_pubkey"] = new
    w.odoo.write("res.partner", [w.ana.partner], {"belfry_key_custody": "self"})
    report = migration.verify(w.plan(), w.signer, w.svc, keys_only=True)
    assert report["ok"], report["mismatches"]


# ── rollback carries revocations and suspensions back ──────────────────────────────────────
def test_rollback_carries_back_what_was_signed_out_and_suspended(moved):
    w = moved
    w.signer.revoke(w.ana.vine_id, w.live.bunker)  # signed out on the signer after the switch
    w.signer.accounts[w.guest.vine_id]["status"] = "suspended"
    back = migration.carry_back(w.plan(), w.signer)
    assert back == {"revoke": [[w.live.bunker, NOW]], "suspend": [w.guest.pubkey]}


def test_the_signers_messages_are_shortened():
    r = httpx.Response(409, json={"error": "mismatch", "message": "the key is " + "ab" * 32})
    assert migration.SignerAdmin._error(r) == "mismatch: the key is abababab…"


def test_a_backup_is_as_old_as_its_own_name_says_and_never_from_the_future(tmp_path):
    import subprocess
    import time as _time

    fns = functions((SCRIPTS / "migrate-to-own-signer.sh").read_text())
    defs = "\n".join(
        f"{n}() {{\n{fns[n]}}}" for n in ("latest_backup", "backup_time", "backup_age")
    )

    def age(*names: str) -> str:
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        for i, name in enumerate(names):
            (d / name).write_text("x")
            t = _time.time() - 100 + i
            __import__("os").utime(d / name, (t, t))
        script = f'now() {{ date +%s; }}\nBACKUP_DIR="{d}"\n{defs}\nbackup_age'
        env = {"PATH": "/usr/bin:/bin", "BELFRY_BACKUP_STAMP": "/dev/null"}
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env
        ).stdout.strip()

    stamp = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime(_time.time() - 600))
    assert 590 <= int(age(f"belfry-box-{stamp}.tar.age")) <= 700
    future = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime(_time.time() + 86400))
    assert age(f"belfry-box-{future}.tar.age") == ""  # from the future: no fresh backup
    assert age("belfry-box-notastamp.tar.age") == ""
    assert age() == ""


def test_the_box_guard_stops_on_a_silent_postgres_and_a_state_without_a_box(tmp_path):
    import subprocess

    fns = functions((SCRIPTS / "migrate-to-own-signer.sh").read_text())
    defs = "\n".join(f"{n}() {{\n{fns[n]}}}" for n in ("state_get", "box_id", "need_this_box"))

    def run(db: str, state: str | None) -> subprocess.CompletedProcess:
        st = tmp_path / "state.json"
        if state is None:
            st.unlink(missing_ok=True)
        else:
            st.write_text(state)
        script = (
            'die() { echo "migrate: $*" >&2; exit 1; }\n'
            f'compose() {{ printf "%s" "{db}"; }}\n'
            "domain() { echo belfry.test; }\n"
            f'SECRETS="{tmp_path}"; INFRA="{tmp_path}"; STATE="{st}"; BELFRY=belfry; BOX=""\n'
            f"{defs}\nneed_this_box && echo passed"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    r = run("", None)
    assert (
        r.returncode == 1 and "Postgres didn't answer" in r.stderr and "another box" not in r.stderr
    )
    assert run("7123", None).stdout.strip() == "passed"  # nothing on record yet
    r = run("7123", '{"backup_at": 1}')
    assert r.returncode == 1 and "names no box" in r.stderr
    box = json.dumps(
        {"domain": "belfry.test", "keycast_db": "7123", "master": "none"}, sort_keys=True
    )
    assert run("7123", json.dumps({"box": box})).stdout.strip() == "passed"
    r = run("9999", json.dumps({"box": box}))
    assert r.returncode == 1 and "another box" in r.stderr
