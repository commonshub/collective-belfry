"""A channel's guests (guests.py): a link from a channel's admin, a guest pass for 30 days that
puts them in that channel only, out again when it closes."""

from __future__ import annotations

import datetime as dt
import json
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
def rsvc(services, tmp_path, settings, monkeypatch):
    mailer.SENT.clear()
    # the channels of these tests are open ones (may_invite asks the relay; none runs here)
    monkeypatch.setattr(guests, "_closed", lambda svc, channel: False)
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
        assert r.json()["days"] == 30 and "/join/" in r.json()["url"]


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
    row = rsvc.odoo.create_guest_invite(
        ada, key.public_key_xonly.format().hex(), "garden", 30, people=50
    )
    token = row["url"].rsplit("/", 1)[1]
    agent = PrivateKey()
    agent_pk = agent.public_key_xonly.format().hex()
    with TestClient(create_app(rsvc)) as c:
        assert c.get(f"/invite/{token}", follow_redirects=False).status_code == 302
        md = c.get(f"/invite/{token}.md")
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
        url = f"https://belfry.belfry.test/api/v1/guest-invites/{token}/accept"
        assert (
            url in md.text
            and "wss://groups-relay.belfry.test" in md.text
            and '"#h": ["garden"]' in md.text
        )
        # how to attach pictures and files: Blossom upload, then an imeta tag
        assert "## 7. Attach pictures and files" in md.text and "/upload" in md.text
        assert "kind 24242" in md.text and '["imeta", "url <url>"' in md.text
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


