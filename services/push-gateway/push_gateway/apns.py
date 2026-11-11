"""Apple Push Notification service over HTTP/2, token-based (a .p8 key, ES256 JWT).

The provider token (``iss`` = team id, ``iat``, header ``kid`` = key id) is made once and used
for 50 minutes (Apple refuses one older than an hour and dislikes new ones more often than every
20 minutes); an ``ExpiredProviderToken`` answer makes a new one and tries once more.

The notification is an alert with ``mutable-content`` (the app's Notification Service Extension
decrypts ``b`` and replaces the placeholder text), ``thread-id`` and ``apns-collapse-id`` = the
collapse key. Dead tokens (410 Unregistered, 400 BadDeviceToken / DeviceTokenNotForTopic) are
``gone``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

import httpx
import jwt

log = logging.getLogger(__name__)

HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
TOKEN_SECONDS = 50 * 60
DEAD = {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}
REFRESH = {"ExpiredProviderToken", "InvalidProviderToken"}


class Apns:
    def __init__(
        self,
        key_pem: str,
        key_id: str,
        team_id: str,
        topic: str,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ):
        self.key_pem, self.key_id, self.team_id, self.topic = key_pem, key_id, team_id, topic
        self.http = http
        self.clock = clock
        self._token: tuple[float, str] = (0.0, "")

    @property
    def configured(self) -> bool:
        return bool(self.key_pem and self.key_id and self.team_id)

    def provider_token(self) -> str:
        made, token = self._token
        now = self.clock()
        if not token or now - made > TOKEN_SECONDS:
            token = jwt.encode(
                {"iss": self.team_id, "iat": int(now)},
                self.key_pem,
                algorithm="ES256",
                headers={"kid": self.key_id},
            )
            self._token = (now, token)
        return token

    async def send(self, token: str, env: str, payload: str, collapse: str, urgent: bool) -> str:
        """ok, gone, rate, invalid or error."""
        body = json.dumps(
            {
                "aps": {
                    "alert": {"title": "Belfry", "body": "New notification"},
                    "sound": "default",
                    "mutable-content": 1,
                    **({"thread-id": collapse} if collapse else {}),
                },
                "b": payload,
            },
            separators=(",", ":"),
        )
        for attempt in (1, 2):
            headers = {
                "authorization": f"bearer {self.provider_token()}",
                "apns-topic": self.topic,
                "apns-push-type": "alert",
                "apns-priority": "10" if urgent else "5",
                "apns-expiration": str(int(self.clock()) + 86400),
            }
            if collapse:
                headers["apns-collapse-id"] = collapse.encode()[:64].decode("utf-8", "ignore")
            try:
                r = await self.http.post(
                    f"{HOSTS[env]}/3/device/{token}", content=body, headers=headers
                )
            except httpx.HTTPError as exc:
                log.warning("apns: %s", exc)
                return "error"
            if r.status_code == 200:
                return "ok"
            reason = _reason(r)
            if r.status_code == 403 and reason in REFRESH and attempt == 1:
                self._token = (0.0, "")
                continue
            if r.status_code == 410 or (r.status_code == 400 and reason in DEAD):
                return "gone"
            if r.status_code == 429:
                return "rate"
            log.warning("apns: %s %s", r.status_code, reason)
            return "invalid" if r.status_code in (400, 413) else "error"
        return "error"


def _reason(r: httpx.Response) -> str:
    try:
        return str(r.json().get("reason") or "")
    except ValueError:
        return ""
