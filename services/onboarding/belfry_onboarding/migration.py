"""Moving a belfry from Keycast and Authentik to its own signer (docs/own-signer.md §6 and
"Moving a belfry"; driven by ``infra/scripts/migrate-to-own-signer.sh``).

Three steps, all of them safe to run again, none of which writes to Keycast, to Authentik or to
anything of theirs:

- :func:`plan` (``signer-migrate plan``): read-only. What Keycast holds (the rows of §6's two
  queries, handed in as one JSON document) against the register: accounts by kind, usernames,
  sessions by what becomes of each, and every problem that must stop a migration.
- :func:`run_import` (``signer-migrate import``): the plan into the signer. Keys with their
  public key named (the signer refuses a key that opens to another), members' own keys,
  usernames, suspended state, sessions with their bunker keys; the admins from Authentik's
  group into the register.
- :func:`verify` (``signer-migrate verify``): every account, key, username, state, session
  (the revoked ones too, read back with ``?all=true``) and admin compared, and no account in
  the signer that Keycast did not have; a report with counts and every difference. Anything
  off is a failure.
- ``signer-migrate open``: the signer makes new accounts again. ``import`` closes that (the
  signer's "no new accounts" switch) so that nothing but the import puts an account there
  until the belfry runs on the signer; ``switch`` opens it once the verify after it is clean.

**What is never printed or logged**: keys of any kind other than public keys, and those only
shortened (the report file holds them shortened too); sealed keys; session secrets' hashes;
e-mail addresses other than masked.

The rules for sessions (docs/own-signer.md §6, "How the rows go in"), as :func:`classify`:

- *served* (``connected_client_pubkey`` = ``verified_client_pubkey``, not revoked, not
  expired): carried, bound to that client key;
- *revoked*, and *expired* (as revoked at ``expires_at``): carried as revoked rows, so their
  client keys stay linked to their account;
- *never connected*, and *connected but not verified*: left out and counted (nobody is signed
  in on them; Keycast's bcrypt secret cannot be carried).

**One client key, one identity** (Keycast patch 0023 holds new sessions to it; older rows may
not): when two accounts have sessions with the same client key, one account keeps it
(:func:`key_owners`: an active account before a suspended one, a served session before a
revoked one, then the session used last). The other account's rows with that key go in revoked
and without the key; if one of them was a served session of an active account, somebody is
signed out by the migration: counted as ``lost`` and a failure unless accepted by number.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import signerguard, stewards
from .signerguard import short

log = logging.getLogger(__name__)

HEX = re.compile(r"^[0-9a-f]{64}$")
VINE = re.compile(r"^odoo-partner-(\d+)$")
# what the signer takes as a username (services/signer, valid_username)
SIGNER_NAME = re.compile(r"^[a-z0-9._-]{1,63}$")
GUEST_NAME = re.compile(r"^guest-\d+$")
APP_MAX = 64
# the signer's HTTP body limit is axum's default (2 MB): batches stay well under it
BATCH_BYTES = 256_000
ACCOUNT_BATCH = 100
SESSION_BATCH = 400


class MigrationError(RuntimeError):
    """The migration cannot go on (a source that does not answer, rows that make no sense)."""


def mask(email: str) -> str:
    """An e-mail address as reports show it: ``a•••@example.org``."""
    local, _, domain = str(email or "").partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


# ── the rows ────────────────────────────────────────────────────────────────────────────────
@dataclass
class Account:
    pubkey: str
    vine_id: str
    username: str
    status: str  # active | suspended (Keycast's banned is kept as suspended)
    sealed: str  # hex of Keycast's encrypted key; "": the member holds their own key
    kind: str = "member"  # member | guest | agent | own_key | no_vine_id
    excluded: bool = False

    @property
    def own(self) -> bool:
        return not self.sealed


@dataclass
class Session:
    user: str  # the account's pubkey
    bunker: str
    secret_hash: str
    app: str
    origin: str
    client: str  # the client key the row is carried with ("" for none)
    state: str  # served | revoked | expired | never_connected | unverified
    created_at: int
    last_used_at: int | None
    uses: int
    revoked_at: int | None
    scope: str = "outside"
    notes: list[str] = field(default_factory=list)

    @property
    def carried(self) -> bool:
        return self.state in ("served", "revoked", "expired")

    @property
    def live(self) -> bool:
        return self.state == "served"


@dataclass
class Plan:
    accounts: list[Account]
    sessions: list[Session]
    problems: list[dict[str, Any]]
    lost: list[dict[str, Any]]
    register: dict[str, str]  # vine_id -> the key the register has
    now: int
    digest: str

    def by_user(self) -> dict[str, list[Session]]:
        out: dict[str, list[Session]] = {}
        for s in self.sessions:
            out.setdefault(s.user, []).append(s)
        return out

    def included(self) -> list[Account]:
        return [a for a in self.accounts if not a.excluded]


def _int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hex(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if HEX.fullmatch(v) else ""


def source_digest(doc: dict[str, Any]) -> str:
    """A fingerprint of what Keycast holds that a migration carries: accounts, keys, names,
    states, sessions and their client keys. Use counters and last-use times are left out (they
    move with every request and are carried as they are at the last import). Two reads with
    the same digest hold the same people, keys and sessions."""
    accounts = sorted(
        (
            str(a.get("pubkey")),
            str(a.get("vine_id")),
            str(a.get("username")),
            str(a.get("status")),
            hashlib.sha256(str(a.get("sealed") or "").encode()).hexdigest(),
        )
        for a in doc.get("accounts") or []
    )
    sessions = sorted(
        (
            str(s.get("user_pubkey")),
            str(s.get("bunker_public_key")),
            str(s.get("client_id")),
            str(s.get("client_pubkey")),
            str(s.get("connected_client_pubkey")),
            str(s.get("verified_client_pubkey")),
            str(s.get("revoked_at")),
            str(s.get("expires_at")),
        )
        for s in doc.get("sessions") or []
    )
    return hashlib.sha256(json.dumps([accounts, sessions]).encode()).hexdigest()


def classify(row: dict[str, Any], now: int) -> tuple[str, str, int | None]:
    """(state, the client key the row is carried with, revoked_at) for one row of
    ``oauth_authorizations``."""
    connected = _hex(row.get("connected_client_pubkey"))
    verified = _hex(row.get("verified_client_pubkey"))
    declared = _hex(row.get("client_pubkey"))
    revoked_at = _int(row.get("revoked_at"))
    expires_at = _int(row.get("expires_at"))
    client = connected or declared
    if revoked_at is not None:
        return "revoked", client, revoked_at
    if expires_at is not None and expires_at <= now:
        return "expired", client, expires_at
    if not connected:
        return "never_connected", "", None
    if connected != verified:
        return "unverified", "", None
    return "served", connected, None


def own_names(svc: Any) -> set[str]:
    """The names the belfry itself gives signer sessions (the chat, the phone apps, a ticket
    buyer's browser, the first admin's)."""
    from . import apps

    return {*apps.chat_names(svc), "Tickets", "Setup page (first session)"}


def scope_of(svc: Any, client_id: str, origin: str, names: set[str]) -> str:
    """``own`` for a session of one of the belfry's own apps, ``outside`` for any other; when
    in doubt ``outside`` (the app can sign in again, with the limits of an outside app)."""
    from . import appsignin, device
    from .appshome import app_url

    origin = (origin or "").strip().rstrip("/")
    chat = app_url(svc.settings, "chat").rstrip("/")
    name, _ = device.split(client_id)
    if name in names and origin in ("", chat):
        return "own"
    if origin:
        if origin == chat or appsignin.suite_name(svc, origin + "/"):
            return "own"
    return "outside"


def _app(client_id: str, notes: list[str]) -> str:
    app = " ".join(str(client_id or "").split())
    if not app:
        notes.append("empty name: called App")
        return "App"
    if len(app) > APP_MAX:
        notes.append("name cut to 64 characters")
        return app[:APP_MAX]
    return app


def key_owners(accounts: dict[str, Account], sessions: list[Session]) -> dict[str, str]:
    """client key -> the pubkey of the account that keeps it. Only keys of carried rows count.
    An active account before a suspended one; a served session before a revoked or expired
    one; then the session used last, made last; then the lower pubkey (so two runs agree)."""
    best: dict[str, tuple[tuple[int, int, int, int], str]] = {}
    for s in sessions:
        if not s.carried or not s.client:
            continue
        acct = accounts.get(s.user)
        rank = (
            1 if acct is not None and acct.status == "active" else 0,
            1 if s.live else 0,
            int(s.last_used_at or 0),
            int(s.created_at or 0),
        )
        have = best.get(s.client)
        if have is None or rank > have[0] or (rank == have[0] and s.user < have[1]):
            best[s.client] = (rank, s.user)
    return {client: user for client, (_, user) in best.items()}


def plan(
    svc: Any,
    doc: dict[str, Any],
    exclude: Iterable[str] = (),
    now: int | None = None,
) -> Plan:
    """What a migration of DOC (``{"accounts": [...], "sessions": [...], "now"}``: the rows of
    docs/own-signer.md §6) would do, checked against the register. Reads only."""
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        raise MigrationError("the rows are not {accounts: [...], sessions: [...]}")
    now = int(now or _int(doc.get("now")) or time.time())
    excluded = {_hex(e) for e in exclude} - {""}
    problems: list[dict[str, Any]] = []

    def problem(kind: str, **more: Any) -> None:
        problems.append({"problem": kind, **more})

    try:
        people = signerguard.members(svc)
        tiers = _tiers(svc)
    except Exception as exc:  # noqa: BLE001
        raise MigrationError(f"the register could not be read: {type(exc).__name__}") from exc
    register = {m["vine_id"]: m["pubkey"] for m in people}

    accounts: dict[str, Account] = {}
    vines: dict[str, str] = {}
    names: dict[str, str] = {}
    for row in doc["accounts"]:
        pubkey = _hex(row.get("pubkey"))
        if not pubkey:
            problem("account_without_pubkey")
            continue
        if pubkey in accounts:
            problem("account_twice", pubkey=short(pubkey))
            continue
        vine = str(row.get("vine_id") or "").strip()
        username = str(row.get("username") or "").strip()
        status = str(row.get("status") or "active").strip().lower()
        if status not in ("active", "suspended", "banned"):
            problem("unknown_status", pubkey=short(pubkey), status=status[:20])
        sealed = str(row.get("sealed") or "").strip().lower()
        a = Account(
            pubkey=pubkey,
            vine_id=vine,
            username=username,
            status="active" if status == "active" else "suspended",
            sealed=sealed,
            excluded=pubkey in excluded,
        )
        accounts[pubkey] = a
        m = VINE.fullmatch(vine)
        if not vine:
            a.kind = "no_vine_id"
        elif a.own:
            a.kind = "own_key"
        elif m and tiers.get(int(m.group(1))) == "agent":
            a.kind = "agent"
        elif GUEST_NAME.fullmatch(username) or (m and tiers.get(int(m.group(1))) == "guest"):
            a.kind = "guest"
        if a.excluded:
            continue
        if not vine:
            # §6: list them and stop, never skip them silently (--exclude names one on purpose)
            problem("no_vine_id", pubkey=short(pubkey), username=username[:63])
            continue
        if vine in vines:
            problem("vine_id_twice", vine_id=vine, pubkey=short(pubkey))
        vines[vine] = pubkey
        if sealed and not re.fullmatch(r"(?:[0-9a-f]{2})+", sealed):
            problem("sealed_not_hex", vine_id=vine)
        if username:
            if not SIGNER_NAME.fullmatch(username):
                problem("username_not_carried", vine_id=vine, username=username[:63])
            elif username in names:
                problem("username_twice", vine_id=vine, username=username)
            names[username] = pubkey
        have = register.get(vine)
        if have and have != pubkey:
            problem(
                "register_has_another_key",
                vine_id=vine,
                keycast=short(pubkey),
                register=short(have),
            )
    for vine, key in register.items():
        if vine not in vines:
            # a person the register expects in the signer and Keycast does not have
            problem("member_not_in_keycast", vine_id=vine, register=short(key))

    own = own_names(svc)
    sessions: list[Session] = []
    bunkers: dict[str, str] = {}
    for row in doc.get("sessions") or []:
        user = _hex(row.get("user_pubkey"))
        bunker = _hex(row.get("bunker_public_key"))
        acct = accounts.get(user)
        if acct is None:
            problem("session_without_account", bunker=short(bunker))
            continue
        state, client, revoked_at = classify(row, now)
        notes: list[str] = []
        s = Session(
            user=user,
            bunker=bunker,
            secret_hash=str(row.get("secret_hash") or ""),
            app=_app(str(row.get("client_id") or ""), notes),
            origin=str(row.get("redirect_origin") or ""),
            client=client,
            state=state,
            created_at=_int(row.get("created_at")) or 0,
            last_used_at=_int(row.get("last_used_at")),
            uses=_int(row.get("uses")) or 0,
            revoked_at=revoked_at,
            notes=notes,
        )
        s.scope = scope_of(svc, str(row.get("client_id") or ""), s.origin, own)
        sessions.append(s)
        if acct.excluded or not s.carried:
            continue
        if not bunker or not s.secret_hash:
            problem("session_without_bunker_key", vine_id=acct.vine_id)
            s.state = "never_connected"  # nothing to derive from: not carried
            continue
        if bunker in bunkers and bunkers[bunker] != user:
            problem("bunker_key_of_two_accounts", bunker=short(bunker))
        bunkers[bunker] = user
        if acct.own:
            # the signer has no key to derive a bunker key from; Keycast deletes these rows
            # when a member takes their own key (patch 0012), so one that is left is stale
            s.notes.append("the account holds its own key: not carried")
            s.state = "not_carried_own_key"

    # one client key, one identity
    lost: list[dict[str, Any]] = []
    owners = key_owners(
        {k: a for k, a in accounts.items() if not a.excluded},
        [s for s in sessions if not accounts[s.user].excluded],
    )
    for s in sessions:
        acct = accounts[s.user]
        if acct.excluded or not s.carried or not s.client:
            continue
        if owners.get(s.client) == s.user:
            continue
        keeper = accounts[owners[s.client]]
        if s.live:
            # a session Keycast still served, whose key another account keeps
            s.state = "revoked"
            s.revoked_at = now
            s.notes.append("its client key is another account's: carried revoked")
            if acct.status == "active":
                lost.append(
                    {
                        "vine_id": acct.vine_id,
                        "app": s.app,
                        "bunker": short(s.bunker),
                        "client": short(s.client),
                        "kept_by": keeper.vine_id,
                    }
                )
        else:
            s.notes.append("its client key is another account's: carried without it")
        s.client = ""
    return Plan(
        accounts=sorted(accounts.values(), key=lambda a: (a.vine_id, a.pubkey)),
        sessions=sessions,
        problems=problems,
        lost=lost,
        register=register,
        now=now,
        digest=source_digest(doc),
    )


def _tiers(svc: Any) -> dict[int, str]:
    """partner -> ``agent`` or ``guest`` when every pass they have is one (for the counts)."""
    out: dict[int, set[str]] = {}
    for r in svc.odoo.search_read("belfry.pass", [], ["partner_id", "tier", "guest_agent"]):
        partner = signerguard._m2o(r.get("partner_id"))
        if not partner:
            continue
        tier = str(r.get("tier") or "")
        kind = "agent" if tier == "agent" or r.get("guest_agent") else tier
        out.setdefault(partner, set()).add(kind)
    return {
        p: ("agent" if kinds <= {"agent"} else "guest")
        for p, kinds in out.items()
        if kinds <= {"agent"} or kinds <= {"guest", "agent"}
    }


def counts(p: Plan) -> dict[str, Any]:
    """The numbers every phase prints."""
    kinds: dict[str, int] = {}
    for a in p.accounts:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    states: dict[str, int] = {}
    scopes = {"own": 0, "outside": 0}
    for s in p.sessions:
        states[s.state] = states.get(s.state, 0) + 1
        if s.live:
            scopes[s.scope] += 1
    inc = p.included()
    return {
        "accounts": len(p.accounts),
        "excluded": len(p.accounts) - len(inc),
        "by_kind": dict(sorted(kinds.items())),
        "with_username": sum(1 for a in inc if a.username),
        "suspended": sum(1 for a in inc if a.status != "active"),
        "register_members": len(p.register),
        "sessions": len(p.sessions),
        "sessions_by_state": dict(sorted(states.items())),
        "live_by_scope": scopes,
        "lost": len(p.lost),
        "problems": len(p.problems),
    }


# ── the signer's API, as the migration uses it ──────────────────────────────────────────────
class SignerAdmin:
    """The calls of the signer's API a migration needs (services/signer/README.md, "API").
    Internal network, bearer token. Nothing here reads or returns a secret key."""

    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        if len(token or "") < 16:
            raise MigrationError("the signer's token is not set (the secret signer_token)")
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=60.0)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _call(self, method: str, path: str, body: Any = None) -> httpx.Response:
        kw: dict[str, Any] = {"headers": self._headers}
        if body is not None:
            kw["json"] = body
        return self._client.request(method, self.url + path, **kw)

    @staticmethod
    def _path(vine_id: str) -> str:
        from urllib.parse import quote

        return "/accounts/" + quote(vine_id, safe="")

    @staticmethod
    def _error(r: httpx.Response) -> str:
        try:
            data = r.json()
        except ValueError:
            return f"http {r.status_code}"
        if isinstance(data, dict):
            # the signer's messages name vine ids, row indexes and public keys: no secrets
            return f"{data.get('error') or r.status_code}: {str(data.get('message') or '')[:200]}"
        return f"http {r.status_code}"

    def health(self) -> dict[str, Any]:
        r = self._client.get(self.url + "/health")
        r.raise_for_status()
        return r.json()

    def account(self, vine_id: str) -> dict[str, Any] | None:
        r = self._call("GET", self._path(vine_id))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def import_accounts(self, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        """``POST /import``: one result per row (``derived``, ``imported``, ``exists`` or
        ``error`` with its code)."""
        r = self._call("POST", "/import", {"accounts": rows})
        if r.status_code != 200:
            raise MigrationError(f"the signer refused the keys: {self._error(r)}")
        return list(r.json().get("results") or [])

    def put_own(self, vine_id: str, pubkey: str) -> str:
        """An account for a member who holds their own key. "" or what was refused."""
        r = self._call("PUT", self._path(vine_id), {"own_pubkey": pubkey})
        return "" if r.status_code == 200 else self._error(r)

    def set_username(self, vine_id: str, username: str) -> str:
        r = self._call("PUT", self._path(vine_id) + "/username", {"username": username})
        return "" if r.status_code == 200 else self._error(r)

    def set_status(self, vine_id: str, status: str) -> str:
        r = self._call("POST", self._path(vine_id) + "/status", {"status": status})
        return "" if r.status_code == 200 else self._error(r)

    def import_connections(self, vine_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """``POST /import/connections``: ``{imported, existing, revoked}`` or ``{"error"}``."""
        r = self._call("POST", "/import/connections", {"vine_id": vine_id, "connections": rows})
        if r.status_code != 200:
            return {"error": self._error(r)}
        return dict(r.json())

    def connections(self, vine_id: str, all: bool = False) -> list[dict[str, Any]]:
        """The account's live connections; with ALL every one it ever had (``revoked_at`` set
        on the revoked ones)."""
        r = self._call("GET", self._path(vine_id) + "/connections" + ("?all=true" if all else ""))
        r.raise_for_status()
        return list(r.json() or [])

    def list_accounts(self) -> list[dict[str, Any]] | None:
        """Every account the signer has (``GET /accounts``, page by page), or None from a
        signer that cannot list them."""
        out: list[dict[str, Any]] = []
        after = ""
        while True:
            r = self._call("GET", "/accounts?limit=1000" + (f"&after={after}" if after else ""))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            out.extend(data.get("accounts") or [])
            after = str(data.get("next") or "")
            if not after:
                return out

    def close_accounts(self, closed: bool) -> bool | None:
        """The signer's "no new accounts" switch (``POST /settings``); returns it as it now
        is, or None from a signer that has none."""
        r = self._call("POST", "/settings", {"no_new_accounts": bool(closed)})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise MigrationError(f"the signer refused the switch: {self._error(r)}")
        return bool(r.json().get("no_new_accounts"))


def batches(rows: list[dict[str, Any]], most: int) -> Iterable[list[dict[str, Any]]]:
    """ROWS in runs of at most MOST rows and BATCH_BYTES of JSON."""
    run: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        n = len(json.dumps(row))
        if run and (len(run) >= most or size + n > BATCH_BYTES):
            yield run
            run, size = [], 0
        run.append(row)
        size += n
    if run:
        yield run


def connection_row(s: Session) -> dict[str, Any]:
    row: dict[str, Any] = {
        "bunker_pubkey": s.bunker,
        "keycast_secret_hash": s.secret_hash,
        "app": s.app,
        "scope": s.scope,
        "created_at": s.created_at,
        "uses": s.uses,
    }
    if s.client:
        row["client_pubkey"] = s.client
    if s.last_used_at is not None:
        row["last_used_at"] = s.last_used_at
    if s.origin:
        # Keycast's redirect_origin: the signer gives it back as the session's origin
        row["origin"] = s.origin[:200]
    if s.state != "served":
        row["revoked_at"] = s.revoked_at or s.created_at
    return row


# ── import ──────────────────────────────────────────────────────────────────────────────────
def run_import(p: Plan, signer: Any) -> dict[str, Any]:
    """The plan into the signer. Refuses to start while the plan has problems. Every step is
    safe to repeat: an account, a name, a state or a session that is there already stays as it
    is. Returns counts and ``errors`` (each names an account by its vine id)."""
    if p.problems:
        raise MigrationError(
            f"{len(p.problems)} problems in what Keycast holds: nothing was imported "
            "(signer-migrate plan lists them)"
        )
    out: dict[str, Any] = {
        "accounts": {"derived": 0, "imported": 0, "exists": 0, "own_key": 0},
        "usernames": 0,
        "suspended": 0,
        "sessions": {"imported": 0, "existing": 0, "revoked": 0},
        "errors": [],
    }
    errors: list[dict[str, Any]] = out["errors"]
    accounts = p.included()
    held = [a for a in accounts if not a.own]
    failed: set[str] = set()
    for run in batches(
        [{"vine_id": a.vine_id, "pubkey": a.pubkey, "sealed": a.sealed} for a in held],
        ACCOUNT_BATCH,
    ):
        for res in signer.import_accounts(run):
            how = str(res.get("result") or "")
            if how in out["accounts"]:
                out["accounts"][how] += 1
            else:
                failed.add(str(res.get("vine_id")))
                errors.append(
                    {
                        "step": "key",
                        "vine_id": res.get("vine_id"),
                        "error": f"{res.get('error')}: {str(res.get('message') or '')[:200]}",
                    }
                )
    for a in accounts:
        if a.own:
            refused = signer.put_own(a.vine_id, a.pubkey)
            if refused:
                failed.add(a.vine_id)
                errors.append({"step": "own_key", "vine_id": a.vine_id, "error": refused})
            else:
                out["accounts"]["own_key"] += 1
    for a in accounts:
        if a.vine_id in failed:
            continue
        have = signer.account(a.vine_id)
        if have is None or str(have.get("pubkey") or "").lower() != a.pubkey:
            failed.add(a.vine_id)
            errors.append({"step": "account", "vine_id": a.vine_id, "error": "not as imported"})
            continue
        if a.username and str(have.get("username") or "") != a.username:
            refused = signer.set_username(a.vine_id, a.username)
            if refused:
                errors.append({"step": "username", "vine_id": a.vine_id, "error": refused})
            else:
                out["usernames"] += 1
        if str(have.get("status") or "active") != a.status:
            refused = signer.set_status(a.vine_id, a.status)
            if refused:
                errors.append({"step": "status", "vine_id": a.vine_id, "error": refused})
            elif a.status != "active":
                out["suspended"] += 1
    by_user = p.by_user()
    # accounts that keep a client key first, then the rest: with the keys' owners settled in
    # the plan no order can fail, and this one would still be right if a rule were missed
    for a in sorted(accounts, key=lambda a: (a.status != "active", a.vine_id)):
        if a.vine_id in failed or a.own:
            continue
        rows = [connection_row(s) for s in by_user.get(a.pubkey, []) if s.carried]
        for run in batches(rows, SESSION_BATCH):
            res = signer.import_connections(a.vine_id, run)
            if "error" in res:
                errors.append({"step": "sessions", "vine_id": a.vine_id, "error": res["error"]})
                break
            for k in out["sessions"]:
                out["sessions"][k] += int(res.get(k) or 0)
    return out


def import_stewards(svc: Any, authentik: Any) -> dict[str, Any]:
    """Authentik's stewards group into the register (stewards.import_from_authentik), with
    the addresses masked."""
    res = stewards.import_from_authentik(svc, authentik)
    return {k: [mask(e) for e in v] for k, v in res.items()}


# ── verify ──────────────────────────────────────────────────────────────────────────────────
def verify(
    p: Plan,
    signer: Any,
    svc: Any,
    authentik: Any = None,
    accept_lost: int = 0,
    accept_missing_stewards: int = 0,
    keys_only: bool = False,
) -> dict[str, Any]:
    """Everything a migration carries, compared: for every account the signer's key, Keycast's
    and the register's; custody, username and state; for every carried session its bunker key
    and client key among the signer's live connections; the admins. Returns the report:
    ``ok`` is true only when ``mismatches`` is empty. Reads only.

    KEYS_ONLY is for a belfry that already runs on its signer: people have signed in and out,
    joined, been renamed since, and Keycast's rows are a picture of the past. What must hold
    for ever is checked (every account Keycast had is in the signer with the same key and the
    same custody, and so is every person of the register); usernames, states, sessions and
    admins are not, and a member Keycast never had is not a problem."""
    mismatches: list[dict[str, Any]] = []

    def miss(kind: str, **more: Any) -> None:
        mismatches.append({"mismatch": kind, **more})

    for prob in p.problems:
        if keys_only and prob.get("problem") == "member_not_in_keycast":
            continue  # joined after the switch
        miss("problem", **prob)
    if keys_only:
        pass
    elif len(p.lost) != int(accept_lost or 0):
        for lost in p.lost:
            miss("session_lost", **lost)
        if not p.lost:
            miss("accept_lost_does_not_match", accepted=int(accept_lost), lost=0)
    checked = {
        "accounts": 0,
        "keys_equal": 0,
        "usernames": 0,
        "live_sessions": 0,
        "revoked_sessions": 0,
    }
    unreadable = 0  # revoked rows of a signer that cannot list them
    by_user = p.by_user()
    for a in p.included():
        if not a.vine_id:
            continue  # a problem already
        checked["accounts"] += 1
        have = signer.account(a.vine_id)
        if have is None:
            miss("account_not_imported", vine_id=a.vine_id, keycast=short(a.pubkey))
            continue
        got = str(have.get("pubkey") or "").lower()
        reg = p.register.get(a.vine_id)
        if got != a.pubkey:
            miss("signer_key", vine_id=a.vine_id, keycast=short(a.pubkey), signer=short(got))
        if reg and reg != a.pubkey:
            miss("register_key", vine_id=a.vine_id, keycast=short(a.pubkey), register=short(reg))
        if got == a.pubkey and (not reg or reg == a.pubkey):
            checked["keys_equal"] += 1
        if (str(have.get("custody") or "") == "own") != a.own:
            miss("custody", vine_id=a.vine_id, signer=str(have.get("custody")))
        if keys_only:
            continue
        if str(have.get("username") or "") != a.username:
            miss("username", vine_id=a.vine_id)
        elif a.username:
            checked["usernames"] += 1
        if str(have.get("status") or "") != a.status:
            miss("status", vine_id=a.vine_id, keycast=a.status, signer=str(have.get("status")))
        want = {s.bunker: s for s in by_user.get(a.pubkey, []) if s.live and not a.own}
        live = {str(c.get("bunker_pubkey") or ""): c for c in signer.connections(a.vine_id)}
        # the revoked and expired rows came in revoked: their client keys stay this account's
        gone = {
            s.bunker: s for s in by_user.get(a.pubkey, []) if s.carried and not s.live and not a.own
        }
        rows = signer.connections(a.vine_id, all=True) if gone else []
        listed = {str(c.get("bunker_pubkey") or ""): c for c in rows}
        if gone and not any("revoked_at" in c for c in rows) and rows:
            unreadable += len(gone)  # an older signer: its list has no revoked rows
            gone = {}
        for bunker, s in gone.items():
            c = listed.get(bunker)
            if c is None:
                miss("revoked_session_missing", vine_id=a.vine_id, bunker=short(bunker))
            elif not c.get("revoked_at"):
                miss("revoked_session_live", vine_id=a.vine_id, bunker=short(bunker))
            elif s.client and str(c.get("client_pubkey") or "").lower() != s.client:
                miss("session_client_key", vine_id=a.vine_id, bunker=short(bunker))
            else:
                checked["revoked_sessions"] += 1
        for bunker, s in want.items():
            c = live.get(bunker)
            if c is None:
                miss("session_missing", vine_id=a.vine_id, bunker=short(bunker), app=s.app)
            elif str(c.get("client_pubkey") or "").lower() != s.client:
                miss("session_client_key", vine_id=a.vine_id, bunker=short(bunker))
            else:
                checked["live_sessions"] += 1
        for bunker in live.keys() - want.keys():
            # nothing may be live in the signer that Keycast did not serve (a session made on
            # the signer before the switch, or a revoked row that came in live)
            miss("session_unexpected", vine_id=a.vine_id, bunker=short(bunker))
    # nothing in the signer that Keycast did not have (before the switch: nothing may have
    # made an account there but the import; after it, people join, so keys only)
    listing = signer.list_accounts() if hasattr(signer, "list_accounts") else None
    if listing is not None and not keys_only:
        known = {a.vine_id for a in p.accounts if a.vine_id}
        for acct in listing:
            vine = str(acct.get("vine_id") or "")
            if vine not in known:
                miss("account_unexpected", vine_id=vine, signer=short(acct.get("pubkey")))
    if listing is not None:
        checked["signer_accounts"] = len(listing)
    for vine, key in p.register.items():
        have = signer.account(vine)
        if have is None:
            miss("member_not_in_signer", vine_id=vine, register=short(key))
        elif str(have.get("pubkey") or "").lower() != key:
            miss("member_key", vine_id=vine, register=short(key), signer=short(have.get("pubkey")))
    report: dict[str, Any] = {"checked": checked, "keys_only": bool(keys_only)}
    if authentik is not None and not keys_only:
        report["stewards"] = _verify_stewards(svc, authentik, miss, accept_missing_stewards)
    want_accounts = sum(1 for a in p.included() if a.vine_id)
    if checked["accounts"] != want_accounts:
        miss("account_count", keycast=want_accounts, checked=checked["accounts"])
    report.update(
        {
            "at": int(time.time()),
            "digest": p.digest,
            "counts": counts(p),
            "lost": p.lost,
            "accepted_lost": int(accept_lost or 0),
            "mismatches": mismatches,
            "ok": not mismatches,
            "not_verifiable": {
                # revoked rows of a signer that cannot list them (before GET …?all=true)
                "revoked_sessions": unreadable,
                # whether the signer could list its accounts (GET /accounts)
                "accounts_list": listing is None,
            },
        }
    )
    return report


def _acct(p: Plan, pubkey: str) -> Account:
    return next(a for a in p.accounts if a.pubkey == pubkey)


def _verify_stewards(svc: Any, authentik: Any, miss: Any, accept_missing: int) -> dict[str, Any]:
    group = svc.settings.steward_group
    want = sorted({e.strip().lower() for e in authentik.group_member_emails(group) if e.strip()})
    flagged = sorted(
        {
            str(r["email"]).strip().lower()
            for r in stewards._flagged(svc, ["email"])
            if r.get("email")
        }
    )
    missing = [e for e in want if e not in flagged]
    extra = [e for e in flagged if e not in want]
    # an admin in Authentik with no person in the register has nothing to carry: reported,
    # and a failure unless accepted by number
    without_person = [e for e in missing if not stewards._partners(svc, e)]
    not_marked = [e for e in missing if e not in without_person]
    for e in not_marked:
        miss("steward_not_marked", email=mask(e))
    for e in extra:
        miss("steward_only_in_register", email=mask(e))
    if len(without_person) != int(accept_missing or 0):
        for e in without_person:
            miss("steward_without_person", email=mask(e))
        if not without_person:
            miss("accept_missing_stewards_does_not_match", accepted=int(accept_missing))
    return {
        "authentik": len(want),
        "register": len(flagged),
        "without_person": len(without_person),
    }


# ── the command line ────────────────────────────────────────────────────────────────────────
def summary(c: dict[str, Any]) -> list[str]:
    kinds = ", ".join(f"{n} {k}" for k, n in c["by_kind"].items()) or "none"
    states = ", ".join(f"{n} {k}" for k, n in c["sessions_by_state"].items()) or "none"
    return [
        f"accounts in Keycast: {c['accounts']} ({kinds}); excluded on purpose: {c['excluded']}",
        f"with a username: {c['with_username']}; suspended: {c['suspended']}; "
        f"people with a key in the register: {c['register_members']}",
        f"sessions: {c['sessions']} ({states})",
        f"live sessions carried: {c['live_by_scope']['own']} of the belfry's own apps, "
        f"{c['live_by_scope']['outside']} of outside apps; lost to a shared client key: "
        f"{c['lost']}",
        f"problems: {c['problems']}",
    ]


def cli(args: Any, svc: Any, settings: Any, stdin: Any, out: Any, err: Any) -> int:
    """``python -m belfry_onboarding signer-migrate plan|import|verify|open`` with the rows on
    stdin. The report (JSON, one line) goes to stdout, what a person reads to stderr.
    Exit 0: clean; 1: problems or mismatches; 2: a source that does not answer."""

    def say(line: str) -> None:
        print(line, file=err)

    try:
        doc = json.load(stdin)
        p = plan(svc, doc, exclude=args.exclude or ())
    except (ValueError, MigrationError) as exc:
        say(f"signer-migrate: {exc}")
        return 2
    c = counts(p)
    for line in summary(c):
        say(line)
    for prob in p.problems[:50]:
        say("  problem: " + json.dumps(prob, sort_keys=True))
    for lost in p.lost[:50]:
        say("  signed out by the move: " + json.dumps(lost, sort_keys=True))
    if args.phase == "plan":
        json.dump({"counts": c, "problems": p.problems, "lost": p.lost, "digest": p.digest}, out)
        out.write("\n")
        return 1 if p.problems else 0
    try:
        signer = SignerAdmin(settings.signer_url, settings.signer_token)
        health = signer.health()
    except (MigrationError, httpx.HTTPError) as exc:
        say(f"signer-migrate: the signer does not answer: {type(exc).__name__}")
        return 2
    if health.get("seed") not in (True, "unlocked"):
        say("signer-migrate: the signer has no custody seed (locked): unlock it first")
        return 2
    authentik = None
    if not args.no_stewards:
        from .clients.authentik import AuthentikClient

        if not settings.authentik_token:
            say("signer-migrate: AUTHENTIK_TOKEN is not set (--no-stewards to leave admins out)")
            return 2
        authentik = AuthentikClient(settings.authentik_url, settings.authentik_token)
    try:
        if args.phase == "open":
            # the belfry runs on its signer and the verify after the switch is clean: people
            # may join again (docs/own-signer.md, "Moving a belfry")
            state = signer.close_accounts(False)
            say("new accounts: " + ("the signer has no such switch" if state is None else "open"))
            json.dump({"no_new_accounts": state}, out)
            out.write("\n")
            return 0
        if args.phase == "import":
            # from the first import to the switch the signer makes no account but the ones
            # the import names: a caller that forgot to say which key it expects cannot mint
            # one in between (lifted by `open` after the switch)
            closed = signer.close_accounts(True)
            say(
                "new accounts: "
                + ("the signer has no such switch" if closed is None else "closed until the switch")
            )
            res = run_import(p, signer)
            if authentik is not None:
                res["stewards"] = import_stewards(svc, authentik)
            a, s = res["accounts"], res["sessions"]
            say(
                f"keys: {a['derived']} derived from the seed, {a['imported']} imported as "
                f"they are, {a['exists']} there already, {a['own_key']} members' own"
            )
            say(f"usernames set: {res['usernames']}; suspended: {res['suspended']}")
            say(
                f"sessions: {s['imported']} live written, {s['revoked']} revoked written, "
                f"{s['existing']} there already"
            )
            if authentik is not None:
                st = res["stewards"]
                say(
                    f"admins: {len(st['marked'])} marked, {len(st['already'])} already, "
                    f"{len(st['missing'])} without a person in the register"
                )
            for e in res["errors"][:50]:
                say("  error: " + json.dumps(e, sort_keys=True))
            json.dump({"counts": c, "result": res, "digest": p.digest}, out)
            out.write("\n")
            return 1 if res["errors"] else 0
        report = verify(
            p,
            signer,
            svc,
            authentik,
            args.accept_lost_sessions,
            args.accept_missing_stewards,
            keys_only=bool(getattr(args, "keys_only", False)),
        )
    except MigrationError as exc:
        say(f"signer-migrate: {exc}")
        return 1
    except httpx.HTTPError as exc:
        say(f"signer-migrate: a source stopped answering: {type(exc).__name__}")
        return 2
    ch = report["checked"]
    say(
        f"verified: {ch['accounts']} accounts, {ch['keys_equal']} keys equal in Keycast, the "
        f"signer and the register, {ch['usernames']} usernames, {ch['live_sessions']} live "
        f"sessions, {ch['revoked_sessions']} revoked ones; "
        f"{report['not_verifiable']['revoked_sessions']} revoked rows not readable from the "
        "signer"
    )
    for m in report["mismatches"][:100]:
        say("  MISMATCH: " + json.dumps(m, sort_keys=True))
    if report["keys_only"]:
        say("(keys only: usernames, states, sessions and admins move on after a switch)")
    say("verify: clean" if report["ok"] else f"verify: {len(report['mismatches'])} mismatches")
    json.dump(report, out)
    out.write("\n")
    return 0 if report["ok"] else 1
