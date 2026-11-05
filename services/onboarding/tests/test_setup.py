"""belfry.<domain>/setup (setup.py): the setup code, admins, pending.env, status.json."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import console, mypass, setup
from belfry_onboarding.app import create_app

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
def web(services, authentik, folder):
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


def csrf_of(page: str) -> str:
    return page.split('name="csrf" value="')[1].split('"')[0]


def test_setup_asks_for_the_code(web):
    page = web.get("/setup").text
    assert 'action="/setup/code"' in page and "belfry setup-code" in page
    assert 'name="name"' in page
    assert 'name="BELFRY_RESEND_API_KEY"' not in page


def test_a_wrong_or_expired_code_is_refused(web, folder):
    r = web.post("/setup/code", data={"code": "WRONG-CODE"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert setup.COOKIE not in web.cookies
    data = json.loads((folder / "code.json").read_text())
    data["codes"][0]["expires"] = time.time() - 1
    (folder / "code.json").write_text(json.dumps(data))
    web.post("/setup/code", data={"code": CODE})
    assert setup.COOKIE not in web.cookies


def test_providers_are_folded_and_secrets_stay_write_only(web, folder):
    web.post("/setup/code", data={"code": CODE.lower()})  # case and dashes don't matter
    page = web.get("/setup").text
    assert '<details id="resend">' in page and '<details id="resend" open>' not in page
    assert "Changes are recorded in the" in page and "<strong>Ana</strong>" in page
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
    assert r.status_code == 303 and r.headers["location"] == "/setup?msg=Resend+saved."
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
    assert "err=" in r.headers["location"] and setup.COOKIE not in web.cookies
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
    assert "Failed" in page and "bad value" in page
