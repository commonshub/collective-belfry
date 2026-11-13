"""The belfry's branding: its logo, cover image and two colours, set by its admins and used
throughout (the chat, belfry.json, the mails). Xavier, 2026-11-11: "make sure admins of the belfry
can define the logo, cover image, primary and secondary color and reuse that throughout (including
in email). Those should be recorded in the nostr profile of the belfry main nostr account."

They live in the kind-0 profile of the belfry's own key (secret ``belfry_nsec``, the ``nostr``
key of belfry.json) on the community relay: ``picture`` (the logo), ``banner`` (the cover) and
``primary_color`` / ``secondary_color`` (``#rrggbb``), next to its ``name``, ``about`` and
``website``. Any Nostr client sees the logo and cover; Belfry's apps read the colours too.

- ``GET /api/branding`` (belfry.<domain>/api/v1/branding): ``{"logo","cover","primary",
  "secondary","description","name"}``, anyone. ``description`` is the profile's ``about``: the
  line under the name on the default homepage.
- ``POST /api/branding``: NIP-98 by an admin, any of ``logo``, ``cover`` (https URLs, "" clears),
  ``primary``, ``secondary`` (``#rrggbb``, "" clears), ``description`` (plain text, at most 500
  characters, "" clears). The profile is republished with the rest
  of it kept.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from html import escape
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)
PATH = "/api/branding"
TTL = 300
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_PRIMARY = "#2563eb"
# field in the API → field in the kind-0 profile
FIELDS = {
    "logo": "picture",
    "cover": "banner",
    "primary": "primary_color",
    "secondary": "secondary_color",
    "description": "about",
}
DEFAULT_DESCRIPTION = (
    "A sovereign community that is self organised and keeps its data on their own server."
)

_cache: dict[str, Any] = {"at": 0.0, "profile": {}, "created_at": 0}
_lock = threading.Lock()


def _signer(svc: Services):
    from .community import belfry_signer
    from .homepage import belfry_pubkey

    pubkey = belfry_pubkey(svc.settings)
    if not pubkey or svc.relay is None:
        return None
    return pubkey, belfry_signer(svc)


def _latest(svc: Services) -> tuple[dict[str, Any], int]:
    got = _signer(svc)
    if got is None:
        return {}, 0
    pubkey, sign = got
    events = [
        e
        for e in svc.relay.query([{"kinds": [0], "authors": [pubkey], "limit": 5}], sign) or []
        if e.get("pubkey") == pubkey
    ]
    if not events:
        return {}, 0
    latest = max(events, key=lambda e: e.get("created_at", 0))
    try:
        content = json.loads(latest.get("content") or "{}")
    except ValueError:
        content = {}
    return (content if isinstance(content, dict) else {}), int(latest.get("created_at", 0))


def _view(profile: dict[str, Any]) -> dict[str, str]:
    out = {k: str(profile.get(v) or "") for k, v in FIELDS.items()}
    out["name"] = str(profile.get("name") or "")
    return out


def current(svc: Services, fresh: bool = False) -> dict[str, str]:
    """The branding, read from the relay at most every TTL seconds (best effort)."""
    if fresh or time.monotonic() - _cache["at"] > TTL:
        try:
            profile, at = _latest(svc)
            with _lock:
                _cache.update(at=time.monotonic(), profile=profile, created_at=at)
        except Exception as exc:  # noqa: BLE001 — the last known one stays
            log.warning("branding: profile not read: %s", exc)
            _cache["at"] = time.monotonic() - TTL + 60  # try again in a minute
    return _view(_cache["profile"])


def cached() -> dict[str, str]:
    """The last branding read, without asking the relay (the mailer has no services)."""
    return _view(_cache["profile"])


def primary() -> str:
    return cached()["primary"] or DEFAULT_PRIMARY


def _clean(data: dict[str, Any]) -> dict[str, str]:
    from .holder_invites import ApiError

    change: dict[str, str] = {}
    for k in FIELDS:
        if k not in data:
            continue
        v = data[k]
        if not isinstance(v, str):
            raise ApiError(400, "invalid", f"{k}: a string.")
        v = v.strip()
        if (
            k in ("logo", "cover")
            and v
            and (not v.startswith("https://") or len(v) > 600 or any(c in v for c in ' "<>'))
        ):
            raise ApiError(400, "invalid", f"{k}: an https:// address of an image.")
        if k == "description" and len(v) > 500:
            raise ApiError(400, "invalid", "description: at most 500 characters.")
        if k in ("primary", "secondary") and v and not COLOR.match(v):
            raise ApiError(400, "invalid", f"{k}: a colour as #rrggbb.")
        change[k] = v.lower() if k in ("primary", "secondary") else v
    if not change:
        raise ApiError(
            400, "invalid_json", 'Send any of {"logo","cover","primary","secondary","description"}.'
        )
    return change


def save(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, str]:
    from . import apps
    from .holder_invites import ApiError

    if caller not in apps.admin_pubkeys(svc):
        raise ApiError(403, "not_admin", "Only the belfry's admins change its branding.")
    change = _clean(data)
    got = _signer(svc)
    if got is None:
        raise ApiError(503, "no_key", "This belfry has no key of its own to sign its profile with.")
    _pubkey, sign = got
    profile, at = _latest(svc)
    profile = dict(profile)
    if not profile.get("name"):
        profile["name"] = (svc.settings.community_name or "").strip() or svc.settings.nip05_domain
    for k, v in change.items():
        if v:
            profile[FIELDS[k]] = v
        else:
            profile.pop(FIELDS[k], None)
    event = sign(
        {
            "kind": 0,
            "created_at": max(int(time.time()), at + 1),
            "tags": [],
            "content": json.dumps(profile, separators=(",", ":"), ensure_ascii=False),
        }
    )
    svc.relay.publish(event, sign)
    with _lock:
        _cache.update(at=time.monotonic(), profile=profile, created_at=event["created_at"])
    log.info("branding: %s changed %s", caller[:12], ", ".join(sorted(change)))
    return _view(profile)


def mail_header(name: str, url: str = "") -> str:
    """The top of a mail: the logo (else the name's initial in the primary colour) and the name.
    People are in several communities: this says which one it is about."""
    b = cached()
    color = primary()
    logo = b["logo"]
    initial = escape((name.strip()[:1] or "B").upper())
    mark = (
        f'<img src="{escape(logo)}" alt="" width="28" height="28" style="width:28px;height:28px;'
        'border-radius:6px;vertical-align:middle;margin-right:8px;object-fit:cover;">'
        if logo
        else f'<span style="display:inline-block;width:24px;height:24px;line-height:24px;'
        f"border-radius:6px;background:{color};color:#fff;text-align:center;font-size:14px;"
        f'vertical-align:middle;margin-right:8px;">{initial}</span>'
    )
    inner = f'{mark}<span style="vertical-align:middle;">{escape(name)}</span>'
    style = (
        "display:inline-block;color:#18181b;text-decoration:none;font-weight:600;"
        "font-size:16px;margin:0 0 18px;"
    )
    if url:
        return f'<a href="{escape(url)}" style="{style}">{inner}</a>'
    return f'<div style="{style}">{inner}</div>'


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.get(PATH)
    async def get_branding(request: Request) -> JSONResponse:
        s = request.app.state.services
        return JSONResponse(
            await run_in_threadpool(current, s), headers={"Cache-Control": "no-cache"}
        )

    @app.post(PATH)
    async def set_branding(request: Request) -> JSONResponse:
        s = request.app.state.services
        body = await request.body()
        caller = authenticate(
            s, request.app.state.api_seen, request.headers.get("authorization"), "POST", PATH, body
        )
        try:
            data: Any = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ApiError(
                400, "invalid_json", 'Send any of {"logo","cover","primary","secondary"}.'
            )
        out = await run_in_threadpool(save, s, caller, data)
        return JSONResponse({"ok": True, **out}, headers={"Cache-Control": "no-store"})
