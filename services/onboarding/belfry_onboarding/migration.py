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
- **The migration's window** (the signer's ``migration`` setting): ``import`` opens it, and
  only inside it does the signer honour an import's ``sync`` (rows that are there brought to
  Keycast's state) or archive a held key a move to an own key replaces.
  ``signer-migrate close-window`` (the switch, after the verify that decides) and ``open``
  close it. ``import`` and ``carry-back`` check the mode first: ``import`` only while the
  belfry runs on Keycast (after the switch an import would bring the signer back to Keycast's
  past), ``carry-back`` only after the switch (what it reads is what happened on the signer).

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


def shorten_keys(text: str) -> str:
    """Any 64-hex key in TEXT shortened as logs show it."""
    return re.sub(r"\b[0-9a-f]{64}\b", lambda m: short(m.group(0)), text)


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
    kind: str = "member"  # member | guest | agent | own_key | no_vine_id | phantom
    excluded: bool = False
    keycast_admin: bool = False  # Keycast's own admin account, left out by itself
    # an account Keycast made for a person who holds their own key (an agent: register's pass
    # names the agent's own key, Keycast minted a derived one when something asked for the
    # partner's token): not carried, the signer must not hold that key
    phantom: bool = False

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
    scope: str = "own"
    # the scope was not known from the row (an origin that is neither the belfry's own nor an
    # approved outside app's): carried as own and counted
    guessed: bool = False
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
    # vine_id -> "self" | "belfry": the register's belfry_key_custody
    custody: dict[str, str] = field(default_factory=dict)
    # people of the register with their own key and no Keycast account (agents): vine_id -> key
    no_account: dict[str, str] = field(default_factory=dict)

    def by_user(self) -> dict[str, list[Session]]:
        out: dict[str, list[Session]] = {}
        for s in self.sessions:
            out.setdefault(s.user, []).append(s)
        return out

    def included(self) -> list[Account]:
        return [a for a in self.accounts if not a.excluded and not a.phantom]


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


def outside_origins(svc: Any) -> set[str]:
    """The origins of the outside apps an admin approved (apps.state), as Keycast stores them
    (``redirect_origin``: scheme, host and port of the app's redirect URI). Raises
    :class:`MigrationError` when the approvals cannot be read (the community relay does not
    answer): without them every outside app would be guessed own, with an own app's powers."""
    from . import apps

    try:
        state = apps.state(svc)
    except Exception as exc:  # noqa: BLE001
        raise MigrationError(
            f"the apps' approvals could not be read ({type(exc).__name__}): nothing is "
            "planned without them (an outside app would be taken for one of the belfry's own)"
        ) from exc
    return {
        apps.origin_of(uri) for uri, a in state.items() if a.get("approved") and apps.origin_of(uri)
    }


