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
  - ``<channel>@<domain>``: a channel, group or event chat of the groups relay. Only a member may
    post, from their own address, and the mail must prove it (DKIM or SPF aligned with From): it
    is posted as the member, attachments on Blossom.
  - ``postmaster@`` and ``abuse@`` go to the admins, like a member's mail; ``bounces@`` (the
    envelope sender of forwarded copies) is taken and dropped.
  - A channel with a confirmed forward address (mailforward.py) sends it a copy of every mail
    it took, after posting.
  The usernames come first: a channel named like a member gets no mail.
- **Out.** A member's app sends a kind 1301 rumor with ``mail-from`` (their ``username@domain``)
  and ``rcpt-to``, gift-wrapped to the belfry key on the community relay. The belfry checks the
  sender owns that address, sends the message unchanged but for Bcc through the SMTP server the
  belfry already sends with, and answers with a kind 7679 delivery report (Nostr Mail DSN).
  Reports are also how a restart knows what was sent already.

Sealed conversations (docs/sealed.md §7, sealed.py): a member whose devices hold a letterbox
key gets their mail sealed to it (the signer cannot open it), may seal what they send with their
device's key instead of the account's, and gets the delivery report under the letterbox.

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

from . import apps, community, drops, followers, homepage, sealed, stewards
from .blossom import event_id, verify_signature
from .core import Services
from .nip46check import conversation_key, nip44_decrypt, nip44_encrypt, sign_event
from .relay import AUTH_KIND

log = logging.getLogger(__name__)

MAIL, SEAL, WRAP, DSN = 1301, 13, 1059, 7679
INLINE = 60_000  # bytes of MIME never tried inline; under it the rumor decides (sealed.fits)
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


class Later(Exception):
    """Something this needs did not answer (the relay, the signer): not a refusal. The wrap is
    tried again (``take_wrap``)."""


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
    """An active pass holder with a mailbox: a buyer whose address is not proven yet has none
    (sales.py), neither for mail in nor out."""
    from . import sales

    rows = svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"], ["keycast_pubkey", "=", pubkey]], ["id"], limit=1
    )
    return bool(rows) and not sales.unverified_key(svc, pubkey)


def channel_exists(svc: Services, channel: str) -> bool:
    got = followers._relay(svc)
    if got is None:
        return False
    relay, sign = got
    return bool(relay.query([{"kinds": [39000], "#d": [channel], "limit": 1}], sign))


def _top_channels(svc: Services) -> list[tuple[str, str]]:
    """(id, name) of the relay's channels (kind 39000), cached a minute."""
    with _lock:
        held = _cache.get("channels:")
    if held and time.monotonic() - held[0] < 60:
        return held[1]
    got = followers._relay(svc)
    found: list[tuple[str, str]] = []
    if got is not None:
        relay, sign = got
        for e in relay.query([{"kinds": [39000], "limit": 1000}], sign):
            d = tag(e, "d")
            if d:
                found.append((d, tag(e, "name") or d))
    with _lock:
        _cache["channels:"] = (time.monotonic(), found)
    return found


_SUFFIX = re.compile(r"-[0-9a-z]{3,8}$")


def _slug(name: str) -> str:
    """The chat's slugify (core/model.ts): ASCII letters and digits, dashes between words."""
    import unicodedata

    folded = unicodedata.normalize("NFKD", (name or "").lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:40]


def channel_by_slug(svc: Services, local: str) -> str | None:
    """The channel a plain address names (Xavier, 2026-11-12: bugs@ rather than bugs-c3e5@):
    the one channel whose id is LOCAL plus its random suffix, or whose name slugifies to LOCAL.
    Two channels answering to it: neither (the full id still works). Only a channel a belfry
    admin made (or the belfry itself) gets a plain address: anyone else's keeps its full id, so
    nobody squats a nice name (#issues 2026-11-15)."""
    hits = {
        cid
        for cid, name in _top_channels(svc)
        if _SUFFIX.sub("", cid) == local or _slug(name) == local
    }
    if len(hits) != 1:
        return None
    cid = hits.pop()
    return cid if _made_by_admin(svc, cid) else None


