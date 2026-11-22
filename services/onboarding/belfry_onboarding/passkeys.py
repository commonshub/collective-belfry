"""Passkeys: sign in without an e-mail code (#issues 2026-11-15, Xavier: "Allow users to add a
passkey to their account instead of or on top of their email"). Phase 1: members add passkeys
and sign in with one on the belfry's sign-in page; e-mail stays as it is.

- **Relying party.** RP ID = ``<domain>``, so one passkey works on ``chat.``, ``belfry.`` and
  ``pass.<domain>``. Assertions are only taken from those three origins: never from the apex
  ``https://<domain>``, which serves member-proposed websites (a published page must not be able
  to ask for an assertion and pass it on).
- **Stored** in the register (``belfry.passkey``: partner, credential id, COSE public key, sign
  count, label, created, last used, active); at most ``MAX_PER_ACCOUNT``. Removing one marks it
  inactive. With Odoo: 409 ``unsupported``.
- **Challenges** are single use, five minutes, kept in memory (at most ``MAX_PENDING``).
  ``userVerification`` is required; credentials are discoverable (nothing to type first). A sign
  count goes up or stays 0: synced passkeys (iCloud, Google) always say 0, so a regression is
  refused only when the stored count is above 0.
- ``POST /api/passkeys`` (belfry.<domain>/api/v1/passkeys), NIP-98 by a member's key (not a
  guest's, not an agent's):
  - ``{}`` → ``{"passkeys": [{"id", "label", "created", "last_used"}], "email": bool}``;
  - ``{"options": "add"}`` → creation options (WebAuthn JSON); ``{"add": <credential>, "label"?}``
    → the list;
  - ``{"options": "confirm"}`` → request options for this account's passkeys; ``{"remove": <id>,
    "confirm": <assertion>}`` → the list. Removing needs a fresh assertion by one of the account's
    passkeys, so an open session someone else holds can't lock the owner out; the last passkey of
    an account without e-mail can't go;
  - ``{"rename": <id>, "label"}`` → the list.
- **Signing in** (connect.py, which pass.<domain>/me/login shares): ``/passkey/options`` then
  ``/passkey/verify`` take the place of the e-mail code; what follows is the same as after a right
  code. Native apps: later (sessions.py).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services, m2o_id

log = logging.getLogger(__name__)

MODEL = "belfry.passkey"
MAX_PER_ACCOUNT = 10
MAX_PENDING = 2000
TTL = 300
_pending: dict[str, tuple[bytes, float]] = {}
_lock = threading.Lock()


class PasskeyError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def own(svc: Services) -> bool:
    from . import invite

    return invite.own(svc)


def rp_id(svc: Services) -> str:
    return svc.settings.nip05_domain


def origins(svc: Services) -> list[str]:
    """Where an assertion may come from: the belfry's own apps, never the apex (member websites)."""
    d = rp_id(svc)
    return [f"https://chat.{d}", f"https://belfry.{d}", f"https://pass.{d}"]


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── challenges ──────────────────────────────────────────────────────────────────────────────
def _keep(key: str, challenge: bytes) -> None:
    now = time.monotonic()
    with _lock:
        for k in [k for k, (_c, at) in _pending.items() if now - at > TTL]:
            del _pending[k]
        if len(_pending) >= MAX_PENDING:
            raise PasskeyError(429, "too_many", "Too many sign-ins at once. Try again in a minute.")
        _pending[key] = (challenge, now)


def _take(key: str) -> bytes:
    with _lock:
        got = _pending.pop(key, None)
    if got is None or time.monotonic() - got[1] > TTL:
        raise PasskeyError(410, "expired", "That took too long. Try again.")
    return got[0]


# ── the register ────────────────────────────────────────────────────────────────────────────
def _rows(svc: Services, partner: int | None = None) -> list[dict[str, Any]]:
    domain: list[Any] = [["active", "=", True]]
    if partner is not None:
        domain.append(["partner_id", "=", partner])
    return svc.odoo.search_read(
        MODEL,
        domain,
        [
            "partner_id",
            "credential_id",
            "public_key",
            "sign_count",
            "label",
            "created",
            "last_used",
        ],
    )


