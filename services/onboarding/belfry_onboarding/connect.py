"""Sign in with Open Source Village: how apps get a signer for the member (replaces Keycast's
OAuth, 2026-11-19). Same paths, parameters and token response as Keycast had, so the apps'
builds only point at this service instead:

    GET  /api/oauth/authorize   client_id, redirect_uri, code_challenge (S256), state,
                                scope, prompt=none -> redirect_uri?code=…&state=…
    POST /api/oauth/consent     the consent screen's answer (outside apps)
    POST /api/oauth/token       authorization_code (+ code_verifier) or refresh_token ->
                                {access_token, refresh_token, bunker_url, pubkey, …}
    POST /api/oauth/revoke      {token, client_id}: the app's connection ends
    POST /api/nostr             HTTP RPC with the access token (the wallet): get_public_key,
                                sign_event, nip44/nip04 encrypt and decrypt, derive_secret

Every sign-in makes a connection at the signer (``services/signer``): a bunker URL with its own
key and a secret that binds to the first client that connects, and the tokens name it, so
revoking it (the app's logout, the account page) ends both the bunker and the HTTP RPC.

Belfry's own apps (OAUTH_CLIENTS, with their redirect URIs) skip the consent screen and may sign
in silently (``prompt=none``); each keeps its five newest connections. Other apps (any
client_id, an https redirect URI or localhost) see the consent screen until the member allows
them, and again once that app's connection is revoked (its logout, the account page); they may not
derive secrets. Errors on the redirect: ``login_required`` and ``consent_required`` (silent
sign-in not possible), ``access_denied`` with ``error_description=self_custody`` (the member
holds their own key: the app should use the member's own signer) or ``not_active``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import session
from .claim import page
from .clients.signer import SignerError, vine_id
from .core import Services

log = logging.getLogger(__name__)
E = html.escape

CODE_TTL = 300
ACCESS_TTL = 3600
CONSENT_TTL = 600
KEEP_CONNECTIONS = 5  # per own app and member: devices beyond this are signed out
CLIENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
RPC_METHODS = (
    "get_public_key",
    "sign_event",
    "nip44_encrypt",
    "nip44_decrypt",
    "nip04_encrypt",
    "nip04_decrypt",
    "derive_secret",
)


class ConnectError(Exception):
    def __init__(self, status: int, error: str, description: str = ""):
        super().__init__(description or error)
        self.status = status
        self.error = error
        self.description = description


def registered_clients(settings: Any) -> dict[str, str]:
    """OAUTH_CLIENTS: client_id=redirect_uri, comma separated."""
    out: dict[str, str] = {}
    for entry in (settings.oauth_clients or "").split(","):
        if "=" in entry:
            cid, uri = entry.split("=", 1)
            if cid.strip() and uri.strip():
                out[cid.strip()] = uri.strip()
    return out


def derive_rules(settings: Any) -> dict[str, list[str]]:
    """DERIVE_SECRETS: client:purpose|purpose;client:… (Keycast's KEYCAST_DERIVE_SECRETS)."""
    out: dict[str, list[str]] = {}
    for entry in (settings.derive_secrets or "").split(";"):
        if ":" in entry:
            cid, purposes = entry.split(":", 1)
            out[cid.strip()] = [p.strip() for p in purposes.split("|") if p.strip()]
    return out


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def outside_redirect_ok(uri: str) -> bool:
    parts = urllib.parse.urlsplit(uri)
    if parts.fragment or not parts.netloc:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "[::1]", "::1")


@dataclass
class Code:
    partner_id: int
    client_id: str
    redirect_uri: str
    code_challenge: str
    trusted: bool
    created: float = field(default_factory=time.time)


class CodeStore:
    def __init__(self) -> None:
        self.codes: dict[str, Code] = {}
        self.lock = threading.Lock()

    def put(self, code: Code) -> str:
        value = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            for k in [k for k, c in self.codes.items() if now - c.created > CODE_TTL]:
                self.codes.pop(k, None)
            self.codes[value] = code
        return value

    def take(self, value: str) -> Code | None:
        with self.lock:
            code = self.codes.pop(value or "", None)
        if code is None or time.time() - code.created > CODE_TTL:
            return None
        return code