def test_a_person_joins_with_a_name_and_saves_the_account_later(
    rsvc, published, keycast, monkeypatch
):
    """Scan, name, in: an account in the signer and a session for the chat at once, in the
    answer to the chat's own signed request; "not saved" until the guest proves an e-mail,
    signed with their own identity."""
    from .test_holder_invites import nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    rsvc.settings.nip05_domain = "belfry.test"
    key, ada = admin_with_key(rsvc)
    row = rsvc.odoo.create_guest_invite(
        ada, key.public_key_xonly.format().hex(), "garden", 30, people=50
    )
    token = row["url"].rsplit("/", 1)[1]
    monkeypatch.setattr(guests, "_in_channel", lambda *a: False)
    jhost = {"Host": "belfry.belfry.test", "Content-Type": "application/json"}
    jurl = f"https://belfry.belfry.test/api/v1/channel-invites/{token}/join"

    def join(c, k, body):
        return c.post(
            f"/api/v1/channel-invites/{token}/join",
            content=body,
            headers={**jhost, "Authorization": nip98(k, jurl, "POST", body)},
        )

    with TestClient(create_app(rsvc)) as c:
        # the old page hands over to the chat, which holds the session
        r = c.get(f"/invite/{token}", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == f"https://chat.belfry.test/join/{token}"
        info = c.get(f"/api/v1/channel-invites/{token}", headers=jhost).json()
        assert info["channel"] == "garden" and info["state"] == "open"
        assert info["inviter"] == "Ada Admin"
        session = PrivateKey()  # the chat's own new key signs; no code anywhere
        assert join(c, session, b"{}").json()["error"] == "name_needed"
        got = join(c, session, b'{"name": "Gus"}')
        assert got.status_code == 200 and got.json()["bunker_url"].startswith("bunker://")
        assert got.json()["channel"] == "garden" and got.json()["new"] is True
        # the session is made for the key that signed, in the signer itself (Keycast 0022)
        session_pk = session.public_key_xonly.format().hex()
        assert keycast.bunkers[-1]["client_pubkey"] == session_pk
        # the same device asks again (a second tap, a retry): the same account, announced once
        people, lines = len(rsvc.odoo.search_read("res.partner", [], ["id"])), len(published)
        time.sleep(1.1)
        again = join(c, session, b'{"name": "Gus"}')
        assert again.status_code == 200 and again.json()["pubkey"] == got.json()["pubkey"]
        assert len(rsvc.odoo.search_read("res.partner", [], ["id"])) == people
        assert len(published) == lines
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


def _guest_link(rsvc, monkeypatch, channel="garden"):
    from .test_holder_invites import nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    rsvc.settings.nip05_domain = "belfry.test"
    monkeypatch.setattr(guests, "_in_channel", lambda *a: False)
    key, ada = admin_with_key(rsvc)

    def link(channel=channel):
        row = rsvc.odoo.create_guest_invite(
            ada, key.public_key_xonly.format().hex(), channel, 30, people=50
        )
        return row["url"].rsplit("/", 1)[1]

    def join(c, token, k, name):
        body = json.dumps({"name": name}).encode()
        url = f"https://belfry.belfry.test/api/v1/channel-invites/{token}/join"
        return c.post(
            f"/api/v1/channel-invites/{token}/join",
            content=body,
            headers={
                "Host": "belfry.belfry.test",
                "Content-Type": "application/json",
                "Authorization": nip98(k, url, "POST", body),
            },
        )

    return link, join


def test_a_session_key_gets_one_identity(rsvc, published, keycast, monkeypatch):
    """The signer binds the new session to the key that signed the request, and refuses a key
    it knows as another account's: a second invite can't turn one session key into a second
    identity."""
    link, join = _guest_link(rsvc, monkeypatch)
    first, second = link("garden"), link("kitchen")
    session = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        got = join(c, first, session, "Gus")
        assert got.status_code == 200, got.text
        people, sessions = (
            len(rsvc.odoo.search_read("res.partner", [], ["id"])),
            len(keycast.bunkers),
        )
        time.sleep(1.1)
        # the key is not an account's key (it only holds a session): a new guest is tried
        r = join(c, second, session, "Gus again")
        assert r.status_code == 409 and r.json()["error"] == "session_taken"
        assert len(keycast.bunkers) == sessions
        passes = rsvc.odoo.search_read(
            "belfry.pass", [["guest_channel", "=", "kitchen"]], ["state"]
        )
        assert [p["state"] for p in passes] == ["revoked"]
        assert len(rsvc.odoo.search_read("res.partner", [], ["id"])) == people + 1
        # another device's key joins the second link as usual
        other = join(c, second, PrivateKey(), "Hal")
        assert other.status_code == 200 and other.json()["pubkey"] != got.json()["pubkey"]


def test_a_signer_without_the_binding_still_lets_guests_in(rsvc, published, keycast, monkeypatch):
    """A Keycast image built before patch 0022 ignores ``client_pubkey``: the session is bound
    by the first connect, as it was."""
    keycast.binds = False
    link, join = _guest_link(rsvc, monkeypatch)
    with TestClient(create_app(rsvc)) as c:
        got = join(c, link(), PrivateKey(), "Gus")
        assert got.status_code == 200 and got.json()["bunker_url"].startswith("bunker://")
        assert keycast.bunkers[-1]["client_pubkey"] is None


def test_a_link_is_for_one_person_and_one_day_unless_asked(rsvc, published, monkeypatch):
    from .test_holder_invites import nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.nip05_domain = "belfry.test"
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: True)
    key, _ = admin_with_key(rsvc)
    with TestClient(create_app(rsvc)) as c:

        def make(body):
            return c.post(
                "/api/channel-invites",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": nip98(key, BASE + "/api/channel-invites", "POST", body),
                },
            )

        one = make(b'{"channel": "garden"}').json()
        assert one["people"] == 1 and one["link_days"] == 1
        assert one["url"].startswith("https://chat.belfry.test/join/")
        inv = rsvc.odoo.invite_by_token(one["url"].rsplit("/", 1)[1])
        assert inv["guest_max"] == 1
        left = dt.datetime.fromisoformat(inv["expires_at"].replace(" ", "T")) - dt.datetime.utcnow()
        assert dt.timedelta(hours=23) < left <= dt.timedelta(days=1)
        many = make(b'{"channel": "garden", "people": 50, "link_days": 30}').json()
        assert many["people"] == 50 and many["link_days"] == 30
        assert make(b'{"channel": "garden", "people": 7}').status_code == 422


