"""Sign in to any app as ``<name>@<domain>``: the app's own session key asks the belfry to open a
session on the member's key.

1. The app makes a fresh key pair, its *session key*, and sends the belfry a direct message
   signed with it (NIP-17: a kind 14 message, sealed and gift-wrapped, kind 1059) on the bunker
   relay (``wss://bunker.<domain>``, open to any key; the belfry announces it in its kind 10050
   list and in ``/.well-known/belfry.json``): "this key claims to be xavier@belfry.sh".
2. The belfry looks the name up (Keycast's NIP-05), finds the member's e-mail address in Odoo and
   mails a 6-digit code (Odoo's ``belfry.key.code``: 10 minutes, five tries, rate limits). It
   answers "a code is on its way to x•••@example.org".
3. The person types the code in the app, which sends it in the same conversation.
4. With the right code the belfry creates a NIP-46 connection on the member's key in Keycast
   (``bunker://…``) and sends it back, encrypted to the session key. The app connects with the
   session key (NIP-46 ``connect`` with the URL's secret binds that key) and from then on every
   event it asks the bunker to sign is signed with the member's key.

The same two steps work over HTTPS for apps that don't do NIP-17, NIP-98 signed with the session
key: ``POST /api/session {"nip05", "app", "client"}`` then ``POST /api/session/verify {"code"}``
(belfry.<domain>/api/v1/session). The phone apps sign in this way, without a browser: ``app`` =
``sh.belfry.app://connect`` and ``client`` = ``belfry-android`` / ``belfry-ios`` (names the
session). Answers: ``{"state": "code_sent", "to": "x•••@…", "nip05"}`` then ``{"state": "open",
"bunker_url", "pubkey", "npub", "nip05"}``; errors ``{"error": code}``: 404 ``unknown``, 409
``no_email`` / ``own_key`` / ``mismatch`` / ``key_taken``, 503 ``signer`` (try again), 422 ``invalid`` / ``wrong_code`` (with
``tries_left``), 410 ``expired``, 429 ``too_many``, 403 ``app_not_approved``. The connection is
bound to the session key when it is made.

``app`` is the app's address (https://…). Only apps an admin approved get a session (apps.py):
for any other app the right code sends the admins a request (a Nostr event signed by the member)
and the answer is ``app_not_approved``.

Messages are text for people and tags for apps: a request is any message naming the address
(``{"nip05": …, "app": …}`` JSON works too); replies carry ``["belfry", "code_sent" | "open" |
"error"]``, and the last one ``["bunker", <url>]``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import threading
import time
from typing import Any

from coincurve import PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import apps as apps_mod
from . import homepage, nostr
from .blossom import event_id, verify_signature
from .clients.keycast import ClientKeyTaken
from .clients.odoo import OdooError
from .core import Services
from .holder_invites import ApiError, authenticate
from .nip46check import conversation_key, nip44_decrypt, nip44_encrypt, sign_event

log = logging.getLogger(__name__)

WRAP, SEAL, DM, DM_RELAYS = 1059, 13, 14, 10050
REQUEST_SECONDS = 900
NAME = re.compile(r"\b([a-z0-9][a-z0-9._-]{0,63})@([a-z0-9.-]+\.[a-z]{2,})\b", re.I)
CODE = re.compile(r"\b(\d{6})\b")
PATH = "/api/session"


class SessionError(Exception):
    def __init__(self, status: int, code: str, message: str, **data: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.data = status, code, message, data


# ── NIP-17 / NIP-59 ─────────────────────────────────────────────────────────────────────────
def unwrap(secret: bytes, wrap: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(sender pubkey, the kind 14 message) of a gift wrap addressed to SECRET's key."""
    if wrap.get("kind") != WRAP or not verify_signature(wrap):
        raise ValueError("not a valid gift wrap")
    seal = json.loads(
        nip44_decrypt(conversation_key(secret, bytes.fromhex(wrap["pubkey"])), wrap["content"])
    )
    if seal.get("kind") != SEAL or not verify_signature(seal):
        raise ValueError("the seal is not signed")
    rumor = json.loads(
        nip44_decrypt(conversation_key(secret, bytes.fromhex(seal["pubkey"])), seal["content"])
    )
    if rumor.get("pubkey") != seal["pubkey"] or rumor.get("kind") != DM:
        raise ValueError("the message's author is not the seal's")
    return seal["pubkey"], rumor


