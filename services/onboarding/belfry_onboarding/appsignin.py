"""Keycast's sign-in for apps, answered by Belfry itself (``SIGNER=belfry``).

The apps of the Open Source Village suite (calendar, vote, wallet, groups, the village pages) and
any app written against "Sign in with …" of a Keycast belfry talk to five addresses on
``apps.<domain>``. On a belfry without Keycast the proxy sends them here, and they keep working
without a change:

    GET  /api/oauth/authorize   → the belfry's own sign-in, ``belfry.<domain>/connect`` (connect.py)
    POST /api/oauth/token       {grant_type: authorization_code | refresh_token, …}
    POST /api/oauth/revoke      {token}
    GET  /api/auth/oidc/logout  ?return_to=
    POST /api/nostr             Bearer access token, {method, params} → {result}

What is Belfry's own here, on purpose (the review of PR #7's first version of this):

- **Who may ask**: only an app in the registry (apps.py: served by this belfry, or approved by
  an admin). The sign-in itself is connect.py's: e-mail and code or a known browser, PKCE S256
  required, a code that works once within five minutes. An address nobody approved is never
  redirected to; the pages name an app by its address or the name an admin gave it, never by
  text from the link.
- **Tokens are random and kept only as HMACs**, under a key per kind of token (HKDF of the
  session secret with a label each: an access token is never a refresh token, and the table
  alone opens nothing). An access token lives an hour; a refresh token thirty days, works once
  and is replaced each time; one used twice ends the whole sign-in.
- **Every use is checked against the table**: revoked, expired, signed out in the signer, or
  the member's pass gone, and the token is refused. ``/api/oauth/revoke`` and the logout end
  them here and in the signer.
- **What an outside app may have signed** is what remoteapps.py allows a remote app (one rule
  set, ``remoteapps.allowed``): no NIP-98 for the belfry's hosts, no AUTH for its relays, no
  kind 24133, no NIP-29 moderation, no app data named ``belfry:…`` (30078), no label in a
  ``belfry/…`` namespace (1985). It encrypts; it decrypts only when the member said so for that
  app (the grant's ``decrypt``; nothing sets it yet). The belfry's own apps sign anything.
- **Browser sessions end on the server**: a session of ``belfry.<domain>`` carries an id, and a
  logout writes it down (``ended``), so a copied cookie is worth nothing afterwards.

**A grant is not a NIP-46 connection.** The token exchange also opens a connection in the signer
and hands the app its bunker URL. On Belfry's own signer that connection is of kind ``http``:
an app may connect to it within ten minutes, and one that never does (the wallet signs only
through ``/api/nostr``) keeps it all the same; only its secret ends. So the grant's session
stays in the signer's list, with the app's origin, and the table here decides whether a token
works. :func:`with_grants` still adds a grant whose connection the signer does not list (a
sign-in made before connections had a kind, or a signer that dropped it), under the same id
(the connection's bunker key), and signing a session out ends the grant with it
(:func:`session_revoked`): every place that lists or revokes sessions calls these two
(discovery.py, console.py, sales.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import apps, community, connect, console, remoteapps, stewards
from .core import Services

log = logging.getLogger(__name__)

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
GRANTS_PER_MEMBER = 50
RPC_METHODS = (
    "get_public_key",
    "sign_event",
    "nip44_encrypt",
    "nip44_decrypt",
    "nip04_encrypt",
    "nip04_decrypt",
    "derive_secret",
)
# Keycast patch 0011: the village wallet derives its backup seed and its nutzap key from the
# member's key. Only the wallet this belfry serves itself, only these purposes.
DERIVE = {"wallet": ("cashu-wallet-v1", "cashu-nutzap-v1")}
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class SignInError(Exception):
    """An OAuth error answer: ``{"error", "error_description"}`` with a status."""

    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status = status
        self.error = error
        self.description = description


def enabled(svc: Services) -> bool:
    """Only a belfry on its own signer answers for Keycast; with Keycast the proxy sends these
    paths to Keycast and this module is not reached (404 if it is)."""
    return stewards.own(svc) and bool(svc.settings.pass_page_secret)


# ── keys: one per kind of token ─────────────────────────────────────────────────────────────
def key_for(settings: Any, label: str) -> bytes:
    secret = str(getattr(settings, "pass_page_secret", "") or "")
    if len(secret) < 16:
        raise SignInError(503, "temporarily_unavailable", "the session secret is not set")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"belfry/appsignin/v1",
        info=label.encode(),
    ).derive(secret.encode())


def mark(settings: Any, kind: str, token: str) -> str:
    """What the table keeps of a token: its HMAC under the key of its kind."""
    return hmac.new(key_for(settings, f"token/{kind}"), token.encode(), hashlib.sha256).hexdigest()


# ── the table ───────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY,
                partner INTEGER NOT NULL,
                pubkey TEXT NOT NULL,
                client_id TEXT NOT NULL,
                uri TEXT NOT NULL,
                scope TEXT NOT NULL,
                bunker TEXT NOT NULL,
                access TEXT NOT NULL,
                access_until INTEGER NOT NULL,
                refresh TEXT NOT NULL,
                refresh_until INTEGER NOT NULL,
                spent TEXT NOT NULL DEFAULT '',
                created INTEGER NOT NULL,
                used INTEGER,
                revoked INTEGER,
                label TEXT NOT NULL DEFAULT '',
                decrypt INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS grants_access ON grants (access);
            CREATE INDEX IF NOT EXISTS grants_refresh ON grants (refresh);
            CREATE INDEX IF NOT EXISTS grants_partner ON grants (partner);
            CREATE TABLE IF NOT EXISTS ended (sid TEXT PRIMARY KEY, until INTEGER NOT NULL);
            """
        )
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(grants)")}
        if "label" not in cols:  # a table made before grants carried their session's name
            self._db.execute("ALTER TABLE grants ADD COLUMN label TEXT NOT NULL DEFAULT ''")
        if "decrypt" not in cols:
            self._db.execute("ALTER TABLE grants ADD COLUMN decrypt INTEGER NOT NULL DEFAULT 0")

    def add(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        with self._lock:
            self._db.execute(
                f"INSERT INTO grants ({cols}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )

    def by(self, column: str, value: str) -> dict[str, Any] | None:
        if column not in ("id", "access", "refresh"):
            raise ValueError(column)
        with self._lock:
            row = self._db.execute(f"SELECT * FROM grants WHERE {column} = ?", (value,)).fetchone()
        return dict(row) if row else None

    def spent(self, value: str) -> dict[str, Any] | None:
        """The grant a refresh token belonged to before it was replaced."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM grants WHERE instr(spent, ?) > 0", (f"|{value}|",)
            ).fetchone()
        return dict(row) if row else None

    def rotate(self, gid: str, old: str, access: str, refresh: str, now: int) -> bool:
        """Replace the grant's tokens, only if OLD is still its refresh token (two requests at
        once: one wins, the other is a reuse)."""
        with self._lock:
            done = self._db.execute(
                "UPDATE grants SET access = ?, access_until = ?, refresh = ?, refresh_until = ?, "
                "spent = substr(spent || '|' || ? || '|', -1400), used = ? "
                "WHERE id = ? AND refresh = ? AND revoked IS NULL",
                (access, now + ACCESS_SECONDS, refresh, now + REFRESH_SECONDS, old, now, gid, old),
            )
            return done.rowcount == 1

    def touch(self, gid: str, now: int) -> None:
        with self._lock:
            self._db.execute("UPDATE grants SET used = ? WHERE id = ?", (now, gid))

    def revoke(self, gid: str, now: int) -> bool:
        with self._lock:
            done = self._db.execute(
                "UPDATE grants SET revoked = ? WHERE id = ? AND revoked IS NULL", (now, gid)
            )
            return done.rowcount == 1

    def live_of(self, partner: int, now: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM grants WHERE partner = ? AND revoked IS NULL AND refresh_until > ? "
                "ORDER BY created",
                (partner, now),
            ).fetchall()
        return [dict(r) for r in rows]

    def sweep(self, now: int) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM grants WHERE refresh_until < ? "
                "OR (revoked IS NOT NULL AND revoked < ?)",
                (now - 86400, now - 86400),
            )
            self._db.execute("DELETE FROM ended WHERE until < ?", (now,))

    # browser sessions of belfry.<domain> that were signed out
    def end(self, sid: str, until: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO ended (sid, until) VALUES (?, ?)", (sid, until)
            )

    def is_ended(self, sid: str) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM ended WHERE sid = ?", (sid,)).fetchone() is not None
            )


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def store(svc: Services) -> Store:
    path = str(Path(svc.settings.register_path).with_name("appsignin.sqlite"))
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Store(path)
        return _stores[path]


def _now() -> int:
    return int(time.time())


# ── browser sessions that end on the server ─────────────────────────────────────────────────
def new_sid(svc: Services) -> str:
    """The id a new session of belfry.<domain> carries ("" where sessions are not kept: a belfry
    with Keycast, whose cookies stay as they were)."""
    return secrets.token_urlsafe(18) if stewards.own(svc) else ""


def session_ended(svc: Services, claims: dict[str, Any]) -> bool:
    sid = str(claims.get("sid") or "")
    if not sid or not stewards.own(svc):
        return False
    try:
        return store(svc).is_ended(sid)
    except sqlite3.Error:
        log.exception("appsignin: the table of ended sessions cannot be read")
        return True  # not knowing is not a reason to let a signed-out browser back in


def end_session(svc: Services, claims: dict[str, Any] | None) -> None:
    """The console's session SID is over (a logout). Never raises: a table that can't be
    written must not stop the logout itself (the cookie still goes); it is logged."""
    sid = str((claims or {}).get("sid") or "")
    if sid and stewards.own(svc):
        try:
            store(svc).end(sid, int((claims or {}).get("exp") or _now() + 90 * 86400))
        except Exception:  # noqa: BLE001
            log.exception("appsignin: the end of session %s was not written down", sid[:8])


# ── which apps are whose ────────────────────────────────────────────────────────────────────
def suite_name(svc: Services, uri: str) -> str:
    """The name of the app of the belfry's own suite (appshome.EXTRAS, when it runs here) that
    lives at URI's host; "" for anything else."""
    from . import appshome

    domain = str(svc.settings.nip05_domain or "").lower()
    u = urllib.parse.urlsplit(uri)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.port or not domain or not host.endswith("." + domain):
        return ""
    label = host[: -len(domain) - 1]
    for name in appshome.added(svc.settings):
        if label in appshome.EXTRAS[name]["hosts"]:
            return name
    return ""


# ── the token endpoint ──────────────────────────────────────────────────────────────────────
def _tokens(svc: Services) -> tuple[str, str, str, str]:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    return (
        access,
        mark(svc.settings, "access", access),
        refresh,
        mark(svc.settings, "refresh", refresh),
    )


def _answer(access: str, refresh: str, grant: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": ACCESS_SECONDS,
        "pubkey": grant["pubkey"],
        "scope": "policy:belfry-event",
    }


def _bunker_of(url: str) -> str:
    from .nip46check import parse_bunker_url

    return parse_bunker_url(url)[0]


def exchange(svc: Services, body: dict[str, Any], ua: str = "") -> dict[str, Any]:
    grant_type = str(body.get("grant_type") or "")
    if grant_type == "refresh_token":
        return refresh(svc, body)
    if grant_type != "authorization_code":
        raise SignInError(400, "unsupported_grant_type")
    try:
        # a sign-in used over /api/nostr: no NIP-46 connect is needed, and none is waited for
        c = connect.redeem(svc, body, ua, kind="http")
    except connect.ConnectError as exc:
        if exc.message == "own_key":
            # Keycast patch 0012's words: the app then signs in with the member's own signer
            raise SignInError(403, "access_denied", "self_custody") from exc
        if exc.message == "key_taken":
            raise SignInError(409, "invalid_request", "client_key_taken") from exc
        if exc.message == "app_not_approved":
            raise SignInError(403, "access_denied", "app_not_approved") from exc
        raise SignInError(400, "invalid_grant") from exc
    now = _now()
    st = store(svc)
    st.sweep(now)
    live = st.live_of(int(c["partner"]), now)
    for old in live[: max(0, len(live) + 1 - GRANTS_PER_MEMBER)]:
        end_grant(svc, old, "more sign-ins than one account keeps")
    access, access_mark, refresh_token, refresh_mark = _tokens(svc)
    grant = {
        "id": secrets.token_hex(16),
        "partner": int(c["partner"]),
        "pubkey": c["pubkey"],
        "client_id": c["client_id"],
        "uri": c["uri"],
        "scope": c["scope"],
        "bunker": c["bunker"].get("bunker_pubkey") or _bunker_of(c["bunker"]["bunker_url"]),
        "access": access_mark,
        "access_until": now + ACCESS_SECONDS,
        "refresh": refresh_mark,
        "refresh_until": now + REFRESH_SECONDS,
        "created": now,
        "label": str(c["bunker"].get("app_name") or "")[:64],
        # whether the member let this app read their private messages. The sign-in page has
        # no such question yet, so this is 0 for every app that is not the belfry's own: an
        # outside app encrypts, and does not decrypt (remoteapps.allowed).
        "decrypt": 1 if c.get("decrypt") is True else 0,
    }
    st.add(grant)
    log.info(
        "appsignin: %s signed in to %s (%s, grant %s)",
        c["pubkey"][:12],
        c["uri"],
        c["scope"],
        grant["id"][:8],
    )
    return {**_answer(access, refresh_token, grant), "bunker_url": c["bunker"]["bunker_url"]}


def refresh(svc: Services, body: dict[str, Any]) -> dict[str, Any]:
    token = str(body.get("refresh_token") or "")
    if not token:
        raise SignInError(400, "invalid_grant")
    st = store(svc)
    value = mark(svc.settings, "refresh", token)
    grant = st.by("refresh", value)
    if grant is None:
        reused = st.spent(value)
        if reused is not None and reused.get("revoked") is None:
            # a refresh token works once: a second use means it was copied
            end_grant(svc, reused, "a refresh token was used twice")
        raise SignInError(400, "invalid_grant")
    now = _now()
    client = str(body.get("client_id") or "")
    if (
        grant.get("revoked") is not None
        or grant["refresh_until"] <= now
        or (client and client[:120] != grant["client_id"])
    ):
        raise SignInError(400, "invalid_grant")
    check_live(svc, grant)
    access, access_mark, refresh_token, refresh_mark = _tokens(svc)
    if not st.rotate(grant["id"], value, access_mark, refresh_mark, now):
        raise SignInError(400, "invalid_grant")
    return _answer(access, refresh_token, grant)


def member_token(svc: Services, grant: dict[str, Any]) -> str:
    """The signer token of the grant's member, or the grant is over: no active pass any more,
    an own key since, or another key."""
    member = apps.member_by_partner(svc, int(grant["partner"]))
    if member is None or member["pubkey"] != grant["pubkey"]:
        raise SignInError(401, "invalid_token", "this sign-in has ended")
    found = community.member_token(svc, int(grant["partner"]))
    if found is None or found[0] != grant["pubkey"]:
        raise SignInError(401, "invalid_token", "this sign-in has ended")
    return found[1]


def check_live(svc: Services, grant: dict[str, Any]) -> str:
    """The member's signer token when the grant still stands on every side: the app is still in
    the registry, the member still has a pass and this key, and the grant was not ended (by
    the app, by a refresh token used twice, or by the member signing the session out: the
    console's Devices list and the chat's Sessions call :func:`session_revoked`).

    The grant's connection in the signer is not asked for here (one more call on every
    request); revoking it there ends the grant through :func:`session_revoked`."""
    if not apps.is_approved(svc, grant["uri"]):
        end_grant(svc, grant, "the app is no longer approved")
        raise SignInError(401, "invalid_token", "this sign-in has ended")
    fresh = store(svc).by("id", grant["id"])
    if fresh is None or fresh.get("revoked") is not None:
        raise SignInError(401, "invalid_token", "this sign-in has ended")
    return member_token(svc, grant)


def _iso(ts: Any) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def with_grants(svc: Services, partner: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The signer's sessions of a member plus their apps' sign-ins whose connection the signer
    no longer has (never connected to: the app signs over HTTP). Same fields; the id is the
    bunker key the connection had. Nothing is added on a belfry with Keycast."""
    if not enabled(svc):
        return rows
    try:
        grants = store(svc).live_of(int(partner), _now())
    except (sqlite3.Error, SignInError):
        log.exception("appsignin: the grants of partner %s cannot be read", partner)
        return rows
    have = {str(r.get("bunker_pubkey") or "") for r in rows}
    more = [
        {
            "application_name": g.get("label") or apps.session_label(svc, g["uri"], g["client_id"]),
            "redirect_origin": apps.origin_of(g["uri"]),
            "bunker_pubkey": g["bunker"],
            "client_pubkey": None,
            "created_at": _iso(g["created"]),
            "last_activity": _iso(g.get("used")),
            "activity_count": 0,
        }
        for g in reversed(grants)
        if g["bunker"] not in have
    ]
    return [*rows, *more]


def session_revoked(svc: Services, partner: int, bunker: str | None = None) -> int:
    """A member's session was signed out (BUNKER: its bunker key; None: all of them): the
    sign-ins of apps that stand on it end too. Returns how many. Never raises: the sign-out
    that calls it has happened in the signer already and must be answered as done; a table
    that can't be written is logged."""
    if not enabled(svc):
        return 0
    n = 0
    try:
        st, now = store(svc), _now()
        for g in st.live_of(int(partner), now):
            if (bunker is None or g["bunker"] == bunker) and st.revoke(g["id"], now):
                n += 1
                log.info(
                    "appsignin: grant %s of %s ended: its session was signed out",
                    g["id"][:8],
                    g["uri"],
                )
    except Exception:  # noqa: BLE001
        log.exception("appsignin: the app sign-ins of partner %s were not all ended", partner)
    return n


def end_grant(svc: Services, grant: dict[str, Any], why: str) -> None:
    """Over here and in the signer. The signer's side is best effort: the table decides."""
    if store(svc).revoke(grant["id"], _now()):
        log.info("appsignin: grant %s of %s ended: %s", grant["id"][:8], grant["uri"], why)
    try:
        found = community.member_token(svc, int(grant["partner"]))
        if found is not None:
            svc.keycast.revoke_session(found[1], grant["bunker"])
    except Exception as exc:  # noqa: BLE001 — the grant is revoked here whatever the signer says
        log.warning("appsignin: session of grant %s not revoked: %s", grant["id"][:8], exc)


def revoke(svc: Services, token: str) -> None:
    """RFC 7009: an access or a refresh token ends its sign-in; an unknown one is no error."""
    if not token:
        return
    st = store(svc)
    grant = st.by("refresh", mark(svc.settings, "refresh", token)) or st.by(
        "access", mark(svc.settings, "access", token)
    )
    if grant is not None and grant.get("revoked") is None:
        end_grant(svc, grant, "revoked by the app")


# ── signing for an app ──────────────────────────────────────────────────────────────────────
def grant_of(svc: Services, authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise SignInError(401, "invalid_token", "bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    grant = store(svc).by("access", mark(svc.settings, "access", token)) if token else None
    if grant is None or grant.get("revoked") is not None or grant["access_until"] <= _now():
        raise SignInError(401, "invalid_token", "expired or unknown")
    return grant


def rpc(svc: Services, authorization: str | None, body: dict[str, Any]) -> Any:
    grant = grant_of(svc, authorization)
    method = str(body.get("method") or "")
    params = body.get("params")
    if method not in RPC_METHODS or not isinstance(params, list):
        raise SignInError(400, "invalid_request", "unsupported method")
    token = check_live(svc, grant)
    store(svc).touch(grant["id"], _now())
    if method == "get_public_key":
        return grant["pubkey"]
    own = remoteapps.own_hosts(svc.settings)
    outside = grant["scope"] != "own"
    try:
        if method == "derive_secret":
            purpose = str(params[0]) if params else ""
            name = suite_name(svc, grant["uri"])
            if outside or purpose not in DERIVE.get(name, ()):
                raise SignInError(403, "forbidden", "this app may not derive that")
            derive = getattr(svc.keycast, "derive_secret", None)
            if derive is None:
                raise SignInError(400, "invalid_request", "unsupported method")
            return derive(token, purpose)
        if method == "sign_event":
            event = params[0] if params else None
            if isinstance(event, str):
                event = json.loads(event)
            if not isinstance(event, dict):
                raise SignInError(400, "invalid_request", "sign_event needs an event")
            if event.get("pubkey") not in (None, "", grant["pubkey"]):
                raise SignInError(403, "forbidden", "not this account's event")
            if outside and not remoteapps.allowed(None, True, method, event, own):
                raise SignInError(403, "forbidden", "not allowed for this app")
            unsigned = {k: v for k, v in event.items() if k not in ("id", "sig")}
            return svc.keycast.sign_event(token, {**unsigned, "pubkey": grant["pubkey"]})
        if len(params) < 2 or not all(isinstance(p, str) for p in params[:2]):
            raise SignInError(400, "invalid_request", "needs a public key and a text")
        if outside and not remoteapps.allowed(None, bool(grant.get("decrypt")), method, None, own):
            # reading what was written to the member needs their word, per app
            raise SignInError(403, "forbidden", "not allowed for this app")
        return svc.keycast.nostr_rpc(token, method, [params[0], params[1]])
    except SignInError:
        raise
    except ValueError as exc:
        raise SignInError(400, "invalid_request", "not an event") from exc
    except Exception as exc:  # noqa: BLE001 — the signer's reasons are not the app's business
        log.warning("appsignin: %s for grant %s failed: %s", method, grant["id"][:8], exc)
        raise SignInError(502, "signer_unavailable") from exc


# ── HTTP ────────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    def cors(request: Request, response: Response) -> Response:
        # no cookie is ever read here: any origin may call, and gets only what its token opens
        origin = request.headers.get("origin")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return response

    def off() -> Response:
        return JSONResponse({"error": "not_found"}, status_code=404)

    def fail(request: Request, exc: SignInError) -> Response:
        body = {"error": exc.error}
        if exc.description:
            body["error_description"] = exc.description
        return cors(request, JSONResponse(body, status_code=exc.status, headers=NO_STORE))

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            if "json" in request.headers.get("content-type", ""):
                body = await request.json()
            else:
                body = dict(await request.form())
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    @app.get("/api/oauth/authorize", include_in_schema=False)
    async def authorize(request: Request):
        """The sign-in itself is the belfry's (connect.py), on belfry.<domain>, where the
        browser's session lives. The address is this belfry's own and fixed; the query goes
        along as it came and is checked there."""
        services = svc(request)
        base = console.console_url(services.settings)
        if not enabled(services) or not base:
            return off()
        query = request.url.query
        return RedirectResponse(
            base + "/connect/authorize" + (f"?{query}" if query else ""),
            status_code=302,
            headers=NO_STORE,
        )

    @app.options("/api/oauth/token", include_in_schema=False)
    @app.options("/api/oauth/revoke", include_in_schema=False)
    @app.options("/api/nostr", include_in_schema=False)
    async def preflight(request: Request):
        return cors(request, Response(status_code=204))

    async def guarded(request: Request, fn: Any, *args: Any) -> Any:
        """FN in a thread; the signer or the register failing underneath (httpx, the signer's
        own errors: no key held, no new accounts…) answers 502 signer_unavailable with the
        CORS headers, never a bare 500 the app's browser can't read."""
        try:
            return await run_in_threadpool(fn, *args)
        except SignInError:
            raise
        except Exception as exc:  # noqa: BLE001 — the reasons are the signer's, not the app's
            log.warning("appsignin: %s failed: %s", request.url.path, type(exc).__name__)
            raise SignInError(502, "signer_unavailable") from exc

    @app.post("/api/oauth/token", include_in_schema=False)
    async def token(request: Request):
        services = svc(request)
        if not enabled(services):
            return off()
        try:
            result = await guarded(
                request,
                exchange,
                services,
                await body_of(request),
                request.headers.get("user-agent", ""),
            )
        except SignInError as exc:
            return fail(request, exc)
        return cors(request, JSONResponse(result, headers=NO_STORE))

    @app.post("/api/oauth/revoke", include_in_schema=False)
    async def revoke_(request: Request):
        services = svc(request)
        if not enabled(services):
            return off()
        body = await body_of(request)
        try:
            await guarded(request, revoke, services, str(body.get("token") or ""))
        except SignInError as exc:
            return fail(request, exc)
        return cors(request, JSONResponse({}, headers=NO_STORE))

    @app.post("/api/nostr", include_in_schema=False)
    async def nostr_rpc(request: Request):
        services = svc(request)
        if not enabled(services):
            return off()
        try:
            result = await guarded(
                request, rpc, services, request.headers.get("authorization"), await body_of(request)
            )
        except SignInError as exc:
            return fail(request, exc)
        return cors(request, JSONResponse({"result": result}, headers=NO_STORE))

    @app.get("/api/auth/oidc/logout", include_in_schema=False)
    async def logout(request: Request):
        """Where the apps send the browser after their own logout: on to belfry.<domain>, which
        ends its session (there and on the server) and returns to the app only when the app is
        in the registry (connect.py's ``/connect/logout``)."""
        services = svc(request)
        base = console.console_url(services.settings)
        if not enabled(services) or not base:
            return off()
        back = request.query_params.get("return_to", "")[:500]
        query = urllib.parse.urlencode({"return_to": back}) if back else ""
        return RedirectResponse(
            base + "/connect/logout" + (f"?{query}" if query else ""),
            status_code=303,
            headers=NO_STORE,
        )

    @app.get("/api/oauth/{rest:path}", include_in_schema=False)
    async def gone(request: Request, rest: str):
        return HTMLResponse("Not found", status_code=404)
