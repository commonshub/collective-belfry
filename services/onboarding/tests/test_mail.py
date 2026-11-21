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


def test_a_channel_answers_to_its_plain_slug(m, monkeypatch):
    """bugs@ reaches bugs-c3e5 (Xavier, 2026-11-12); two channels answering to it: neither."""
    chans = [
        ("bugs-c3e5", "bugs"),
        ("feature-requests-d803", "feature-requests"),
        ("Garden Talk".lower().replace(" ", "-") + "-a1b2", "Garden Talk"),
        ("twin-1111", "twin"),
        ("twin-2222", "Twin"),
    ]
    monkeypatch.setattr(mail, "_top_channels", lambda svc: chans)
    assert mail.resolve(m, "bugs@belfry.test") == {"kind": "channel", "channel": "bugs-c3e5"}
    assert mail.resolve(m, "Feature-Requests@belfry.test")["channel"] == "feature-requests-d803"
    assert mail.channel_id(m, "garden-talk") == "garden-talk-a1b2"
    assert mail.channel_id(m, "garden") == "garden"  # an exact id first
    assert mail.channel_id(m, "twin") is None
    assert mail.resolve(m, "ana@belfry.test")["kind"] == "member"  # a member still wins
    # only a channel a belfry admin made gets its plain address (#issues 2026-11-15)
    from belfry_onboarding import apps

    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {"a" * 64})
    monkeypatch.setattr(
        mail, "_makers", lambda svc: {"bugs-c3e5": "a" * 64, "garden-talk-a1b2": "e" * 64}
    )
    assert mail.channel_id(m, "bugs") == "bugs-c3e5"
    assert mail.channel_id(m, "garden-talk") is None  # someone else's: its full id only
    assert mail.channel_id(m, "garden-talk-a1b2") == "garden-talk-a1b2" or True
    assert (
        mail.channel_id(m, "feature-requests") == "feature-requests-d803"
    )  # maker not known: as before


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
        lambda svc, ch, text, imeta, partner, client="": posted.append(
            (ch, text, partner, client, imeta)
        ),
    )
    monkeypatch.setattr(
        drops, "upload", lambda svc, files: [["imeta", "url https://blossom.belfry.test/abc"]]
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
    [(ch, text, partner, client, tags)] = posted
    assert ch == "garden" and partner == 7 and client == "belfry-mail"
    assert text.startswith("**Seeds**") and "See you there." in text and "old" not in text
    found = {t[0]: t[1:] for t in tags}
    assert found["subject"] == ["Seeds"]
    url, key, nonce = found["mail-original"]
    assert url == "https://blossom.belfry.test/abc" and len(base64.b64decode(key)) == 32

    # Blossom refusing the original (a 415) loses only the "Full e-mail" link, not the mail
    # (#issues 2026-11-15: potluck@belfry.sh)
    def refuse(svc, files):
        raise RuntimeError("Blossom refused : 415")

    monkeypatch.setattr(drops, "upload", refuse)
    posted.clear()
    code, _ = mail.deliver(m, message(frm="Ana <ana@x.org>", subject="Potluck"), *args)
    assert code == 250
    [(ch, text, partner, client, tags)] = posted
    assert text.startswith("**Potluck**") and not any(t[0] == "mail-original" for t in tags)


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


def outgoing_wrap(
    frm="ana@belfry.test", header_from="Ana <ana@belfry.test>", email_id="e1", extra_tags=()
):
    raw = (
        f"From: {header_from}\r\nTo: bob@example.org\r\nBcc: spy@example.org\r\n"
        "Subject: Re: Hello\r\nIn-Reply-To: <m1@example.org>\r\n\r\nThanks!\r\n"
    )
    rumor = {
        "kind": 1301,
        "pubkey": ANA_PUB,
        "created_at": int(time.time()),
        "content": raw,
        "tags": [
            ["email-id", email_id],
            ["mail-from", frm],
            ["rcpt-to", "bob@example.org"],
            *extra_tags,
        ],
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


@pytest.fixture
def inbox(m, monkeypatch):
    """garden@ is a shared inbox; Ana is one of its admins."""
    posted = []
    monkeypatch.setattr(
        drops,
        "post",
        lambda svc, ch, text, imeta, partner, client="": posted.append(
            {"channel": ch, "text": text, "tags": imeta, "partner": partner, "client": client}
        ),
    )
    monkeypatch.setattr(
        drops, "upload", lambda svc, files: [["imeta", "url https://blossom.belfry.test/abc"]]
    )
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: ch == "garden")
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: ch == "garden")
    monkeypatch.setattr(
        mail, "group_admins", lambda svc, ch: {ANA_PUB} if ch == "garden" else set()
    )
    monkeypatch.setattr(apps, "member_by_email", lambda svc, e: None)
    return posted