def test_someone_with_an_account_joins_with_that_account(rsvc, published, keycast, monkeypatch):
    """No second account: a member is put in the channel; a guest gets the channel too."""
    from .test_holder_invites import nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    rsvc.settings.nip05_domain = "belfry.test"
    key, ada = admin_with_key(rsvc)
    ada_pk = key.public_key_xonly.format().hex()
    inside: set[tuple[str, str]] = set()
    monkeypatch.setattr(guests, "_in_channel", lambda svc, ch, pk: (ch, pk) in inside)
    host = {"Host": "belfry.belfry.test", "Content-Type": "application/json"}

    def join(c, token, k, body=b"{}"):
        url = f"https://belfry.belfry.test/api/v1/channel-invites/{token}/join"
        time.sleep(1.1)  # the same signed request twice in a second is a replay
        return c.post(
            f"/api/v1/channel-invites/{token}/join",
            content=body,
            headers={**host, "Authorization": nip98(k, url, "POST", body)},
        )

    def link(channel, people=1):
        row = rsvc.odoo.create_guest_invite(ada, ada_pk, channel, 30, people=people)
        return row["url"].rsplit("/", 1)[1]

    with TestClient(create_app(rsvc)) as c:
        # a member (here: another member's link would do the same) joins as themselves
        bea = steward.steward_pass(rsvc, "bea@belfry.test", "Bea")
        bea_key = PrivateKey()
        bea_pk = bea_key.public_key_xonly.format().hex()
        rsvc.odoo.write("belfry.pass", [bea["pass_id"]], {"keycast_pubkey": bea_pk})
        people_before = len(rsvc.odoo.search_read("res.partner", [], ["id"]))
        t1 = link("garden")
        got = join(c, t1, bea_key).json()
        assert got == {"channel": "garden", "pubkey": bea_pk, "new": False, "guest": False}
        kind, tags = published[-1]
        assert kind == 9000 and ["p", bea_pk, "member"] in tags and ["name", "Bea"] in tags
        assert ["invited-by", ada_pk] in tags
        assert rsvc.odoo.invite_by_token(t1)["state"] == "used"  # it was for one person
        assert len(rsvc.odoo.search_read("res.partner", [], ["id"])) == people_before
        # in it already: it just opens, even with a spent link, and nothing is published
        inside.add(("garden", bea_pk))
        n = len(published)
        assert join(c, t1, bea_key).json()["already"] is True and len(published) == n
        # a spent link, and not in the channel: refused
        inside.clear()
        assert join(c, t1, bea_key).status_code == 410

        # a guest of one channel is invited to another: the same account, one more channel
        t2 = link("garden", people=50)
        session = PrivateKey()
        gus = join(c, t2, session, b'{"name": "Gus"}').json()
        gus_key = PrivateKey()  # stands in for the guest's identity (the signer holds the real one)
        gus_pk = gus_key.public_key_xonly.format().hex()
        p = rsvc.odoo.search_read("belfry.pass", [["keycast_pubkey", "=", gus["pubkey"]]], ["id"])
        rsvc.odoo.write("belfry.pass", [p[0]["id"]], {"keycast_pubkey": gus_pk})
        people_before = len(rsvc.odoo.search_read("res.partner", [], ["id"]))
        t3 = link("retreat")
        got = join(c, t3, gus_key).json()
        assert got["channel"] == "retreat" and got["new"] is False and got["guest"] is True
        kind, tags = published[-1]
        assert kind == 9000 and ["h", "retreat"] in tags and ["p", gus_pk, "member"] in tags
        assert len(rsvc.odoo.search_read("res.partner", [], ["id"])) == people_before
        passes = rsvc.odoo.search_read(
            "belfry.pass",
            [["keycast_pubkey", "=", gus_pk], ["state", "=", "active"]],
            ["guest_channel"],
        )
        assert sorted(x["guest_channel"] for x in passes) == ["garden", "retreat"]
        assert rsvc.odoo.invite_by_token(t3)["state"] == "used"
        n = len(published)
        assert join(c, t2, gus_key).json()["already"] is True and len(published) == n
        assert gus_pk not in core.active_pubkeys(rsvc)  # still a guest