def _listing(svc: Services, partner: int) -> dict[str, Any]:
    p = svc.odoo.read_one("res.partner", partner, ["email"]) or {}
    return {
        "passkeys": [
            {
                "id": r["id"],
                "label": r.get("label") or "Passkey",
                "created": r.get("created") or "",
                "last_used": r.get("last_used") or "",
            }
            for r in _rows(svc, partner)
        ],
        "email": bool((p.get("email") or "").strip()),
    }


def _handle(svc: Services, partner: int) -> bytes:
    """An opaque user handle per account (not the key, not the partner id)."""
    secret = (svc.settings.pass_page_secret or "belfry-passkeys").encode()
    return hmac.new(secret, f"passkey-user:{partner}".encode(), hashlib.sha256).digest()[:16]


# ── adding and removing (a signed-in member) ────────────────────────────────────────────────
def add_options(svc: Services, partner: int, name: str) -> dict[str, Any]:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    from . import theme

    rows = _rows(svc, partner)
    if len(rows) >= MAX_PER_ACCOUNT:
        raise PasskeyError(409, "limit", f"At most {MAX_PER_ACCOUNT} passkeys per account.")
    opts = generate_registration_options(
        rp_id=rp_id(svc),
        rp_name=theme.current().name or rp_id(svc),
        user_id=_handle(svc, partner),
        user_name=name or rp_id(svc),
        user_display_name=name or rp_id(svc),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=unb64url(r["credential_id"])) for r in rows
        ],
    )
    _keep(f"add:{partner}", opts.challenge)
    return json.loads(options_to_json(opts))


def add(svc: Services, partner: int, credential: Any, label: str) -> dict[str, Any]:
    from webauthn import verify_registration_response

    challenge = _take(f"add:{partner}")
    try:
        v = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(svc),
            expected_origin=origins(svc),
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 — a bad credential, any reason
        log.info("passkeys: registration refused: %s", exc)
        raise PasskeyError(422, "invalid", "This passkey could not be added.") from exc
    cid = b64url(v.credential_id)
    if svc.odoo.search_read(MODEL, [["credential_id", "=", cid], ["active", "=", True]], ["id"]):
        raise PasskeyError(409, "taken", "This passkey is added already.")
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    svc.odoo.create(
        MODEL,
        {
            "partner_id": partner,
            "credential_id": cid,
            "public_key": b64url(v.credential_public_key),
            "sign_count": int(v.sign_count),
            "label": (label or "").strip()[:60] or "Passkey",
            "created": now,
            "last_used": "",
            "active": True,
        },
    )
    log.info("passkeys: partner %s added a passkey", partner)
    return _listing(svc, partner)


def confirm_options(svc: Services, partner: int) -> dict[str, Any]:
    """Request options for this account's own passkeys (a fresh proof before removing one)."""
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    rows = _rows(svc, partner)
    opts = generate_authentication_options(
        rp_id=rp_id(svc),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=unb64url(r["credential_id"])) for r in rows
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _keep(f"confirm:{partner}", opts.challenge)
    return json.loads(options_to_json(opts))


def remove(svc: Services, partner: int, pid: Any, assertion: Any) -> dict[str, Any]:
    if authenticate(svc, f"confirm:{partner}", assertion) != partner:
        raise PasskeyError(403, "not_yours", "Confirm with one of your own passkeys.")
    rows = _rows(svc, partner)
    row = next((r for r in rows if r["id"] == pid), None)
    if row is None:
        raise PasskeyError(404, "not_found", "No such passkey.")
    if len(rows) == 1 and not _listing(svc, partner)["email"]:
        raise PasskeyError(
            409, "last", "This is your only way in: add an e-mail or another passkey first."
        )
    svc.odoo.write(MODEL, [row["id"]], {"active": False})
    log.info("passkeys: partner %s removed a passkey", partner)
    return _listing(svc, partner)


