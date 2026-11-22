"""A channel's e-mail address, chosen by a belfry admin (mailalias.py, docs/mail.md)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import checkin, mail, mailalias, mailforward, usernames, whois
from belfry_onboarding.app import create_app
from belfry_onboarding.holder_invites import ApiError
from belfry_onboarding.register import Register

from .test_holder_invites import nip98
from .test_mailforward import message

ROOT = "e" * 64  # an admin of the belfry
ADA = "a" * 64  # an admin of #contact, not of the belfry
BEA = "b" * 64  # a member
LINA = "1" * 64  # the member whose username is lina
CONTACT = "contact-79f0"
GARDEN = "garden-12ab"
CHANNELS = [(CONTACT, "Contact"), (GARDEN, "Garden"), ("support-9z9z", "Support")]
HOST = {"Host": "belfry.belfry.test", "Content-Type": "application/json"}


@pytest.fixture
def al(services, tmp_path, settings, monkeypatch):
    settings.register_path = str(tmp_path / "register.sqlite")
    settings.public_url = "https://pass.belfry.test"
    settings.console_url = "https://belfry.belfry.test"
    settings.nip05_domain = "belfry.test"
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    mail._cache.clear()
    usernames._taken.clear()
    ids = {c for c, _ in CHANNELS}
    monkeypatch.setattr(mail, "channel_exists", lambda svc, c: c in ids)
    monkeypatch.setattr(mail, "_top_channels", lambda svc: list(CHANNELS))
    monkeypatch.setattr(mail, "_made_by_admin", lambda svc, c: True)
    monkeypatch.setattr(mail, "active_pubkey", lambda svc, p: p == LINA)
    monkeypatch.setattr(whois, "kinds", lambda svc: {ROOT: "member", ADA: "member", BEA: "member"})
    monkeypatch.setattr(checkin, "_belfry_admins", lambda svc: {ROOT})
    monkeypatch.setattr(checkin, "group_state", lambda svc, g: SimpleNamespace(admins={ADA}))
    monkeypatch.setattr(checkin, "is_archived", lambda svc, g: False)
    monkeypatch.setattr(
        services.keycast, "pubkey_for_name", lambda name: LINA if name == "lina" else None
    )
    monkeypatch.setattr(usernames, "_guest_slug", lambda svc, name: "g" if name == "gus" else "")
    return services


def code_of(fn) -> tuple[int, str]:
    with pytest.raises(ApiError) as exc:
        fn()
    return exc.value.status, exc.value.error


def set_alias(al, name, channel=CONTACT, who=ROOT):
    return mailalias.api(al, who, {"channel": channel, "alias": name})


def test_a_channel_starts_with_the_address_its_name_gives(al):
    assert mailalias.api(al, ADA, {"channel": CONTACT}) == {
        "channel": CONTACT,
        "alias": "contact",
        "address": "contact@belfry.test",
        "custom": False,
        "may_set": False,
    }
    assert mailalias.api(al, ROOT, {"channel": CONTACT})["may_set"] is True


def test_a_channel_gets_the_address_an_admin_chose(al):
    assert set_alias(al, " Hello ") == {
        "channel": CONTACT,
        "alias": "hello",
        "address": "hello@belfry.test",
        "custom": True,
        "may_set": True,
    }
    assert mailforward.alias(al, CONTACT) == "hello"
    assert mailalias.api(al, ADA, {"channel": CONTACT})["address"] == "hello@belfry.test"
    # the whole address typed works too
    assert set_alias(al, "team@belfry.test")["alias"] == "team"


def test_who_may_read_and_who_may_set(al):
    assert code_of(lambda: mailalias.api(al, BEA, {"channel": CONTACT})) == (403, "not_allowed")
    assert code_of(lambda: mailalias.api(al, "d" * 64, {"channel": CONTACT}))[1] == "not_allowed"
    # the channel's own admin reads and checks, but only a belfry admin sets or clears
    assert mailalias.api(al, ADA, {"channel": CONTACT, "check": "hello"})["state"] == "available"
    assert code_of(lambda: set_alias(al, "hello", who=ADA)) == (403, "not_allowed")
    assert code_of(lambda: set_alias(al, None, who=ADA)) == (403, "not_allowed")
    assert mailforward.alias(al, CONTACT) == "contact"
    assert code_of(lambda: mailalias.api(al, ROOT, {"channel": "nope"})) == (
        404,
        "unknown_channel",
    )
    assert code_of(lambda: mailalias.api(al, ROOT, {"channel": "no/pe"})) == (422, "invalid")


@pytest.mark.parametrize(
    "wanted,state",
    [
        ("hello", "available"),
        ("Hello ", "available"),
        ("press.team_2", "available"),
        ("contact", "available"),  # what it has today
        (CONTACT, "available"),  # its own id
        ("lina", "taken"),  # a member's username
        ("gus", "taken"),  # a guest's page
        ("garden", "taken"),  # another channel's plain address
        (GARDEN, "taken"),  # another channel's id
        ("support", "taken"),  # reserved for people, and another channel answers there
        ("postmaster", "reserved"),
        ("abuse", "reserved"),
        ("bounces", "reserved"),
        ("admin", "reserved"),
        ("noreply", "reserved"),
        ("guest-12", "reserved"),  # a name accounts start with
        ("0007", "reserved"),
        ("_smtp", "invalid"),
        ("", "invalid"),
        ("a b", "invalid"),
        ("-x", "invalid"),
        ("é", "invalid"),
        ("x" * 31, "invalid"),
        ("someone@example.org", "invalid"),
    ],
)
def test_checking_a_name(al, wanted, state):
    out = mailalias.api(al, ROOT, {"channel": CONTACT, "check": wanted})
    assert out["state"] == state
    assert out["alias"] == wanted.strip().lower()[:64]
    assert mailalias.custom(al, CONTACT) == ""  # a check writes nothing


def test_names_people_cannot_take_are_the_ones_a_channel_is_for(al):
    for name in ("hello", "info", "help"):
        assert name in usernames.RESERVED
        assert mailalias.api(al, ROOT, {"channel": CONTACT, "check": name})["state"] == "available"
    # its own plain address stays its own even when the word is reserved
    out = mailalias.api(al, ROOT, {"channel": "support-9z9z", "check": "support"})
    assert out["state"] == "available"


@pytest.mark.parametrize(
    "wanted,code",
    [
        ("lina", (409, "taken")),
        ("garden", (409, "taken")),
        ("postmaster", (409, "reserved")),
        ("a b", (422, "invalid")),
        (12, (422, "invalid")),
    ],
)
def test_a_name_that_cannot_be_set(al, wanted, code):
    assert code_of(lambda: set_alias(al, wanted)) == code
    assert mailforward.alias(al, CONTACT) == "contact"


def test_two_channels_never_share_an_address(al):
    set_alias(al, "hello")
    assert code_of(lambda: set_alias(al, "hello", channel=GARDEN)) == (409, "taken")
    out = mailalias.api(al, ROOT, {"channel": GARDEN, "check": "hello"})
    assert out == {"state": "taken", "alias": "hello"}
    # the channel that has it may send it again, and sees it as available
    assert set_alias(al, "hello")["alias"] == "hello"
    assert mailalias.api(al, ROOT, {"channel": CONTACT, "check": "hello"})["state"] == "available"
    # an address that was replaced is free again, for anyone
    set_alias(al, "team")
    assert mail.channel_id(al, "hello") is None
    assert set_alias(al, "hello", channel=GARDEN)["address"] == "hello@belfry.test"
    assert mail.channel_id(al, "hello") == GARDEN and mail.channel_id(al, "team") == CONTACT


def test_a_chosen_address_wins_over_a_name_another_channel_derives(al, monkeypatch):
    set_alias(al, "plants")
    # a channel made later whose name gives the same word does not take the mail away
    monkeypatch.setattr(mail, "_top_channels", lambda svc: [*CHANNELS, ("plants-77aa", "Plants")])
    assert mail.channel_by_slug(al, "plants") == "plants-77aa"
    assert mail.channel_id(al, "plants") == CONTACT


def test_mail_to_the_chosen_address_reaches_the_channel_and_the_old_ones_still_do(al):
    assert mail.resolve(al, "hello@belfry.test") is None
    set_alias(al, "hello")
    want = {"kind": "channel", "channel": CONTACT}
    assert mail.resolve(al, "hello@belfry.test") == want  # at once, not a minute later
    assert mail.resolve(al, "Hello+tag@belfry.test") == want
    assert mail.resolve(al, "contact@belfry.test") == want
    assert mail.resolve(al, f"{CONTACT}@belfry.test") == want
    assert mail.resolve(al, "lina@belfry.test")["kind"] == "member"


def test_back_to_automatic(al):
    set_alias(al, "hello")
    assert mail.resolve(al, "hello@belfry.test")
    out = set_alias(al, None)
    assert (out["alias"], out["custom"]) == ("contact", False)
    assert mail.resolve(al, "hello@belfry.test") is None
    assert mail.resolve(al, "contact@belfry.test")["channel"] == CONTACT
    assert al.odoo.param(mailalias.PARAM + CONTACT) == ""
    assert al.odoo.param(mailalias.OWNER + "hello") == ""
    assert set_alias(al, "")["custom"] is False  # twice is fine


def test_the_forwarded_copy_leaves_from_the_chosen_address(al):
    set_alias(al, "hello")
    head = mailforward.build(al, CONTACT, message()).partition(b"\r\n\r\n")[0].decode()
    assert 'From: "Marie Dubois via hello@belfry.test" <hello@belfry.test>' in head
    assert "X-Forwarded-For-Channel: hello@belfry.test" in head


def test_a_shared_inbox_answers_as_the_chosen_address(al, monkeypatch):
    """mail.outgoing takes the From a channel's admin sends as: it reaches the channel through
    the same lookup as mail coming in."""
    set_alias(al, "hello")
    monkeypatch.setattr(mail, "inbox_on", lambda svc, ch: ch == CONTACT)
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: {ADA})
    monkeypatch.setattr(mail, "active_pubkey", lambda svc, p: True)
    sent: list = []
    monkeypatch.setattr(
        mail, "smtp_send", lambda svc, frm, rcpts, data: sent.append(frm) or (250, "OK")
    )
    raw = "From: hello@belfry.test\r\nTo: marie@example.org\r\nSubject: Re\r\n\r\nYes.\r\n"
    rumor = {
        "content": raw,
        "tags": [["mail-from", "hello@belfry.test"], ["rcpt-to", "marie@example.org"]],
    }
    _, code, _, as_channel = mail.outgoing(al, ADA, rumor)
    assert code == 250 and as_channel[0] == CONTACT and sent == ["hello@belfry.test"]
    with pytest.raises(mail.MailError):  # not for someone who is no admin of the channel
        mail.outgoing(al, BEA, rumor)


def test_nobody_takes_a_channels_chosen_address_as_a_username(al):
    assert usernames.holder(al, "plants") == ""
    set_alias(al, "plants")
    assert usernames.holder(al, "plants") == "#"
    assert code_of(lambda: usernames.require(al, "plants")) == (409, "username_taken")
    assert "plants" not in usernames.suggestions(al, "Plants")
    assert usernames.check(al, BEA, {"username": "plants"})["state"] == "taken"
    set_alias(al, None)
    assert usernames.holder(al, "plants") == ""


def test_an_archived_channel_keeps_what_it_has(al, monkeypatch):
    monkeypatch.setattr(checkin, "is_archived", lambda svc, g: True)
    assert code_of(lambda: set_alias(al, "hello")) == (409, "archived")


def test_a_signer_that_does_not_answer_sets_nothing(al, monkeypatch):
    def down(name):
        raise RuntimeError("signer down")

    monkeypatch.setattr(al.keycast, "pubkey_for_name", down)
    assert code_of(lambda: set_alias(al, "hello")) == (503, "signer")
    assert mailalias.custom(al, CONTACT) == ""


def test_a_half_written_record_names_nobody(al):
    al.odoo.set_param(mailalias.OWNER + "hello", CONTACT)  # the owner without the alias
    assert mailalias.channel_of(al, "hello") is None
    assert mail.channel_id(al, "hello") is None
    assert set_alias(al, "hello", channel=GARDEN)["alias"] == "hello"
    # an alias kept for a channel that is gone reaches nothing
    al.odoo.set_param(mailalias.OWNER + "old", "gone-0000")
    al.odoo.set_param(mailalias.PARAM + "gone-0000", "old")
    assert mail.channel_id(al, "old") is None


def test_with_odoo_as_the_register_it_is_not_there(services, monkeypatch):
    assert code_of(lambda: mailalias.api(services, ROOT, {"channel": CONTACT})) == (
        409,
        "unsupported",
    )
    assert mailalias.custom(services, CONTACT) == ""
    assert mailalias.channel_of(services, "hello") is None


def test_the_api_over_http(al, monkeypatch):
    key = PrivateKey()
    pub = key.public_key.format()[1:].hex()
    url = "https://belfry.belfry.test/api/v1/mail-alias"

    def post(c, data):
        body = json.dumps(data).encode()
        return c.post(
            "/api/v1/mail-alias",
            content=body,
            headers={**HOST, "Authorization": nip98(key, url, "POST", body)},
        )

    with TestClient(create_app(al)) as c:
        r = post(c, {"channel": CONTACT})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        assert c.post("/api/v1/mail-alias", content=b"{}", headers=HOST).status_code == 401
    kinds = whois.kinds(al)
    monkeypatch.setattr(whois, "kinds", lambda svc: {**kinds, pub: "member"})
    monkeypatch.setattr(checkin, "_belfry_admins", lambda svc: {pub})
    with TestClient(create_app(al)) as c:
        r = post(c, {"channel": CONTACT, "alias": "hello"})
        assert r.status_code == 200 and r.json()["address"] == "hello@belfry.test"
        assert r.headers["cache-control"] == "no-store"
        r = post(c, {"channel": GARDEN, "alias": "hello"})
        assert r.status_code == 409 and r.json()["error"] == "taken"
        r = post(c, {"channel": GARDEN, "check": "postmaster"})
        assert r.json() == {"state": "reserved", "alias": "postmaster"}
        # the chat on its own host may call it
        pre = c.options(
            "/api/v1/mail-alias",
            headers={
                "Host": "belfry.belfry.test",
                "Origin": "https://chat.belfry.test",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert pre.status_code == 204
