"""Guessing sign-in codes (2026-11-23 on the test belfry: a script asked for five codes for an
admin's public address and typed five wrong ones for each): an address's codes stop after 10
wrong ones a day and its owner hears of it, a network that guesses waits an hour (not the
venue's), and the sign-in answers alike for a member's address and anyone else's."""

from __future__ import annotations

import re

import pytest

from belfry_onboarding import connect, mailer, register
from belfry_onboarding.connect import ConnectError
from belfry_onboarding.register import Register

KEYS = [c * 64 for c in "abcdef"]


@pytest.fixture
def reg(tmp_path, settings):
    mailer.SENT.clear()
    connect._wrong_networks.reset()
    return Register(tmp_path / "register.sqlite", settings)


@pytest.fixture
def rsvc(services, reg):
    services.odoo = reg
    return services


def code_of(msg) -> str:
    return re.search(r"\b(\d{6})\b", msg["Subject"]).group(1)


def not_it(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_ten_wrong_codes_stop_the_address_for_a_day_and_warn_its_owner(reg):
    email = "ana@example.org"
    call = reg.call
    for key in KEYS[:2]:  # a new key for every code, as the script did
        assert call("belfry.key.code", "belfry_key_code_send", [email, key])["state"] == "sent"
        code = code_of(mailer.SENT[-1])
        for _ in range(register.CODE_TRIES):
            out = call("belfry.key.code", "belfry_key_code_verify", [email, key, not_it(code)])
            assert out["state"] == "wrong_code"
    warning = mailer.SENT[-1]
    assert warning["To"] == email and "trying to sign in" in warning["Subject"]
    text = warning.get_body(preferencelist=("plain",)).get_content()
    assert "passkey" in text and "two-step verification" in text and "Never pass a code on" in text
    sent = len(mailer.SENT)
    # no more codes for the address, whatever the key, and the warning went out once
    out = call("belfry.key.code", "belfry_key_code_send", [email, KEYS[2]])
    assert out == {"state": "too_many", "retry_after": register.WRONG_WINDOW_HOURS * 3600}
    assert len(mailer.SENT) == sent
    # other addresses are not touched
    assert (
        call("belfry.key.code", "belfry_key_code_send", ["bo@example.org", KEYS[3]])["state"]
        == "sent"
    )


def test_a_stopped_address_takes_no_code_not_even_the_right_one(reg):
    email = "ana@example.org"
    call = reg.call
    call("belfry.key.code", "belfry_key_code_send", [email, KEYS[0]])
    wrong = not_it(code_of(mailer.SENT[-1]))
    for _ in range(register.CODE_TRIES):
        call("belfry.key.code", "belfry_key_code_verify", [email, KEYS[0], wrong])
    call("belfry.key.code", "belfry_key_code_send", [email, KEYS[1]])
    code = code_of(mailer.SENT[-1])
    for _ in range(register.WRONG_PER_EMAIL - register.CODE_TRIES - 1):
        call("belfry.key.code", "belfry_key_code_verify", [email, KEYS[1], not_it(code)])
    # one wrong code left before the stop: the right one still works
    assert reg.check_key_code(email, KEYS[1], code)["state"] == "ok"
    call("belfry.key.code", "belfry_key_code_send", [email, KEYS[2]])
    code = code_of(mailer.SENT[-1])
    call("belfry.key.code", "belfry_key_code_verify", [email, KEYS[2], not_it(code)])  # the 10th
    assert reg.check_key_code(email, KEYS[2], code) == {"state": "expired"}


def test_an_unknown_address_is_answered_like_a_members(rsvc):
    f: dict = {"session": KEYS[0]}
    assert connect.send_code(rsvc, f, " Nobody@Example.org ") == {"to": "nobody@example.org"}
    assert mailer.SENT == []  # no mail goes out
    for left in range(connect.CODE_TRIES - 1, -1, -1):
        with pytest.raises(ConnectError) as exc:
            connect.verify_code(rsvc, f, "123456")
        assert (exc.value.status, exc.value.message) == (
            422,
            f"That code is not right ({left} {'try' if left == 1 else 'tries'} left).",
        )
    with pytest.raises(ConnectError) as exc:
        connect.verify_code(rsvc, f, "123456")
    assert exc.value.status == 410
    with pytest.raises(ConnectError) as exc:
        connect.send_code(rsvc, {"session": KEYS[1]}, "not-an-address")
    assert exc.value.status == 422


def guess(svc, ip: str, email: str, times: int) -> None:
    f: dict = {"session": KEYS[0]}
    connect.send_code(svc, f, email, ip)
    for _ in range(times):
        with pytest.raises(ConnectError):
            connect.verify_code(svc, f, "123456", ip)


def test_a_network_that_guesses_waits_an_hour_but_not_the_venue(rsvc):
    ip = "2001:db8:1:2::5"
    guess(rsvc, ip, "x1@example.org", 5)
    guess(rsvc, ip, "x2@example.org", connect.WRONG_PER_NETWORK - 5)
    for other in ("2001:db8:1:2::77", ip):  # the same /64
        with pytest.raises(ConnectError) as exc:
            connect.send_code(rsvc, {"session": KEYS[1]}, "x3@example.org", other)
        assert exc.value.status == 429 and "network" in exc.value.message
    # another network goes on
    assert connect.send_code(rsvc, {"session": KEYS[1]}, "x3@example.org", "198.51.100.9")
    # the venue's WiFi is never paused
    rsvc.settings.rate_limit_exempt = "203.0.113.0/24"
    for n in range(3):
        guess(rsvc, "203.0.113.40", f"v{n}@example.org", 5)
    assert connect.send_code(rsvc, {"session": KEYS[2]}, "v9@example.org", "203.0.113.40")
