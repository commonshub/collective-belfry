"""Never a new key for somebody who has one, and never a belfry with members handed to whoever
comes first (signerguard.py, setup.has_admin, core.preload_account; docs/own-signer.md §6)."""

# ruff: noqa: F811
from __future__ import annotations

import json
import logging

import httpx
import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    community,
    console,
    core,
    setup,
    signerguard,
    steward,
    stewards,
    usernames,
)
from belfry_onboarding.clients.signer import KeyNotHeld

from .test_opensetup import open_wizard
from .test_own_mode import APPS, CONSOLE, own, people, signer, web  # noqa: F401 — fixtures
from .test_setup import folder, fresh, make_first_admin  # noqa: F401 — fixtures


def pk(key: PrivateKey) -> str:
    return key.public_key_xonly.format().hex()


@pytest.fixture
def guarded(own, signer):
    """A belfry on its own signer whose client holds new accounts to the register's keys."""
    signerguard.install(own)
    assert own.keycast.expected is not None
    return own


def member(svc, odoo, name: str) -> dict:
    made = steward.add_steward(svc, f"{name.lower()}@example.org", name)
    p = odoo.tables["belfry.pass"][made["pass"]["pass_id"]]
    return {"partner": p["partner_id"], "pubkey": p["keycast_pubkey"], "pass": p}


# ── the signer makes no other key than the register's ───────────────────────────────────────
def test_an_empty_signer_never_makes_another_key_for_a_member(guarded, odoo, signer, caplog):
    member(guarded, odoo, "First")  # (the fake's keys count up: Ana's is not the first one)
    ana = member(guarded, odoo, "Ana")
    vine = f"odoo-partner-{ana['partner']}"
    assert signerguard.register_key(guarded, vine) == ana["pubkey"]
    assert signerguard.register_key(guarded, "odoo-partner-99999") is None
    assert signerguard.register_key(guarded, "someone-else") is None
    # the worst case: SIGNER=belfry on a signer that was never given the accounts
    signer.users.clear()
    with caplog.at_level(logging.ERROR), pytest.raises(KeyNotHeld):
        community.member_token(guarded, ana["partner"])
    assert signer.users == {}  # nothing was made
    assert "refusing to make a new key" in caplog.text and "Moving a belfry" in caplog.text
    assert ana["pubkey"] not in caplog.text  # shortened in logs
    with pytest.raises(KeyNotHeld):
        core._ensure_keycast_user(guarded, {"id": ana["partner"], "name": "Ana"}, "0001")
    assert signer.users == {}
    # a person the register has no key for yet is made as always
    carol = member(guarded, odoo, "Carol")
    assert community.member_token(guarded, carol["partner"])[0] == carol["pubkey"]


def test_a_key_the_seed_gives_again_is_made_again(guarded, odoo, signer):
    """A lost signer volume and derived keys: the account comes back with the same key."""
    signer.derived = True
    ana = member(guarded, odoo, "Ana")
    vine = f"odoo-partner-{ana['partner']}"
    signer.users.clear()
    assert community.member_token(guarded, ana["partner"])[0] == ana["pubkey"]
    assert signer.users[vine]["pubkey"] == ana["pubkey"]
    # restore-keys reports what cannot come back instead of making another key
    bob = member(guarded, odoo, "Bob")
    signer.users.clear()
    bob["pass"]["keycast_pubkey"] = "7" * 64  # a key from before the seed
    report = core.restore_keys(guarded)["partners"]
    assert sorted(report.values()) == ["mismatch", "ok"]
    assert f"odoo-partner-{bob['partner']}" not in signer.users


def test_a_register_that_cannot_be_read_is_not_no_key(guarded, odoo, signer, monkeypatch):
    ana = member(guarded, odoo, "Ana")
    signer.users.clear()
    real = odoo.search_read

    def down(model, *a, **k):
        if model == "belfry.pass":
            raise RuntimeError("register down")
        return real(model, *a, **k)

    monkeypatch.setattr(odoo, "search_read", down)
    with pytest.raises(RuntimeError, match="register down"):
        guarded.keycast.preload_user(f"odoo-partner-{ana['partner']}", "0001", "Ana")
    assert signer.users == {}