def wrap_dm(secret: bytes, to: str, content: str, tags: list[list[str]]) -> dict[str, Any]:
    """A kind 14 message from SECRET's key to TO, sealed and gift-wrapped (NIP-59)."""
    me = PrivateKey(secret).public_key_xonly.format().hex()
    now = int(time.time())
    rumor = {
        "kind": DM,
        "pubkey": me,
        "created_at": now,
        "content": content,
        "tags": [["p", to], *tags],
    }
    rumor["id"] = event_id(rumor)
    seal = sign_event(
        secret,
        {
            "kind": SEAL,
            "created_at": now - secrets.randbelow(3600),
            "tags": [],
            "content": nip44_encrypt(
                conversation_key(secret, bytes.fromhex(to)), json.dumps(rumor)
            ),
        },
    )
    one_time = PrivateKey()
    return sign_event(
        one_time.secret,
        {
            "kind": WRAP,
            "created_at": now - secrets.randbelow(3600),
            "tags": [["p", to]],
            "content": nip44_encrypt(
                conversation_key(one_time.secret, bytes.fromhex(to)), json.dumps(seal)
            ),
        },
    )


# ── the requests ────────────────────────────────────────────────────────────────────────────
def mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "your address"


def domains(settings: Any) -> set[str]:
    from . import pages

    return {d.lower() for d in (settings.nip05_domain, pages.site_domain(settings)) if d}


