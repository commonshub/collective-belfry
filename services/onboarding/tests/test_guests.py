"""A channel's guests (guests.py): a link from a channel's admin, a guest pass for 30 days that
puts them in that channel only, out again when it closes."""

from __future__ import annotations

import datetime as dt
import re
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import console, core, guests, mail, mailer, steward
from belfry_onboarding.app import create_app
from belfry_onboarding.register import Register

from .test_holder_invites import BASE, post


@pytest.fixture
def rsvc(services, tmp_path, settings):
    mailer.SENT.clear()
    services.odoo = Register(tmp_path / "register.sqlite", settings)
    return services


@pytest.fixture
def published(monkeypatch):
    out: list[tuple[int, list[list[str]]]] = []
    monkeypatch.setattr(
        guests, "_channel_event", lambda svc, kind, tags: out.append((kind, tags)) or True
    )
    return out


def admin_with_key(rsvc) -> tuple[PrivateKey, int]:
    made = steward.steward_pass(rsvc, "admin@belfry.test", "Ada Admin")
    key = PrivateKey()
    rsvc.odoo.write(
        "belfry.pass", [made["pass_id"]], {"keycast_pubkey": key.public_key_xonly.format().hex()}
    )
    partner = rsvc.odoo.read_one("belfry.pass", made["pass_id"], ["partner_id"])["partner_id"]
    return key, core.m2o_id(partner)


def test_a_guest_of_one_channel_end_to_end(rsvc, published):
    reg = rsvc.odoo
    key, ada = admin_with_key(rsvc)
    row = reg.create_guest_invite(ada, key.public_key_xonly.format().hex(), "garden", 30)
    token = row["url"].rsplit("/", 1)[1]
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/invite/{token}").text
        assert "invites you to #garden" in page and "as a guest for 30 days" in page
        c.post(f"/invite/{token}", data={"name": "Gus Guest", "email": "gus@example.org"})
        text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        assert "a guest of #garden" in text
        link = re.search(r"https?://\S+/confirm/\S+", text).group(0)
        done = c.get("/" + link.split("://", 1)[1].split("/", 1)[1])
        assert done.status_code == 200 and "Welcome to #garden" in done.text
        assert "/chat/#/c/garden" in done.text
    inv = reg.invite_by_token(token)
    p = reg.read_one(
        "belfry.pass", inv["pass_id"], ["state", "tier", "date_end", "keycast_pubkey", "partner_id"]
    )
    assert p["state"] == "active" and p["tier"] == "guest"
    assert p["date_end"] == (dt.date.today() + dt.timedelta(days=30)).isoformat()
    # in the channel, with who invited them; on no other whitelist; no member in the console
    kind, tags = published[-1]
    assert kind == 9000 and ["h", "garden"] in tags and ["p", p["keycast_pubkey"], "member"] in tags
    assert ["invited-by", key.public_key_xonly.format().hex()] in tags and [
        "name",
        "Gus Guest",
    ] in tags
    assert p["keycast_pubkey"] not in core.active_pubkeys(rsvc)
    sess = {"partner": core.m2o_id(p["partner_id"])}
    assert console.viewer(rsvc, sess) is None
    # the pass closes: out of the channel
    core.revoke_pass(rsvc, inv["pass_id"], "test")
    kind, tags = published[-1]
    assert kind == 9001 and ["h", "garden"] in tags and ["p", p["keycast_pubkey"]] in tags


def test_only_a_channel_s_admins_get_a_guest_link(rsvc, published, monkeypatch):
    rsvc.settings.public_url = BASE
    key, _ = admin_with_key(rsvc)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: ch == "garden")
    admins: set[str] = set()
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: admins)
    with TestClient(create_app(rsvc)) as c:
        r = post(c, key, guests.API, {"channel": "garden"})
        assert r.status_code == 403  # not this channel's admin (and no steward in the fakes)
        admins.add(key.public_key_xonly.format().hex())
        assert post(c, key, guests.API, {"channel": "nowhere"}).status_code == 404
        assert post(c, key, guests.API, {"channel": "../x"}).status_code == 422
        time.sleep(1.1)  # the same signed request twice in a second is a replay
        r = post(c, key, guests.API, {"channel": "garden"})
        assert r.status_code == 200, r.text
        assert r.json()["days"] == 30 and "/invite/" in r.json()["url"]