def scope_of(svc: Any, origin: str, approved: set[str] | None) -> tuple[str, bool]:
    """(scope, guessed) of a Keycast session, from its ``redirect_origin`` (patched Keycast at
    fe9c646, read in the source):

    - ``""``: ``POST /api/user/bunker/create`` without an origin (auth.rs:1812), which is every
      session the onboarding service makes (clients/keycast.create_bunker never sends one): the
      chat's e-mail sign-in, the phone apps' native sign-in, guests, tickets, site sessions,
      the setup page. **own**.
    - ``https://<host>`` with the belfry's domain or a host under it: the OAuth flow of one of
      its own apps (the chat on chat.<domain>, earlier on the apex, the suite). **own**.
    - the origin of an outside app an admin approved: **outside**.
    - ``http://…`` (a test app on localhost, or an outside app): **outside**: no app of the
      belfry is served over plain HTTP.
    - anything else (``nostrconnect://<client>`` of nostr-login, an https origin nobody
      approved): **own, guessed**, counted, and blocking verify unless accepted by number
      (``--accept-guessed N``). A chat session carried as outside would lock its member out
      of their sealed history; an outside app keeping own powers is the lesser harm once
      somebody has looked at the list. Approvals that cannot be read stop the plan
      (:func:`outside_origins`); APPROVED None is only for callers that read none.

    The import never turns a connection the signer has as ``outside`` into ``own`` (the signer
    refuses to widen a row in its sync): a later guess cannot give an outside app powers."""
    origin = (origin or "").strip().rstrip("/").lower()
    if not origin:
        return "own", False
    from . import apps

    if origin in {o.lower() for o in apps.NATIVE_APPS}:
        return "own", False
    domain = str(getattr(svc.settings, "nip05_domain", "") or "").lower().strip(".")
    host = ""
    if origin.startswith("https://"):
        host = origin[len("https://") :].split("/", 1)[0].split(":", 1)[0]
    if domain and host and (host == domain or host.endswith("." + domain)):
        return "own", False
    if approved is not None and origin in {o.lower().rstrip("/") for o in approved}:
        return "outside", False
    if origin.startswith("http://"):
        # no app of a belfry is served over plain HTTP (the chat, the suite and the phone apps'
        # pages are https or native): such a session is a test app (localhost) or an outside
        # one. Carried as outside, it keeps the outside limits; nothing of the belfry's own is
        # lost by that, and nobody has to accept a guess for it.
        return "outside", False
    return "own", True


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
    keycast_admin: str = "",
) -> Plan:
    """What a migration of DOC (``{"accounts": [...], "sessions": [...], "now"}``: the rows of
    docs/own-signer.md §6) would do, checked against the register. Reads only."""
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), list):
        raise MigrationError("the rows are not {accounts: [...], sessions: [...]}")
    now = int(now or _int(doc.get("now")) or time.time())
    excluded = {_hex(e) for e in exclude} - {""}
    # Keycast's own admin (KEYCAST_ADMIN_PUBKEY, made by every install with Keycast): an account
    # without a vine id that nobody signs in with; left out without being named (Friedger's
    # review of #8: every migration stopped on it)
    keycast_admin = _hex(keycast_admin)
    problems: list[dict[str, Any]] = []

    def problem(kind: str, **more: Any) -> None:
        problems.append({"problem": kind, **more})

    try:
        people = signerguard.members(svc)
        tiers = _tiers(svc)
    except Exception as exc:  # noqa: BLE001
        raise MigrationError(f"the register could not be read: {type(exc).__name__}") from exc
    register = {m["vine_id"]: m["pubkey"] for m in people}
    custody = {m["vine_id"]: m["custody"] for m in people}
    own_people = {m["vine_id"] for m in people if m.get("own")}
    own_pass = {m["vine_id"] for m in people if m.get("own_pass")}

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
            excluded=pubkey in excluded
            or (bool(keycast_admin) and pubkey == keycast_admin and not vine),
        )
        a.keycast_admin = bool(keycast_admin) and pubkey == keycast_admin and not vine
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
        if have and have != pubkey and vine in own_pass and not a.own:
            # Keycast holds a key for a person who has their own (an agent: something asked for
            # the partner's token and Keycast minted one). Nobody signs in on it; it is not
            # carried, and the signer must never hold it.
            a.kind, a.phantom = "phantom", True
            continue
        if have and have == pubkey and (custody.get(vine) == "self") != a.own:
            # the register and Keycast disagree about who holds the key
            problem(
                "register_custody",
                vine_id=vine,
                keycast="own" if a.own else "held",
                register=custody.get(vine),
            )
        if have and have != pubkey:
            problem(
                "register_has_another_key",
                vine_id=vine,
                keycast=short(pubkey),
                register=short(have),
            )
    no_account: dict[str, str] = {}
    for vine, key in register.items():
        if vine in vines:
            if accounts[vines[vine]].phantom:
                no_account[vine] = key
            continue
        if vine in own_people:
            # an agent (or someone) with their own key that Keycast never had an account for
            # (register._agent_pass, accept_agent_guest): nothing to carry, not a problem
            no_account[vine] = key
            continue
        # a person the register expects in the signer and Keycast does not have
        problem("member_not_in_keycast", vine_id=vine, register=short(key))

    approved = outside_origins(svc)
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
        s.scope, s.guessed = scope_of(svc, s.origin, approved)
        sessions.append(s)
        if acct.phantom and s.live:
            problem("phantom_with_sessions", vine_id=acct.vine_id, bunker=short(bunker))
        if acct.excluded or acct.phantom or not s.carried:
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

    def out(a: Account) -> bool:
        return a.excluded or a.phantom

    owners = key_owners(
        {k: a for k, a in accounts.items() if not out(a)},
        [s for s in sessions if not out(accounts[s.user])],
    )
    for s in sessions:
        acct = accounts[s.user]
        if out(acct) or not s.carried or not s.client:
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
        custody=custody,
        no_account=no_account,
    )


