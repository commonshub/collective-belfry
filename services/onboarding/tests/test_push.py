"""Push notifications (push.py, docs/push.md): NIP-44 with the official vectors, the devices and
settings API, who gets what from the watcher, the gateway client and the subscription loop."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx
import pytest
import respx
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import core, followers, mentions, push
from belfry_onboarding.app import create_app
from belfry_onboarding.blossom import event_id
from belfry_onboarding.nip46check import conversation_key, nip44_decrypt, nip44_encrypt
from belfry_onboarding.nostr import hex_to_npub
from belfry_onboarding.wallet_tokens import check_nip98

from .test_agents import guest_agent, member, pk, world  # noqa: F401 — fixtures
from .test_guests import admin_with_key, published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import post as _post

_tick = iter(range(10_000))


def post(c, key, path, data):
    """NIP-98 events are accepted once: the same body by the same key in the same second would
    be the same event, so each call here is dated a second apart (within the minute allowed)."""
    return _post(c, key, path, data, created_at=int(time.time()) - next(_tick) % 50)


VECTORS = json.loads((Path(__file__).parent / "data" / "nip44.vectors.json").read_text())["v2"]


# ── NIP-44 v2 ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("v", VECTORS["valid"]["get_conversation_key"])
def test_nip44_conversation_key_vectors(v):
    got = conversation_key(bytes.fromhex(v["sec1"]), bytes.fromhex(v["pub2"]))
    assert got.hex() == v["conversation_key"]


@pytest.mark.parametrize("v", VECTORS["valid"]["encrypt_decrypt"])
def test_nip44_encrypt_decrypt_vectors(v):
    # the conversation key used directly, as push.encrypt does with the device's key
    ck = bytes.fromhex(v["conversation_key"])
    assert conversation_key(bytes.fromhex(v["sec1"]), _pub(v["sec2"])) == ck
    assert nip44_encrypt(ck, v["plaintext"], bytes.fromhex(v["nonce"])) == v["payload"]
    assert nip44_decrypt(ck, v["payload"]) == v["plaintext"]


@pytest.mark.parametrize("v", VECTORS["invalid"]["decrypt"])
def test_nip44_invalid_payloads_are_refused(v):
    with pytest.raises(Exception):  # noqa: B017 — any refusal will do
        nip44_decrypt(bytes.fromhex(v["conversation_key"]), v["payload"])


def _pub(sec_hex: str) -> bytes:
    return PrivateKey(bytes.fromhex(sec_hex)).public_key_xonly.format()


# ── devices and settings ────────────────────────────────────────────────────────────────────
def device(token="a" * 64, platform="apns", app="belfry-ios", key: bytes | None = None):
    return {
        "platform": platform,
        "token": token,
        "env": "sandbox",
        "key": base64.b64encode(key or os.urandom(32)).decode(),
        "app": app,
        "name": "Ada's  iPhone",
    }


def test_devices_add_list_replace_remove(world):
    rsvc, _published, _left = world
    ada, _ = member(rsvc, "ada@belfry.test", "Ada")
    bob, _ = member(rsvc, "bob@belfry.test", "Bob")
    stranger = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, "/api/push/devices", {"add": device()})
        assert r.status_code == 200, r.text
        dev_id = r.json()["id"]
        r = post(c, ada, "/api/push/devices", {})
        assert r.json()["devices"] == [
            {
                "id": dev_id,
                "app": "belfry-ios",
                "name": "Ada's iPhone",
                "since": r.json()["devices"][0]["since"],
            }
        ]
        # the same token from Bob (two identities on one phone): one row each
        assert post(c, bob, "/api/push/devices", {"add": device()}).status_code == 200
        assert len(post(c, ada, "/api/push/devices", {}).json()["devices"]) == 1
        assert len(post(c, bob, "/api/push/devices", {}).json()["devices"]) == 1
        # the same token and key again replaces that identity's row only
        assert post(c, bob, "/api/push/devices", {"add": device()}).status_code == 200
        assert len(rsvc.odoo.push_devices()) == 2
        # Ada signing out removes only hers
        assert post(c, ada, "/api/push/devices", {"remove": "a" * 64}).json() == {"ok": True}
        assert post(c, ada, "/api/push/devices", {}).json() == {"devices": []}
        assert len(post(c, bob, "/api/push/devices", {}).json()["devices"]) == 1
        assert post(c, bob, "/api/push/devices", {"remove": "a" * 64}).json() == {"ok": True}
        assert post(c, bob, "/api/push/devices", {}).json() == {"devices": []}
        # bad input; a key with no pass
        bad = device() | {"key": base64.b64encode(b"short").decode()}
        assert post(c, ada, "/api/push/devices", {"add": bad}).status_code == 400
        assert (
            post(c, ada, "/api/push/devices", {"add": device() | {"env": "x"}}).status_code == 400
        )
        assert post(c, stranger, "/api/push/devices", {}).status_code == 403


def test_a_guest_registers_too_and_the_v1_path_works(world):
    rsvc, _published, _left = world
    rsvc.settings.console_url = "https://belfry.belfry.test"
    ada, ada_id = member(rsvc, "ada@belfry.test", "Ada")
    with TestClient(create_app(rsvc)) as c:
        bot = guest_agent(c, rsvc, ada, ada_id)
        body = json.dumps({"add": device(platform="fcm", app="belfry-android")}).encode()
        from .test_holder_invites import nip98

        url = "https://belfry.belfry.test/api/v1/push/devices"
        r = c.post(
            "/api/v1/push/devices",
            content=body,
            headers={
                "Host": "belfry.belfry.test",
                "Authorization": nip98(bot, url, "POST", body),
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200, r.text


def test_settings_defaults_merge_and_validation(world):
    rsvc, _published, _left = world
    ada, _ = member(rsvc, "ada@belfry.test", "Ada")
    with TestClient(create_app(rsvc)) as c:
        r = post(c, ada, "/api/notifications", {})
        assert r.json() == {
            "dms": True,
            "mentions": True,
            "announcements": True,
            "pins": True,
            "channels": {},
        }
        r = post(
            c,
            ada,
            "/api/notifications",
            {"set": {"pins": False, "channels": {"garden": "all", "x": "off"}}},
        )
        assert r.json()["pins"] is False and r.json()["channels"] == {"garden": "all", "x": "off"}
        r = post(c, ada, "/api/notifications", {"set": {"channels": {"x": "default"}}})
        assert r.json()["channels"] == {"garden": "all"} and r.json()["pins"] is False
        for bad in ({"pins": "no"}, {"channels": {"x": "loud"}}, {"what": True}, {"channels": []}):
            assert post(c, ada, "/api/notifications", {"set": bad}).status_code == 400


def test_allowed_follows_the_levels_and_switches():
    on = push.DEFAULTS | {"channels": {"a": "all", "m": "mentions", "o": "off"}}
    assert push.allowed(on, "d", "message") is None
    assert push.allowed(on, "a", "message") == "message"
    for kind in ("mention", "reply", "announcement", "pin"):
        assert push.allowed(on, "d", kind) == kind
        assert push.allowed(on, "o", kind) is None
    assert push.allowed(on, "m", "mention") == "mention"
    assert push.allowed(on, "m", "announcement") is None
    assert push.allowed(on, "m", "pin") is None
    off = on | {"mentions": False, "announcements": False, "pins": False, "dms": False}
    assert push.allowed(off, "d", "mention") is None
    assert push.allowed(off, "a", "mention") == "message"  # "all" still brings every message
    assert push.allowed(off, "a", "pin") is None
    assert push.allowed(off, "", "dm") is None


# ── the watcher ─────────────────────────────────────────────────────────────────────────────
class FakeGateway:
    def __init__(self, answer="ok"):
        self.answer = answer
        self.sent: list[tuple[dict, str, str, bool]] = []

    def send(self, dev, payload, collapse, urgent):
        self.sent.append((dev, payload, collapse, urgent))
        return self.answer


def ev(author: PrivateKey, kind: int, tags: list, content: str = "", at: int | None = None):
    e = {
        "pubkey": pk(author),
        "created_at": int(time.time()) if at is None else at,
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    e["id"] = event_id(e)
    return e


@pytest.fixture
def scene(world, monkeypatch):
    """Ada (admin of #garden), Bob and Cy (members), Dee (member, not in #garden): all with a
    phone; the groups relay is a list of events."""
    rsvc, _published, _left = world
    people = {n: member(rsvc, f"{n.lower()}@belfry.test", n) for n in ("Ada", "Bob", "Cy", "Dee")}
    keys = {n: k for n, (k, _) in people.items()}
    devkeys = {}
    for n, (k, partner) in people.items():
        devkeys[n] = os.urandom(32)
        d = device(token=n.lower() * 20, key=devkeys[n])
        rsvc.odoo.push_add(
            push._device(d) | {"pubkey": pk(k), "partner_id": partner}, int(time.time())
        )
    relay = [
        ev(keys["Ada"], 9007, [["h", "garden"]], at=1),
        ev(keys["Ada"], 39001, [["d", "garden"], ["p", pk(keys["Ada"])]], at=1),
        ev(
            keys["Ada"],
            39002,
            [["d", "garden"]] + [["p", pk(keys[n])] for n in ("Ada", "Bob", "Cy")],
            at=1,
        ),
    ]
    gw = FakeGateway()
    w = push.Watcher(rsvc, gw)

    def query(filters):
        out = []
        for f in filters:
            for e in relay:
                if "ids" in f and e["id"] not in f["ids"]:
                    continue
                if "kinds" in f and e["kind"] not in f["kinds"]:
                    continue
                if all(
                    any(t[0] == k[1:] and t[1] in vals for t in e["tags"])
                    for k, vals in f.items()
                    if k.startswith("#")
                ):
                    out.append(e)
        return out

    monkeypatch.setattr(w, "groups_query", query)
    monkeypatch.setattr(mentions, "_names", lambda svc, ks: {pk(keys[n]): n for n in keys})
    monkeypatch.setattr(followers, "channel_label", lambda svc, h: f"#{h}")
    return rsvc, keys, devkeys, relay, gw, w


def opened(gw, devkeys, name):
    """The plaintext of the last notification to NAME's phone."""
    for dev, payload, _c, _u in reversed(gw.sent):
        if dev["token"] == name.lower() * 20:
            return json.loads(nip44_decrypt(devkeys[name], payload))
    return None


def test_a_message_reaches_members_at_level_all_only(scene):
    rsvc, keys, devkeys, relay, gw, w = scene
    bob_partner = rsvc.odoo.push_devices([pk(keys["Bob"])])[0]["partner_id"]
    push.set_settings(rsvc, bob_partner, {"channels": {"garden": "all"}})
    e = ev(keys["Ada"], 9, [["h", "garden"]], "hello all")
    assert w.handle(e, "groups") == [(pk(keys["Bob"]), "message")]
    plain = opened(gw, devkeys, "Bob")
    assert plain == {
        "v": 1,
        "domain": rsvc.settings.nip05_domain,
        "type": "message",
        "title": "#garden",
        "body": "Ada: hello all",
        "channel": "garden",
        "event": e["id"],
        "from": pk(keys["Ada"]),
        "url": f"https://chat.{rsvc.settings.nip05_domain}/#/c/garden/m/{e['id']}",
    }
    assert gw.sent[0][2] == "garden" and gw.sent[0][3] is False  # collapse, not urgent
    # seen once; an old one never
    assert w.handle(e, "groups") == []
    assert (
        w.handle(ev(keys["Ada"], 9, [["h", "garden"]], "x", at=int(time.time()) - 900), "groups")
        == []
    )


def test_mentions_replies_threads_announcements(scene):
    rsvc, keys, devkeys, relay, gw, w = scene
    bob, cy, dee = pk(keys["Bob"]), pk(keys["Cy"]), pk(keys["Dee"])
    # a p tag and a nostr:npub in the text; Dee is not in #garden
    e = ev(keys["Ada"], 9, [["h", "garden"], ["p", bob], ["p", dee]], f"hi nostr:{hex_to_npub(cy)}")
    got = dict(w.handle(e, "groups"))
    assert got == {bob: "mention", cy: "mention"}
    assert opened(gw, devkeys, "Cy")["body"] == "Ada: hi @Cy"
    assert gw.sent[-1][3] is True
    # Bob answers Cy's message in a thread: Cy gets a reply (the p naming her is no mention)
    root = ev(keys["Cy"], 9, [["h", "garden"]], "root")
    relay.append(root)
    reply = ev(
        keys["Bob"],
        1111,
        [
            ["h", "garden"],
            ["E", root["id"], "", cy],
            ["P", cy],
            ["e", root["id"], "", cy],
            ["p", cy],
        ],
        "yes",
    )
    relay.append(reply)
    assert w.handle(reply, "groups") == [(cy, "reply")]
    assert opened(gw, devkeys, "Cy")["url"].endswith(f"/thread/{root['id']}")
    # Ada joins the thread: Cy (root) and Bob (wrote in it) hear about it
    more = ev(keys["Ada"], 1111, [["h", "garden"], ["E", root["id"], "", cy], ["p", cy]], "me too")
    assert dict(w.handle(more, "groups")) == {cy: "reply", bob: "reply"}
    # a quote without an author hint: looked up
    q = ev(keys["Ada"], 9, [["h", "garden"], ["q", root["id"]]], "see this")
    assert w.handle(q, "groups") == [(cy, "reply")]
    # an announcement by the admin reaches everyone; by Bob it's a message (default: nothing)
    a = ev(keys["Ada"], 9, [["h", "garden"], ["t", "announcement"]], "big news")
    assert dict(w.handle(a, "groups")) == {bob: "announcement", cy: "announcement"}
    assert opened(gw, devkeys, "Bob")["title"] == "📣 #garden"
    b = ev(keys["Bob"], 9, [["h", "garden"], ["t", "announcement"]], "not really")
    assert w.handle(b, "groups") == []


def test_pins_and_switches(scene):
    rsvc, keys, devkeys, relay, gw, w = scene
    bob, cy = pk(keys["Bob"]), pk(keys["Cy"])
    msg = ev(keys["Bob"], 9, [["h", "garden"]], "the rules")
    relay.append(msg)
    ns = push.PIN_NS
    pin = ev(keys["Ada"], 1985, [["h", "garden"], ["L", ns], ["l", "pinned", ns], ["e", msg["id"]]])
    cy_partner = rsvc.odoo.push_devices([cy])[0]["partner_id"]
    push.set_settings(rsvc, cy_partner, {"pins": False})
    assert w.handle(pin, "groups") == [(bob, "pin")]
    plain = opened(gw, devkeys, "Bob")
    assert plain["title"] == "📌 #garden" and plain["body"] == "Bob: the rules"
    assert plain["event"] == msg["id"]
    # a pin by a non-admin, or an unpin, notifies nobody
    by_bob = ev(
        keys["Bob"], 1985, [["h", "garden"], ["L", ns], ["l", "pinned", ns], ["e", msg["id"]]]
    )
    assert w.handle(by_bob, "groups") == []
    unpin = ev(
        keys["Ada"], 1985, [["h", "garden"], ["L", ns], ["l", "unpinned", ns], ["e", msg["id"]]]
    )
    assert w.handle(unpin, "groups") == []
    # channel off
    push.set_settings(rsvc, cy_partner, {"channels": {"garden": "off"}})
    m = ev(keys["Ada"], 9, [["h", "garden"], ["p", cy]], "ping")
    assert w.handle(m, "groups") == []  # Cy has it off; for Bob it's a message (default)


def test_dms_only_live_and_a_dead_token_goes(scene):
    rsvc, keys, devkeys, relay, gw, w = scene
    bob = pk(keys["Bob"])
    wrap = ev(PrivateKey(), 1059, [["p", bob]], "…", at=int(time.time()) - 86400)  # backdated
    assert w.handle(wrap, "community", live=False) == []
    wrap2 = ev(PrivateKey(), 1059, [["p", bob]], "…", at=int(time.time()) - 86400)
    gw.answer = "gone"
    assert w.handle(wrap2, "community") == []
    assert rsvc.odoo.push_devices([bob]) == []  # removed on 410
    gw.answer = "ok"
    cy = pk(keys["Cy"])
    wrap3 = ev(PrivateKey(), 1059, [["p", cy]], "…")
    assert w.handle(wrap3, "community") == [(cy, "dm")]
    plain = opened(gw, devkeys, "Cy")
    assert plain["type"] == "dm" and plain["body"] == "New message" and "from" not in plain
    assert plain["url"] == f"https://chat.{rsvc.settings.nip05_domain}/#/dm"
    assert gw.sent[-1][2] == "dm"


def test_devices_go_with_the_pass_and_when_stale(scene):
    rsvc, keys, devkeys, relay, gw, w = scene
    bob, cy = pk(keys["Bob"]), pk(keys["Cy"])
    row = rsvc.odoo.search_read("belfry.pass", [["keycast_pubkey", "=", bob]], ["id"])[0]
    core.revoke_pass(rsvc, row["id"], "test")
    assert rsvc.odoo.push_devices([bob]) == []
    assert len(rsvc.odoo.push_devices([cy])) == 1
    # a device of a key without a pass (the hook missed it) goes with the hourly prune
    stray = push._device(device(token="s" * 30)) | {"pubkey": "f" * 64, "partner_id": 99}
    rsvc.odoo.push_add(stray, int(time.time()))
    assert push.prune(rsvc) == 1 and len(rsvc.odoo.push_devices()) == 3
    assert push.prune(rsvc, now=time.time() + 61 * 86400) == 3  # Ada's, Cy's, Dee's: stale
    assert rsvc.odoo.push_devices() == []


def test_fit_keeps_the_plaintext_small():
    plain = {"v": 1, "type": "message", "title": "t" * 5000, "body": "b" * 5000}
    out = push.fit(plain)
    assert len(out["body"]) <= push.BODY_CHARS
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= push.MAX_PLAIN


# ── the gateway client ──────────────────────────────────────────────────────────────────────
def test_gateway_signs_with_the_belfry_key_and_maps_answers(world):
    rsvc, _published, _left = world
    belfry = PrivateKey()
    rsvc.settings.belfry_nsec = belfry.secret.hex()
    rsvc.settings.push_gateway_url = "https://push.gw.test"
    seen = []
    with respx.mock(assert_all_called=False) as mock:

        def answer(request):
            seen.append(request)
            status = {"dead": 410, "busy": 429}.get(json.loads(request.content)["token"], 200)
            return httpx.Response(status, json={})

        mock.post("https://push.gw.test/v1/send").mock(side_effect=answer)
        g = push.Gateway(rsvc)
        dev = {"platform": "apns", "token": "live", "env": "production"}
        assert g.send(dev, "cipher", "garden", True) == "ok"
        assert g.send(dev | {"token": "dead"}, "cipher", "garden", True) == "gone"
        assert g.send(dev | {"token": "busy"}, "cipher", "garden", True) == "rate"
    r = seen[0]
    event = json.loads(base64.b64decode(r.headers["authorization"][6:]))
    assert check_nip98(event, "POST", ["https://push.gw.test/v1/send"]) == pk(belfry)
    import hashlib

    assert ["payload", hashlib.sha256(r.content).hexdigest()] in event["tags"]
    assert json.loads(r.content) == {
        "platform": "apns",
        "token": "live",
        "env": "production",
        "payload": "cipher",
        "collapse": "garden",
        "urgent": True,
    }


# ── the subscription loop ───────────────────────────────────────────────────────────────────
class FakeSocket:
    def __init__(self, script, sent, stop=None):
        self.script = list(script)
        self.sent = sent
        self.stop = stop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        await asyncio.sleep(0)
        if not self.script:
            if self.stop is not None:
                self.stop.set()
            raise ConnectionError("closed")
        item = self.script.pop(0)
        return json.dumps(item(self.sent) if callable(item) else item)


def test_subscribe_answers_auth_and_reconnects():
    key = PrivateKey()
    from belfry_onboarding.nip46check import sign_event

    sign = lambda e: sign_event(key.secret, e)  # noqa: E731
    old = {"id": "1" * 64, "kind": 9}
    new = {"id": "2" * 64, "kind": 9}
    later = {"id": "3" * 64, "kind": 9}

    def auth_ok(sent):
        return ["OK", sent[-1][1]["id"], True, ""]

    sent1: list = []
    sent2: list = []
    got: list = []
    sleeps: list = []

    async def run():
        stop = asyncio.Event()
        sockets = [
            FakeSocket(
                [
                    ["AUTH", "chal"],
                    ["CLOSED", "belfry-push", "auth-required: sign in"],
                    auth_ok,
                    ["EVENT", "belfry-push", old],
                    ["EOSE", "belfry-push"],
                    ["EVENT", "belfry-push", new],
                ],
                sent1,
            ),
            FakeSocket([["EVENT", "belfry-push", later], ["EOSE", "belfry-push"]], sent2, stop),
        ]

        async def on_event(e, live, first):
            got.append((e["id"][0], live, first))

        async def sleep(s):
            sleeps.append(s)

        await push.subscribe(
            "wss://relay.test",
            lambda first: [{"kinds": [9], "since": 1}],
            sign,
            on_event,
            connect=lambda url: sockets.pop(0),
            sleep=sleep,
            stop=stop,
        )

    asyncio.run(run())
    assert got == [("1", False, True), ("2", True, True), ("3", False, False)]
    # REQ, AUTH (signed for this relay and challenge), REQ again after the OK
    assert [m[0] for m in sent1] == ["REQ", "AUTH", "REQ"]
    assert ["challenge", "chal"] in sent1[1][1]["tags"] and sent1[1][1]["kind"] == 22242
    assert len(sleeps) == 1 and 0.8 <= sleeps[0] <= 1.2


def test_runner_skips_history_on_the_first_connection(scene):
    rsvc, keys, devkeys, relay, gw, w = scene
    runner = push.Runner(rsvc, w)

    async def run():
        on = runner._enqueue("groups")
        old = ev(keys["Ada"], 9, [["h", "garden"]], "old")
        await on(old, False, True)  # history, first connection: only marked seen
        assert runner.queue.empty() and not w.first_time(old["id"])
        await on(ev(keys["Ada"], 9, [["h", "garden"]], "gap"), False, False)  # after a reconnect
        await on(ev(keys["Ada"], 9, [["h", "garden"]], "new"), True, True)
        assert runner.queue.qsize() == 2

    asyncio.run(run())
    f = runner.community_filters(True)[0]
    assert f["kinds"] == [1059] and sorted(f["#p"]) == sorted(pk(k) for k in keys.values())


def test_an_old_one_row_per_token_table_is_rebuilt(tmp_path):
    import sqlite3

    from belfry_onboarding.register import Register

    path = tmp_path / "register.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE push_devices (
            id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, platform TEXT NOT NULL,
            env TEXT NOT NULL, key TEXT NOT NULL, app TEXT NOT NULL, name TEXT NOT NULL,
            pubkey TEXT NOT NULL, partner_id INTEGER NOT NULL, since TEXT NOT NULL,
            seen INTEGER NOT NULL);
        INSERT INTO push_devices VALUES ('d1', 'tok', 'fcm', 'production', 'k', 'belfry-android',
            'Tablet', 'aa', 1, '2026-11-11', 1);
        """
    )
    db.commit()
    db.close()
    reg = Register(path, type("S", (), {"nip05_domain": "belfry.test"})())
    assert [d["id"] for d in reg.push_devices()] == ["d1"]
    dev = {"token": "tok", "platform": "fcm", "env": "production", "key": "k2"}
    dev |= {"app": "belfry-android", "name": "Tablet", "pubkey": "bb", "partner_id": 2}
    reg.push_add(dev, 1)
    assert sorted(d["pubkey"] for d in reg.push_devices()) == ["aa", "bb"]
    assert reg.push_remove("tok") == 2  # a dead token goes for every identity