def _made_by_admin(svc: Services, channel: str) -> bool:
    """Was CHANNEL made (kind 9007) by a belfry admin or the belfry's own groups key? Not known
    (the relay keeps no 9007 for it): yes, as before."""
    from . import apps, webthreads

    maker = _makers(svc).get(channel)
    if maker is None:
        return True
    try:
        own = webthreads._groups_pubkey(svc)
    except Exception:  # noqa: BLE001
        own = ""
    return maker in set(apps.admin_pubkeys(svc)) or maker == own


def _makers(svc: Services) -> dict[str, str]:
    """channel → who made it (kind 9007), cached a minute."""
    with _lock:
        held = _cache.get("makers:")
    if held and time.monotonic() - held[0] < 60:
        return held[1]
    got = followers._relay(svc)
    found: dict[str, str] = {}
    if got is not None:
        relay, sign = got
        for e in sorted(
            relay.query([{"kinds": [9007], "limit": 2000}], sign) or [],
            key=lambda x: x.get("created_at", 0),
        ):
            h = tag(e, "h")
            if h and h not in found:
                found[h] = e.get("pubkey", "")
    with _lock:
        _cache["makers:"] = (time.monotonic(), found)
    return found


def channel_id(svc: Services, local: str) -> str | None:
    """The channel LOCAL@ reaches: its id itself, or its plain slug."""
    if channel_exists(svc, local):
        return local
    try:
        return channel_by_slug(svc, local)
    except Exception as exc:  # noqa: BLE001 — the relay not answering: no slug lookup
        log.info("mail: slug lookup for %s failed: %s", local, exc)
        return None


INBOX_D = "belfry:inbox"  # a group's NIP-78 setting (kind 30078): {"on": true} = shared inbox


def group_admins(svc: Services, channel: str) -> set[str]:
    got = followers._relay(svc)
    if got is None:
        return set()
    relay, sign = got
    found = relay.query(
        [{"kinds": [39001], "#d": [channel], "limit": 1}, {"kinds": [9000, 9001], "#h": [channel]}],
        sign,
    )
    lists = [e for e in found if e.get("kind") == 39001]
    out = {t[1] for e in lists[:1] for t in e.get("tags", []) if t[0] == "p" and len(t) > 1}
    # and whoever was put in with the admin role (co-hosts; zooid's 39001 never lists them)
    from .checkin import role_admins

    return out | role_admins([e for e in found if e.get("kind") in (9000, 9001)])


def inbox_on(svc: Services, channel: str) -> bool:
    """Whether CHANNEL takes mail from anyone: its admins' latest setting (cached a minute)."""
    with _lock:
        held = _cache.get("inbox:" + channel)
    if held and time.monotonic() - held[0] < 60:
        return bool(held[1])
    got = followers._relay(svc)
    on = False
    if got is not None:
        relay, sign = got
        admins = group_admins(svc, channel)
        found = relay.query(
            [{"kinds": [30078], "#d": [INBOX_D], "#h": [channel], "limit": 20}], sign
        )
        mine = sorted(
            (e for e in found if e.get("pubkey") in admins), key=lambda e: e["created_at"]
        )
        if mine:
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                on = bool(json.loads(mine[-1].get("content") or "{}").get("on"))
    with _lock:
        _cache["inbox:" + channel] = (time.monotonic(), {"on": on} if on else None)
    return on


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
    if local == "bounces":
        # the envelope sender of forwarded copies (mailforward.py): taken, logged and dropped
        found = {"kind": "bounces"}
    elif local in RESERVED:
        keys = sorted(apps.admin_pubkeys(svc))
        found = {"kind": "admins", "pubkeys": keys} if keys else None
    else:
        pubkey = svc.keycast.pubkey_for_name(local)
        if pubkey and active_pubkey(svc, pubkey):
            found = {"kind": "member", "pubkeys": [pubkey], "name": local}
        elif ch := channel_id(svc, local):
            found = {"kind": "channel", "channel": ch}
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