def _tiers(svc: Any) -> dict[int, str]:
    """partner -> ``agent`` or ``guest`` when every pass they have is one (for the counts)."""
    out: dict[int, set[str]] = {}
    from .core import pass_fields

    # guest_agent is a field of Belfry's own register only: Odoo answers Invalid field
    fields = pass_fields(svc, "partner_id", "tier", "guest_agent")
    for r in svc.odoo.search_read("belfry.pass", [], fields):
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
    guessed = 0
    for s in p.sessions:
        states[s.state] = states.get(s.state, 0) + 1
        if s.live:
            scopes[s.scope] += 1
        if s.carried and s.guessed:
            guessed += 1
    inc = p.included()
    return {
        "accounts": len(p.accounts),
        "excluded": len(p.accounts) - len(inc),
        "keycast_admin_excluded": sum(1 for a in p.accounts if a.keycast_admin),
        "by_kind": dict(sorted(kinds.items())),
        "with_username": sum(1 for a in inc if a.username),
        "suspended": sum(1 for a in inc if a.status != "active"),
        "register_members": len(p.register),
        "sessions": len(p.sessions),
        "sessions_by_state": dict(sorted(states.items())),
        "live_by_scope": scopes,
        "scope_guessed": guessed,
        "own_key_no_account": len(p.no_account),
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
            # the signer's messages name vine ids, row indexes and public keys (no secrets):
            # the keys are shortened like everywhere else in the reports
            text = shorten_keys(str(data.get("message") or ""))[:200]
            return f"{data.get('error') or r.status_code}: {text}"
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

    def move_own(self, vine_id: str, pubkey: str) -> dict[str, Any]:
        """``POST …/own-key``: the account's key is the member's own from now on (Keycast says
        so). Inside the migration's window a held key it replaces is archived by the signer
        (until the cleanup); outside it the signer drops it. The account, or ``{"error"}``."""
        r = self._call("POST", self._path(vine_id) + "/own-key", {"pubkey": pubkey})
        if r.status_code != 200:
            return {"error": self._error(r)}
        return dict(r.json())

    def revoke(self, vine_id: str, bunker_pubkey: str) -> str:
        """Revokes the account's connection with this bunker key. "" or what was refused."""
        r = self._call(
            "POST", self._path(vine_id) + "/connections/revoke", {"bunker_pubkey": bunker_pubkey}
        )
        return "" if r.status_code == 200 else self._error(r)

    def import_skipped(self) -> list[dict[str, Any]] | None:
        """The sessions imports left out because their bunker key does not derive, or None from
        a signer that does not list them."""
        r = self._call("GET", "/import/skipped")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return list(r.json().get("skipped") or [])

    def set_username(self, vine_id: str, username: str) -> str:
        r = self._call("PUT", self._path(vine_id) + "/username", {"username": username})
        return "" if r.status_code == 200 else self._error(r)

    def set_status(self, vine_id: str, status: str) -> str:
        r = self._call("POST", self._path(vine_id) + "/status", {"status": status})
        return "" if r.status_code == 200 else self._error(r)

    def import_connections(
        self, vine_id: str, rows: list[dict[str, Any]], sync: bool = False
    ) -> dict[str, Any]:
        """``POST /import/connections``: ``{imported, existing, revoked, updated, released}``
        or ``{"error"}``. SYNC: rows that are there are brought to these (the signer honours
        it only inside the migration's window, :meth:`set_window`, and refuses it outside)."""
        body: dict[str, Any] = {"vine_id": vine_id, "connections": rows}
        if sync:
            body["sync"] = True
        r = self._call("POST", "/import/connections", body)
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

    def set_window(self, on: bool) -> bool | None:
        """The migration's window (``POST /settings {migration}``): opened by the import,
        closed by the switch. Returns it as it now is, or None from a signer without one."""
        r = self._call("POST", "/settings", {"migration": bool(on)})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise MigrationError(f"the signer refused the migration's window: {self._error(r)}")
        return bool(r.json().get("migration"))

    def delete_archived_keys(self) -> int:
        """``DELETE /archived-keys`` (the cleanup): how many archived held keys went."""
        r = self._call("DELETE", "/archived-keys")
        if r.status_code != 200:
            raise MigrationError(f"the signer kept its archived keys: {self._error(r)}")
        return int(r.json().get("deleted") or 0)

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
    """One session as ``POST /import/connections`` takes it. An outside app's session keeps
    ``decrypt``: Keycast let an approved outside app decrypt, so the consent is the one the
    app has today (docs/own-signer.md §6, "scope"); the belfry's own apps decrypt anyway."""
    row: dict[str, Any] = {
        "bunker_pubkey": s.bunker,
        "keycast_secret_hash": s.secret_hash,
        "app": s.app,
        "scope": s.scope,
        "decrypt": s.scope == "outside",
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
    """The plan into the signer, as a **sync**: run again, it brings the signer to what Keycast
    holds now. Refuses to start while the plan has problems. Returns counts and ``errors``
    (each names an account by its vine id).

    Between the first import and the switch the belfry runs on Keycast and people sign out,
    take their own key, move to a new one, or a shared client key changes hands. The import
    opens the signer's migration window before it writes and asks for a sync, and inside that
    window the signer treats the import's rows as the truth for rows it has (``POST
    /import/connections`` with ``sync``: revoked or live, client key, name, origin, scope and
    consent, except that an ``outside`` row never becomes ``own``). On top of that:

    - an account Keycast now shows with the member's own key (or a new own key) is moved to it
      (``POST …/own-key``): inside the window a key the signer held is archived sealed (the
      cleanup deletes the archive), and the report lists the account (``custody_moved``);
    - a held key the signer has that is not Keycast's key is an error, never an overwrite;
    - sessions run in two passes: first the rows that carry no client key (one that went to
      another account is released), then all of them, so a key that changed hands is free when
      the account that keeps it is written;
    - a live session in the signer that Keycast no longer has at all (deleted) is revoked;
    - a row whose bunker key the formula does not give is skipped and listed (``skipped``),
      the account's other rows go in."""
    if p.problems:
        raise MigrationError(
            f"{len(p.problems)} problems in what Keycast holds: nothing was imported "
            "(signer-migrate plan lists them)"
        )
    # the migration's window: the signer follows the import's rows (sync) and archives a held
    # key replaced by an own key only inside it, never because new accounts are closed
    window = signer.set_window(True) if hasattr(signer, "set_window") else None
    sync = window is True
    out: dict[str, Any] = {
        "accounts": {"derived": 0, "imported": 0, "exists": 0, "own_key": 0},
        "custody_moved": [],
        "usernames": 0,
        "suspended": 0,
        "sessions": {
            "imported": 0,
            "existing": 0,
            "revoked": 0,
            "updated": 0,
            "released": 0,
            "revoked_gone": 0,
        },
        "skipped": [],
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
                        "error": f"{res.get('error')}: "
                        + shorten_keys(str(res.get("message") or ""))[:200],
                    }
                )
    for a in accounts:
        if not a.own:
            continue
        have = signer.account(a.vine_id)
        if have is not None and (
            str(have.get("pubkey") or "").lower() != a.pubkey
            or str(have.get("custody") or "") != "own"
        ):
            # Keycast: the member took their own key (or moved to a new one) since the first
            # import. The signer follows; a key it held is archived, sealed.
            moved = signer.move_own(a.vine_id, a.pubkey)
            if "error" in moved:
                failed.add(a.vine_id)
                errors.append({"step": "own_key", "vine_id": a.vine_id, "error": moved["error"]})
                continue
            out["custody_moved"].append(
                {
                    "vine_id": a.vine_id,
                    "from": str(have.get("custody") or ""),
                    "from_key": short(have.get("pubkey")),
                    "to_key": short(a.pubkey),
                    "archived_keys": int(moved.get("archived_keys") or 0),
                }
            )
            out["accounts"]["own_key"] += 1
            continue
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
    order = [
        a
        for a in sorted(accounts, key=lambda a: (a.status != "active", a.vine_id))
        if a.vine_id not in failed and not a.own
    ]
    sess = out["sessions"]
    stopped: set[str] = set()

    def send(a: Account, sessions: list[Session]) -> int:
        """One account's rows in batches; how many were sent without an error."""
        sent = 0
        for run in batches_of(sessions, SESSION_BATCH):
            res = signer.import_connections(a.vine_id, [connection_row(s) for s in run], sync)
            if "error" in res:
                errors.append({"step": "sessions", "vine_id": a.vine_id, "error": res["error"]})
                stopped.add(a.vine_id)
                return sent
            for k in ("imported", "existing", "revoked", "updated", "released"):
                sess[k] += int(res.get(k) or 0)
            for i in res.get("skipped") or []:
                if isinstance(i, int) and 0 <= i < len(run):
                    out["skipped"].append(
                        {"vine_id": a.vine_id, "bunker": short(run[i].bunker), "app": run[i].app}
                    )
            sent += len(run)
        return sent

    # pass 1: rows without a client key (a key that went to another account is released here)
    first: dict[str, int] = {}
    for a in order:
        keyless = [s for s in by_user.get(a.pubkey, []) if s.carried and not s.client]
        if keyless:
            first[a.vine_id] = send(a, keyless)
    # pass 2: every carried row. It sends pass 1's rows again, and the signer counts them
    # "there already": those are taken off again, but never more than pass 2 counted so (the
    # count is the rows' first answer, pass 1's; display only)
    for a in order:
        if a.vine_id in stopped:
            continue
        rows = [s for s in by_user.get(a.pubkey, []) if s.carried]
        before = sess["existing"]
        send(a, rows)
        sess["existing"] -= min(first.get(a.vine_id, 0), sess["existing"] - before)
        if a.vine_id in stopped:
            continue
        # a live session the signer has that Keycast no longer has at all: revoked
        known = {s.bunker for s in rows}
        for c in signer.connections(a.vine_id):
            bunker = str(c.get("bunker_pubkey") or "")
            if bunker and bunker not in known:
                refused = signer.revoke(a.vine_id, bunker)
                if refused:
                    errors.append({"step": "revoke", "vine_id": a.vine_id, "error": refused})
                else:
                    sess["revoked_gone"] += 1
    return out


def batches_of(sessions: list[Session], most: int) -> Iterable[list[Session]]:
    """SESSIONS in runs that :func:`batches` would make of their rows."""
    rows = [connection_row(s) for s in sessions]
    i = 0
    for run in batches(rows, most):
        yield sessions[i : i + len(run)]
        i += len(run)


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
    accept_unverified: int = 0,
    accept_skipped: int = 0,
    accept_guessed: int = 0,
) -> dict[str, Any]:
    """Everything a migration carries, compared: for every account the signer's key, Keycast's
    and the register's; custody (Keycast, the signer and the register's belfry_key_custody),
    username and state; for every carried session its bunker key, client key and scope among
    the signer's connections; the admins. Both sides are read on their own: no Keycast rows on
    a belfry whose register has people is a failure, and the account counts compared come from
    Keycast and from the signer's own list. Returns the report: ``ok`` is true only when
    ``mismatches`` is empty. Reads only.

    Blocking unless accepted by their exact number: sessions lost to a shared client key
    (ACCEPT_LOST), connected sessions Keycast does not verify (ACCEPT_UNVERIFIED: if Keycast
    served them after all, those members would be signed out), rows whose bunker key does not
    derive and were left out (ACCEPT_SKIPPED), admins without a person (ACCEPT_MISSING_STEWARDS),
    carried sessions whose scope was guessed own (ACCEPT_GUESSED: an outside app among them
    would keep an own app's powers).

    Every carried session is compared on its bunker key, client key, ``origin``, ``kind``
    (``nip46``) and, for the live ones, scope and ``decrypt`` (true exactly for outside apps).
    A row the signer has as ``outside`` that the plan guessed ``own`` is the sync's rule (it
    never widens a row) and counts as checked (``kept_outside``).

    KEYS_ONLY is for a belfry that already runs on its signer: people have signed in and out,
    joined, been renamed since, and Keycast's rows are a picture of the past. What must hold
    for ever is checked (every account Keycast had is in the signer with the same key, or with
    the member's own key the register knows they moved to, and so is every person of the
    register); usernames, states, sessions and admins are not, and a member Keycast never had
    is not a problem."""
    mismatches: list[dict[str, Any]] = []

    def miss(kind: str, **more: Any) -> None:
        mismatches.append({"mismatch": kind, **more})

    # after the switch: people joined (not in Keycast), or took their own key in the signer
    # (the register follows; checked per account below against the signer)
    skip_problems = (
        {"member_not_in_keycast", "register_custody", "register_has_another_key"}
        if keys_only
        else set()
    )
    for prob in p.problems:
        if prob.get("problem") in skip_problems:
            continue  # joined, or moved to their own key, after the switch
        miss("problem", **prob)
    if p.register and not p.accounts:
        # the query read nothing (wrong tenant, an empty copy): never "nothing to compare"
        miss("keycast_empty", register=len(p.register))
    if keys_only:
        pass
    elif len(p.lost) != int(accept_lost or 0):
        for lost in p.lost:
            miss("session_lost", **lost)
        if not p.lost:
            miss("accept_lost_does_not_match", accepted=int(accept_lost), lost=0)
    included = p.included()
    inc_keys = {a.pubkey for a in included}
    unverified = [s for s in p.sessions if s.state == "unverified" and s.user in inc_keys]
    if not keys_only and len(unverified) != int(accept_unverified or 0):
        miss(
            "unverified_sessions",
            count=len(unverified),
            accepted=int(accept_unverified or 0),
            vine_ids=sorted({_acct(p, s.user).vine_id for s in unverified})[:50],
        )
    guessed = [s for s in p.sessions if s.carried and s.guessed and s.user in inc_keys]
    if not keys_only and len(guessed) != int(accept_guessed or 0):
        miss(
            "scope_guessed",
            count=len(guessed),
            accepted=int(accept_guessed or 0),
            vine_ids=sorted({_acct(p, s.user).vine_id for s in guessed})[:50],
        )
    listed_skipped = signer.import_skipped() if hasattr(signer, "import_skipped") else None
    skipped_bunkers = {str(r.get("bunker_pubkey") or "") for r in listed_skipped or []}
    skipped_rows = [
        s for s in p.sessions if s.carried and s.user in inc_keys and s.bunker in skipped_bunkers
    ]
    if not keys_only and len(skipped_rows) != int(accept_skipped or 0):
        for s in skipped_rows:
            miss(
                "session_skipped",
                vine_id=_acct(p, s.user).vine_id,
                bunker=short(s.bunker),
                app=s.app,
            )
        if not skipped_rows:
            miss("accept_skipped_does_not_match", accepted=int(accept_skipped), skipped=0)
    checked = {
        "accounts": 0,
        "keys_equal": 0,
        "usernames": 0,
        "live_sessions": 0,
        "revoked_sessions": 0,
        "own_key_no_account": 0,
        "kept_outside": 0,
    }
    unreadable = 0  # revoked rows of a signer that cannot list them
    found = 0  # Keycast's accounts the signer has
    by_user = p.by_user()
    for a in included:
        if not a.vine_id:
            continue  # a problem already
        checked["accounts"] += 1
        have = signer.account(a.vine_id)
        if have is None:
            miss("account_not_imported", vine_id=a.vine_id, keycast=short(a.pubkey))
            continue
        found += 1
        got = str(have.get("pubkey") or "").lower()
        reg = p.register.get(a.vine_id)
        got_own = str(have.get("custody") or "") == "own"
        reg_self = p.custody.get(a.vine_id) == "self"
        # after the switch a member may take their own key in the signer: the register follows
        moved = keys_only and got_own and reg_self and reg == got and got != a.pubkey
        if got != a.pubkey and not moved:
            miss("signer_key", vine_id=a.vine_id, keycast=short(a.pubkey), signer=short(got))
        if reg and reg != a.pubkey and not moved:
            miss("register_key", vine_id=a.vine_id, keycast=short(a.pubkey), register=short(reg))
        if (got == a.pubkey or moved) and (not reg or reg == got):
            checked["keys_equal"] += 1
        if got_own != (a.own or moved):
            miss("custody", vine_id=a.vine_id, signer=str(have.get("custody")))
        if a.vine_id in p.custody and got_own != reg_self:
            miss(
                "register_custody",
                vine_id=a.vine_id,
                signer=str(have.get("custody")),
                register=p.custody.get(a.vine_id),
            )
        if keys_only:
            continue
        if str(have.get("username") or "") != a.username:
            miss("username", vine_id=a.vine_id)
        elif a.username:
            checked["usernames"] += 1
        if str(have.get("status") or "") != a.status:
            miss("status", vine_id=a.vine_id, keycast=a.status, signer=str(have.get("status")))
        mine = [s for s in by_user.get(a.pubkey, []) if s.bunker not in skipped_bunkers]
        want = {s.bunker: s for s in mine if s.live and not a.own}
        live = {str(c.get("bunker_pubkey") or ""): c for c in signer.connections(a.vine_id)}
        # the revoked and expired rows came in revoked: their client keys stay this account's
        gone = {s.bunker: s for s in mine if s.carried and not s.live and not a.own}
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
            elif not _same_fields(a, bunker, s, c, miss, scope=False):
                pass
            else:
                checked["revoked_sessions"] += 1
        for bunker, s in want.items():
            c = live.get(bunker)
            if c is None:
                miss("session_missing", vine_id=a.vine_id, bunker=short(bunker), app=s.app)
            elif str(c.get("client_pubkey") or "").lower() != s.client:
                miss("session_client_key", vine_id=a.vine_id, bunker=short(bunker))
            elif not _same_fields(a, bunker, s, c, miss, scope=True):
                pass
            else:
                checked["live_sessions"] += 1
                if str(c.get("scope") or "") != s.scope:
                    checked["kept_outside"] += 1
        for bunker in live.keys() - want.keys():
            # nothing may be live in the signer that Keycast did not serve (a session made on
            # the signer before the switch, or a revoked row that came in live)
            miss("session_unexpected", vine_id=a.vine_id, bunker=short(bunker))
    # people with their own key and no Keycast account (agents): the signer holds no key for
    # them, either no account or a keyless one with that same key
    for vine, key in sorted(p.no_account.items()):
        have = signer.account(vine)
        if have is None or (
            str(have.get("custody") or "") == "own" and str(have.get("pubkey") or "").lower() == key
        ):
            checked["own_key_no_account"] += 1
        else:
            miss(
                "own_key_account",
                vine_id=vine,
                register=short(key),
                signer=short(have.get("pubkey")),
                custody=str(have.get("custody")),
            )
    # nothing in the signer that Keycast did not have (before the switch: nothing may have
    # made an account there but the import; after it, people join, so keys only)
    listing = signer.list_accounts() if hasattr(signer, "list_accounts") else None
    known = {a.vine_id for a in included if a.vine_id}
    if listing is not None and not keys_only:
        for acct in listing:
            vine = str(acct.get("vine_id") or "")
            if vine not in known and vine not in p.no_account:
                miss("account_unexpected", vine_id=vine, signer=short(acct.get("pubkey")))
    custody_counts: dict[str, int] = {}
    for acct in listing or []:
        k = str(acct.get("custody") or "unknown")
        custody_counts[k] = custody_counts.get(k, 0) + 1
    if listing is not None:
        checked["signer_accounts"] = len(listing)
        # counts from the two sides: Keycast's carried accounts, the signer's own list
        in_list = sum(1 for acct in listing if str(acct.get("vine_id") or "") in known)
        if in_list != len(known) or (not keys_only and found != len(known)):
            miss("account_count", keycast=len(known), signer_listed=in_list, signer_found=found)
    for vine, key in p.register.items():
        if vine in p.no_account:
            continue
        have = signer.account(vine)
        if have is None:
            miss("member_not_in_signer", vine_id=vine, register=short(key))
        elif str(have.get("pubkey") or "").lower() != key:
            miss("member_key", vine_id=vine, register=short(key), signer=short(have.get("pubkey")))
    # "imported" keys (random, from before the seed) live only in signer-data and its backups:
    # cleanup asks for a backup that holds them (migrate-to-own-signer.sh)
    report: dict[str, Any] = {
        "checked": checked,
        "keys_only": bool(keys_only),
        "signer_custody": custody_counts,
    }
    if authentik is not None and not keys_only:
        report["stewards"] = _verify_stewards(svc, authentik, miss, accept_missing_stewards)
    report.update(
        {
            "at": int(time.time()),
            "digest": p.digest,
            "counts": counts(p),
            "lost": p.lost,
            "accepted_lost": int(accept_lost or 0),
            "unverified": len(unverified),
            "accepted_unverified": int(accept_unverified or 0),
            "skipped": [
                {"vine_id": _acct(p, s.user).vine_id, "bunker": short(s.bunker), "app": s.app}
                for s in skipped_rows
            ],
            "accepted_skipped": int(accept_skipped or 0),
            "scope_guessed": [
                {"vine_id": _acct(p, s.user).vine_id, "bunker": short(s.bunker), "app": s.app}
                for s in guessed
            ][:200],
            "accepted_guessed": int(accept_guessed or 0),
            "mismatches": mismatches,
            "ok": not mismatches,
            "not_verifiable": {
                # revoked rows of a signer that cannot list them (before GET …?all=true)
                "revoked_sessions": unreadable,
                # whether the signer could list its accounts (GET /accounts)
                "accounts_list": listing is None,
                # whether the signer could list the rows imports skipped (GET /import/skipped)
                "skipped_list": listed_skipped is None,
            },
        }
    )
    return report


