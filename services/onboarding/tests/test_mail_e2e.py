"""Incoming mail end to end: a sending server talks SMTP to the real mail-in (mailfront.py), which
hands RCPT and DATA over HTTP to the real onboarding app (mail.py), which posts the inbox card in
the channel (the groups relay, a fake) and forwards a copy through the belfry's SMTP provider (a
local SMTP server here). Only Blossom and the relays are fakes.

Mail broke silently twice (a clobbered inbox setting; mail-in on IPv4 only): this test is the
whole path a real mail takes, from the first SMTP command to the forwarded copy."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import smtplib
import socket
import threading
import time
from email import message_from_bytes, policy
from email.message import EmailMessage
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from aiosmtpd.controller import Controller
from coincurve import PrivateKey

from belfry_onboarding import (
    apps,
    checkin,
    drops,
    followers,
    mail,
    mailforward,
    mailfront,
    whois,
)
from belfry_onboarding.app import create_app
from belfry_onboarding.groups_members import signed
from belfry_onboarding.register import Register

from .conftest import HOOK_TOKEN
from .fakes import FakeRelay

BELFRY = PrivateKey(b"\x01" * 32)
GROUPS = PrivateKey(b"\x03" * 32)
ADA = PrivateKey(b"\x04" * 32)  # an admin of #hello
ADA_PUB = ADA.public_key_xonly.format().hex()
CH = "hello-79f0"
DOMAIN = "belfry.test"
FORWARD_TO = "old@example.net"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"nothing listens on {port}")


class Provider:
    """The belfry's SMTP provider (Lettermint, Resend…): keeps what it is given."""

    def __init__(self):
        self.got: list[SimpleNamespace] = []

    async def handle_DATA(self, server, session, envelope):
        self.got.append(
            SimpleNamespace(
                mail_from=envelope.mail_from,
                rcpts=list(envelope.rcpt_tos),
                data=envelope.original_content or envelope.content,
            )
        )
        return "250 OK"


def gmail_message() -> bytes:
    """What Gmail sends for a mail with a picture pasted in the text and a PDF attached:
    multipart/mixed( multipart/alternative( text/plain, multipart/related( text/html with
    <img src="cid:…">, image/png inline ) ), application/pdf )."""
    msg = EmailMessage()
    msg["From"] = "Marie Dubois <marie@example.org>"
    msg["To"] = f"hello@{DOMAIN}"
    msg["Subject"] = "Renting the hall"
    msg["Date"] = "Sun, 22 Nov 2026 09:00:00 +0000"
    msg["Message-ID"] = "<CAgmail-e2e-1@mail.gmail.com>"
    msg.set_content("Hi,\n\nCan we rent the hall on Saturday? Our poster:\n\n[image: poster.png]\n")
    msg.add_alternative(
        '<div dir="ltr"><p>Hi,</p><p>Can we rent the hall on Saturday? Our poster:</p>'
        '<img src="cid:ii_poster1" alt="poster.png"></div>',
        subtype="html",
    )
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40_000
    msg.get_payload()[1].add_related(
        png, "image", "png", cid="<ii_poster1>", filename="poster.png", disposition="inline"
    )
    msg.add_attachment(b"%PDF-1.4\n" + b"0" * 20_000, "application", "pdf", filename="plan.pdf")
    raw = msg.as_bytes(policy=policy.SMTP)  # CRLF, as on the wire
    # the shape the test is about
    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed.get_content_type() == "multipart/mixed"
    alt = parsed.get_payload()[0]
    assert alt.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in alt.get_payload()] == [
        "text/plain",
        "multipart/related",
    ]
    assert [p.get_content_type() for p in alt.get_payload()[1].get_payload()] == [
        "text/html",
        "image/png",
    ]
    return raw