def test_the_service_does_not_start_on_a_signer_without_its_members(guarded, odoo, signer):
    # (create_app runs this check only for the app built from the environment: a production
    # start; an app handed its services, as every test's, has run it or not on its own)
    ana, bob = member(guarded, odoo, "Ana"), member(guarded, odoo, "Bob")
    assert signerguard.check_start(guarded) == {
        "checked": 2,
        "held": 2,
        "missing": 0,
        "wrong": 0,
        "skipped": "",
        "closed": False,
    }
    # an import left the signer making no new accounts: said, not a reason to stop
    signer.no_new_accounts = True
    assert signerguard.check_start(guarded)["closed"] is True
    signer.no_new_accounts = False
    assert [m["vine_id"] for m in signerguard.members(guarded)] == [
        f"odoo-partner-{ana['partner']}",
        f"odoo-partner-{bob['partner']}",
    ]
    # one account missing: a warning, the per-account guard decides
    kept = signer.users.pop(f"odoo-partner-{bob['partner']}")
    assert signerguard.check_start(guarded)["missing"] == 1
    # none: no start
    signer.users.clear()
    with pytest.raises(signerguard.SignerNotMigrated, match="holds none of the 2 checked"):
        signerguard.check_start(guarded)
    guarded.settings.signer_recovering = True  # deriving again from the seed, on purpose
    assert signerguard.check_start(guarded)["held"] == 0
    guarded.settings.signer_recovering = False
    # another key than the register's: no start, whatever the flag
    signer.users[f"odoo-partner-{bob['partner']}"] = {**kept, "pubkey": "9" * 64}
    guarded.settings.signer_recovering = True
    with pytest.raises(signerguard.SignerNotMigrated, match="another key") as exc:
        signerguard.check_start(guarded)
    assert bob["pubkey"] not in str(exc.value) and bob["pubkey"][:8] in str(exc.value)
    # a member with their own key is not expected in the signer's custody
    signer.users.clear()
    guarded.settings.signer_recovering = False
    for p in (ana, bob):
        odoo.write("res.partner", [p["partner"]], {"belfry_key_custody": "self"})
    assert signerguard.check_start(guarded)["checked"] == 0
    # a signer that is down stops nothing (nothing can be made meanwhile)
    odoo.write("res.partner", [ana["partner"]], {"belfry_key_custody": "belfry"})

    def unreachable(vine_id):
        raise httpx.ConnectError("no route")

    guarded.keycast.account = unreachable
    assert signerguard.check_start(guarded)["skipped"] == "signer"
    # with Keycast nothing is checked
    guarded.settings.signer = "keycast"
    assert signerguard.check_start(guarded)["skipped"] == "not on the own signer"


# ── the first-run setup ─────────────────────────────────────────────────────────────────────
def test_a_belfry_with_members_is_not_handed_to_the_first_comer(fresh, folder, authentik, odoo):
    svc = fresh.app.state.services
    assert not signerguard.has_people(svc)
    assert setup.open_for_anyone(svc)  # new: no file, no admin, nobody with a pass
    csrf = open_wizard(fresh)
    assert make_first_admin(fresh, folder, csrf, pk(PrivateKey())).status_code == 200
    assert signerguard.has_people(svc)
    # the record of the admin is lost (a restore without the setup folder's file; admins not
    # yet copied out of Authentik during a move): the people are still there
    (folder / setup.FIRST_ADMIN).unlink()
    for email in list(console.admin_emails(svc)):
        console.set_admin(svc, email, False)
    console._admins["at"] = 0.0
    assert not setup.first_admin(svc.settings) and not stewards.emails(svc)
    assert setup.has_admin(svc) and not setup.open_for_anyone(svc)
    stranger = TestClient(fresh.app, base_url="https://belfry.belfry.test")
    assert 'data-start="name"' not in stranger.get("/setup").text
    r = stranger.post(
        "/setup/first-admin",
        json={
            "csrf": "x",
            "name": "Eve",
            "email": "eve@example.org",
            "username": "eve",
            "session_pubkey": pk(PrivateKey()),
        },
    )
    assert r.status_code in (403, 409)
    # a pass that is over still counts: this belfry had members
    for p in odoo.tables["belfry.pass"].values():
        p["state"] = "revoked"
    assert signerguard.has_people(svc) and not setup.open_for_anyone(svc)
    # when the register cannot be read the answer is "taken"
    real = odoo.search_read
    odoo.search_read = lambda model, *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        assert setup.has_admin(svc)
    finally:
        odoo.search_read = real


