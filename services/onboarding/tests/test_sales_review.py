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

import httpx
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
        found = re.findall(r"https://belfry\.belfry\.test(" + kind + r"\S+)", body(m))
        if found:
            return found[-1]  # the mail's button (a cancel link may come before it)
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
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
    o = sales.store(rsvc).order(oid)
    assert "invoice_pdf_missing" in o["flags"]
    inv = [m for m in mailer.SENT if str(m["Subject"]).startswith("Invoice")][0]
    assert "Pay by card: https://checkout.stripe.com/" in body(inv)
    assert "How to pay" not in body(inv)


# ── 6. limits per IP and per address; a free product without a code waits for its address ───
def order_body(email, product="village-2027", **more):
    return {"product": product, "name": "A", "email": email, "pay": "invoice", **more}


def test_limits_per_ip_and_per_address(seller):
    rsvc, _f, _v = seller
    sales.save_product(rsvc, {**sales.product(rsvc, "village-2027"), "quantity": 1000})
    with TestClient(create_app(rsvc)) as c:
        # an address holds at most 5 unpaid orders; the refusal says how to cancel them
        for i in range(5):
            r = post_from(
                c, PrivateKey(), "/checkout", order_body("a@example.org"), f"198.51.100.{i}"
            )
            assert r.status_code == 201, r.text
        r = post_from(c, PrivateKey(), "/checkout", order_body("a@example.org"), "198.51.100.9")
        assert r.status_code == 429 and "cancel them" in r.json()["message"]
        # a limited product: ten unpaid holds per IP (an IPv6 /64 counts as one)
        for i in range(10):
            r = post_from(
                c,
                PrivateKey(),
                "/checkout",
                order_body(f"b{i}@example.org"),
                f"2001:db8:1:2::{i + 1}",
            )
            assert r.status_code == 201, r.text
        r = post_from(
            c, PrivateKey(), "/checkout", order_body("b99@example.org"), "2001:db8:1:2:ffff::1"
        )
        assert r.status_code == 429
        # another /64 is someone else
        r = post_from(
            c, PrivateKey(), "/checkout", order_body("b98@example.org"), "2001:db8:1:3::1"
        )
        assert r.status_code == 201
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
            assert post_from(c, me, "/offers", offer, ip="192.0.2.50").status_code == 201
        assert post_from(c, me, "/offers", offer, ip="192.0.2.51").status_code == 429


def test_thirty_orders_an_hour_per_ip_and_refusals_apart(seller):
    rsvc, _f, _v = seller
    sales.save_product(rsvc, {**sales.product(rsvc, "village-2027"), "quantity": None})
    with TestClient(create_app(rsvc)) as c:
        ip = "192.0.2.9"
        for i in range(30):
            r = post_from(c, PrivateKey(), "/checkout", order_body(f"c{i}@example.org"), ip)
            assert r.status_code == 201, (i, r.text)
        r = post_from(c, PrivateKey(), "/checkout", order_body("c99@example.org"), ip)
        assert r.status_code == 429
        # refusals count apart: 60 an hour
        ip2 = "192.0.2.10"
        for _ in range(60):
            r = post_from(
                c, PrivateKey(), "/checkout", order_body("d@example.org", code="NOPE"), ip2
            )
            assert r.status_code in (404, 429)
        r = post_from(c, PrivateKey(), "/checkout", order_body("d@example.org"), ip2)
        assert r.status_code == 429 and "refused" in r.json()["message"]


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
    assert [st.mail_state("offer_answer", f"x{i}") for i in (5, 6)] == ["due", "due"]
    assert st.due_mails() == []  # tried again in an hour


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
        body_ = json.dumps(event(oid, oid, 20000)).encode()
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


# ── the second review ───────────────────────────────────────────────────────────────────────
def test_a_buyers_polls_never_make_a_paid_order_give_up(seller, keycast, monkeypatch):
    rsvc, _f, _v = seller
    with TestClient(create_app(rsvc)) as c:
        me = PrivateKey()
        oid = buy(c, me, pay="invoice").json()["order"]

        def down(svc, o):
            raise OSError("signer down")

        monkeypatch.setattr(sales, "activate", down)
        sales.mark_paid(rsvc, oid, "hand", "x", "Ada")
        st = sales.store(rsvc)
        for _ in range(40):  # a minute of polling, throttled and never counted
            sales._polled.clear()
            call(c, me, f"/checkout/{oid}", method="GET")
        o = st.order(oid)
        assert o["state"] == "paid" and o["step"] == "activate" and not o.get("tries")
        # the tick counts; after 30 a paid order backs off to hourly, flagged slow, never stuck
        for _ in range(31):
            st.update(oid, next_try_at=None)
            sales.retry(rsvc)
        o = st.order(oid)
        assert o["step"] == "activate" and "slow" in o["flags"] and "stuck" not in o["flags"]
        assert o["next_try_at"] > sales.iso(sales.now())
        sales.retry(rsvc)  # backing off: not tried now
        assert st.order(oid)["tries"]["activate"] == o["tries"]["activate"]
        # the signer is back: an admin's Try again opens it and clears the counts
        monkeypatch.undo()
        o = sales.try_again(rsvc, oid)
        assert o["state"] == "active" and "slow" not in o["flags"] and not o.get("tries")


