"""Forwarding a channel's e-mails to an outside address (mailforward.py, docs/mail.md)."""

from __future__ import annotations

import json
import re
import smtplib
import time
from types import SimpleNamespace

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, checkin, community, drops, mail, mailer, mailforward, whois
from belfry_onboarding.app import create_app
from belfry_onboarding.holder_invites import ApiError
from belfry_onboarding.register import Register

from .test_holder_invites import nip98

ADA = "a" * 64  # an admin of #hello
BEA = "b" * 64  # a member, not an admin
GUS = "c" * 64  # a guest who is an admin of the channel (never allowed)
CH = "hello-79f0"
HOST = {"Host": "belfry.belfry.test", "Content-Type": "application/json"}


def message(frm="Marie Dubois <marie@example.org>", extra="", to="hello@belfry.test"):
    return (
        f"From: {frm}\r\nTo: {to}\r\nCc: team@example.net\r\nSubject: Renting the hall\r\n"
        "Date: Sat, 07 Nov 2026 10:00:00 +0000\r\nMessage-ID: <m1@example.org>\r\n"
        "In-Reply-To: <m0@example.org>\r\nReferences: <a@example.org>\r\n <m0@example.org>\r\n"
        "DKIM-Signature: v=1; a=rsa-sha256; d=example.org; s=s1;\r\n b=abc\r\n"
        "ARC-Seal: i=1; a=rsa-sha256; cv=none\r\nReturn-Path: <marie@example.org>\r\n"
        "Sender: list@example.org\r\n"
        f"{extra}"
        'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="XX"\r\n\r\n'
        "--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHi,\r\n\r\nCan we rent it?\r\n"
        "--XX\r\nContent-Type: application/pdf; name=plan.pdf\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--XX--\r\n"
    ).encode()


