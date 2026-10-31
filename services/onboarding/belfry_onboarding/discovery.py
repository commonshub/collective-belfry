"""What an app needs to connect to this Belfry: ``/.well-known/belfry.json``, ``/api/me`` and
CORS for the JSON APIs.

``/.well-known/belfry.json`` (on the apex and on pass.<domain>) names the community, its relays,
file server, mint, sign-in, APIs, apps and agent skills, so an app asks for the domain only.

``GET /api/me`` answers for the key that signed the request (NIP-98, as ``/api/invites``):
whether it holds an active pass, its passes (also one ordered with this key and not yet paid),
its NIP-05 name, whether its holder is a steward and the e-mail address behind it (also one the
key proved with ``/api/email-code``). Unlike ``/api/invites`` it answers keys
without a pass too, so an app can show a visitor where they stand.

``API_CORS_ORIGINS`` lets pages on other origins (a local app, another community's site) call
``/api/*``; the discovery document answers every origin. No cookies are involved: the APIs
authenticate the NIP-98 header, so a page can only act with a key it signs with.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import pages
from .core import Services, m2o_id
from .holder_invites import authenticate
from .nostr import hex_to_npub

log = logging.getLogger(__name__)

WELL_KNOWN = "/.well-known/belfry.json"
ME = "/api/me"
VERSION = 1
# skills/ in the repo, served at /skills/<name>.md (pages.py); the visitor's also at /skill.md
SKILLS = {"visitor": "open-source-village", "steward": "belfry-steward"}


def _url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def document(settings: Any) -> dict[str, Any]:
    """The discovery document. Addresses not configured here are left out."""
    public = _url(settings.public_url)
    site = _url(settings.site_url) or public
    shop = _url(settings.odoo_public_url)
    domain = pages.site_domain(settings) or settings.nip05_domain
    relays = {
        "community": _url(settings.community_relay_public_url),
        "groups": _url(settings.groups_relay_public_url),
        "signer": [_url(r) for r in settings.bunker_relays.split(",") if r.strip()],
    }
    api = {
        "me": public + ME,
        "invites": public + "/api/invites",
        "email_code": public + "/api/email-code",
        "apply": site + "/apply",
        "invite_offer": shop + "/belfry/api/invites/{token}" if shop else "",
        "wallet_pass_tokens": public + "/wallet/pass-tokens",
    }
    doc = {
        "belfry": VERSION,
        "name": settings.community_name,
        "domain": domain,
        "nip05_domain": settings.nip05_domain,
        "site": site,
        "pass": public,
        "shop": shop,
        "api": {k: v for k, v in api.items() if v},
        "relays": {k: v for k, v in relays.items() if v},
        "blossom": _url(settings.blossom_public_url),
        "mint": {"url": _url(settings.mint_public_url), "unit": settings.mint_unit}
        if settings.mint_url
        else None,
        "sign_in": {
            "login": _url(settings.authentik_public_url),
            "keycast": _url(settings.apps_public_url),
        },
        "credential": {"vct": settings.vct, "issuer": settings.credential_issuer}
        if settings.issuer_url
        else None,
        "apps": [
            {"name": name, "url": url, "description": text}
            for name, url, text in pages.community_apps(os.environ.get("COMMUNITY_APPS", ""))
        ],
        "skills": {role: f"{public}/skills/{name}.md" for role, name in SKILLS.items()},
    }
    return {k: v for k, v in doc.items() if v not in ("", None)}


def me(svc: Services, pubkey: str) -> dict[str, Any]:
    """Where the key stands: its passes (Keycast's key or one of the holder's own) and holder."""
    fields = [
        "id",
        "state",
        "tier",
        "date_start",
        "date_end",
        "partner_id",
        "payment_state",
        "cashu_amount",
    ]
    found: dict[int, dict[str, Any]] = {}
    # keycast_pubkey is set at activation; own_pubkey already when ordering with one's own key
    for field in ("keycast_pubkey", "own_pubkey"):
        for row in svc.odoo.search_read("belfry.pass", [[field, "=", pubkey]], fields):
            found[row["id"]] = row
    rows = sorted(found.values(), key=lambda r: r["id"], reverse=True)
    passes = [
        {
            "id": r["id"],
            "state": r["state"],
            "tier": r.get("tier") or None,
            "date_start": r.get("date_start") or None,
            "date_end": r.get("date_end") or None,
            "payment": r.get("payment_state") or None,
            # the village tokens minted onto the pass at activation (in a wallet once collected)
            "tokens": r.get("cashu_amount") or 0,
        }
        for r in rows
    ]
    active = next((r for r in rows if r["state"] == "active"), None)
    partner_id = m2o_id((active or (rows[0] if rows else {})).get("partner_id"))
    partner_fields = ["email", "belfry_member_number", "belfry_authentik_user_id"]
    partner: dict[str, Any] = {}
    if partner_id:
        partner = svc.odoo.read_one("res.partner", partner_id, partner_fields) or {}
    else:
        # no pass yet: the contact whose address this key proved (/api/email-code)
        found_partners = svc.odoo.search_read(
            "res.partner", [["belfry_verified_pubkey", "=", pubkey]], partner_fields, limit=1
        )
        partner = found_partners[0] if found_partners else {}
    number = partner.get("belfry_member_number") or ""
    user_pk = partner.get("belfry_authentik_user_id")
    steward = bool(active and user_pk) and int(user_pk) in svc.authentik.group_member_pks(
        svc.settings.steward_group
    )
    return {
        "pubkey": pubkey,
        "npub": hex_to_npub(pubkey),
        "member": active is not None,
        "steward": steward,
        "nip05": f"{number.lower()}@{svc.settings.nip05_domain}" if active and number else None,
        # the address of the contact behind the passes or the one this key proved by e-mail
        "email": partner.get("email") or None,
        "passes": passes,
    }


def allowed_origin(settings: Any, origin: str | None) -> str | None:
    if not origin:
        return None
    allowed = [o.strip().rstrip("/") for o in settings.api_cors_origins.split(",") if o.strip()]
    if "*" in allowed:
        return "*"
    return origin if origin.rstrip("/") in allowed else None


def cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def register(app: FastAPI) -> None:
    """Mount /.well-known/belfry.json, /api/me and the CORS of /api/* (create_app calls this)."""

    @app.middleware("http")
    async def api_cors(request: Request, call_next):
        path = request.url.path
        if path == WELL_KNOWN:
            origin: str | None = "*"
        elif path.startswith("/api/"):
            settings = request.app.state.services.settings
            origin = allowed_origin(settings, request.headers.get("origin"))
        else:
            return await call_next(request)
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            if not origin:
                return Response(status_code=403)
            return Response(status_code=204, headers=cors_headers(origin))
        response = await call_next(request)
        if origin:
            response.headers.update(cors_headers(origin))
        return response

    @app.get(WELL_KNOWN)
    async def well_known(request: Request) -> JSONResponse:
        doc = document(request.app.state.services.settings)
        return JSONResponse(doc, headers={"Cache-Control": "public, max-age=300"})

    @app.get(ME)
    async def api_me(request: Request) -> JSONResponse:
        services: Services = request.app.state.services
        pubkey = authenticate(
            services,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "GET",
            ME,
            b"",
        )
        data = await run_in_threadpool(me, services, pubkey)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