def _same_fields(
    a: Account, bunker: str, s: Session, c: dict[str, Any], miss: Any, scope: bool
) -> bool:
    """Origin, kind, and (SCOPE) scope and consent of one carried session against the signer's
    row; each difference reported. True when all agree."""
    ok = True
    want_origin = s.origin[:200].strip()
    if str(c.get("origin") or "") != want_origin:
        miss("session_origin", vine_id=a.vine_id, bunker=short(bunker))
        ok = False
    if str(c.get("kind") or "") != "nip46":
        miss("session_kind", vine_id=a.vine_id, bunker=short(bunker), signer=str(c.get("kind")))
        ok = False
    if not scope:
        return ok
    got = str(c.get("scope") or "")
    # the sync never widens a row: outside where the plan only guessed own is kept
    effective = "outside" if got == "outside" and s.scope == "own" and s.guessed else s.scope
    if got != effective:
        miss(
            "session_scope",
            vine_id=a.vine_id,
            bunker=short(bunker),
            keycast=s.scope,
            signer=got,
        )
        return False
    if c.get("decrypt") is not (effective == "outside"):
        miss(
            "session_decrypt",
            vine_id=a.vine_id,
            bunker=short(bunker),
            want=effective == "outside",
            signer=c.get("decrypt"),
        )
        ok = False
    return ok


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


