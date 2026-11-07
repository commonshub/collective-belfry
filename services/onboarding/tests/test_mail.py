"""E-mail as messages (mail.py): name@ and channel@ receive mail, members answer by e-mail."""

from __future__ import annotations

import base64
import json
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, community, drops, mail
from belfry_onboarding.app import create_app
from belfry_onboarding.nip46check import conversation_key, nip44_decrypt

from .conftest import HOOK_TOKEN

BELFRY = PrivateKey(b"\x01" * 32)
ANA = PrivateKey(b"\x02" * 32)
ANA_PUB = ANA.public_key_xonly.format().hex()
BELFRY_PUB = BELFRY.public_key_xonly.format().hex()


def message(
    subject="Hello",
    body="Hi Ana,\n\nSee you there.\n\nOn Mon, Bob wrote:\n> old",
    frm="Bob <bob@example.org>",
    to="ana@belfry.test",
    extra="",
):
    return (
        f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\n"
        "Date: Sat, 07 Nov 2026 10:00:00 +0000\r\n"
        f"Message-ID: <m1@example.org>\r\n{extra}\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def m(services, monkeypatch):
    s = services.settings
    s.nip05_domain = "belfry.test"
    s.belfry_nsec = BELFRY.secret.hex()
    s.community_relay_public_url = "wss://relay.belfry.test"
    services.relay.require_auth = False  # gift wraps: AUTH as the belfry, signed by one-time keys
    mail._cache.clear()
    mail._counts.clear()
    names = {"ana": ANA_PUB}
    monkeypatch.setattr(services.keycast, "pubkey_for_name", lambda n: names.get(n))
    monkeypatch.setattr(mail, "active_pubkey", lambda svc, p: p == ANA_PUB)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, c: c in {"garden", "ana"})
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {ANA_PUB})
    return services


def unwrap_for(secret: bytes, wrap: dict) -> dict:
    return mail.unwrap(secret, wrap)[1]


def test_who_an_address_is(m):
    assert mail.resolve(m, "Ana@belfry.test") == {
        "kind": "member",
        "pubkeys": [ANA_PUB],
        "name": "ana",
    }
    assert (
        mail.resolve(m, "ana+lists@belfry.test")["kind"] == "member"
    )  # a member wins over a channel
    assert mail.resolve(m, "garden@belfry.test") == {"kind": "channel", "channel": "garden"}
    assert mail.resolve(m, "postmaster@belfry.test") == {"kind": "admins", "pubkeys": [ANA_PUB]}
    assert mail.resolve(m, "nobody@belfry.test") is None
    assert mail.resolve(m, "ana@elsewhere.org") is None
    assert mail.resolve(m, "../x@belfry.test") is None


def test_mail_to_a_member_is_a_gift_wrapped_kind_1301(m):
    code, _ = mail.deliver(
        m, message(), "203.0.113.5", "mx.example.org", "bob@example.org", ["ana@belfry.test"]
    )
    assert code == 250
    [wrap] = [e for e in m.relay.events if e["kind"] == 1059]
    assert ["p", ANA_PUB] in wrap["tags"] and wrap["pubkey"] != BELFRY_PUB  # one-time key
    rumor = unwrap_for(ANA.secret, wrap)
    assert rumor["kind"] == 1301 and rumor["pubkey"] == BELFRY_PUB
    assert mail.tag(rumor, "mail-from") == "bob@example.org"
    assert mail.tag(rumor, "subject") == "Hello"
    assert mail.tag(rumor, "belfry-auth") == "none"  # no DKIM, no SPF here
    assert "Subject: Hello" in rumor["content"] and mail.tag(rumor, "email-id")


def test_a_large_mail_goes_to_blossom_encrypted(m, monkeypatch):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    stored = {}

    def upload(svc, files):
        stored["blob"] = files[0][2]
        return [["imeta", "url https://blossom.belfry.test/abc", "m application/octet-stream"]]

    monkeypatch.setattr(drops, "upload", upload)
    raw = message(body="x" * 70_000)
    assert (
        mail.deliver(m, raw, "203.0.113.5", "h", "bob@example.org", ["ana@belfry.test"])[0] == 250
    )
    rumor = unwrap_for(ANA.secret, [e for e in m.relay.events if e["kind"] == 1059][0])
    assert rumor["content"] == "" and mail.tag(rumor, "encryption-algorithm") == "aes-gcm"
    key = base64.b64decode(mail.tag(rumor, "decryption-key"))
    nonce = base64.b64decode(mail.tag(rumor, "decryption-nonce"))
    assert AESGCM(key).decrypt(nonce, stored["blob"], None) == raw
    assert mail.tag(rumor, "url") == "https://blossom.belfry.test/abc"


