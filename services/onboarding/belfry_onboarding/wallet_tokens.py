"""The pass's village tokens for the participant's wallet: ``GET /wallet/pass-tokens``.

Activation mints the tokens a pass brings onto the pass (core.issue_missing_tokens); before this,
they reached a wallet only through the "Get your tokens" link (``/tokens/<claim token>``), and a
participant who signed in to the wallet without opening it saw 0 tokens. The wallet now asks for
them after signing in, with a NIP-98 event (kind 27235) signed by the participant's key: the
answer is the token bundle of every active pass of that key whose tokens are still unspent at the
mint. A bundle is a bearer token, so it goes only to the key the pass belongs to.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .blossom import (
    BlossomAuthError,
    event_id,
    parse_authorization,
    tag_values,
    verify_signature,
)
from .core import Services

log = logging.getLogger(__name__)

PATH = "/wallet/pass-tokens"
NIP98_KIND = 27235
MAX_AGE = 60  # NIP-98: created_at within a minute


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def check_nip98(event: dict[str, Any], method: str, urls: list[str], now: int | None = None) -> str:
    """Validate a NIP-98 event for ``method`` on one of ``urls``; returns the pubkey (hex)."""
    now = int(time.time()) if now is None else now
    for key in ("id", "pubkey", "sig", "kind", "created_at", "tags", "content"):
        if key not in event:
            raise AuthError(401, f"event lacks {key}")
    if event["kind"] != NIP98_KIND:
        raise AuthError(401, "event is not kind 27235")
    if not isinstance(event["pubkey"], str) or len(event["pubkey"]) != 64:
        raise AuthError(401, "bad pubkey")
    if event_id(event) != event["id"] or not verify_signature(event):
        raise AuthError(401, "bad signature")
    if abs(now - int(event["created_at"])) > MAX_AGE:
        raise AuthError(401, "authorization too old or in the future")
    if [m.upper() for m in tag_values(event, "method")] != [method.upper()]:
        raise AuthError(401, "method tag does not match")
    if not any(u.rstrip("/") in urls for u in tag_values(event, "u")):
        raise AuthError(401, "u tag names another URL")
    return event["pubkey"]


def pass_tokens(svc: Services, pubkey: str) -> list[dict[str, Any]]:
    """The unspent token bundles of the key's active passes."""
    if svc.mint is None:
        return []
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", pubkey], ["cashu_token", "!=", False]],
        ["id", "cashu_token", "cashu_amount"],
    )
    out = []
    for row in rows:
        state = svc.mint.token_state(row["cashu_token"])
        if state == "spent":
            continue
        # "partial": someone redeemed part of it with another wallet; the wallet takes the rest
        # (the mint refuses the spent proofs, the receive swaps the unspent ones)
        out.append(
            {
                "pass_id": row["id"],
                "amount": row["cashu_amount"] or 0,
                "token": row["cashu_token"],
                "state": state,
            }
        )
    return out


def register(app: FastAPI) -> None:
    """Mount /wallet/pass-tokens (create_app calls this)."""

    def cors(request: Request) -> dict[str, str]:
        wallet = request.app.state.services.settings.wallet_public_url.rstrip("/")
        return {
            "Access-Control-Allow-Origin": wallet,
            "Access-Control-Allow-Methods": "GET",
            "Access-Control-Allow-Headers": "Authorization",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
            "Cache-Control": "no-store",
        }

    @app.options(PATH)
    async def pass_tokens_preflight(request: Request) -> Response:
        return Response(status_code=204, headers=cors(request))

    @app.get(PATH)
    async def wallet_pass_tokens(request: Request) -> JSONResponse:
        services: Services = request.app.state.services
        # the URL the wallet signed: the public one (pass.<domain>, or the start page's host,
        # which serves the same site), never the internal address the proxy forwards to
        urls = [
            u.rstrip("/") + PATH
            for u in (services.settings.public_url, services.settings.site_url)
            if u
        ]
        try:
            event = parse_authorization(request.headers.get("authorization"))
            pubkey = check_nip98(event, "GET", urls)
        except (AuthError, BlossomAuthError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status, headers=cors(request))
        tokens = await run_in_threadpool(pass_tokens, services, pubkey)
        if tokens:
            log.info(
                "wallet: pass tokens for %s (passes %s)", pubkey, [t["pass_id"] for t in tokens]
            )
        return JSONResponse(
            {"tokens": [{"amount": t["amount"], "token": t["token"]} for t in tokens]},
            headers=cors(request),
        )
