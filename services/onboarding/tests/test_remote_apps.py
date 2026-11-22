"""Remote apps (remoteapps.py, docs/remote-apps.md): a member signs in to a third-party Nostr
app with a ``nostrconnect://`` code; the belfry is that app's remote signer, on the app's
relays, with the member's key held by the signer. No network: fake relays, a fake signer with
real keys."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import community, core, remoteapps
from belfry_onboarding.app import create_app
from belfry_onboarding.blossom import event_id, verify_signature
from belfry_onboarding.nip46check import (
    conversation_key,
    nip44_decrypt,
    nip44_encrypt,
    sign_event,
)
from belfry_onboarding.remoteapps import Hub, Own, UriError, allowed, parse_uri
from belfry_onboarding.settings import Settings

from .fakes import nip04_decrypt, nip04_encrypt
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures

R1 = "wss://relay.example.org"
R2 = "wss://nos.example.net"
K = "ab" * 32
KS = [pk(PrivateKey((i + 1).to_bytes(32, "big"))) for i in range(16)]  # more client keys


def uri(client: str = K, relays=(R1, R2), secret: str | None = "s3cret", **more) -> str:
    q = [("relay", r) for r in relays]
    if secret is not None:
        q.append(("secret", secret))
    q += list(more.items())
    return f"nostrconnect://{client}?" + urllib.parse.urlencode(q)


# ── fake relays ─────────────────────────────────────────────────────────────────────────────
class Sock:
    def __init__(self, relay: Relay):
        self.relay = relay
        self.inbox: asyncio.Queue[str] | None = None
        self.filter: dict | None = None

    async def __aenter__(self):
        if self.relay.down:
            raise ConnectionError("down")
        self.inbox = asyncio.Queue()
        self.relay.opened += 1
        self.relay.socks.append(self)
        if self.relay.challenge:
            self.inbox.put_nowait(json.dumps(["AUTH", self.relay.challenge]))
        return self

    async def __aexit__(self, *a):
        if self in self.relay.socks:
            self.relay.socks.remove(self)
        return False

    async def send(self, data: str) -> None:
        msg = json.loads(data)
        r = self.relay
        r.sent.append(msg)
        if msg[0] == "REQ":
            if r.challenge and not r.authed:
                self.inbox.put_nowait(json.dumps(["CLOSED", msg[1], "auth-required: who?"]))
                return
            self.filter = msg[2]
            r.filters.append(msg[2])
            self.inbox.put_nowait(json.dumps(["EOSE", msg[1]]))
        elif msg[0] == "AUTH":
            r.authed.append(msg[1])
            self.inbox.put_nowait(json.dumps(["OK", msg[1]["id"], True, ""]))
        elif msg[0] == "EVENT":
            if r.silent:
                return
            if r.refuse:
                self.inbox.put_nowait(json.dumps(["OK", msg[1]["id"], False, "blocked: kind"]))
                return
            r.events.append(msg[1])
            self.inbox.put_nowait(json.dumps(["OK", msg[1]["id"], True, ""]))

    async def recv(self) -> str:
        return await self.inbox.get()


class Relay:
    def __init__(self, url: str):
        self.url = url
        self.down = self.refuse = self.silent = False
        self.challenge = ""
        self.authed: list[dict] = []
        self.opened = 0
        self.socks: list[Sock] = []
        self.sent: list[list] = []
        self.filters: list[dict] = []
        self.events: list[dict] = []  # what it accepted

    def inject(self, event: dict) -> None:
        """An app publishes EVENT here: whoever subscribed to its p tag gets it."""
        for s in self.socks:
            ps = [t[1] for t in event["tags"] if t[0] == "p"]
            if s.filter and any(p in s.filter.get("#p", []) for p in ps):
                s.inbox.put_nowait(json.dumps(["EVENT", "belfry-apps", event]))

    def drop(self) -> None:
        for s in list(self.socks):
            s.inbox.put_nowait("not json")


class Net:
    def __init__(self):
        self.relays: dict[str, Relay] = {}

    def __getitem__(self, url: str) -> Relay:
        return self.relays.setdefault(url, Relay(url))

    def __call__(self, url: str) -> Sock:
        return Sock(self[url])


@pytest.fixture
def net(monkeypatch) -> Net:
    n = Net()
    monkeypatch.setattr(remoteapps, "CONNECT", n)
    return n


@pytest.fixture
def apps(world, keycast, monkeypatch, net):
    """A belfry with its own register, an admin (Ada) and a member (Bob) whose keys the
    signer holds (real keys, so what it encrypts and signs can be checked)."""
    rsvc, _p, _k, ada, ada_id, _s = world
    bob, bob_id = person(rsvc, "bob@belfry.test", "Bob")
    own_key: set[int] = set()

    def member_token(svc, partner):
        if partner in own_key:
            return None
        rows = svc.odoo.search_read(
            "belfry.pass",
            [["partner_id", "=", partner], ["state", "=", "active"]],
            ["keycast_pubkey"],
        )
        if keycast.down:
            raise RuntimeError("signer down")
        return (rows[0]["keycast_pubkey"], "ucan-token/" + rows[0]["keycast_pubkey"])

    monkeypatch.setattr(community, "member_token", member_token)
    for k in (ada, bob):
        keycast.keys[pk(k)] = k.secret
        keycast.status[pk(k)] = "active"
    remoteapps.CONNECTS.reset()
    monkeypatch.setattr(remoteapps, "ACK_SECONDS", 0.5)

    class W:
        pass

    w = W()
    w.svc, w.keycast, w.net, w.ada, w.bob = rsvc, keycast, net, ada, bob
    w.ada_id, w.bob_id, w.own_key = ada_id, bob_id, own_key
    return w


def add_person(w, email, name, tier=None) -> PrivateKey:
    key, pid = person(w.svc, email, name)
    w.keycast.keys[pk(key)] = key.secret
    if tier:
        ids = [
            r["id"] for r in w.svc.odoo.search_read("belfry.pass", [["partner_id", "=", pid]], [])
        ]
        w.svc.odoo.write("belfry.pass", ids, {"tier": tier})
    return key


def opened(client: PrivateKey, member: str, event: dict) -> dict:
    """What the app reads in an event the signer sent it."""
    assert event["kind"] == 24133 and event["pubkey"] == member
    assert event["tags"] == [["p", pk(client)]]
    assert event_id(event) == event["id"] and verify_signature(event)
    if "?iv=" in event["content"]:
        return json.loads(nip04_decrypt(client.secret, member, event["content"]))
    conv = conversation_key(client.secret, bytes.fromhex(member))
    return json.loads(nip44_decrypt(conv, event["content"]))


_ids = iter(range(1, 10**6))


def request(client, member, method, params=(), scheme="nip44", rid=None, at=None) -> dict:
    body = json.dumps({"id": rid or f"r{next(_ids)}", "method": method, "params": list(params)})
    if scheme == "nip04":
        content = nip04_encrypt(client.secret, member, body)
    else:
        content = nip44_encrypt(conversation_key(client.secret, bytes.fromhex(member)), body)
    return sign_event(
        client.secret,
        {
            "kind": 24133,
            "created_at": at or int(time.time()),
            "tags": [["p", member]],
            "content": content,
        },
    )


# ── the URI ─────────────────────────────────────────────────────────────────────────────────
def test_a_uri_is_read_strictly():
    got = parse_uri(
        uri(
            relays=(R1, R1 + "/", "WSS://Nos.Example.net:443", "wss://r.example.com:7443/sub"),
            name="Git Workshop",
            url="https://gitworkshop.dev/x",
            image="https://gitworkshop.dev/icon.png",
            perms="sign_event:1,nip44_encrypt, sign_event:30617,bad perm!,sign_event:1",
        )
    )
    assert got == {
        "client": K,
        "relays": [R1, R2, "wss://r.example.com:7443/sub"],
        "secret": "s3cret",
        "perms": ["sign_event:1", "nip44_encrypt", "sign_event:30617"],
        "name": "Git Workshop",
        "url": "https://gitworkshop.dev/x",
        "host": "gitworkshop.dev",
        "image": "https://gitworkshop.dev/icon.png",
    }
    assert parse_uri(uri(client=K.upper()))["client"] == K
    plain = parse_uri(uri())
    assert plain["perms"] is None and plain["name"] is None and plain["url"] is None
    assert plain["host"] is None and plain["image"] is None
    # what is shown to people is dropped when it isn't clean, never an error
    odd = parse_uri(
        uri(name="x" * 61, url="http://gitworkshop.dev", image="https://10.0.0.1/a.png", perms="")
    )
    assert (odd["name"], odd["url"], odd["host"], odd["image"], odd["perms"]) == (None,) * 5
    assert parse_uri(uri(name="two\nlines"))["name"] is None
    assert parse_uri(uri(url="https://" + "a" * 200 + ".dev"))["url"] is None
    assert parse_uri(uri(url="https://user@gitworkshop.dev/"))["url"] is None


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (None, "bad_scheme"),
        (17, "bad_scheme"),
        ("", "bad_scheme"),
        ("bunker://" + K + "?relay=" + R1 + "&secret=s", "bad_scheme"),
        ("https://example.org", "bad_scheme"),
        ("nostrconnect://" + "x" * 5000, "bad_scheme"),
        (uri(client="abc"), "bad_key"),
        (uri(client="g" * 64), "bad_key"),
        (uri(client="00" * 32), "bad_key"),  # 64 hex characters that are no key
        (uri(client="npub1" + "q" * 58), "bad_key"),
        (uri(relays=()), "no_relay"),
        (uri(relays=("ws://relay.example.org",)), "bad_relay"),
        (uri(relays=("https://relay.example.org",)), "bad_relay"),
        (uri(relays=("wss://127.0.0.1",)), "bad_relay"),
        (uri(relays=("wss://10.1.2.3:8080",)), "bad_relay"),
        (uri(relays=("wss://[::1]",)), "bad_relay"),
        (uri(relays=("wss://2130706433",)), "bad_relay"),
        (uri(relays=("wss://localhost",)), "bad_relay"),
        (uri(relays=("wss://relay",)), "bad_relay"),
        (uri(relays=("wss://keycast-relay:8080",)), "bad_relay"),
        (uri(relays=("wss://relay.internal",)), "bad_relay"),
        (uri(relays=("wss://box.local",)), "bad_relay"),
        (uri(relays=("wss://u:p@relay.example.org",)), "bad_relay"),
        (uri(relays=("wss://relay.example.org/?x=1",)), "bad_relay"),
        (uri(relays=("wss://relay.example.org:99999",)), "bad_relay"),
        (uri(relays=(R1, "wss://")), "bad_relay"),
        (uri(relays=tuple(f"wss://r{i}.example.org" for i in range(9))), "bad_relay"),
        (uri(secret=None), "no_secret"),
        (uri(secret=""), "no_secret"),
        (uri(secret="x" * 129), "no_secret"),
    ],
)
def test_what_is_not_a_connect_code_says_why(text, reason):
    with pytest.raises(UriError) as exc:
        parse_uri(text)
    assert exc.value.reason == reason


def test_eight_relays_are_fine_and_duplicates_dont_count():
    relays = tuple(f"wss://r{i}.example.org" for i in range(8))
    assert len(parse_uri(uri(relays=relays + relays))["relays"]) == 8


# ── what an app may ask ─────────────────────────────────────────────────────────────────────
OWN = Own(
    domain="belfry.test",
    api=frozenset(
        {"belfry.test", "belfry.belfry.test", "pass.belfry.test", "apps.belfry.test"}
        | {"chat.belfry.test"}
    ),
    relays=frozenset({"relay.belfry.test", "groups-relay.belfry.test", "bunker.belfry.test"}),
    blossom=frozenset({"blossom.belfry.test"}),
)


def ev(kind, *tags):
    return {"kind": kind, "tags": [list(t) for t in tags], "content": "", "created_at": 1}


@pytest.mark.parametrize(
    ("perms", "decrypt", "method", "event", "ok"),
    [
        # always answered
        (["sign_event:1"], False, "connect", None, True),
        ([], False, "ping", None, True),
        ([], False, "get_public_key", None, True),
        ([], False, "get_relays", None, True),
        # no perms: anything the rules allow
        (None, False, "sign_event", ev(1), True),
        (None, False, "sign_event", ev(30617), True),
        (None, False, "nip44_encrypt", None, True),
        (None, False, "nip04_encrypt", None, True),
        # reading messages: the member's choice, and the perms when there are some
        (None, False, "nip44_decrypt", None, False),
        (None, False, "nip04_decrypt", None, False),
        (None, True, "nip44_decrypt", None, True),
        (None, True, "nip04_decrypt", None, True),
        (["nip44_decrypt"], False, "nip44_decrypt", None, False),
        (["nip44_decrypt"], True, "nip44_decrypt", None, True),
        (["nip44_decrypt"], True, "nip04_decrypt", None, False),
        (["sign_event"], True, "nip44_decrypt", None, False),
        # perms cover a request, or it is refused
        (["sign_event"], False, "sign_event", ev(1), True),
        (["sign_event"], False, "sign_event", ev(30023), True),
        (["sign_event:1"], False, "sign_event", ev(1), True),
        (["sign_event:1"], False, "sign_event", ev(10), False),
        (["sign_event:1"], False, "sign_event", ev(11), False),
        (["sign_event:1", "sign_event:7"], False, "sign_event", ev(7), True),
        (["nip44_encrypt"], False, "sign_event", ev(1), False),
        (["sign_event:1"], False, "nip44_encrypt", None, False),
        (["nip44_encrypt"], False, "nip44_encrypt", None, True),
        (["nip44_encrypt"], False, "nip04_encrypt", None, False),
        ([], False, "sign_event", ev(1), False),
        # not an event, not a method
        (None, True, "sign_event", None, False),
        (None, True, "sign_event", {"kind": "1", "tags": []}, False),
        (None, True, "sign_event", {"kind": True, "tags": []}, False),
        (None, True, "delete_account", None, False),
        # NIP-98: never for the belfry's own hosts
        (None, True, "sign_event", ev(27235, ["u", "https://belfry.belfry.test/api/v1/me"]), False),
        (["sign_event"], True, "sign_event", ev(27235, ["u", "https://belfry.test/x"]), False),
        (
            ["sign_event:27235"],
            True,
            "sign_event",
            ev(27235, ["u", "https://PASS.belfry.test/"]),
            False,
        ),
        (None, True, "sign_event", ev(27235, ["u", "https://apps.belfry.test/api/oauth"]), False),
        (None, True, "sign_event", ev(27235, ["u", "https://chat.belfry.test:443/x"]), False),
        (None, True, "sign_event", ev(27235, ["u", "https://groups-relay.belfry.test/"]), False),
        (None, True, "sign_event", ev(27235), False),
        (None, True, "sign_event", ev(27235, ["u", "not an address"]), True),
        (None, True, "sign_event", ev(27235, ["u", ""]), False),
        (
            None,
            True,
            "sign_event",
            ev(27235, ["u", "https://git.example.org/x"], ["u", "https://belfry.test/api/me"]),
            False,
        ),
        (None, True, "sign_event", ev(27235, ["u", "https://git.example.org/api"]), True),
        (None, True, "sign_event", ev(27235, ["u", "https://notbelfry.test/api"]), True),
        # NIP-42: never for the belfry's own relays; other relays are the app's business
        (None, True, "sign_event", ev(22242, ["relay", "wss://relay.belfry.test"]), False),
        (None, True, "sign_event", ev(22242, ["relay", "wss://relay.belfry.test/"]), False),
        (None, True, "sign_event", ev(22242, ["relay", "wss://groups-relay.belfry.test"]), False),
        (
            ["sign_event:22242"],
            True,
            "sign_event",
            ev(22242, ["relay", "wss://bunker.belfry.test"]),
            False,
        ),
        (None, True, "sign_event", ev(22242, ["relay", "WSS://Relay.Belfry.Test:443/x"]), False),
        (None, True, "sign_event", ev(22242), False),
        (None, True, "sign_event", ev(22242, ["relay", "wss://relay.example.org"]), True),
        # any host under the belfry's domain, as for NIP-98: git.<domain> admits members by AUTH
        (None, True, "sign_event", ev(22242, ["relay", "wss://git.belfry.test"]), False),
        (None, True, "sign_event", ev(22242, ["relay", "wss://belfry.test"]), False),
        (None, True, "sign_event", ev(22242, ["relay", "wss://notbelfry.test"]), True),
        (
            None,
            True,
            "sign_event",
            ev(22242, ["relay", "wss://relay.example.org"], ["relay", "wss://relay.belfry.test"]),
            False,
        ),
        # never: the signer's own kind, NIP-29 moderation and joins
        (["sign_event"], True, "sign_event", ev(24133), False),
        (["sign_event:24133"], True, "sign_event", ev(24133), False),
        *[(None, True, "sign_event", ev(k, ["h", "general"]), False) for k in range(9000, 10010)][
            :10
        ],
        (["sign_event:9021"], True, "sign_event", ev(9021, ["h", "general"]), False),
        (None, True, "sign_event", ev(9022, ["h", "general"]), False),
        (None, True, "sign_event", ev(9010), True),
        (None, True, "sign_event", ev(9, ["h", "general"]), True),
        # Blossom: never for the belfry's own storage, nor for any server
        (None, True, "sign_event", ev(24242, ["t", "upload"]), False),
        (None, True, "sign_event", ev(24242, ["server", "blossom.belfry.test"]), False),
        (None, True, "sign_event", ev(24242, ["server", "https://blossom.belfry.test/"]), False),
        (
            ["sign_event:24242"],
            True,
            "sign_event",
            ev(24242, ["server", "blossom.belfry.test"]),
            False,
        ),
        (None, True, "sign_event", ev(24242, ["server", "cdn.example.org"]), True),
        (
            None,
            True,
            "sign_event",
            ev(24242, ["server", "cdn.example.org"], ["server", "blossom.belfry.test"]),
            False,
        ),
        # the profile and the contact list: only when the app said so
        (None, True, "sign_event", ev(0), False),
        (None, True, "sign_event", ev(3), False),
        (["sign_event"], True, "sign_event", ev(0), False),
        (["sign_event"], True, "sign_event", ev(3), False),
        (["sign_event:0"], True, "sign_event", ev(0), True),
        (["sign_event:0"], True, "sign_event", ev(3), False),
        (["sign_event", "sign_event:3"], True, "sign_event", ev(3), True),
    ],
)
def test_what_an_app_may_ask(perms, decrypt, method, event, ok):
    assert allowed(perms, decrypt, method, event, OWN) is ok


def test_the_belfrys_own_names_come_from_its_settings():
    s = Settings(
        NIP05_DOMAIN="village.example",
        PUBLIC_URL="https://pass.village.example",
        CONSOLE_URL="https://belfry.village.example",
        RELAY_URL="ws://relay:8080",
        COMMUNITY_RELAY_URL="wss://relay.village.example",
        GROUPS_RELAY_URL="wss://groups-relay.village.example",
        BUNKER_PUBLIC_RELAYS="wss://bunker.village.example, wss://other.example.org",
        BLOSSOM_PUBLIC_URL="https://files.village.example",
        AUTHENTIK_TOKEN="x",
        KEYCAST_ADMIN_TOKEN="x",
        KEYCAST_SERVICE_TOKEN="x",
        HOOK_TOKEN="x",
        _env_file=None,
    )
    own = remoteapps.own_hosts(s)
    assert own.domain == "village.example"
    assert {"village.example", "belfry.village.example", "pass.village.example"} <= own.api
    assert {"apps.village.example", "chat.village.example"} <= own.api
    assert {"relay.village.example", "groups-relay.village.example"} <= own.relays
    assert {"bunker.village.example", "other.example.org", "relay"} <= own.relays
    assert {"files.village.example", "blossom.village.example"} <= own.blossom


# ── the API ─────────────────────────────────────────────────────────────────────────────────
def test_who_may_connect_an_app(apps):
    w = apps
    gus = add_person(w, "gus@example.org", "Gus", tier="guest")
    bot = add_person(w, "bot@example.org", "Bot", tier="agent")
    cy = add_person(w, "cy@example.org", "Cy")
    w.own_key.add(
        core.m2o_id(
            w.svc.odoo.search_read(
                "belfry.pass", [["keycast_pubkey", "=", pk(cy)]], ["partner_id"]
            )[0]["partner_id"]
        )
    )
    site = PrivateKey()  # a website session of Bob's account: not the account's key
    w.svc.odoo.site_put(pk(site), w.bob_id, "Firefox", int(time.time()), 10)
    with TestClient(create_app(w.svc)) as c:
        for path, body, method in (
            ("/remote-apps/preview", {"uri": uri()}, "POST"),
            ("/remote-apps", {"uri": uri()}, "POST"),
            ("/remote-apps", None, "GET"),
            ("/remote-apps/x/disconnect", {}, "POST"),
        ):
            for key, status, error in (
                (gus, 403, "guest"),
                (bot, 403, "agent"),
                (site, 403, "no_pass"),
                (PrivateKey(), 403, "no_pass"),
            ):
                r = call(c, key, path, body, method=method)
                assert (r.status_code, r.json()["error"]) == (status, error), (path, error)
        assert (
            c.get("/api/v1/remote-apps", headers={"Host": "belfry.belfry.test"}).status_code == 401
        )
        # someone who holds their own key connects the app with it, not through the belfry
        for path in ("/remote-apps/preview", "/remote-apps"):
            r = call(c, cy, path, {"uri": uri()})
            assert (r.status_code, r.json()["error"]) == (409, "own_key")
        assert call(c, cy, "/remote-apps", method="GET").json() == {"apps": []}
        # a member and an admin may
        assert call(c, w.bob, "/remote-apps/preview", {"uri": uri()}).status_code == 200
        assert call(c, w.ada, "/remote-apps/preview", {"uri": uri()}).status_code == 200
        # the signer down
        w.keycast.down = True
        r = call(c, w.bob, "/remote-apps/preview", {"uri": uri()})
        assert (r.status_code, r.json()["error"]) == (503, "signer")
    assert w.svc.odoo.remote_list() == []


def test_without_the_own_register_there_are_no_remote_apps(client, services):
    services.settings.console_url = "https://belfry.belfry.test"
    r = call(client, PrivateKey(), "/remote-apps", method="GET")
    assert (r.status_code, r.json()["error"]) == (409, "unsupported")


def test_preview_says_what_the_app_is_and_asks(apps):
    w = apps
    with TestClient(create_app(w.svc)) as c:
        r = call(
            c,
            w.bob,
            "/remote-apps/preview",
            {
                "uri": uri(
                    name="Git Workshop",
                    url="https://gitworkshop.dev",
                    image="https://gitworkshop.dev/icon.png",
                    perms="sign_event:1,sign_event:30617",
                )
            },
        )
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert r.json() == {
            "name": "Git Workshop",
            "url": "https://gitworkshop.dev",
            "host": "gitworkshop.dev",
            "image": "https://gitworkshop.dev/icon.png",
            "asks": {"sign": True, "decrypt": False},
            "known": False,
        }
        got = call(c, w.bob, "/remote-apps/preview", {"uri": uri()}).json()
        assert got == {
            "name": None,
            "url": None,
            "host": None,
            "image": None,
            "asks": {"sign": True, "decrypt": True},  # no perms: the app may ask anything
            "known": False,
        }
        got = call(c, w.bob, "/remote-apps/preview", {"uri": uri(perms="nip04_decrypt")}).json()
        assert got["asks"] == {"sign": True, "decrypt": True}
        for bad, reason in (
            ("bunker://x", "bad_scheme"),
            (uri(client="zz"), "bad_key"),
            (uri(relays=()), "no_relay"),
            (uri(relays=("ws://relay.example.org",)), "bad_relay"),
            (uri(secret=None), "no_secret"),
        ):
            for path in ("/remote-apps/preview", "/remote-apps"):
                r = call(c, w.bob, path, {"uri": bad})
                assert r.status_code == 422, r.text
                assert (r.json()["error"], r.json()["reason"]) == ("invalid_uri", reason)
        assert call(c, w.bob, "/remote-apps/preview", {}).json()["reason"] == "bad_scheme"
        # known: this client key is connected to this account already
        assert call(c, w.bob, "/remote-apps", {"uri": uri()}).status_code == 201
        assert call(c, w.bob, "/remote-apps/preview", {"uri": uri()}).json()["known"] is True
        assert call(c, w.ada, "/remote-apps/preview", {"uri": uri()}).json()["known"] is False
    assert w.net.relays.keys() == {R1, R2}, "a preview opens no connection"


def test_connecting_tells_the_app_on_each_of_its_relays(apps, caplog):
    w = apps
    app_key = PrivateKey()
    code = uri(
        client=pk(app_key),
        secret="the-secret-of-the-code",
        name="Git Workshop",
        url="https://gitworkshop.dev/",
        perms="sign_event:1,nip44_decrypt",
    )
    caplog.set_level("DEBUG")
    with TestClient(create_app(w.svc)) as c:
        assert call(c, w.bob, "/remote-apps", {"uri": code, "decrypt": "yes"}).status_code == 422
        r = call(c, w.bob, "/remote-apps", {"uri": code, "decrypt": True})
        assert r.status_code == 201, r.text
        app = r.json()["app"]
        assert set(app) == {
            "id", "name", "url", "host", "image", "decrypt", "created", "used", "expires",
        }  # fmt: skip
        assert (app["name"], app["host"], app["decrypt"], app["used"]) == (
            "Git Workshop",
            "gitworkshop.dev",
            True,
            None,
        )
        assert app["expires"] == app["created"] + 30 * 86400
        for url in (R1, R2):
            relay = w.net[url]
            # listening for the app before it is told (kind 24133 is not kept by relays)
            first_req = next(i for i, m in enumerate(relay.sent) if m[0] == "REQ")
            first_event = next(i for i, m in enumerate(relay.sent) if m[0] == "EVENT")
            assert first_req < first_event
            assert relay.filters[0]["kinds"] == [24133] and relay.filters[0]["#p"] == [pk(w.bob)]
            assert abs(relay.filters[0]["since"] - (int(time.time()) - 10)) < 5
            deadline = time.time() + 2
            while not relay.events and time.time() < deadline:
                time.sleep(0.01)
            [ack] = relay.events
            got = opened(app_key, pk(w.bob), ack)  # signed as the member, for the app only
            assert got["result"] == "the-secret-of-the-code" and set(got) == {"id", "result"}
        assert w.net[R1].events[0]["id"] == w.net[R2].events[0]["id"]
        [row] = w.svc.odoo.remote_list()
        assert row["partner_id"] == w.bob_id and row["pubkey"] == pk(w.bob)
        assert row["client"] == pk(app_key) and row["relays"] == [R1, R2]
        assert row["perms"] == ["sign_event:1", "nip44_decrypt"] and row["decrypt"] is True
        assert "the-secret-of-the-code" not in json.dumps(row)
        # the list: no keys, no relays
        r = call(c, w.bob, "/remote-apps", method="GET")
        assert r.json() == {"apps": [app]}
        text = r.text + json.dumps(app)
        assert pk(app_key) not in text and pk(w.bob) not in text and "wss://" not in text
        assert call(c, w.ada, "/remote-apps", method="GET").json() == {"apps": []}
        # the member's "no" to reading messages; perms that ask for none
        k2 = PrivateKey()
        r = call(c, w.bob, "/remote-apps", {"uri": uri(client=pk(k2), perms="nip44_decrypt")})
        assert r.json()["app"]["decrypt"] is False
        k3 = PrivateKey()
        r = call(
            c,
            w.bob,
            "/remote-apps",
            {"uri": uri(client=pk(k3), perms="sign_event:1"), "decrypt": True},
        )
        assert r.json()["app"]["decrypt"] is False
        # the same app again replaces its connection
        r = call(c, w.bob, "/remote-apps", {"uri": code})
        assert r.status_code == 201 and r.json()["app"]["id"] != app["id"]
        ids = [a["id"] for a in call(c, w.bob, "/remote-apps", method="GET").json()["apps"]]
        assert len(ids) == 3 and app["id"] not in ids
        assert w.svc.odoo.remote_get(app["id"]) is None
    assert "the-secret-of-the-code" not in caplog.text
    assert pk(app_key) not in caplog.text


def test_an_app_that_cant_be_reached_is_not_stored(apps):
    w = apps
    with TestClient(create_app(w.svc)) as c:
        w.net[R1].down = True
        w.net[R2].refuse = True
        r = call(c, w.bob, "/remote-apps", {"uri": uri()})
        assert (r.status_code, r.json()["error"]) == (502, "unreachable")
        w.net[R2].refuse, w.net[R2].silent = False, True
        r = call(c, w.bob, "/remote-apps", {"uri": uri()})
        assert (r.status_code, r.json()["error"]) == (502, "unreachable")
        assert w.svc.odoo.remote_list() == []
        hub = c.app.state.remote_apps
        assert hub.links == {} and hub.pending == {} and hub.rows == {}
        # one relay that takes it is enough
        w.net[R2].silent = False
        r = call(c, w.bob, "/remote-apps", {"uri": uri()})
        assert r.status_code == 201
        assert len(w.net[R2].events) == 1 and w.net[R1].events == []
        assert set(hub.links) == {R1, R2}, "the relay that was down is tried again"
        # the signer down: 503, nothing stored
        w.keycast.down = True
        r = call(c, w.ada, "/remote-apps", {"uri": uri(client=KS[12])})
        assert (r.status_code, r.json()["error"]) == (503, "signer")
        assert len(w.svc.odoo.remote_list()) == 1 and hub.pending == {}


def test_a_client_key_is_one_accounts_and_the_limits(apps, monkeypatch):
    w = apps
    with TestClient(create_app(w.svc)) as c:
        r = call(c, w.bob, "/remote-apps", {"uri": uri()})
        first = r.json()["app"]["id"]
        r = call(c, w.ada, "/remote-apps", {"uri": uri()})
        assert (r.status_code, r.json()["error"]) == (409, "key_taken")
        # a disconnected one too: the key was Bob's
        assert call(c, w.bob, f"/remote-apps/{first}/disconnect").json() == {"ok": True}
        r = call(c, w.ada, "/remote-apps", {"uri": uri()})
        assert (r.status_code, r.json()["error"]) == (409, "key_taken")
        assert call(c, w.bob, "/remote-apps", {"uri": uri()}).status_code == 201
        # ten an hour
        for i in range(8):
            r = call(c, w.bob, "/remote-apps", {"uri": uri(client=KS[i])})
            assert r.status_code == 201, r.text
        r = call(c, w.bob, "/remote-apps", {"uri": uri(client=KS[8])})
        assert (r.status_code, r.json()["error"]) == (429, "too_many")
        # ten live apps per account
        remoteapps.CONNECTS.reset()
        assert call(c, w.bob, "/remote-apps", {"uri": uri(client=KS[8])}).status_code == 201
        assert len(call(c, w.bob, "/remote-apps", method="GET").json()["apps"]) == 10
        r = call(c, w.bob, "/remote-apps", {"uri": uri(client=KS[9])})
        assert (r.status_code, r.json()["error"]) == (409, "limit")
        assert call(c, w.bob, "/remote-apps", {"uri": uri()}).status_code == 201  # one of the ten
        # thirty relays over all: a new connection uses those in use and what fits
        monkeypatch.setattr(remoteapps, "MAX_RELAYS", 3)
        r3 = "wss://three.example.org"
        r = call(
            c,
            w.ada,
            "/remote-apps",
            {"uri": uri(client=KS[10], relays=(r3, "wss://four.example.org", R2))},
        )
        assert r.status_code == 201
        [row] = w.svc.odoo.remote_list(partner_id=w.ada_id)
        assert row["relays"] == [r3, R2]
        r = call(
            c,
            w.ada,
            "/remote-apps",
            {"uri": uri(client=KS[11], relays=("wss://five.example.org",))},
        )
        assert (r.status_code, r.json()["error"]) == (503, "relays_full")


def test_disconnecting_and_what_else_ends_a_connection(apps):
    w = apps
    with TestClient(create_app(w.svc)) as c:
        hub = c.app.state.remote_apps
        a = call(c, w.bob, "/remote-apps", {"uri": uri()}).json()["app"]["id"]
        b = call(c, w.bob, "/remote-apps", {"uri": uri(client=KS[12])}).json()["app"]["id"]
        z = call(c, w.ada, "/remote-apps", {"uri": uri(client=KS[13], relays=(R1,))}).json()
        # someone else's, or none: 404
        r = call(c, w.ada, f"/remote-apps/{a}/disconnect")
        assert (r.status_code, r.json()["error"]) == (404, "not_found")
        assert call(c, w.bob, "/remote-apps/nope/disconnect").status_code == 404
        assert call(c, w.bob, f"/remote-apps/{a}/disconnect").json() == {"ok": True}
        assert call(c, w.bob, f"/remote-apps/{a}/disconnect").json() == {"ok": True}
        assert [x["id"] for x in call(c, w.bob, "/remote-apps", method="GET").json()["apps"]] == [b]
        assert len(hub.rows) == 2
        # thirty days without use
        w.svc.odoo.remote_touch(b, int(time.time()) - 31 * 86400)
        assert call(c, w.bob, "/remote-apps", method="GET").json() == {"apps": []}
        w.svc.odoo.remote_touch(b, int(time.time()) - 29 * 86400)
        [app] = call(c, w.bob, "/remote-apps", method="GET").json()["apps"]
        assert app["expires"] == app["used"] + 30 * 86400
        # the member's pass closes: their connections end, the others stay
        [p] = w.svc.odoo.search_read("belfry.pass", [["keycast_pubkey", "=", pk(w.bob)]], [])
        core.revoke_pass(w.svc, p["id"], "test")
        assert all(r["revoked"] for r in w.svc.odoo.remote_list(partner_id=w.bob_id))
        assert call(c, w.ada, "/remote-apps", method="GET").json() == {"apps": [z["app"]]}
        assert call(c, w.bob, "/remote-apps", method="GET").json()["error"] == "no_pass"


# ── the signer loop ─────────────────────────────────────────────────────────────────────────
def connected(w, member: PrivateKey, partner: int, client: PrivateKey, **over) -> dict:
    row = {
        "id": f"app-{pk(client)[:6]}",
        "partner_id": partner,
        "pubkey": pk(member),
        "client": pk(client),
        "relays": [R1],
        "perms": None,
        "decrypt": False,
        "name": "App",
        "url": None,
        "image": None,
        "created": int(time.time()),
        "used": None,
        **over,
    }
    w.svc.odoo.remote_put(row)
    return row


async def started(w, **kw) -> Hub:
    async def nap(_s):
        await asyncio.sleep(0.01)

    hub = Hub(w.svc, sleep=nap, **kw)
    await hub.refresh()
    for link in hub.links.values():
        if not w.net[link.url].down:
            await asyncio.wait_for(link.ready.wait(), 1)
    return hub


async def until(cond, seconds=2.0):
    end = time.time() + seconds
    while not cond():
        assert time.time() < end, "timed out"
        await asyncio.sleep(0.005)


def answers(w, client, member, url=R1) -> list[dict]:
    return [opened(client, pk(member), e) for e in w.net[url].events]


def test_requests_are_answered_as_the_member(apps):
    w = apps
    app_key = PrivateKey()
    me = pk(w.bob)
    connected(w, w.bob, w.bob_id, app_key, relays=[R1, R2])

    async def run():
        hub = await started(w)
        tasks = [asyncio.create_task(t) for t in hub.tasks()]
        try:
            # through the relay: the subscription, the queue, the answer on that relay only
            w.net[R1].inject(request(app_key, me, "get_public_key", rid="a"))
            await until(lambda: len(w.net[R1].events) == 1)
            assert answers(w, app_key, w.bob) == [{"id": "a", "result": me}]
            assert w.net[R2].events == []
            w.net[R2].inject(request(app_key, me, "ping", rid="b"))
            await until(lambda: len(w.net[R2].events) == 1)
            assert answers(w, app_key, w.bob, R2) == [{"id": "b", "result": "pong"}]
            assert len(w.net[R1].events) == 1
        finally:
            for t in tasks:
                t.cancel()
            await hub.stop()
        # one at a time from here: what each request gets
        hub = await started(w)
        try:
            n = len(w.net[R1].events)

            async def ask(method, params=(), **kw):
                state = await hub.handle(R1, request(app_key, me, method, params, **kw))
                return state, answers(w, app_key, w.bob)[-1] if len(w.net[R1].events) > n else None

            assert (await ask("connect", [me]))[1]["result"] == "ack"
            assert (await ask("connect", [me, "sec", "sign_event"]))[1]["result"] == "sec"
            assert (await ask("get_relays"))[1]["result"] == "{}"
            state, got = await ask("logout", rid="x1")
            assert (state, got) == ("error", {"id": "x1", "error": "Unsupported method"})
            state, got = await ask("sign_event", ["{not json"], rid="x2")
            assert (state, got) == ("error", {"id": "x2", "error": "Invalid params"})
            state, got = await ask("ping", [1, 2], rid="x3")
            assert got == {"id": "x3", "error": "Invalid params"}
            # sign_event: the member's event, really signed
            note = {
                "kind": 1,
                "content": "hello from the app",
                "tags": [["t", "x"]],
                "created_at": 1700000000,
            }
            state, got = await ask("sign_event", [json.dumps(note)], rid="s1")
            assert state == "ok" and got["id"] == "s1"
            signed = json.loads(got["result"])
            assert signed["pubkey"] == me and signed["content"] == "hello from the app"
            assert signed["kind"] == 1 and signed["created_at"] == 1700000000
            assert event_id(signed) == signed["id"] and verify_signature(signed)
            # someone else's event is not signed as the member
            state, got = await ask("sign_event", [json.dumps({**note, "pubkey": pk(w.ada)})])
            assert got["error"] == "Invalid params"
            # refused: the belfry's own API, relays, groups; and it says no more than that
            for bad in (
                {"kind": 27235, "tags": [["u", "https://belfry.belfry.test/api/v1/admins"]]},
                {"kind": 22242, "tags": [["relay", "wss://relay.belfry.test"], ["challenge", "c"]]},
                {"kind": 9000, "tags": [["h", "general"], ["p", me]]},
                {"kind": 0, "content": "{}"},
                {"kind": 24133},
            ):
                before = len(w.keycast.signed)
                state, got = await ask("sign_event", [json.dumps({"content": "", **bad})], rid="no")
                assert (state, got) == ("refused", {"id": "no", "error": "Not allowed"}), bad
                kinds = [e["kind"] for e in w.keycast.signed[before:]]
                assert kinds == [24133], "only the answer was signed"
            ok = {"kind": 22242, "content": "", "tags": [["relay", R1], ["challenge", "c"]]}
            assert (await ask("sign_event", [json.dumps(ok)]))[0] == "ok"
            # encrypting is fine; reading is off for this connection
            other = PrivateKey()
            state, got = await ask("nip44_encrypt", [pk(other), "for you"])
            conv = conversation_key(other.secret, bytes.fromhex(me))
            assert state == "ok" and nip44_decrypt(conv, got["result"]) == "for you"
            state, got = await ask("nip04_encrypt", [pk(other), "old style"])
            assert nip04_decrypt(other.secret, me, got["result"]) == "old style"
            secret_text = nip44_encrypt(conv, "a private message")
            state, got = await ask("nip44_decrypt", [pk(other), secret_text], rid="d")
            assert (state, got) == ("refused", {"id": "d", "error": "Not allowed"})
            state, got = await ask(
                "nip04_decrypt", [pk(other), nip04_encrypt(other.secret, me, "x")]
            )
            assert state == "refused"
            assert (await ask("nip44_encrypt", ["nope", "x"]))[1]["error"] == "Invalid params"
            # used: written, at most once a minute
            used = w.svc.odoo.remote_get(f"app-{pk(app_key)[:6]}")["used"]
            assert used and abs(used - time.time()) < 5
        finally:
            await hub.stop()

    asyncio.run(run())


def test_reading_messages_when_the_member_said_yes_and_the_old_encryption(apps):
    w = apps
    app_key, other = PrivateKey(), PrivateKey()
    me = pk(w.bob)
    connected(w, w.bob, w.bob_id, app_key, decrypt=True)

    async def run():
        hub = await started(w)
        try:
            conv = conversation_key(other.secret, bytes.fromhex(me))
            req = request(app_key, me, "nip44_decrypt", [pk(other), nip44_encrypt(conv, "psst")])
            assert await hub.handle(R1, req) == "ok"
            assert answers(w, app_key, w.bob)[-1]["result"] == "psst"
            old = nip04_encrypt(other.secret, me, "from 2021")
            assert (
                await hub.handle(R1, request(app_key, me, "nip04_decrypt", [pk(other), old]))
                == "ok"
            )
            assert answers(w, app_key, w.bob)[-1]["result"] == "from 2021"
            # not a message for this key: an error, not a crash
            req = request(app_key, me, "nip44_decrypt", [pk(other), "AAAA"], rid="bad")
            assert await hub.handle(R1, req) == "error"
            assert answers(w, app_key, w.bob)[-1] == {"id": "bad", "error": "Could not decrypt"}
            # a NIP-04 request is read and answered with NIP-04
            req = request(app_key, me, "get_public_key", scheme="nip04", rid="o1")
            assert await hub.handle(R1, req) == "ok"
            last = w.net[R1].events[-1]
            assert "?iv=" in last["content"]
            assert opened(app_key, me, last) == {"id": "o1", "result": me}
            assert "?iv=" not in w.net[R1].events[0]["content"]
        finally:
            await hub.stop()

    asyncio.run(run())


def test_perms_of_the_uri_bound_what_is_signed(apps):
    w = apps
    app_key = PrivateKey()
    me = pk(w.bob)
    connected(w, w.bob, w.bob_id, app_key, perms=["sign_event:1", "sign_event:0"], decrypt=True)

    async def run():
        hub = await started(w)
        try:
            for kind, state in ((1, "ok"), (0, "ok"), (7, "refused"), (3, "refused")):
                t = json.dumps({"kind": kind, "content": "{}", "tags": []})
                assert await hub.handle(R1, request(app_key, me, "sign_event", [t])) == state
            req = request(app_key, me, "nip44_encrypt", [pk(PrivateKey()), "x"])
            assert await hub.handle(R1, req) == "refused"
            assert await hub.handle(R1, request(app_key, me, "get_public_key")) == "ok"
        finally:
            await hub.stop()

    asyncio.run(run())


def test_what_is_not_a_connected_apps_request_is_ignored(apps):
    w = apps
    app_key, stranger = PrivateKey(), PrivateKey()
    me = pk(w.bob)
    row = connected(w, w.bob, w.bob_id, app_key)
    ada_app = PrivateKey()
    connected(w, w.ada, w.ada_id, ada_app, relays=[R2])

    async def run():
        hub = await started(w)
        try:
            before = len(w.keycast.rpc)
            # another key, with or without a connection of its own; the wrong member
            assert await hub.handle(R1, request(stranger, me, "get_public_key")) is None
            assert await hub.handle(R1, request(ada_app, me, "get_public_key")) is None
            assert await hub.handle(R1, request(app_key, pk(w.ada), "get_public_key")) is None
            # a relay that is not this connection's
            assert await hub.handle(R2, request(app_key, me, "get_public_key")) is None
            # not signed by the app, another kind, old, two p tags
            forged = {**request(stranger, me, "get_public_key"), "pubkey": pk(app_key)}
            assert await hub.handle(R1, forged) is None
            tampered = {**request(app_key, me, "get_public_key"), "content": "x"}
            assert await hub.handle(R1, tampered) is None
            assert await hub.handle(R1, {**request(app_key, me, "ping"), "kind": 1}) is None
            old = request(app_key, me, "ping", at=int(time.time()) - 600)
            assert await hub.handle(R1, old) is None
            assert await hub.handle(R1, "nonsense") is None
            assert w.keycast.rpc[before:] == [], "the signer was asked nothing"
            assert w.net[R1].events == []
            # content that is not a request: read, never answered
            junk = sign_event(
                app_key.secret,
                {
                    "kind": 24133,
                    "created_at": int(time.time()),
                    "tags": [["p", me]],
                    "content": "zz",
                },
            )
            assert await hub.handle(R1, junk) == "error" and w.net[R1].events == []
            # the same event twice (two relays, or a relay repeating itself): once
            req = request(app_key, me, "get_public_key")
            assert await hub.handle(R1, req) == "ok"
            assert await hub.handle(R1, req) is None
            assert len(w.net[R1].events) == 1
            # only the last 2000 ids are kept
            for i in range(remoteapps.SEEN_MAX + 5):
                hub._mark(f"{i:064x}")
            assert len(hub.seen) == remoteapps.SEEN_MAX
            # disconnected: at once, before the next refresh
            w.svc.odoo.remote_revoke(app_id=row["id"])
            assert await hub.handle(R1, request(app_key, me, "get_public_key")) is None
            assert (me, pk(app_key)) not in hub.rows
        finally:
            await hub.stop()

    asyncio.run(run())


def test_too_many_requests_and_a_signer_that_fails(apps, monkeypatch):
    w = apps
    app_key = PrivateKey()
    me = pk(w.bob)
    connected(w, w.bob, w.bob_id, app_key)
    monkeypatch.setattr(remoteapps, "REQUESTS_PER_MINUTE", 3)
    now = [time.time()]

    async def run():
        hub = await started(w, clock=lambda: now[0])
        try:
            for _ in range(3):
                assert await hub.handle(R1, request(app_key, me, "ping")) == "ok"
            assert await hub.handle(R1, request(app_key, me, "ping", rid="t")) == "limited"
            assert answers(w, app_key, w.bob)[-1] == {"id": "t", "error": "Too many requests"}
            for _ in range(2):
                assert await hub.handle(R1, request(app_key, me, "ping")) == "limited"
            n, asked = len(w.net[R1].events), len(w.keycast.rpc)
            assert await hub.handle(R1, request(app_key, me, "ping")) == "dropped"
            assert (len(w.net[R1].events), len(w.keycast.rpc)) == (n, asked)
            now[0] += 61
            assert await hub.handle(R1, request(app_key, me, "ping", at=int(now[0]))) == "ok"
            # the signer fails: nothing crashes, the next request is answered
            now[0] += 61
            w.keycast.down = True
            assert await hub.handle(R1, request(app_key, me, "ping", at=int(now[0]))) == "error"
            w.keycast.down = False
            assert await hub.handle(R1, request(app_key, me, "ping", at=int(now[0]))) == "ok"
            # the member moved to a key of their own: the signer has no token for them
            w.own_key.add(w.bob_id)
            hub.tokens.clear()
            assert await hub.handle(R1, request(app_key, me, "ping", at=int(now[0]))) == "error"
        finally:
            await hub.stop()

    asyncio.run(run())


def test_the_token_is_asked_at_most_every_five_minutes(apps, monkeypatch):
    w = apps
    app_key = PrivateKey()
    me = pk(w.bob)
    connected(w, w.bob, w.bob_id, app_key)
    calls = []
    real = community.member_token
    monkeypatch.setattr(community, "member_token", lambda svc, p: calls.append(p) or real(svc, p))
    now = [time.time()]

    async def run():
        hub = await started(w, clock=lambda: now[0])
        try:
            for _ in range(3):
                assert await hub.handle(R1, request(app_key, me, "ping", at=int(now[0]))) == "ok"
            assert calls == [w.bob_id]
            now[0] += 301
            assert await hub.handle(R1, request(app_key, me, "ping", at=int(now[0]))) == "ok"
            assert calls == [w.bob_id, w.bob_id]
        finally:
            await hub.stop()

    asyncio.run(run())


def test_relays_one_socket_each_resubscribed_and_reconnected(apps, caplog):
    w = apps
    a, b, z = PrivateKey(), PrivateKey(), PrivateKey()
    connected(w, w.bob, w.bob_id, a, relays=[R1, R2])
    connected(w, w.bob, w.bob_id, b, relays=[R1])
    w.net[R2].down = True

    async def run():
        hub = await started(w)
        try:
            assert set(hub.links) == {R1, R2} and w.net[R1].opened == 1
            assert w.net[R1].filters == [
                {"kinds": [24133], "#p": [pk(w.bob)], "since": w.net[R1].filters[0]["since"]}
            ]
            # a relay that is down is tried again, and said once
            await asyncio.sleep(0.1)
            assert w.net[R2].opened == 0
            assert caplog.text.count("relay nos.example.net") == 1
            w.net[R2].down = False
            await until(lambda: w.net[R2].opened == 1 and hub.links[R2].ready.is_set())
            # a connection more on R1 for another member: the same socket, a new REQ
            connected(w, w.ada, w.ada_id, z, relays=[R1])
            await hub.refresh()
            assert w.net[R1].opened == 1
            assert w.net[R1].filters[-1]["#p"] == sorted([pk(w.ada), pk(w.bob)])
            await hub.refresh()
            assert len(w.net[R1].filters) == 2, "nothing changed: no new REQ"
            # the socket drops: connected and subscribed again, requests answered again
            w.net[R1].drop()
            await until(lambda: w.net[R1].opened == 2 and hub.links[R1].ready.is_set())
            assert w.net[R1].filters[-1]["#p"] == sorted([pk(w.ada), pk(w.bob)])
            assert await hub.handle(R1, request(z, pk(w.ada), "ping")) == "ok"
            assert answers(w, z, w.ada)[-1]["result"] == "pong"
            # connections gone: their relays are left
            for r in w.svc.odoo.remote_list(partner_id=w.bob_id):
                w.svc.odoo.remote_revoke(app_id=r["id"])
            await hub.refresh()
            assert set(hub.links) == {R1} and w.net[R1].filters[-1]["#p"] == [pk(w.ada)]
            await until(lambda: w.net[R2].socks == [])
        finally:
            await hub.stop()

    asyncio.run(run())


def test_a_relay_that_wants_to_know_who_asks(apps):
    w = apps
    app_key = PrivateKey()
    connected(w, w.bob, w.bob_id, app_key)
    w.net[R1].challenge = "who-are-you"

    async def run():
        hub = await started(w)
        try:
            await until(lambda: w.net[R1].filters)
            [auth] = w.net[R1].authed
            assert auth["kind"] == 22242 and auth["pubkey"] == pk(w.bob)
            assert ["relay", R1] in auth["tags"] and ["challenge", "who-are-you"] in auth["tags"]
            assert verify_signature(auth)
            assert await hub.handle(R1, request(app_key, pk(w.bob), "ping")) == "ok"
        finally:
            await hub.stop()

    asyncio.run(run())


def test_the_real_connection_refuses_addresses_that_are_not_public(monkeypatch):
    async def run(addresses):
        loop = asyncio.get_running_loop()

        async def lookup(host, port, **kw):
            return [(2, 1, 6, "", (a, port)) for a in addresses]

        monkeypatch.setattr(loop, "getaddrinfo", lookup)
        async with remoteapps._default_connect("wss://relay.example.org"):
            pass

    for bad in (["10.0.0.5"], ["127.0.0.1"], ["8.8.8.8", "192.168.1.1"], ["::1"], []):
        with pytest.raises(RuntimeError, match="not a public address"):
            asyncio.run(run(bad))