@pytest.fixture
def fw(services, tmp_path, settings, monkeypatch):
    settings.register_path = str(tmp_path / "register.sqlite")
    settings.public_url = "https://pass.belfry.test"
    settings.console_url = "https://belfry.belfry.test"
    settings.nip05_domain = "belfry.test"
    settings.smtp_host = "smtp.example.net"
    settings.community_name = "Belfry Test"
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    mail._cache.clear()
    mail._counts.clear()
    mailforward._hits.clear()
    monkeypatch.setattr(mailforward, "IN_THREAD", False)
    monkeypatch.setattr(mailforward, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(mail, "channel_exists", lambda svc, c: c == CH)
    monkeypatch.setattr(mail, "_top_channels", lambda svc: [(CH, "Hello")])
    monkeypatch.setattr(mail, "_made_by_admin", lambda svc, c: True)
    monkeypatch.setattr(whois, "kinds", lambda svc: {ADA: "member", BEA: "member", GUS: "guest"})
    monkeypatch.setattr(checkin, "_belfry_admins", lambda svc: set())
    monkeypatch.setattr(checkin, "group_state", lambda svc, g: SimpleNamespace(admins={ADA, GUS}))
    monkeypatch.setattr(checkin, "is_archived", lambda svc, g: False)
    mails: list = []
    monkeypatch.setattr(mailer.Mailer, "_smtp", lambda self, msg, rcpts: mails.append(msg))
    sent: list = []
    services.sent = sent  # type: ignore[attr-defined]
    services.mails = mails  # type: ignore[attr-defined]
    monkeypatch.setattr(
        mail,
        "smtp_send",
        lambda svc, frm, rcpts, data: sent.append((frm, rcpts, data)) or (250, "OK"),
    )
    return services


def token_of(msg) -> str:
    text = msg.get_body(("plain",)).get_content()
    return re.search(r"/mail-forward/([A-Za-z0-9_-]+)", text).group(1)


def turn_on(fw, address="old@example.net") -> str:
    out = mailforward.api(fw, ADA, {"channel": CH, "address": address})
    assert out["forward"]["state"] == "pending"
    tok = token_of(fw.mails[-1])
    assert mailforward.confirm(fw, tok)
    return tok


def code_of(fn) -> tuple[int, str]:
    with pytest.raises(ApiError) as exc:
        fn()
    return exc.value.status, exc.value.error


def test_who_may_set_it(fw):
    assert code_of(lambda: mailforward.api(fw, BEA, {"channel": CH})) == (403, "not_allowed")
    # a guest is never allowed, even as the channel's admin; nor someone without a pass
    assert code_of(lambda: mailforward.api(fw, GUS, {"channel": CH})) == (403, "not_allowed")
    assert code_of(lambda: mailforward.api(fw, "d" * 64, {"channel": CH}))[1] == "not_allowed"
    assert code_of(lambda: mailforward.api(fw, ADA, {"channel": "nope"})) == (
        404,
        "unknown_channel",
    )
    assert mailforward.api(fw, ADA, {"channel": CH}) == {"forward": None}


def test_belfry_admins_may_set_it(fw, monkeypatch):
    monkeypatch.setattr(checkin, "_belfry_admins", lambda svc: {BEA})
    assert mailforward.api(fw, BEA, {"channel": CH}) == {"forward": None}


@pytest.mark.parametrize(
    "address,code",
    [
        ("not an address", (422, "invalid")),
        ("x@belfry.test", (422, "own_domain")),
        ("x@chat.belfry.test", (422, "own_domain")),
        ("X@Belfry.Test", (422, "own_domain")),
    ],
)
def test_bad_addresses(fw, address, code):
    assert code_of(lambda: mailforward.api(fw, ADA, {"channel": CH, "address": address})) == code
    assert not fw.mails


def test_archived_odoo_no_mail_and_rate(fw, monkeypatch):
    monkeypatch.setattr(checkin, "is_archived", lambda svc, g: True)
    body = {"channel": CH, "address": "old@example.net"}
    assert code_of(lambda: mailforward.api(fw, ADA, body)) == (409, "archived")
    monkeypatch.setattr(checkin, "is_archived", lambda svc, g: False)
    fw.settings.smtp_host = ""
    assert code_of(lambda: mailforward.api(fw, ADA, body)) == (503, "no_mail")
    fw.settings.smtp_host = "smtp.example.net"
    mailforward.api(fw, ADA, body)
    mailforward.api(fw, ADA, {"channel": CH, "resend": True})
    mailforward.api(fw, ADA, {"channel": CH, "resend": True})
    assert len(fw.mails) == 3
    assert code_of(lambda: mailforward.api(fw, ADA, {"channel": CH, "resend": True})) == (
        429,
        "too_many",
    )
    real = fw.odoo
    fw.odoo = object()
    assert code_of(lambda: mailforward.api(fw, ADA, {"channel": CH}))[1] == "unsupported"
    fw.odoo = real


def test_the_confirmation_mail_and_page(fw):
    out = mailforward.api(fw, ADA, {"channel": CH, "address": "old@example.net"})
    f = out["forward"]
    assert f["address"] == "old@example.net" and f["state"] == "pending" and f["set_by"] == ADA
    assert f["same_domain_skipped"] is True
    [msg] = fw.mails
    assert msg["To"] == "old@example.net"
    text = msg.get_body(("plain",)).get_content()
    assert "Belfry Test's channel “Hello” wants to forward" in text
    tok = token_of(msg)
    assert "https://belfry.belfry.test/mail-forward/" + tok in text
    with TestClient(create_app(fw)) as c:
        page = c.get(f"/mail-forward/{tok}", headers=HOST)
        assert page.status_code == 200 and "Confirm forwarding" in page.text
        assert "old@example.net" in page.text and "Hello" in page.text
        # GET never confirms (mail scanners open links)
        assert mailforward.load(fw, CH)["state"] == "pending"
        assert c.get("/mail-forward/nonsense<b>x</b>", headers=HOST).status_code == 404
        assert "nonsense" not in c.get("/mail-forward/nonsense", headers=HOST).text
        done = c.post(f"/mail-forward/{tok}", headers=HOST)
        assert done.status_code == 200 and "Done" in done.text
        assert mailforward.load(fw, CH)["state"] == "on"
        # single use
        assert c.post(f"/mail-forward/{tok}", headers=HOST).status_code == 404
    out = mailforward.api(fw, ADA, {"channel": CH})
    assert out["forward"]["state"] == "on"
    # changing the address resets to pending, off removes it
    out = mailforward.api(fw, ADA, {"channel": CH, "address": "new@gmail.com"})
    assert out["forward"]["state"] == "pending" and out["forward"]["same_domain_skipped"] is False
    assert mailforward.api(fw, ADA, {"channel": CH, "address": None}) == {"forward": None}
    assert mailforward.confirm(fw, token_of(fw.mails[-1])) is None


def test_an_expired_token_does_nothing(fw):
    mailforward.api(fw, ADA, {"channel": CH, "address": "old@example.net"})
    rec = mailforward.load(fw, CH)
    rec["token_exp"] = int(time.time()) - 1
    mailforward._save(fw, CH, rec)
    assert mailforward.confirm(fw, token_of(fw.mails[-1])) is None


def test_the_api_over_http(fw):
    key = PrivateKey()  # signed, but holds no pass here
    with TestClient(create_app(fw)) as c:
        body = json.dumps({"channel": CH}).encode()
        r = c.post(
            "/api/v1/mail-forward",
            content=body,
            headers={
                **HOST,
                "Authorization": nip98(
                    key, "https://belfry.belfry.test/api/v1/mail-forward", "POST", body
                ),
            },
        )
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"


def test_the_forward_keeps_the_mail_and_rewrites_from(fw):
    raw = message(extra="Reply-To: Marie Office <office@example.org>\r\n")
    data = mailforward.build(fw, CH, raw)
    head, _, body = data.partition(b"\r\n\r\n")
    assert body == raw.partition(b"\r\n\r\n")[2]  # body and attachments byte for byte
    lines = head.split(b"\r\n")
    for name in (b"Message-ID", b"In-Reply-To", b"Subject", b"Date", b"To", b"Cc"):
        [orig] = [x for x in raw.split(b"\r\n") if x.startswith(name + b":")]
        assert orig in lines
    assert b"References: <a@example.org>\r\n <m0@example.org>" in head
    assert b'From: "Marie Dubois via hello@belfry.test" <hello@belfry.test>' in lines
    assert b"Reply-To: Marie Dubois <marie@example.org>" in lines
    assert b"X-Original-From: Marie Dubois <marie@example.org>" in lines
    assert b"X-Original-Reply-To: Marie Office <office@example.org>" in lines
    assert b"X-Forwarded-For-Channel: hello@belfry.test" in lines
    assert b"X-Belfry-Forward: belfry.test" in lines
    for gone in (b"DKIM-Signature", b"ARC-Seal", b"Return-Path", b"Sender", b" b=abc"):
        assert not any(x.startswith(gone) for x in lines)
    assert sum(1 for x in lines if x.lower().startswith(b"from:")) == 1
    assert sum(1 for x in lines if x.lower().startswith(b"reply-to:")) == 1
    # no display name: the address stands in
    data = mailforward.build(fw, CH, message(frm="marie@example.org"))
    assert b'From: "marie@example.org via hello@belfry.test" <hello@belfry.test>' in data


def test_a_non_ascii_name_is_encoded(fw):
    data = mailforward.build(fw, CH, message(frm="=?utf-8?q?Ren=C3=A9e?= <renee@example.org>"))
    frm = next(x for x in data.split(b"\r\n") if x.startswith(b"From:"))
    assert frm.endswith(b"<hello@belfry.test>") and b"=?utf-8?" in frm
    assert data.decode("ascii")  # still ASCII headers


@pytest.fixture
def routes(fw, monkeypatch):
    """#hello takes mail three ways; what gets posted is kept."""
    posted: list = []
    monkeypatch.setattr(
        drops,
        "post",
        lambda svc, ch, text, imeta, partner, client="", kind=9: posted.append((ch, kind)),
    )
    monkeypatch.setattr(drops, "upload", lambda svc, files: [])
    monkeypatch.setattr(mail, "store_large", lambda svc, raw: [])
    monkeypatch.setattr(mail, "_original", lambda svc, raw: [])
    monkeypatch.setattr(community, "member_signer", lambda svc, p: ("b" * 64, None))
    monkeypatch.setattr(fw.keycast, "pubkey_for_name", lambda n: None)
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {ADA})
    return posted


