"""Free day passes for week-pass holders (daypass.py) and the pass /me shows on the day."""

from __future__ import annotations

import datetime as dt
import urllib.parse

from belfry_onboarding import core, mypass

from .test_mypass import login, passing, web  # noqa: F401  (fixtures)

DAY = "2027-01-27"  # an open event day for the fake Odoo (FakeOdoo.today is 2027-01-26)


def _rows(*passes):
    return [{"id": i, **p} for i, p in enumerate(passes, start=1)][::-1]  # newest first


def test_rank_prefers_the_day_pass_on_its_day():
    week = {"state": "active", "tier": "week", "date_start": "2027-01-25", "date_end": "2027-01-31"}
    day = {"state": "active", "tier": "day", "date_start": DAY, "date_end": DAY}
    rows = _rows(week, day)

    def best(today):
        return min(rows, key=lambda r: core.pass_rank(r, dt.date.fromisoformat(today)))["tier"]

    assert best(DAY) == "day"
    assert best("2027-01-26") == "week"
    assert best("2027-01-10") == "week"  # before the event: the pass that starts first
    rows[0]["state"] = "revoked"  # a revoked day pass is never shown over the week pass
    assert best(DAY) == "week"


def test_week_pass_holder_takes_a_free_day_pass(web, passing, odoo, keycast):  # noqa: F811
    login(web)
    page = web.get("/me").text
    assert "Free day passes" in page and f'value="{DAY}"' in page
    taken = web.post("/me/day-passes", data={"day": DAY}, follow_redirects=False)
    assert taken.status_code == 303 and "msg=day-taken" in taken.headers["location"]

    day_pass = next(p for p in odoo.tables["belfry.pass"].values() if p["tier"] == "day")
    assert (day_pass["state"], day_pass["date_start"], day_pass["date_end"]) == ("active", DAY, DAY)
    assert day_pass["free_of_pass_id"] == passing["pass"]
    assert not day_pass.get("cashu_amount")  # no tokens: the week pass has them
    assert not day_pass.get("login_setup_url")  # no sign-in link, so no activation mail
    assert day_pass["claim_token"] and day_pass["credential_status_index"] == day_pass["id"]
    assert len(keycast.users) == 1  # the holder's own account and key

    page = web.get("/me").text
    assert "/claim/" + day_pass["claim_token"] in page  # its own wallet credential
    assert "Week pass" in page  # not its day yet: /me still shows the week pass

    again = web.post("/me/day-passes", data={"day": DAY}, follow_redirects=False)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(again.headers["location"]).query)
    assert query == {"msg": ["day-refused"], "why": ["already"]}
    assert "day pass for that day already" in web.get(again.headers["location"]).text
    over = web.post("/me/day-passes", data={"day": "2027-01-25"}, follow_redirects=False)
    assert "why=over" in over.headers["location"]
    # the reason shown is one of Belfry's own texts, never one from the link
    assert "Injected-Text" not in web.get("/me?msg=day-refused&why=Injected-Text").text


def test_the_day_pass_is_shown_on_its_day(services, passing, odoo):  # noqa: F811
    day_id = odoo.add(
        "belfry.pass",
        partner_id=passing["partner"],
        tier="day",
        state="active",
        date_start=DAY,
        date_end=DAY,
        free_of_pass_id=passing["pass"],
    )
    on_the_day = mypass.partner_pass(services, passing["partner"], dt.date.fromisoformat(DAY))
    assert on_the_day["id"] == day_id
    # the passing fixture's week pass is valid today; the day pass is not
    assert mypass.partner_pass(services, passing["partner"])["id"] == passing["pass"]


def test_no_free_day_passes_without_a_week_pass(web, passing, odoo):  # noqa: F811
    odoo.get("belfry.pass", passing["pass"])["tier"] = "day"
    login(web)
    assert "Free day passes" not in web.get("/me").text
    refused = web.post("/me/day-passes", data={"day": DAY}, follow_redirects=False)
    assert "why=week" in refused.headers["location"]


def test_day_pass_form_only_from_the_pass_page(web, passing):  # noqa: F811
    login(web)
    r = web.post("/me/day-passes", data={"day": DAY}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_revoking_the_week_pass_revokes_its_day_passes(services, passing, odoo):  # noqa: F811
    day_id = odoo.add(
        "belfry.pass",
        partner_id=passing["partner"],
        tier="day",
        state="active",
        date_start=DAY,
        date_end=DAY,
        free_of_pass_id=passing["pass"],
    )
    assert core.revoke_pass(services, passing["pass"], "refund") == "revoked"
    day_pass = odoo.get("belfry.pass", day_id)
    assert day_pass["state"] == "revoked"
    assert day_pass["revoke_reason"] == f"refund (week pass {passing['pass']})"


def test_reconcile_mints_no_tokens_for_free_day_passes(services, passing, odoo):  # noqa: F811
    class Mint:
        minted: list[int] = []

        def mint_token(self, amount):
            self.minted.append(amount)
            return "cashuB"

    services.mint = Mint()  # type: ignore[assignment]
    odoo.get("belfry.pass", passing["pass"])["cashu_amount"] = 350
    odoo.add(
        "belfry.pass",
        partner_id=passing["partner"],
        tier="day",
        state="active",
        date_start=DAY,
        date_end=DAY,
        free_of_pass_id=passing["pass"],
    )
    assert core.issue_missing_tokens(services) == {}
    assert Mint.minted == []


def test_account_card_keeps_the_week_pass_and_its_tokens(services, passing, odoo):  # noqa: F811
    from belfry_onboarding import claim

    week = odoo.get("belfry.pass", passing["pass"])
    week["cashu_amount"] = 350
    odoo.add(
        "belfry.pass",
        partner_id=passing["partner"],
        tier="day",
        state="active",
        date_start=DAY,
        date_end=DAY,
        free_of_pass_id=passing["pass"],
        claim_token="d" * 32,
    )
    info = claim.pass_info(services, passing["partner"])
    assert info["tier"] == "week"  # today is not the day pass's day
    assert info["tokens"] == 350 and info["tokens_url"].endswith("/tokens/" + "c" * 32)
