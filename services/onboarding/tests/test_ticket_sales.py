"""Event tickets through the checkout (ticketsales.py, docs/sales.md §10): a ticket with a
price is ordered like a product, paid by card or against an invoice, and its buyer is admitted
to the event."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import re
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import events, guests, mailer, sales, testers, ticketsales
from belfry_onboarding.app import create_app

from .test_events import GROUP, D, World, make_event
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, shop  # noqa: F401 — fixtures
from .test_sales_console import H, post, signed_in
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import event, stripe  # noqa: F401 — fixtures
from .test_testers import LIVE_WH, TEST_WH, TESTER, deliver, envs, make_tester  # noqa: F401

DAY = f"{D}:day"


@pytest.fixture
def fair(envs, world, monkeypatch):
    """The sales tests' shop with both environments, and an event of Ada's on fake relays."""
    rsvc, live_odoo, test_odoo, made = envs
    ada = world[3]
    w = World()
    monkeypatch.setattr(events, "community", lambda svc, f: w.comm.query(f, None))
    monkeypatch.setattr(events, "groups", lambda svc, f: w.grp.query(f, None))

    def publish(svc, kind, tags, content=""):
        w.put(w.gkey, kind, tags, content)
        return True

    monkeypatch.setattr(events, "publish", publish)
    monkeypatch.setattr(guests, "_channel_event", lambda svc, kind, tags: publish(svc, kind, tags))
    monkeypatch.setattr(events, "names", lambda svc, pks: {})
    events._stores.clear()
    events.PER_IP.__init__(1000, 60)
    events.PER_EVENT.__init__(1000, 60)
    rsvc.settings.site_url = "https://belfry.test"
    sales.save_settings(rsvc, {"ticket_product": 1})
    sales.save_settings(rsvc, {"ticket_product": 1}, "test")
    make_event(w, ada)
    return rsvc, w, ada, live_odoo, test_odoo, made


def tk(w: World, host: PrivateKey, tid: str, title: str, *tags: list[str]):
    """A ticket type of the event, with whatever price tags."""
    return w.put(
        host,
        30402,
        [
            ["d", f"{D}:{tid}"],
            ["a", f"31923:{pk(host)}:{D}"],
            ["h", GROUP],
            ["title", title],
            ["t", "ticket"],
            *tags,
        ],
    )


def order(c, key, ticket=DAY, email="gus@example.org", **more):
    body = {"ticket": ticket, "name": "Gus Guest", "email": email, **more}
    return call(c, key, "/checkout", body)


def pay_card(c, o: dict, cents: int, eid: str, test: bool = False):
    ev = {**event(o["order"], o["order"], cents, eid=eid), "livemode": not test}
    if test:
        return deliver(c, ev, TEST_WH, "/hooks/stripe/test")
    return deliver(c, ev, LIVE_WH, "/hooks/stripe")


def admissions(w: World, ticket: str = DAY) -> list[dict]:
    return [e for e in w.grp.events if e["kind"] == 9000 and ["ticket", ticket] in e["tags"]]


ORG = {
    "kind": "organisation",
    "name": "Acme",
    "vat": "BE0123456789",
    "city": "Brussels",
    "country": "BE",
}


def test_a_ticket_paid_by_card_admits_a_new_person(fair, keycast):
    rsvc, w, ada, live_odoo, _t, made = fair
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["vat", "21"], ["quantity", "5"])
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        # off until an admin turns it on
        r = order(c, me)
        assert r.status_code == 409 and r.json()["error"] == "tickets_off"
        sales.save_settings(rsvc, {"tickets": True})
        # one of product / ticket; a ticket takes only a code made for it (an offer's)
        r = call(c, me, "/checkout", {"ticket": DAY, "product": "village-2027", "email": "a@b.co"})
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        r = order(c, me, code="ABCD-EFGH")
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        r = order(c, me, return_url="https://belfry.test/tickets/thanks")
        assert r.status_code == 201, r.text
        got = r.json()
        oid = got["order"]
        # the price is before VAT: 50 + 21%
        assert got["total"] == {"net": 50.0, "vat": 10.5, "gross": 60.5, "percent_off": 0}
        assert got["ticket"] == DAY and got["product"] is None and got["test"] is False
        assert got["state"] == "pending" and got["invoice"] is None and got["card_url"]
        f = made[-1]["form"]
        assert f["line_items[0][price_data][unit_amount]"] == "6050"
        assert f["line_items[0][price_data][product_data][name]"] == "Day ticket · Garden day"
        assert admissions(w) == []  # nothing before the money
        assert sales.store(rsvc).order(oid)["partner_id"] is None  # and no account
        # asking again: the same order
        assert order(c, me).json()["order"] == oid
        assert pay_card(c, got, 6050, "evt_1").json()["result"] == "paid"
        o = sales.store(rsvc).order(oid)
        assert o["state"] == "active" and o["step"] == "done"
        # admitted as a host's approval admits: the group's 9000, paid, with the ticket
        (e,) = admissions(w)
        assert e["pubkey"] == pk(w.gkey)
        assert ["paid", "paid"] in e["tags"] and ["h", GROUP] in e["tags"]
        assert ["by", "checkout"] in e["tags"] and ["name", "Gus Guest"] in e["tags"]
        who = next(t[1] for t in e["tags"] if t[0] == "p")
        assert who == o["ticket_pubkey"] != pk(me)
        assert events.people(rsvc, events.load(rsvc, D)).holders == {who: DAY}
        # the browser that paid is signed in
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["state"] == "active" and got["bunker_url"].startswith("bunker://")
        assert keycast.bunkers[-1]["client_pubkey"] == pk(me)
    # the event only: a guest of its group, no membership pass; the address is not proven
    reg = rsvc.odoo
    passes = reg.search_read(
        "belfry.pass", [["partner_id", "=", o["partner_id"]]], ["tier", "state", "guest_channel"]
    )
    assert [(p["tier"], p["state"], p["guest_channel"]) for p in passes] == [
        ("guest", "active", GROUP)
    ]
    assert not events.is_member(rsvc, who)
    partner = reg.read_one("res.partner", o["partner_id"], ["email", "belfry_pending_email"])
    assert not partner["email"] and partner["belfry_pending_email"] == "gus@example.org"
    # the invoice, made once paid: a person's line includes the VAT
    move = live_odoo.records["account.move"][o["invoice"]["move_id"]]
    line = move["invoice_line_ids"][0][2]
    assert line["name"] == "Day ticket · Garden day" and line["product_id"] == 1
    assert line["price_unit"] == 60.5 and line["tax_ids"] == [[6, 0, [10]]]
    assert move["payment_state"] == "paid" and "odoo_total" not in o["flags"]
    receipt = [m for m in mailer.SENT if "Day ticket · Garden day" in str(m["Subject"])]
    assert receipt and "confirm" in receipt[-1].get_body(("plain",)).get_content().lower()


def test_an_invoice_for_an_organisation_and_a_member_on_their_own_account(fair, keycast):
    rsvc, w, ada, live_odoo, _t, _made = fair
    tk(w, ada, "day", "Day ticket", ["price", "121", "EUR"], ["vat", "21", "included"])
    tk(w, ada, "eve", "Evening", ["price", "10", "EUR"])
    sales.save_settings(rsvc, {"tickets": True})
    bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    with TestClient(create_app(rsvc)) as c:
        r = order(c, bob, email="bob@example.org", pay="invoice", billing=ORG)
        assert r.status_code == 201, r.text
        got = r.json()
        # the price includes the VAT; an organisation's invoice shows it on top of the net
        assert got["total"] == {"net": 100.0, "vat": 21.0, "gross": 121.0, "percent_off": 0}
        assert got["state"] == "invoiced" and got["invoice"]["number"]
        o = sales.store(rsvc).order(got["order"])
        move = live_odoo.records["account.move"][o["invoice"]["move_id"]]
        line = move["invoice_line_ids"][0][2]
        assert line["price_unit"] == 100.0 and line["tax_ids"] == [[6, 0, [11]]]
        assert line["name"] == "Day ticket · Garden day"
        assert admissions(w) == []
        # the transfer is reconciled in Odoo
        move["payment_state"] = "paid"
        sales.tick(rsvc, force=True)
        o = sales.store(rsvc).order(o["id"])
        assert o["state"] == "active" and "odoo_total" not in o["flags"]
        (e,) = admissions(w)
        assert ["p", pk(bob), "member"] in e["tags"] and ["paid", "paid"] in e["tags"]
        # on his own account: nothing new, no pass
        assert o["partner_id"] == bob_id and not o.get("pass_id") and not o.get("bunker_url")
        tiers = rsvc.odoo.search_read("belfry.pass", [["partner_id", "=", bob_id]], ["tier"])
        assert len(tiers) == 1 and tiers[0]["tier"] != "guest"
        # he sees his ticket; a second one of the same event is refused
        me = call(c, bob, f"/events/{D}", method="GET").json()["me"]
        assert me["ticket"] == DAY and me["ticket_code"]
        r = order(c, bob, email="bob@example.org")  # a retried request: the same order
        assert r.status_code == 200 and r.json()["order"] == o["id"]
        r = order(c, bob, ticket=f"{D}:eve", email="bob@example.org")
        assert r.status_code == 409 and r.json()["error"] == "already"
        # a full credit note later: nobody is removed, the order is flagged
        move["payment_state"] = "reversed"
        sales.tick(rsvc, force=True)
        o = sales.store(rsvc).order(o["id"])
        assert "refunded_admitted" in o["flags"] and o["closed"]
        assert pk(bob) in events.people(rsvc, events.load(rsvc, D)).holders


def test_vat_before_and_included_for_people_and_organisations():
    def totals(kind: str, *vat: str) -> dict:
        t = events.Ticket(d=DAY, id="day", title="T", priced=True, amount="50", currency="EUR")
        if vat:
            t.vat, t.vat_included = vat[0], "included" in vat
        w = ticketsales.Wanted(ev=None, t=t)
        return sales.split(ticketsales.unit(w, kind), ticketsales.amounts(t)[2], 0, kind)

    before = {"net": 5000, "vat": 1050, "gross": 6050, "percent_off": 0}
    assert totals("person", "21") == before and totals("organisation", "21") == before
    incl = {"net": 4132, "vat": 868, "gross": 5000, "percent_off": 0}
    assert totals("person", "21", "included") == incl
    assert totals("organisation", "21", "included") == incl
    # no vat tag: the price is the total, no VAT in Belfry's own sum
    none = {"net": 5000, "vat": 0, "gross": 5000, "percent_off": 0}
    assert totals("person") == none and totals("organisation") == none


def test_what_the_checkout_refuses(fair):
    rsvc, w, ada, _l, _t, _made = fair
    now = int(time.time())
    tk(w, ada, "past", "Early bird", ["price_at", "40", "EUR", "", str(now - 60)])
    tk(
        w,
        ada,
        "now",
        "Regular",
        ["price_at", "40", "EUR", "", str(now + 3600)],
        ["price", "75", "EUR"],
        ["vat", "21", "included"],
    )
    tk(w, ada, "time", "Helper", ["price", "2", "hours"])
    tk(w, ada, "usd", "Dollars", ["price", "20", "USD"])
    tk(w, ada, "free", "Free one")
    tk(w, ada, "odd", "Odd", ["price", "lots", "EUR"])
    tk(w, ada, "ask", "By application", ["price", "20", "EUR"], ["closed"])
    tk(w, ada, "mem", "Members", ["price", "20", "EUR"], ["members"])
    sales.save_settings(rsvc, {"tickets": True})
    me = PrivateKey()
    bob, _ = person(rsvc, "bob@example.org", "Bob")

    def err(c, ticket, key=me, **more):
        r = order(c, key, ticket=ticket, **more)
        return r.status_code, r.json().get("error")

    with TestClient(create_app(rsvc)) as c:
        assert err(c, f"{D}:nope") == (404, "unknown_ticket")
        assert err(c, "zzzzzz:day") == (404, "unknown_ticket")
        assert err(c, "no colon") == (404, "unknown_ticket")
        assert err(c, f"{D}:past") == (409, "not_on_sale")
        assert err(c, f"{D}:odd") == (409, "not_on_sale")
        assert err(c, f"{D}:time") == (409, "currency")
        assert err(c, f"{D}:usd") == (409, "currency")
        assert err(c, f"{D}:free") == (409, "free")
        assert err(c, f"{D}:ask") == (409, "needs_application")
        assert err(c, f"{D}:mem") == (403, "members_only")
        assert err(c, f"{D}:now", email="nope") == (422, "invalid_email")
        assert sales.store(rsvc).orders() == []
        # the dated price that applies now is the one charged
        r = order(c, me, ticket=f"{D}:now")
        assert r.status_code == 201, r.text
        assert r.json()["total"] == {"net": 33.06, "vat": 6.94, "gross": 40.0, "percent_off": 0}
        # a member buys a members' ticket
        assert order(c, bob, ticket=f"{D}:mem", email="bob@example.org").status_code == 201
        # no Odoo product for tickets: nothing can be sold
        sales.save_settings(rsvc, {"ticket_product": None, "odoo_products": {"a": 1, "b": 2}})
        assert err(c, f"{D}:now", key=PrivateKey()) == (409, "cannot_sell")
        sales.save_settings(rsvc, {"ticket_product": 1})
        # a cancelled event sells nothing
        w.put(ada, 1985, [["h", GROUP], ["L", events.STATUS_NS], ["l", "cancelled"]])
        assert err(c, f"{D}:now", key=PrivateKey()) == (409, "event_over")


def test_an_event_with_no_ticket_types_is_free(fair):
    rsvc, _w, _ada, _l, _t, _made = fair
    sales.save_settings(rsvc, {"tickets": True})
    with TestClient(create_app(rsvc)) as c:
        r = order(c, PrivateKey(), ticket=f"{D}:free")
        assert r.status_code == 409 and r.json()["error"] == "free"


def test_a_tester_buys_while_the_switch_is_off(fair, keycast):
    rsvc, w, ada, live_odoo, test_odoo, made = fair
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["vat", "21"], ["quantity", "1"])
    tess, tess_id = make_tester(rsvc)
    partners = len(rsvc.odoo.search_read("res.partner", [], ["id"]))
    browser = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        assert not ticketsales.on(rsvc)
        r = order(c, browser, email=TESTER)
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["test"] is True and got["total"]["gross"] == 60.5
        assert made[-1]["auth"].endswith("T" * 24)  # Stripe's test key
        # a test order holds no place
        ev = events.load(rsvc, D)
        assert ticketsales.left_now(rsvc, ticketsales.Wanted(ev, events.tickets(rsvc, ev)[0])) == 1
        assert pay_card(c, got, 6050, "evt_t", test=True).json()["result"] == "paid"
        o = sales.store(rsvc).order(got["order"])
        assert o["state"] == "active" and o["partner_id"] == tess_id
        # on the tester's own account, marked as a test; nothing created, nobody signed in
        (e,) = admissions(w)
        assert ["p", pk(tess), "member"] in e["tags"] and ["test", "1"] in e["tags"]
        assert not o.get("bunker_url") and not o.get("pass_id")
        assert len(rsvc.odoo.search_read("res.partner", [], ["id"])) == partners
        assert o["invoice"]["move_id"] in test_odoo.records["account.move"]
        assert not live_odoo.records.get("account.move")
        # the tester holds the ticket and still takes no place
        ppl = events.people(rsvc, ev)
        assert ppl.holders == {pk(tess): DAY} and ppl.taken(DAY) == 0
        j = call(c, tess, f"/events/{D}", method="GET").json()
        assert j["me"]["ticket"] == DAY and j["tickets"][0]["left"] == 1
        # a normal buyer still can't
        r = order(c, PrivateKey())
        assert r.status_code == 409 and r.json()["error"] == "tickets_off"
    # the test environment not set up: never production instead
    testers.add_tester(rsvc, "tom@example.org", "Tom")
    sales.save_settings(rsvc, {"ticket_product": None, "odoo_products": {}}, "test")
    with TestClient(create_app(rsvc)) as c:
        r = order(c, PrivateKey(), email="tom@example.org")
        assert r.status_code == 409 and r.json()["error"] == "test_not_set_up"
        assert "Plugins → " in r.json()["message"] and "Test" in r.json()["message"]


def test_places_sold_out_and_freed_when_an_order_expires(fair):
    rsvc, w, ada, _l, _t, _made = fair
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["vat", "21"], ["quantity", "1"])
    sales.save_settings(rsvc, {"tickets": True})
    make_tester(rsvc)
    one, two = PrivateKey(), PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        # a tester's order takes no place
        assert order(c, PrivateKey(), email=TESTER).status_code == 201
        assert c.get(f"/api/v1/events/{D}", headers=H).json()["tickets"][0]["left"] == 1
        r = order(c, one, email="one@example.org")
        assert r.status_code == 201, r.text
        oid = r.json()["order"]
        t = c.get(f"/api/v1/events/{D}", headers=H).json()["tickets"][0]
        assert t["left"] == 0 and t["can_pay"] is False and t["quantity"] == 1
        r = order(c, two, email="two@example.org")
        assert r.status_code == 409 and r.json()["error"] == "sold_out"
        # the hold ends unpaid: nobody admitted, the place is free again
        past = sales.iso(sales.now() - dt.timedelta(minutes=1))
        sales.store(rsvc).update(oid, hold_until=past)
        assert sales.expire_holds(rsvc) == 1
        assert sales.store(rsvc).order(oid)["state"] == "expired" and admissions(w) == []
        t = c.get(f"/api/v1/events/{D}", headers=H).json()["tickets"][0]
        assert t["left"] == 1 and t["can_pay"] is True
        r = order(c, two, email="two@example.org")
        assert r.status_code == 201
        # cancelled by an admin: free again too
        sales.cancel(rsvc, r.json()["order"], "a test")
        assert c.get(f"/api/v1/events/{D}", headers=H).json()["tickets"][0]["left"] == 1


def test_a_product_checkout_is_unchanged(fair, keycast):
    rsvc, _w, _ada, live_odoo, _t, _made = fair
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, PrivateKey(), pay="invoice")
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["product"] == "village-2027" and "ticket" not in got
        assert got["total"] == {"net": 165.29, "vat": 34.71, "gross": 200.0, "percent_off": 0}
        line = live_odoo.records["account.move"][1]["invoice_line_ids"][0][2]
        assert line["name"] == "Two-week pass" and line["price_unit"] == 200.0
        r = call(c, PrivateKey(), "/checkout", {"email": "a@example.org"})
        assert r.status_code == 404 and r.json()["error"] == "unknown_product"


def test_can_pay_left_and_checkout_url_in_the_events_api(fair):
    rsvc, w, ada, _l, _t, _made = fair
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["quantity", "3"], ["position", "1"])
    tk(w, ada, "time", "Helper", ["price", "2", "hours"], ["position", "2"])
    tk(w, ada, "open", "Open", ["position", "3"])
    tess, _ = make_tester(rsvc)
    bob, _ = person(rsvc, "bob@example.org", "Bob")
    template = "https://belfry.test/tickets?event={link}&ticket={id}"
    sales.save_settings(rsvc, {"checkout_url": template})

    def seen(c, key=None):
        r = (
            call(c, key, f"/events/{D}", method="GET")
            if key
            else c.get(f"/api/v1/events/{D}", headers=H)
        )
        return {t["id"]: t for t in r.json()["tickets"]}

    with TestClient(create_app(rsvc)) as c:
        # off: nobody but a tester
        by = seen(c)
        assert by["day"]["can_pay"] is False and by["day"]["checkout_url"] is None
        assert by["day"]["left"] == 3 and by["day"]["needs_payment"] is True
        assert seen(c, bob)["day"]["can_pay"] is False
        assert seen(c, tess)["day"]["can_pay"] is True
        r = call(c, bob, f"/events/{D}/join", {"ticket": DAY})
        assert r.status_code == 409 and r.json()["error"] == "payment"
        assert "checkout" not in r.json()
        r = call(c, tess, f"/events/{D}/join", {"ticket": DAY})
        assert r.status_code == 409 and r.json()["checkout"] is True
        # on: anyone, for the tickets priced in the sales currency
        sales.save_settings(rsvc, {"tickets": True})
        by = seen(c)
        assert by["day"]["can_pay"] is True and by["day"]["left"] == 3
        assert by["day"]["checkout_url"] == (
            f"https://belfry.test/tickets?event={D}&ticket={D}%3Aday"
        )
        assert by["time"]["can_pay"] is False and by["time"]["checkout_url"] is None
        assert by["time"]["needs_payment"] is True and by["time"]["left"] is None
        assert by["open"]["can_pay"] is False and by["open"]["needs_payment"] is False
        r = call(c, bob, f"/events/{D}/join", {"ticket": DAY})
        assert r.status_code == 409 and r.json()["error"] == "payment"
        assert r.json()["checkout"] is True
        r = call(c, bob, f"/events/{D}/join", {"ticket": f"{D}:time"})
        assert r.status_code == 409 and "checkout" not in r.json()
        # the event's own address fills {link}
        assert events.store(rsvc).claim(D, "garden-day-k3x9")
        assert seen(c)["day"]["checkout_url"].startswith(
            "https://belfry.test/tickets?event=garden-day-k3x9&ticket="
        )
        # an unpaid order holds a place
        assert order(c, PrivateKey()).status_code == 201
        assert seen(c)["day"]["left"] == 2
        # Odoo can't invoice tickets: not payable
        sales.save_settings(rsvc, {"ticket_product": None, "odoo_products": {"a": 1, "b": 2}})
        assert seen(c)["day"]["can_pay"] is False


def test_the_checkout_url_template_is_checked(fair, world):
    rsvc = fair[0]
    ok = ticketsales.clean_checkout_url
    good = "https://belfry.test/tickets?event={link}&ticket={id}"
    assert ok(rsvc, good) == good and ok(rsvc, "  ") == ""
    assert ok(rsvc, "https://www.belfry.test/buy/{link}/{id}")
    for bad in (
        "http://belfry.test/t?e={link}",
        "https://evil.test/t?e={link}",
        "https://belfry.test.evil.test/{id}",
        "https://evilbelfry.test/{id}",
        "https://belfry.test/t?e={link}&x={name}",
        "https://belfry.test/t?e={link",
        "https://{link}.belfry.test/",
        "https://{id}/x",
        "https://user@belfry.test/{id}",
        "https://belfry.test:8443/{id}",
        "https://belfry.test/{id}#frag",
        "https://belfry.test/ {id}",
        'https://belfry.test/"><script>{id}',
        "javascript:alert(1)",
        "https://belfry.test/" + "a" * 500,
    ):
        with pytest.raises(ValueError):
            ok(rsvc, bad)
    # the console: admins, a fixed message for a bad one, nothing saved then
    rsvc.settings.pass_page_oidc_client_secret = "pass-secret"
    rsvc.settings.pass_page_secret = "p" * 32
    _bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, bob_id)
        post(c, "/sales/tickets", tickets="1", checkout_url=good)
        assert not sales.settings(rsvc)["tickets"] and not sales.settings(rsvc)["checkout_url"]
        signed_in(c, rsvc, world[4])
        page = c.get("/sales", headers=H).text
        assert (
            "Sell event tickets through the checkout" in page
            and 'name="tickets" value="1" checked' not in page
        )
        post(c, "/sales/tickets", tickets="1", checkout_url="https://evil.test/{id}")
        st = sales.settings(rsvc)
        assert not st["tickets"] and not st["checkout_url"]
        assert "https, on this belfry" in c.get("/sales", headers=H).text
        post(c, "/sales/tickets", tickets="1", checkout_url=good)
        st = sales.settings(rsvc)
        assert st["tickets"] is True and st["checkout_url"] == good
        page = c.get("/sales", headers=H).text
        assert 'name="tickets" value="1" checked' in page and "event={link}&amp;ticket={id}" in page
        assert "can be bought" in page
        post(c, "/sales/tickets", checkout_url="")
        st = sales.settings(rsvc)
        assert st["tickets"] is False and st["checkout_url"] == ""
        # the Odoo product for tickets is picked with the others
        t = c.get("/plugins/odoo", headers=H).text
        assert "Event tickets are invoiced as" in t
        post(c, "/plugins/odoo/live/picks", ticket_product="1", tax_included="10")
        assert sales.settings(rsvc)["ticket_product"] == 1


def test_the_public_page_links_to_where_tickets_are_bought(fair):
    rsvc, w, ada, _l, _t, _made = fair
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["position", "1"])
    tk(w, ada, "time", "Helper", ["price", "2", "hours"], ["position", "2"])
    sales.save_settings(rsvc, {"tickets": True})
    link = f"https://belfry.test/tickets?event={D}&amp;ticket={D}%3Aday"
    with TestClient(create_app(rsvc)) as c:
        # no template: rows as they were, RSVP in the chat
        page = c.get(f"/events/{D}").text
        assert "<li><b>Day ticket</b>" in page and 'class="buy"' not in page
        assert ">RSVP</a>" in page
        sales.save_settings(
            rsvc, {"checkout_url": "https://belfry.test/tickets?event={link}&ticket={id}"}
        )
        page = c.get(f"/events/{D}").text
        assert f'<li><a class="buy" href="{link}"><b>Day ticket</b></a>' in page
        assert "<li><b>Helper</b>" in page  # hours are settled with the hosts
        assert f'<a class="go" href="{link}">Get tickets</a>' in page and ">RSVP</a>" not in page
        # nothing from the request is ever in the page
        page = c.get(f"/events/{D}?ticket=%22%3E%3Cscript%3Ex&event=evil").text
        assert "script>x" not in page and "evil" not in page and link in page
        # a free ticket next to it: the main button stays the RSVP
        tk(w, ada, "open", "Open", ["position", "3"])
        page = c.get(f"/events/{D}").text
        assert ">RSVP</a>" in page and f'<a class="buy" href="{link}">' in page
        # switched off: no links
        sales.save_settings(rsvc, {"tickets": False})
        assert 'class="buy"' not in c.get(f"/events/{D}").text


def test_a_buyer_whose_address_is_an_accounts_gets_the_ticket_there(fair, keycast):
    rsvc, w, ada, _l, _t, _made = fair
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["vat", "21"])
    sales.save_settings(rsvc, {"tickets": True})
    bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        r = order(c, me, email="BOB@example.org")
        assert r.status_code == 201  # the checkout doesn't say the address is a member's
        assert pay_card(c, r.json(), 6050, "evt_1").json()["result"] == "paid"
        oid = r.json()["order"]
        o = sales.store(rsvc).order(oid)
        temp, temp_key = o["partner_id"], o["ticket_pubkey"]
        assert temp != bob_id and temp_key != pk(bob)
        receipt = [m for m in mailer.SENT if "confirm your e-mail" in str(m["Subject"])][-1]
        text = receipt.get_body(preferencelist=("plain",)).get_content()
        url = re.search(r"https://belfry\.belfry\.test(/sales/confirm/\S+)", text).group(1)
        assert c.post(url, headers=H).status_code == 200
    o = sales.store(rsvc).order(oid)
    assert o["partner_id"] == bob_id and o["moved_from"] == temp and o["ticket_pubkey"] == pk(bob)
    # Bob holds the ticket; the temporary guest is out of the event and closed
    assert events.people(rsvc, events.load(rsvc, D)).holders == {pk(bob): DAY}
    old = rsvc.odoo.search_read("belfry.pass", [["partner_id", "=", temp]], ["state"])
    assert [p["state"] for p in old] == ["revoked"]
    mine = rsvc.odoo.search_read("belfry.pass", [["partner_id", "=", bob_id]], ["tier"])
    assert len(mine) == 1  # a member needs no guest pass, and got no second pass
