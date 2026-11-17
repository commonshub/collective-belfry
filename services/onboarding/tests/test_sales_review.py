"""Selling passes, the review of 2026-11-17: bound confirmation links, payments after an order
ended, locks, Odoo contacts never matched by an unproven address, abuse limits, the outbox,
return_url, the tick's invoice reads, and the small ones."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import console, core, mailer, sales, salesmail, salesodoo, salespay
from belfry_onboarding.app import create_app
from belfry_onboarding.stripe_sig import sign_header

from .fakes import FakeOdoo
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import nip98
from .test_roles import API, _tick, call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, free_code, shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import WHSEC, event, stripe  # noqa: F401 — fixtures

H = {"Host": "belfry.belfry.test"}


def post_from(c, key, path, data, ip="203.0.113.7"):
    at = int(time.time()) - 50 + next(_tick) % 50
    body = json.dumps(data).encode()
    headers = {
        **H,
        "Authorization": nip98(key, API + path, "POST", body, created_at=at),
        "Content-Type": "application/json",
        "X-Forwarded-For": ip,
    }
    return c.post("/api/v1" + path, content=body, headers=headers)


def body(m) -> str:
    return m.get_body(("plain",)).get_content()


def last_link(kind="/sales/confirm/") -> str:
    for m in reversed(mailer.SENT):
        found = re.search(r"https://belfry\.belfry\.test(" + kind + r"\S+)", body(m))
        if found:
            return found.group(1)
    raise AssertionError("no link mailed")


# ── 1. a confirmation link proves only the address it was mailed to ─────────────────────────
def test_an_unproven_account_has_one_address(shop, keycast):
    rsvc = shop[0]
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, me, code=free_code(rsvc)).json()["order"]
        o = sales.store(rsvc).order(oid)
        # the account's own key signs a second order (stand in: our key on its pass)
        rsvc.odoo.write("belfry.pass", [o["pass_id"]], {"keycast_pubkey": pk(me)})
        r = buy(c, me, product="friend", email="victim@example.org")
        assert r.status_code == 409 and r.json()["error"] == "email_mismatch"
        # the same address is fine
        r = buy(c, me, code=free_code(rsvc, "friend"), product="friend")
        assert r.status_code == 201
    pid = o["partner_id"]
    # confirming another address does nothing
    assert sales.confirm(rsvc, pid, "victim@example.org")["state"] == "mismatch"
    assert sales.is_unverified(rsvc, pid)
    # a token is bound to its address: one made for another address is refused
    st = sales.store(rsvc)
    forged = st.confirm_token(oid, pid, "victim@example.org", "receipt")
    with TestClient(create_app(rsvc)) as c:
        assert c.get(f"/sales/confirm/{forged}", headers=H).status_code == 404
        assert c.post(f"/sales/confirm/{forged}", headers=H).status_code == 404
    assert sales.is_unverified(rsvc, pid)
    # every confirmation link mailed went to the address it confirms
    for m in mailer.SENT:
        if "/sales/confirm/" in body(m):
            token = re.search(r"/sales/confirm/(\S+)", body(m)).group(1)
            assert st.confirm_of(token)["address"] == str(m["To"]).lower()


def test_an_unproven_address_never_makes_an_admin(shop, monkeypatch):
    rsvc = shop[0]
    rsvc.settings.pass_page_oidc_client_secret = "x"
    rsvc.settings.pass_page_secret = "p" * 32
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), code=free_code(rsvc), email="admin2@belfry.test").json()["order"]
    pid = sales.store(rsvc).order(oid)["partner_id"]
    monkeypatch.setattr(console, "admin_emails", lambda svc: {"admin2@belfry.test"})
    who = console.viewer(rsvc, {"partner": pid, "csrf": "x"})
    assert who is not None and who["admin"] is False and who["email"] == ""


# ── 2 and 4. a payment after the order ended reopens it ─────────────────────────────────────
def expired_order(rsvc, c, **more):
    oid = buy(c, PrivateKey(), pay="invoice", **more).json()["order"]
    sales.store(rsvc).update(oid, hold_until=sales.iso(sales.now() - dt.timedelta(minutes=1)))
    sales.tick(rsvc, force=True)
    o = sales.store(rsvc).order(oid)
    assert o["state"] == "expired" and o["invoice"]["credited"]
    return oid


def test_a_card_payment_after_expiry_reopens_with_a_new_invoice(stripe, keycast):
    (rsvc, fake, _v), _made, _s, _e = stripe
    with TestClient(create_app(rsvc)) as c:
        oid = expired_order(rsvc, c)
        body_ = json.dumps(event(oid, "INV/2026/0001", 20000)).encode()
        r = c.post(
            "/hooks/stripe",
            content=body_,
            headers={**H, "Stripe-Signature": sign_header(WHSEC, body_)},
        )
        assert r.json()["result"] == "paid"
    o = sales.store(rsvc).order(oid)
    assert o["state"] == "active" and "paid_late" in o["flags"] and o["step"] == "done"
    assert o["history"][0]["number"] == "INV/2026/0001" and o["history"][0]["credited"]
    new = fake.records["account.move"][o["invoice"]["move_id"]]
    assert new["ref"] == f"Belfry {oid}-2" and new["payment_state"] == "paid"
    assert new["paid_journal"] == 7
    # the old invoice is reversed: the tick looks only at the current one
    sales.tick(rsvc, force=True)
    passes = rsvc.odoo.search_read("belfry.pass", [["sale_order", "=", oid]], ["state"])
    assert [p["state"] for p in passes] == ["active"]


def test_reopen_and_mark_paid_by_hand_and_oversold(seller, world, keycast):
    rsvc, fake, _v = seller
    sales.save_product(rsvc, {**sales.product(rsvc, "village-2027"), "quantity": 1})
    rsvc.settings.pass_page_oidc_client_secret = "x"
    rsvc.settings.pass_page_secret = "p" * 32
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        oid = expired_order(rsvc, c)
        # someone else takes the only place meanwhile
        buy(c, PrivateKey(), code=free_code(rsvc), email="z@example.org")
        from .test_sales_console import CSRF, signed_in

        signed_in(c, rsvc, world[4])
        c.post(
            "/sales/order",
            data={"csrf": CSRF, "id": oid, "reason": "", "do": "reopen"},
            headers=H,
            follow_redirects=False,
        )
        assert sales.store(rsvc).order(oid)["state"] == "expired"  # a reason is required
        c.post(
            "/sales/order",
            data={"csrf": CSRF, "id": oid, "reason": "paid on day 6", "do": "reopen"},
            headers=H,
        )
    o = sales.store(rsvc).order(oid)
    assert o["state"] == "active" and {"paid_late", "oversold"} <= set(o["flags"])
    assert fake.records["account.move"][o["invoice"]["move_id"]]["paid_journal"] == 8


def test_no_endless_payment_on_a_reversed_invoice(seller, keycast):
    rsvc, fake, _v = seller
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
    fake.down = True
    sales.mark_paid(rsvc, oid, "hand", "x", "Ada")
    assert sales.store(rsvc).order(oid)["step"] == "register_payment"
    fake.down = False
    fake.records["account.move"][1]["payment_state"] = "reversed"
    sales.tick(rsvc, force=True)
    o = sales.store(rsvc).order(oid)
    assert o["step"] == "done" and "payment_not_booked" in o["flags"]


def test_held_until_on_the_reminder_and_the_pay_page(seller):
    rsvc, _f, _v = seller
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
        o = sales.store(rsvc).order(oid)
        page = c.get(f"/pay/{oid}", headers=H).text
        assert f"Your place is held until {o['hold_until'][:10]}" in page
        sales.store(rsvc).update(oid, hold_until=sales.iso(sales.now() + dt.timedelta(days=1)))
        sales.tick(rsvc, force=True)
    rem = [m for m in mailer.SENT if "held until" in str(m["Subject"])]
    assert len(rem) == 1 and "still counts" in body(rem[0])


# ── 3. cancel and expiry under the order's lock, state read again ───────────────────────────
def test_expiry_and_cancel_read_the_state_again(seller, keycast, monkeypatch):
    rsvc, _f, _v = seller
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
    st = sales.store(rsvc)
    stale = st.orders(("confirm", "pending", "invoiced"))
    st.update(oid, hold_until=sales.iso(sales.now() - dt.timedelta(minutes=1)))
    stale = [{**x, "hold_until": sales.iso(sales.now() - dt.timedelta(minutes=1))} for x in stale]
    sales.mark_paid(rsvc, oid, "hand", "x", "Ada")  # paid between the listing and the expiry
    monkeypatch.setattr(st, "orders", lambda *a, **k: stale)
    assert sales.expire_holds(rsvc) == 0
    monkeypatch.undo()
    assert st.order(oid)["state"] == "active"
    assert sales.cancel(rsvc, oid, "late")["state"] == "active"


# ── 5. Odoo contacts: never matched by an unproven address ──────────────────────────────────
def test_odoo_contacts_and_the_invoice_mail(seller):
    rsvc, fake, vies = seller
    sales.save_product(rsvc, {**sales.product(rsvc, "village-2027"), "quantity": None})
    victim = fake.add("res.partner", name="Victim", email="ada@example.org", is_company=False)
    acme = fake.add("res.partner", name="Acme", vat="BE0123456789", is_company=True)
    mailer.SENT.clear()
    org = {
        "kind": "organisation",
        "name": "Acme",
        "vat": "BE0123456789",
        "city": "Brussels",
        "country": "BE",
    }
    with TestClient(create_app(rsvc)) as c:
        buy(c, PrivateKey(), pay="invoice")
        buy(
            c,
            PrivateKey(),
            pay="invoice",
            email="ada@example.org",
            product="village-2027",
            name="Other",
        )
        vies.answer = None  # VIES down: no match by VAT
        buy(c, PrivateKey(), pay="invoice", billing=org, email="a@acme.test")
        vies.answer = True
        buy(c, PrivateKey(), pay="invoice", billing=org, email="b@acme.test")
    partners = [m["partner_id"] for m in fake.records["account.move"].values()]
    assert victim not in partners[:2] and partners[0] != partners[1]
    assert partners[2] != acme and partners[3] == acme
    # the invoice mail carries the PDF; Odoo sent nothing itself
    inv = [m for m in mailer.SENT if str(m["Subject"]).startswith("Invoice INV/2026/0001")][0]
    att = [p for p in inv.iter_attachments()]
    assert att and att[0].get_filename() == "INV-2026-0001.pdf"
    assert att[0].get_content().startswith(b"%PDF")
    assert "How to pay" in body(inv)


def test_an_invoice_without_its_pdf_is_flagged_and_card_only_gets_the_card_link(stripe):
    (rsvc, fake, _v), _m, _s, _e = stripe
    sales.save_settings(rsvc, {"bank_holder": "", "bank_iban": ""})
    fake.no_pdf = True
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="card").json()["order"]
    o = sales.store(rsvc).order(oid)
    assert "invoice_pdf_missing" in o["flags"]
    inv = [m for m in mailer.SENT if str(m["Subject"]).startswith("Invoice")][0]
    assert "Pay by card: https://checkout.stripe.com/" in body(inv)
    assert "How to pay" not in body(inv)


# ── 6. limits per IP and per address; a free product without a code waits for its address ───
def test_limits_per_ip_and_per_address(seller):
    rsvc, _f, _v = seller
    sales.save_product(rsvc, {**sales.product(rsvc, "village-2027"), "quantity": 100})
    with TestClient(create_app(rsvc)) as c:
        # an address holds at most 3 open orders
        for i in range(3):
            r = post_from(
                c,
                PrivateKey(),
                "/checkout",
                {
                    "product": "village-2027",
                    "name": "A",
                    "email": "a@example.org",
                    "pay": "invoice",
                },
                ip=f"198.51.100.{i}",
            )
            assert r.status_code == 201, r.text
        r = post_from(
            c,
            PrivateKey(),
            "/checkout",
            {"product": "village-2027", "name": "A", "email": "a@example.org", "pay": "invoice"},
            ip="198.51.100.9",
        )
        assert r.status_code == 429
        # a limited product: two unpaid holds per IP
        for i in range(2):
            r = post_from(
                c,
                PrivateKey(),
                "/checkout",
                {
                    "product": "village-2027",
                    "name": "B",
                    "email": f"b{i}@example.org",
                    "pay": "invoice",
                },
                ip="192.0.2.1",
            )
            assert r.status_code == 201
        r = post_from(
            c,
            PrivateKey(),
            "/checkout",
            {"product": "village-2027", "name": "B", "email": "b9@example.org", "pay": "invoice"},
            ip="192.0.2.1",
        )
        assert r.status_code == 429
        # ten checkouts an hour per IP, whatever they are
        for i in range(7):
            post_from(
                c,
                PrivateKey(),
                "/checkout",
                {"product": "friend", "name": "C", "email": f"c{i}@example.org"},
                ip="192.0.2.1",
            )
        r = post_from(
            c,
            PrivateKey(),
            "/checkout",
            {"product": "friend", "name": "C", "email": "c99@example.org"},
            ip="192.0.2.1",
        )
        assert r.status_code == 429
        # offers: three a day per key
        me = PrivateKey()
        offer = {
            "product": "village-2027",
            "name": "K",
            "email": "k@example.org",
            "offer": "help",
            "percent": 50,
        }
        for _ in range(3):
            assert post_from(c, me, "/offers", offer, ip="192.0.2.50").status_code in (201, 429)
        assert post_from(c, me, "/offers", offer, ip="192.0.2.51").status_code == 429


def test_five_sales_mails_a_day_per_address(seller):
    rsvc, _f, _v = seller
    st = sales.store(rsvc)
    for i in range(7):
        st.queue_mail("offer_answer", f"x{i}", "z@example.org")
    ids = [m["id"] for m in st.due_mails()]
    for mid in ids[:5]:
        st.mail_done(mid, "sent")
    assert st.sent_to("z@example.org") == 5
    assert salesmail.deliver_due(rsvc) == 0  # held back (nothing can be sent today)
    assert len(st.due_mails()) == 2


def test_a_free_product_without_a_code_waits_for_its_address(shop, keycast):
    rsvc = shop[0]
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, me, product="friend", email="kim@example.org")
        assert r.status_code == 201 and r.json()["state"] == "confirm"
        oid = r.json()["order"]
        assert rsvc.odoo.search_read("belfry.pass", [["sale_order", "=", oid]], ["id"]) == []
        link = last_link()
        page = c.get(link, headers=H).text
        assert "Friend" in page and "free" in page and "kim@example.org" in page
        done = c.post(link, headers=H, data={"answer": "yes"})
        assert "your pass is ready" in done.text
        got = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert got["state"] == "active" and "bunker_url" not in got
        o = sales.store(rsvc).order(oid)
        partner = rsvc.odoo.read_one("res.partner", o["partner_id"], ["email"])
        assert partner["email"] == "kim@example.org"  # proven by the link
        assert c.post(link, headers=H, data={"answer": "yes"}).status_code == 404
        # this wasn't me: the order ends
        r = buy(c, PrivateKey(), product="friend", email="lee@example.org")
        link = last_link()
        assert "Nothing more happens" in c.post(link, headers=H, data={"answer": "no"}).text
        o = sales.store(rsvc).order(r.json()["order"])
        assert o["state"] == "cancelled" and "not_me" in o["flags"]


def test_this_wasnt_me_on_a_receipt_ends_the_sessions(shop, keycast):
    rsvc = shop[0]
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), code=free_code(rsvc)).json()["order"]
        assert any(not b.get("revoked") for b in keycast.bunkers)
        c.post(last_link(), headers=H, data={"answer": "no"})
    o = sales.store(rsvc).order(oid)
    assert "not_me" in o["flags"] and not o.get("bunker_url")
    assert all(b.get("revoked") for b in keycast.bunkers if b["app_name"] == "Tickets")
    assert not sales.is_unverified(rsvc, o["partner_id"])  # no pending address any more
    p = rsvc.odoo.read_one("res.partner", o["partner_id"], ["email"])
    assert not p["email"]


# ── 7. the outbox: sent only when the provider took it ──────────────────────────────────────
def test_a_mail_that_fails_is_sent_again(shop, monkeypatch, keycast):
    rsvc = shop[0]
    rsvc.settings.smtp_host = "smtp.test"
    calls = {"n": 0}

    def smtp(self, msg, rcpts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("down")

    monkeypatch.setattr(mailer.Mailer, "_smtp", smtp)
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), code=free_code(rsvc)).json()["order"]
    assert sales.store(rsvc).mail_state("receipt", oid) == "due"
    sales.tick(rsvc, force=True)
    assert sales.store(rsvc).mail_state("receipt", oid) == "sent" and calls["n"] == 2
    sales.tick(rsvc, force=True)
    assert calls["n"] == 2


def test_an_order_paid_by_hand_before_its_invoice_still_gets_one(seller, keycast):
    rsvc, fake, _v = seller
    with TestClient(create_app(rsvc)) as c:
        fake.down = True
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
        sales.mark_paid(rsvc, oid, "hand", "cash at the door", "Ada")
        assert sales.store(rsvc).order(oid)["state"] == "active"
        fake.down = False
        sales.tick(rsvc, force=True)
    o = sales.store(rsvc).order(oid)
    assert o["invoice"]["number"] and o["step"] == "done"
    assert fake.records["account.move"][o["invoice"]["move_id"]]["payment_state"] == "paid"


# ── 8. return_url ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://belfry.test\\@evil.test/",
        "https://belfry.test /x",
        "https://user@belfry.test/x",
        "https://belfry.test:8443/x",
        "https://belfry.test/x\x00",
        "https://evilbelfry.test/x",
        "https://belfry.test.evil.test/x",
        "https://belfry.test/x?a=1",
        "https://belfry.test/x#f",
        "http://belfry.test/x",
        "https://belfry.test//evil.test/x",
        "javascript:alert(1)",
    ],
)
def test_bad_return_urls(shop, url):
    assert sales._own_url(shop[0], url) is None


def test_good_return_urls_are_rebuilt(shop):
    rsvc = shop[0]
    assert sales._own_url(rsvc, "https://Belfry.Test/tickets/thanks") == (
        "https://belfry.test/tickets/thanks"
    )
    assert sales._own_url(rsvc, "https://www.belfry.test") == "https://www.belfry.test/"


# ── 9. the tick's invoice reads ─────────────────────────────────────────────────────────────
def test_the_tick_reads_one_by_one_when_the_batch_fails(seller, keycast):
    rsvc, fake, _v = seller
    with TestClient(create_app(rsvc)) as c:
        a = buy(c, PrivateKey(), pay="invoice").json()["order"]
        b = buy(c, PrivateKey(), pay="invoice", email="b@example.org").json()["order"]
    st = sales.store(rsvc)
    ma, mb = st.order(a)["invoice"]["move_id"], st.order(b)["invoice"]["move_id"]
    fake.records["account.move"][mb]["payment_state"] = "paid"
    fake.batch_fails = True
    fake.unreadable = {ma}
    out = salesodoo.read_open_invoices(rsvc)
    assert out["paid"] == 1 and out["unread"] == 1
    assert st.order(b)["state"] == "active"
    assert "odoo_read_failed" in st.order(a)["flags"]


# ── 10. Odoo belfries never touch the new field ─────────────────────────────────────────────
def test_odoo_belfries_never_read_the_pending_address(services):
    assert isinstance(services.odoo, FakeOdoo)

    def boom(*a, **k):
        raise AssertionError("read")

    services.odoo.read_one = boom
    assert sales.is_unverified(services, 1) is False
    assert sales.unverified_key(services, "a" * 64) is False


# ── the small ones ──────────────────────────────────────────────────────────────────────────
def test_a_non_ascii_signature_is_just_unsigned(seller):
    rsvc = seller[0]
    with TestClient(create_app(rsvc)) as c:
        r = c.get("/pay?amount=5&description=Hi&sig=%C3%A9%C3%A9", headers=H)
        assert r.status_code == 200 and "Hi" not in r.text.split("<h1>")[1]


def test_activation_respects_a_closed_pass_and_reuses_its_pass(shop, keycast, monkeypatch):
    rsvc = shop[0]
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), code=free_code(rsvc)).json()["order"]
    st = sales.store(rsvc)
    o = st.order(oid)
    # a crash after the pass was made but before the order knew: the same pass
    st.update(oid, pass_id=None, state="paid", step="activate")
    sales.advance(rsvc, oid)
    assert st.order(oid)["pass_id"] == o["pass_id"]
    # a pass closed by someone: the order doesn't say active
    core.revoke_pass(rsvc, o["pass_id"], "test")
    st.update(oid, pass_id=None, state="paid", step="activate")
    monkeypatch.setattr(core, "activate_free_pass", lambda svc, pid: "already_revoked")
    sales.advance(rsvc, oid)
    o = st.order(oid)
    assert o["state"] == "paid" and "pass_closed" in o["flags"] and o["step"] == "stuck"


def test_moving_a_pass_keeps_its_tokens(shop, keycast):
    rsvc = shop[0]
    _bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), code=free_code(rsvc), email="bob@example.org").json()["order"]
        o = sales.store(rsvc).order(oid)
        rsvc.odoo.write("belfry.pass", [o["pass_id"]], {"cashu_amount": 50, "cashu_token": "t"})
        c.post(last_link(), headers=H, data={"answer": "yes"})
    o = sales.store(rsvc).order(oid)
    p = rsvc.odoo.read_one("belfry.pass", o["pass_id"], ["cashu_amount", "partner_id"])
    assert p["partner_id"][0] == bob_id and p["cashu_amount"] == 50


def test_a_stripe_event_left_half_done_is_handled_again(stripe, keycast):
    (rsvc, _f, _v), _m, _s, _e = stripe
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="card").json()["order"]
        sales.store(rsvc).first_event("evt_1")  # received, then a crash
        body_ = json.dumps(event(oid, "INV/2026/0001", 20000)).encode()
        r = c.post(
            "/hooks/stripe",
            content=body_,
            headers={**H, "Stripe-Signature": sign_header(WHSEC, body_)},
        )
        assert r.json()["result"] == "paid"


def test_products_count_places_fresh(shop):
    rsvc = shop[0]
    with TestClient(create_app(rsvc)) as c:
        get = lambda: next(  # noqa: E731
            p
            for p in c.get("/api/v1/products", headers=H).json()["products"]
            if p["id"] == "village-2027"
        )
        assert get()["left"] == 3
        buy(c, PrivateKey(), code=free_code(rsvc))
        assert get()["left"] == 2


def test_the_mailer_masks_addresses(caplog):
    from belfry_onboarding.settings import Settings

    caplog.set_level(logging.WARNING)
    s = Settings(
        HOOK_TOKEN="h",
        AUTHENTIK_TOKEN="t",
        KEYCAST_ADMIN_TOKEN="t",
        KEYCAST_SERVICE_TOKEN="t",
        _env_file=None,
    )
    mailer.Mailer(s).send("someone.private@example.org", "Hi", ["x"])
    assert "someone.private" not in caplog.text and "s•••@example.org" in caplog.text


def test_pay_link_signatures_still_work(seller):
    rsvc = seller[0]
    link = salespay.make_link(rsvc, "5", "", "Locker")
    with TestClient(create_app(rsvc)) as c:
        assert "Locker" in c.get(link.split("belfry.belfry.test", 1)[1], headers=H).text


def test_a_transfer_seen_as_the_hold_ends_keeps_its_invoice(seller, keycast):
    rsvc, fake, _v = seller
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
    fake.records["account.move"][1]["payment_state"] = "paid"
    sales.store(rsvc).update(oid, hold_until=sales.iso(sales.now() - dt.timedelta(minutes=1)))
    sales.tick(rsvc, force=True)  # expires it (not credited: paid), then reads it: reopens
    o = sales.store(rsvc).order(oid)
    assert o["state"] == "active" and o["invoice"]["move_id"] == 1 and not o.get("history")
    refunds = [m for m in fake.records["account.move"].values() if m["move_type"] == "out_refund"]
    assert refunds == [] and "paid_late" in o["flags"]
