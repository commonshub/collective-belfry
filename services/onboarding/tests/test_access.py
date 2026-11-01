"""Access as Nostr events (access.py) and the NIP-44 / NIP-59 code under it (nip44.py)."""
# ruff: noqa: E501  (test vectors)

from __future__ import annotations

import json
import time

import pytest
from coincurve import PrivateKey

from belfry_onboarding import access, core, nip44
from belfry_onboarding.access import ADMIN, GRANT, INVITE, RECEIPT, REVOKE, AccessService
from belfry_onboarding.nostr import hex_to_npub, npub_to_hex

# Made with nostr-tools 2.10 (village app): secret keys 1 and 2, nonce 1.
CONV_1_2 = "c41c775356fd92eadc63ff5a0dc1da211b268cbea22316767095b2871ea1412d"
CT_A = "AgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABee0G5VSK0/9YypIObAtDKfYEAjD35uVkHyB0F4DwrcNaCXlCWZKaArsGrY6M9wnuTMxWfp1RTN9Xga8no+kF5Vsb"
WRAP_FROM_1_TO_2 = json.loads(
    r"""{"kind": 1059, "content": "ApwspNyXGQCVWwsEyNNC9H7LRIEu/hYXStEqP1BQPddbOQzEVGGPOD0YvOM5lYXOibW/SLutY69/bo3w+AjHAPBBGKgv/u73a32D4leP7fllB3TCtGbXkDeliuJmLUOvKfl2lX7R9zkQjmzrvUhiSLBk3FLcFdNGEqTnzVybrIPWEZFm9xQFXK4uoafQ/Yo7k4DJS9aF/iXVbdVb4xnycIRLib3my3RAh7H/sO/P7H1cHJoR1lLtQhWKWCivDLDRMI3i5wjAXQrPz5SOG/mUwEBXlnfup1RekKUTGW+wsC0fjM9TGxf4QHnhkgtiry0iWKTxcyagCgv6gPNzwXWgyz61GWvPnq93Iqp9zcDhxA0JhmJ26FqaXpOxHa+ojy+AJC+oq4MVklf/hyITGNz0Jd+oqHc+rxsVzSdKnQXx6j9oQG8n1QIhuT5O89jIaytghIkfkUW5+pBxcBZge4cXCCuM4yxCxqBnmv2I7Ta14l4I/yPuEXYdFHKCX2M+dA2zKPc+fqYBKc+02gE1CkpL8wiHwEbtblDsFblK6wOrQvtYNnYt/HDwm2Ex3erbzsdvGma33tTy2oGuSd5H68pzwQUmb4mg3sj6Sil2EnUtHOssAkF3T3w+0FpYU3w6oqGF5CR6ZBK/sbjRkIExrCddNGmEbSZBXFYPTWbF7kGxa44r8Dj1XMsAwgKILyP3TUHs7omrYF1VS1Qwo9N5R7Ja+powcIS/fhnNbbNr5LbVqRsmtmH2j3X4asF9DHG0oT5hPvBkUsC4c6yVnuGeiVDPfyEDJRN6G7kjUy3f7s/nMvO+JHf1TsPSWmXCOkndLjj2mPa33P8kSeTkc18n56K/ITLkouXeSzLHhgwdSXa+iiN3dtaqCKqYZ6V6pfyv5Srsejr4W7bXU/07SMLXAwn0zA4vOrcwkkm8jbGpo8fLtHdqTioE6lHRotXQqoDgM2m3LXTG/328SAozzuIVCUKiBdVy6v4KdJc1xZkThT3nkLLwc1UWKyltnLEy+UKticJ+591Hn9MKF3DMEw2MR8oYNnafMRPWpkwbVKr6W9Jz59qyd99mUqTpQwJDfA/syX750nqQaG7d3XqdsyeOS3tt1HN3ZpwRK3+WnMQbZsrNj4w7SgbLw7xKhj+4EQxETSyrAGas/dPDUsc2owft2GVviZTnh1BW5hD1O3X5I9cux/W5Nhq2BaTHSOmdmHHoqed1i41VcKL+50dW4ZHM9PnxlVe2IQGsLhzrGNp013Md204iLrPkx/LlawG5894ftTzee/hw", "created_at": 1793428358, "tags": [["p", "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"]], "pubkey": "596ddad18aabceb83a5a04f66ddeb53c43644997d235b1f2e0eb1ceebbd800ee", "id": "b551f5dbf3666f5eb5b74ef3e864466f34d7dae7034d9a8bfb0625091f6ea738", "sig": "efa32f0b6d7c5868f5cc67c13c133e0e1d08198a82838ad524affcd326f4d255873d2d998dbd577554ae212e7f8138a5c8a4f35d955733f5760766aec05aaf44"}"""
)


