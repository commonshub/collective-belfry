"""Forward a channel's e-mails to an outside address (docs/mail.md, "Forwarding").

For a transition (hello@ moves into a channel, but an old mailbox must keep getting every mail),
a channel's admins set "Forward all e-mails to": every mail the channel takes (its plain alias or
its id address; a member's mail posted as a message or a thread reply, a shared-inbox card) is
still posted, and a copy goes to that address.

- The setting is the belfry's, never in a Nostr event (every member could read the address): a
  register param ``belfry.mail_forward.<channel>`` (JSON). REGISTER=belfry only.
- ``POST /api/mail-forward`` (belfry.<domain>/api/v1/mail-forward, NIP-98) by someone who may edit
  the channel (the /group-meta rule: its admins, admins by role, the belfry's admins; members only,
  never guests or agents).
- Nothing is forwarded before the outside address confirms: a mail with a link to
  ``belfry.<domain>/mail-forward/<token>`` (7 days); GET shows one button, only the POST confirms
  (mail scanners open links).
- The copy is the original message with its header block edited in place (body and attachments
  untouched, Message-ID / Subject / Date / To / Cc / In-Reply-To / References byte for byte):
  From becomes ``"<name> via <alias>@<domain>" <alias@domain>`` (the provider signs only for our
  domain), Reply-To the original From, the original's signatures and envelope headers go. It
  leaves through the belfry's SMTP provider, envelope sender ``bounces@<domain>`` (mail to it is
  dropped).
"""

from __future__ import annotations

import email.policy
import email.utils
import hashlib
import json
import logging
import re
import secrets
import smtplib
import threading
import time
from html import escape as E
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .core import Services

log = logging.getLogger(__name__)

PATH = "/api/mail-forward"
PAGE = "/mail-forward"
PARAM = "belfry.mail_forward."
TOKEN_PARAM = "belfry.mail_forward_token."
TOKEN_DAYS = 7
CONFIRMS_PER_HOUR = 3
FORWARDS_PER_HOUR = 200
BOUNCES = "bounces"
HEADER = "X-Belfry-Forward"
RETRY_DELAYS = (0, 60, 240)  # seconds before each try
IN_THREAD = True  # tests: forward in the caller's thread
ADDRESS = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$"
)
GROUP_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# public mailbox providers: a forward to one of these doesn't stop mail from everyone else there
PUBLIC_PROVIDERS = frozenset(
    "gmail.com googlemail.com outlook.com hotmail.com live.com msn.com yahoo.com ymail.com "
    "icloud.com me.com mac.com proton.me protonmail.com protonmail.ch pm.me gmx.com gmx.net "
    "gmx.de aol.com fastmail.com fastmail.fm mail.ru yandex.com yandex.ru zoho.com".split()
)
# headers of the original that go: replaced, or proofs that no longer hold
DROP = {
    "from",
    "reply-to",
    "sender",
    "return-path",
    "dkim-signature",
    "x-original-from",
    "x-original-reply-to",
    "x-forwarded-for-channel",
    HEADER.lower(),
}
DROP_PREFIX = ("arc-",)

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def own(svc: Services) -> bool:
    from .register import Register

    return isinstance(svc.odoo, Register)


def _domain(svc: Services) -> str:
    return (svc.settings.nip05_domain or "").lower()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── the stored record ───────────────────────────────────────────────────────────────────────
def load(svc: Services, channel: str) -> dict[str, Any] | None:
    if not own(svc):
        return None
    raw = svc.odoo.param(PARAM + channel, "")
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("address") else None


def _save(svc: Services, channel: str, rec: dict[str, Any] | None) -> None:
    svc.odoo.set_param(PARAM + channel, json.dumps(rec) if rec else "")


