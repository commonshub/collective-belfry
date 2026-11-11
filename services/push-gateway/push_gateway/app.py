"""The push gateway: ``POST /v1/send`` (docs/push.md §2).

Every belfry encrypts its notifications to a key the phone made and sends them here, signed
with the belfry's own key (NIP-98; any key may: tokens are only known to the belfry the phone
registered with). The gateway holds the publisher's APNs key and FCM service account and passes
the ciphertext on; it never sees what a notification says.

    {"platform": "apns" | "fcm", "token", "env": "production" | "sandbox",
     "payload": "<base64, ≤ 3000 bytes>", "collapse": "<≤ 64 chars>", "urgent": true}

Answers: 200 ``{"ok": true}``; 410 ``gone`` (APNs or FCM say the token is dead: the belfry
deletes the device); 429 ``rate`` (120 an hour per token, 10 000 an hour per belfry key); 400
``invalid``; 401 ``unauthorized``; 503 ``not_configured`` (that platform's credentials are
missing); 502 ``upstream`` (APNs or FCM failed). Logs carry no payloads and tokens only as a
hash prefix.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import nip98
from .apns import Apns
from .fcm import Fcm
from .settings import Settings

log = logging.getLogger("push_gateway")

MAX_BODY = 8192
MAX_PAYLOAD = 3000
TOKEN = re.compile(r"^[A-Za-z0-9_:.\-]{8,4096}$")
HOUR = 3600.0


def token_ref(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


class Limits:
    """Sliding one-hour windows per token and per belfry key, in memory."""

    def __init__(self, per_token: int, per_belfry: int, clock: Callable[[], float] = time.time):
        self.per_token, self.per_belfry, self.clock = per_token, per_belfry, clock
        self._tokens: dict[str, deque[float]] = {}
        self._belfries: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._swept = 0.0

    @staticmethod
    def _window(d: deque[float], now: float) -> deque[float]:
        while d and d[0] <= now - HOUR:
            d.popleft()
        return d

    def take(self, belfry: str, token: str) -> bool:
        now = self.clock()
        with self._lock:
            if now - self._swept > HOUR:  # forget the quiet ones
                for table in (self._tokens, self._belfries):
                    for k in [k for k, d in table.items() if not self._window(d, now)]:
                        del table[k]
                self._swept = now
            t = self._window(self._tokens.setdefault(token_ref(token), deque()), now)
            b = self._window(self._belfries.setdefault(belfry, deque()), now)
            if len(t) >= self.per_token or len(b) >= self.per_belfry:
                return False
            t.append(now)
            b.append(now)
            return True


def _err(status: int, error: str, message: str = "") -> JSONResponse:
    return JSONResponse({"error": error, **({"message": message} if message else {})}, status)


def _valid(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    platform, env, token = data.get("platform"), data.get("env", "production"), data.get("token")
    payload, collapse, urgent = data.get("payload"), data.get("collapse", ""), data.get("urgent")
    if platform not in ("apns", "fcm") or env not in ("production", "sandbox"):
        return None
    if not isinstance(token, str) or not TOKEN.match(token):
        return None
    if not isinstance(payload, str) or not payload or len(payload) > MAX_PAYLOAD:
        return None
    try:
        base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not isinstance(collapse, str) or len(collapse) > 64:
        return None
    if urgent is None:
        urgent = True
    if not isinstance(urgent, bool):
        return None
    return {
        "platform": platform,
        "env": env,
        "token": token,
        "payload": payload,
        "collapse": collapse,
        "urgent": urgent,
    }


def create_app(
    settings: Settings | None = None,
    http: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    settings = settings or Settings()
    # httpx logs every request's URL at INFO, and an APNs URL ends with the device token
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        own = http is None
        client = http or httpx.AsyncClient(http2=True, timeout=httpx.Timeout(20.0, connect=5.0))
        app.state.apns = Apns(
            settings.apns_pem(),
            settings.apns_key_id,
            settings.apns_team_id,
            settings.apns_topic,
            client,
            clock,
        )
        app.state.fcm = Fcm(settings.fcm_json(), client, clock)
        log.info(
            "push gateway: apns %s, fcm %s",
            "on" if app.state.apns.configured else "not configured",
            "on" if app.state.fcm.configured else "not configured",
        )
        try:
            yield
        finally:
            if own:
                await client.aclose()

    app = FastAPI(title="Belfry push gateway", version="0.1.0", lifespan=lifespan)
    replays = nip98.Replays()
    limits = Limits(settings.per_token_hour, settings.per_belfry_hour, clock)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "apns": app.state.apns.configured,
            "fcm": app.state.fcm.configured,
        }

    @app.post("/v1/send")
    async def send(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > MAX_BODY:
            return _err(400, "invalid", "body too large")
        try:
            belfry = nip98.check(
                request.headers.get("authorization"), settings.send_url, body, replays, clock()
            )
        except nip98.AuthError as exc:
            return _err(401, "unauthorized", str(exc))
        try:
            data = _valid(json.loads(body or b"{}"))
        except ValueError:
            data = None
        if data is None:
            return _err(400, "invalid")
        service = app.state.apns if data["platform"] == "apns" else app.state.fcm
        if not service.configured:
            return _err(503, "not_configured")
        if not limits.take(belfry, data["token"]):
            return _err(429, "rate")
        if data["platform"] == "apns":
            result = await service.send(
                data["token"], data["env"], data["payload"], data["collapse"], data["urgent"]
            )
        else:
            result = await service.send(
                data["token"], data["payload"], data["collapse"], data["urgent"]
            )
        log.info(
            "send %s %s from %s… %d bytes: %s",
            data["platform"],
            token_ref(data["token"]),
            belfry[:12],
            len(data["payload"]),
            result,
        )
        if result == "ok":
            return JSONResponse({"ok": True})
        return {
            "gone": _err(410, "gone"),
            "rate": _err(429, "rate"),
            "invalid": _err(400, "invalid"),
        }.get(result, _err(502, "upstream"))

    return app


def main() -> None:  # pragma: no cover — the container's entry point
    import uvicorn

    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8200, proxy_headers=True)


if __name__ == "__main__":  # pragma: no cover
    main()
