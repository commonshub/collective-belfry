"""The push gateway against fake APNs and FCM endpoints (respx): NIP-98, limits, the provider
tokens and their caches, dead tokens, and a gateway without credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

import httpx
import jwt
import pytest
import respx
from coincurve import PrivateKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from push_gateway.app import create_app, token_ref
from push_gateway.nip98 import event_id
from push_gateway.settings import Settings

URL = "https://push.gw.test/v1/send"
APNS_TOKEN = "ab" * 32
FCM_TOKEN = "fcm-token:" + "x" * 140
SANDBOX = "https://api.sandbox.push.apple.com/3/device/"
PROD = "https://api.push.apple.com/3/device/"
OAUTH = "https://oauth2.test/token"
FCM_SEND = "https://fcm.googleapis.com/v1/projects/belfry-app/messages:send"


def pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def keys():
    return {
        "apns": ec.generate_private_key(ec.SECP256R1()),
        "fcm": rsa.generate_private_key(65537, 2048),
    }


def settings(keys, apns=True, fcm=True, **over) -> Settings:
    account = {
        "type": "service_account",
        "project_id": "belfry-app",
        "client_email": "push@belfry-app.iam.gserviceaccount.com",
        "private_key": pem(keys["fcm"]),
        "private_key_id": "k1",
        "token_uri": OAUTH,
    }
    return Settings(
        PUBLIC_URL="https://push.gw.test",
        APNS_KEY=pem(keys["apns"]) if apns else "",
        APNS_KEY_ID="KEY123" if apns else "",
        APNS_TEAM_ID="TEAM45" if apns else "",
        FCM_SERVICE_ACCOUNT=json.dumps(account) if fcm else "",
        APNS_KEY_PATH="/nonexistent",
        FCM_SERVICE_ACCOUNT_PATH="/nonexistent",
        **over,
    )


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def auth(key: PrivateKey, body: bytes, url: str = URL, at: int | None = None) -> str:
    e = {
        "pubkey": key.public_key_xonly.format().hex(),
        "created_at": int(time.time()) if at is None else at,
        "kind": 27235,
        "tags": [["u", url], ["method", "POST"], ["payload", hashlib.sha256(body).hexdigest()]],
        "content": "",
    }
    e["id"] = event_id(e)
    e["sig"] = key.sign_schnorr(bytes.fromhex(e["id"])).hex()
    return "Nostr " + base64.b64encode(json.dumps(e).encode()).decode()


def payload() -> str:
    return base64.b64encode(os.urandom(300)).decode()


_n = iter(range(100_000))


def send(c, key, at=None, **over):
    data = {
        "platform": "apns",
        "token": APNS_TOKEN,
        "env": "sandbox",
        "payload": payload(),
        "collapse": "garden",
        "urgent": True,
    } | over
    body = json.dumps(data).encode()
    # the same body and second would be the same event (accepted once): date each one apart
    at = (int(time.time()) if at is None else int(at)) - next(_n) % 50
    return c.post(
        URL.replace("https://push.gw.test", ""),
        content=body,
        headers={"Authorization": auth(key, body, at=at)},
    )


def test_apns_send_with_a_cached_provider_token(keys):
    belfry = PrivateKey()
    clock = Clock()
    seen = []
    with respx.mock() as mock:
        mock.post(SANDBOX + APNS_TOKEN).mock(
            side_effect=lambda r: seen.append(r) or httpx.Response(200)
        )
        mock.post(PROD + APNS_TOKEN).mock(return_value=httpx.Response(200))
        with TestClient(create_app(settings(keys), clock=clock)) as c:
            r = send(c, belfry)
            assert r.status_code == 200 and r.json() == {"ok": True}
            assert send(c, belfry, env="production", urgent=False).status_code == 200
            assert send(c, belfry).status_code == 200
            clock.now += 51 * 60  # a new provider token after 50 minutes
            assert send(c, belfry, at=clock.now).status_code == 200
    req = seen[0]
    assert req.headers["apns-topic"] == "sh.belfry.app"
    assert req.headers["apns-push-type"] == "alert" and req.headers["apns-priority"] == "10"
    assert req.headers["apns-collapse-id"] == "garden"
    body = json.loads(req.content)
    assert body["aps"]["mutable-content"] == 1 and body["aps"]["thread-id"] == "garden"
    assert body["aps"]["alert"] == {"title": "Belfry", "body": "New notification"}
    assert base64.b64decode(body["b"])
    tok = req.headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(tok) == {"alg": "ES256", "kid": "KEY123", "typ": "JWT"}
    claims = jwt.decode(tok, keys["apns"].public_key(), algorithms=["ES256"])
    assert claims["iss"] == "TEAM45"
    toks = [r.headers["authorization"] for r in seen]
    assert toks[0] == toks[1] != toks[2]


def test_apns_dead_tokens_refresh_and_errors(keys):
    belfry = PrivateKey()
    answers = [
        httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
        httpx.Response(200),
        httpx.Response(410, json={"reason": "Unregistered"}),
        httpx.Response(400, json={"reason": "BadDeviceToken"}),
        httpx.Response(400, json={"reason": "PayloadTooLarge"}),
        httpx.Response(500, json={"reason": "InternalServerError"}),
    ]
    with respx.mock() as mock:
        route = mock.post(SANDBOX + APNS_TOKEN).mock(side_effect=answers)
        with TestClient(create_app(settings(keys))) as c:
            assert send(c, belfry).status_code == 200  # refreshed once
            assert route.call_count == 2
            assert send(c, belfry).json() == {"error": "gone"}
            assert send(c, belfry).status_code == 410
            assert send(c, belfry).json() == {"error": "invalid"}
            assert send(c, belfry).json() == {"error": "upstream"}


def test_fcm_send_and_its_access_token(keys):
    belfry = PrivateKey()
    clock = Clock()
    grants, sent = [], []
    with respx.mock() as mock:
        mock.post(OAUTH).mock(
            side_effect=lambda r: (
                grants.append(r)
                or httpx.Response(
                    200, json={"access_token": f"at-{len(grants)}", "expires_in": 3599}
                )
            )
        )
        answers = iter(
            [
                httpx.Response(200, json={"name": "m1"}),
                httpx.Response(200, json={"name": "m2"}),
                httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}}),
                httpx.Response(200, json={"name": "m3"}),
                httpx.Response(
                    404,
                    json={
                        "error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}
                    },
                ),
                httpx.Response(
                    400,
                    json={
                        "error": {
                            "status": "INVALID_ARGUMENT",
                            "message": "The registration token is not a valid FCM registration token",  # noqa: E501
                        }
                    },
                ),
                httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
            ]
        )
        mock.post(FCM_SEND).mock(side_effect=lambda r: sent.append(r) or next(answers))
        with TestClient(create_app(settings(keys), clock=clock)) as c:
            assert send(c, belfry, platform="fcm", token=FCM_TOKEN).status_code == 200
            assert send(c, belfry, platform="fcm", token=FCM_TOKEN, urgent=False).status_code == 200
            assert len(grants) == 1  # cached
            assert send(c, belfry, platform="fcm", token=FCM_TOKEN).status_code == 200  # 401 → new
            assert len(grants) == 2
            assert send(c, belfry, platform="fcm", token=FCM_TOKEN).status_code == 410
            assert send(c, belfry, platform="fcm", token=FCM_TOKEN).status_code == 410
            assert send(c, belfry, platform="fcm", token=FCM_TOKEN).json() == {"error": "rate"}
    msg = json.loads(sent[0].content)["message"]
    assert msg["token"] == FCM_TOKEN and set(msg) == {"token", "data", "android"}  # data only
    assert msg["android"]["priority"] == "high" and msg["android"]["collapse_key"] == "garden"
    assert json.loads(sent[1].content)["message"]["android"]["priority"] == "normal"
    assert sent[0].headers["authorization"] == "Bearer at-1"
    form = dict(x.split("=", 1) for x in grants[0].content.decode().split("&"))
    assert form["grant_type"] == "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
    claims = jwt.decode(
        form["assertion"], keys["fcm"].public_key(), algorithms=["RS256"], audience=OAUTH
    )
    assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"


def test_auth_and_input_are_checked(keys):
    belfry = PrivateKey()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(SANDBOX + APNS_TOKEN).mock(return_value=httpx.Response(200))
        mock.post(PROD + APNS_TOKEN).mock(return_value=httpx.Response(200))  # env: production
        with TestClient(create_app(settings(keys))) as c:
            body = json.dumps(
                {"platform": "apns", "token": APNS_TOKEN, "payload": payload()}
            ).encode()
            path = "/v1/send"
            assert c.post(path, content=body).status_code == 401
            wrong_url = auth(belfry, body, url="https://elsewhere.test/v1/send")
            assert (
                c.post(path, content=body, headers={"Authorization": wrong_url}).status_code == 401
            )
            other_body = auth(belfry, b"{}")
            assert (
                c.post(path, content=body, headers={"Authorization": other_body}).status_code == 401
            )
            old = auth(belfry, body, at=int(time.time()) - 300)
            assert c.post(path, content=body, headers={"Authorization": old}).status_code == 401
            once = auth(belfry, body)
            assert c.post(path, content=body, headers={"Authorization": once}).status_code == 200
            assert c.post(path, content=body, headers={"Authorization": once}).status_code == 401
            for bad in (
                {"platform": "mpns"},
                {"token": "no spaces allowed"},
                {"payload": "x" * 3001},
                {"payload": "not base64!"},
                {"collapse": "c" * 65},
                {"env": "staging"},
                {"urgent": "yes"},
            ):
                assert send(c, belfry, **bad).json() == {"error": "invalid"}, bad


def test_rate_limits_per_token_and_per_belfry(keys):
    belfry, other = PrivateKey(), PrivateKey()
    clock = Clock()
    with respx.mock() as mock:
        mock.post(url__startswith=SANDBOX).mock(return_value=httpx.Response(200))
        app = create_app(settings(keys, LIMIT_PER_TOKEN=3, LIMIT_PER_BELFRY=5), clock=clock)
        with TestClient(app) as c:
            assert [send(c, belfry).status_code for _ in range(4)] == [200, 200, 200, 429]
            assert send(c, other).status_code == 429  # the token's limit, whoever sends
            # refused sends don't count: the belfry has used 3 of its 5
            assert send(c, belfry, token="cd" * 32).status_code == 200
            assert send(c, belfry, token="ef" * 32).status_code == 200
            assert send(c, belfry, token="01" * 32).status_code == 429  # the belfry's 5
            assert send(c, other, token="01" * 32).status_code == 200
            clock.now += 3601
            assert send(c, belfry, at=clock.now).status_code == 200


def test_without_credentials_it_answers_not_configured(keys):
    belfry = PrivateKey()
    with TestClient(create_app(settings(keys, apns=False, fcm=False))) as c:
        assert c.get("/health").json() == {"ok": True, "apns": False, "fcm": False}
        assert send(c, belfry).status_code == 503
        assert send(c, belfry, platform="fcm", token=FCM_TOKEN).json() == {
            "error": "not_configured"
        }
    with respx.mock() as mock:
        mock.post(SANDBOX + APNS_TOKEN).mock(return_value=httpx.Response(200))
        with TestClient(create_app(settings(keys, fcm=False))) as c:
            assert send(c, belfry).status_code == 200
            assert send(c, belfry, platform="fcm", token=FCM_TOKEN).status_code == 503


def test_logs_carry_no_payload_and_no_token(keys, caplog):
    belfry = PrivateKey()
    p = payload()
    with respx.mock() as mock:
        mock.post(SANDBOX + APNS_TOKEN).mock(return_value=httpx.Response(200))
        with TestClient(create_app(settings(keys))) as c, caplog.at_level("INFO"):
            send(c, belfry, payload=p)
    text = caplog.text
    assert p not in text and APNS_TOKEN not in text and token_ref(APNS_TOKEN) in text
