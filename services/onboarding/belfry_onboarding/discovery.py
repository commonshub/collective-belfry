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

import json
import logging
import re
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import device, pages
from .core import Services, m2o_id
from .holder_invites import ApiError, authenticate
from .nostr import hex_to_npub

log = logging.getLogger(__name__)

WELL_KNOWN = "/.well-known/belfry.json"
ME = "/api/me"
SESSIONS = "/api/me/sessions"
VERSION = 1
# skills/ in the repo, served at /skills/<name>.md (pages.py); the visitor's also at /skill.md
SKILLS = {
    "visitor": "open-source-village",
    "steward": "belfry-steward",
    "website": "belfry-homepage",
    "theme": "belfry-theme",
    "app": "belfry-app",
}


def _url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _belfry_key(settings: Any) -> dict[str, str]:
    from .homepage import belfry_pubkey

    pk = belfry_pubkey(settings)
    return {"pubkey": pk, "npub": hex_to_npub(pk)} if pk else {}


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
    # the belfry's API lives on belfry.<domain>/api/v1 (pass.<domain>/api still answers)
    console = _url(getattr(settings, "console_url", ""))
    base = (console + "/api/v1") if console else (public + "/api" if public else "")
    api = {
        "base": base,
        "me": base + ME.removeprefix("/api"),
        "invites": base + "/invites",
        "email_code": base + "/email-code",
        "session": base + "/session",
        "channel_invites": base + "/channel-invites",
        "follows": base + "/me/follows",
        "apply": site + "/apply",
        "invite_offer": shop + "/belfry/api/invites/{token}" if shop else "",
        "wallet_pass_tokens": public + "/wallet/pass-tokens",
    }
    doc = {
        "belfry": VERSION,
        "code": _code(settings),
        "name": settings.community_name,
        "domain": domain,
        "nip05_domain": settings.nip05_domain,
        "site": site,
        "pass": public,
        "shop": shop,
        "api": {k: v for k, v in api.items() if v},
        "relays": {k: v for k, v in relays.items() if v},
        "blossom": _url(settings.blossom_public_url),
        # the git server (ngit-grasp, GRASP-08): its relay and git HTTP share this address
        "git": _url(settings.git_public_url),
        "console": _url(getattr(settings, "console_url", "")),
        # the belfry's own key: signs its website (nsite) and receives proposals
        "nostr": _belfry_key(settings),
        "mint": {"url": _url(settings.mint_public_url), "unit": settings.mint_unit}
        if settings.mint_url
        else None,
        "sign_in": {
            "login": _url(settings.authentik_public_url),
            "keycast": _url(settings.apps_public_url),
            # Sign in with <domain> for web apps: OAuth code + PKCE, approved apps only (connect.py)
            "connect": {
                "authorize": _url(getattr(settings, "console_url", "")) + "/connect/authorize",
                "token": _url(getattr(settings, "console_url", "")) + "/connect/token",
                "logout": _url(getattr(settings, "console_url", "")) + "/connect/logout",
            }
            if getattr(settings, "console_url", "")
            else None,
            # name@domain sign-in for any app (sessions.py, skills/belfry-app): DM the belfry's
            # key from the app's session key on these relays (NIP-17), or use the HTTP API
            "sessions": {
                **_belfry_key(settings),
                "relays": [_url(r) for r in settings.bunker_relays.split(",") if r.strip()],
                "api": (
                    (_url(getattr(settings, "console_url", "")) + "/api/v1")
                    if getattr(settings, "console_url", "")
                    else public + "/api"
                )
                + "/session",
                "skill": f"{public}/skills/belfry-app.md",
            }
            if _belfry_key(settings)
            else None,
        },
        "credential": {"vct": settings.vct, "issuer": settings.credential_issuer}
        if settings.issuer_url
        else None,
        "apps": [
            {"name": name, "url": url, "description": text}
            for name, url, text in pages.listed_apps(settings)
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
        "nip05": member_nip05(svc, partner_id, number, pubkey) if active else None,
        # the address of the contact behind the passes or the one this key proved by e-mail
        "email": partner.get("email") or None,
        "passes": passes,
    }


def member_nip05(
    svc: Services, partner_id: int | None, number: str = "", pubkey: str = ""
) -> str | None:
    """The member's address: their username in the signer (``<username>@<domain>``), else the
    member number (the username the signer gives a new account)."""
    if partner_id:
        try:
            from . import community

            found = community.member_token(svc, int(partner_id))
            if found and (not pubkey or found[0] == pubkey):
                name = (svc.keycast.get_profile(found[1]) or {}).get("username")
                if name:
                    return f"{name}@{svc.settings.nip05_domain}"
        except Exception as exc:  # noqa: BLE001 — the signer not answering: the member number
            log.info("discovery: no username for partner %s: %s", partner_id, exc)
    return f"{number.lower()}@{svc.settings.nip05_domain}" if number else None


def allowed_origin(settings: Any, origin: str | None, svc: Services | None = None) -> str | None:
    """API_CORS_ORIGINS, the community's own domain (the apps the belfry installs live at
    https://<domain>/<name>/), plus the origin of every app an admin approved (apps.py)."""
    if not origin:
        return None
    allowed = [o.strip().rstrip("/") for o in settings.api_cors_origins.split(",") if o.strip()]
    domain = getattr(settings, "nip05_domain", "") or ""
    if "." in domain:
        from .appshome import OWN_HOST

        allowed.append(f"https://{domain}")
        allowed += [f"https://{name}.{domain}" for name in OWN_HOST]  # chat.<domain>
    if "*" in allowed:
        return "*"
    if origin.rstrip("/") in allowed:
        return origin
    if svc is not None:
        from . import apps

        try:
            for uri, a in apps.state(svc).items():
                u = httpx.URL(uri)
                if a["approved"] and origin.rstrip("/") == f"{u.scheme}://{u.netloc.decode()}":
                    return origin
        except Exception as exc:  # noqa: BLE001 — no relay: only the configured origins
            log.info("discovery: approved apps not readable: %s", exc)
    return None


def cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


def _session_row(r: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any]:
    row = {k: r.get(k) for k in keep}
    row["application_name"], row["device"] = device.split(r.get("application_name"))
    return row


def register(app: FastAPI) -> None:
    """Mount /.well-known/belfry.json, /api/me and the CORS of /api/* (create_app calls this)."""

    @app.middleware("http")
    async def api_cors(request: Request, call_next):
        path = request.url.path
        if path == WELL_KNOWN:
            origin: str | None = "*"
        elif path.startswith("/api/"):
            settings = request.app.state.services.settings
            origin = await run_in_threadpool(
                allowed_origin, settings, request.headers.get("origin"), request.app.state.services
            )
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
        services = request.app.state.services
        doc = document(services.settings)
        # the community's admins (keys): they administer every channel too (zooid patch 0003)
        try:
            from . import apps

            admins = sorted(await run_in_threadpool(apps.admin_pubkeys, services))
            if admins:
                doc["admins"] = admins
        except Exception as exc:  # noqa: BLE001 — the document must answer anyway
            log.warning("belfry.json: admins unknown: %s", exc)
        # members' own agents (agents.py): agent key → its member's key
        owners = getattr(services.odoo, "agent_owners", None)
        if owners is not None:
            try:
                found = await run_in_threadpool(owners)
                if found:
                    doc["agents"] = found
            except Exception as exc:  # noqa: BLE001
                log.warning("belfry.json: agents unknown: %s", exc)
        return JSONResponse(doc, headers={"Cache-Control": "public, max-age=300"})

    async def session_owner(
        request: Request, method: str, path: str, body: bytes
    ) -> tuple[int, str]:
        services: Services = request.app.state.services
        pubkey = authenticate(
            services,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            method,
            path,
            body,
        )
        from . import community
        from .holder_invites import holder

        partner = (await run_in_threadpool(holder, services, pubkey))["partner_id"]
        found = await run_in_threadpool(community.member_token, services, partner)
        if found is None or found[0] != pubkey:
            raise ApiError(409, "own_key", "This key is your own: its sessions are not here.")
        return partner, found[1]

    @app.get(SESSIONS)
    async def api_sessions(request: Request) -> JSONResponse:
        """The signed-in member's sessions (NIP-46 connections in the signer), NIP-98."""
        services: Services = request.app.state.services
        _, token = await session_owner(request, "GET", SESSIONS, b"")
        rows = await run_in_threadpool(services.keycast.list_sessions, token)
        keep = (
            "application_name",
            "redirect_origin",
            "bunker_pubkey",
            "client_pubkey",
            "created_at",
            "last_activity",
            "activity_count",
        )
        return JSONResponse(
            {"sessions": [_session_row(r, keep) for r in rows]},
            headers={"Cache-Control": "no-store"},
        )

    @app.post(SESSIONS + "/revoke")
    async def api_sessions_revoke(request: Request) -> JSONResponse:
        services: Services = request.app.state.services
        body = await request.body()
        _, token = await session_owner(request, "POST", SESSIONS + "/revoke", body)
        try:
            bunker = str(json.loads(body or b"{}").get("bunker_pubkey", ""))
        except ValueError:
            bunker = ""
        if not re.fullmatch(r"[0-9a-f]{64}", bunker):
            raise ApiError(422, "invalid", "bunker_pubkey: 64 hex characters")
        await run_in_threadpool(services.keycast.revoke_session, token, bunker)
        return JSONResponse({"state": "revoked"})

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


def _code(settings: Any) -> dict[str, str]:
    """The code this belfry runs (version.json, written by the host's belfry command)."""
    from .setup import read_json, setup_dir

    folder = setup_dir(settings)
    v = read_json(folder / "version.json") if folder else {}
    return {k: str(v[k]) for k in ("short", "commit", "branch", "date") if v.get(k)}
