"""E-mail as messages: ``<username>@`` and ``<channel>@<domain>`` receive mail, members answer.

The protocol is Nostr Mail (nogringo/nostr-mail, the convention Nostr clients use for e-mail; there
is no numbered NIP yet): an e-mail is a kind 1301 rumor whose content is the RFC 2822 message,
gift-wrapped with NIP-59 like a NIP-17 DM. The belfry is the bridge, ``_smtp@<domain>`` in its
nostr.json:

- **In.** The mail-in service (mailfront.py) takes mail on port 25 and hands every address and
  message to this service (``/internal/mail/*``, the hook token).
  - ``<username>@<domain>``: a member with an active pass. The message becomes a kind 1301 rumor
    from the belfry key with ``mail-from`` (the legacy sender), gift-wrapped to the member and
    published on the community relay, their DM relay (10050). A message too big for a gift wrap
    goes to Blossom encrypted with AES-GCM (Nostr Mail's large MIME), the key in the rumor.
  - ``<channel>@<domain>``: a channel, pod or event chat of the groups relay. Only a member may
    post, from their own address, and the mail must prove it (DKIM or SPF aligned with From): it
    is posted as the member, attachments on Blossom.
  - ``postmaster@`` and ``abuse@`` go to the admins, like a member's mail.
  The usernames come first: a channel named like a member gets no mail.
- **Out.** A member's app sends a kind 1301 rumor with ``mail-from`` (their ``username@domain``)
  and ``rcpt-to``, gift-wrapped to the belfry key on the community relay. The belfry checks the
  sender owns that address, sends the message unchanged but for Bcc through the SMTP server the
  belfry already sends with, and answers with a kind 7679 delivery report (Nostr Mail DSN).
  Reports are also how a restart knows what was sent already.

Every check that marks mail "verified" (``belfry-auth`` tag, true only on rumors from the belfry
key) is DKIM or SPF aligned with the From domain (relaxed: same domain or a subdomain).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import email
import email.policy
import email.utils
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import smtplib
import ssl
import threading
import time
import uuid
from email.message import EmailMessage
from typing import Any

from coincurve import PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import apps, community, drops, followers, homepage
from .blossom import event_id, verify_signature
from .core import Services
from .nip46check import conversation_key, nip44_decrypt, nip44_encrypt, sign_event
from .relay import AUTH_KIND

log = logging.getLogger(__name__)

MAIL, SEAL, WRAP, DSN = 1301, 13, 1059, 7679
INLINE = 60_000  # bytes of MIME that still fit in a gift wrap (NIP-44 caps at 65535, twice)
MAX_RCPT = 20
RESERVED = {"postmaster", "abuse"}
LOCAL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SENT_PER_HOUR = 60  # mails a member sends
IN_PER_10_MIN = 20  # mails from one sender address
WINDOW_DAYS = 2  # gift wraps are back-dated up to two days (NIP-59)

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_counts: dict[str, list[float]] = {}
DNSFUNC = None  # tests: a DKIM key lookup instead of DNS


class MailError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def enabled(svc: Services) -> bool:
    return bool(svc.settings.mail_in and homepage.belfry_key(svc.settings))


def domain(svc: Services) -> str:
    return (svc.settings.nip05_domain or "").lower()


def _limited(key: str, limit: int, seconds: float) -> bool:
    now = time.monotonic()
    with _lock:
        hits = [t for t in _counts.get(key, []) if t > now - seconds]
        if len(hits) >= limit:
            _counts[key] = hits
            return True
        _counts[key] = [*hits, now]
        return False


# ── who an address is ───────────────────────────────────────────────────────────────────────
def active_pubkey(svc: Services, pubkey: str) -> bool:
    rows = svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]], ["id"], limit=1
    )
    return bool(rows)


def channel_exists(svc: Services, channel: str) -> bool:
    got = followers._relay(svc)
    if got is None:
        return False
    relay, sign = got
    return bool(relay.query([{"kinds": [39000], "#d": [channel], "limit": 1}], sign))


def resolve(svc: Services, address: str) -> dict[str, Any] | None:
    """What ADDRESS is here: a member, a channel or the admins (cached a minute), else None."""
    local, _, dom = address.strip().lower().rpartition("@")
    local = local.split("+", 1)[0]
    if dom != domain(svc) or not LOCAL.match(local):
        return None
    with _lock:
        held = _cache.get(local)
    if held and time.monotonic() - held[0] < 60:
        return held[1]
    found: dict[str, Any] | None = None
    if local in RESERVED:
        keys = sorted(apps.admin_pubkeys(svc))
        found = {"kind": "admins", "pubkeys": keys} if keys else None
    else:
        pubkey = svc.keycast.pubkey_for_name(local)
        if pubkey and active_pubkey(svc, pubkey):
            found = {"kind": "member", "pubkeys": [pubkey], "name": local}
        elif channel_exists(svc, local):
            found = {"kind": "channel", "channel": local}
    with _lock:
        _cache[local] = (time.monotonic(), found)
    return found


# ── is the sender who they say ──────────────────────────────────────────────────────────────
def related(a: str, b: str) -> bool:
    """Relaxed DMARC alignment without the public suffix list: the same domain or a subdomain."""
    a, b = a.lower().strip("."), b.lower().strip(".")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def authenticate(raw: bytes, peer: str, helo: str, mail_from: str, from_addr: str) -> dict:
    """DKIM and SPF results and whether either aligns with the From domain."""
    from_domain = from_addr.rpartition("@")[2]
    dkim_ok = ""
    try:
        import dkim

        sigs = sum(1 for k in email.message_from_bytes(raw).keys() if k.lower() == "dkim-signature")
        for i in range(min(sigs, 5)):
            d = dkim.DKIM(raw)
            ok = d.verify(idx=i, dnsfunc=DNSFUNC) if DNSFUNC else d.verify(idx=i)
            if ok and d.domain:
                dom = d.domain.decode() if isinstance(d.domain, bytes) else str(d.domain)
                if related(dom, from_domain):
                    dkim_ok = dom
                    break
    except Exception as exc:  # noqa: BLE001 — a broken signature is just no signature
        log.debug("mail: dkim: %s", exc)
    spf_result = "none"
    try:
        ip = ipaddress.ip_address(peer)
        if not (ip.is_private or ip.is_loopback):
            import spf

            spf_result, _ = spf.check2(i=peer, s=mail_from or f"postmaster@{helo}", h=helo)
    except Exception as exc:  # noqa: BLE001
        log.debug("mail: spf: %s", exc)
    envelope_domain = (mail_from or "").rpartition("@")[2] or helo
    spf_aligned = spf_result == "pass" and related(envelope_domain, from_domain)
    return {"dkim": dkim_ok, "spf": spf_result, "aligned": bool(dkim_ok) or spf_aligned}


# ── reading a message ───────────────────────────────────────────────────────────────────────
def parse(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[return-value]


def address_of(header: Any) -> str:
    return email.utils.parseaddr(str(header or ""))[1].lower()


def text_of(msg: EmailMessage) -> str:
    """The plain text of a message, without the quoted message a reply carries."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&nbsp;", " ", text)
        import html

        text = html.unescape(text)
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s*(On .{4,200} wrote:|-----\s*Original Message\s*-----)\s*$", line):
            break
        if line.startswith(">"):
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def attachments(msg: EmailMessage) -> list[tuple[str, str, bytes]]:
    out = []
    for part in msg.iter_attachments():
        body = part.get_payload(decode=True) or b""
        if body:
            out.append((part.get_filename() or "", part.get_content_type(), body))
    return out[:10]


