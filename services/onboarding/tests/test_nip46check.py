"""NIP-44 v2 and the NIP-46 client's building blocks (no relay needed)."""

import pytest
from coincurve import PrivateKey

from belfry_onboarding import nip46check as n
from belfry_onboarding.blossom import event_id, verify_signature


# padding table from NIP-44's test vectors (unpadded length -> padded length)
@pytest.mark.parametrize(
    "unpadded,padded",
    [(16, 32), (32, 32), (33, 64), (37, 64), (45, 64), (49, 64), (64, 64), (65, 96), (100, 128),
     (111, 128), (200, 224), (250, 256), (320, 320), (383, 384), (384, 384), (400, 448), (500, 512),
     (512, 512), (515, 640), (700, 768), (800, 896), (900, 1024), (1020, 1024), (65535, 65536)],
)
def test_padded_len_matches_the_spec(unpadded, padded):
    assert n.padded_len(unpadded) == padded


def test_conversation_key_is_symmetric_and_the_round_trip_works():
    a, b = PrivateKey(), PrivateKey()
    ka = n.conversation_key(a.secret, b.public_key_xonly.format())
    kb = n.conversation_key(b.secret, a.public_key_xonly.format())
    assert ka == kb
    for text in ["a", "hello village", "ü" * 40, "x" * 1000]:
        assert n.nip44_decrypt(kb, n.nip44_encrypt(ka, text)) == text


def test_nip44_rejects_a_tampered_payload_and_a_foreign_key():
    a, b, c = PrivateKey(), PrivateKey(), PrivateKey()
    k = n.conversation_key(a.secret, b.public_key_xonly.format())
    payload = n.nip44_encrypt(k, "secret")
    with pytest.raises(ValueError):
        n.nip44_decrypt(n.conversation_key(c.secret, b.public_key_xonly.format()), payload)
    broken = payload[:-6] + ("A" if payload[-6] != "A" else "B") + payload[-5:]
    with pytest.raises(ValueError):
        n.nip44_decrypt(k, broken)
    with pytest.raises(ValueError):
        n.nip44_decrypt(k, "#" + payload)


def test_sign_event_produces_a_valid_schnorr_signature():
    key = PrivateKey()
    event = n.sign_event(
        key.secret, {"created_at": 1, "kind": 24133, "tags": [["p", "ab" * 32]], "content": "x"}
    )
    assert event["pubkey"] == key.public_key_xonly.format().hex()
    assert event["id"] == event_id(event)
    assert verify_signature(event)


def test_parse_bunker_url_and_host_map():
    pk = "ab" * 32
    url = f"bunker://{pk}?relay=ws%3A%2F%2Flocalhost%3A3080&secret=s3cret"
    assert n.parse_bunker_url(url) == (pk, ["ws://localhost:3080"], "s3cret")
    assert n.HostMap("localhost:3080=keycast-relay:8080")("ws://localhost:3080") == "ws://keycast-relay:8080"
    assert n.HostMap("")("wss://bunker.example.org") == "wss://bunker.example.org"
    assert (
        n.HostMap("ws://localhost:3080=wss://127.0.0.1:8443")("ws://localhost:3080")
        == "wss://127.0.0.1:8443"
    )
    with pytest.raises(ValueError):
        n.parse_bunker_url("https://example.org/")
    with pytest.raises(ValueError):
        n.parse_bunker_url(f"bunker://{pk}?secret=x")
