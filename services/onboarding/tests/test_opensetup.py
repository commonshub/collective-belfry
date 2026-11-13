"""A new belfry's setup is open to the first comer (setup.py), `belfry setup-reset` undoes a
claim, and agents ask to join by themselves (agentrequests.py)."""

# ruff: noqa: F811
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import agentrequests, apps, console, core, mailer, setup
from belfry_onboarding.app import create_app

from .test_guests import admin_with_key, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import BASE, post
from .test_setup import folder, fresh, make_first_admin  # noqa: F401 — fixtures

REVERT = Path(__file__).resolve().parents[3] / "infra" / "scripts" / "setup-revert.py"


def pk(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


def open_wizard(client) -> str:
    """GET /setup with no code: the wizard, its csrf, and an open setup cookie."""
    r = client.get("/setup")
    assert r.status_code == 200 and 'data-start="name"' in r.text, r.text[:300]
    assert setup.COOKIE in r.cookies and setup.COOKIE in client.cookies
    return r.text.split('data-csrf="')[1].split('"')[0]


# ── first come, first served ────────────────────────────────────────────────────────────────
def test_a_new_belfry_needs_no_code(fresh, folder, authentik):
    csrf = open_wizard(fresh)
    assert "XXXX-XXXX" not in fresh.get("/setup").text  # still the wizard, no code asked
    # a second visitor opens the page at the same time
    other = TestClient(fresh.app, base_url="https://belfry.belfry.test")
    other_csrf = open_wizard(other)
    r = make_first_admin(fresh, folder, csrf, pk(PrivateKey()))
    assert r.status_code == 200, r.text
    assert len(authentik.members("belfry-stewards")) == 1
    # the other one's open session ended with the first admin: refused, nothing created
    r = make_first_admin(other, folder, other_csrf, pk(PrivateKey()))
    assert r.status_code == 403
    assert len(authentik.members("belfry-stewards")) == 1
    # and /setup no longer opens for strangers: it says someone set it up
    stranger = TestClient(fresh.app, base_url="https://belfry.belfry.test")
    page = stranger.get("/setup").text
    assert "Someone is setting up this belfry already" in page and "belfry setup-reset" in page
    assert setup.COOKIE not in stranger.cookies
    # the claimant keeps going (mail step): their session is no longer the open one
    assert fresh.get("/setup/progress").json()["state"] == "sender"


def test_two_at_once_make_one_admin(fresh, folder, authentik, monkeypatch):
    svc = fresh.app.state.services
    real = setup._create_first_admin

    def slow(*a, **k):
        time.sleep(0.3)  # both are past the page's check when the first one writes
        return real(*a, **k)

    monkeypatch.setattr(setup, "_create_first_admin", slow)
    results: list[str] = []

    def claim(email: str, user: str) -> None:
        try:
            setup.create_first_admin(svc, email, user, pk(PrivateKey()))
            results.append("admin")
        except setup.AdminTaken:
            results.append("taken")

    threads = [
        threading.Thread(target=claim, args=("ana@example.org", "ana")),
        threading.Thread(target=claim, args=("bob@example.org", "bob")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["admin", "taken"]
    assert len(authentik.members("belfry-stewards")) == 1


def test_the_taken_message_is_fixed(fresh, folder, monkeypatch):
    csrf = open_wizard(fresh)

    def taken(*a, **k):
        raise setup.AdminTaken(setup.TAKEN)

    monkeypatch.setattr(setup, "create_first_admin", taken)
    r = make_first_admin(fresh, folder, csrf, pk(PrivateKey()))
    assert r.status_code == 409 and r.json() == {"message": setup.TAKEN, "error": "taken"}


# ── setup-reset ─────────────────────────────────────────────────────────────────────────────
def test_setup_reset_undoes_a_claim(fresh, folder, authentik, keycast, monkeypatch):
    svc = fresh.app.state.services
    assert setup.reset(svc)["removed"] == []  # nobody yet: nothing to do
    csrf = open_wizard(fresh)
    assert make_first_admin(fresh, folder, csrf, pk(PrivateKey())).status_code == 200
    monkeypatch.setattr(setup, "check_resend", lambda key, domain: f"belfry@{domain}")
    r = fresh.post("/setup/mail", json={"csrf": csrf, "api_key": "re_strangers_key"})
    assert r.status_code == 200, r.text
    changes = json.loads((folder / setup.CHANGES_FILE).read_text())
    assert changes["BELFRY_RESEND_API_KEY"] == {"secret": True}
    assert changes["BELFRY_NAME"] == {"secret": False, "before": "Example Commons"}
    assert "re_strangers_key" not in json.dumps(changes)

    dry = setup.reset(svc, dry_run=True)
    assert dry["removed"] == ["xavier@example.org"] and (folder / "first-admin.json").exists()
    out = setup.reset(svc)
    assert out["removed"] == ["xavier@example.org"] and out["open"] is True
    assert set(out["revert"]) == {"BELFRY_RESEND_API_KEY", "BELFRY_MAIL_FROM", "BELFRY_NAME"}
    assert not (folder / "first-admin.json").exists()
    assert list(folder.glob("first-admin.reset-*.json"))
    assert authentik.members("belfry-stewards") == set()
    passes = svc.odoo.search_read("belfry.pass", [["state", "=", "active"]], ["id"])
    assert passes == []
    # the claimant's cookies stop working; /setup is open to the next comer
    data = json.loads((folder / setup.RESET_FILE).read_text())
    data["at"] = int(time.time()) + 5  # their cookie was made before the reset
    (folder / setup.RESET_FILE).write_text(json.dumps(data))
    assert fresh.post("/setup/mail", json={"csrf": csrf, "api_key": "re_x"}).status_code == 403
    console._admins["at"] = 0.0
    nxt = TestClient(fresh.app, base_url="https://belfry.belfry.test")
    assert 'data-start="name"' in nxt.get("/setup").text
    assert setup.reset(svc)["removed"] == []  # again: nothing left to do


def test_setup_revert_clears_secrets_and_puts_values_back(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'COMMUNITY_NAME="Strangers Inc"\nCOMPANY_NAME="Strangers Inc"\nSMTP_HOST=smtp.resend.com\n'
        "SMTP_PORT=587\nSMTP_USER=resend\nSMTP_ENCRYPTION=starttls\nMAIL_FROM=x@evil.example\n"
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "smtp_password").write_text("re_strangers_key")
    reset = {
        "revert": {
            "BELFRY_RESEND_API_KEY": {"secret": True},
            "BELFRY_MAIL_FROM": {"secret": False, "before": "belfry@example.org"},
            "BELFRY_NAME": {"secret": False, "before": "Example Commons"},
        }
    }
    r = subprocess.run(
        [sys.executable, str(REVERT), json.dumps(reset), str(env), str(secrets)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == "1" and "re_strangers_key" not in r.stderr
    text = env.read_text()
    assert 'COMMUNITY_NAME="Example Commons"' in text and "SMTP_HOST=mailpit" in text
    assert "MAIL_FROM=belfry@example.org" in text and "SMTP_USER=\n" in text
    assert (secrets / "smtp_password").read_text() == ""
    r = subprocess.run(
        [sys.executable, str(REVERT), json.dumps({"revert": {}}), str(env), str(secrets)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == "0"


# ── agents ask to join ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def world(rsvc, monkeypatch):  # noqa: F811
    rsvc.settings.public_url = BASE
    rsvc.settings.console_url = "https://belfry.belfry.test"
    key, ada = admin_with_key(rsvc)
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {pk(key)})
    monkeypatch.setattr(console, "admin_emails", lambda svc: {"admin@belfry.test"})
    monkeypatch.setattr(core, "_keycast_pubkey_for", lambda svc, row: None)
    return rsvc, key, ada


def test_an_agent_asks_and_an_admin_lets_it_in(world):
    rsvc, admin, ada = world
    bot = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        md = c.get("/console/agents.md").text
        assert "POST https://belfry.belfry.test/api/v1/agent-requests" in md
        assert "kind 9007" in md and "/agents" in md
        doc = c.get("/.well-known/belfry.json").json()
        assert doc["agents_join"] == {
            "instructions": "https://belfry.belfry.test/agents.md",
            "api": "https://belfry.belfry.test/api/v1/agent-requests",
        }
        assert doc["skills"]["join_as_agent"] == "https://belfry.belfry.test/agents.md"
        name = "<b>Helper</b>\nbot"
        r = post(c, bot, "/api/agent-requests", {"name": name, "about": "Sets up <i>channels</i>"})
        assert r.status_code == 202 and r.json()["state"] == "pending", r.text
        rid = r.json()["id"]
        # the admin is told, the agent's words as plain text, never in the subject
        mail = mailer.SENT[-1]
        assert mail["To"] == "admin@belfry.test" and "Helper" not in mail["Subject"]
        html_part = mail.get_body(preferencelist=("html",)).get_content()
        assert "<b>Helper</b>" not in html_part and "&lt;b&gt;Helper&lt;/b&gt; bot" in html_part
        assert "https://belfry.belfry.test/agents" in html_part
        # asking again while it waits: the same request, no second mail
        n = len(mailer.SENT)
        time.sleep(1.1)
        r = post(c, bot, "/api/agent-requests", {"name": name, "about": "Sets up <i>channels</i>"})
        assert r.status_code == 202 and r.json()["id"] == rid and len(mailer.SENT) == n
        assert post(c, bot, "/api/agent-requests/me", {}).json() == {"id": rid, "state": "pending"}
        # only admins list and decide
        assert post(c, bot, "/api/agent-requests", {"list": True}).status_code == 403
        listed = post(c, admin, "/api/agent-requests", {"list": True}).json()["requests"]
        assert [x["id"] for x in listed] == [rid] and listed[0]["name"] == "<b>Helper</b> bot"
        r = post(c, admin, "/api/agent-requests", {"decide": rid, "approve": True})
        assert r.status_code == 200 and r.json()["request"]["state"] == "approved", r.text
        # in: the admin's own agent, on the relays, in belfry.json
        assert pk(bot) in core.active_pubkeys(rsvc)
        assert rsvc.odoo.agent_owners() == {pk(bot): pk(admin)}
        time.sleep(1.1)
        me = post(c, bot, "/api/agent-requests/me", {}).json()
        assert me["state"] == "approved" and me["owner"]["pubkey"] == pk(admin)
        assert me["api"] == "https://belfry.belfry.test/api/v1"
        time.sleep(1.1)
        r = post(c, bot, "/api/agent-requests", {"name": "again"})
        assert r.status_code == 409 and r.json()["error"] == "in_already"
        # admins have no agent limit: a second agent comes in too
        bot2 = PrivateKey()
        rid2 = post(c, bot2, "/api/agent-requests", {"name": "Second"}).json()["id"]
        time.sleep(1.1)
        r = post(c, admin, "/api/agent-requests", {"decide": rid2, "approve": True})
        assert r.status_code == 200, r.text
        assert len(rsvc.odoo.agents_of(ada)) == 2
        time.sleep(1.1)
        mine = post(c, admin, "/api/agents", {}).json()
        assert mine["unlimited"] is True and len(mine["agents"]) == 2
        # declined
        bot3 = PrivateKey()
        rid3 = post(c, bot3, "/api/agent-requests", {"name": "Third"}).json()["id"]
        time.sleep(1.1)
        post(c, admin, "/api/agent-requests", {"decide": rid3, "approve": False})
        assert post(c, bot3, "/api/agent-requests/me", {}).json()["state"] == "declined"
        assert pk(bot3) not in core.active_pubkeys(rsvc)
        # removed by its admin: the agent hears it
        time.sleep(1.1)
        post(c, admin, "/api/agents", {"pubkey": pk(bot), "mine": False})
        time.sleep(1.1)
        assert post(c, bot, "/api/agent-requests/me", {}).json()["state"] == "removed"


def test_requests_are_limited(world, monkeypatch):
    rsvc, admin, ada = world
    monkeypatch.setattr(agentrequests, "LIMITS", {"pending": 2, "per_hour": 10, "per_key_day": 2})
    with TestClient(create_app(rsvc)) as c:
        assert post(c, PrivateKey(), "/api/agent-requests", {}).status_code == 400  # no name
        a, b = PrivateKey(), PrivateKey()
        assert post(c, a, "/api/agent-requests", {"name": "A"}).status_code == 202
        assert post(c, b, "/api/agent-requests", {"name": "B"}).status_code == 202
        r = post(c, PrivateKey(), "/api/agent-requests", {"name": "C"})
        assert r.status_code == 429 and r.json()["error"] == "busy"
        # a declined key may ask again, but not without end
        rid = rsvc.odoo.agent_requests(pubkey=pk(a))[0]["id"]
        rsvc.odoo.agent_request_decide(rid, False, ada)
        time.sleep(1.1)
        assert post(c, a, "/api/agent-requests", {"name": "A"}).status_code == 202
        rid = rsvc.odoo.agent_requests(pubkey=pk(a))[0]["id"]
        rsvc.odoo.agent_request_decide(rid, False, ada)
        time.sleep(1.1)
        r = post(c, a, "/api/agent-requests", {"name": "A"})
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        # unknown keys: 404 on /me
        assert post(c, PrivateKey(), "/api/agent-requests/me", {}).status_code == 404


def test_the_console_page_lets_an_admin_answer(world):
    rsvc, admin, ada = world
    rsvc.settings.pass_page_secret = "p" * 32
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    bot = PrivateKey()
    limits = agentrequests.LIMITS
    rid = rsvc.odoo.agent_request_ask(pk(bot), "<script>x</script>", "", "", limits)["id"]
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        assert c.get("/agents", follow_redirects=False).status_code == 303  # sign in first
        claims = {"kind": "console", "partner": ada, "csrf": "tok"}
        c.cookies.set(console.COOKIE, console.mypass._sign(rsvc, claims, 3600))
        page = c.get("/agents").text
        assert "&lt;script&gt;x&lt;/script&gt;" in page and "<script>x</script>" not in page
        assert "Let it in as my agent" in page and 'name="csrf" value="tok"' in page
        assert "/static/busy.js" in page
        r = c.post(f"/agents/{rid}/approve", data={"csrf": "wrong"}, follow_redirects=False)
        assert r.status_code == 303 and rsvc.odoo.agent_requests("pending")
        r = c.post(f"/agents/{rid}/approve", data={"csrf": "tok"}, follow_redirects=False)
        assert r.status_code == 303 and rsvc.odoo.agent_requests("approved")
        assert pk(bot) in core.active_pubkeys(rsvc)
        assert "evil" not in c.get("/agents?msg=evil&err=evil").text


def test_agent_requests_need_the_own_register(services, odoo):
    services.settings.public_url = BASE
    with TestClient(create_app(services)) as c:
        r = post(c, PrivateKey(), "/api/agent-requests", {"name": "A"})
        assert r.status_code == 409 and r.json()["error"] == "unsupported"


def test_requests_before_the_first_admin_wait_for_them(world, monkeypatch):
    rsvc, admin, ada = world
    monkeypatch.setattr(console, "admin_emails", lambda svc: set())
    rsvc.odoo.agent_request_ask(pk(PrivateKey()), "Early", "", "", agentrequests.LIMITS)
    n = len(mailer.SENT)
    assert agentrequests.notify_waiting(rsvc) == 0 and len(mailer.SENT) == n
    monkeypatch.setattr(console, "admin_emails", lambda svc: {"admin@belfry.test"})
    assert agentrequests.notify_waiting(rsvc) == 1 and mailer.SENT[-1]["To"] == "admin@belfry.test"
    assert agentrequests.notify_waiting(rsvc) == 0  # told once