ARGS = ("203.0.113.5", "h", "", ["hello@belfry.test"])


def test_pending_does_not_forward(fw, routes, monkeypatch):
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: True)
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    mailforward.api(fw, ADA, {"channel": CH, "address": "old@example.net"})
    assert mail.deliver(fw, message(), *ARGS)[0] == 250
    assert routes == [(CH, 9)] and not fw.sent


def test_all_three_routes_forward_once(fw, routes, monkeypatch):
    turn_on(fw)
    # 1. a shared-inbox card from an outsider, addressed to the alias and the id at once
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: True)
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    two = ("203.0.113.5", "h", "", ["hello@belfry.test", f"{CH}@belfry.test"])
    assert mail.deliver(fw, message(), *two)[0] == 250
    assert routes == [(CH, 9)] and len(fw.sent) == 1
    frm, rcpts, data = fw.sent[0]
    assert frm == "bounces@belfry.test" and rcpts == ["old@example.net"]
    assert b"Message-ID: <m1@example.org>" in data
    # 2. a member's mail, posted as a message
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: False)
    monkeypatch.setattr(
        apps, "member_by_email", lambda svc, e: {"partner": 7, "name": "Ana", "email": e}
    )
    monkeypatch.setattr(mail, "authenticate", lambda *a: {"aligned": True, "dkim": "x.org"})
    monkeypatch.setattr(mail, "thread_of", lambda svc, ch, refs: None)
    assert mail.deliver(fw, message(frm="Ana <ana@x.org>"), *ARGS)[0] == 250
    assert routes[-1] == (CH, 9) and len(fw.sent) == 2
    # 3. a member's mail answering something in the channel: a thread reply
    monkeypatch.setattr(
        mail,
        "thread_of",
        lambda svc, ch, refs: {"id": "e" * 64, "pubkey": "f" * 64, "kind": 9, "tags": []},
    )
    assert mail.deliver(fw, message(frm="Ana <ana@x.org>"), *ARGS)[0] == 250
    assert routes[-1] == (CH, 1111) and len(fw.sent) == 3