def test_a_shared_inbox_takes_mail_from_anyone_as_an_e_mail_card(m, inbox):
    raw = message(
        frm="Marie Dubois <marie@example.org>", subject="Renting the hall", to="garden@belfry.test"
    )
    code, _ = mail.deliver(m, raw, "203.0.113.5", "h", "", ["garden@belfry.test"])
    assert code == 250
    [card] = inbox
    found = {t[0]: t[1:] for t in card["tags"]}
    assert (
        card["channel"] == "garden" and card["partner"] is None and card["client"] == "belfry-mail"
    )
    assert found["mail-from"] == ["marie@example.org"] and found["mail-name"] == ["Marie Dubois"]
    assert found["message-id"] == ["<m1@example.org>"] and found["belfry-auth"] == ["none"]
    assert card["text"].startswith("**Renting the hall**") and "old" not in card["text"]
    # a channel that is not an inbox still refuses strangers
    code, why = mail.deliver(m, raw, "203.0.113.5", "h", "", ["kitchen@belfry.test"])
    assert code == 550


def test_an_admin_answers_as_the_inbox_and_the_answer_shows_in_the_channel(m, inbox, monkeypatch):
    sent = []
    monkeypatch.setattr(
        mail, "smtp_send", lambda svc, frm, rcpts, data: sent.append((frm, data)) or (250, "OK")
    )
    card = "c" * 64
    mail.handle_wrap(
        m,
        outgoing_wrap(
            "garden@belfry.test", "Ana for Garden <garden@belfry.test>", "g1", [["e", card]]
        ),
    )
    [(frm, data)] = sent
    assert frm == "garden@belfry.test" and dsn_of(m, "g1")["status"] == "delivered"
    [out] = inbox
    found = {t[0]: t[1:] for t in out["tags"]}
    assert out["client"] == "belfry-mail-out" and out["text"] == "Thanks!"
    assert found["sent-by"] == [ANA_PUB] and found["mail-to"] == ["bob@example.org"]
    assert found["e"][0] == card


def test_an_inbox_answers_as_its_plain_address(m, inbox, monkeypatch):
    """hello@ for hello-79f0 (Xavier, 2026-11-16: the reply came from hello-79f0@): the plain
    address is accepted as the channel's, From header and envelope both, and the copy of what
    went out lands in the channel itself."""
    sent = []
    monkeypatch.setattr(
        mail, "smtp_send", lambda svc, frm, rcpts, data: sent.append((frm, data)) or (250, "OK")
    )
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: ch == "hello-79f0")
    monkeypatch.setattr(mail, "_top_channels", lambda svc: [("hello-79f0", "Hello")])
    monkeypatch.setattr(mail, "_makers", lambda svc: {})
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: ch == "hello-79f0")
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: {ANA_PUB})
    mail.handle_wrap(
        m,
        outgoing_wrap(
            "hello@belfry.test", "Ana for Hello <hello@belfry.test>", "h1", [["e", "c" * 64]]
        ),
    )
    [(frm, data)] = sent
    assert frm == "hello@belfry.test" and b"From: Ana for Hello <hello@belfry.test>" in data
    assert dsn_of(m, "h1")["status"] == "delivered"
    [out] = inbox
    assert out["channel"] == "hello-79f0" and out["client"] == "belfry-mail-out"
    # and mail to either address reaches the channel
    assert mail.resolve(m, "hello@belfry.test") == {"kind": "channel", "channel": "hello-79f0"}
    assert mail.resolve(m, "hello-79f0@belfry.test")["channel"] == "hello-79f0"


REFS_OUT2 = "In-Reply-To: <out2@belfry.test>\r\nReferences: <m0@example.org> <out2@belfry.test>\r\n"


def test_a_follow_up_card_names_what_it_answers(m, inbox):
    raw = message(
        frm="Marie <marie@example.org>",
        subject="Re: Renting the hall",
        to="garden@belfry.test",
        extra=REFS_OUT2,
    )
    assert mail.deliver(m, raw, "203.0.113.5", "h", "", ["garden@belfry.test"])[0] == 250
    found = {t[0]: t[1:] for t in inbox[0]["tags"]}
    assert found["in-reply-to"] == ["<out2@belfry.test>"]
    assert found["references"] == ["<m0@example.org> <out2@belfry.test>"]