# Where the quoted history starts in a reply, in the languages members are likely to write in.
_QUOTE_HEAD = re.compile(
    r"^\s*(On\b.{4,300}\bwrote\s*:"
    r"|Le\b.{4,300}\ba [ée]crit\s*:"
    r"|Am\b.{4,300}\bschrieb.{0,80}:"
    r"|El\b.{4,300}\bescribi[óo]\s*:"
    r"|Op\b.{4,300}\bschreef.{0,80}:"
    r"|Il\b.{4,300}\bha scritto\s*:"
    r"|Em\b.{4,300}\bescreveu\s*:"
    r"|-{2,}\s*(Original Message|Message d'origine|Ursprüngliche Nachricht|Mensaje original"
    r"|Oorspronkelijk bericht|Forwarded message|Message transféré)\s*-{2,}"
    r"|_{10,})\s*$",
    re.I,
)
# Outlook's header block: "From: …" followed within a few lines by "Sent:/Date:/To:/Subject:".
_HDR = re.compile(r"^\s*\*?(From|De|Von|Van|Da)\s*:\*?\s", re.I)
_HDR_NEXT = re.compile(
    r"^\s*\*?(Sent|Date|To|Subject|Envoy[ée]|Datum|Gesendet|Aan|Onderwerp|Enviado|Asunto|Objet|À|An|Betreff)\s*:",
    re.I,
)
# Lines that end a message without being part of it: signatures and phone footers.
_FOOTER = re.compile(
    r"^\s*(--|—|Sent from my \w+.*|Sent from (Mail|Outlook|Yahoo|Proton).*|Get Outlook for .*"
    r"|Envoy[ée] de mon \w+.*|Von meinem \w+ gesendet.*|Verstuurd vanaf mijn \w+.*"
    r"|Enviado desde mi \w+.*|Inviato da .*)\s*$",
    re.I,
)


def _html_text(text: str) -> str:
    import html

    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", text)
    # the quoted history in HTML replies: Gmail, Apple Mail and Thunderbird, Outlook
    text = re.sub(r"(?is)<blockquote\b.*", "", text) if "<blockquote" in text.lower() else text
    text = re.sub(
        r'(?is)<div[^>]+(class="[^"]*(gmail_quote|moz-cite-prefix|yahoo_quoted)[^"]*"'
        r'|id="(appendonsend|divRplyFwdMsg|mail-editor-reference-message-container)").*',
        "",
        text,
    )
    text = (
        re.sub(r"(?is)<hr\b.*", "", text)
        if re.search(r"(?i)<hr\b[^>]*>\s*<div[^>]*>\s*<(b|font|span)", text)
        else text
    )
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text.replace("&nbsp;", " ")).replace("\xa0", " ")


