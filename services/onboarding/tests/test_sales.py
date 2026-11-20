"""Selling passes (sales.py, docs/sales.md): products and prices, codes, free checkouts, the
browser key's account and its unproven address."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import apps, connect, mail, mailer, sales
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures


def day(n: int = 0) -> str:
    return (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=n)).isoformat()


@pytest.fixture
def shop(world, tmp_path):
    rsvc = world[0]
    rsvc.settings.register_path = str(tmp_path / "register.sqlite")
    sales.CODE_CHECKS.reset()
    sales.save_product(
        rsvc,
        {
            "id": "village-2027",
            "name": "Two-week pass",
            "tier": "participant",
            "valid_from": "2027-06-01",
            "valid_until": "2027-06-14",
            "prices": [{"until": day(10), "amount": 200}, {"amount": 300}],
            "vat_rate": 21,
            "quantity": 3,
        },
    )
    sales.save_product(
        rsvc, {"id": "friend", "name": "Friend", "tier": "member", "prices": [{"amount": 0}]}
    )
    return world


def free_code(rsvc, product="village-2027", uses=1, percent=100) -> str:
    return sales.make_codes(rsvc, percent, product, uses)[0]["code"]


def buy(c, key, product="village-2027", email="ada@example.org", **more):
    body = {"product": product, "name": "Ada Lovelace", "email": email, **more}
    return call(c, key, "/checkout", body)


def test_products_show_the_price_of_today_and_the_next(shop):
    rsvc = shop[0]
    with TestClient(create_app(rsvc)) as c:
        r = c.get("/api/v1/products", headers={"Host": "belfry.belfry.test"})
        assert r.status_code == 200
        got = r.json()
        assert got["currency"] == "EUR" and got["can_sell"] is False
        p = next(x for x in got["products"] if x["id"] == "village-2027")
        assert p["price"] == {"amount": 200.0, "vat_rate": 21.0, "until": day(10)}
        assert p["next_price"] == {"amount": 300.0, "from": day(11)}
        assert p["left"] == 3 and p["valid_until"] == "2027-06-14"
    # a price whose date has passed no longer applies
    pr, nxt = sales.price_now(
        sales.product(rsvc, "village-2027"), dt.date.today() + dt.timedelta(days=11)
    )
    assert pr["amount"] == 30000 and nxt is None


def test_vat_split_for_people_and_organisations():
    assert sales.split(20000, 21, 0, "person") == {
        "net": 16529,
        "vat": 3471,
        "gross": 20000,
        "percent_off": 0,
    }
    assert sales.split(20000, 21, 50, "organisation") == {
        "net": 10000,
        "vat": 2100,
        "gross": 12100,
        "percent_off": 50,
    }
    assert sales.split(20000, 21, 100, "person")["gross"] == 0


def test_bad_products_are_refused():
    for bad in (
        {"id": "x y", "name": "a", "prices": [{"amount": 1}]},
        {"id": "a", "name": "", "prices": [{"amount": 1}]},
        {"id": "a", "name": "a", "prices": []},
        {"id": "a", "name": "a", "prices": [{"amount": "1.234"}]},
        {"id": "a", "name": "a", "prices": [{"amount": 1}, {"amount": 2}]},
        {"id": "a", "name": "a", "tier": "guest", "prices": [{"amount": 1}]},
    ):
        with pytest.raises(ValueError):
            sales.clean_product(bad)


def test_paid_product_without_a_way_to_pay_cannot_sell(shop):
    rsvc = shop[0]
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, PrivateKey())
        assert r.status_code == 409 and r.json()["error"] == "cannot_sell"
        assert sales.store(rsvc).orders() == []


def test_codes_check_and_rate_limit(shop):
    rsvc, _p, _k, ada, _a, _s = shop
    code = free_code(rsvc, uses=2, percent=50)
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        r = call(c, me, "/codes/check", {"product": "village-2027", "code": code.lower()})
        assert r.status_code == 200 and r.json() == {"percent": 50, "left": 2}
        r = call(c, me, "/codes/check", {"product": "friend", "code": code})
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        for _ in range(8):
            call(c, me, "/codes/check", {"product": "village-2027", "code": "NOPE-NOPE"})
        r = call(c, me, "/codes/check", {"product": "village-2027", "code": code})
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        # admins make codes over the API
        r = call(c, PrivateKey(), "/codes", {"percent": 20})
        assert r.status_code == 403
        r = call(c, ada, "/codes", {"percent": 20, "count": 3, "uses": 5, "note": "sponsor"})
        assert r.status_code == 201 and len(r.json()["codes"]) == 3
        assert r.json()["codes"][0]["left"] == 5


def test_a_free_pass_with_a_browser_key_end_to_end(shop, keycast):
    rsvc = shop[0]
    code = free_code(rsvc)
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, me, code=code)
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["state"] == "active" and got["total"]["gross"] == 0.0
        assert got["total"]["percent_off"] == 100 and got["invoice"] is None
        assert got["bunker_url"].startswith("bunker://")  # the page that paid is signed in
        oid = got["order"]
        again = call(c, me, f"/checkout/{oid}", method="GET").json()
        assert again["state"] == "active" and again["bunker_url"].startswith("bunker://")
        # the session is bound to the browser key
        assert keycast.bunkers[-1]["client_pubkey"] == pk(me)
        assert keycast.bunkers[-1]["app_name"] == "Tickets"
        # someone else's key sees nothing
        r = call(c, PrivateKey(), f"/checkout/{oid}", method="GET")
        assert r.status_code == 404
        # the code is used up
        r = buy(c, PrivateKey(), code=code, email="bob@example.org")
        assert r.status_code == 409 and r.json()["error"] == "code_used"
        # asking again with the same key and address: the same order
        r = buy(c, me, code=code)
        assert r.status_code == 200 and r.json()["order"] == oid
    o = sales.store(rsvc).order(oid)
    reg = rsvc.odoo
    p = reg.read_one("belfry.pass", o["pass_id"], ["state", "tier", "date_start", "date_end"])
    assert p["state"] == "active" and p["tier"] == "participant"
    assert (p["date_start"], p["date_end"]) == ("2027-06-01", "2027-06-14")
    partner = reg.read_one("res.partner", o["partner_id"], ["email", "belfry_pending_email"])
    assert not partner["email"] and partner["belfry_pending_email"] == "ada@example.org"
    # nobody finds it by its address
    apps.forget()
    assert apps.member_by_email(rsvc, "ada@example.org") is None
    assert reg.search_read("res.partner", [["email", "=ilike", "ada@example.org"]], ["id"]) == []
    # the receipt asks to confirm the address; the default username stays the member number
    text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
    assert "confirm" in text.lower() and "/sales/confirm/" in text
    assert not any(u["username"] == "ada-lovelace" for u in keycast.users.values())
    # the quantity counts it
    assert sales.left(rsvc, sales.product(rsvc, "village-2027")) == 2


def test_an_unproven_buyer_cannot_invite_name_itself_or_receive_mail(shop, keycast):
    rsvc = shop[0]
    code = free_code(rsvc)
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, me, code=code).json()["order"]
        o = sales.store(rsvc).order(oid)
        account = rsvc.odoo.read_one("belfry.pass", o["pass_id"], ["keycast_pubkey"])[
            "keycast_pubkey"
        ]
        # the account's key (the signer holds it); stand in with our own key for the API
        stand_in = PrivateKey()
        rsvc.odoo.write("belfry.pass", [o["pass_id"]], {"keycast_pubkey": pk(stand_in)})
        assert sales.unverified_key(rsvc, pk(stand_in))
        assert not mail.active_pubkey(rsvc, pk(stand_in))
        r = call(c, stand_in, "/channel-invites", {"channel": "general"})
        assert r.status_code == 403 and r.json()["error"] == "unverified"
        r = call(c, stand_in, "/username", {"username": "ada"})
        assert r.status_code == 403 and r.json()["error"] == "unverified"
        with pytest.raises(Exception, match="Confirm your e-mail"):
            rsvc.odoo.create_guest_invite(o["partner_id"], pk(stand_in), "general", 30)
        assert rsvc.odoo.agents_limit(o["partner_id"]) == 0
        rsvc.odoo.write("belfry.pass", [o["pass_id"]], {"keycast_pubkey": account})


def test_the_receipt_link_confirms_the_address(shop, keycast, monkeypatch):
    rsvc = shop[0]
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: False)
    monkeypatch.setattr(mail, "channel_by_slug", lambda svc, ch: None)
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, me, code=free_code(rsvc)).json()["order"]
        text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        link = re.search(r"https://belfry\.belfry\.test(/sales/confirm/\S+)", text).group(1)
        h = {"Host": "belfry.belfry.test"}
        page = c.get(link, headers=h)
        assert (
            page.status_code == 200
            and "Yes, this was me" in page.text
            and "t me</button>" in page.text
        )
        # opening the link changes nothing (mail scanners open links)
        o = sales.store(rsvc).order(oid)
        assert sales.is_unverified(rsvc, o["partner_id"])
        done = c.post(link, headers=h)
        assert done.status_code == 200 and "your address is confirmed" in done.text
        assert not sales.is_unverified(rsvc, o["partner_id"])
        assert c.post(link, headers=h).status_code == 200  # again: nothing more
        assert c.get("/sales/confirm/" + "x" * 30, headers=h).status_code == 404
    apps.forget()
    m = apps.member_by_email(rsvc, "ada@example.org")
    assert m and m["partner"] == o["partner_id"]
    # a username of its own now: the name
    assert any(u["username"] == "ada-lovelace" for u in keycast.users.values())


def test_an_address_of_an_existing_account_moves_the_pass_there(shop, keycast):
    rsvc = shop[0]
    _bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    me = PrivateKey()
    mailer.SENT.clear()
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, me, code=free_code(rsvc), email="BOB@example.org")
        # the checkout doesn't say the address is a member's
        assert r.status_code == 201 and r.json()["state"] == "active"
        oid = r.json()["order"]
        temp = sales.store(rsvc).order(oid)["partner_id"]
        assert temp != bob_id
        text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
        link = re.search(r"https://belfry\.belfry\.test(/sales/confirm/\S+)", text).group(1)
        done = c.post(link, headers={"Host": "belfry.belfry.test"})
        assert "your pass is on that account now" in done.text
    o = sales.store(rsvc).order(oid)
    assert o["partner_id"] == bob_id and o["moved_from"] == temp
    reg = rsvc.odoo
    new = reg.read_one("belfry.pass", o["pass_id"], ["partner_id", "state", "tier", "date_end"])
    assert new["state"] == "active" and new["tier"] == "participant"
    assert new["partner_id"][0] == bob_id and new["date_end"] == "2027-06-14"
    old = reg.search_read("belfry.pass", [["partner_id", "=", temp]], ["state"])
    assert [p["state"] for p in old] == ["revoked"]


def test_the_first_code_sign_in_proves_the_address(shop, keycast):
    rsvc = shop[0]
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, me, code=free_code(rsvc)).json()["order"]
    partner = sales.store(rsvc).order(oid)["partner_id"]
    f = {"session": pk(PrivateKey())}
    mailer.SENT.clear()
    sent = connect.send_code(rsvc, f, "ada@example.org")
    assert sent["to"] == "ada@example.org"
    text = mailer.SENT[-1].get_body(preferencelist=("plain",)).get_content()
    code = re.search(r"\b(\d{6})\b", text).group(1)
    with pytest.raises(connect.ConnectError):
        connect.verify_code(rsvc, f, "000000" if code != "000000" else "111111")
    member = connect.verify_code(rsvc, f, code)
    assert member["partner"] == partner and member["email"] == "ada@example.org"
    assert not sales.is_unverified(rsvc, partner)


def test_a_signed_in_member_buys_on_their_own_account(shop, keycast):
    rsvc, _p, _k, _ada, _a, _s = shop
    bob, bob_id = person(rsvc, "bob@example.org", "Bob")
    with TestClient(create_app(rsvc)) as c:
        r = buy(c, bob, product="friend", email="bob@example.org")
        assert r.status_code == 201 and r.json()["state"] == "active"
        o = sales.store(rsvc).order(r.json()["order"])
        assert o["partner_id"] == bob_id and not o.get("bunker_url")
        got = call(c, bob, f"/checkout/{o['id']}", method="GET").json()
        assert "bunker_url" not in got
    assert not sales.is_unverified(rsvc, bob_id)
    p = rsvc.odoo.read_one("belfry.pass", o["pass_id"], ["partner_id", "date_end"])
    assert p["partner_id"][0] == bob_id and p["date_end"] == "2099-12-31"


def test_checkout_limits_and_bad_input(shop):
    rsvc = shop[0]
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        assert buy(c, me, product="nope").json()["error"] == "unknown_product"
        assert buy(c, me, email="nope").json()["error"] == "invalid_email"
        r = buy(c, me, code="ZZZZ-ZZZZ-ZZZZ")
        assert r.status_code == 404 and r.json()["error"] == "unknown_code"
        r = buy(c, me, product="friend", billing={"kind": "organisation", "name": "X"})
        assert r.json()["error"] == "invalid_billing"
        r = buy(c, me, product="friend", billing={"kind": "person", "vat": "!!"})
        assert r.json()["error"] == "invalid_vat"
        sales.save_product(rsvc, {**sales.product(rsvc, "friend"), "on_sale": False})
        assert buy(c, me, product="friend").json()["error"] == "not_on_sale"
        # sold out: three places, three free orders
        for i in range(3):
            assert (
                buy(c, PrivateKey(), code=free_code(rsvc), email=f"p{i}@example.org").json()[
                    "state"
                ]
                == "active"
            )
        r = buy(c, PrivateKey(), code=free_code(rsvc), email="late@example.org")
        assert r.status_code == 409 and r.json()["error"] == "sold_out"
        # the code of the refused order is still usable
    # return_url only on the belfry's own domain
    assert sales._own_url(rsvc, "https://belfry.test/tickets/thanks")
    assert sales._own_url(rsvc, "https://www.belfry.test/x")
    assert not sales._own_url(rsvc, "https://evil.test/x")
    assert not sales._own_url(rsvc, "http://belfry.test/x")
    assert not sales._own_url(rsvc, "https://belfry.test.evil.test/x")
    assert not sales._own_url(rsvc, "https://belfry.test/x?y=1")


def test_secrets_are_write_only_and_atomic(shop, tmp_path):
    rsvc = shop[0]
    assert sales.secret_state(rsvc, "stripe_key") == {"set": False, "from": "", "last4": ""}
    sales.set_secret(rsvc, "stripe_key", "rk_test_" + "a" * 20 + "WXYZ")
    st = sales.secret_state(rsvc, "stripe_key")
    assert st == {"set": True, "from": "console", "last4": "WXYZ"}
    path = tmp_path / "sales-secrets.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text())["stripe_key"].endswith("WXYZ")
    sales.set_secret(rsvc, "stripe_key", None)
    assert not sales.secret_state(rsvc, "stripe_key")["set"]
    # the environment's value is the fallback
    rsvc.settings.stripe_webhook_secret = "whsec_" + "b" * 30
    assert sales.secret_state(rsvc, "stripe_webhook_secret")["from"] == "environment"
    with pytest.raises(ValueError):
        sales.set_secret(rsvc, "other", "x")