def test_a_stuck_unpaid_step_can_be_tried_again_from_the_console(seller, world, keycast):
    rsvc, fake, _v = seller
    rsvc.settings.pass_page_oidc_client_secret = "x"
    rsvc.settings.pass_page_secret = "p" * 32
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        fake.down = True
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
        for _ in range(30):
            sales.retry(rsvc)
        st = sales.store(rsvc)
        assert st.order(oid)["step"] == "stuck"
        fake.down = False
        from .test_sales_console import CSRF, signed_in

        signed_in(c, rsvc, world[4])
        page = c.get("/sales", headers=H).text
        assert "a step gave up" in page and "Try again" in page
        c.post("/sales/order", data={"csrf": CSRF, "id": oid, "do": "retry"}, headers=H)
    o = st.order(oid)
    assert o["state"] == "invoiced" and "stuck" not in o["flags"] and not o.get("tries")


def test_every_mail_about_an_unpaid_order_can_cancel_it(seller):
    rsvc, _f, _v = seller
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice", email="kim@example.org").json()["order"]
        inv = [m for m in mailer.SENT if str(m["Subject"]).startswith("Invoice")][0]
        link = re.search(r"cancel it here: https://belfry\.belfry\.test(\S+)", body(inv)).group(1)
        page = c.get(link, headers=H).text
        assert "Cancel this order" in page and "Two-week pass" in page
        assert c.post(link, headers=H, data={"answer": "yes"}).status_code == 404
        assert sales.store(rsvc).order(oid)["state"] == "invoiced"
        assert "Cancelled" in c.post(link, headers=H, data={"answer": "cancel"}).text
        o = sales.store(rsvc).order(oid)
        assert o["state"] == "cancelled" and "not_me" in o["flags"]
        assert c.get(link, headers=H).status_code == 404
        # the free order's confirmation mail carries it too
        buy(c, PrivateKey(), product="friend", email="lee@example.org")
        assert "cancel it here:" in body(mailer.SENT[-1])


def test_a_free_order_confirmed_by_link_proves_the_unverified_account(shop, keycast):
    rsvc = shop[0]
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, me, code=free_code(rsvc)).json()["order"]
        o = sales.store(rsvc).order(oid)
        rsvc.odoo.write("belfry.pass", [o["pass_id"]], {"keycast_pubkey": pk(me)})
        r = buy(c, me, product="friend")
        assert r.json()["state"] == "confirm"
        c.post(last_link(), headers=H, data={"answer": "yes"})
        o2 = sales.store(rsvc).order(r.json()["order"])
    assert o2["state"] == "active" and o2["partner_id"] == o["partner_id"]
    assert not sales.is_unverified(rsvc, o["partner_id"])
    receipt = [m for m in mailer.SENT if str(m["Subject"]) == "Your Friend"]
    assert receipt and "confirm" not in body(receipt[-1]).lower()


def test_a_mail_is_claimed_before_it_is_sent(seller):
    rsvc = seller[0]
    st = sales.store(rsvc)
    st.queue_mail("offer_answer", "f_x", "a@example.org")
    m = st.due_mails()[0]
    assert st.claim_mail(m["id"]) is True
    assert st.claim_mail(m["id"]) is False  # the tick finds it taken
    assert st.due_mails() == []
    # a sender that died: reclaimed after 10 minutes
    with st._lock:
        st._db.execute(
            "UPDATE mails SET claimed_at = ? WHERE id = ?", (int(time.time()) - 700, m["id"])
        )
    assert [x["id"] for x in st.due_mails()] == [m["id"]]


