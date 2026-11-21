"""Applying and logging in from the chat with a proven address and a browser key (Xavier,
2026-11-19; docs/membership.md, "Applying from the chat (verified)").

The chat makes a key in the browser (the *applicant key*) and signs every request here with it
(NIP-98). A 6-digit code mailed to an address is the only proof of that address; a session is
only ever bound to the key that signed the request.

- ``POST /api/apply/start`` ``{"email", "motivation", "name", "username"?, "agreed": [...]}`` →
  a code to the address, 202 ``{"sent": true}``. With ``"proof"`` (from ``/api/login/verify``)
  no code is mailed and the answer is the one of ``/api/apply/verify``. ``username``: the
  address they would like here (usernames.py; 422 ``username_invalid``, 409 ``username_taken``
  with ``suggestions``; needs ``name``); kept with the request, not reserved while it waits.
  Older chats send neither: the name then comes from the address.
- ``POST /api/apply/verify`` ``{"email", "code"}`` → ``{"state": "signed_in", …}`` when the
  address has an account here, else the application is stored (address verified, applicant key)
  and the admins are told once → ``{"state": "pending"}``.
- ``POST /api/apply/status`` ``{}`` → ``{"state": "none" | "pending" | "declined" |
  "approved"}``; pending: also ``name``, ``username``, ``email``, ``motivation`` (their own,
  shown back to them); approved: a session on the new member's key, bound to the applicant key.
- ``POST /api/login/start`` ``{"email"}`` → a code to any address, 202 ``{"sent": true}``.
  ``email`` may be ``<username>@<this belfry's domain>``: the code then goes to that member's
  own e-mail address (``verify`` takes the same words); a username nobody has is an address
  like any other.
- ``POST /api/login/verify`` ``{"email", "code"}`` → ``signed_in`` (an account), ``pending``
  (an application that waits: this key becomes its applicant key) or ``unknown`` with a
  ``proof`` for ``/api/apply/start``. With ``"site": true`` (the community's website,
  sitesessions.py): the key becomes a site session of the account, no ``bunker_url``.

Accepting such an application (membership.accept: an admin, or enough vouches) makes the person a
member at once: contact (their name), free membership, the username they asked for when it is
still free (else the first free suggestion), their ticked agreements signed, one welcome mail. No
invite. Needs the belfry's own register (REGISTER=belfry): 409 ``unsupported`` otherwise.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .clients.odoo import OdooError
from .core import Services
from .events import Limiter
from .holder_invites import ApiError, authenticate
from .mailer import masked

log = logging.getLogger(__name__)

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MOTIVATION_MAX = 4000
PROOF_SECONDS = 900
NO_STORE = {"Cache-Control": "no-store"}
# codes asked from one address of the network, on top of the register's limits per e-mail
# address and per key (register.py CODES_PER_EMAIL / CODES_PER_KEY)
PER_IP = Limiter(20, 3600)
_lock = threading.RLock()


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


# ── the proof of an address, for a key (login → apply without a second code) ───────────────
def _secret(svc: Services) -> bytes:
    key = (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()
    return hashlib.sha256(b"belfry-apply-proof\0" + key).digest()


def _mac(svc: Services, email: str, key: str, exp: int) -> str:
    msg = f"{email.strip().lower()}\n{key.lower()}\n{exp}".encode()
    return (
        base64.urlsafe_b64encode(hmac.new(_secret(svc), msg, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )


def make_proof(svc: Services, email: str, key: str, now: float | None = None) -> str:
    """EMAIL was proven by KEY just now (the right code): good for PROOF_SECONDS, for that key
    and that address only."""
    exp = int(time.time() if now is None else now) + PROOF_SECONDS
    return f"{exp}.{_mac(svc, email, key, exp)}"


def check_proof(svc: Services, proof: Any, email: str, key: str) -> bool:
    if not isinstance(proof, str) or len(proof) > 100:
        return False
    head, _, mac = proof.partition(".")
    if not head.isdigit() or not mac:
        return False
    exp = int(head)
    if exp < time.time() or exp > time.time() + PROOF_SECONDS + 60:
        return False
    return hmac.compare_digest(mac, _mac(svc, email, key, exp))


# ── an address that is proven ───────────────────────────────────────────────────────────────
def _account(svc: Services, email: str) -> dict[str, Any] | None:
    """The account that has EMAIL (a member, or a guest who saved it), now that it is proven. A
    buyer whose address was not proven yet has it from here on (sales.confirm, as connect.py)."""
    from . import apps, sales

    member = apps.signin_member(svc, email)  # an admin whose pass lapsed too (own signer)
    if member is None:
        buyer = sales.pending_member(svc, email)
        if buyer is not None:
            sales.confirm(svc, buyer["partner"], buyer["email"])
            member = apps.member_by_email(svc, email)
    return member


def sign_in(svc: Services, email: str, key: str, client: str) -> dict[str, Any] | None:
    """EMAIL, proven by KEY, has an account: a session on it bound to KEY (guests._sign_in_as) →
    ``{"state": "signed_in", "bunker_url", "pubkey", "npub", "nip05"}``. None: no account has
    it. 409 ``taken`` (with ``nip05``): the account holds its own key, its own signer signs it
    in; 409 ``key_taken``; 503 ``signer``."""
    from . import guests

    member = _account(svc, email)
    if member is None:
        return None
    opened = guests._sign_in_as(svc, int(member["partner"]), key, client)
    if not opened:
        exc = ApiError(409, "taken", "This address has an account here already: log in with it.")
        if nip05 := guests._account_nip05(svc, int(member["partner"])):
            exc.extra["nip05"] = nip05
        raise exc
    log.info("apply: %s signed in with key %s…", masked([email]), key[:8])
    return {"state": "signed_in", **{k: v for k, v in opened.items() if k != "signed_in"}}


def _waiting(svc: Services) -> list[dict[str, Any]]:
    from . import guestapply, membership

    rows = svc.odoo.search_read(
        "belfry.application", [["state", "=", "new"]], membership.FIELDS, order="id desc"
    )
    return [r for r in rows if r.get("kind") != guestapply.KIND]


def take_over(svc: Services, email: str, key: str, verified_only: bool = False) -> bool:
    """An application for the proven EMAIL waits: KEY is its applicant key from now on (the key
    that proved the address last), and it counts as verified. False: none waits. With
    VERIFIED_ONLY a request nobody proved (an old form: anyone may have typed that address and
    those words) is left alone: the owner of the address applies in their own words."""
    with _lock:
        for r in _waiting(svc):
            if (r.get("email") or "").strip().lower() != email.lower():
                continue
            if verified_only and not r.get("verified"):
                continue
            if r.get("client") != key or not r.get("verified"):
                svc.odoo.write("belfry.application", [r["id"]], {"client": key, "verified": True})
                log.info("apply: request %s goes on with key %s…", r["id"], key[:8])
            return True
    return False


def store(svc: Services, email: str, key: str, form: dict[str, Any]) -> dict[str, Any]:
    """The application of a proven address: stored once, the admins told once. A request that
    waits already for this key or this address is the answer (no duplicate, no second mail)."""
    with _lock:
        waiting = _waiting(svc)
        if any(r.get("client") == key and r.get("verified") for r in waiting):
            return {"state": "pending"}
        mine = next(
            (r for r in waiting if (r.get("email") or "").strip().lower() == email.lower()), None
        )
        if mine is not None:
            vals: dict[str, Any] = {"client": key, "verified": True}
            if not mine.get("verified"):
                # asked before on the old form, unproven: the proven person's words stand
                vals.update(
                    name=form["name"],
                    motivation=form["motivation"],
                    agreed=form["agreed"],
                    username=form.get("username") or False,
                )
            svc.odoo.write("belfry.application", [mine["id"]], vals)
            return {"state": "pending"}
        aid = svc.odoo.create(
            "belfry.application",
            {
                "name": form["name"],
                "email": email,
                "motivation": form["motivation"],
                "kind": "visitor",
                "state": "new",
                "handled_by": False,
                "duplicate_note": False,
                "agreed": form["agreed"],
                "vouched_by": [],
                "verified": True,
                "client": key,
                "username": form.get("username") or False,
            },
        )
    log.info("apply: request %s from %s (key %s…)", aid, masked([email]), key[:8])
    try:
        notify(svc, {"id": aid, "email": email, **form})
    except Exception:  # noqa: BLE001 — the request is in; the admins see it in the chat
        log.exception("apply: admins not told about request %s", aid)
    return {"state": "pending"}


def notify(svc: Services, row: dict[str, Any]) -> None:
    """The admins' mail of a request to join (the words of the web form's) and their phones."""
    from . import agentrequests, console, guestapply
    from .agentrequests import text

    emails = sorted({e for e in console.admin_emails(svc) if e and "@" in e})
    community = svc.settings.community_name or "your belfry"
    chat = f"https://chat.{svc.settings.nip05_domain}/#/requests"
    name = row.get("name") or "Someone"
    mailer = getattr(svc.odoo, "mailer", None)
    if mailer is not None and emails:
        mailer.send(
            emails,
            f"{community}: {name} asks to join",
            [
                f"{name} ({row.get('email') or ''}) asks to join:",
                row.get("motivation") or "",
                "Answer in the chat, under Members (top of the channel list).",
            ],
            button=(chat, "Review the request"),
            footer="You get this because you are an admin of " + community + ".",
        )
    guestapply._push(
        svc,
        [a["pubkey"] for a in agentrequests.admins(svc)],
        text(name, 40) + " asks to join",
        chat,
    )


def proven(svc: Services, email: str, key: str, form: dict[str, Any], client: str) -> dict:
    """What applying ends in once the address is proven: signed in (it has an account), or the
    application waits."""
    return sign_in(svc, email, key, client) or store(svc, email, key, form)


# ── the form ────────────────────────────────────────────────────────────────────────────────
def _form(svc: Services, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(e-mail, {"name", "motivation", "agreed", "username"}) of a request, checked as
    /api/apply checks. ``username`` is as typed here: ``start`` checks it."""
    from . import apply, charter

    email = str(body.get("email") or "").strip()
    motivation = str(body.get("motivation") or "").strip()
    if not email or not motivation:
        raise ApiError(400, "missing", apply.MISSING_API)
    if not EMAIL.match(email) or len(email) > 200:
        raise ApiError(422, "invalid", "That is not an e-mail address.")
    raw = body.get("agreed")
    ticked = [str(v) for v in raw][:100] if isinstance(raw, list) else []
    rules = apply._agreements(svc)
    if {r["id"] for r in rules} - set(ticked):
        raise ApiError(400, "unticked", apply.UNTICKED)
    known = {r["id"] for r in rules}
    name = " ".join(str(body.get("name") or "").split())
    username = str(body.get("username") or "").strip()
    if username and not name:
        raise ApiError(400, "missing", "Please give your name.")
    name = name or email.split("@")[0]  # older chats: no name, no username
    return email, {
        "username": username[:64],
        "name": name[:200],
        "motivation": motivation[:MOTIVATION_MAX],
        "agreed": charter.clean_ids([t for t in ticked if t in known]) or [],
    }