def key(n: int) -> PrivateKey:
    return PrivateKey(n.to_bytes(32, "big"))


def pub(k: PrivateKey) -> str:
    return k.public_key_xonly.format().hex()


# -- NIP-44 and gift wraps ------------------------------------------------------------------------


def test_nip44_matches_nostr_tools():
    conv = nip44.conversation_key(key(1).secret, pub(key(2)))
    assert conv.hex() == CONV_1_2
    assert nip44.encrypt("a", conv, (1).to_bytes(32, "big")) == CT_A
    assert nip44.decrypt(CT_A, conv) == "a"
    long = "é" * 200 + " long"
    assert nip44.decrypt(nip44.encrypt(long, conv), conv) == long


def test_nip44_refuses_a_tampered_payload():
    conv = nip44.conversation_key(key(1).secret, pub(key(2)))
    raw = bytearray(__import__("base64").b64decode(CT_A))
    raw[40] ^= 1
    with pytest.raises(nip44.Nip44Error):
        nip44.decrypt(__import__("base64").b64encode(bytes(raw)).decode(), conv)


def test_unwrap_a_nostr_tools_gift_wrap():
    rumor = nip44.unwrap(key(2), WRAP_FROM_1_TO_2)
    assert rumor["kind"] == 8537 and rumor["content"] == "please let me in"
    assert rumor["pubkey"] == pub(key(1)) and rumor["tags"] == [["claim", "abc"]]
    with pytest.raises(nip44.Nip44Error):
        nip44.unwrap(key(3), WRAP_FROM_1_TO_2)


def test_wrap_round_trip_hides_the_author():
    wrapped = nip44.wrap(
        key(1), {"kind": 14, "created_at": 1, "tags": [], "content": "hi"}, pub(key(2))
    )
    assert wrapped["kind"] == 1059 and wrapped["pubkey"] != pub(key(1))
    assert wrapped["tags"] == [["p", pub(key(2))]]
    assert nip44.unwrap(key(2), wrapped)["pubkey"] == pub(key(1))


def test_npub_round_trip():
    hexkey = pub(key(5))
    assert npub_to_hex(hex_to_npub(hexkey)) == hexkey
    with pytest.raises(ValueError):
        npub_to_hex(hex_to_npub(hexkey)[:-1] + "q")


# -- the state ------------------------------------------------------------------------------------

SERVICE, ALICE, BOB, CAROL, STEWARD = key(10), key(11), key(12), key(13), key(14)


def ev(k: PrivateKey, kind: int, tags: list[list[str]], at: int, content: str = ""):
    return nip44.sign_event(k, {"kind": kind, "created_at": at, "tags": tags, "content": content})


def test_compute_follows_admins_through_time():
    now = int(time.time())
    events = [
        # Bob grants before he is an admin: ignored
        ev(BOB, GRANT, [["p", pub(CAROL)]], now - 100),
        ev(SERVICE, GRANT, [["p", pub(ALICE)], ["role", ADMIN]], now - 90),
        ev(
            ALICE,
            GRANT,
            [["p", pub(BOB)], ["role", "volunteer"], ["until", str(now + 60)]],
            now - 80,
        ),
        ev(ALICE, GRANT, [["p", pub(CAROL)], ["until", str(now - 1)]], now - 70),
    ]
    state = access.compute(events, pub(SERVICE))
    assert state.admins(now) == {pub(ALICE)}
    members = state.members(now)
    assert set(members) == {pub(ALICE), pub(BOB)}  # Carol's grant expired
    assert members[pub(BOB)].role == "volunteer" and members[pub(BOB)].by == pub(ALICE)
    assert len(state.ignored) == 1
    # an admin revoked: their later events stop counting
    events += [
        ev(SERVICE, REVOKE, [["p", pub(ALICE)]], now - 60),
        ev(ALICE, GRANT, [["p", pub(CAROL)]], now - 50),
    ]
    state = access.compute(events, pub(SERVICE))
    assert state.admins(now) == set() and pub(CAROL) not in state.members(now)
    # stewards are admins by right
    state = access.compute(events, pub(SERVICE), stewards=[pub(STEWARD)])
    assert state.admins(now) == {pub(STEWARD)}