class MailIn:
    """The real mail-in: mailfront.smtp sessions on mailfront.listen (IPv4 and IPv6), in its own
    event loop, talking HTTP to BACKEND."""

    def __init__(self, port: int):
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

        async def up():
            self.http = httpx.AsyncClient(timeout=60.0)
            handler = mailfront.Handler(self.http)
            self.server = await mailfront.listen(
                self.loop, lambda: mailfront.smtp(handler), port=self.port
            )

        asyncio.run_coroutine_threadsafe(up(), self.loop).result(10)

    def stop(self) -> None:
        async def down():
            self.server.close()
            await self.server.wait_closed()
            await self.http.aclose()

        asyncio.run_coroutine_threadsafe(down(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def world(services, settings, router, tmp_path, monkeypatch):
    # mail-in → onboarding is real HTTP on localhost (respx mocks every other request)
    router.route(host="127.0.0.1").pass_through()
    # the belfry
    settings.register_path = str(tmp_path / "register.sqlite")
    settings.public_url = f"https://pass.{DOMAIN}"
    settings.console_url = f"https://belfry.{DOMAIN}"
    settings.nip05_domain = DOMAIN
    settings.community_name = "Belfry Test"
    settings.belfry_nsec = BELFRY.secret.hex()
    settings.mail_in = True
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    mail._cache.clear()
    mail._counts.clear()
    mailforward._hits.clear()
    monkeypatch.setattr(mailforward, "IN_THREAD", False)
    monkeypatch.setattr(mailforward, "RETRY_DELAYS", (0,))

    # the provider every outgoing mail goes through (the forward and its confirmation)
    provider = Provider()
    pport = free_port()
    ctl = Controller(provider, hostname="127.0.0.1", port=pport, decode_data=False)
    ctl.start()
    settings.smtp_host = "127.0.0.1"
    settings.smtp_port = pport
    settings.smtp_encryption = "none"
    settings.smtp_user = ""

    # the groups relay: #hello, Ada its admin, and Ada's setting "E-mails from anyone"
    groups = FakeRelay()
    now = int(time.time())

    def by(key: PrivateKey, kind: int, tags: list, content: str = "", at: int = now - 60):
        return signed(key, {"kind": kind, "created_at": at, "tags": tags, "content": content})

    groups.events += [
        by(GROUPS, 39000, [["d", CH], ["name", "Hello"]]),
        by(GROUPS, 39001, [["d", CH], ["p", ADA_PUB, "admin"]]),
        by(ADA, 30078, [["d", mail.inbox_d(CH)], ["h", CH]], json.dumps({"on": True})),
    ]
    monkeypatch.setattr(followers, "_relay", lambda svc: (groups, lambda t: signed(GROUPS, t)))
    monkeypatch.setattr(services.keycast, "pubkey_for_name", lambda name: None)

    # Blossom: keeps the files it is given
    blossom: list[tuple[str, str, bytes]] = []

    def upload(svc, files):
        blossom.extend(files)
        return [
            ["imeta", f"url https://blossom.{DOMAIN}/{i}-{len(blossom)}", f"m {f[1]}"]
            for i, f in enumerate(files)
        ]

    monkeypatch.setattr(drops, "upload", upload)

    # who may set the forward (the /group-meta rule): Ada, a member and the channel's admin
    monkeypatch.setattr(whois, "kinds", lambda svc: {ADA_PUB: "member"})
    monkeypatch.setattr(checkin, "_belfry_admins", lambda svc: set())
    monkeypatch.setattr(checkin, "group_state", lambda svc, g: SimpleNamespace(admins={ADA_PUB}))
    monkeypatch.setattr(checkin, "is_archived", lambda svc, g: False)
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {ADA_PUB})

    # Ada forwards #hello to the old mailbox, which confirms (the link in a real mail)
    out = mailforward.api(services, ADA_PUB, {"channel": CH, "address": FORWARD_TO})
    assert out["forward"]["state"] == "pending"
    [confirmation] = provider.got
    assert confirmation.rcpts == [FORWARD_TO]
    text = message_from_bytes(confirmation.data, policy=policy.default)
    token = re.search(
        r"/mail-forward/([A-Za-z0-9_-]+)", text.get_body(("plain",)).get_content()
    ).group(1)
    assert mailforward.confirm(services, token)
    assert mailforward.load(services, CH)["state"] == "on"
    provider.got.clear()

    # the onboarding app, served over HTTP like in Compose
    app = create_app(services)
    app.state.services = services
    aport = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=aport, lifespan="off", log_level="warning")
    )
    web = threading.Thread(target=server.run, daemon=True)
    web.start()
    wait_port(aport)

    # mail-in, pointed at it with the hook token
    (tmp_path / "hook").write_text(HOOK_TOKEN)
    monkeypatch.setenv("HOOK_TOKEN_FILE", str(tmp_path / "hook"))
    monkeypatch.setattr(mailfront, "BACKEND", f"http://127.0.0.1:{aport}")
    monkeypatch.setattr(mailfront, "DOMAIN", DOMAIN)
    mport = free_port()
    mail_in = MailIn(mport)
    mail_in.start()
    try:
        yield SimpleNamespace(
            port=mport, groups=groups, blossom=blossom, provider=provider, svc=services
        )
    finally:
        mail_in.stop()
        server.should_exit = True
        web.join(5)
        ctl.stop()


