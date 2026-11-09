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
    reg.write("belfry.invite", [row["id"]], {"guest_max": False})  # an older one-person link
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/invite/{token}").text
        assert "invites you to #garden" in page and "as a guest for 30 days" in page
        c.post(f"/invite/{token}", data={"name": "Gus Guest", "email": "gus@example.org"})
        text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        assert "a guest of #garden" in text
        link = re.search(r"https?://\S+/confirm/\S+", text).group(0)
        done = c.get("/" + link.split("://", 1)[1].split("/", 1)[1])
        assert done.status_code == 200 and "Welcome to #garden" in done.text
        assert f"https://chat.{rsvc.settings.nip05_domain}/#/c/garden" in done.text
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


def test_a_member_gets_a_guest_link_for_a_channel(rsvc, published, monkeypatch):
    rsvc.settings.public_url = BASE
    key, _ = admin_with_key(rsvc)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: ch == "garden")
    admins: set[str] = set()
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: admins)
    with TestClient(create_app(rsvc)) as c:
        assert post(c, key, guests.API, {"channel": "nowhere"}).status_code == 404
        assert post(c, key, guests.API, {"channel": "../x"}).status_code == 422
        time.sleep(1.1)  # the same signed request twice in a second is a replay
        r = post(c, key, guests.API, {"channel": "garden"})
        assert r.status_code == 200, r.text
        assert r.json()["days"] == 30 and "/invite/" in r.json()["url"]