def test_parse_answer():
    a = access.parse_answer("Approve 3fa2c1d0 volunteer 14 days")
    assert (a.approve, a.ref, a.role, a.days) == (True, "3fa2c1d0", "volunteer", 14)
    a = access.parse_answer("yes")
    assert (a.approve, a.ref, a.role, a.days) == (True, None, "member", None)
    a = access.parse_answer("ok, as steward for 30d")
    assert (a.role, a.days) == ("steward", 30)
    assert access.parse_answer("decline 3fa2c1d0").approve is False
    assert access.parse_answer("thanks!") is None


# -- the service ----------------------------------------------------------------------------------


class Relay:
    """Stands in for the groups relay: keeps what the service publishes."""

    def __init__(self):
        self.events: list[dict] = []

    def publish(self, event, sign=None):
        self.events.append(event)

    def dms(self, k: PrivateKey) -> list[dict]:
        out = []
        for e in self.events:
            if e["kind"] == 1059 and e["tags"][0][1] == pub(k):
                out.append(nip44.unwrap(k, e))
        return out


def make_service(relay: Relay, stewards=(), members=None) -> AccessService:
    calls = members if members is not None else []
    svc = AccessService(
        SERVICE,
        "wss://groups-relay.belfry.test",
        community="Test Village",
        app_url="https://app.test/",
        stewards=lambda: stewards,
        on_members=calls.append,
    )
    svc.publish = relay.publish  # type: ignore[method-assign]
    svc.refresh_stewards()
    svc.synced = True
    svc.add(e for e in relay.events if e["kind"] != 1059)
    return svc


def request(k: PrivateKey, note: str = "", **tags: str) -> dict:
    rumor = {
        "kind": access.REQUEST,
        "created_at": int(time.time()),
        "tags": [[n, v] for n, v in tags.items()],
        "content": note,
    }
    return nip44.wrap(k, rumor, pub(SERVICE))


def answer(k: PrivateKey, text: str) -> dict:
    rumor = {
        "kind": 14,
        "created_at": int(time.time()),
        "tags": [["p", pub(SERVICE)]],
        "content": text,
    }
    return nip44.wrap(k, rumor, pub(SERVICE))


def test_first_request_for_admin_wins_then_requests_go_to_the_admin():
    relay, members = Relay(), []
    svc = make_service(relay, members=members)
    assert svc.public_state()["needs_admin"] is True
    svc.handle_wrap(request(ALICE, role=ADMIN))
    assert svc.admins() == [pub(ALICE)]
    assert svc.public_state()["needs_admin"] is False
    assert members[-1] == [pub(ALICE)]
    assert "admin" in relay.dms(ALICE)[-1]["content"]

    # the second one asking for admin is an ordinary request, sent to Alice
    wrapped = request(BOB, "I run the coffee corner", role=ADMIN, name="Bob")
    svc.handle_wrap(wrapped)
    assert svc.admins() == [pub(ALICE)] and pub(BOB) not in svc.members()
    dm = relay.dms(ALICE)[-1]
    assert "Bob (npub1" in dm["content"] and "I run the coffee corner" in dm["content"]
    assert f"approve {wrapped['id'][:8]}" in dm["content"]
    assert f"https://app.test/#/admin/requests/{wrapped['id']}" in dm["content"]
    assert ["requester", pub(BOB)] in dm["tags"] and ["note", "I run the coffee corner"] in dm[
        "tags"
    ]
    receipt = [e for e in relay.events if e["kind"] == RECEIPT][-1]
    assert ["p", pub(BOB)] in receipt["tags"] and ["role", ADMIN] in receipt["tags"]
    assert [r.pubkey for r in svc.state.pending(svc.now())] == [pub(BOB)]

    # Alice answers in her DM client: Bob is in for 14 days as a volunteer
    svc.handle_wrap(answer(ALICE, f"approve {wrapped['id'][:8]} volunteer 14"))
    grant = svc.state.members(svc.now())[pub(BOB)]
    assert grant.role == "volunteer" and grant.by == pub(SERVICE)
    assert abs(grant.until - (svc.now() + 14 * 86400)) < 5
    assert members[-1] == sorted([pub(ALICE), pub(BOB)])
    assert relay.dms(ALICE)[-1]["content"].startswith("Done:")
    assert "volunteer" in relay.dms(BOB)[-1]["content"]
    granted = [e for e in relay.events if e["kind"] == GRANT][-1]
    assert ["approved_by", pub(ALICE)] in granted["tags"]
    assert svc.state.pending(svc.now()) == []