def send(port: int, raw: bytes, rcpt: str = f"hello@{DOMAIN}"):
    """Like a sending server: EHLO, MAIL FROM, RCPT TO, DATA. The DATA answer."""
    with smtplib.SMTP("127.0.0.1", port, local_hostname="mail-sor-f41.google.com") as s:
        s.ehlo()
        assert s.mail("marie@example.org")[0] == 250
        code, why = s.rcpt(rcpt)
        if code != 250:
            return code, why
        return s.data(raw)


def test_a_gmail_mail_from_outside_lands_in_the_shared_inbox_and_is_forwarded(world):
    raw = gmail_message()
    code, answer = send(world.port, raw)
    assert code == 250, answer

    # the card, in #hello, posted by the groups key
    cards = [e for e in world.groups.events if e["kind"] == 9]
    assert len(cards) == 1
    card = cards[0]
    tags = {t[0]: t[1:] for t in card["tags"]}
    assert ["h", CH] in card["tags"] and tags["client"] == ["belfry-mail"]
    assert card["pubkey"] == GROUPS.public_key_xonly.format().hex()
    assert tags["mail-from"] == ["marie@example.org"] and tags["mail-name"] == ["Marie Dubois"]
    assert tags["subject"] == ["Renting the hall"]
    assert tags["message-id"] == ["<CAgmail-e2e-1@mail.gmail.com>"]
    assert card["content"].startswith("**Renting the hall**")
    assert "Can we rent the hall on Saturday?" in card["content"]
    assert "mail-original" in tags  # the whole e-mail, encrypted on Blossom

    # both files: the pasted picture and the PDF, each on Blossom and on the card
    # three uploads: the picture, the PDF and the whole e-mail (AES-GCM, no name, octet-stream)
    assert sorted(ctype for _, ctype, _ in world.blossom) == [
        "application/octet-stream",
        "application/pdf",
        "image/png",
    ]
    kept = sorted((name, ctype) for name, ctype, _ in world.blossom if name)
    assert kept == [("plan.pdf", "application/pdf"), ("poster.png", "image/png")]
    imeta = [t for t in card["tags"] if t[0] == "imeta"]
    assert sorted(x for t in imeta for x in t[1:] if x.startswith("m ")) == [
        "m application/pdf",
        "m image/png",
    ]
    assert "Not kept" not in card["content"]

    # the forward, through the provider, from bounces@, the mail itself kept
    [copy] = world.provider.got
    assert copy.mail_from == f"bounces@{DOMAIN}" and copy.rcpts == [FORWARD_TO]
    fwd = message_from_bytes(copy.data, policy=policy.default)
    assert fwd["Message-ID"] == "<CAgmail-e2e-1@mail.gmail.com>"
    assert fwd["X-Belfry-Forward"] == DOMAIN and "via hello@" in str(fwd["From"])
    assert fwd["Reply-To"] == "Marie Dubois <marie@example.org>"
    names = sorted(p.get_filename() for p in fwd.walk() if p.get_filename())
    assert names == ["plan.pdf", "poster.png"]


def test_with_the_inbox_off_the_mail_is_refused_at_data_and_the_log_says_why(world, caplog):
    # Ada turns "E-mails from anyone" off (a newer setting)
    world.groups.events.append(
        signed(
            ADA,
            {
                "kind": 30078,
                "created_at": int(time.time()),
                "tags": [["d", mail.inbox_d(CH)], ["h", CH]],
                "content": json.dumps({"on": False}),
            },
        )
    )
    mail._cache.clear()
    caplog.set_level(logging.INFO, logger="belfry_onboarding.mail")
    code, answer = send(world.port, gmail_message())
    assert code == 550
    assert b"Only members" in answer
    assert not [e for e in world.groups.events if e["kind"] == 9]
    assert not world.provider.got  # nothing posted: nothing forwarded
    lines = [r.getMessage() for r in caplog.records if "refused for" in r.getMessage()]
    assert lines and "550" in lines[0] and "channel" in lines[0]
    assert all("marie@example.org" not in r.getMessage() for r in caplog.records)