def _send(svc: Services, email: str, key: str, purpose: str, data: Any, ip: str) -> None:
    if not PER_IP.allow("ip:" + ip):
        raise ApiError(429, "too_many", "Too many codes for now. Try again in half an hour.")
    try:
        row = svc.odoo.call(
            "belfry.key.code", "belfry_key_code_send", [email, key, False, purpose, data]
        )
    except OdooError as exc:
        raise ApiError(422, "invalid", "That is not an e-mail address.") from exc
    if row.get("state") == "too_many":
        raise ApiError(429, "too_many", "Too many codes for now. Try again in half an hour.")


def _check(svc: Services, email: str, key: str, code: str, purpose: str) -> dict[str, Any]:
    """The data that rode on the right code; 422 ``wrong_code`` / 410 ``expired``."""
    try:
        out = svc.odoo.check_key_code(email, key, code, purpose)
    except OdooError as exc:
        raise ApiError(410, "expired", "That code expired. Send a new one.") from exc
    if out.get("state") == "wrong_code":
        left = int(out.get("tries_left") or 0)
        exc = ApiError(422, "wrong_code", f"That code is not right ({left} tries left).")
        exc.extra["tries_left"] = left
        raise exc
    if out.get("state") != "ok":
        raise ApiError(410, "expired", "That code expired. Send a new one.")
    data = out.get("data")
    return data if isinstance(data, dict) else {}