def test_decline_and_answers_from_others():
    relay = Relay()
    svc = make_service(relay, stewards=[pub(STEWARD)])
    assert svc.public_state()["needs_admin"] is False
    svc.handle_wrap(request(CAROL, role=ADMIN))  # stewards exist: no first-admin claim
    assert pub(CAROL) not in svc.admins()
    svc.handle_wrap(answer(BOB, "approve"))  # Bob is no admin: ignored
    assert svc.members() == []
    svc.handle_wrap(answer(STEWARD, "no"))  # the only pending request: declined
    assert svc.state.pending(svc.now()) == [] and svc.members() == []
    revoke = [e for e in relay.events if e["kind"] == REVOKE][-1]
    assert ["declined_by", pub(STEWARD)] in revoke["tags"]


def test_ambiguous_answer_asks_which():
    relay = Relay()
    svc = make_service(relay, stewards=[pub(STEWARD)])
    svc.handle_wrap(request(ALICE))
    svc.handle_wrap(request(BOB))
    svc.handle_wrap(answer(STEWARD, "approve"))
    assert svc.members() == []
    assert relay.dms(STEWARD)[-1]["content"].startswith("Which request?")


def test_invite_codes():
    relay = Relay()
    svc = make_service(relay, stewards=[pub(STEWARD)])
    now = int(time.time())
    invite = ev(
        STEWARD,
        INVITE,
        [["code", access.code_hash("OSV-1234")], ["role", "speaker"], ["days", "3"], ["max", "1"]],
        now,
    )
    relay.publish(invite)
    svc.add([invite])
    svc.handle_wrap(request(ALICE, claim=" OSV-1234 "))
    grant = svc.state.members(svc.now())[pub(ALICE)]
    assert grant.role == "speaker" and abs(grant.until - (now + 3 * 86400)) < 5
    assert svc.state.invites[invite["id"]].uses == 1
    # used up: Bob's request goes to the admins instead
    svc.handle_wrap(request(BOB, claim="OSV-1234"))
    assert pub(BOB) not in svc.members()
    assert "not valid" in relay.dms(STEWARD)[-1]["content"]


def test_restart_does_not_act_twice():
    relay = Relay()
    svc = make_service(relay, stewards=[pub(STEWARD)])
    wraps = [request(ALICE, "hello"), request(BOB)]
    for w in wraps:
        svc.handle_wrap(w)
    ans = answer(STEWARD, f"approve {wraps[0]['id'][:8]}")
    svc.handle_wrap(ans)
    count = len(relay.events)
    again = make_service(relay, stewards=[pub(STEWARD)])
    for w in [*wraps, ans]:
        again.handle_wrap(w)
    assert len(relay.events) == count
    assert again.members() == [pub(ALICE)]


def test_operator_commands():
    relay = Relay()
    svc = make_service(relay)
    svc.set_role(pub(ALICE), ADMIN)
    svc.set_role(pub(BOB), "member", days=1)
    assert svc.admins() == [pub(ALICE)] and set(svc.members()) == {pub(ALICE), pub(BOB)}
    svc.revoke(pub(ALICE))
    assert svc.admins() == [] and svc.public_state()["needs_admin"] is True
    with pytest.raises(ValueError):
        svc.set_role(pub(BOB), "Not A Role")


def test_granted_members_join_the_relay_whitelist_and_discovery(services, client):
    relay = Relay()
    svc = make_service(relay)
    svc.set_role(pub(ALICE), ADMIN)
    services.access = svc
    assert pub(ALICE) in core.active_pubkeys(services)
    doc = client.get("/.well-known/belfry.json").json()
    assert doc["access"]["admins"] == [pub(ALICE)]
    assert doc["access"]["needs_admin"] is False
    assert doc["access"]["service"] == pub(SERVICE)
    assert doc["access"]["kinds"]["request"] == 8537