def admins_overview(svc: Any, emails: Iterable[str] | None = None) -> dict[str, Any]:
    """Who is (``emails``: Authentik's stewards group, for check) or is marked (the register's
    flag, after the import) an admin, and which of them have no active pass, with the addresses
    masked. Information for the operator: on its own signer an admin signs in whatever their
    pass (apps.signin_member), but they are left off the relay and the chat until a pass is
    active again (``belfry add-steward`` gives one that lasts)."""
    if emails is None:
        emails = [str(r["email"]) for r in stewards._flagged(svc, ["email"]) if r.get("email")]
    wanted = sorted({e.strip().lower() for e in emails if e and e.strip()})
    active: set[int] = set()
    for r in svc.odoo.search_read("belfry.pass", [["state", "=", "active"]], ["partner_id"]):
        pid = signerguard._m2o(r.get("partner_id"))
        if pid:
            active.add(pid)
    without: list[str] = []
    for e in wanted:
        people = stewards._partners(svc, e)
        if people and not any(int(p["id"]) in active for p in people):
            without.append(e)
    return {
        "admins": [mask(e) for e in wanted],
        "without_active_pass": [mask(e) for e in without],
    }


def say_admins(say: Any, overview: dict[str, Any], becomes: bool) -> None:
    names = ", ".join(overview["admins"]) or "nobody"
    say(f"admins{' after the move' if becomes else ''} ({len(overview['admins'])}): {names}")
    if overview["without_active_pass"]:
        say(
            f"admins without an active pass ({len(overview['without_active_pass'])}): "
            + ", ".join(overview["without_active_pass"])
            + ". They can still sign in to the console on the own signer; "
            "'belfry add-steward <address>' gives them a pass that lasts (relay, chat)."
        )