def test_channel_mail_only_from_members_who_prove_it(m, monkeypatch):
    posted = []
    monkeypatch.setattr(
        drops,
        "post",
        lambda svc, ch, text, imeta, partner, client="": posted.append((ch, text, partner, client)),
    )
    monkeypatch.setattr(community, "member_signer", lambda svc, p: ("b" * 64, None))
    monkeypatch.setattr(
        apps,
        "member_by_email",
        lambda svc, e: {"partner": 7, "name": "Ana", "email": e} if e == "ana@x.org" else None,
    )
    args = ("203.0.113.5", "h", "", ["garden@belfry.test"])
    code, why = mail.deliver(m, message(frm="Bob <bob@example.org>"), *args)
    assert code == 550 and "Only members" in why
    code, why = mail.deliver(m, message(frm="Ana <ana@x.org>"), *args)
    assert code == 550 and "DKIM or SPF" in why and not posted
    monkeypatch.setattr(
        mail, "authenticate", lambda *a: {"aligned": True, "dkim": "x.org", "spf": "none"}
    )
    code, _ = mail.deliver(m, message(frm="Ana <ana@x.org>", subject="Seeds"), *args)
    assert code == 250
    [(ch, text, partner, client)] = posted
    assert ch == "garden" and partner == 7 and client == "belfry-mail"
    assert text.startswith("**Seeds**") and "See you there." in text and "old" not in text


def test_dkim_aligned_with_from_marks_mail_verified():
    import dkim
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    pub = k.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    record = b"v=DKIM1; k=rsa; p=" + base64.b64encode(pub)
    raw = message(frm="Bob <bob@example.org>")
    signed = (
        dkim.sign(raw, b"s1", b"example.org", pem, include_headers=[b"from", b"to", b"subject"])
        + raw
    )
    mail.DNSFUNC = lambda name, timeout=5: record if name == b"s1._domainkey.example.org." else b""
    try:
        assert mail.authenticate(signed, "10.0.0.1", "h", "", "bob@example.org")["aligned"]
        assert mail.authenticate(signed, "10.0.0.1", "h", "", "bob@other.org")["aligned"] is False
        tampered = signed.replace(b"See you there", b"Pay me now")
        assert (
            mail.authenticate(tampered, "10.0.0.1", "h", "", "bob@example.org")["aligned"] is False
        )
    finally:
        mail.DNSFUNC = None


def outgoing_wrap(frm="ana@belfry.test", header_from="Ana <ana@belfry.test>", email_id="e1"):
    raw = (
        f"From: {header_from}\r\nTo: bob@example.org\r\nBcc: spy@example.org\r\n"
        "Subject: Re: Hello\r\nIn-Reply-To: <m1@example.org>\r\n\r\nThanks!\r\n"
    )
    rumor = {
        "kind": 1301,
        "pubkey": ANA_PUB,
        "created_at": int(time.time()),
        "content": raw,
        "tags": [["email-id", email_id], ["mail-from", frm], ["rcpt-to", "bob@example.org"]],
    }
    return mail.gift_wrap(ANA.secret, rumor, BELFRY_PUB)


def dsn_of(m, email_id):
    [e] = [e for e in m.relay.events if e["kind"] == 7679 and ["r", email_id] in e["tags"]]
    assert e["pubkey"] == BELFRY_PUB and not any(t[0] == "p" for t in e["tags"])
    temp = mail.tag(e, "ephemeral-pubkey")
    return json.loads(
        nip44_decrypt(conversation_key(ANA.secret, bytes.fromhex(temp)), e["content"])
    )


