"""Firebase Cloud Messaging HTTP v1, with a service account.

The service account's key signs an RS256 JWT (scope firebase.messaging, ``aud`` = its token
URI), exchanged for an OAuth2 access token that is kept until five minutes before it expires; a
401 drops it and tries once more. The message is data-only (``data.b`` = the ciphertext): the
app's messaging service decrypts it and posts the notification itself. Dead tokens
(UNREGISTERED, an invalid registration token, a token of another sender) are ``gone``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
SEND = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
DEAD_CODES = {"UNREGISTERED", "SENDER_ID_MISMATCH"}


class Fcm:
    def __init__(
        self,
        account_json: str,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
    ):
        self.http = http
        self.clock = clock
        self.account: dict[str, Any] = {}
        if account_json:
            try:
                self.account = json.loads(account_json)
            except ValueError:
                log.error("fcm: the service account is not JSON; FCM is off")
        self._access: tuple[float, str] = (0.0, "")

    @property
    def configured(self) -> bool:
        a = self.account
        return bool(a.get("project_id") and a.get("client_email") and a.get("private_key"))

    async def access_token(self) -> str:
        expires, token = self._access
        now = self.clock()
        if token and now < expires - 300:
            return token
        uri = self.account.get("token_uri") or "https://oauth2.googleapis.com/token"
        assertion = jwt.encode(
            {
                "iss": self.account["client_email"],
                "scope": SCOPE,
                "aud": uri,
                "iat": int(now),
                "exp": int(now) + 3600,
            },
            self.account["private_key"],
            algorithm="RS256",
            headers={"kid": self.account.get("private_key_id", "")}
            if self.account.get("private_key_id")
            else None,
        )
        r = await self.http.post(
            uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        r.raise_for_status()
        data = r.json()
        token = data["access_token"]
        self._access = (now + int(data.get("expires_in", 3600)), token)
        return token

    async def send(self, token: str, payload: str, collapse: str, urgent: bool) -> str:
        """ok, gone, rate, invalid or error."""
        android: dict[str, Any] = {"priority": "high" if urgent else "normal", "ttl": "86400s"}
        if collapse:
            android["collapse_key"] = collapse
        message = {"message": {"token": token, "data": {"b": payload}, "android": android}}
        url = SEND.format(project=self.account["project_id"])
        for attempt in (1, 2):
            try:
                access = await self.access_token()
                r = await self.http.post(
                    url, json=message, headers={"Authorization": f"Bearer {access}"}
                )
            except httpx.HTTPError as exc:
                log.warning("fcm: %s", exc)
                return "error"
            if r.status_code == 200:
                return "ok"
            status, code, text = _error(r)
            if r.status_code == 401 and attempt == 1:
                self._access = (0.0, "")
                continue
            if (
                code in DEAD_CODES
                or r.status_code == 404
                or (status == "INVALID_ARGUMENT" and "registration token" in text.lower())
            ):
                return "gone"
            if r.status_code == 429:
                return "rate"
            log.warning("fcm: %s %s %s", r.status_code, status, code)
            return "invalid" if r.status_code == 400 else "error"
        return "error"


def _error(r: httpx.Response) -> tuple[str, str, str]:
    """(status, FCM error code, message) of an error answer."""
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return "", "", ""
    code = ""
    for d in err.get("details") or []:
        if isinstance(d, dict) and d.get("errorCode"):
            code = str(d["errorCode"])
    return str(err.get("status") or ""), code, str(err.get("message") or "")