def text_of(msg: EmailMessage) -> str:
    """What the sender actually wrote: no quoted history, no signature, no phone footer."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = _html_text(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        # "On Mon, 3 Nov 2026 at 10:00, Ana <ana@x.org>" + "wrote:" on the next line (Gmail wraps)
        joined = line + " " + lines[i + 1] if i + 1 < len(lines) else line
        if _QUOTE_HEAD.match(line) or (
            _QUOTE_HEAD.match(joined) and not line.strip().endswith(":")
        ):
            break
        if _HDR.match(line) and any(_HDR_NEXT.match(n) for n in lines[i + 1 : i + 5]):
            break
        if _FOOTER.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


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
            # compact, as sealed rumors are (docs/sealed.md §8): what mail_rumor measured
            "content": nip44_encrypt(
                conversation_key(secret, bytes.fromhex(to)), sealed.rumor_json(rumor)
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


def device_may_send(svc: Services, member: str, device: str) -> bool:
    """docs/sealed.md §7 Out: DEVICE may seal MEMBER's outgoing mail: it is a device of the head
    of their valid chain and the client key of one of their live chat sessions in the signer.
    ``Later`` when the relay or the signer does not answer: that is no refusal."""
    try:
        chain = sealed.chain_for(svc, member, strict=True)
        if chain is None or device not in chain.devices:
            # a device added a moment ago: don't refuse it on a minute-old answer
            chain = sealed.chain_for(svc, member, fresh=True, strict=True)
        if chain is None or device not in chain.devices:
            return False
        return device in sealed.live_devices(svc, member)
    except Exception as exc:  # noqa: BLE001
        raise Later(f"the device of {member[:8]}… could not be checked: {exc!r}") from exc


def unwrap_out(svc: Services, wrap: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(member, rumor) of a gift wrap to the belfry key. The seal is signed by the rumor's
    author (the account, through the signer) or by one of the author's devices (sealed.py)."""
    seal, rumor = sealed.open_wrap(_belfry(svc).secret, wrap)
    author, signer = rumor.get("pubkey"), seal["pubkey"]
    if author == signer:
        return signer, rumor  # as before sealing
    if rumor.get("kind") != MAIL:
        raise ValueError("not a mail")
    rumor = sealed.with_id(rumor)
    if not device_may_send(svc, author, signer):
        raise ValueError("the seal is not signed by the rumor's author or a device of theirs")
    return author, rumor


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
    """The kind 1301 rumor of a mail for TO. A rumor whose JSON would pass 40 000 bytes takes
    the Blossom route (docs/sealed.md §8): more does not fit a gift wrap, sealed or not (the
    wrap holds the seal's base64 of the padded rumor, under NIP-44's 65 535 bytes)."""
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
    rumor = {
        "kind": MAIL,
        "pubkey": key.public_key_xonly.format().hex(),
        "created_at": int(time.time()),
        "tags": rumor_tags,
        "content": raw.decode("utf-8", "replace") if len(raw) <= INLINE else "",
    }
    rumor["id"] = event_id(rumor)
    if not rumor["content"] or not sealed.fits(rumor):
        rumor["content"] = ""
        rumor["tags"] = rumor_tags + store_large(svc, raw)
        rumor["id"] = event_id(rumor)
    return rumor


def to_member(svc: Services, pubkey: str, raw: bytes, msg: EmailMessage, sender: str, auth: dict):
    secret = _belfry(svc).secret
    # sealed conversations (docs/sealed.md §7): a member whose chain is ready gets the mail
    # under their head letterbox, which only their devices open; anyone else as before
    try:
        chain = sealed.chain_for(svc, pubkey, strict=True)
    except Exception as exc:  # noqa: BLE001
        # not known whether this member's mail is sealed: never delivered unsealed because of
        # a failure. The sending server tries again.
        log.warning("mail: the chain of %s… could not be read: %r", pubkey[:8], exc)
        raise MailError(451, "4.3.0 Delivery failed here; try again later.") from exc
    rumor = mail_rumor(svc, raw, msg, sender, auth, pubkey)
    if chain is not None and chain.ready:
        wrap = sealed.seal_to_letterbox(secret, rumor, pubkey, chain.lb)
    else:
        wrap = gift_wrap(secret, rumor, pubkey)
    _publish(svc, wrap)


# ── in: a channel's mail ────────────────────────────────────────────────────────────────────
_MSGID = re.compile(r"<[^<>\s]{1,250}>")


def message_refs(msg: EmailMessage) -> list[str]:
    """The Message-IDs a mail answers: In-Reply-To, then References (newest last), at most 10."""
    found: list[str] = []
    for h in ("References", "In-Reply-To"):
        for mid in _MSGID.findall(str(msg[h] or "")):
            if mid not in found:
                found.append(mid)
    return found[-10:]


def _mail_tags(msg: EmailMessage) -> list[list[str]]:
    """message-id, in-reply-to and references: what lets a later reply find its thread."""
    out = [["message-id", str(msg["Message-ID"] or "").strip()[:300]]]
    if msg["In-Reply-To"]:
        out.append(["in-reply-to", str(msg["In-Reply-To"]).strip()[:300]])
    refs = message_refs(msg)
    if refs:
        out.append(["references", " ".join(refs)[:1000]])
    return out