# ── rollback ────────────────────────────────────────────────────────────────────────────────
def carry_back(p: Plan, signer: Any) -> dict[str, Any]:
    """What a rollback writes back into Keycast so that it does not undo what happened on the
    signer since the switch: ``{"revoke": [[bunker_pubkey, revoked_at]], "suspend":
    [pubkey]}``: sessions Keycast still has live that the signer has revoked, and accounts
    Keycast has active that the signer has suspended. Full keys: this goes down a pipe into
    the SQL of migrate-to-own-signer.sh, never to a report. Sessions opened on the signer
    after the switch cannot go back (Keycast has no row for them); reactivations are not
    carried back (a suspension Keycast still has stays)."""
    revoke: list[list[Any]] = []
    suspend: list[str] = []
    by_user = p.by_user()
    for a in p.included():
        if not a.vine_id or a.own:
            continue
        have = signer.account(a.vine_id)
        if have is None:
            continue
        if a.status == "active" and str(have.get("status") or "") == "suspended":
            suspend.append(a.pubkey)
        live_in_keycast = {s.bunker for s in by_user.get(a.pubkey, []) if s.live}
        if not live_in_keycast:
            continue
        for c in signer.connections(a.vine_id, all=True):
            bunker = str(c.get("bunker_pubkey") or "").lower()
            at = c.get("revoked_at")
            if bunker in live_in_keycast and at and HEX.fullmatch(bunker):
                revoke.append([bunker, int(at)])
    return {"revoke": revoke, "suspend": suspend}