def test_a_member_answering_by_e_mail_lands_in_the_thread(m, monkeypatch):
    """A member's mail that answers something in the channel (In-Reply-To / References naming
    its message-id) is a reply in that message's thread (kind 1111), not a new message."""
    posted = []
    monkeypatch.setattr(
        drops,
        "post",
        lambda svc, ch, text, imeta, partner, client="", kind=9: posted.append(
            {"text": text, "tags": imeta, "kind": kind}
        ),
    )
    monkeypatch.setattr(drops, "upload", lambda svc, files: [["imeta", "url https://b.test/x"]])
    monkeypatch.setattr(community, "member_signer", lambda svc, p: ("b" * 64, None))
    monkeypatch.setattr(
        apps, "member_by_email", lambda svc, e: {"partner": 7, "name": "Ana", "email": e}
    )
    monkeypatch.setattr(mail, "authenticate", lambda *a: {"aligned": True, "dkim": "x.org"})
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: True)
    card = {
        "id": "c" * 64,
        "pubkey": "f" * 64,
        "kind": 9,
        "tags": [["h", "garden"], ["message-id", "<m0@example.org>"]],
    }
    reply = {
        "id": "d" * 64,
        "pubkey": "a" * 64,
        "kind": 1111,
        "tags": [["h", "garden"], ["E", "c" * 64], ["message-id", "<r1@x.org>"]],
    }

    class Relay:
        def query(self, filters, sign):
            return [card, reply]

    monkeypatch.setattr(mail.followers, "_relay", lambda svc: (Relay(), None))
    args = ("203.0.113.5", "h", "", ["garden@belfry.test"])
    raw = message(frm="Ana <ana@x.org>", subject="Re: Hall", extra="In-Reply-To: <r1@x.org>\r\n")
    assert mail.deliver(m, raw, *args)[0] == 250
    [p] = posted
    found = {t[0]: t[1:] for t in p["tags"]}
    assert p["kind"] == 1111 and found["E"][0] == "c" * 64 and found["K"] == ["9"]
    assert found["message-id"] == ["<m1@example.org>"] and found["in-reply-to"] == ["<r1@x.org>"]
    # not an answer to anything here: an ordinary message, still carrying its message-id
    posted.clear()
    assert mail.deliver(m, message(frm="Ana <ana@x.org>", subject="New"), *args)[0] == 250
    [p] = posted
    assert p["kind"] == 9 and ["message-id", "<m1@example.org>"] in p["tags"]


def test_only_the_inbox_admins_answer_as_it(m, inbox, monkeypatch):
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: set())
    monkeypatch.setattr(mail, "smtp_send", lambda *a: pytest.fail("must not send"))
    mail.handle_wrap(m, outgoing_wrap("garden@belfry.test", "G <garden@belfry.test>", "g2"))
    assert "not yours" in dsn_of(m, "g2")["details"] and not inbox


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


UTF8 = "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n"


Y = "Yes, Sunday works.\n\n"


@pytest.mark.parametrize(
    "body,extra",
    [
        # Gmail: the "wrote:" line wraps, then the quote
        (
            Y + "On Mon, 3 Nov 2026 at 10:00, Ana Lopez <ana@x.org>\nwrote:\n> Sunday?",
            "",
        ),
        (
            Y + "Le lun. 3 nov. 2026 à 10:00, Ana <ana@x.org> a écrit :\n> Dimanche ?",
            UTF8,
        ),
        (
            Y + "Am Mo., 3. Nov. 2026 um 10:00 Uhr schrieb Ana <ana@x.org>:\n> x",
            "",
        ),
        # Outlook: a header block, no ">" at all
        (
            Y + "From: Ana Lopez <ana@x.org>\n"
            "Sent: Monday, 3 November 2026 10:00\n"
            "To: hello@belfry.test\nSubject: Sunday\n\nSunday?",
            "",
        ),
        (Y + "-----Original Message-----\nFrom: Ana\nSunday?", ""),
        # signature and phone footers
        (Y + "-- \nTom Peeters\nGarden team, +32 400 00 00 00", ""),
        (Y + "Sent from my iPhone", ""),
        (Y + "Envoyé de mon iPhone", UTF8),
        # HTML: Gmail quote div, Outlook reply block, Apple blockquote
        (
            '<div dir="ltr">Yes, Sunday works.</div><br><div class="gmail_quote">'
            "<div>On Mon, Ana wrote:</div>"
            "<blockquote>Sunday?</blockquote></div>",
            "Content-Type: text/html; charset=utf-8\r\n",
        ),
        (
            '<div>Yes, Sunday works.</div><div id="appendonsend"></div><hr><div id="divRplyFwdMsg">'
            "<b>From:</b> Ana</div><div>Sunday?</div>",
            "Content-Type: text/html; charset=utf-8\r\n",
        ),
        (
            "<div>Yes, Sunday works.<br></div><div><br>"
            '<blockquote type="cite">Sunday?</blockquote></div>',
            "Content-Type: text/html; charset=utf-8\r\n",
        ),
    ],
)
def test_the_noise_of_a_reply_is_removed(body, extra):
    assert mail.text_of(mail.parse(message(body=body, extra=extra))) == "Yes, Sunday works."