def test_the_api_also_answers_on_the_belfry_host(rsvc, published, monkeypatch):
    """belfry.<domain>/api/v1/<name> is pass.<domain>/api/<name>, signed with its own URL."""
    from belfry_onboarding import mail

    from .test_holder_invites import nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    key, _ = admin_with_key(rsvc)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: True)
    url = "https://belfry.belfry.test/api/v1/channel-invites"
    body = b'{"channel": "garden"}'
    with TestClient(create_app(rsvc)) as c:
        r = c.post(
            "/api/v1/channel-invites",
            content=body,
            headers={
                "Host": "belfry.belfry.test",
                "Authorization": nip98(key, url, "POST", body),
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200, r.text
        doc = c.get("/.well-known/belfry.json").json()
        assert doc["api"]["base"] == "https://belfry.belfry.test/api/v1"
        assert doc["api"]["channel_invites"] == url


def test_an_agent_accepts_a_guest_invite_with_its_own_key(rsvc, published, monkeypatch):
    """/invite/<token>.md tells an agent what to do; it accepts with its own key (NIP-98), no
    e-mail; it is put in the channel with its name and who invited it, and leaves at the end."""
    from .test_holder_invites import nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    rsvc.settings.groups_relay_public_url = "wss://groups-relay.belfry.test"
    key, ada = admin_with_key(rsvc)
    row = rsvc.odoo.create_guest_invite(ada, key.public_key_xonly.format().hex(), "garden", 30)
    token = row["url"].rsplit("/", 1)[1]
    agent = PrivateKey()
    agent_pk = agent.public_key_xonly.format().hex()
    with TestClient(create_app(rsvc)) as c:
        assert "Are you an AI agent?" in c.get(f"/invite/{token}").text
        md = c.get(f"/invite/{token}.md")
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
        url = f"https://belfry.belfry.test/api/v1/guest-invites/{token}/accept"
        assert (
            url in md.text
            and "wss://groups-relay.belfry.test" in md.text
            and '"#h": ["garden"]' in md.text
        )
        assert c.get(f"/invite/{token}", headers={"Accept": "text/markdown"}).text == md.text
        body = b'{"name": "Gardening bot", "about": "Waters on schedule"}'
        r = c.post(
            f"/api/v1/guest-invites/{token}/accept",
            content=body,
            headers={
                "Host": "belfry.belfry.test",
                "Authorization": nip98(agent, url, "POST", body),
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200, r.text
        assert (
            r.json()["channel"] == "garden"
            and r.json()["relay"] == "wss://groups-relay.belfry.test"
        )
        kind, tags = published[-1]
        assert (
            kind == 9000 and ["p", agent_pk, "member"] in tags and ["name", "Gardening bot"] in tags
        )
        assert ["invited-by", key.public_key_xonly.format().hex()] in tags and [
            "agent",
            "1",
        ] in tags
        time.sleep(1.1)
        again = c.post(
            f"/api/v1/guest-invites/{token}/accept",
            content=body,
            headers={
                "Host": "belfry.belfry.test",
                "Authorization": nip98(PrivateKey(), url, "POST", body),
                "Content-Type": "application/json",
            },
        )
        assert again.status_code == 200  # a guest link works for many (a QR shown to a room)
        assert rsvc.odoo.invite_by_token(token)["guest_uses"] == 2
    p = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", agent_pk]], ["id", "tier", "state"]
    )[0]
    assert (
        p["tier"] == "guest"
        and p["state"] == "active"
        and agent_pk not in core.active_pubkeys(rsvc)
    )
    core.revoke_pass(rsvc, p["id"], "test")  # no signer account to suspend: no error
    assert published[-1][0] == 9001


def test_a_person_joins_with_a_name_and_saves_the_account_later(rsvc, published, keycast):
    """Scan, name, in: an account in the signer and a session for the chat at once (a one-time
    code); "not saved" until the guest proves an e-mail, signed with their own identity."""
    from .test_holder_invites import nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    rsvc.settings.nip05_domain = "belfry.test"
    key, ada = admin_with_key(rsvc)
    row = rsvc.odoo.create_guest_invite(ada, key.public_key_xonly.format().hex(), "garden", 30)
    token = row["url"].rsplit("/", 1)[1]
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/invite/{token}").text
        assert 'action="/invite/' in page and "/join" in page and 'type="email"' not in page
        r = c.post(f"/invite/{token}/join", data={"name": "Gus"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(
            "https://chat.belfry.test/#/welcome/"
        )
        code = r.headers["location"].rsplit("/", 1)[1]
        got = c.post(f"/api/guest-sessions/{code}")
        assert got.status_code == 200 and got.json()["bunker_url"].startswith("bunker://")
        assert got.json()["channel"] == "garden" and got.json()["guest"] is True
        assert c.post(f"/api/guest-sessions/{code}").status_code == 410  # once
        guest_pk = got.json()["pubkey"]
        kind, tags = published[-1]
        assert kind == 9000 and ["p", guest_pk, "member"] in tags and ["unsaved", "1"] in tags
        assert ["name", "Gus"] in tags
        inv = rsvc.odoo.invite_by_token(token)
        assert inv["state"] == "open" and inv["guest_uses"] == 1  # a QR for a room: still open
        # saving: our own key stands in for the guest's (the signer holds the real one)
        me = PrivateKey()
        me_pk = me.public_key_xonly.format().hex()
        p = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", guest_pk]], ["id", "partner_id"]
        )[0]
        rsvc.odoo.write("belfry.pass", [p["id"]], {"keycast_pubkey": me_pk})
        url = "https://belfry.belfry.test/api/v1/guest/save"
        body = b'{"email": "gus@example.org"}'
        host = {"Host": "belfry.belfry.test", "Content-Type": "application/json"}
        r = c.post(
            "/api/v1/guest/save",
            content=body,
            headers={**host, "Authorization": nip98(me, url, "POST", body)},
        )
        assert r.status_code == 202
        text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        sent_code = re.search(r"\b(\d{6})\b", text).group(1)
        vurl = url + "/verify"
        wrong = b'{"email": "gus@example.org", "code": "000000"}'
        r = c.post(
            "/api/v1/guest/save/verify",
            content=wrong,
            headers={**host, "Authorization": nip98(me, vurl, "POST", wrong)},
        )
        assert r.status_code == 422 or sent_code == "000000"
        ok = f'{{"email": "gus@example.org", "code": "{sent_code}"}}'.encode()
        time.sleep(1.1)
        r = c.post(
            "/api/v1/guest/save/verify",
            content=ok,
            headers={**host, "Authorization": nip98(me, vurl, "POST", ok)},
        )
        assert r.status_code == 200, r.text
        partner = rsvc.odoo.read_one("res.partner", core.m2o_id(p["partner_id"]), ["email"])
        assert partner["email"] == "gus@example.org"
        kind, tags = published[-1]
        assert kind == 1985 and ["l", "saved", "belfry/guest"] in tags and ["p", me_pk] in tags
        # someone else's address can't be taken over
        time.sleep(1.1)
        body2 = b'{"email": "admin@belfry.test"}'
        c.post(
            "/api/v1/guest/save",
            content=body2,
            headers={**host, "Authorization": nip98(me, url, "POST", body2)},
        )
        code2 = re.search(
            r"\b(\d{6})\b", mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        ).group(1)
        take = f'{{"email": "admin@belfry.test", "code": "{code2}"}}'.encode()
        r = c.post(
            "/api/v1/guest/save/verify",
            content=take,
            headers={**host, "Authorization": nip98(me, vurl, "POST", take)},
        )
        assert r.status_code == 409