def test_an_admin_makes_a_saved_guest_an_admin(rsvc, published, keycast, monkeypatch):
    """A guest with an e-mail on their account becomes a member with the admin role: the same
    key, one membership instead of the guest pass, still in the channel. Without an e-mail the
    admin is told why not; an agent never."""
    from belfry_onboarding import admins, apps
    from belfry_onboarding.holder_invites import ApiError
    from belfry_onboarding.places import member_key

    stewards = {"admin@belfry.test"}
    monkeypatch.setattr(console, "admin_emails", lambda svc: set(stewards))

    def set_admin(svc, email, admin):
        if admin:
            stewards.add(email)
            steward.steward_pass(svc, email)
        else:
            stewards.discard(email)

    monkeypatch.setattr(console, "set_admin", set_admin)
    monkeypatch.setattr(guests, "_in_channel", lambda *a: False)
    key, ada = admin_with_key(rsvc)
    ada_pk = key.public_key_xonly.format().hex()
    token = rsvc.odoo.create_guest_invite(ada, ada_pk, "garden", 30, people=50)["url"].rsplit(
        "/", 1
    )[1]
    gus = guests.join_new(rsvc, token, "Gus", PrivateKey().public_key_xonly.format().hex())
    gus_pk = gus["pubkey"]
    apps.forget()
    with pytest.raises(ApiError) as e:
        admins.change(rsvc, ada_pk, gus_pk, True)
    assert e.value.error == "guest_not_saved"
    guest_pass = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", gus_pk]], ["id", "partner_id"]
    )[0]
    rsvc.odoo.write(
        "res.partner", [core.m2o_id(guest_pass["partner_id"])], {"email": "gus@example.org"}
    )
    lines = len(published)
    assert gus_pk in admins.change(rsvc, ada_pk, gus_pk, True)
    assert member_key(rsvc, gus_pk)["tier"] == "member"  # the same key, now a member's
    active = rsvc.odoo.search_read(
        "belfry.pass", [["keycast_pubkey", "=", gus_pk], ["state", "=", "active"]], ["tier"]
    )
    assert [p["tier"] for p in active] == ["member"]  # the guest pass closed quietly
    told = published[lines:]
    assert (
        1985,
        [
            ["h", "garden"],
            ["L", "belfry/guest"],
            ["l", "member", "belfry/guest"],
            ["p", gus_pk],
        ],
    ) in told
    assert not [k for k, _ in told if k == 9001]  # and they stay in the channel
    assert gus_pk in core.active_pubkeys(rsvc)


def test_a_member_starts_with_their_name_as_username_and_changes_it(rsvc, keycast, monkeypatch):
    """Not the member number: "Friedger Müffke" is friedger-muffke@; a second one gets -2; the
    member picks another name from the chat; reserved names and channels' names are refused."""
    from belfry_onboarding import mail, usernames
    from belfry_onboarding.holder_invites import ApiError

    monkeypatch.setattr(mail, "channel_exists", lambda svc, name: name == "garden")
    assert usernames.slug("Friedger Müffke ") == "friedger-muffke"
    assert usernames.slug("  ¡Ana María!  ") == "ana-maria"
    assert usernames.slug("李") == ""
    a = steward.steward_pass(rsvc, "f1@example.org", "Friedger Müffke")
    names = {u["pubkey"]: u["username"] for u in keycast.users.values()}
    row = rsvc.odoo.read_one("belfry.pass", a["pass_id"], ["keycast_pubkey", "state"])
    assert row["state"] == "active" and names[row["keycast_pubkey"]] == "friedger-muffke"
    b = steward.steward_pass(rsvc, "f2@example.org", "Friedger Muffke")
    row2 = rsvc.odoo.read_one("belfry.pass", b["pass_id"], ["keycast_pubkey"])
    names = {u["pubkey"]: u["username"] for u in keycast.users.values()}
    assert names[row2["keycast_pubkey"]] == "friedger-muffke-2"
    # the second member changes theirs (the fake signer answers for the last account it saw)
    pk = row2["keycast_pubkey"]
    assert usernames.change(rsvc, pk, "Fried")["nip05"].startswith("fried@")
    for bad, code in (
        ("garden", "taken"),
        ("admin", "taken"),
        ("friedger-muffke", "taken"),
        ("-x", "invalid"),
        ("a b", "invalid"),
    ):
        with pytest.raises(ApiError) as e:
            usernames.change(rsvc, pk, bad)
        assert e.value.error == code, bad
    with pytest.raises(ApiError) as e:
        usernames.change(rsvc, "ab" * 32, "someone")
    assert e.value.error == "not_member"
    # a chosen name is never replaced by the default again
    assert usernames.give_default(rsvc, "ucan-token", "Friedger Muffke", "0002") == "fried"