def test_refused_mail_is_not_forwarded(fw, routes, monkeypatch):
    turn_on(fw)
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: False)
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    assert mail.deliver(fw, message(), *ARGS)[0] == 550
    assert not fw.sent


@pytest.mark.parametrize(
    "frm,extra,forwarded",
    [
        ("Old <old@example.net>", "", False),  # the forward address itself
        ("Colleague <x@example.net>", "", False),  # its domain (not a public provider)
        ("Hello <hello@belfry.test>", "", False),  # this belfry's own domain
        ("Ana <ana@chat.belfry.test>", "", False),  # one of its hosts
        ("Marie <marie@example.org>", "X-Belfry-Forward: belfry.test\r\n", False),
        ("Marie <marie@example.org>", "X-Belfry-Forward: other.test\r\n", True),
        ("Marie <marie@example.org>", "", True),
    ],
)
def test_loop_guards(fw, routes, monkeypatch, frm, extra, forwarded):
    turn_on(fw)
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: True)
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    assert mail.deliver(fw, message(frm=frm, extra=extra), *ARGS)[0] == 250
    assert routes == [(CH, 9)]  # always posted
    assert bool(fw.sent) is forwarded


def test_a_public_provider_forwards_mail_from_others_there(fw, routes, monkeypatch):
    turn_on(fw, "old@gmail.com")
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: True)
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    assert mail.deliver(fw, message(frm="Someone <someone@gmail.com>"), *ARGS)[0] == 250
    assert len(fw.sent) == 1
    assert mail.deliver(fw, message(frm="Old <OLD@gmail.com>"), *ARGS)[0] == 250
    assert len(fw.sent) == 1


def test_the_hourly_cap(fw, routes, monkeypatch):
    turn_on(fw)
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: True)
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    monkeypatch.setattr(mailforward, "FORWARDS_PER_HOUR", 2)
    monkeypatch.setattr(mail, "IN_PER_10_MIN", 100)
    for _ in range(3):
        assert mail.deliver(fw, message(), *ARGS)[0] == 250
    assert len(routes) == 3 and len(fw.sent) == 2


def test_posting_is_unaffected_when_smtp_fails(fw, routes, monkeypatch):
    turn_on(fw)
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: True)
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    tries = []

    def down(svc, frm, rcpts, data):
        tries.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr(mail, "smtp_send", down)
    assert mail.deliver(fw, message(), *ARGS)[0] == 250
    assert routes == [(CH, 9)] and len(tries) == 3  # retried, then given up

    def too_big(svc, frm, rcpts, data):
        tries.append(1)
        raise smtplib.SMTPResponseException(552, b"message size exceeds limit")

    tries.clear()
    monkeypatch.setattr(mail, "smtp_send", too_big)
    assert mail.deliver(fw, message(), *ARGS)[0] == 250
    assert len(tries) == 1  # refused for good: no retry


def test_bounces_are_dropped(fw, routes, monkeypatch):
    turn_on(fw)
    assert mail.resolve(fw, "bounces@belfry.test") == {"kind": "bounces"}
    args = ("203.0.113.5", "h", "", ["bounces@belfry.test"])
    assert (
        mail.deliver(fw, message(frm="MAILER-DAEMON <mailer-daemon@example.net>"), *args)[0] == 250
    )
    assert not routes and not fw.sent