# ── gift wraps ──────────────────────────────────────────────────────────────────────────────
def gift_wrap(secret: bytes, rumor: dict[str, Any], to: str) -> dict[str, Any]:
    """RUMOR (unsigned, its pubkey SECRET's) sealed by SECRET and gift-wrapped to TO (NIP-59)."""
    now = int(time.time())
    seal = sign_event(
        secret,
        {
            "kind": SEAL,
            "created_at": now - secrets.randbelow(2 * 86400),
            "tags": [],
            "content": nip44_encrypt(
                conversation_key(secret, bytes.fromhex(to)), json.dumps(rumor)
            ),
        },
    )
    one_time = PrivateKey()
    return sign_event(
        one_time.secret,
        {
            "kind": WRAP,
            "created_at": now - secrets.randbelow(2 * 86400),
            "tags": [["p", to]],
            "content": nip44_encrypt(
                conversation_key(one_time.secret, bytes.fromhex(to)), json.dumps(seal)
            ),
        },
    )


def unwrap(secret: bytes, wrap: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(sender, rumor) of a gift wrap to SECRET's key; the seal's signer is the rumor's author."""
    if wrap.get("kind") != WRAP or not verify_signature(wrap):
        raise ValueError("not a valid gift wrap")
    seal = json.loads(
        nip44_decrypt(conversation_key(secret, bytes.fromhex(wrap["pubkey"])), wrap["content"])
    )
    if seal.get("kind") != SEAL or not verify_signature(seal):
        raise ValueError("the seal is not signed")
    rumor = json.loads(
        nip44_decrypt(conversation_key(secret, bytes.fromhex(seal["pubkey"])), seal["content"])
    )
    if rumor.get("pubkey") != seal["pubkey"]:
        raise ValueError("the rumor's author is not the seal's")
    return seal["pubkey"], rumor


def tag(event: dict[str, Any], name: str) -> str:
    return next((t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == name), "")


def tags(event: dict[str, Any], name: str) -> list[str]:
    return [t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == name]


def _belfry(svc: Services) -> PrivateKey:
    key = homepage.belfry_key(svc.settings)
    if key is None:
        raise MailError(451, "4.3.0 This belfry has no key yet.")
    return key


def _sign_as_belfry(svc: Services):
    key = _belfry(svc)
    return lambda event: sign_event(key.secret, event)


def _publish(svc: Services, event: dict[str, Any]) -> None:
    if svc.relay is None:
        raise MailError(451, "4.3.0 The community relay is not configured.")
    svc.relay.publish(event, _sign_as_belfry(svc))


# ── in: a member's mail ─────────────────────────────────────────────────────────────────────
def store_large(svc: Services, raw: bytes) -> list[list[str]]:
    """Nostr Mail large MIME: AES-GCM on Blossom; the tags that find and open it."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, nonce = AESGCM.generate_key(bit_length=256), secrets.token_bytes(12)
    blob = AESGCM(key).encrypt(nonce, raw, None)
    [imeta] = drops.upload(svc, [("", "application/octet-stream", blob)])
    url = next(v[4:] for v in imeta[1:] if v.startswith("url "))
    return [
        ["encryption-algorithm", "aes-gcm"],
        ["decryption-key", base64.b64encode(key).decode()],
        ["decryption-nonce", base64.b64encode(nonce).decode()],
        ["x", hashlib.sha256(blob).hexdigest()],
        ["url", url],
    ]


def mail_rumor(
    svc: Services, raw: bytes, msg: EmailMessage, sender: str, auth: dict, to: str
) -> dict[str, Any]:
    key = _belfry(svc)
    when = email.utils.parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
    rumor_tags = [
        ["email-id", uuid.uuid4().hex],
        ["mail-from", sender],
        ["p", to],
        ["subject", str(msg["Subject"] or "")[:300]],
        ["from", str(msg["From"] or sender)[:300]],
        ["to", str(msg["To"] or "")[:300]],
        ["belfry-auth", "pass" if auth.get("aligned") else "none"],
    ]
    with contextlib.suppress(TypeError, ValueError, OverflowError):
        if when:
            rumor_tags.append(["date", str(int(when.timestamp()))])
    content = ""
    if len(raw) <= INLINE:
        content = raw.decode("utf-8", "replace")
    else:
        rumor_tags += store_large(svc, raw)
    rumor = {
        "kind": MAIL,
        "pubkey": key.public_key_xonly.format().hex(),
        "created_at": int(time.time()),
        "tags": rumor_tags,
        "content": content,
    }
    rumor["id"] = event_id(rumor)
    return rumor


def to_member(svc: Services, pubkey: str, raw: bytes, msg: EmailMessage, sender: str, auth: dict):
    rumor = mail_rumor(svc, raw, msg, sender, auth, pubkey)
    _publish(svc, gift_wrap(_belfry(svc).secret, rumor, pubkey))


# ── in: a channel's mail ────────────────────────────────────────────────────────────────────
def to_channel(svc: Services, channel: str, msg: EmailMessage, sender: str, auth: dict) -> None:
    member = apps.member_by_email(svc, sender)
    if member is None:
        raise MailError(550, f"5.7.1 Only members of {domain(svc)} can post to channels by e-mail.")
    if not auth.get("aligned"):
        raise MailError(
            550, "5.7.1 Your provider did not prove this mail is yours (no DKIM or SPF pass)."
        )
    subject = str(msg["Subject"] or "").strip()
    body = text_of(msg)[:8000]
    text = "\n\n".join(x for x in (f"**{subject}**" if subject else "", body) if x)
    files = [f for f in attachments(msg) if len(f[2]) <= 20_000_000]
    imeta = drops.upload(svc, files) if files else []
    if not text and not imeta:
        raise MailError(550, "5.6.0 This mail is empty.")
    if community.member_signer(svc, member["partner"]) is None:
        text = f"✉️ From {member['name']}\n\n{text}"  # their own key: posted by the belfry
    try:
        drops.post(svc, channel, text, imeta, member["partner"], client="belfry-mail")
    except Exception as exc:  # noqa: BLE001 — the relay refuses non-members of closed channels
        log.info("mail: post to %s refused: %s", channel, exc)
        raise MailError(550, f"5.7.1 You cannot post in {channel}.") from exc


# ── in: one message, every recipient ────────────────────────────────────────────────────────
def deliver(
    svc: Services, raw: bytes, peer: str, helo: str, mail_from: str, rcpts: list[str]
) -> tuple[int, str]:
    msg = parse(raw)
    sender = address_of(msg["From"]) or (mail_from or "").lower()
    if not sender:
        return 550, "5.1.7 The mail has no sender."
    if _limited("in:" + sender, IN_PER_10_MIN, 600):
        return 451, "4.7.1 Too many messages from you; try again later."
    auth = authenticate(raw, peer, helo, mail_from, sender)
    done, errors = 0, []
    for rcpt in rcpts[:MAX_RCPT]:
        target = resolve(svc, rcpt)
        try:
            if target is None:
                raise MailError(550, f"5.1.1 No one is called {rcpt} here.")
            if target["kind"] == "channel":
                to_channel(svc, target["channel"], msg, sender, auth)
            else:
                for pubkey in target["pubkeys"]:
                    to_member(svc, pubkey, raw, msg, sender, auth)
            done += 1
            log.info("mail: in for %s (%s, auth %s)", target["kind"], rcpt, auth)
        except MailError as exc:
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001 — the sender's server tries again later
            log.warning("mail: delivery to %s failed: %r", rcpt, exc, exc_info=True)
            errors.append(MailError(451, "4.3.0 Delivery failed here; try again later."))
    if done:
        return 250, "2.0.0 Delivered"
    worst = max(errors, key=lambda e: e.code) if errors else MailError(550, "5.1.1 No recipient")
    if any(e.code < 500 for e in errors):
        worst = next(e for e in errors if e.code < 500)
    return worst.code, worst.message


# ── out: a member's mail through the belfry's SMTP server ───────────────────────────────────
def smtp_send(svc: Services, mail_from: str, rcpts: list[str], raw: bytes) -> tuple[int, str]:
    s = svc.settings
    if not s.smtp_host:
        raise MailError(554, "This belfry has no mail provider yet.")
    enc = (s.smtp_encryption or "").lower()
    cls = smtplib.SMTP_SSL if enc == "ssl" else smtplib.SMTP
    with cls(s.smtp_host, s.smtp_port, timeout=30) as conn:
        if enc == "starttls":
            conn.starttls(context=ssl.create_default_context())
        if s.smtp_user:
            conn.login(s.smtp_user, s.smtp_password)
        refused = conn.sendmail(mail_from, rcpts, raw)
    if refused:
        code, why = next(iter(refused.values()))
        return code, why.decode("utf-8", "replace") if isinstance(why, bytes) else str(why)
    return 250, "OK"


def outgoing(svc: Services, sender: str, rumor: dict[str, Any]) -> tuple[list[str], int, str]:
    """Send a member's kind 1301; (recipients, SMTP code, answer). MailError: refused here."""
    mail_from = tag(rumor, "mail-from").lower()
    rcpts = [r.strip() for r in tags(rumor, "rcpt-to") if "@" in r][:MAX_RCPT]
    local, _, dom = mail_from.rpartition("@")
    if not rcpts:
        raise MailError(554, "No recipient (rcpt-to).")
    if dom != domain(svc) or not LOCAL.match(local) or local in RESERVED:
        raise MailError(554, f"You can only send as your own address at {domain(svc)}.")
    if svc.keycast.pubkey_for_name(local) != sender or not active_pubkey(svc, sender):
        raise MailError(554, f"{mail_from} is not yours, or your membership is not active.")
    if _limited("out:" + sender, SENT_PER_HOUR, 3600):
        raise MailError(451, "Too many mails this hour; try again later.")
    raw = rumor.get("content") or ""
    if not raw and tag(rumor, "x"):
        raise MailError(554, "Large mails cannot be sent yet.")
    msg = parse(raw.encode("utf-8"))
    if address_of(msg["From"]) != mail_from:
        raise MailError(554, "The From header must be your own address.")
    del msg["Bcc"]
    if not msg["Date"]:
        msg["Date"] = email.utils.formatdate(localtime=False)
    if not msg["Message-ID"]:
        msg["Message-ID"] = email.utils.make_msgid(domain=domain(svc))
    data = msg.as_bytes(policy=email.policy.SMTP)
    if len(data) > svc.settings.mail_max_bytes:
        raise MailError(552, "This mail is too big.")
    try:
        code, answer = smtp_send(svc, mail_from, rcpts, data)
    except smtplib.SMTPResponseException as exc:
        code, answer = exc.smtp_code, str(exc.smtp_error)
    except smtplib.SMTPRecipientsRefused as exc:
        code, why = next(iter(exc.recipients.values()))
        answer = str(why)
    except (smtplib.SMTPException, OSError) as exc:
        code, answer = 451, str(exc)
    return rcpts, code, answer


def report(svc: Services, sender: str, email_id: str, rcpts: list[str], code: int, answer: str):
    """A kind 7679 delivery report to SENDER (Nostr Mail DSN): an ephemeral key, no p tag."""
    status = "delivered" if 200 <= code < 300 else "failed" if code >= 500 else "delayed"
    now = int(time.time())
    body = {
        "status": status,
        "recipient": ", ".join(rcpts),
        "smtp_code": code,
        "smtp_response": answer[:500],
        "attempted_at": now,
        ("delivered_at" if status == "delivered" else "failed_at"): now,
        "details": answer[:500],
    }
    temp = PrivateKey()
    content = nip44_encrypt(conversation_key(temp.secret, bytes.fromhex(sender)), json.dumps(body))
    pub = temp.public_key_xonly.format().hex()
    del temp
    event = _sign_as_belfry(svc)(
        {
            "kind": DSN,
            "created_at": now,
            "tags": [["r", email_id], ["ephemeral-pubkey", pub]],
            "content": content,
        }
    )
    _publish(svc, event)


def reported(svc: Services, email_id: str) -> bool:
    if svc.relay is None:
        return False
    me = _belfry(svc).public_key_xonly.format().hex()
    found = svc.relay.query(
        [{"kinds": [DSN], "authors": [me], "#r": [email_id], "limit": 1}], _sign_as_belfry(svc)
    )
    return bool(found)


def handle_wrap(svc: Services, wrap: dict[str, Any]) -> None:
    sender, rumor = unwrap(_belfry(svc).secret, wrap)
    if rumor.get("kind") != MAIL:
        return
    if rumor.get("created_at", 0) < time.time() - (WINDOW_DAYS + 1) * 86400:
        return
    email_id = tag(rumor, "email-id") or rumor.get("id") or wrap["id"]
    if reported(svc, email_id):
        return  # sent before a restart
    try:
        rcpts, code, answer = outgoing(svc, sender, rumor)
    except MailError as exc:
        rcpts, code, answer = tags(rumor, "rcpt-to")[:MAX_RCPT], exc.code, exc.message
    log.info("mail: out from %s… to %d recipients: %s", sender[:8], len(rcpts), code)
    report(svc, sender, email_id, rcpts, code, answer)


async def listen(app: FastAPI) -> None:
    """Gift wraps for the belfry key on the community relay (members' outgoing mail)."""
    from websockets.asyncio.client import connect

    svc: Services = app.state.services
    url = svc.settings.relay_url
    key = homepage.belfry_key(svc.settings)
    if not url or key is None or not svc.settings.mail_in:
        return
    me = key.public_key_xonly.format().hex()
    seen: dict[str, float] = {}
    delay = 2.0
    while True:
        try:
            async with connect(url, open_timeout=10) as ws:
                req = json.dumps(
                    [
                        "REQ",
                        "mail",
                        {"kinds": [WRAP], "#p": [me], "since": int(time.time()) - 3 * 86400},
                    ]
                )
                await ws.send(req)
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg[0] == "AUTH":
                        auth = sign_event(
                            key.secret,
                            {
                                "kind": AUTH_KIND,
                                "created_at": int(time.time()),
                                "tags": [["relay", url], ["challenge", msg[1]]],
                                "content": "",
                            },
                        )
                        await ws.send(json.dumps(["AUTH", auth]))
                    elif msg[0] == "OK" and msg[2]:
                        await ws.send(req)  # authenticated: ask again
                    elif msg[0] == "EOSE":
                        delay = 2.0
                    elif msg[0] == "EVENT" and len(msg) > 2 and msg[2].get("id") not in seen:
                        now = time.time()
                        seen[msg[2]["id"]] = now
                        for k in [k for k, t in seen.items() if t < now - 4 * 86400]:
                            del seen[k]
                        try:
                            await run_in_threadpool(handle_wrap, svc, msg[2])
                        except (ValueError, KeyError, TypeError) as exc:
                            log.debug("mail: skipped a wrap: %s", exc)
                        except Exception as exc:  # noqa: BLE001 — the next wrap still counts
                            log.warning("mail: a wrap failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep listening whatever the relay does
            log.info("mail: relay %s: %s; again in %.0fs", url, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120.0)


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    task = asyncio.create_task(listen(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ── the routes ──────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    def allowed(request: Request) -> bool:
        expected = svc(request).settings.hook_token
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return bool(expected and got) and hmac.compare_digest(got, expected)

    def answer(code: int, message: str) -> JSONResponse:
        return JSONResponse({"code": code, "message": message})

    @app.post("/internal/mail/rcpt")
    async def mail_rcpt(request: Request):
        if not allowed(request):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        if not enabled(svc(request)):
            return answer(550, "5.1.1 This belfry takes no mail.")
        body = await request.json()
        address = str((body or {}).get("address", ""))[:320]
        found = await run_in_threadpool(resolve, svc(request), address)
        return (
            answer(250, "OK") if found else answer(550, f"5.1.1 No one is called {address} here.")
        )

    @app.post("/internal/mail/data")
    async def mail_data(request: Request):
        if not allowed(request):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        services = svc(request)
        if not enabled(services):
            return answer(550, "5.1.1 This belfry takes no mail.")
        raw = await request.body()
        if len(raw) > services.settings.mail_max_bytes:
            return answer(552, "5.3.4 This mail is too big.")
        h = request.headers
        rcpts = [r for r in h.get("x-rcpt", "").split(",") if r][:MAX_RCPT]
        code, message = await run_in_threadpool(
            deliver,
            services,
            raw,
            h.get("x-peer", ""),
            h.get("x-helo", ""),
            h.get("x-mail-from", ""),
            rcpts,
        )
        return answer(code, message)

    @app.get("/.well-known/nostr.json")
    async def smtp_name(request: Request, name: str = ""):
        """Nostr Mail: the bridge's key is ``_smtp@<domain>`` (the proxy sends only that here)."""
        services = svc(request)
        pub = homepage.belfry_pubkey(services.settings)
        if name != "_smtp" or not pub or not services.settings.mail_in:
            return JSONResponse({"names": {}}, headers={"Access-Control-Allow-Origin": "*"})
        relay = services.settings.community_relay_public_url
        return JSONResponse(
            {"names": {"_smtp": pub}, "relays": {pub: [relay] if relay else []}},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"},
        )