def start(svc: Services, key: str, body: dict[str, Any], ip: str) -> tuple[dict[str, Any], int]:
    if str(body.get("website") or "").strip():  # the hidden field: only bots fill it in
        log.info("apply: honeypot filled, dropped")
        return {"sent": True}, 202
    email, form = _form(svc, body)
    if form["username"]:
        from . import usernames

        # 422 username_invalid, 409 username_taken (with suggestions)
        form["username"] = usernames.require(svc, form["username"], form["name"])
    if body.get("proof") is not None:
        if not check_proof(svc, body.get("proof"), email, key):
            raise ApiError(422, "proof", "Confirm your e-mail address with a code.")
        return proven(svc, email, key, form, str(body.get("client") or "")), 200
    _send(svc, email, key, "apply", form, ip)
    return {"sent": True}, 202


def verify(svc: Services, key: str, body: dict[str, Any]) -> dict[str, Any]:
    email = str(body.get("email") or "").strip()
    form = _check(svc, email, key, str(body.get("code") or ""), "apply")
    if not form.get("motivation"):
        raise ApiError(410, "expired", "That code expired. Send a new one.")
    form = {
        "name": str(form.get("name") or email.split("@")[0])[:200],
        "motivation": str(form["motivation"])[:MOTIVATION_MAX],
        "agreed": [a for a in form.get("agreed") or [] if isinstance(a, str)],
        "username": str(form.get("username") or "")[:64],
    }
    return proven(svc, email, key, form, str(body.get("client") or ""))