def thread_of(svc: Services, channel: str, refs: list[str]) -> dict[str, Any] | None:
    """The message in CHANNEL a reply belongs under: the newest one whose ``message-id`` is in
    REFS (an e-mail card, what went out, a member's mail), or, when that is itself a thread
    reply, the message its thread hangs off. None: not an answer to anything here."""
    if not refs:
        return None
    got = followers._relay(svc)
    if got is None:
        return None
    relay, sign = got
    try:
        found = relay.query([{"kinds": [9, 1111], "#h": [channel], "limit": 1000}], sign) or []
    except Exception as exc:  # noqa: BLE001 — not threaded is better than not delivered
        log.info("mail: thread lookup in %s failed: %r", channel, exc)
        return None
    wanted = set(refs)
    hits = [e for e in found if tag(e, "message-id") in wanted]
    if not hits:
        return None
    hit = max(hits, key=lambda e: refs.index(tag(e, "message-id")))  # the newest it names
    if hit.get("kind") == 1111:
        root_id = tag(hit, "E")
        root = next((e for e in found if e.get("id") == root_id), None)
        return root
    return hit


def thread_tags(root: dict[str, Any]) -> list[list[str]]:
    """A NIP-22 reply's tags under ROOT (the chat's postThreadReply)."""
    rid, pk, kind = root["id"], root.get("pubkey", ""), str(root.get("kind", 9))
    return [
        ["E", rid, "", pk],
        ["K", kind],
        ["P", pk],
        ["e", rid, "", pk],
        ["k", kind],
        ["p", pk],
    ]


def to_channel(
    svc: Services, channel: str, raw: bytes, msg: EmailMessage, sender: str, auth: dict
) -> None:
    member = apps.member_by_email(svc, sender)
    if not (member and auth.get("aligned")) and inbox_on(svc, channel):
        # a member whose provider did not prove the address is treated as anyone else (it could
        # be forged); say which of the two it was, never the address (Xavier's card, 2026-11-16)
        log.info(
            "mail: to the inbox of %s (sender is a member: %s, aligned: %s, dkim: %s, spf: %s)",
            channel,
            bool(member),
            bool(auth.get("aligned")),
            auth.get("dkim") or "none",
            auth.get("spf") or "none",
        )
        return to_inbox(svc, channel, raw, msg, sender, auth)
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
    imeta, refused = _files(svc, files) if files else ([], [])
    text += _refused_note(refused)
    if not text.strip() and not imeta:
        raise MailError(550, "5.6.0 This mail is empty.")
    if community.member_signer(svc, member["partner"]) is None:
        text = f"✉️ From {member['name']}\n\n{text}"  # their own key: posted by the belfry
    # the whole e-mail, encrypted on Blossom; the key travels in the (members-only) message
    extra = [["subject", subject[:300]], *_original(svc, raw), *_mail_tags(msg)]
    # an answer to something in the channel (an e-mail card, what went out, a member's mail):
    # a reply in that message's thread, not a new message in the channel
    root = thread_of(svc, channel, message_refs(msg))
    try:
        if root is not None:
            drops.post(
                svc,
                channel,
                text,
                imeta + thread_tags(root) + extra,
                member["partner"],
                client="belfry-mail",
                kind=1111,
            )
        else:
            drops.post(svc, channel, text, imeta + extra, member["partner"], client="belfry-mail")
    except Exception as exc:  # noqa: BLE001 — the relay refuses non-members of closed channels
        log.info("mail: post to %s refused: %s", channel, exc)
        raise MailError(550, f"5.7.1 You cannot post in {channel}.") from exc


def _original(svc: Services, raw: bytes) -> list[list[str]]:
    """The whole e-mail, encrypted on Blossom: a ``mail-original`` tag, or none when it could not
    be stored (the mail itself still goes in; #issues 2026-11-15, potluck@ lost a mail on a 415)."""
    try:
        found = {t[0]: t[1] for t in store_large(svc, raw)}
    except Exception as exc:  # noqa: BLE001
        log.warning("mail: the original could not be kept: %r", exc)
        return []
    return [["mail-original", found["url"], found["decryption-key"], found["decryption-nonce"]]]


def _files(svc: Services, files: list[tuple[str, str, bytes]]) -> tuple[list[list[str]], list[str]]:
    """Each attachment on Blossom, one by one: (their imeta tags, the names it refused)."""
    tags: list[list[str]] = []
    refused: list[str] = []
    for f in files:
        try:
            tags += drops.upload(svc, [f])
        except Exception as exc:  # noqa: BLE001 — a type it does not take, too large
            log.info("mail: attachment %r not kept: %r", f[0], exc)
            refused.append(f[0] or "an attachment")
    return tags, refused


