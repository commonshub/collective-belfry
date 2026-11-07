"""The belfry's mail server for incoming mail (the ``mail-in`` service, port 25 on the host).

A thin SMTP front: every RCPT TO and every message goes to the onboarding service
(``/internal/mail/rcpt`` and ``/internal/mail/data``, mail.py) with the hook token, and its
answer is the SMTP answer. Nothing is queued here: a sending server whose mail could not be
delivered keeps it and tries again (4xx), for days.

STARTTLS uses the certificate Caddy holds for the domain (the proxy's data volume, read-only;
this service runs as Caddy's user), reloaded every six hours; without one, plain SMTP.

    python -m belfry_onboarding.mailfront
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import ssl

import httpx
from aiosmtpd.smtp import SMTP, Envelope, Session

log = logging.getLogger("belfry.mailfront")

PORT = int(os.environ.get("MAIL_PORT", "2525"))
DOMAIN = os.environ.get("NIP05_DOMAIN", "localhost")
BACKEND = os.environ.get("MAIL_BACKEND", "http://onboarding:8100").rstrip("/")
CERTS = os.environ.get("MAIL_CERTS", "/caddy/caddy/certificates")
MAX_BYTES = int(os.environ.get("MAIL_MAX_BYTES", "15000000"))
MAX_RCPT = 20


def token() -> str:
    path = os.environ.get("HOOK_TOKEN_FILE", "/run/secrets/hook_token")
    with open(path) as f:
        return f.read().strip()


def cert_files() -> tuple[str, str] | None:
    for crt in sorted(glob.glob(f"{CERTS}/*/{DOMAIN}/{DOMAIN}.crt")):
        key = crt[:-4] + ".key"
        if os.path.exists(key):
            return crt, key
    return None


def tls_context() -> ssl.SSLContext | None:
    found = cert_files()
    if not found:
        log.info("no certificate for %s under %s: plain SMTP", DOMAIN, CERTS)
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(*found)
    return ctx


def reply(data: dict) -> str:
    code = int(data.get("code") or 451)
    message = " ".join(str(data.get("message") or "").split())[:400] or "OK"
    return f"{code} {message}"


class Handler:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {token()}"}

    async def handle_RCPT(self, server, session: Session, envelope: Envelope, address, options):
        if len(envelope.rcpt_tos) >= MAX_RCPT:
            return "452 4.5.3 Too many recipients"
        try:
            r = await self.http.post(
                f"{BACKEND}/internal/mail/rcpt", json={"address": address}, headers=self._headers()
            )
            r.raise_for_status()
            answer = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("rcpt %s: backend: %s", address, exc)
            return "451 4.3.0 Try again later"
        if int(answer.get("code", 451)) == 250:
            envelope.rcpt_tos.append(address)
            return "250 OK"
        return reply(answer)

    async def handle_DATA(self, server, session: Session, envelope: Envelope):
        raw = envelope.original_content or envelope.content or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        headers = {
            **self._headers(),
            "Content-Type": "message/rfc822",
            "X-Peer": str((session.peer or ("",))[0]),
            "X-Helo": str(session.host_name or "")[:255],
            "X-Mail-From": str(envelope.mail_from or "")[:320],
            "X-Rcpt": ",".join(envelope.rcpt_tos),
        }
        try:
            r = await self.http.post(f"{BACKEND}/internal/mail/data", content=raw, headers=headers)
            r.raise_for_status()
            return reply(r.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("data: backend: %s", exc)
            return "451 4.3.0 Try again later"


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s"
    )
    ctx = tls_context()
    async with httpx.AsyncClient(timeout=120.0) as http:
        handler = Handler(http)

        def factory() -> SMTP:
            return SMTP(
                handler,
                hostname=DOMAIN,
                ident="Belfry",
                data_size_limit=MAX_BYTES,
                enable_SMTPUTF8=True,
                decode_data=False,
                tls_context=ctx,
                require_starttls=False,
            )

        loop = asyncio.get_running_loop()
        server = await loop.create_server(factory, host="0.0.0.0", port=PORT)
        log.info("listening on %d for @%s (STARTTLS %s)", PORT, DOMAIN, "on" if ctx else "off")
        async with server:
            while True:
                await asyncio.sleep(6 * 3600)
                found = cert_files()
                if ctx and found:
                    with contextlib.suppress(OSError, ssl.SSLError):
                        ctx.load_cert_chain(*found)  # Caddy renews about every 60 days


if __name__ == "__main__":
    asyncio.run(main())