# ── the command line ────────────────────────────────────────────────────────────────────────
def summary(c: dict[str, Any]) -> list[str]:
    kinds = ", ".join(f"{n} {k}" for k, n in c["by_kind"].items()) or "none"
    states = ", ".join(f"{n} {k}" for k, n in c["sessions_by_state"].items()) or "none"
    return [
        f"accounts in Keycast: {c['accounts']} ({kinds}); excluded on purpose: {c['excluded']}"
        + (
            f" (of them Keycast's own admin, KEYCAST_ADMIN_PUBKEY: {c['keycast_admin_excluded']},"
            " left out by itself)"
            if c.get("keycast_admin_excluded")
            else ""
        ),
        f"with a username: {c['with_username']}; suspended: {c['suspended']}; "
        f"people with a key in the register: {c['register_members']}",
        f"sessions: {c['sessions']} ({states})",
        f"live sessions carried: {c['live_by_scope']['own']} of the belfry's own apps, "
        f"{c['live_by_scope']['outside']} of outside apps; scope guessed (carried as own): "
        f"{c['scope_guessed']}; lost to a shared client key: {c['lost']}",
        f"people with their own key and no Keycast account (agents; nothing to carry): "
        f"{c['own_key_no_account']}; Keycast accounts of theirs left behind: "
        f"{c['by_kind'].get('phantom', 0)}",
        f"problems: {c['problems']}",
    ]