def _refused_note(refused: list[str]) -> str:
    return (
        f"\n\n_(Not kept: {', '.join(refused)}. "
        "Only pictures, sound, video, PDF and text files are.)_"
        if refused
        else ""
    )


def to_inbox(
    svc: Services, channel: str, raw: bytes, msg: EmailMessage, sender: str, auth: dict
) -> None:
    """Mail from anyone to a shared inbox: an e-mail card in the channel, posted by the groups
    key (the chat knows it as ``_groups@<domain>`` and only then shows the card)."""
    subject = str(msg["Subject"] or "").strip()
    body = text_of(msg)[:8000]
    name = email.utils.parseaddr(str(msg["From"] or ""))[0].strip()
    files = [f for f in attachments(msg) if len(f[2]) <= 20_000_000]
    imeta, refused = _files(svc, files) if files else ([], [])
    text = "\n\n".join(x for x in (f"**{subject}**" if subject else "", body) if x) + _refused_note(
        refused
    )
    extra = [
        ["subject", subject[:300]],
        *_original(svc, raw),
        ["mail-from", sender],
        ["mail-name", name[:100]],
        *_mail_tags(msg),
        ["belfry-auth", "pass" if auth.get("aligned") else "none"],
    ]
    drops.post(svc, channel, text or "(empty)", imeta + extra, None, client="belfry-mail")


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
    posted: list[str] = []  # channels that took it: their forward addresses get a copy
    for rcpt in rcpts[:MAX_RCPT]:
        target = resolve(svc, rcpt)
        try:
            if target is None:
                raise MailError(550, f"5.1.1 No one is called {rcpt} here.")
            if target["kind"] == "bounces":
                # a bounce or an auto-reply to a forwarded copy: never posted, never forwarded
                log.info("mail: a mail to bounces@ dropped")
                done += 1
                continue
            if target["kind"] == "channel":
                if target["channel"] in posted:  # its plain alias and its id, both: once
                    done += 1
                    continue
                to_channel(svc, target["channel"], raw, msg, sender, auth)
                posted.append(target["channel"])
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
    if posted:
        from . import mailforward

        mailforward.forward(svc, posted, raw, sender)
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


def outgoing(
    svc: Services, sender: str, rumor: dict[str, Any]
) -> tuple[list[str], int, str, tuple[str, EmailMessage] | None]:
    """Send a member's kind 1301: (recipients, SMTP code, answer, (channel, message) when sent
    as a shared inbox). MailError: refused here."""
    mail_from = tag(rumor, "mail-from").lower()
    rcpts = [r.strip() for r in tags(rumor, "rcpt-to") if "@" in r][:MAX_RCPT]
    local, _, dom = mail_from.rpartition("@")
    if not rcpts:
        raise MailError(554, "No recipient (rcpt-to).")
    if dom != domain(svc) or not LOCAL.match(local) or local in RESERVED:
        raise MailError(554, f"You can only send as your own address at {domain(svc)}.")
    as_channel = None
    if svc.keycast.pubkey_for_name(local) != sender or not active_pubkey(svc, sender):
        # a shared inbox answers as the channel (its id or its plain slug): its admins only
        ch = channel_id(svc, local)
        if not (
            ch
            and inbox_on(svc, ch)
            and sender in group_admins(svc, ch)
            and active_pubkey(svc, sender)
        ):
            raise MailError(554, f"{mail_from} is not yours, or your membership is not active.")
        as_channel = ch
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
    return rcpts, code, answer, (as_channel, msg) if as_channel else None


def sent_card(
    svc: Services, channel: str, sender: str, msg: EmailMessage, rcpts: list[str], reply_to: str
) -> None:
    """What went out as the channel, in the channel: only the bridge posts these."""
    subject = str(msg["Subject"] or "").strip()
    tags_ = [
        ["subject", subject[:300]],
        ["mail-to", ", ".join(rcpts)[:600]],
        ["sent-by", sender],
        ["message-id", str(msg["Message-ID"] or "").strip()[:300]],
    ]
    if re.fullmatch(r"[0-9a-f]{64}", reply_to or ""):
        tags_.append(["e", reply_to, "", "reply"])
    drops.post(
        svc, channel, text_of(msg)[:8000] or "(empty)", tags_, None, client="belfry-mail-out"
    )