def rename(svc: Services, partner: int, pid: Any, label: str) -> dict[str, Any]:
    row = next((r for r in _rows(svc, partner) if r["id"] == pid), None)
    if row is None:
        raise PasskeyError(404, "not_found", "No such passkey.")
    svc.odoo.write(MODEL, [row["id"]], {"label": (label or "").strip()[:60] or "Passkey"})
    return _listing(svc, partner)


# ── signing in ──────────────────────────────────────────────────────────────────────────────
def sign_in_options(svc: Services, key: str) -> dict[str, Any]:
    """Request options for any passkey of this belfry (discoverable: nothing typed first)."""
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import UserVerificationRequirement

    opts = generate_authentication_options(
        rp_id=rp_id(svc), user_verification=UserVerificationRequirement.REQUIRED
    )
    _keep(f"in:{key}", opts.challenge)
    return json.loads(options_to_json(opts))


def authenticate(svc: Services, key: str, credential: Any) -> int:
    """The account (partner id) a passkey assertion proves, for the challenge kept under KEY."""
    from webauthn import verify_authentication_response

    challenge = _take(key)
    try:
        cid = str((credential or {}).get("rawId") or (credential or {}).get("id") or "")
    except AttributeError:
        cid = ""
    rows = svc.odoo.search_read(
        MODEL,
        [["credential_id", "=", cid], ["active", "=", True]],
        ["partner_id", "public_key", "sign_count"],
        limit=1,
    )
    if not cid or not rows:
        raise PasskeyError(401, "unknown", "This passkey is not known here.")
    row = rows[0]
    stored = int(row.get("sign_count") or 0)
    try:
        v = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(svc),
            expected_origin=origins(svc),
            credential_public_key=unb64url(row["public_key"]),
            # synced passkeys always say 0: a regression only counts above 0
            credential_current_sign_count=stored,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("passkeys: assertion refused: %s", exc)
        raise PasskeyError(401, "refused", "This passkey could not be checked.") from exc
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    svc.odoo.write(
        MODEL, [row["id"]], {"sign_count": max(stored, int(v.new_sign_count)), "last_used": now}
    )
    return int(m2o_id(row["partner_id"]) or 0)


def member_of_partner(svc: Services, partner: int) -> dict[str, Any] | None:
    """The member a passkey signs in (the shape connect.py's code path hands on)."""
    from . import apps

    p = svc.odoo.read_one("res.partner", partner, ["email"]) or {}
    return apps.signin_member(svc, p.get("email") or "")


def new_flow_key() -> str:
    return secrets.token_urlsafe(16)


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    from .holder_invites import ApiError
    from .holder_invites import authenticate as signed
    from .places import member_key

    PATH = "/api/passkeys"

    def caller_partner(svc: Services, caller: str) -> tuple[int, str]:
        row = member_key(svc, caller)
        if row is None or row.get("tier") == "agent":
            raise ApiError(403, "not_member", "Only members add passkeys.")
        partner = int(m2o_id(row.get("partner_id")) or 0)
        p = svc.odoo.read_one("res.partner", partner, ["name"]) or {}
        return partner, p.get("name") or ""

    def run(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
        if not own(svc):
            raise ApiError(409, "unsupported", "Passkeys need the belfry's own register.")
        partner, name = caller_partner(svc, caller)
        try:
            if data.get("options") == "add":
                return add_options(svc, partner, name)
            if data.get("options") == "confirm":
                return confirm_options(svc, partner)
            if "add" in data:
                return add(svc, partner, data["add"], str(data.get("label") or ""))
            if "remove" in data:
                return remove(svc, partner, data["remove"], data.get("confirm"))
            if "rename" in data:
                return rename(svc, partner, data["rename"], str(data.get("label") or ""))
            return _listing(svc, partner)
        except PasskeyError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from exc

    @app.post(PATH)
    async def passkeys(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = signed(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        out = await run_in_threadpool(run, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
