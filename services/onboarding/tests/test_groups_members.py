"""Groups relay members: the NIP-98 calls to zooid's member API and the sync from active passes."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from coincurve import PrivateKey, PublicKeyXOnly

from belfry_onboarding import core
from belfry_onboarding.blossom import event_id
from belfry_onboarding.groups_members import GroupsMembers, GroupsMembersError, api_key

API = "http://groups-relay:3334"
KEY = PrivateKey(bytes.fromhex("11" * 32))
OWN = KEY.public_key_xonly.format().hex()
RELAY_SELF = "f" * 64
A, B, C = "a" * 64, "b" * 64, "c" * 64


class FakeZooid:
    """zooid's member API: NIP-98 by the whitelisted key, a 60 s window, the exact URL. Like
    zooid 0.3, the list names the owner but not the relay's own key."""

    def __init__(self, members=()):
        self.members = {OWN, *members}
        self.imported: list[dict] = []
        self.calls: list[tuple[str, str]] = []

    def check_auth(self, request: httpx.Request) -> str | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Nostr":
            return "no auth"
        event = json.loads(base64.b64decode(token))
        if event["kind"] != 27235 or event["pubkey"] != OWN or event["id"] != event_id(event):
            return "bad event"
        key = PublicKeyXOnly(bytes.fromhex(event["pubkey"]))
        if not key.verify(bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])):
            return "bad signature"
        if abs(time.time() - event["created_at"]) > 60:
            return "stale"
        tags = dict((t[0], t[1]) for t in event["tags"])
        if tags.get("u") != str(request.url) or tags.get("method") != request.method:
            return "wrong url or method"
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if error := self.check_auth(request):
            return httpx.Response(401, json={"error": error})
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path == "/relay/groups/members":
            return httpx.Response(200, json={"members": sorted(self.members)})
        if request.method == "POST" and path == "/relay/groups/import":
            lines = [json.loads(line) for line in request.content.decode().splitlines() if line]
            self.imported += lines
            return httpx.Response(200, json={"imported": len(lines), "invalid": 0, "skipped": 0})
        pubkey = path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            self.members.add(pubkey)
            return httpx.Response(200, json={"message": "member added successfully"})
        if request.method == "DELETE":
            if pubkey in (OWN, RELAY_SELF):
                return httpx.Response(409, json={"error": "cannot remove"})
            self.members.discard(pubkey)
            return httpx.Response(200, json={"message": "member removed"})
        return httpx.Response(404)


def client(zooid: FakeZooid) -> GroupsMembers:
    return GroupsMembers(API, KEY, http=httpx.Client(transport=httpx.MockTransport(zooid)))


def test_sync_adds_missing_and_removes_closed_passes():
    zooid = FakeZooid(members=[A, B])
    result = client(zooid).sync([B, C.upper()])
    assert result == {"added": [C], "removed": [A], "members": 2}
    assert zooid.members == {OWN, B, C}
    assert ("PUT", f"/relay/groups/members/{C}") in zooid.calls
    assert ("DELETE", f"/relay/groups/members/{A}") in zooid.calls


def test_sync_is_idempotent_and_leaves_the_relay_keys_alone():
    zooid = FakeZooid(members=[A])
    client(zooid).sync([A])
    assert zooid.calls == [("GET", "/relay/groups/members")]
    assert OWN in zooid.members


def test_sync_skips_a_member_zooid_will_not_remove():
    zooid = FakeZooid(members=[RELAY_SELF])
    assert client(zooid).sync([])["removed"] == []
    assert RELAY_SELF in zooid.members


def test_failed_change_raises_after_trying_the_rest():
    zooid = FakeZooid()
    real = zooid.__call__

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith(A):
            return httpx.Response(500, text="boom")
        return real(request)

    members = GroupsMembers(API, KEY, http=httpx.Client(transport=httpx.MockTransport(flaky)))
    with pytest.raises(GroupsMembersError):
        members.sync([A, B])
    assert B in zooid.members


def test_api_key_reads_hex_and_refuses_anything_else():
    assert api_key("") is None
    assert api_key("11" * 32).public_key_xonly.format().hex() == OWN
    with pytest.raises(GroupsMembersError):
        api_key("nsec1notahexkey")


def test_relay_whitelist_sync_also_syncs_the_groups_relay(services, monkeypatch):
    zooid = FakeZooid(members=[A])
    services.groups = client(zooid)
    monkeypatch.setattr(core, "active_pubkeys", lambda svc: [B])
    core.sync_relay_whitelist(services, include=[C])
    assert zooid.members == {OWN, B, C}


def test_groups_relay_failure_does_not_stop_the_whitelist(services, monkeypatch):
    services.groups = GroupsMembers(
        API, KEY, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    )
    monkeypatch.setattr(core, "active_pubkeys", lambda svc: [B])
    core.sync_relay_whitelist(services)  # logged, not raised
    with pytest.raises(GroupsMembersError):
        core.sync_relay_whitelist(services, strict=True)


class FakeCommunityRelay:
    """RelayPublisher.query: answers kind-0 filters after checking the AUTH signer is ours."""

    def __init__(self, profiles):
        self.profiles = profiles
        self.filters: list[dict] = []

    def query(self, filters, sign):
        auth = sign({"kind": 22242, "created_at": int(time.time()), "tags": [], "content": ""})
        assert auth["pubkey"] == OWN and auth["id"] == event_id(auth)
        self.filters += filters
        authors = set(filters[0]["authors"])
        return [e for e in self.profiles if e["pubkey"] in authors]


def profile(pubkey: str, name: str, created_at: int) -> dict:
    return {
        "id": name,
        "pubkey": pubkey,
        "kind": 0,
        "created_at": created_at,
        "tags": [],
        "content": json.dumps({"name": name}),
        "sig": "00" * 64,
    }


def test_import_profiles_copies_the_newest_profile_of_each_member():
    zooid = FakeZooid()
    relay = FakeCommunityRelay(
        [profile(A, "old a", 1), profile(A, "new a", 2), profile(B, "b", 1), profile(C, "c", 1)]
    )
    assert client(zooid).import_profiles(relay, [A, B]) == 2
    assert sorted(e["id"] for e in zooid.imported) == ["b", "new a"]
    assert relay.filters[0]["kinds"] == [0] and set(relay.filters[0]["authors"]) == {A, B}


def test_import_profiles_asks_in_chunks_and_skips_an_empty_import():
    zooid = FakeZooid()
    relay = FakeCommunityRelay([])
    assert client(zooid).import_profiles(relay, [A, B, C], chunk=2) == 0
    assert [len(f["authors"]) for f in relay.filters] == [2, 1]
    assert zooid.calls == []


def test_whitelist_sync_imports_the_members_profiles(services, monkeypatch):
    zooid = FakeZooid()
    services.groups = client(zooid)
    services.relay = FakeCommunityRelay([profile(B, "b", 1)])
    monkeypatch.setattr(core, "active_pubkeys", lambda svc: [B])
    core.sync_relay_whitelist(services)
    assert [e["id"] for e in zooid.imported] == ["b"]