def test_a_member_answers_by_e_mail_and_gets_a_delivery_report(m, monkeypatch):
    sent = []
    monkeypatch.setattr(
        mail,
        "smtp_send",
        lambda svc, frm, rcpts, data: sent.append((frm, rcpts, data)) or (250, "OK"),
    )
    mail.handle_wrap(m, outgoing_wrap())
    [(frm, rcpts, data)] = sent
    assert frm == "ana@belfry.test" and rcpts == ["bob@example.org"]
    assert (
        b"Bcc" not in data and b"In-Reply-To: <m1@example.org>" in data and b"Message-ID:" in data
    )
    assert dsn_of(m, "e1")["status"] == "delivered"
    mail.handle_wrap(m, outgoing_wrap())  # the relay sends it again after a restart
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("frm", "header", "why"),
    [
        ("bob@belfry.test", "Ana <bob@belfry.test>", "not yours"),
        ("ana@elsewhere.org", "Ana <ana@elsewhere.org>", "own address"),
        ("ana@belfry.test", "Ana <ceo@bank.example>", "From header"),
    ],
)
def test_no_one_sends_as_someone_else(m, monkeypatch, frm, header, why):
    monkeypatch.setattr(mail, "smtp_send", lambda *a: pytest.fail("must not send"))
    mail.handle_wrap(m, outgoing_wrap(frm, header, email_id="x" + why[:3]))
    report = dsn_of(m, "x" + why[:3])
    assert report["status"] == "failed" and why in report["details"]


def test_the_mail_in_service_asks_with_the_hook_token(m):
    with TestClient(create_app(m)) as c:
        assert c.post("/internal/mail/rcpt", json={"address": "ana@belfry.test"}).status_code == 401
        auth = {"Authorization": f"Bearer {HOOK_TOKEN}"}
        assert (
            c.post("/internal/mail/rcpt", json={"address": "ana@belfry.test"}, headers=auth).json()[
                "code"
            ]
            == 250
        )
        assert (
            c.post("/internal/mail/rcpt", json={"address": "zed@belfry.test"}, headers=auth).json()[
                "code"
            ]
            == 550
        )
        r = c.post(
            "/internal/mail/data",
            content=message(),
            headers={
                **auth,
                "X-Peer": "203.0.113.5",
                "X-Mail-From": "bob@example.org",
                "X-Rcpt": "ana@belfry.test",
            },
        )
        assert r.json()["code"] == 250
        names = c.get("/.well-known/nostr.json", params={"name": "_smtp"}).json()
        assert names["names"] == {"_smtp": BELFRY_PUB}
        assert names["relays"][BELFRY_PUB] == ["wss://relay.belfry.test"]


def test_reading_a_reply_keeps_only_the_new_text():
    html = message(
        body="<p>Hello<br>there</p><blockquote>x</blockquote>", extra="Content-Type: text/html\r\n"
    )
    assert mail.text_of(mail.parse(html)).startswith("Hello\nthere")
    assert mail.text_of(mail.parse(message())) == "Hi Ana,\n\nSee you there."


def test_the_smtp_front_asks_the_belfry_for_every_address_and_message(tmp_path, monkeypatch):
    import smtplib

    import httpx
    from aiosmtpd.controller import Controller

    from belfry_onboarding import mailfront

    (tmp_path / "t").write_text("tok")
    monkeypatch.setenv("HOOK_TOKEN_FILE", str(tmp_path / "t"))
    seen = []

    def backend(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        if request.url.path.endswith("/rcpt"):
            ok = json.loads(request.content)["address"] == "ana@belfry.test"
            return httpx.Response(200, json={"code": 250 if ok else 550, "message": "5.1.1 No one"})
        seen.append((request.headers["x-rcpt"], request.headers["x-mail-from"], request.content))
        return httpx.Response(200, json={"code": 250, "message": "2.0.0 Delivered"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ctl = Controller(mailfront.Handler(http), hostname="127.0.0.1", port=port, decode_data=False)
    ctl.start()
    try:
        with smtplib.SMTP("127.0.0.1", port) as s:
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                s.sendmail("bob@example.org", ["zed@belfry.test"], message())
            assert s.sendmail(
                "bob@example.org", ["ana@belfry.test", "zed@belfry.test"], message()
            ) == {"zed@belfry.test": (550, b"5.1.1 No one")}
    finally:
        ctl.stop()
    [(rcpt, frm, raw)] = seen
    assert rcpt == "ana@belfry.test" and frm == "bob@example.org" and b"Subject: Hello" in raw
