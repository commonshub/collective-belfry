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


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "setup"
    d.mkdir()
    (d / "code.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(CODE.replace("-", "").encode()).hexdigest(),
                "expires": time.time() + 3600,
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
    assert 'name="BELFRY_RESEND_API_KEY"' not in page


def test_a_wrong_or_expired_code_is_refused(web, folder):
    r = web.post("/setup/code", data={"code": "WRONG-CODE"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert setup.COOKIE not in web.cookies
    data = json.loads((folder / "code.json").read_text())
    data["expires"] = time.time() - 1
    (folder / "code.json").write_text(json.dumps(data))
    web.post("/setup/code", data={"code": CODE})
    assert setup.COOKIE not in web.cookies


def test_the_code_opens_the_form_and_secrets_stay_write_only(web, folder):
    web.post("/setup/code", data={"code": CODE.lower()})  # case and dashes don't matter
    page = web.get("/setup").text
    assert 'type="password" name="BELFRY_RESEND_API_KEY"' in page
    assert 'value="Example Commons"' in page and "missing" in page
    r = web.post(
        "/setup",
        data={
            "csrf": csrf_of(page),
            "BELFRY_RESEND_API_KEY": "re_secret'value",
            "BELFRY_NAME": "Example Commons",  # unchanged: not sent
            "BELFRY_MAIL_FROM": "hello@belfry.test",
            "BELFRY_STRIPE_SECRET_KEY": "",  # empty secret: kept
            "by": "Ana",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/setup?msg=Saved."
    pending = (folder / "pending.env").read_text()
    assert 'BELFRY_RESEND_API_KEY="re_secret\'value"\n' in pending
    assert "BELFRY_MAIL_FROM='hello@belfry.test'" in pending
    assert "BELFRY_NAME" not in pending and "BELFRY_STRIPE_SECRET_KEY" not in pending
    assert "BELFRY_BY='Ana via the setup code'" in pending
    assert oct((folder / "pending.env").stat().st_mode & 0o777) == "0o660"
    page = web.get("/setup").text
    assert "re_secret" not in page and "Applying the changes" in page
    assert 'http-equiv="refresh"' in page


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