def own_address(svc: Services, typed: str) -> str:
    """The e-mail address TYPED stands for when logging in: itself, or, for ``<username>@<this
    belfry's domain>`` that is no account's e-mail address, the own e-mail address of the member
    with that username (the code goes there, never to the belfry mailbox). A username nobody
    has is an address like any other."""
    from . import apps, usernames

    local, _, domain = typed.partition("@")
    name = usernames.clean(local)
    if domain.lower() != (svc.settings.nip05_domain or "").lower() or not name:
        return typed
    if usernames.problem(name) in ("long", "chars") or apps.member_by_email(svc, typed):
        return typed
    try:
        pubkey = svc.keycast.pubkey_for_name(name)
    except Exception as exc:  # noqa: BLE001 — as an address nobody has
        log.info("login: the signer did not answer for a name: %s", exc)
        return typed
    if not pubkey:
        return typed
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]],
        ["partner_id"],
        limit=1,
    )
    pid = rows[0].get("partner_id") if rows else None
    pid = pid[0] if isinstance(pid, list) else pid
    partner = svc.odoo.read_one("res.partner", int(pid), ["email"]) if pid else None
    email = str((partner or {}).get("email") or "").strip()
    return email if EMAIL.match(email) else typed


def login_start(svc: Services, key: str, body: dict[str, Any], ip: str) -> dict[str, Any]:
    email = str(body.get("email") or "").strip()
    if not EMAIL.match(email) or len(email) > 200:
        raise ApiError(422, "invalid", "That is not an e-mail address.")
    _send(svc, own_address(svc, email), key, "login", False, ip)
    return {"sent": True}


