"""The website preview may call the sales API (and nothing else), and `erase_after_event` on
POST /checkout and POST /offers (docs/sales.md §4)."""

# ruff: noqa: F811
from __future__ import annotations

import json

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import sales
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import nip98
from .test_roles import API, _tick, call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import buy, free_code, shop  # noqa: F401 — fixtures
from .test_sales_offers import ASK, offers  # noqa: F401 — fixtures

PREVIEW = "https://preview.belfry.test"
HOST = {"Host": "belfry.belfry.test"}


def signed(c, key, path, data=None, method="POST", origin=PREVIEW):
    import time

    at = int(time.time()) - 50 + next(_tick) % 50
    body = b"" if method == "GET" else json.dumps(data or {}).encode()
    headers = {
        **HOST,
        "Origin": origin,
        "Authorization": nip98(key, API + path, method, body, created_at=at),
    }
    if method == "GET":
        return c.get("/api/v1" + path, headers=headers)
    headers["Content-Type"] = "application/json"
    return c.post("/api/v1" + path, content=body, headers=headers)


def preflight(c, path, method="POST", origin=PREVIEW):
    return c.options(
        "/api/v1" + path,
        headers={
            **HOST,
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )


def test_the_preview_may_call_the_sales_api_without_credentials(shop):
    rsvc = shop[0]
    with TestClient(create_app(rsvc)) as c:
        r = c.get("/api/v1/products", headers={**HOST, "Origin": PREVIEW})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == PREVIEW
        assert "access-control-allow-credentials" not in r.headers
        for path in (
            "/products",
            "/codes/check",
            "/checkout",
            "/checkout/o_abcdefghijklmnop",
            "/checkout/o_abcdefghijklmnop/pay",
            "/billing",
            "/offers",
            "/offers/f_abcdefghijklmnop",
        ):
            method = "GET" if path in ("/products", "/billing") else "POST"
            r = preflight(c, path, method)
            assert r.status_code == 204, path
            assert r.headers["access-control-allow-origin"] == PREVIEW
            assert "Authorization" in r.headers["access-control-allow-headers"]
            assert "POST" in r.headers["access-control-allow-methods"]
            assert "access-control-allow-credentials" not in r.headers
        # the answers themselves, errors too (so the page can read why)
        me = PrivateKey()
        code = free_code(rsvc)
        r = signed(
            c,
            me,
            "/checkout",
            {"product": "village-2027", "name": "Ada", "email": "ada@example.org", "code": code},
        )
        assert r.status_code == 201, r.text
        assert r.headers["access-control-allow-origin"] == PREVIEW
        assert "access-control-allow-credentials" not in r.headers
        oid = r.json()["order"]
        r = signed(c, me, f"/checkout/{oid}", method="GET")
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == PREVIEW
        r = signed(c, me, "/codes/check", {"product": "village-2027", "code": "NOPE-NOPE"})
        assert r.status_code == 404 and r.headers["access-control-allow-origin"] == PREVIEW
        r = signed(c, me, "/offers", ASK)
        assert r.headers["access-control-allow-origin"] == PREVIEW


def test_the_preview_gets_nothing_else(shop):
    rsvc = shop[0]
    with TestClient(create_app(rsvc)) as c:
        for path, method in (
            ("/roles", "GET"),
            ("/roles", "POST"),
            ("/me/follows", "GET"),
            ("/codes", "POST"),  # admins make codes: not from a preview
            ("/offers/f_abcdefghijklmnop/decide", "POST"),
            ("/checkout/o_abcdefghijklmnop/pay/x", "POST"),
            ("/channel-invites", "POST"),
            ("/admins", "POST"),
        ):
            assert preflight(c, path, method).status_code == 403, path
        r = c.get("/api/v1/roles", headers={**HOST, "Origin": PREVIEW})
        assert "access-control-allow-origin" not in r.headers
        r = c.get("/api/me/follows", headers={"Origin": PREVIEW})
        assert "access-control-allow-origin" not in r.headers
        # another host's preview, or plain http, is nobody
        for other in (
            "https://preview.evil.test",
            "http://preview.belfry.test",
            "https://x.preview.belfry.test",
        ):
            r = c.get("/api/v1/products", headers={**HOST, "Origin": other})
            assert "access-control-allow-origin" not in r.headers, other
        # the apex keeps what it had (every /api path)
        r = preflight(c, "/roles", "GET", origin="https://belfry.test")
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "https://belfry.test"


def test_erase_after_event_is_checked_kept_and_copied_at_activation(shop, keycast):
    rsvc = shop[0]
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        for bad in ("yes", 1, None, "true", [], {}):
            r = buy(c, me, code=free_code(rsvc), erase_after_event=bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        assert sales.store(rsvc).orders() == []
        r = buy(c, me, code=free_code(rsvc), erase_after_event=True)
        assert r.status_code == 201, r.text
        got = r.json()
        assert got["state"] == "active" and got["erase_after_event"] is True
        oid = got["order"]
        assert call(c, me, f"/checkout/{oid}", method="GET").json()["erase_after_event"] is True
        # unknown fields are ignored, as before
        r = buy(c, PrivateKey(), code=free_code(rsvc), email="b@example.org", colour="blue")
        assert r.status_code == 201 and r.json()["erase_after_event"] is False
    o = sales.store(rsvc).order(oid)
    row = rsvc.odoo.read_one(
        "res.partner",
        o["partner_id"],
        [sales.ERASE, sales.ERASE + "_order", sales.ERASE + "_at"],
    )
    assert row[sales.ERASE] is True and row[sales.ERASE + "_order"] == oid
    assert row[sales.ERASE + "_at"] == o["erase_after_event_at"]
    # a later order without it never clears it; an admin does
    sales.mark_erase(rsvc, o["partner_id"], {**o, "id": "o_other", "erase_after_event": False})
    assert sales.erase_of(rsvc, o["partner_id"])
    sales.clear_erase(rsvc, o["partner_id"], "Ada Admin", "asked to keep the account")
    assert not sales.erase_of(rsvc, o["partner_id"])


def test_a_retry_may_add_the_request_but_never_takes_it_back(shop, keycast):
    rsvc = shop[0]
    me = PrivateKey()
    code = free_code(rsvc)
    with TestClient(create_app(rsvc)) as c:
        oid = buy(c, me, code=code).json()["order"]
        assert not sales.erase_of(rsvc, sales.store(rsvc).order(oid)["partner_id"])
        r = buy(c, me, code=code, erase_after_event=True)
        assert r.status_code == 200 and r.json()["order"] == oid
        assert r.json()["erase_after_event"] is True
        assert sales.erase_of(rsvc, sales.store(rsvc).order(oid)["partner_id"])
        r = buy(c, me, code=code, erase_after_event=False)
        assert r.json()["erase_after_event"] is True


def test_an_offer_keeps_the_request_and_its_code_carries_it(offers, keycast):
    rsvc, _relay, _ada = offers
    sales.save_settings(rsvc, {"requests_channel": "requests"})
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        r = call(c, me, "/offers", {**ASK, "erase_after_event": "please"})
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        r = call(c, me, "/offers", {**ASK, "percent": 100, "erase_after_event": True})
        assert r.status_code == 201 and r.json()["erase_after_event"] is True
        fid = r.json()["offer"]
        f = sales.store(rsvc).offer(fid)
        assert f["erase_after_event"] is True
        sales.store(rsvc).put_offer({**f, "state": "open"})  # as if its link was pressed
        code = sales.decide_offer(rsvc, fid, 100, "", "Ada")["code"]
        # the checkout leaves it out: the offer asked it, so the order has it
        r = buy(c, me, code=code, email=ASK["email"])
        assert r.status_code == 201, r.text
        assert r.json()["erase_after_event"] is True
        o = sales.store(rsvc).order(r.json()["order"])
        assert sales.erase_of(rsvc, o["partner_id"])