class AppOrigins:
    """Origins of apps people signed in to lately (30 days, in memory): an outside app's logout
    may return there. Belfry's own apps are always allowed (session.allowed_origins)."""

    TTL = 30 * 86400

    def __init__(self) -> None:
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def note(self, origin: str) -> None:
        with self.lock:
            self.seen[origin.lower()] = time.time() + self.TTL

    def current(self) -> set[str]:
        now = time.time()
        with self.lock:
            for o in [o for o, exp in self.seen.items() if exp < now]:
                self.seen.pop(o, None)
            return set(self.seen)


def _redirect(uri: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in uri else "?"
    clean = {k: v for k, v in params.items() if v}
    return RedirectResponse(uri + sep + urllib.parse.urlencode(clean), status_code=302)


def check_request(settings: Any, params: dict[str, str]) -> tuple[str, str, bool]:
    """(client_id, redirect_uri, trusted) or ConnectError for a request that cannot even be
    answered on its redirect URI."""
    client_id = params.get("client_id") or ""
    redirect_uri = params.get("redirect_uri") or ""
    if not CLIENT_ID.match(client_id):
        raise ConnectError(400, "invalid_client", "The app did not say who it is (client_id).")
    registered = registered_clients(settings)
    if client_id in registered:
        if redirect_uri != registered[client_id]:
            raise ConnectError(
                400, "invalid_request", "This app's return address is not registered."
            )
        return client_id, redirect_uri, True
    if not outside_redirect_ok(redirect_uri):
        raise ConnectError(400, "invalid_request", "The app's return address must be https.")
    return client_id, redirect_uri, False


# -- tokens -------------------------------------------------------------------------------------


def _key(settings: Any) -> str:
    if not session.configured(settings):
        raise ConnectError(503, "temporarily_unavailable", "SESSION_SECRET is not set")
    return settings.session_secret


def make_tokens(settings: Any, vid: str, cid: str, pubkey: str, client_id: str) -> dict[str, Any]:
    now = int(time.time())
    base = {"vid": vid, "cid": cid, "pk": pubkey, "cl": client_id, "iat": now}
    access = jwt.encode(
        {"typ": "belfry-at", **base, "exp": now + ACCESS_TTL}, _key(settings), algorithm="HS256"
    )
    refresh = jwt.encode({"typ": "belfry-rt", **base}, _key(settings), algorithm="HS256")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
    }