def login_verify(svc: Services, key: str, body: dict[str, Any], agent: str = "") -> dict[str, Any]:
    email = str(body.get("email") or "").strip()
    if EMAIL.match(email) and len(email) <= 200:
        email = own_address(svc, email)
    site = body.get("site") is True  # the community's website: a site session, never a bunker
    if site:
        from . import sitesessions

        sitesessions.before_code(svc, key, body)
    _check(svc, email, key, str(body.get("code") or ""), "login")
    if site:
        return sitesessions.login(svc, key, email, body, agent)
    opened = sign_in(svc, email, key, str(body.get("client") or ""))
    if opened is not None:
        return opened
    if take_over(svc, email, key, verified_only=True):
        return {"state": "pending"}
    return {"state": "unknown", "proof": make_proof(svc, email, key)}


# ── the answer, for the browser that waits ──────────────────────────────────────────────────
def status(svc: Services, key: str, client: str = "") -> dict[str, Any]:
    """One register lookup while the request waits; the session is made on the first call after
    the acceptance, once."""
    from . import guests, membership

    rows = svc.odoo.search_read(
        "belfry.application",
        [["client", "=", key], ["verified", "=", True]],
        membership.FIELDS,
        limit=1,
        order="id desc",
    )
    if not rows:
        return {"state": "none"}
    row = rows[0]
    state = row.get("state")
    if state == "new":
        # their own words, shown back to the browser that holds the applicant key
        return {
            "state": "pending",
            "name": row.get("name") or "",
            "username": row.get("username") or "",
            "email": row.get("email") or "",
            "motivation": row.get("motivation") or "",
        }
    if state == "declined":
        return {"state": "declined"}
    pid = row.get("member_partner")
    if state != "accepted" or not pid:
        return {"state": "none"}
    with _lock:
        row = svc.odoo.read_one("belfry.application", row["id"], ["session_at", "nip05"]) or {}
        if row.get("session_at"):  # fetched before, or its answer was lost: they log in
            return {"state": "approved", "signed_in": False, "nip05": row.get("nip05") or ""}
        nip05 = ""
        try:
            opened = guests._sign_in_as(svc, int(pid), key, client)
        except ApiError as exc:
            if exc.error != "key_taken":
                raise
            opened = None  # the key has a session already: nothing to hand out twice
        if opened:
            nip05 = opened["nip05"]
        else:
            nip05 = guests._account_nip05(svc, int(pid))
        svc.odoo.write(
            "belfry.application", [rows[0]["id"]], {"session_at": _stamp(), "nip05": nip05}
        )
    if not opened:
        return {"state": "approved", "signed_in": False, "nip05": nip05}
    log.info("apply: request %s: session for key %s…", rows[0]["id"], key[:8])
    return {"state": "approved", **opened}


# ── accepted ────────────────────────────────────────────────────────────────────────────────
def direct(row: dict[str, Any]) -> bool:
    """Whether accepting ROW makes a member at once (a proven address and an applicant key)."""
    return bool(row.get("verified") and row.get("client"))


