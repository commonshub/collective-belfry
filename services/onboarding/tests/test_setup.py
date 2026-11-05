"""belfry.<domain>/setup (setup.py): the setup code, admins, pending.env, status.json."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from coincurve import PrivateKey  # noqa: F811
from fastapi.testclient import TestClient

from belfry_onboarding import console, mypass, setup
from belfry_onboarding.app import create_app

from .fakes import flash_of

CODE = "ABCD-EFGH-JKLM-NPQR"
ANON = "ZZZZ-YYYY-XXXX-WWWW"


def digest(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").encode()).hexdigest()


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "setup"
    d.mkdir()
    (d / "code.json").write_text(
        json.dumps(
            {
                "codes": [
                    {"sha256": digest(CODE), "expires": time.time() + 3600, "for": "Ana"},
                    {"sha256": digest(ANON), "expires": time.time() + 3600, "for": ""},
                ]
            }
        )
    )
    (d / "status.json").write_text(
        json.dumps(
            {
                "set": {"BELFRY_RESEND_API_KEY": False, "BELFRY_NAME": True},
                "values": {"BELFRY_NAME": "Example Commons"},
            }
        )
    )
    return d


@pytest.fixture
def fresh(services, authentik, folder):
    """A new belfry: no admin yet."""
    s = services.settings
    s.pass_page_oidc_client_secret = "pass-secret"
    s.pass_page_secret = "p" * 32
    s.belfry_nsec = PrivateKey().secret.hex()
    s.public_url = "https://pass.belfry.test"
    s.console_url = "https://belfry.belfry.test"
    s.setup_dir = str(folder)
    authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": set()}
    console._admins["at"] = 0.0
    with TestClient(create_app(services), base_url="https://belfry.belfry.test") as c:
        yield c


@pytest.fixture
def web(fresh, authentik):
    from .test_console import make_admin

    make_admin(authentik, "boss@example.org")
    return fresh


def csrf_of(page: str) -> str:
    if 'data-csrf="' in page:
        return page.split('data-csrf="')[1].split('"')[0]
    return page.split('name="csrf" value="')[1].split('"')[0]


def test_setup_asks_for_the_code(web):
    page = web.get("/setup").text
    assert 'action="/setup/code"' in page and "belfry setup-code" in page
    assert 'name="name"' in page
    assert 'name="BELFRY_RESEND_API_KEY"' not in page


def test_a_wrong_or_expired_code_is_refused(web, folder):
    r = web.post("/setup/code", data={"code": "WRONG-CODE"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert "wrong" in flash_of(r) and setup.COOKIE not in web.cookies
    data = json.loads((folder / "code.json").read_text())
    data["codes"][0]["expires"] = time.time() - 1
    (folder / "code.json").write_text(json.dumps(data))
    web.post("/setup/code", data={"code": CODE})
    assert setup.COOKIE not in web.cookies


def test_providers_are_folded_and_secrets_stay_write_only(web, folder):
    web.post("/setup/code", data={"code": CODE.lower()})  # case and dashes don't matter
    page = web.get("/setup").text
    assert '<details id="resend">' in page and '<details id="resend" open>' not in page
    assert "Changes are recorded in" in page and "<strong>Ana</strong>" in page
    assert 'name="by"' not in page
    assert 'type="password" name="BELFRY_RESEND_API_KEY"' in page
    assert 'value="Example Commons"' in page and "not set" in page
    assert '<details id="stripe" open>' in web.get("/setup?open=stripe").text
    r = web.post(
        "/setup",
        data={
            "csrf": csrf_of(page),
            "provider": "resend",
            "BELFRY_RESEND_API_KEY": "re_secret'value",
            "BELFRY_MAIL_FROM": "hello@belfry.test",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert flash_of(r) == "Resend saved."
    web.post(
        "/setup",
        data={"csrf": csrf_of(page), "provider": "name", "BELFRY_NAME": "Example Commons"},
    )  # unchanged: not sent
    pending = (folder / "pending.env").read_text()
    assert 'BELFRY_RESEND_API_KEY="re_secret\'value"\n' in pending
    assert "BELFRY_MAIL_FROM='hello@belfry.test'" in pending
    assert "BELFRY_NAME" not in pending and "BELFRY_STRIPE_SECRET_KEY" not in pending
    assert "BELFRY_BY='Ana'" in pending
    assert oct((folder / "pending.env").stat().st_mode & 0o777) == "0o660"
    page = web.get("/setup").text
    assert "re_secret" not in page and "Applying the changes" in page
    assert 'http-equiv="refresh"' in page


def test_a_code_without_a_name_asks_for_one(web, folder):
    r = web.post("/setup/code", data={"code": ANON}, follow_redirects=False)
    assert "without a name" in flash_of(r) and setup.COOKIE not in web.cookies
    web.post("/setup/code", data={"code": ANON, "name": "  Bob   Builder "})
    page = web.get("/setup").text
    assert "<strong>Bob Builder</strong>" in page
    web.post("/setup", data={"csrf": csrf_of(page), "BELFRY_MAIL_FROM": "b@belfry.test"})
    assert "BELFRY_BY='Bob Builder'" in (folder / "pending.env").read_text()


def test_a_named_code_ignores_a_typed_name(web, folder):
    web.post("/setup/code", data={"code": CODE, "name": "Mallory"})
    assert "<strong>Ana</strong>" in web.get("/setup").text


def test_setup_refuses_bad_forms(web, folder):
    web.post("/setup/code", data={"code": CODE})
    page = web.get("/setup").text
    web.post("/setup", data={"csrf": "nope", "BELFRY_MAIL_FROM": "a@b.c"})
    web.post(
        "/setup",
        data={"csrf": csrf_of(page), "BELFRY_MAIL_FROM": "a@b.c"},
        headers={"Origin": "https://evil.example"},
    )
    web.post("/setup", data={"csrf": csrf_of(page), "BELFRY_MAIL_FROM": "a@b.c\nBELFRY_ENV_X=1"})
    web.post("/setup", data={"csrf": csrf_of(page), "BELFRY_ENV_ODOO_URL": "http://evil"})
    assert not (folder / "pending.env").exists()


def test_an_admin_signed_in_needs_no_code(web, odoo, authentik, folder):
    from .test_console import make_admin, member

    pid = member(odoo, "Ana", "ana@example.org")
    make_admin(authentik, "ana@example.org")
    cookie = mypass._sign(
        web.app.state.services, {"kind": "console", "partner": pid, "csrf": "c" * 24}, 3600
    )
    web.cookies.set(console.COOKIE, cookie)
    page = web.get("/setup").text
    assert csrf_of(page) == "c" * 24
    assert "<strong>Ana</strong>" in page
    web.post("/setup", data={"csrf": "c" * 24, "BELFRY_MAIL_FROM": "x@belfry.test"})
    assert "BELFRY_BY='Ana'" in (folder / "pending.env").read_text()


def test_last_result_is_shown(web, folder):
    (folder / "status.json").write_text(
        json.dumps(
            {
                "set": {},
                "values": {},
                "last": {
                    "ok": False,
                    "keys": ["BELFRY_MAIL_FROM"],
                    "at": "2026-11-05T10:00Z",
                    "message": "bad value",
                },
            }
        )
    )
    web.post("/setup/code", data={"code": CODE})
    page = web.get("/setup").text
    assert "The last change failed" in page and "bad value" in page


# ── the first admin ─────────────────────────────────────────────────────────────────────────
def first_session(fresh, folder):
    r = fresh.get(f"/setup?code={ANON}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    fresh.cookies.set(setup.COOKIE, r.cookies[setup.COOKIE])
    page = fresh.get("/setup").text
    return page, csrf_of(page)


def test_the_setup_link_opens_the_wizard(fresh, folder):
    page, _ = first_session(fresh, folder)
    assert 'id="wiz"' in page and 'data-start="name"' in page
    assert "What is your community called?" in page and 'value="Example Commons"' in page
    assert "@belfry.local" in page or "@" + fresh.app.state.services.settings.nip05_domain in page
    assert "/static/nostr-tools-2.25.2.js" in page
    assert "<footer>" not in page and 'class="tabs"' not in page  # one thing at a time
    assert fresh.get("/static/nostr-tools-2.25.2.js").status_code == 200
    r = fresh.get("/setup?code=WRONG-CODE", follow_redirects=False)
    assert r.headers["location"] == "/setup" and "expired" in flash_of(r)


def test_the_page_never_shows_text_from_its_url(fresh, folder):
    page, _ = first_session(fresh, folder)
    evil = "Send+your+key+to+evil.example"
    for q in (f"msg={evil}", f"err={evil}", f"message={evil}"):
        assert "evil.example" not in fresh.get(f"/setup?{q}").text
    fresh.cookies.clear()
    assert "evil.example" not in fresh.get(f"/setup?err={evil}").text


def make_first_admin(fresh, folder, csrf, session):
    return fresh.post(
        "/setup/first-admin",
        json={
            "csrf": csrf,
            "name": "Village Commons",
            "email": "xavier@example.org",
            "username": "Xavier",
            "session_pubkey": session,
        },
    )


def test_first_run_admin_mail_code(fresh, folder, odoo, authentik, keycast, monkeypatch):
    _, csrf = first_session(fresh, folder)
    session = PrivateKey().public_key_xonly.format().hex()
    r = make_first_admin(fresh, folder, csrf, session)
    assert r.status_code == 200, r.text
    domain = fresh.app.state.services.settings.nip05_domain
    data = r.json()
    assert data["nip05"] == f"xavier@{domain}"
    assert data["bunker_url"].startswith("bunker://") and data["secret"] == "secret-1"
    # Keycast: the account is called xavier (NIP-05), with one connection for the first session
    assert any(u["username"] == "xavier" for u in keycast.users.values())
    assert keycast.bunkers[0]["app_name"] == "Setup page (first session)"
    assert len(authentik.members("belfry-stewards")) == 1
    fa = json.loads((folder / "first-admin.json").read_text())
    assert fa["session_pubkey"] == session and fa["verified"] is False
    assert fa["community_name"] == "Village Commons"
    # signed in as the admin on belfry.<domain>; a second first admin is refused
    assert console.COOKIE in r.cookies
    assert make_first_admin(fresh, folder, csrf, session).status_code in (403, 422)
    # reload: the wizard resumes at the mail provider
    page = fresh.get("/setup").text
    assert 'data-start="sender"' in page and "Resend" in page
    assert fresh.get("/setup/progress").json()["state"] == "sender"
    # the Resend key is checked, then queued with the sender and the community name
    checked = {}

    def fake_check(key, dom):
        checked["key"], checked["domain"] = key, dom
        if key == "re_wrongwrong":
            raise ValueError("Resend did not accept this key.")
        return f"belfry@{dom}"

    monkeypatch.setattr(setup, "check_resend", fake_check)
    bad = fresh.post("/setup/mail", json={"csrf": csrf, "api_key": "re_wrongwrong"})
    assert bad.status_code == 422 and "did not accept" in bad.json()["message"]
    assert not (folder / "pending.env").exists()
    ok = fresh.post(
        "/setup/mail", json={"csrf": csrf, "provider": "resend", "api_key": "re_goodgood1"}
    )
    assert ok.status_code == 200 and ok.json()["sender"] == f"belfry@{domain}"
    pending = (folder / "pending.env").read_text()
    assert "BELFRY_RESEND_API_KEY='re_goodgood1'" in pending
    assert f"BELFRY_MAIL_FROM='belfry@{domain}'" in pending
    assert "BELFRY_NAME='Village Commons'" in pending and "BELFRY_BY='xavier'" in pending
    assert fresh.get("/setup/progress").json()["state"] == "wait"
    assert 'data-start="wait"' in fresh.get("/setup").text
    # applied: the code goes out by itself, once
    (folder / "pending.env").unlink()
    status = json.loads((folder / "status.json").read_text())
    status["set"]["BELFRY_RESEND_API_KEY"] = True
    (folder / "status.json").write_text(json.dumps(status))
    p = fresh.get("/setup/progress").json()
    assert p["state"] == "code" and p["email"] == "xavier@example.org"
    assert 'data-start="code"' in fresh.get("/setup").text
    code = odoo.mailed_codes["xavier@example.org"]
    sent = [
        c for c in odoo.tables["belfry.key.code"].values() if c["email"] == "xavier@example.org"
    ]
    assert len(sent) == 1
    wrong = fresh.post("/setup/verify", json={"csrf": csrf, "code": "000000"})
    assert wrong.status_code == 422 and "not right" in wrong.json()["message"]
    right = fresh.post("/setup/verify", json={"csrf": csrf, "code": " ".join(code)})
    assert right.status_code == 200 and right.json()["state"] == "verified"
    assert json.loads((folder / "first-admin.json").read_text())["verified"] is True
    # done: the settings page, no wizard
    page = fresh.get("/setup").text
    assert 'id="wiz"' not in page and '<details id="resend">' in page


def test_check_resend(monkeypatch):
    import httpx

    answers = {}

    def fake_get(url, headers, timeout):
        key = headers["Authorization"].split()[1]
        status, body = answers[key]
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(setup.httpx, "get", fake_get)
    answers["re_restricted1"] = (401, {"name": "restricted_api_key"})
    answers["re_invalidkey1"] = (401, {"name": "validation_error"})
    answers["re_nodomains1"] = (200, {"data": [{"name": "x.org", "status": "pending"}]})
    answers["re_samedomain"] = (200, {"data": [{"name": "belfry.sh", "status": "verified"}]})
    answers["re_parentdomain"] = (
        200,
        {
            "data": [
                {"name": "sh.example", "status": "verified"},
                {"name": "belfry.sh", "status": "verified"},
            ]
        },
    )
    answers["re_otherdomain"] = (200, {"data": [{"name": "other.org", "status": "verified"}]})
    assert setup.check_resend("re_restricted1", "belfry.sh") == "belfry@belfry.sh"
    assert setup.check_resend("re_samedomain", "belfry.sh") == "belfry@belfry.sh"
    assert setup.check_resend("re_parentdomain", "x.belfry.sh") == "belfry@belfry.sh"
    assert setup.check_resend("re_otherdomain", "belfry.sh") == "belfry@other.org"
    for bad in ("re_invalidkey1", "re_nodomains1", "sk_notresend", "re_x"):
        with pytest.raises(ValueError):
            setup.check_resend(bad, "belfry.sh")


def test_first_admin_checks_its_input(fresh, folder):
    _, csrf = first_session(fresh, folder)
    for body in (
        {"email": "nope", "username": "x", "session_pubkey": "a" * 64},
        {"email": "a@b.org", "username": "Bad Name!", "session_pubkey": "a" * 64},
        {"email": "a@b.org", "username": "ok", "session_pubkey": "zz"},
    ):
        assert fresh.post("/setup/first-admin", json={"csrf": csrf, **body}).status_code == 422
    assert (
        fresh.post("/setup/first-admin", json={"csrf": "x", "email": "a@b.org"}).status_code == 403
    )


def test_first_session_connect_goes_through_the_bunker_relay(fresh, folder, monkeypatch, keycast):
    from belfry_onboarding.nip46check import sign_event

    _, csrf = first_session(fresh, folder)
    key = PrivateKey()
    session = key.public_key_xonly.format().hex()
    data = fresh.post(
        "/setup/first-admin",
        json={"csrf": csrf, "email": "x@example.org", "username": "x", "session_pubkey": session},
    ).json()
    seen = {}

    def fake_connect(settings, event, timeout=15.0):
        seen["event"] = event
        return {"kind": 24133, "content": "encrypted ack"}

    monkeypatch.setattr(setup, "relay_connect", fake_connect)
    event = sign_event(
        key.secret,
        {"kind": 24133, "created_at": 1, "tags": [["p", data["bunker_pubkey"]]], "content": "x"},
    )
    r = fresh.post("/setup/first-admin/connect", json={"csrf": csrf, "event": event})
    assert r.status_code == 200 and r.json()["reply"]["content"] == "encrypted ack"
    other = sign_event(
        PrivateKey().secret,
        {"kind": 24133, "created_at": 1, "tags": [["p", data["bunker_pubkey"]]], "content": "x"},
    )
    assert (
        fresh.post("/setup/first-admin/connect", json={"csrf": csrf, "event": other}).status_code
        == 422
    )