def cli(args: Any, svc: Any, settings: Any, stdin: Any, out: Any, err: Any) -> int:
    """``python -m belfry_onboarding signer-migrate
    plan|import|verify|close-window|open|carry-back|drop-archive`` with the rows on stdin.
    The report (JSON, one line) goes to stdout, what a person reads to stderr.
    Exit 0: clean; 1: problems or mismatches; 2: a source that does not answer."""

    def say(line: str) -> None:
        print(line, file=err)

    mode = str(getattr(settings, "signer", "") or "keycast").strip().lower()
    if args.phase == "import" and mode == "belfry":
        # after the switch the signer is the truth: an import would bring it back to Keycast's
        # past (sign-outs, own-key moves and suspensions made since undone)
        say("signer-migrate: this belfry runs on its own signer (SIGNER=belfry): no import")
        return 2
    if args.phase == "carry-back" and mode != "belfry":
        # what it reads is what happened on the signer since the switch: nothing before it
        say("signer-migrate: carry-back is for a rollback after the switch (SIGNER=belfry)")
        return 2
    try:
        doc = json.load(stdin)
        p = plan(
            svc,
            doc,
            exclude=args.exclude or (),
            keycast_admin=str(getattr(args, "keycast_admin", "") or ""),
        )
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
        doc_out: dict[str, Any] = {
            "counts": c,
            "problems": p.problems,
            "lost": p.lost,
            "digest": p.digest,
        }
        if not args.no_stewards and getattr(settings, "authentik_token", ""):
            # who becomes an admin: Authentik's stewards group, as the import will mark it
            try:
                from .clients.authentik import AuthentikClient

                group = AuthentikClient(
                    settings.authentik_url, settings.authentik_token
                ).group_member_emails(settings.steward_group)
                doc_out["admins"] = admins_overview(svc, group)
                say_admins(say, doc_out["admins"], becomes=True)
            except Exception as exc:  # noqa: BLE001 — information only; check reads it again
                say(f"admins: Authentik's stewards group not readable ({type(exc).__name__})")
        json.dump(doc_out, out)
        out.write("\n")
        return 1 if p.problems else 0
    try:
        signer = SignerAdmin(settings.signer_url, settings.signer_token)
        health = signer.health()
    except (MigrationError, httpx.HTTPError) as exc:
        say(f"signer-migrate: the signer does not answer: {type(exc).__name__}")
        return 2
    # carry-back only reads what the signer holds (a rollback after a reboot finds the seed
    # locked, and must still go back); every other phase writes or compares derived keys
    if args.phase != "carry-back" and health.get("seed") not in (True, "unlocked"):
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
        if args.phase == "close-window":
            # the switch, once the verify that decides is clean: no import syncs any more, and a
            # move to an own key drops the held key again
            window = signer.set_window(False)
            say(
                "the migration's window: " + ("none on this signer" if window is None else "closed")
            )
            json.dump({"migration": window}, out)
            out.write("\n")
            return 0
        if args.phase == "drop-archive":
            # the cleanup, after its guards: the held keys archived during the window go
            if mode != "belfry":
                say("signer-migrate: drop-archive is for the cleanup after the switch")
                return 2
            signer.set_window(False)
            n = signer.delete_archived_keys()
            say(f"archived held keys deleted: {n}")
            json.dump({"deleted": n}, out)
            out.write("\n")
            return 0
        if args.phase == "open":
            # the belfry runs on its signer and the verify after the switch is clean: people
            # may join again (docs/own-signer.md, "Moving a belfry"); the window is over
            signer.set_window(False)
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
                f"{s['existing']} there already, {s['updated']} brought to Keycast's state "
                f"({s['released']} client keys handed to the account that keeps them), "
                f"{s['revoked_gone']} revoked because Keycast no longer has them; "
                f"{len(res['skipped'])} left out (bunker key does not derive)"
            )
            for m in res["custody_moved"][:50]:
                say("  moved to the member's own key: " + json.dumps(m, sort_keys=True))
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
        if args.phase == "carry-back":
            back = carry_back(p, signer)
            say(
                f"to carry back to Keycast: {len(back['revoke'])} sessions signed out and "
                f"{len(back['suspend'])} accounts suspended on the signer since the switch"
            )
            json.dump(back, out)
            out.write("\n")
            return 0
        report = verify(
            p,
            signer,
            svc,
            authentik,
            args.accept_lost_sessions,
            args.accept_missing_stewards,
            keys_only=bool(getattr(args, "keys_only", False)),
            accept_unverified=int(getattr(args, "accept_unverified", 0) or 0),
            accept_skipped=int(getattr(args, "accept_skipped_rows", 0) or 0),
            accept_guessed=int(getattr(args, "accept_guessed", 0) or 0),
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
    try:
        report["admins"] = admins_overview(svc)  # the register's flag: information only
        say_admins(say, report["admins"], becomes=False)
    except Exception as exc:  # noqa: BLE001 — a count for the operator, never a mismatch
        say(f"admins: the register's admins not readable ({type(exc).__name__})")
    say("verify: clean" if report["ok"] else f"verify: {len(report['mismatches'])} mismatches")
    json.dump(report, out)
    out.write("\n")
    return 0 if report["ok"] else 1