def public(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rec:
        return None
    return {
        "address": rec["address"],
        "state": rec.get("state", "pending"),
        "set_by": rec.get("set_by", ""),
        "since": rec.get("since", 0),
        "same_domain_skipped": not _public_provider(rec["address"]),
    }


def _public_provider(address: str) -> bool:
    return address.rpartition("@")[2].lower() in PUBLIC_PROVIDERS


# ── names ───────────────────────────────────────────────────────────────────────────────────
def channel_name(svc: Services, channel: str) -> str:
    from . import mail

    try:
        for cid, name in mail._top_channels(svc):
            if cid == channel:
                return name
    except Exception:  # noqa: BLE001
        pass
    return channel


def alias(svc: Services, channel: str) -> str:
    """The left part of the channel's address: its plain slug when that reaches it, else its id
    (the chat's model.mailLocal)."""
    from . import mail

    want = mail._slug(channel_name(svc, channel)) or mail._SUFFIX.sub("", channel)
    if not want or want == channel:
        return channel
    try:
        return want if mail.channel_by_slug(svc, want) == channel else channel
    except Exception:  # noqa: BLE001
        return channel


# ── who may change it ───────────────────────────────────────────────────────────────────────
def may_edit(svc: Services, caller: str, channel: str) -> bool:
    from . import checkin, whois

    if whois.kinds(svc).get(caller) != "member":
        return False
    if caller in checkin._belfry_admins(svc):
        return True
    return caller in checkin.group_state(svc, channel).admins


def _ours(dom: str, mine: str) -> bool:
    """DOM is this belfry's domain or one of its hosts (a parent domain is someone else's)."""
    dom = dom.lower().strip(".")
    return bool(mine and dom) and (dom == mine or dom.endswith("." + mine))


def own_domain(svc: Services, address: str) -> bool:
    return _ours(address.rpartition("@")[2], _domain(svc))


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def api(svc: Services, caller: str, data: dict[str, Any]) -> dict[str, Any]:
    from . import checkin, mail
    from .holder_invites import ApiError

    if not own(svc):
        raise ApiError(409, "unsupported", "Forwarding needs the belfry's own register.")
    channel = str(data.get("channel") or "").strip()
    if not GROUP_RE.match(channel):
        raise ApiError(422, "invalid", 'Send {"channel"}.')
    if not mail.channel_exists(svc, channel):
        raise ApiError(404, "unknown_channel", "No such channel.")
    if not may_edit(svc, caller, channel):
        raise ApiError(403, "not_allowed", "Only the channel's admins change this.")
    rec = load(svc, channel)
    if "address" in data:
        address = data["address"]
        if address is None or address == "":
            if rec:
                _drop_token(svc, rec)
                _save(svc, channel, None)
                log.info(
                    "mail: forward of %s turned off by %s (was @%s)",
                    channel,
                    caller[:12],
                    rec["address"].rpartition("@")[2],
                )
            return {"forward": None}
        if not isinstance(address, str) or not ADDRESS.match(address.strip()):
            raise ApiError(422, "invalid", "That is not an e-mail address.")
        address = address.strip()
        if own_domain(svc, address):
            raise ApiError(422, "own_domain", "Forward to an address outside this belfry.")
        if checkin.is_archived(svc, channel):
            raise ApiError(409, "archived", "This channel is archived. Unarchive it first.")
        _need_mail(svc)
        if rec and rec["address"].lower() == address.lower() and rec.get("state") == "on":
            return {"forward": public(rec)}  # nothing changes
        _rate(svc, rec)
        if rec:
            _drop_token(svc, rec)
        new = {
            "address": address,
            "state": "pending",
            "set_by": caller,
            "since": int(time.time()),
            "mails": (rec or {}).get("mails", []),
        }
        _send_confirmation(svc, channel, new)
        _save(svc, channel, new)
        log.info(
            "mail: forward of %s set by %s to @%s (pending)",
            channel,
            caller[:12],
            address.rpartition("@")[2],
        )
        return {"forward": public(new)}
    if data.get("resend"):
        if not rec or rec.get("state") != "pending":
            raise ApiError(409, "not_pending", "Nothing is waiting for a confirmation.")
        if checkin.is_archived(svc, channel):
            raise ApiError(409, "archived", "This channel is archived. Unarchive it first.")
        _need_mail(svc)
        _rate(svc, rec)
        _drop_token(svc, rec)
        _send_confirmation(svc, channel, rec)
        _save(svc, channel, rec)
        return {"forward": public(rec)}
    return {"forward": public(rec)}


def _need_mail(svc: Services) -> None:
    from .holder_invites import ApiError

    if not svc.settings.smtp_host:
        raise ApiError(503, "no_mail", "This belfry has no mail provider yet.")


def _rate(svc: Services, rec: dict[str, Any] | None) -> None:
    from .holder_invites import ApiError

    now = time.time()
    recent = [t for t in (rec or {}).get("mails", []) if t > now - 3600]
    if len(recent) >= CONFIRMS_PER_HOUR:
        raise ApiError(429, "too_many", "Too many confirmation mails this hour.")


def _drop_token(svc: Services, rec: dict[str, Any]) -> None:
    if rec.get("token"):
        svc.odoo.set_param(TOKEN_PARAM + rec["token"], "")
    rec.pop("token", None)
    rec.pop("token_exp", None)


def _send_confirmation(svc: Services, channel: str, rec: dict[str, Any]) -> None:
    """A new token in REC, and the mail to its address (REC is saved by the caller)."""
    from . import console, mailer
    from .holder_invites import ApiError

    token = secrets.token_urlsafe(24)
    rec["token"] = _hash(token)
    rec["token_exp"] = int(time.time()) + TOKEN_DAYS * 86400
    now = time.time()
    rec["mails"] = [t for t in rec.get("mails", []) if t > now - 3600] + [now]
    svc.odoo.set_param(TOKEN_PARAM + rec["token"], channel)
    base = console.console_url(svc.settings) or (svc.settings.public_url or "").rstrip("/")
    link = f"{base}{PAGE}/{token}"
    m = mailer.Mailer(svc.settings)
    name = m.name
    cname = channel_name(svc, channel)
    sent = m.send(
        rec["address"],
        f"Forward {cname}'s e-mails to this address?",
        [
            f"{name}'s channel “{cname}” wants to forward every e-mail it receives "
            "to this address.",
            "Nothing is forwarded until you confirm. If you didn't expect this, ignore this "
            "mail: the link stops working in 7 days.",
        ],
        button=(link, "Confirm forwarding"),
    )
    if not sent:
        raise ApiError(503, "no_mail", "The confirmation mail could not be sent.")


def _by_token(svc: Services, token: str) -> tuple[str, dict[str, Any]] | None:
    if not own(svc) or not token or len(token) > 100:
        return None
    h = _hash(token)
    channel = svc.odoo.param(TOKEN_PARAM + h, "")
    if not channel:
        return None
    rec = load(svc, channel)
    if not rec or rec.get("token") != h or int(rec.get("token_exp", 0)) < time.time():
        return None
    return channel, rec


def confirm(svc: Services, token: str) -> tuple[str, dict[str, Any]] | None:
    found = _by_token(svc, token)
    if not found:
        return None
    channel, rec = found
    _drop_token(svc, rec)
    rec["state"] = "on"
    rec["confirmed"] = int(time.time())
    _save(svc, channel, rec)
    log.info("mail: forward of %s confirmed by @%s", channel, rec["address"].rpartition("@")[2])
    return channel, rec


# ── forwarding ──────────────────────────────────────────────────────────────────────────────
def _split(raw: bytes) -> tuple[list[bytes], bytes, bytes]:
    """(header fields, each with its folded lines, the line ending, the rest from the blank
    line on)."""
    nl = b"\r\n" if b"\r\n" in raw[: raw.find(b"\n") + 1 or len(raw)] else b"\n"
    end = raw.find(nl + nl)
    head, rest = (raw, b"") if end < 0 else (raw[: end + len(nl)], raw[end + len(nl) :])
    fields: list[bytes] = []
    for line in head.split(nl):
        if not line:
            continue
        if line[:1] in (b" ", b"\t") and fields:
            fields[-1] += nl + line
        else:
            fields.append(line)
    return fields, nl, rest


def _name(field: bytes) -> str:
    return field.split(b":", 1)[0].strip().decode("ascii", "replace").lower()


def _value(field: bytes) -> bytes:
    return field.split(b":", 1)[1].strip() if b":" in field else b""


def build(svc: Services, channel: str, raw: bytes) -> bytes:
    """The copy: RAW with its header block edited, the body untouched."""
    fields, nl, rest = _split(raw)
    original_from = next((f for f in fields if _name(f) == "from"), None)
    original_reply = next((f for f in fields if _name(f) == "reply-to"), None)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    shown, addr = email.utils.parseaddr(str(msg["From"] or ""))
    who = alias(svc, channel)
    dom = _domain(svc)
    mine = f"{who}@{dom}"
    display = f"{(shown or addr or 'Someone').strip()} via {mine}"
    frm = email.utils.formataddr((display, mine), charset="utf-8")
    kept = [f for f in fields if _name(f) not in DROP and not _name(f).startswith(DROP_PREFIX)]
    head = [f"From: {frm}".encode()]
    if original_from is not None:
        head.append(b"Reply-To: " + _value(original_from))
        head.append(b"X-Original-From: " + _value(original_from))
    if original_reply is not None:
        head.append(b"X-Original-Reply-To: " + _value(original_reply))
    head.append(f"X-Forwarded-For-Channel: {mine}".encode())
    head.append(f"{HEADER}: {dom}".encode())
    return nl.join(head + kept) + nl + rest


def skip_reason(svc: Services, rec: dict[str, Any], raw: bytes, sender: str) -> str:
    """Why this mail is not forwarded ("" = it is)."""
    if rec.get("state") != "on":
        return "pending"
    fields, _, _ = _split(raw)
    dom = _domain(svc)
    for f in fields:
        if _name(f) == HEADER.lower() and _value(f).decode("ascii", "replace").lower() == dom:
            return "forwarded already"
    target = rec["address"].lower()
    sender = (sender or "").lower()
    sdom = sender.rpartition("@")[2]
    if sender == target:
        return "from the forward address"
    if _ours(sdom, dom):
        return "from this belfry's domain"
    if not _public_provider(target) and sdom == target.rpartition("@")[2]:
        return "from the forward address's domain"
    return ""


def _limited(channel: str) -> bool:
    now = time.monotonic()
    with _lock:
        hits = [t for t in _hits.get(channel, []) if t > now - 3600]
        if len(hits) >= FORWARDS_PER_HOUR:
            _hits[channel] = hits
            return True
        _hits[channel] = [*hits, now]
        return False


def forward(svc: Services, channels: list[str], raw: bytes, sender: str) -> None:
    """After a mail was posted in CHANNELS: a copy to each one's forward address. Never raises."""
    for channel in dict.fromkeys(channels):
        try:
            rec = load(svc, channel)
            if not rec:
                continue
            why = skip_reason(svc, rec, raw, sender)
            if why:
                if why != "pending":
                    log.info("mail: not forwarding a mail of %s (%s)", channel, why)
                continue
            if _limited(channel):
                log.warning("mail: forward of %s stopped: %d this hour", channel, FORWARDS_PER_HOUR)
                continue
            data = build(svc, channel, raw)
        except Exception as exc:  # noqa: BLE001 — the post is made already; nothing to undo
            log.warning("mail: forward of %s failed: %s", channel, type(exc).__name__)
            continue
        if IN_THREAD:
            threading.Thread(
                target=_send, args=(svc, channel, rec["address"], data), daemon=True
            ).start()
        else:
            _send(svc, channel, rec["address"], data)


def _send(svc: Services, channel: str, address: str, data: bytes) -> None:
    from . import mail

    bounce = f"{BOUNCES}@{_domain(svc)}"
    for i, wait in enumerate(RETRY_DELAYS):
        if wait:
            time.sleep(wait)
        try:
            code, answer = mail.smtp_send(svc, bounce, [address], data)
        except smtplib.SMTPResponseException as exc:
            code, answer = exc.smtp_code, str(exc.smtp_error)
        except smtplib.SMTPRecipientsRefused as exc:
            code, why = next(iter(exc.recipients.values()))
            answer = str(why)
        except Exception as exc:  # noqa: BLE001 — a network error, no provider: try again
            code, answer = 451, type(exc).__name__
        if 200 <= code < 300:
            log.info("mail: forwarded a mail of %s to @%s", channel, address.rpartition("@")[2])
            return
        size = code == 552 or "size" in str(answer).lower()
        log.warning(
            "mail: forward of %s failed: %s%s (try %d)",
            channel,
            code,
            " too big" if size else "",
            i + 1,
        )
        if 500 <= code < 600:
            return  # refused for good (size, address): no retry


# ── the routes ──────────────────────────────────────────────────────────────────────────────
TITLE = "Forwarding e-mails"


def _page(body: str, status: int = 200) -> HTMLResponse:
    from . import theme

    return HTMLResponse(
        theme.frame(TITLE, f"<h1>{TITLE}</h1>{body}"),
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


GONE = "<p>This link doesn't work any more. Ask the channel's admins to send a new one.</p>"


def register(app: FastAPI) -> None:
    from .holder_invites import ApiError, authenticate

    @app.post(PATH)
    async def mail_forward(request: Request) -> JSONResponse:
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
            raise ApiError(400, "invalid_json", "Send a JSON object.")
        out = await run_in_threadpool(api, s, caller, data)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.get(PAGE + "/{token}", response_class=HTMLResponse)
    async def forward_page(request: Request, token: str):
        s = request.app.state.services
        found = await run_in_threadpool(_by_token, s, token)
        if not found:
            return _page(GONE, 404)
        channel, rec = found
        from . import mailer

        name = mailer.Mailer(s.settings).name
        cname = await run_in_threadpool(channel_name, s, channel)
        return _page(
            f"<p>{E(name)}'s channel “{E(cname)}” wants to forward every e-mail it receives to "
            f"<b>{E(rec['address'])}</b>.</p>"
            "<p>The e-mails still show up in the channel; a copy comes here. To stop, ask the "
            "channel's admins to turn forwarding off.</p>"
            f'<form method="post" action="{PAGE}/{E(token)}">'
            '<button class="btn" type="submit">Confirm forwarding</button></form>'
        )

    @app.post(PAGE + "/{token}")
    async def forward_confirm(request: Request, token: str) -> Response:
        s = request.app.state.services
        found = await run_in_threadpool(confirm, s, token)
        if not found:
            return _page(GONE, 404)
        return _page(
            "<p>Done: this address now gets a copy of every e-mail the channel receives. "
            "To stop, ask the channel's admins to turn forwarding off.</p>"
        )


__all__ = ["register", "api", "forward", "build", "confirm", "load", "BOUNCES"]