def test_an_older_sales_file_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "sales.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        """CREATE TABLE orders (id TEXT PRIMARY KEY, key TEXT NOT NULL, email TEXT NOT NULL,
           state TEXT NOT NULL, created INTEGER NOT NULL, data TEXT NOT NULL);
           CREATE TABLE confirms (token TEXT PRIMARY KEY, order_id TEXT NOT NULL,
           at INTEGER NOT NULL);
           CREATE TABLE mails (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
           ref TEXT NOT NULL, address TEXT NOT NULL, created INTEGER NOT NULL,
           tries INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'due',
           sent_at INTEGER, UNIQUE (kind, ref));"""
    )
    db.close()
    st = sales.Store(str(path))
    assert st.count_ip("x", 0) == 0 and st.open_for("ip", "x") == 0
    tok = st.confirm_token("o_" + "a" * 16, 1, "a@example.org", "receipt")
    assert st.confirm_of(tok)["address"] == "a@example.org"
    st.queue_mail("x", "y", "a@example.org")
    assert st.claim_mail(st.due_mails()[0]["id"])


def test_a_second_round_invoice_that_differs_from_what_was_paid_is_flagged(stripe, keycast):
    (rsvc, fake, _v), _m, _s, _e = stripe
    with TestClient(create_app(rsvc)) as c:
        oid = expired_order(rsvc, c)
        fake.TAXES = {10: (6.0, True), 11: (6.0, False)}  # the rate changed meanwhile
        try:
            body_ = json.dumps(event(oid, "INV/2026/0001", 20000)).encode()
            c.post(
                "/hooks/stripe",
                content=body_,
                headers={**H, "Stripe-Signature": sign_header(WHSEC, body_)},
            )
        finally:
            fake.TAXES = {10: (21.0, True), 11: (21.0, False)}
    o = sales.store(rsvc).order(oid)
    assert o["state"] == "active" and o["total"]["gross"] == 20000
    assert "odoo_total" not in o["flags"]  # incl. VAT: the same 200.00
    sales.store(rsvc).update(oid, total={**o["total"], "gross": 19999})
    sales.store(rsvc).update(oid, invoice={}, step="invoice", invoice_round=3)
    sales.advance(rsvc, oid)
    assert "odoo_total" in sales.store(rsvc).order(oid)["flags"]


def test_a_late_payment_asks_odoo_whether_the_invoice_stands(seller, keycast):
    rsvc, fake, _v = seller
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, PrivateKey(), pay="invoice").json()["order"]
    st = sales.store(rsvc)
    # Odoo made the credit note, but Belfry crashed before writing it down
    sales.cancel(rsvc, oid, "x")
    st.update(oid, invoice={**st.order(oid)["invoice"], "credited": False}, step="done")
    assert fake.records["account.move"][1]["payment_state"] == "reversed"
    # Odoo down: nothing changes, it is tried again
    fake.down = True
    with pytest.raises(httpx.ConnectError):
        sales.mark_paid(rsvc, oid, "hand", "late", "Ada")
    assert st.order(oid)["state"] == "cancelled"
    fake.down = False
    o = sales.mark_paid(rsvc, oid, "hand", "late", "Ada")
    assert o["state"] == "active" and o["invoice"]["move_id"] != 1 and o["history"]


def test_mails_wait_for_a_provider_and_back_off_after_failures(shop, monkeypatch):
    rsvc = shop[0]
    st = sales.store(rsvc)
    st.queue_mail("offer_answer", "f_none", "a@example.org")
    monkeypatch.setattr(
        salesmail,
        "BUILD",
        {**salesmail.BUILD, "offer_answer": lambda svc, r, a: ("S", ["x"], None, [])},
    )
    salesmail.deliver_due(rsvc)  # no provider: kept, hourly
    assert st.mail_state("offer_answer", "f_none") == "due" and st.due_mails() == []
    rsvc.settings.smtp_host = "smtp.test"
    monkeypatch.setattr(
        mailer.Mailer, "_smtp", lambda self, msg, r: (_ for _ in ()).throw(OSError())
    )
    with st._lock:
        st._db.execute("UPDATE mails SET next_at = NULL, tries = 7")
    salesmail.deliver_due(rsvc)  # the 8th failure: hourly from now on, not given up
    assert st.mail_state("offer_answer", "f_none") == "due" and st.due_mails() == []
    with st._lock:
        st._db.execute(
            "UPDATE mails SET next_at = NULL, created = ?", (int(time.time()) - 4 * 86400,)
        )
    salesmail.deliver_due(rsvc)  # after 3 days: given up
    assert st.mail_state("offer_answer", "f_none") == "failed"
