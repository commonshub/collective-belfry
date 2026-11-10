"""The belfry's own register (register.py): the same RPC as Odoo, on SQLite, for a belfry without
Odoo; the invite page (invite.py) and the free activation (core.activate_free_pass)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import core, mailer, steward
from belfry_onboarding.app import create_app
from belfry_onboarding.clients.odoo import OdooError
from belfry_onboarding.register import Register

ANA_KEY = "a" * 64


@pytest.fixture
def reg(tmp_path, settings):
    mailer.SENT.clear()
    return Register(tmp_path / "register.sqlite", settings)


@pytest.fixture
def rsvc(services, reg):
    services.odoo = reg
    return services


def last_mail():
    msg = mailer.SENT[-1]
    return msg, msg.get_body(preferencelist=("plain",)).get_content()


def test_records_persist_and_search_like_odoo(tmp_path, settings, reg):
    pid = reg.create("res.partner", {"name": "Ana", "email": "Ana@Example.org"})
    reg.create("belfry.pass", {"partner_id": pid, "state": "active", "keycast_pubkey": ANA_KEY})
    again = Register(tmp_path / "register.sqlite", settings)  # a restart
    [row] = again.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "=", ANA_KEY]],
        ["partner_id", "member_number", "email"],
    )
    assert row["partner_id"] == [pid, "Ana"] and row["email"] == "Ana@Example.org"
    assert again.search_read("res.partner", [["email", "=ilike", "ana@example.org"]], ["id"])
    assert again.call("res.partner", "belfry_ensure_member_number", [[pid]]) == ["0001"]
    assert again.call("res.partner", "belfry_ensure_member_number", [[pid]]) == ["0001"]
    # billing is the Odoo add-on's: nothing found, and asking to act says so
    assert again.search_read("account.move", [], ["id"]) == []
    with pytest.raises(OdooError, match="Odoo add-on"):
        again.call("belfry.shift", "belfry_shift_signup", [pid, 1])
    # the pages that list shifts and volunteers (the steward pages, /admin/shifts) show none
    assert again.call("belfry.shift", "belfry_shift_board", []) == []
    assert again.call("belfry.pass", "belfry_steward_volunteers", []) == []
    assert again.call("belfry.shift", "belfry_volunteer_of_partner", [pid]) is False


def test_the_steward_dashboard_opens_without_odoo(rsvc):
    data = steward.dashboard_data(rsvc)
    assert data["shifts"] == [] and data["volunteers"] == []


def test_member_numbers_continue_after_imported_ones(reg):
    a = reg.create("res.partner", {"name": "Old", "belfry_member_number": "0007"})
    b = reg.create("res.partner", {"name": "New"})
    assert reg.call("res.partner", "belfry_ensure_member_number", [[b]]) == ["0008"]
    assert a < b


def test_an_admin_gets_a_free_membership_and_a_sign_in_mail(rsvc, reg, authentik, keycast):
    made = steward.steward_pass(rsvc, "admin@belfry.test", "Ada Admin")
    assert made["result"] == "activated" and made["state"] == "active"
    row = reg.read_one(
        "belfry.pass",
        made["pass_id"],
        ["state", "keycast_pubkey", "date_end", "claim_token", "login_setup_url", "invoice_id"],
    )
    assert row["keycast_pubkey"] and row["date_end"] == "2099-12-31" and not row["invoice_id"]
    msg, text = last_mail()
    assert msg["To"] == "admin@belfry.test" and row["login_setup_url"] in text
    # again: nothing new
    assert steward.steward_pass(rsvc, "admin@belfry.test")["result"] == "already_active"


def test_key_codes_are_mailed_and_bind_the_key(reg):
    sent = reg.call("belfry.key.code", "belfry_key_code_send", ["bob@example.org", ANA_KEY, "Chat"])
    assert sent["state"] == "sent"
    msg, text = last_mail()
    code = re.search(r"\b(\d{6})\b", msg["Subject"]).group(1)
    assert code in text and "Chat asks" in text
    wrong = reg.call(
        "belfry.key.code", "belfry_key_code_verify", ["bob@example.org", ANA_KEY, "000000"]
    )
    if code != "000000":
        assert wrong == {"state": "wrong_code", "tries_left": 4}
    ok = reg.call("belfry.key.code", "belfry_key_code_verify", ["bob@example.org", ANA_KEY, code])
    assert ok["state"] == "bound" and ok["member"] is False
    [p] = reg.search_read(
        "res.partner", [["email", "=ilike", "bob@example.org"]], ["belfry_verified_pubkey"]
    )
    assert p["belfry_verified_pubkey"] == ANA_KEY
    again = reg.call(
        "belfry.key.code", "belfry_key_code_verify", ["bob@example.org", ANA_KEY, code]
    )
    assert again["state"] == "expired"  # used once


def test_joining_with_an_invite_end_to_end(rsvc, reg):
    made = reg.call(
        "belfry.invite", "belfry_steward_create", ["admin@belfry.test", "for Bo", "Ada"]
    )
    assert made["url"].endswith("/invite/" + made["token"])
    with TestClient(create_app(rsvc)) as c:
        page = c.get(f"/invite/{made['token']}")
        assert page.status_code == 200 and "Ada invites you" in page.text and "for Bo" in page.text
        r = c.post(
            f"/invite/{made['token']}",
            data={"name": "Bo Brown", "email": "bo@example.org"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = c.get(f"/invite/{made['token']}")
        assert "Check your mail" in page.text and "bo@example.org" in page.text
        msg, text = last_mail()
        assert msg["To"] == "bo@example.org"
        link = re.search(r"https?://\S+/confirm/\S+", text).group(0)
        path = link.split("://", 1)[1].split("/", 1)[1]
        assert c.get("/" + path.replace("/confirm/", "/confirm/x")).status_code == 404
        done = c.get("/" + path)
        assert done.status_code == 200 and "You are a member now" in done.text
        # the invite is used up, the membership is open, the sign-in link was mailed
        inv = reg.invite_by_token(made["token"])
        assert inv["state"] == "used"
        row = reg.read_one("belfry.pass", inv["pass_id"], ["state", "login_setup_url"])
        assert row["state"] == "active"
        msg, text = last_mail()
        assert "Welcome to" in msg["Subject"] and row["login_setup_url"] in text
        assert c.get(f"/invite/{made['token']}").status_code == 410
        assert c.get("/" + path).status_code == 200  # confirming again changes nothing


def test_the_invite_page_is_odoo_s_when_odoo_is_on(client):
    assert client.get("/invite/" + "x" * 30).status_code == 404


def test_member_invites_applications_reports_feedback_follows(reg):
    pid = reg.create("res.partner", {"name": "Ana", "email": "ana@example.org"})
    with pytest.raises(OdooError):
        reg.call("belfry.invite", "belfry_participant_create", [pid, "hi"])
    reg.create("belfry.pass", {"partner_id": pid, "state": "active", "keycast_pubkey": ANA_KEY})
    row = reg.call(
        "belfry.invite", "belfry_holder_invite", [pid, "cy@example.org", "Cy", "", False]
    )
    assert row["state"] == "open" and row["email"] == "cy@example.org"
    assert last_mail()[0]["To"] == "cy@example.org"
    assert reg.call("belfry.invite", "belfry_participant_invites", [pid])["left"] == 2
    reg.call("belfry.invite", "belfry_holder_invite_revoke", [pid, row["id"]])
    assert reg.call("belfry.invite", "belfry_participant_invites", [pid])["left"] == 3
    member = reg.call(
        "belfry.invite", "belfry_holder_invite", [pid, "ana@example.org", "", "", False]
    )
    assert member["state"] == "member"

    aid = reg.call(
        "belfry.application",
        "belfry_application_create",
        [{"name": "Di", "email": "di@example.org", "motivation": "garden"}, ["admin@x.org"]],
    )
    invited = reg.call("belfry.application", "belfry_application_invite", [[aid], "admin@x.org"])
    assert last_mail()[0]["To"] == "di@example.org" and invited["token"] in last_mail()[1]
    with pytest.raises(OdooError, match="answered already"):
        reg.call("belfry.application", "belfry_application_decline", [[aid], "admin@x.org"])

    rid = reg.call(
        "belfry.report",
        "belfry_report_create",
        [
            {
                "reporter_pubkey": ANA_KEY,
                "target_pubkey": "b" * 64,
                "target_ref": "e1",
                "app": "photos",
                "reason": "spam",
            },
            ["admin@x.org"],
        ],
    )
    reg.call("belfry.report", "belfry_report_handle", [[rid], "hide", "admin@x.org"])
    assert reg.call("belfry.report", "belfry_hidden_refs", []) == ["e1"]
    [rep] = reg.read("belfry.report", [rid], ["reporter_partner_id", "state"])
    assert rep["reporter_partner_id"][0] == pid and rep["state"] == "hidden"

    fid = reg.call(
        "belfry.feedback",
        "belfry_feedback_create",
        [{"happened": "x", "expected": "y"}, ["admin@x.org"]],
    )
    assert (
        reg.call("belfry.feedback", "belfry_feedback_handle", [[fid], "done", "a@x.org"]) == "done"
    )

    asked = reg.call("belfry.follower", "belfry_follow_request", ["ed@example.org", "general"])
    assert asked["state"] == "send"
    reg.call("belfry.follower", "belfry_follow_set", [asked["token"], "asap"])
    [due] = reg.call("belfry.follower", "belfry_follow_due", ["asap"])
    assert due["email"] == "ed@example.org"
    reg.call("belfry.follower", "belfry_follow_mail", [due["id"], "News", "<p>Hi</p>", 99])
    assert last_mail()[0]["To"] == "ed@example.org" and "Hi" in last_mail()[1]
    assert reg.call("belfry.follower", "belfry_follow_claim_day", ["2026-11-08"]) is True
    assert reg.call("belfry.follower", "belfry_follow_claim_day", ["2026-11-08"]) is False


def test_a_membership_brings_no_tokens(rsvc):
    import datetime as dt

    assert (
        core.token_allowance(rsvc, "member", None, dt.date(2026, 1, 1), dt.date(2099, 12, 31)) == 0
    )


def test_importing_from_odoo_keeps_ids_and_drops_billing(tmp_path, settings, odoo):
    from belfry_onboarding.register_import import run

    def fields_get(model):
        rows = list(odoo.tables[model].values())
        names = {k for r in rows for k in r if k != "id"}
        m2o = {
            "partner_id": "res.partner",
            "invite_id": "belfry.invite",
            "invoice_id": "account.move",
        }
        return {
            n: {"type": "many2one" if n in m2o else "char", "store": True, "relation": m2o.get(n)}
            for n in names
        }

    real = odoo.execute_kw

    def execute_kw(model, method, args, kwargs=None):
        if method == "fields_get":
            return fields_get(model)
        return real(model, method, args, kwargs)

    odoo.execute_kw = execute_kw  # type: ignore[method-assign]
    ana = odoo.add("res.partner", id=12, name="Ana", email="ana@x.org", belfry_member_number="0003")
    odoo.add("res.partner", id=13, name="Stranger", email="s@x.org")
    odoo.add(
        "belfry.pass", id=7, partner_id=ana, state="active", keycast_pubkey=ANA_KEY, invoice_id=99
    )
    reg = Register(tmp_path / "r.sqlite", settings)
    counts = run(odoo, reg)
    assert counts["res.partner"] == 1 and counts["belfry.pass"] == 1
    [p] = reg.search_read(
        "belfry.pass", [["state", "=", "active"]], ["partner_id", "member_number", "invoice_id"]
    )
    assert p["id"] == 7 and p["partner_id"][0] == 12 and p["member_number"] == "0003"
    assert p["invoice_id"] is False  # billing stays in Odoo
    new = reg.create("res.partner", {"name": "New"})
    assert new == 13 and reg.call("res.partner", "belfry_ensure_member_number", [[new]]) == ["0004"]
    with pytest.raises(SystemExit):
        run(odoo, reg)


def test_a_name_with_a_line_break_still_gets_its_mail(reg):
    """The subject carries the name: a line break in it raised ValueError (a 500 on /apply)."""
    reg.call(
        "belfry.application",
        "belfry_application_create",
        [
            {"name": "Ana\nBcc: x@example.org", "email": "ana@example.org", "motivation": "Hi"},
            ["admin@belfry.test"],
        ],
    )
    msg = mailer.SENT[-1]
    assert "Ana Bcc: x@example.org asks to join" in msg["Subject"] and msg["Bcc"] is None


def test_unsent_mails_are_not_kept_for_ever(reg):
    for _ in range(mailer.SENT_MAX + 5):
        reg.mailer.send("ana@example.org", "Hello", ["Hi"])
    assert len(mailer.SENT) == mailer.SENT_MAX