def accept(svc: Services, row: dict[str, Any], by: str) -> None:
    """The applicant becomes a member now: their contact and a free membership (the activation
    an accepted invite ends in: default role, username from the name, the welcome mail), then
    the agreements they ticked are signed for them. No invite and no other mail."""
    from . import apps, core, guestapply, guests, roles
    from .places import member_key

    email = (row.get("email") or "").strip()
    # the name and the username they gave (the newer form); the activation gives the username
    # when it is still free, else the first free suggestion (usernames.give_default)
    theirs: dict[str, Any] = {}
    if row.get("username"):
        theirs = {
            "belfry_wanted_username": str(row["username"]),
            "name": str(row.get("name") or "").strip()[:100] or email,
        }
    pid: int | None = None
    try:
        account = apps.member_by_email(svc, email)
        if account is not None and member_key(svc, account["pubkey"]) is None:
            # a guest with this address since they asked: a member on the same key
            pid = int(account["partner"])
            if theirs:
                svc.odoo.write("res.partner", [pid], theirs)
            roles.make_member(svc, account["pubkey"], roles.default_role(svc), 0)
            if theirs:
                _profile_name(svc, pid, account["pubkey"], theirs["name"])
        else:
            made = svc.odoo.application_pass(int(row["id"]))
            pid = int(made["partner_id"])
            if made["state"] != "active":
                if theirs:
                    svc.odoo.write("res.partner", [pid], theirs)
                core.activate_free_pass(svc, made["pass_id"])
            state = (svc.odoo.read_one("belfry.pass", made["pass_id"], ["state"]) or {}).get(
                "state"
            )
            if state != "active":
                raise RuntimeError(f"pass {made['pass_id']} is {state}")
            apps.forget()
    except ApiError:
        raise
    except Exception as exc:
        log.exception("apply: request %s: the membership did not open", row["id"])
        raise ApiError(
            503, "failed", "The membership could not be opened just now. Try again."
        ) from exc
    vals: dict[str, Any] = {
        "state": guestapply.ACCEPTED,
        "handled_by": by,
        "handled_at": _stamp(),
        "member_partner": pid,
    }
    if theirs and (got := guests._account_nip05(svc, pid)):
        vals["username"] = got.split("@")[0]  # the one they got
    svc.odoo.write("belfry.application", [row["id"]], vals)
    log.info("apply: request %s accepted (%s): partner %s is a member", row["id"], by, pid)
    try:
        guestapply.ticks(svc, row, pid)
    except Exception:  # noqa: BLE001 — they can tick them in the app
        log.exception("apply: request %s: charter ticks not published", row["id"])


def _profile_name(svc: Services, partner: int, pubkey: str, name: str) -> None:
    """A guest who had a profile already: it gets the name they applied with (best effort)."""
    from . import community, usernames

    try:
        found = community.member_token(svc, partner)
        if not found or found[0] != pubkey:
            return
        token = found[1]
        username = usernames.current(svc, token)
        if username:
            usernames.publish_nip05(
                svc,
                pubkey,
                lambda u: svc.keycast.sign_event(token, {"pubkey": pubkey, **u}),
                username,
                {"name": name, "display_name": name},
            )
    except Exception as exc:  # noqa: BLE001 — theirs to set in their profile
        log.info("apply: profile of partner %s not renamed: %s", partner, exc)


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from .sales import client_ip

    async def signed(request: Request, path: str) -> tuple[Services, str, dict[str, Any]]:
        s: Services = request.app.state.services
        raw = await request.body()
        key = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", path, raw
        )
        if not own(s):
            raise ApiError(409, "unsupported", "Applying here needs the belfry's own register.")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        return s, key, body

    @app.post("/api/apply/start")
    async def apply_start(request: Request) -> JSONResponse:
        s, key, body = await signed(request, "/api/apply/start")
        out, code = await run_in_threadpool(start, s, key, body, client_ip(request))
        return JSONResponse(out, status_code=code, headers=NO_STORE)

    @app.post("/api/apply/verify")
    async def apply_verify(request: Request) -> JSONResponse:
        s, key, body = await signed(request, "/api/apply/verify")
        return JSONResponse(await run_in_threadpool(verify, s, key, body), headers=NO_STORE)

    @app.post("/api/apply/status")
    async def apply_status(request: Request) -> JSONResponse:
        s, key, body = await signed(request, "/api/apply/status")
        out = await run_in_threadpool(status, s, key, str(body.get("client") or ""))
        return JSONResponse(out, headers=NO_STORE)

    @app.post("/api/login/start")
    async def login_start_api(request: Request) -> JSONResponse:
        s, key, body = await signed(request, "/api/login/start")
        out = await run_in_threadpool(login_start, s, key, body, client_ip(request))
        return JSONResponse(out, status_code=202, headers=NO_STORE)

    @app.post("/api/login/verify")
    async def login_verify_api(request: Request) -> JSONResponse:
        s, key, body = await signed(request, "/api/login/verify")
        agent = request.headers.get("user-agent") or ""
        out = await run_in_threadpool(login_verify, s, key, body, agent)
        return JSONResponse(out, headers=NO_STORE)
