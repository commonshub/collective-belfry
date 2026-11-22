"""A channel's shared inbox turned on or off by the belfry itself (``belfry inbox``), no client."""

from __future__ import annotations

import json
import time

import pytest
from coincurve import PrivateKey

from belfry_onboarding import __main__ as cli
from belfry_onboarding import followers, mail
from belfry_onboarding.groups_members import signed

from .fakes import FakeRelay

GROUPS = PrivateKey(b"\x03" * 32)
GROUPS_PUB = GROUPS.public_key_xonly.format().hex()
ADA = PrivateKey(b"\x04" * 32)
CH = "hello-79f0"


@pytest.fixture
def grelay(services, monkeypatch):
    groups = FakeRelay()
    now = int(time.time()) - 60
    groups.events += [
        signed(GROUPS, {"kind": 39000, "created_at": now, "tags": [["d", CH]], "content": ""}),
        signed(
            GROUPS,
            {
                "kind": 39001,
                "created_at": now,
                "tags": [["d", CH], ["p", GROUPS_PUB, "admin"], ["p", "a" * 64, "admin"]],
                "content": "",
            },
        ),
    ]
    monkeypatch.setattr(followers, "_relay", lambda svc: (groups, lambda t: signed(GROUPS, t)))
    mail._cache.clear()
    return groups


def test_the_belfry_turns_an_inbox_on_and_off(services, grelay):
    assert mail.inbox_now(services, CH) is False
    assert mail.set_inbox(services, CH, True) is True
    [setting] = [e for e in grelay.events if e["kind"] == 30078]
    assert setting["pubkey"] == GROUPS_PUB
    assert ["d", f"belfry:inbox:{CH}"] in setting["tags"] and ["h", CH] in setting["tags"]
    assert json.loads(setting["content"]) == {"on": True}
    assert mail.inbox_on(services, CH) is True  # the cache holds the new state
    assert mail.set_inbox(services, CH, False) is False
    assert mail.inbox_on(services, CH) is False


def test_the_state_read_skips_the_cache(services, grelay):
    assert mail.inbox_on(services, CH) is False  # cached for a minute
    grelay.events.append(
        signed(
            GROUPS,
            {
                "kind": 30078,
                "created_at": int(time.time()),
                "tags": [["d", mail.inbox_d(CH)], ["h", CH]],
                "content": json.dumps({"on": True}),
            },
        )
    )
    assert mail.inbox_on(services, CH) is False
    assert mail.inbox_now(services, CH) is True


def test_an_unknown_channel(services, grelay):
    with pytest.raises(LookupError):
        mail.set_inbox(services, "nope", True)
    assert not [e for e in grelay.events if e["kind"] == 30078]


def test_the_command(services, grelay, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: services.settings)
    monkeypatch.setattr(cli, "build_services", lambda s: services)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    assert cli.main(["inbox", CH]) == 0
    assert capsys.readouterr().out == f"{CH}: e-mails from anyone off\n"
    assert cli.main(["inbox", CH, "on"]) == 0
    assert capsys.readouterr().out == f"{CH}: e-mails from anyone on\n"
    assert cli.main(["inbox", "nope"]) == 2
    # a setting the relay would not count (the groups key not among the channel's admins)
    grelay.events[1]["tags"] = [["d", CH], ["p", "a" * 64, "admin"]]
    assert cli.main(["inbox", CH, "on"]) == 1
    assert "did not take it" in capsys.readouterr().err