def _has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 on this machine")
def test_a_sender_over_ipv6_gets_the_same_answers(world):
    """mail-in refused every IPv6 sender once (it listened on IPv4 only, 2026-11-22)."""
    with smtplib.SMTP("::1", world.port, local_hostname="mail-sor-f41.google.com") as s:
        s.ehlo()
        assert s.mail("marie@example.org")[0] == 250
        assert s.rcpt(f"hello@{DOMAIN}")[0] == 250
        assert s.rcpt(f"nobody@{DOMAIN}")[0] == 550
        assert s.data(gmail_message())[0] == 250
    assert len([e for e in world.groups.events if e["kind"] == 9]) == 1


@pytest.fixture(scope="module")
def dkim_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    import base64

    return pem, b"v=DKIM1; k=rsa; p=" + base64.b64encode(der)


@pytest.mark.parametrize(
    "d,result,auth",
    [
        ("example.org", "ok", "pass"),
        # a Google Workspace domain without its own DKIM set up: Google signs for it as
        # <domain>.<date>.gappssmtp.com, valid but not aligned with From (DMARC says no)
        ("example-org.20230601.gappssmtp.com", "ok, not aligned with From", "none"),
    ],
)
def test_a_dkim_signed_mail_keeps_its_signature_through_mail_in(
    world, monkeypatch, caplog, dkim_key, d, result, auth
):
    """The bytes mail-in hands over are the bytes that were signed (CRLF, dots, headers)."""
    import dkim

    pem, txt = dkim_key
    raw = gmail_message()
    # a line starting with a dot: SMTP stuffs it, mail-in must unstuff it
    raw = raw.replace(
        b"Can we rent the hall on Saturday?", b"Can we rent the hall\r\n.on Saturday?", 1
    )
    sig = dkim.sign(
        raw,
        b"sel1",
        d.encode(),
        pem,
        include_headers=[b"From", b"To", b"Subject", b"Date", b"Message-ID", b"MIME-Version"],
    )
    signed_raw = sig + raw
    asked = []

    def dns(name, timeout=5):
        asked.append(name)
        return txt if name == f"sel1._domainkey.{d}.".encode() else None

    monkeypatch.setattr(mail, "DNSFUNC", dns)
    caplog.set_level(logging.INFO, logger="belfry_onboarding.mail")
    code, answer = send(world.port, signed_raw)
    assert code == 250, answer
    [card] = [e for e in world.groups.events if e["kind"] == 9]
    assert ["belfry-auth", auth] in card["tags"]
    lines = [r.getMessage() for r in caplog.records if "dkim d=" in r.getMessage()]
    assert lines == [f"mail: dkim d={d} s=sel1: {result}"]
    assert all("marie" not in r.getMessage() for r in caplog.records)


def test_a_broken_dkim_signature_says_why(world, monkeypatch, caplog, dkim_key):
    import dkim

    pem, txt = dkim_key
    raw = gmail_message()
    sig = dkim.sign(raw, b"sel1", b"example.org", pem, include_headers=[b"From", b"Subject"])
    tampered = sig + raw.replace(b"Saturday", b"Sunday", 1)
    monkeypatch.setattr(mail, "DNSFUNC", lambda name, timeout=5: txt)
    caplog.set_level(logging.INFO, logger="belfry_onboarding.mail")
    assert send(world.port, tampered)[0] == 250
    [line] = [r.getMessage() for r in caplog.records if "dkim d=" in r.getMessage()]
    assert line == "mail: dkim d=example.org s=sel1: fail (body hash mismatch)"


def test_a_dkim_key_missing_from_dns_says_so(monkeypatch, caplog, dkim_key):
    import dkim

    pem, _ = dkim_key
    raw = gmail_message()
    signed_raw = dkim.sign(raw, b"sel1", b"example.org", pem, include_headers=[b"From"]) + raw
    monkeypatch.setattr(mail, "DNSFUNC", lambda name, timeout=5: None)
    caplog.set_level(logging.INFO, logger="belfry_onboarding.mail")
    got = mail.authenticate(signed_raw, "127.0.0.1", "h", "marie@example.org", "marie@example.org")
    assert got["dkim"] == "" and not got["aligned"]
    [line] = [r.getMessage() for r in caplog.records if "dkim d=" in r.getMessage()]
    assert line.startswith("mail: dkim d=example.org s=sel1: fail (") and "key" in line