def test_setup_reset_opens_such_a_belfry_on_purpose(fresh, folder, authentik, odoo):
    svc = fresh.app.state.services
    csrf = open_wizard(fresh)
    assert make_first_admin(fresh, folder, csrf, pk(PrivateKey())).status_code == 200
    (folder / setup.FIRST_ADMIN).unlink()
    for email in list(console.admin_emails(svc)):
        console.set_admin(svc, email, False)
    console._admins["at"] = 0.0
    dry = setup.reset(svc, dry_run=True)
    assert dry["removed"] == [] and dry["closed_without_admin"] is True and dry["open"] is False
    assert not setup.open_for_anyone(svc)  # a dry run opens nothing
    out = setup.reset(svc)
    assert out["closed_without_admin"] is True and out["open"] is True
    assert json.loads((folder / setup.RESET_FILE).read_text())["open"] is True
    assert setup.open_for_anyone(svc)
    assert setup.reset(svc)["closed_without_admin"] is False  # again: nothing to do
    # the next first admin closes it again, and it stays closed when their record is lost too
    nxt = TestClient(fresh.app, base_url="https://belfry.belfry.test")
    csrf = open_wizard(nxt)
    r = nxt.post(
        "/setup/first-admin",
        json={
            "csrf": csrf,
            "name": "Village Commons",
            "email": "nora@example.org",
            "username": "nora",
            "session_pubkey": pk(PrivateKey()),
        },
    )
    assert r.status_code == 200, r.text
    assert json.loads((folder / setup.RESET_FILE).read_text())["open"] is False
    (folder / setup.FIRST_ADMIN).unlink()
    for email in list(console.admin_emails(svc)):
        console.set_admin(svc, email, False)
    console._admins["at"] = 0.0
    assert not setup.open_for_anyone(svc)


def test_a_reset_file_from_before_never_opens_anything(fresh, folder, odoo):
    svc = fresh.app.state.services
    csrf = open_wizard(fresh)
    assert make_first_admin(fresh, folder, csrf, pk(PrivateKey())).status_code == 200
    (folder / setup.FIRST_ADMIN).unlink()
    for email in list(console.admin_emails(svc)):
        console.set_admin(svc, email, False)
    console._admins["at"] = 0.0
    (folder / setup.RESET_FILE).write_text(json.dumps({"at": 1, "removed": ["x@example.org"]}))
    assert not setup.reopened(svc.settings) and not setup.open_for_anyone(svc)


# ── usernames ───────────────────────────────────────────────────────────────────────────────
def test_nobody_chooses_a_name_accounts_start_with():
    for name in ("50", "0007", "member-12", "guest-12", "osv-0007", "ab-12"):
        assert usernames.problem(name) == "reserved", name
    for name in ("lina", "lina-2", "member", "guest", "m50", "50cent", "member-x"):
        assert usernames.problem(name) is None, name
    with pytest.raises(ValueError, match="member numbers"):
        setup.create_first_admin(None, "a@example.org", "7", "ab" * 32)  # type: ignore[arg-type]


def test_a_taken_member_number_does_not_stop_the_account(services, odoo, keycast):
    """Somebody holds "0002" as their username (chosen before such names were refused): the
    member with that number still gets an account, under a name nobody can choose."""
    first = services.keycast.preload_user("odoo-partner-1", "0002", "Squatter")
    made = core.preload_account(services, 2, "0002", "Second")
    assert made["pubkey"] != first["pubkey"]
    assert keycast.users["odoo-partner-2"]["username"] == "member-2"
    assert usernames.DEFAULT.fullmatch("member-2")  # give_default replaces it with their name
    # that one taken too (an older account): a third name, still a default one
    services.keycast.preload_user("odoo-partner-3", "member-4", "Another")
    services.keycast.preload_user("odoo-partner-5", "0004", "And another")
    core.preload_account(services, 4, "0004", "Fourth")
    name = keycast.users["odoo-partner-4"]["username"]
    assert name.startswith("member-40") and usernames.DEFAULT.fullmatch(name)
    # again: the account as it is
    assert core.preload_account(services, 2, "0002", "Second")["pubkey"] == made["pubkey"]