def report(
    svc: Services,
    sender: str,
    email_id: str,
    rcpts: list[str],
    code: int,
    answer: str,
    chain: sealed.Chain | None = None,
):
    """A kind 7679 delivery report to SENDER (Nostr Mail DSN): an ephemeral key, no p tag.
    Encrypted to the head letterbox of CHAIN (the sender's), named in an ``lb`` tag, when it is
    ready (docs/sealed.md §7); else to their account key. The caller reads the chain, before
    anything is sent (handle_wrap)."""
    sealed_to = chain.lb if chain is not None and chain.ready else ""
    event = report_event(
        _belfry(svc).secret, sender, sealed_to, email_id, rcpts, code, answer, int(time.time())
    )
    _publish(svc, event)


def report_event(
    belfry_secret: bytes,
    sender: str,
    lb: str,
    email_id: str,
    rcpts: list[str],
    code: int,
    answer: str,
    now: int,
    temp: bytes | None = None,
    nonce: bytes | None = None,
    aux: bytes | None = b"",
) -> dict[str, Any]:
    """The kind 7679 itself, signed by the belfry key: encrypted from a throwaway key (named in
    ``ephemeral-pubkey``) to the letterbox LB, with an ``lb`` tag, or to SENDER's account key
    when LB is "". TEMP, NONCE, AUX fix what is otherwise drawn (the vectors)."""
    status = "delivered" if 200 <= code < 300 else "failed" if code >= 500 else "delayed"
    body = {
        "status": status,
        "recipient": ", ".join(rcpts),
        "smtp_code": code,
        "smtp_response": answer[:500],
        "attempted_at": now,
        ("delivered_at" if status == "delivered" else "failed_at"): now,
        "details": answer[:500],
    }
    once = temp or PrivateKey().secret
    content = nip44_encrypt(
        conversation_key(once, bytes.fromhex(lb or sender)), json.dumps(body), nonce
    )
    return sealed.sign(
        belfry_secret,
        {
            "kind": DSN,
            "created_at": now,
            "tags": [
                ["r", email_id],
                ["ephemeral-pubkey", sealed.pubkey_of(once)],
                *([["lb", lb]] if lb else []),
            ],
            "content": content,
        },
        aux,
    )


def reported(svc: Services, email_id: str) -> bool:
    if svc.relay is None:
        return False
    me = _belfry(svc).public_key_xonly.format().hex()
    found = svc.relay.query(
        [{"kinds": [DSN], "authors": [me], "#r": [email_id], "limit": 1}], _sign_as_belfry(svc)
    )
    return bool(found)


def handle_wrap(svc: Services, wrap: dict[str, Any]) -> None:
    sender, rumor = unwrap_out(svc, wrap)
    if rumor.get("kind") != MAIL:
        return
    if rumor.get("created_at", 0) < time.time() - (WINDOW_DAYS + 1) * 86400:
        return
    email_id = tag(rumor, "email-id") or rumor.get("id") or wrap["id"]
    if reported(svc, email_id):
        return  # sent before a restart
    try:
        # whom the report is for (their letterbox, or their account key), known before the mail
        # leaves: a report is never encrypted to the account key because the read failed, and a
        # mail is never sent twice because its report could not be made
        chain = sealed.chain_for(svc, sender, strict=True)
    except Exception as exc:  # noqa: BLE001
        raise Later(f"the chain of {sender[:8]}… could not be read: {exc!r}") from exc
    sent = None
    try:
        rcpts, code, answer, sent = outgoing(svc, sender, rumor)
    except MailError as exc:
        rcpts, code, answer = tags(rumor, "rcpt-to")[:MAX_RCPT], exc.code, exc.message
    log.info("mail: out from %s… to %d recipients: %s", sender[:8], len(rcpts), code)
    report(svc, sender, email_id, rcpts, code, answer, chain)
    if sent and 200 <= code < 300:
        try:
            sent_card(svc, sent[0], sender, sent[1], rcpts, tag(rumor, "e"))
        except Exception as exc:  # noqa: BLE001 — the mail is gone; the card is a courtesy
            log.warning("mail: sent card in %s failed: %r", sent[0], exc)