def read_token(settings: Any, token: str, typ: str | None = None) -> dict[str, Any]:
    try:
        claims = jwt.decode(token or "", _key(settings), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise ConnectError(401, "invalid_token", "expired") from exc
    except jwt.PyJWTError as exc:
        raise ConnectError(401, "invalid_token", "not a token of this Belfry") from exc
    if claims.get("typ") not in ("belfry-at", "belfry-rt") or (typ and claims.get("typ") != typ):
        raise ConnectError(401, "invalid_token", "wrong kind of token")
    return claims


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return hmac.compare_digest(digest, challenge)


# -- the steps ----------------------------------------------------------------------------------


def account_for(svc: Services, partner_id: int) -> dict[str, Any]:
    """The member's signer account, for an app to sign with; ConnectError otherwise."""
    if not svc.odoo.call("belfry.login.code", "belfry_may_sign_in", [partner_id]):
        raise ConnectError(403, "access_denied", "not_active")
    account = svc.signer.account(vine_id(partner_id))
    if account is None:
        raise ConnectError(403, "access_denied", "no_account")
    if account.get("custody") == "own":
        raise ConnectError(403, "access_denied", "self_custody")
    if account.get("status") != "active":
        raise ConnectError(403, "access_denied", "not_active")
    return account


def outside_label(client_id: str, redirect_uri: str) -> str:
    return f"{client_id} ({_origin(redirect_uri)})"


def approved_before(svc: Services, partner_id: int, client_id: str, redirect_uri: str) -> bool:
    label = outside_label(client_id, redirect_uri)
    return any(
        c.get("app") == client_id and c.get("label") == label
        for c in svc.signer.connections(vine_id(partner_id))
    )


def exchange(svc: Services, codes: CodeStore, body: dict[str, Any]) -> dict[str, Any]:
    grant = str(body.get("grant_type") or "")
    if grant == "refresh_token":
        claims = read_token(svc.settings, str(body.get("refresh_token") or ""), "belfry-rt")
        live = {c["id"] for c in svc.signer.connections(claims["vid"])}
        if claims["cid"] not in live:
            raise ConnectError(400, "invalid_grant", "this sign-in has ended")
        return make_tokens(svc.settings, claims["vid"], claims["cid"], claims["pk"], claims["cl"])
    if grant != "authorization_code":
        raise ConnectError(400, "unsupported_grant_type")
    code = codes.take(str(body.get("code") or ""))
    if code is None:
        raise ConnectError(400, "invalid_grant", "unknown, used or expired code")
    if str(body.get("client_id") or "") != code.client_id or (
        body.get("redirect_uri") and body["redirect_uri"] != code.redirect_uri
    ):
        raise ConnectError(400, "invalid_grant", "client_id or redirect_uri do not match")
    if not _pkce_ok(str(body.get("code_verifier") or ""), code.code_challenge):
        raise ConnectError(400, "invalid_grant", "PKCE verification failed")
    account = account_for(svc, code.partner_id)
    vid = vine_id(code.partner_id)
    derive = derive_rules(svc.settings).get(code.client_id, []) if code.trusted else []
    label = code.client_id if code.trusted else outside_label(code.client_id, code.redirect_uri)
    conn = svc.signer.create_connection(
        vid, code.client_id, label, derive, KEEP_CONNECTIONS if code.trusted else None
    )
    log.info(
        "partner %s signed in to %s (connection %s)", code.partner_id, code.client_id, conn["id"]
    )
    out = make_tokens(svc.settings, vid, conn["id"], account["pubkey"], code.client_id)
    out.update(
        {
            "bunker_url": conn["bunker_url"],
            "pubkey": account["pubkey"],
            "scope": "policy:belfry-event",
        }
    )
    return out


def revoke(svc: Services, token: str) -> None:
    try:
        claims = read_token(svc.settings, token)
    except ConnectError:
        return  # RFC 7009: an unknown token is not an error
    try:
        svc.signer.revoke(claims["vid"], connection=claims["cid"])
        log.info(
            "connection %s of %s revoked by %s", claims["cid"], claims["vid"], claims.get("cl")
        )
    except SignerError as exc:
        if exc.status != 404:
            raise


def rpc(svc: Services, authorization: str | None, body: dict[str, Any]) -> Any:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ConnectError(401, "invalid_token", "bearer token required")
    claims = read_token(svc.settings, authorization.split(" ", 1)[1].strip(), "belfry-at")
    method = str(body.get("method") or "")
    params = body.get("params") or []
    if method not in RPC_METHODS or not isinstance(params, list):
        raise ConnectError(400, "invalid_request", f"unsupported method {method!r}")
    vid, cid = claims["vid"], claims["cid"]
    try:
        if method == "get_public_key":
            # still live? (a revoked sign-in must not even tell the key)
            if cid not in {c["id"] for c in svc.signer.connections(vid)}:
                raise ConnectError(403, "forbidden", "this sign-in has ended")
            return claims["pk"]
        if method == "sign_event":
            event = params[0] if params else None
            if isinstance(event, str):
                event = json.loads(event)
            if not isinstance(event, dict):
                raise ConnectError(400, "invalid_request", "sign_event needs an event")
            return svc.signer.sign(vid, event, connection=cid)
        if method == "derive_secret":
            return svc.signer.derive(vid, str(params[0]) if params else "", connection=cid)
        scheme, op = method.split("_", 1)
        if len(params) < 2:
            raise ConnectError(400, "invalid_request", f"{method} needs a public key and a text")
        return svc.signer.crypt(vid, op, scheme, str(params[0]), str(params[1]), connection=cid)
    except SignerError as exc:
        status = 403 if exc.status in (403, 409, 404) else (400 if exc.status == 422 else 502)
        raise ConnectError(status, exc.code, exc.message) from exc


# -- pages --------------------------------------------------------------------------------------


def consent_page(client_id: str, redirect_uri: str, form_token: str) -> str:
    host = urllib.parse.urlsplit(redirect_uri).netloc
    body = f"""<div class="card">
<h1>Sign in to {E(host)}?</h1>
<p><strong>{E(client_id)}</strong> at <strong>{E(host)}</strong> asks to sign in with your
Open Source Village account. It can then post, send and read messages in your name, until you
sign out there or remove it on your <a href="/account">account page</a>.</p>
<form method="post" action="/api/oauth/consent" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:16px">
<input type="hidden" name="consent" value="{E(form_token)}">
<button class="btn" type="submit" name="answer" value="allow" style="border:0;font:inherit;font-weight:600;cursor:pointer">Allow</button>
<button class="btn" type="submit" name="answer" value="deny" style="border:0;font:inherit;cursor:pointer;background:var(--osv-surface-deep);color:var(--osv-foreground)">Cancel</button>
</form>
</div>"""
    return page("Sign in to an app", body)


def own_key_page(redirect_uri: str) -> str:
    host = urllib.parse.urlsplit(redirect_uri).netloc
    body = f"""<div class="card">
<h1>Use your own signer</h1>
<p>You hold your own Nostr key, so Open Source Village cannot sign in to apps for you. Sign
in to {E(host)} with your own signer (a browser extension or a signer app) instead.</p>
<p><a class="btn" href="{E(redirect_uri)}">Back to {E(host)}</a></p>
</div>"""
    return page("Use your own signer", body)


def error_page(text: str) -> str:
    body = f"""<div class="card"><h1>Sign-in not possible</h1><p>{E(text)}</p>
<p><a class="btn" href="/account">Your account</a></p></div>"""
    return page("Sign-in not possible", body)


def no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def _body(request: Request) -> dict[str, Any]:
    if "application/json" in (request.headers.get("content-type") or ""):
        try:
            data = await request.json()
        except ValueError:
            data = None
        return data if isinstance(data, dict) else {}
    return {k: str(v) for k, v in (await request.form()).items()}


def register(app: FastAPI) -> None:
    """Mount /api/oauth/* and /api/nostr (create_app calls this)."""
    app.state.connect_codes = CodeStore()
    app.state.app_origins = AppOrigins()

    def svc(request: Request) -> Services:
        return request.app.state.services

    async def issue(
        request: Request,
        partner_id: int,
        client_id: str,
        redirect_uri: str,
        challenge: str,
        trusted: bool,
        state: str,
    ) -> Response:
        code = request.app.state.connect_codes.put(
            Code(partner_id, client_id, redirect_uri, challenge, trusted)
        )
        # the app's logout may return there (as Keycast's did for any app signed in to)
        request.app.state.app_origins.note(_origin(redirect_uri))
        return no_store(_redirect(redirect_uri, code=code, state=state))

    @app.get("/api/oauth/authorize")
    async def authorize(request: Request):
        services = svc(request)
        params = {k: str(v) for k, v in request.query_params.items()}
        try:
            client_id, redirect_uri, trusted = check_request(services.settings, params)
        except ConnectError as exc:
            return no_store(HTMLResponse(error_page(exc.description), status_code=exc.status))
        state = params.get("state", "")
        silent = params.get("prompt") == "none"
        challenge = params.get("code_challenge") or ""
        if not challenge or params.get("code_challenge_method", "S256") != "S256":
            return no_store(
                _redirect(
                    redirect_uri,
                    error="invalid_request",
                    error_description="PKCE with S256 is required",
                    state=state,
                )
            )
        current = session.current(request)
        if current is None:
            if silent:
                return no_store(_redirect(redirect_uri, error="login_required", state=state))
            here = "/api/oauth/authorize?" + urllib.parse.urlencode(params)
            return no_store(
                RedirectResponse("/login?" + urllib.parse.urlencode({"next": here}), 303)
            )
        try:
            await run_in_threadpool(account_for, services, current.partner_id)
        except ConnectError as exc:
            if exc.description == "self_custody" and not silent:
                return no_store(HTMLResponse(own_key_page(redirect_uri), status_code=403))
            if exc.description == "not_active" and silent:
                return no_store(_redirect(redirect_uri, error="login_required", state=state))
            return no_store(
                _redirect(
                    redirect_uri,
                    error="access_denied",
                    error_description=exc.description,
                    state=state,
                )
            )
        if trusted:
            return await issue(
                request, current.partner_id, client_id, redirect_uri, challenge, True, state
            )
        # An outside app the member allowed before and is still signed in to (a live connection
        # of this app at this address) comes back without the consent screen, as with Keycast.
        if await run_in_threadpool(
            approved_before, services, current.partner_id, client_id, redirect_uri
        ):
            return await issue(
                request, current.partner_id, client_id, redirect_uri, challenge, False, state
            )
        if silent:
            return no_store(_redirect(redirect_uri, error="consent_required", state=state))
        now = int(time.time())
        form_token = jwt.encode(
            {
                "typ": "belfry-consent",
                "pid": current.partner_id,
                "cl": client_id,
                "ru": redirect_uri,
                "cc": challenge,
                "st": state,
                "exp": now + CONSENT_TTL,
            },
            services.settings.session_secret,
            algorithm="HS256",
        )
        return no_store(HTMLResponse(consent_page(client_id, redirect_uri, form_token)))

    @app.post("/api/oauth/consent")
    async def consent(request: Request):
        services = svc(request)
        form = await request.form()
        current = session.current(request)
        try:
            claims = jwt.decode(
                str(form.get("consent") or ""),
                services.settings.session_secret,
                algorithms=["HS256"],
            )
        except jwt.PyJWTError:
            claims = {}
        if (
            claims.get("typ") != "belfry-consent"
            or current is None
            or claims.get("pid") != current.partner_id
        ):
            return no_store(
                HTMLResponse(
                    error_page("This sign-in has expired. Start it again in the app."), 400
                )
            )
        if form.get("answer") != "allow":
            return no_store(
                _redirect(claims["ru"], error="access_denied", state=claims.get("st", ""))
            )
        return await issue(
            request,
            current.partner_id,
            claims["cl"],
            claims["ru"],
            claims["cc"],
            False,
            claims.get("st", ""),
        )

    @app.post("/api/oauth/token")
    async def token(request: Request) -> JSONResponse:
        body = await _body(request)
        try:
            out = await run_in_threadpool(
                exchange, svc(request), request.app.state.connect_codes, body
            )
        except ConnectError as exc:
            return JSONResponse(
                {"error": exc.error, "error_description": exc.description},
                status_code=exc.status,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post("/api/oauth/revoke")
    async def revoke_token(request: Request) -> JSONResponse:
        body = await _body(request)
        await run_in_threadpool(revoke, svc(request), str(body.get("token") or ""))
        return JSONResponse({"ok": True})

    @app.post("/api/nostr")
    async def nostr_rpc(request: Request) -> JSONResponse:
        body = await _body(request)
        try:
            result = await run_in_threadpool(
                rpc, svc(request), request.headers.get("authorization"), body
            )
        except ConnectError as exc:
            return JSONResponse(
                {"error": exc.description or exc.error, "code": exc.error}, status_code=exc.status
            )
        return JSONResponse({"result": result})
