"""Offers for event tickets (docs/sales.md §10 "A ticket against a contribution"): a request
names a ticket instead of a product, its approval is a code for that ticket alone, and its
author redeems it at the checkout with the code or with the offer itself."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import json
import re

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import actions, apps, events, mailer, sales, ticketsales, webrequests
from belfry_onboarding.app import create_app

from .test_events import D
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, free_code, shop  # noqa: F401 — fixtures
from .test_sales_console import CSRF, signed_in
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_offers import ASK, body, offers, press  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_testers import TESTER, envs, make_tester  # noqa: F401 — fixtures
from .test_ticket_sales import DAY, ORG, admissions, fair, order, pay_card, tk  # noqa: F401
from .test_webrequests import H, ask, clean, get, reqs  # noqa: F401 — fixtures

EVE = f"{D}:eve"
TITLE = "Kim Cook: 50% off Day ticket · Garden day"
INCL = (["price", "121", "EUR"], ["vat", "21", "included"])


def day_ticket(w, ada, *more):
    return tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["vat", "21"], *more)


def approved(c, relay, rsvc, key, percent=100, **more):
    """An offer for the day ticket, confirmed and approved: (offer id, card, thread key)."""
    fid, card, k = ask(c, relay, key, product=None, ticket=DAY, **more)
    sales.decide_offer(rsvc, fid, percent, "", "Ada")
    webrequests.forget()
    return fid, card, k


def kim(c, key, **more):
    return order(c, key, email="kim@example.org", name="Kim Cook", **more)


def test_an_offer_names_a_ticket_or_a_product(fair, reqs):
    rsvc, w, ada, *_ = fair
    relay = reqs[1]
    day_ticket(w, ada)
    tk(w, ada, "free", "Free")
    tk(w, ada, "hours", "Hours", ["price", "3", "HOUR"])
    tk(w, ada, "later", "Later", ["price_at", "40", "EUR", "4000000000", ""])
    tk(w, ada, "asks", "Asks", ["price", "5", "EUR"], ["closed"])
    me = PrivateKey()
    ask_t = {k: v for k, v in ASK.items() if k != "product"}
    with TestClient(create_app(rsvc)) as c:
        for bad in ({**ASK, "ticket": DAY}, ask_t, {**ask_t, "ticket": 7}):
            r = call(c, me, "/offers", bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        for tid, status, error in (
            (f"{D}:nope", 404, "unknown_ticket"),
            ("nonsense", 404, "unknown_ticket"),
            (f"{D}:free", 409, "free"),
            (f"{D}:hours", 409, "currency"),
            (f"{D}:asks", 409, "needs_application"),
        ):
            r = call(c, me, "/offers", {**ask_t, "ticket": tid})
            assert (r.status_code, r.json()["error"]) == (status, error), tid
        for bad in ("gift", "", None, 1):
            r = call(c, me, "/offers", {**ask_t, "ticket": DAY, "kind": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        # the switch is off and this ticket's sale has not started: a request can be made
        assert not sales.settings(rsvc)["tickets"]
        r = call(c, me, "/offers", {**ask_t, "ticket": f"{D}:later", "kind": "sponsored"})
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["state"] == "confirm" and got["kind"] == "sponsored"
        assert got["product"] is None and got["ticket"] == f"{D}:later"
        assert got["event"] == D and got["event_link"] == D
        # a product's offer is as it was
        fid, card, _k = ask(c, relay, PrivateKey())
        got = call(c, me, f"/offers/{fid}", method="GET")
        assert got.status_code == 404
        f = sales.store(rsvc).offer(fid)
        assert f["product"] == "village-2027" and not f.get("ticket") and f["kind"] == "in_kind"
        assert ["subject", "Kim Cook: 50% off Two-week pass in kind"] in card["tags"]
        assert ["product", "village-2027"] in card["tags"]


def test_the_thread_names_the_ticket_and_the_event_and_says_sponsored(fair, reqs):
    rsvc, w, ada, *_ = fair
    relay = reqs[1]
    day_ticket(w, ada)
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me, product=None, ticket=DAY, kind="sponsored")
        tags = card["tags"]
        title = next(t[1] for t in tags if t[0] == "subject")
        ev = events.load(rsvc, D)
        # on one day: the title ends with the date (not when the test runs across midnight)
        day = ticketsales.one_day(ev)
        assert title == TITLE + (f", {day}" if day else "")
        assert ["ticket", DAY] in tags and ["offer-kind", "sponsored"] in tags
        assert not [t for t in tags if t[0] in ("product", "a")]
        assert "kim@example.org" not in json.dumps(card) or ["mail-from", "kim@example.org"] in tags
        assert pk(me) not in title and "@" not in title
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["kind"] == "sponsored" and got["ticket"] == DAY and got["event"] == D
        assert got["event_link"] == events.link_of(rsvc, ev) and got["state"] == "open"
        one = get(c, f"/requests/{card['id']}").json()
        assert one["kind"] == "sponsored" and one["label"] is None and one["title"] == title
        (row,) = get(c, "/requests").json()["items"]
        assert row["kind"] == "sponsored"
        fid2, _c2, _k2 = ask(c, relay, PrivateKey(), product=None, ticket=DAY, label="food")
        rows = get(c, "/requests").json()["items"]
        assert [(r["kind"], r["label"]) for r in rows] == [("in_kind", "food"), ("sponsored", None)]
        # the event's public activity counts them apart
        sales.decide_offer(rsvc, fid, 100, "", "Ada")
        sales.decide_offer(rsvc, fid2, 50, "", "Ada")
        webrequests.forget()
        stats = get(c, f"/events/{D}/activity").json()["stats"]
        assert stats["in_kind_passes"] == 1 and stats["sponsored_passes"] == 1
    clean(one, rows, keys=(pk(me),))


def test_an_approval_is_a_code_for_that_ticket_alone(fair, reqs, monkeypatch, keycast):
    rsvc, w, ada, *_ = fair
    relay = reqs[1]
    day_ticket(w, ada)
    tk(w, ada, "eve", "Evening", ["price", "10", "EUR"])
    sales.save_settings(rsvc, {"tickets": True})
    monkeypatch.setattr(actions, "_name", lambda svc, pk: "Ada")
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {pk(ada)})
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fid, card, _k = ask(c, relay, me, product=None, ticket=DAY)
        mailer.SENT.clear()
        res = press(rsvc, card, "offer-75", pk(ada))  # the chat's button decides it too
        assert res.status == "ok" and res.text == "Approved by Ada: 75% off."
        text = body(mailer.SENT[-1])
        assert "gives you 75% off Day ticket · Garden day." in text
        assert "when you get your ticket" in text
        f = sales.store(rsvc).offer(fid)
        assert f["my_url"] in text  # the link goes to the thread
        code = re.search(r"code: ([A-Z0-9-]+)", text).group(1)
        c_ = sales.store(rsvc).code(code)
        assert c_["product"] == "ticket:" + DAY and c_["percent"] == 75 and c_["uses"] == 1
        assert c_["bound_key"] == pk(me) and c_["bound_email"] == "kim@example.org"
        # another ticket, a product: not for this
        r = kim(c, me, ticket=EVE, code=code)
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        r = buy(c, me, email="kim@example.org", code=code)
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        # a product's code, a code for any product: never for a ticket
        for other in (free_code(rsvc), sales.make_codes(rsvc, 100)[0]["code"]):
            r = kim(c, me, code=other)
            assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        # somebody else's key
        r = kim(c, PrivateKey(), code=code)
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        assert admissions(w) == [] and not sales.store(rsvc).code(code)["used"]
        # the requester, on the right ticket: a quarter of 50 + 21%
        r = kim(c, me, code=code)
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["total"] == {"net": 12.5, "vat": 2.63, "gross": 15.13, "percent_off": 75}
        assert got["state"] == "pending" and got["card_url"]
        o = sales.store(rsvc).order(got["order"])
        assert o["offer"] == fid and o["code"] == code
        pay_card(c, got, 1513, "evt_k")
        (e,) = admissions(w)
        assert ["paid", "paid"] in e["tags"]  # money was paid: not "offer"
        # once
        r = order(c, PrivateKey(), email="other@example.org", offer=fid)
        assert r.status_code == 403
        r = order(c, me, email="again@example.org", code=code)
        assert r.status_code == 409 and r.json()["error"] == "code_used"


def test_the_offer_itself_at_the_checkout(fair, reqs, keycast):
    rsvc, w, ada, live_odoo, *_ = fair
    relay = reqs[1]
    day_ticket(w, ada, ["quantity", "5"])
    me, lou, web = PrivateKey(), PrivateKey(), PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me, product=None, ticket=DAY, public_name=True)
        # not approved yet; code and offer together; no such offer
        r = kim(c, me, offer=fid)
        assert r.status_code == 409 and r.json()["error"] == "not_approved"
        r = kim(c, me, offer=fid, code="ABCD-EFGH")
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        for bad in ("f_" + "0" * 16, "nope", 3):
            r = kim(c, me, offer=bad)
            assert r.status_code == 404 and r.json()["error"] == "not_found", bad
        sales.decide_offer(rsvc, fid, 100, "", "Ada")
        fid2, _card2, k2 = approved(c, relay, rsvc, lou, 50, email="lou@example.org", name="Lou")
        code = sales.store(rsvc).offer(fid)["code"]
        # a stranger; another thread's key; the key in the body or the query string
        r = kim(c, PrivateKey(), offer=fid)
        assert r.status_code == 403 and r.json()["error"] == "not_author"
        for hdr in ({"X-Thread-Key": k2}, {"X-Thread-Key": "0" * 24}, {"X-Thread-Key": "x"}):
            data = {"ticket": DAY, "email": "kim@example.org", "offer": fid}
            r = signed(c, PrivateKey(), data, hdr)
            assert r.status_code == 403 and r.json()["error"] == "not_author", hdr
        r = kim(c, PrivateKey(), offer=fid, k=k, thread_key=k)
        assert r.status_code == 403
        # 100%: nothing to pay, and it works while the tickets' switch is off
        assert not sales.settings(rsvc)["tickets"]
        data = {"ticket": DAY, "name": "Kim Cook", "email": "kim@example.org", "offer": fid}
        r = signed(c, web, data, {"X-Thread-Key": k})
        assert r.status_code == 201, r.text
        got = r.json()
        assert code not in r.text
        assert got["total"] == {"net": 0.0, "vat": 0.0, "gross": 0.0, "percent_off": 100}
        assert got["state"] == "active" and got["card_url"] is None and got["invoice"] is None
        o = sales.store(rsvc).order(got["order"])
        assert o["offer"] == fid and o["erase_after_event"] is False
        (e,) = admissions(w)
        assert ["paid", "offer"] in e["tags"] and ["ticket", DAY] in e["tags"]
        assert ["by", "checkout"] in e["tags"] and ["name", "Kim Cook"] in e["tags"]
        who = next(t[1] for t in e["tags"] if t[0] == "p")
        ev = events.load(rsvc, D)
        assert events.people(rsvc, ev).holders == {who: DAY}  # holds the ticket: check-in knows
        assert not live_odoo.records.get("account.move")  # nothing invoiced
        # as a 100% code does for a product: no confirmation first, the address not proven
        fields = ["email", "belfry_pending_email"]
        partner = rsvc.odoo.read_one("res.partner", o["partner_id"], fields)
        assert not partner["email"] and partner["belfry_pending_email"] == "kim@example.org"
        # used: once
        data = {"ticket": DAY, "email": "b@example.org", "offer": fid}
        r = signed(c, PrivateKey(), data, {"X-Thread-Key": k})
        assert r.status_code == 409 and r.json()["error"] == "code_used"
        r = order(c, me, email="c@example.org", offer=fid)
        assert r.status_code == 409 and r.json()["error"] == "code_used"
        # below 100% something is sold: the switch decides
        r = order(c, lou, email="lou@example.org", offer=fid2)
        assert r.status_code == 409 and r.json()["error"] == "tickets_off"
        assert not sales.store(rsvc).code(sales.store(rsvc).offer(fid2)["code"])["used"]
        sales.save_settings(rsvc, {"tickets": True})
        # half the price, as each buyer is invoiced: a person with the VAT in it…
        r = order(c, lou, email="lou@example.org", offer=fid2)
        assert r.status_code == 201, r.text
        assert r.json()["total"] == {"net": 25.0, "vat": 5.25, "gross": 30.25, "percent_off": 50}
        assert r.json()["state"] == "pending" and len(admissions(w)) == 1
        # …an organisation with the VAT on top of half the net
        sales.cancel(rsvc, r.json()["order"], "test")
        r = order(c, lou, email="lou@example.org", offer=fid2, pay="invoice", billing=ORG)
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["total"] == {"net": 25.0, "vat": 5.25, "gross": 30.25, "percent_off": 50}
        o2 = sales.store(rsvc).order(got["order"])
        line = live_odoo.records["account.move"][o2["invoice"]["move_id"]]["invoice_line_ids"][0][2]
        assert line["price_unit"] == 50.0 and line["discount"] == 50
        assert line["tax_ids"] == [[6, 0, [11]]]
        # the public activity: a ticket like any other
        webrequests.forget()
        act = get(c, f"/events/{D}/activity").json()
        assert act["stats"]["villagers"] == 1 and act["stats"]["in_kind_passes"] == 2
    clean(act, keys=(pk(me), pk(lou), pk(web), who))


def signed(c, key, data, headers):
    from .test_holder_invites import nip98
    from .test_roles import API

    raw = json.dumps(data).encode()
    return c.post(
        "/api/v1/checkout",
        content=raw,
        headers={
            **H,
            **headers,
            "Content-Type": "application/json",
            "Authorization": nip98(key, API + "/checkout", "POST", raw),
        },
    )


def test_included_vat_and_the_places(fair, reqs, keycast):
    rsvc, w, ada, *_ = fair
    relay = reqs[1]
    tk(w, ada, "day", "Day ticket", *INCL, ["quantity", "1"])
    sales.save_settings(rsvc, {"tickets": True})
    make_tester(rsvc)
    a, b, t = PrivateKey(), PrivateKey(), PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fa, _c, _k = approved(c, relay, rsvc, a, 50)
        fb, _c, _k = approved(c, relay, rsvc, b, 100, email="lou@example.org", name="Lou")
        ft, _c, _k = approved(c, relay, rsvc, t, 100, email=TESTER, name="Tess")
        # a tester's test order takes no place, and never one of the code's real uses
        r = order(c, t, email=TESTER, offer=ft)
        assert r.status_code == 201, r.text
        assert r.json()["test"] is True and r.json()["state"] == "active"
        ct = sales.store(rsvc).code(sales.store(rsvc).offer(ft)["code"])
        assert ct["used"] == [] and ct["test_used"] == [r.json()["order"]]
        (e,) = admissions(w)
        assert ["test", "1"] in e["tags"] and ["paid", "offer"] in e["tags"]
        r = kim(c, a, offer=fa, billing=ORG, pay="invoice")
        assert r.status_code == 201, r.text
        assert r.json()["total"] == {"net": 50.0, "vat": 10.5, "gross": 60.5, "percent_off": 50}
        # the one place is held: an approved offer takes a place like any order
        r = order(c, b, email="lou@example.org", offer=fb)
        assert r.status_code == 409 and r.json()["error"] == "sold_out"
        assert not sales.store(rsvc).code(sales.store(rsvc).offer(fb)["code"])["used"]
        # the unpaid order ends: its place and its approval come back
        sales.cancel(rsvc, kim(c, a, offer=fa, billing=ORG, pay="invoice").json()["order"], "test")
        r = order(c, b, email="lou@example.org", offer=fb)
        assert r.status_code == 201 and r.json()["state"] == "active"
        r = kim(c, a, offer=fa)
        assert r.status_code == 409 and r.json()["error"] == "sold_out"


def test_me_says_where_the_author_goes_on(fair, reqs, keycast):
    rsvc, w, ada, *_ = fair
    relay = reqs[1]
    day_ticket(w, ada)
    me = PrivateKey()
    seen = []
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me, product=None, ticket=DAY, kind="sponsored")
        base = {"author": True, "offer": fid, "ticket": DAY, "event": D, "redeemed": False}
        got = get(c, f"/requests/{card['id']}", **{"X-Thread-Key": k}).json()
        assert got["me"] == {**base, "state": "open"}
        sales.save_settings(rsvc, {"checkout_url": "https://belfry.test/tickets/{link}"})
        got = get(c, "/requests/1", **{"X-Thread-Key": k}).json()
        assert "ticket_url" not in got["me"]  # nowhere to go before the approval
        sales.decide_offer(rsvc, fid, 100, "", "Ada")
        webrequests.forget()
        got = call(c, me, f"/requests/{card['id']}", method="GET").json()
        seen.append(got)
        assert got["me"] == {
            **base,
            "state": "accepted",
            "ticket_url": f"https://belfry.test/tickets/{D}?offer={fid}",
        }
        sales.save_settings(
            rsvc, {"checkout_url": "https://belfry.test/tickets?event={link}&ticket={id}"}
        )
        got = get(c, "/requests/1", **{"X-Thread-Key": k}).json()
        seen.append(got)
        assert got["me"]["ticket_url"] == (
            f"https://belfry.test/tickets?event={D}&ticket={D}%3Aday&offer={fid}"
        )
        sales.save_settings(rsvc, {"checkout_url": ""})
        assert "ticket_url" not in get(c, "/requests/1", **{"X-Thread-Key": k}).json()["me"]
        # nobody else
        assert "me" not in get(c, "/requests/1").json()
        assert "me" not in call(c, PrivateKey(), "/requests/1", method="GET").json()
        # redeemed once an order holds the approval
        assert kim(c, me, offer=fid).status_code == 201
        got = get(c, "/requests/1", **{"X-Thread-Key": k}).json()
        seen.append(got)
        assert got["me"]["redeemed"] is True and got["me"]["state"] == "accepted"
        code = sales.store(rsvc).offer(fid)["code"]
        assert code and code not in json.dumps(seen)
        # a declined one
        f2, _c2, k2 = ask(c, relay, PrivateKey(), product=None, ticket=DAY, email="lou@example.org")
        sales.decide_offer(rsvc, f2, 0, "", "Ada")
        webrequests.forget()
        got = get(c, "/requests/2", **{"X-Thread-Key": k2}).json()
        assert got["me"]["state"] == "declined" and "ticket_url" not in got["me"]
    clean(*seen, keys=(pk(me),))


def test_the_console_shows_the_ticket_and_decides(fair, reqs, world):
    rsvc, w, ada, *_ = fair
    relay = reqs[1]
    day_ticket(w, ada)
    rsvc.settings.pass_page_oidc_client_secret = "x"
    rsvc.settings.pass_page_secret = "p" * 32
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, world[4])
        fid, _card, _k = ask(c, relay, PrivateKey(), product=None, ticket=DAY, kind="sponsored")
        page = c.get("/sales", headers=H).text
        assert "Day ticket · Garden day · asks 50% off · sponsored" in page
        assert DAY not in page.split('id="offers"')[1].split("<h2")[0]  # by name, never the id
        form = {"csrf": CSRF, "id": fid, "percent": "100", "note": ""}
        c.post("/sales/offer", data=form, headers=H)
        f = sales.store(rsvc).offer(fid)
        assert f["state"] == "accepted" and f["decided_percent"] == 100
        page = c.get("/sales", headers=H).text
        assert "<td>Day ticket · Garden day</td>" in page  # its code, in the codes' list


def test_a_products_offer_is_as_it_was_and_takes_the_offer_too(fair, reqs, keycast):
    rsvc, w, ada, *_ = fair
    relay = reqs[1]
    day_ticket(w, ada)
    sales.save_settings(rsvc, {"tickets": True})
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me)
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["product"] == "village-2027" and got["kind"] == "in_kind"
        assert not {"ticket", "event", "event_link"} & set(got)
        sales.decide_offer(rsvc, fid, 100, "", "Ada")
        webrequests.forget()
        text = body(mailer.SENT[-1])
        assert "gives you 100% off." in text and "when you get your pass" in text
        code = sales.store(rsvc).offer(fid)["code"]
        assert sales.store(rsvc).code(code)["product"] == "village-2027"
        assert get(c, "/requests/1", **{"X-Thread-Key": k}).json()["me"] == {"author": True}
        # its approval is for that product: never for a ticket
        r = kim(c, me, offer=fid)
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        r = kim(c, me, code=code)
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        assert admissions(w) == []
        r = buy(c, me, email="kim@example.org", offer=fid)
        assert r.status_code == 201, r.text
        assert r.json()["state"] == "active" and r.json()["total"]["percent_off"] == 100


def test_the_date_of_a_one_day_event():
    def ev(start, end, tz=""):
        return events.Event(
            d=D, kind=31923, creator="", version={}, hosts=set(), start=start, end=end, tz=tz
        )

    utc = dt.UTC
    noon = dt.datetime(2027, 1, 25, 12, 0, tzinfo=utc)
    assert ticketsales.one_day(ev(noon, noon + dt.timedelta(hours=3))) == "Monday 25 January 2027"
    assert ticketsales.one_day(ev(noon, None)) == "Monday 25 January 2027"
    assert ticketsales.one_day(ev(noon, noon + dt.timedelta(hours=13))) == ""
    # the day is the event's own time zone's: 23:30 UTC is the 26th in Brussels
    late = dt.datetime(2027, 1, 25, 23, 30, tzinfo=utc)
    assert ticketsales.one_day(ev(late, None, "Europe/Brussels")) == "Tuesday 26 January 2027"
    day = dt.date(2027, 1, 25)
    assert ticketsales.one_day(ev(day, None)) == "Monday 25 January 2027"
    assert ticketsales.one_day(ev(day, day)) == events.when_text(ev(day, day))
    assert ticketsales.one_day(ev(day, dt.date(2027, 2, 5))) == ""
    assert ticketsales.one_day(ev(None, None)) == ""