# How long after each failed try a wrap that hit a ``Later`` is tried again: 30 s, 2 min, 10 min,
# then every 30 min, for a day. In memory: after a restart the listener reads the last three
# days again and what has no report yet is taken up.
RETRY_AFTER = (30.0, 120.0, 600.0)
RETRY_EVERY = 1800.0
RETRY_FOR = 86400.0


def _later(seconds: float, call) -> None:
    timer = threading.Timer(seconds, call)
    timer.daemon = True
    timer.start()


def take_wrap(svc: Services, wrap: dict[str, Any], tries: int = 0, waited: float = 0.0) -> None:
    """``handle_wrap``, and again later when the relay or the signer did not answer. Anything
    else it raises is the caller's (a wrap that is not ours, a refusal: dropped)."""
    try:
        handle_wrap(svc, wrap)
    except Later as exc:
        delay = RETRY_AFTER[tries] if tries < len(RETRY_AFTER) else RETRY_EVERY
        if waited + delay > RETRY_FOR:
            log.warning("mail: a wrap given up after a day: %s", exc)
            return
        log.info("mail: a wrap again in %.0f s: %s", delay, exc)
        _later(delay, lambda: _retry(svc, wrap, tries + 1, waited + delay))


def _retry(svc: Services, wrap: dict[str, Any], tries: int, waited: float) -> None:
    try:
        take_wrap(svc, wrap, tries, waited)
    except (ValueError, KeyError, TypeError) as exc:
        log.debug("mail: skipped a wrap: %s", exc)
    except Exception as exc:  # noqa: BLE001 — a timer's thread: nothing above to tell
        log.warning("mail: a wrap failed: %s", exc)


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
                            await run_in_threadpool(take_wrap, svc, msg[2])
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

async def member_name(services: Any, name: str) -> JSONResponse:
    """NIP-05 for a member's username, read from the signer: ``{"names": {<name>: <pubkey>}}``
    (the name in lowercase, as NIP-05 asks), or no names."""
    from .usernames import VALID

    cors = {"Access-Control-Allow-Origin": "*"}
    low = name.strip().lower()
    if not VALID.match(low):
        return JSONResponse({"names": {}}, headers=cors)
    try:
        pub = await run_in_threadpool(services.keycast.pubkey_for_name, low)
    except Exception as exc:  # noqa: BLE001 — the signer not answering is not "no such name"
        log.warning("nostr.json: the signer did not answer: %s", exc)
        return JSONResponse({"names": {}}, status_code=503, headers={**cors, "Retry-After": "5"})
    if not pub:
        return JSONResponse({"names": {}}, headers=cors)
    return JSONResponse(
        {"names": {low: pub}}, headers={**cors, "Cache-Control": "public, max-age=60"}
    )

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
        if name == "_groups" and services.groups is not None and services.settings.mail_in:
            # the key that posts e-mail cards in shared inboxes (the chat trusts only it)
            gpub = services.groups.own
            return JSONResponse(
                {"names": {"_groups": gpub}},
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "public, max-age=300",
                },
            )
        if name not in ("_smtp", "_groups") and stewards.own_mode(services.settings):
            # Belfry's own signer has no web side: its usernames are answered here (Keycast
            # answers them itself, and the proxy sends it everything but the two names above;
            # docs/own-signer.md, "Still to move")
            return await member_name(services, name)
        pub = homepage.belfry_pubkey(services.settings)
        if name != "_smtp" or not pub or not services.settings.mail_in:
            return JSONResponse({"names": {}}, headers={"Access-Control-Allow-Origin": "*"})
        relay = services.settings.community_relay_public_url
        return JSONResponse(
            {"names": {"_smtp": pub}, "relays": {pub: [relay] if relay else []}},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"},
        )