class OneMint:
    def __init__(self):
        self.minted = []

    def mint_token(self, amount):
        self.minted.append(amount)
        return "cashuA-test"


def test_a_guest_pass_brings_no_tokens(rsvc, published, authentik, keycast):
    """A guest's 30 days counted as 31 pass days of tokens when the belfry has a mint."""
    rsvc.mint = OneMint()
    today = dt.date.today()
    assert core.token_allowance(rsvc, "guest", None, today, today + dt.timedelta(days=30)) == 0
    reg = rsvc.odoo
    pid = reg.create("res.partner", {"name": "Gus Guest", "email": "gus@example.org"})
    pass_id = reg.create(
        "belfry.pass", {"partner_id": pid, "tier": "guest", "guest_channel": "garden"}
    )
    assert core.activate_free_pass(rsvc, pass_id) == "activated"
    row = reg.read_one("belfry.pass", pass_id, ["state", "cashu_amount"])
    assert row["state"] == "active" and not row["cashu_amount"] and rsvc.mint.minted == []


def test_passes_that_ended_are_closed_by_the_service_itself(rsvc, published, authentik, keycast):
    """With the belfry's own register no cron runs `expire`: the background round does."""
    from belfry_onboarding import followers

    reg = rsvc.odoo
    pid = reg.create("res.partner", {"name": "Gus Guest"})
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    pass_id = reg.create(
        "belfry.pass",
        {
            "partner_id": pid,
            "tier": "guest",
            "guest_channel": "garden",
            "guest_agent": True,  # its own key: no account in the signer to suspend
            "state": "active",
            "keycast_pubkey": "c" * 64,
            "date_end": yesterday,
        },
    )
    followers._expired_at = 0.0
    assert followers.expire_passes(rsvc) == {"expired": [pass_id]}
    assert reg.read_one("belfry.pass", pass_id, ["state"])["state"] == "expired"
    assert (9001, [["h", "garden"], ["p", "c" * 64], ["guest", "ended"]]) in published
    assert followers.expire_passes(rsvc) is None  # not again within the hour


def test_only_someone_who_has_the_channel_makes_a_guest_link(rsvc, monkeypatch):
    """Any member for an open channel; a closed one only its members and admins; the community's
    admins always; nobody but an admin when the relay can't say."""
    from belfry_onboarding import apps

    state = {"closed": True, "inside": set(), "admins": set(), "belfry_admins": set()}
    monkeypatch.setattr(guests, "_closed", lambda svc, ch: state["closed"])
    monkeypatch.setattr(guests, "_in_channel", lambda svc, ch, pk: pk in state["inside"])
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: state["admins"])
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: state["belfry_admins"])
    me = "aa" * 32
    assert guests.may_invite(rsvc, me, "pod-x", False) is False  # a closed pod I am not in
    state["inside"] = {me}
    assert guests.may_invite(rsvc, me, "pod-x", False) is True
    state["inside"], state["admins"] = set(), {me}
    assert guests.may_invite(rsvc, me, "pod-x", False) is True
    state["admins"], state["belfry_admins"] = set(), {me}
    assert guests.may_invite(rsvc, me, "pod-x", False) is True
    state["belfry_admins"], state["closed"] = set(), False
    assert guests.may_invite(rsvc, me, "garden", False) is True  # an open channel: any member
    state["closed"] = None
    assert guests.may_invite(rsvc, me, "garden", False) is False  # the relay can't say
    assert guests.may_invite(rsvc, me, "garden", True) is True