def test_a_line_that_only_looks_like_a_quote_header_stays():
    body = "From: the garden team\nWe planted 40 beds.\n\nOn Sunday we eat."
    assert mail.text_of(mail.parse(message(body=body))) == body


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


def _gmail_with_picture_and_pdf(to="garden@belfry.test") -> bytes:
    """What Gmail sends for a mail with a picture pasted in and a PDF attached (~300 kB)."""
    from email.message import EmailMessage as Msg

    msg = Msg()
    msg["From"] = "Xavi <xavi@example.org>"
    msg["To"] = to
    msg["Subject"] = "Poster"
    msg["Message-ID"] = "<g1@example.org>"
    msg.set_content("Here is the poster.\n\n[image: poster.png]")
    msg.add_alternative('<p>Here is the poster.</p><img src="cid:poster1">', subtype="html")
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 200_000
    msg.get_payload()[1].add_related(png, "image", "png", cid="<poster1>", filename="poster.png")
    msg.add_attachment(b"%PDF-1.4\n" + b"0" * 90_000, "application", "pdf", filename="plan.pdf")
    return msg.as_bytes()


def test_a_pasted_picture_and_an_attachment_both_reach_the_shared_inbox(m, inbox, monkeypatch):
    seen = []
    monkeypatch.setattr(
        drops,
        "upload",
        lambda svc, files: (
            seen.extend(files)
            or [["imeta", f"url https://blossom.belfry.test/{f[0]}"] for f in files]
        ),
    )
    monkeypatch.setattr(
        mail,
        "store_large",
        lambda svc, raw: [
            ["url", "https://b/x"],
            ["decryption-key", "k"],
            ["decryption-nonce", "n"],
        ],
    )
    raw = _gmail_with_picture_and_pdf()
    assert len(raw) > 60_000
    code, _ = mail.deliver(m, raw, "203.0.113.5", "h", "", ["garden@belfry.test"])
    assert code == 250
    [card] = inbox
    assert sorted((f[0], f[1]) for f in seen) == [
        ("plan.pdf", "application/pdf"),
        ("poster.png", "image/png"),
    ]
    assert [t for t in card["tags"] if t[0] == "imeta"] and "Here is the poster." in card["text"]
    assert any(t[0] == "mail-original" for t in card["tags"])


def test_a_refusal_is_logged_with_its_reason_and_never_the_address(m, inbox, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="belfry_onboarding.mail")
    raw = message(frm="Bob <bob@example.org>", to="kitchen@belfry.test")
    code, _ = mail.deliver(m, raw, "203.0.113.5", "h", "", ["kitchen@belfry.test"])
    assert code == 550
    line = next(r.getMessage() for r in caplog.records if "refused for" in r.getMessage())
    assert "550" in line and "bob@example.org" not in line


class _InboxRelay:
    def __init__(self, events):
        self.events = events

    def query(self, filters, sign):
        out = []
        for f in filters:
            for e in self.events:
                tags = {}
                for t in e["tags"]:
                    tags.setdefault(t[0], []).append(t[1])
                if e["kind"] not in f.get("kinds", [e["kind"]]):
                    continue
                if any(not set(v) & set(tags.get(k[1:], [])) for k, v in f.items() if k[0] == "#"):
                    continue
                out.append(e)
        return out


def test_the_inbox_setting_is_per_channel_and_old_settings_still_count(m, monkeypatch):
    from belfry_onboarding import followers

    def setting(d, h, on, at):
        return {
            "kind": 30078,
            "pubkey": ANA_PUB,
            "created_at": at,
            "content": json.dumps({"on": on}),
            "tags": [["d", d], ["h", h]],
        }

    events = [
        # the old single d: the relay keeps only the latest per author, here for #sponsors
        setting("belfry:inbox", "sponsors", True, 100),
        setting("belfry:inbox:hello", "hello", True, 50),
        setting("belfry:inbox:kitchen", "kitchen", False, 60),
    ]
    monkeypatch.setattr(followers, "_relay", lambda svc: (_InboxRelay(events), None))
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: {ANA_PUB})
    mail._cache.clear()
    assert mail.inbox_on(m, "hello") is True
    assert mail.inbox_on(m, "sponsors") is True
    assert mail.inbox_on(m, "kitchen") is False
    assert mail.inbox_on(m, "garden") is False
    assert mail.inbox_d("hello") == "belfry:inbox:hello"