class Sessions:
    def __init__(self, svc: Services):
        self.svc = svc
        self.pending: dict[str, dict[str, Any]] = {}  # session pubkey → request
        self._lock = threading.Lock()

    def _member(self, name: str) -> tuple[str, int, str]:
        """(pubkey, partner id, e-mail) of the member called NAME."""
        pubkey = self.svc.keycast.pubkey_for_name(name)
        if not pubkey:
            raise SessionError(404, "unknown", f"No member is called {name} here.")
        rows = self.svc.odoo.search_read(
            "belfry.pass",
            [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
            ["partner_id"],
            limit=1,
        )
        if not rows:
            raise SessionError(404, "unknown", f"{name} has no active membership.")
        partner = rows[0]["partner_id"]
        pid = partner[0] if isinstance(partner, list) else int(partner)
        email = (self.svc.odoo.read("res.partner", [pid], ["email"]) or [{}])[0].get("email") or ""
        if "@" not in email:
            raise SessionError(409, "no_email", f"{name} has no e-mail address to send a code to.")
        return pubkey, pid, email

    def request(self, session: str, nip05: str, app: str = "", client: str = "") -> dict[str, Any]:
        m = NAME.search(nip05 or "")
        if not m:
            raise SessionError(422, "invalid", "Name the address to sign in with: name@domain.")
        name, domain = m.group(1).lower(), m.group(2).lower()
        if domain not in domains(self.svc.settings):
            raise SessionError(
                404,
                "unknown",
                f"This belfry answers for {self.svc.settings.nip05_domain}, not {domain}.",
            )
        try:
            app = apps_mod.app_uri(app)
        except ValueError as exc:
            raise SessionError(
                422,
                "app_address",
                "Name the app by its address (https://…): admins approve apps by address.",
            ) from exc
        pubkey, pid, email = self._member(name)
        try:
            row = self.svc.odoo.call(
                "belfry.key.code", "belfry_key_code_send", [email, session, app or False]
            )
        except OdooError as exc:
            raise SessionError(422, "invalid", "The code could not be sent.") from exc
        if row.get("state") == "too_many":
            raise SessionError(429, "too_many", "Too many codes; try again in half an hour.")
        with self._lock:
            now = time.time()
            for k in [k for k, v in self.pending.items() if v["at"] < now - REQUEST_SECONDS]:
                del self.pending[k]
            self.pending[session] = {
                "name": name,
                "pubkey": pubkey,
                "partner": pid,
                "email": email,
                "app": app,
                "client": client[:40],
                "at": now,
            }
        log.info("sessions: key %s asks for %s@%s (%s)", session[:12], name, domain, app or "app")
        return {
            "state": "code_sent",
            "to": mask(email),
            "nip05": f"{name}@{self.svc.settings.nip05_domain}",
        }

    def verify(self, session: str, code: str) -> dict[str, Any]:
        with self._lock:
            req = self.pending.get(session)
        if not req or req["at"] < time.time() - REQUEST_SECONDS:
            raise SessionError(
                410, "expired", "Ask again with your address: no request is waiting."
            )
        try:
            row = self.svc.odoo.call(
                "belfry.key.code", "belfry_key_code_verify", [req["email"], session, code]
            )
        except OdooError as exc:
            raise SessionError(422, "invalid", "The code could not be checked.") from exc
        state = row.get("state")
        if state == "wrong_code":
            raise SessionError(
                422,
                "wrong_code",
                f"That code is not right ({int(row.get('tries_left') or 0)} tries left).",
                tries_left=int(row.get("tries_left") or 0),
            )
        if state == "expired":
            raise SessionError(410, "expired", "The code expired: ask again with your address.")
        # the right code: Odoo answers other_key with the member's own key (the session key is
        # not theirs, and stays out of the registry)
        if state != "other_key" or row.get("npub") != nostr.hex_to_npub(req["pubkey"]):
            raise SessionError(409, "mismatch", "This address does not belong to that member.")
        name = req["name"]
        if not apps_mod.is_approved(self.svc, req["app"]):
            member = {
                "partner": req["partner"],
                "pubkey": req["pubkey"],
                "name": name,
                "email": req["email"],
            }
            where = f"{self.svc.settings.console_url.rstrip('/')}/apps"
            with self._lock:
                self.pending.pop(session, None)
            if apps_mod.is_admin(self.svc, member):
                raise SessionError(
                    403,
                    "app_not_approved",
                    f"{apps_mod.address(req['app'])} isn't approved yet. You are an admin: approve it at {where}, then sign in again.",
                )
            try:
                apps_mod.request(self.svc, member, req["app"], "")
            except Exception as exc:  # noqa: BLE001 — the refusal stands either way
                log.warning("sessions: request for %s not published: %s", req["app"], exc)
            raise SessionError(
                403,
                "app_not_approved",
                f"{apps_mod.address(req['app'])} isn't approved yet. We asked the admins; sign in again once they approve it.",
            )
        result = self.svc.keycast.preload_user(
            vine_id=f"odoo-partner-{req['partner']}", username=name, display_name=name
        )
        if result.get("pubkey") != req["pubkey"]:
            raise SessionError(409, "mismatch", "The member's key moved; ask again.")
        label = apps_mod.native_name(req["app"], req.get("client", "")) or apps_mod.display_name(
            self.svc, req["app"]
        )
        try:
            # bound to the session key when it is made: the URL alone opens nothing
            bunker = self.svc.keycast.create_bunker(result["token"], label, session)
        except ClientKeyTaken as exc:
            raise SessionError(
                409, "key_taken", "This device's key is linked to another account: start again."
            ) from exc
        except Exception as exc:  # noqa: BLE001 — a self-custody member has no key here
            log.warning("sessions: no bunker for %s: %s", name, exc)
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if not 400 <= status < 500:
                # the signer is down or failing (the code is spent: the app asks for a new one)
                raise SessionError(
                    503, "signer", "The signer isn't answering: try again in a moment."
                ) from exc
            raise SessionError(
                409, "own_key", f"{name} holds their own key: sign in with your own signer."
            ) from exc
        with self._lock:
            self.pending.pop(session, None)
        log.info("sessions: opened a session for %s with key %s", name, session[:12])
        return {
            "state": "open",
            "bunker_url": bunker["bunker_url"],
            "pubkey": req["pubkey"],
            "npub": nostr.hex_to_npub(req["pubkey"]),
            "nip05": f"{name}@{self.svc.settings.nip05_domain}",
        }

    # ── direct messages ──
    def answer_dm(self, sender: str, rumor: dict[str, Any]) -> tuple[str, list[list[str]]]:
        text = (rumor.get("content") or "").strip()
        app = next((t[1] for t in rumor.get("tags", []) if len(t) > 1 and t[0] == "client"), "")
        nip05 = ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                nip05, app = str(data.get("nip05", "")), str(data.get("app", app))
                text = str(data.get("code", "")) or nip05
        except ValueError:
            pass
        try:
            if (m := CODE.search(text)) and not NAME.search(text):
                r = self.verify(sender, m.group(1))
                return (
                    f"Session open: this key now signs as {r['nip05']}. Connect with:\n{r['bunker_url']}",
                    [["belfry", "open"], ["bunker", r["bunker_url"]], ["nip05", r["nip05"]]],
                )
            r = self.request(sender, nip05 or text, app)
            return (
                f"A code is on its way to {r['to']}. Send it back here to sign in as {r['nip05']}.",
                [["belfry", "code_sent"], ["nip05", r["nip05"]]],
            )
        except SessionError as exc:
            return exc.message, [["belfry", "error"], ["error", exc.code]]


# ── the listener on the bunker relay ────────────────────────────────────────────────────────
async def listen(app: FastAPI) -> None:
    """Gift wraps for the belfry key on SESSION_RELAY_URL; answers each. Reconnects forever."""
    from websockets.asyncio.client import connect

    svc: Services = app.state.services
    key = homepage.belfry_key(svc.settings)
    url = getattr(svc.settings, "session_relay_url", "")
    if key is None or not url:
        return
    me = key.public_key_xonly.format().hex()
    sessions: Sessions = app.state.sessions
    seen: dict[str, float] = {}
    delay = 2.0
    while True:
        try:
            async with connect(url, open_timeout=10) as ws:
                relays = [r for r in (svc.settings.bunker_relays or "").split(",") if r]
                if relays:
                    ann = sign_event(
                        key.secret,
                        {
                            "kind": DM_RELAYS,
                            "created_at": int(time.time()),
                            "tags": [["relay", r] for r in relays],
                            "content": "",
                        },
                    )
                    await ws.send(json.dumps(["EVENT", ann]))
                # gift wraps are back-dated up to two days (NIP-59)
                await ws.send(
                    json.dumps(
                        [
                            "REQ",
                            "dm",
                            {"kinds": [WRAP], "#p": [me], "since": int(time.time()) - 2 * 86400},
                        ]
                    )
                )
                delay = 2.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg[0] != "EVENT" or len(msg) < 3:
                        continue
                    wrap = msg[2]
                    if wrap.get("id") in seen:
                        continue
                    now = time.time()
                    seen[wrap["id"]] = now
                    for k in [k for k, t in seen.items() if t < now - 3 * 86400]:
                        del seen[k]
                    try:
                        sender, rumor = unwrap(key.secret, wrap)
                    except (ValueError, KeyError, TypeError) as exc:
                        log.debug("sessions: skipped a wrap: %s", exc)
                        continue
                    if rumor.get("created_at", 0) < now - REQUEST_SECONDS:
                        continue  # old messages from before a restart
                    text, tags = await run_in_threadpool(sessions.answer_dm, sender, rumor)
                    await ws.send(json.dumps(["EVENT", wrap_dm(key.secret, sender, text, tags)]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep listening whatever the relay does
            log.info("sessions: relay %s: %s; again in %.0fs", url, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120.0)


def register(app: FastAPI) -> None:
    def answer(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    async def signed(request: Request) -> tuple[Sessions, str, dict[str, Any]]:
        services: Services = request.app.state.services
        raw = await request.body()
        pubkey = authenticate(
            services,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            request.method,
            request.url.path,
            raw,
        )
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return request.app.state.sessions, pubkey, body

    async def run(fn, *args) -> JSONResponse:
        try:
            return answer(await run_in_threadpool(fn, *args))
        except SessionError as exc:
            return answer({"error": exc.code, "message": exc.message, **exc.data}, exc.status)

    @app.post(PATH)
    async def api_session(request: Request):
        try:
            sessions, pubkey, body = await signed(request)
        except ApiError as exc:
            return answer({"error": exc.error, "message": exc.message}, exc.status)
        return await run(
            sessions.request,
            pubkey,
            str(body.get("nip05", "")),
            str(body.get("app", "")),
            str(body.get("client", "")),
        )

    @app.post(PATH + "/verify")
    async def api_session_verify(request: Request):
        try:
            sessions, pubkey, body = await signed(request)
        except ApiError as exc:
            return answer({"error": exc.error, "message": exc.message}, exc.status)
        return await run(sessions.verify, pubkey, str(body.get("code", "")))


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    task = asyncio.create_task(listen(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
