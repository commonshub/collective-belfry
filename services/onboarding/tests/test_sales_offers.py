"""Offers in kind are public requests (Xavier, 2026-11-17): a thread from outside in the
belfry's requests channel (webthreads.py), confirmed by its author's mailed link, with buttons
for the admins (belfry:offer-decide); the decision is a public reply, the code a private mail."""

# ruff: noqa: F811
from __future__ import annotations

import re

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import actions, apps, followers, mail, mailer, sales, webthreads
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_webthreads import GK, FakeRelay

SITE = {
    "channel": "requests",
    "list": "public",
    "name": "Requests",
    "about": "",
    "periods": [(0, None)],
    "posts": "",
}


@pytest.fixture
def offers(shop, monkeypatch):
    rsvc, _p, _k, ada, _ada_id, _s = shop
    rsvc.settings.site_url = "https://belfry.test"
    relay = FakeRelay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, relay.sign))
    monkeypatch.setattr(webthreads, "_groups_pubkey", lambda svc: GK)
    monkeypatch.setattr(webthreads, "web_identity", lambda *a: None)  # the card path
    monkeypatch.setattr(webthreads, "sites", lambda svc, fresh=False: {"requests": dict(SITE)})
    monkeypatch.setattr(actions, "_name", lambda svc, pk: "Ada")
    mail._counts.clear()
    mailer.SENT.clear()
    return rsvc, relay, ada


def body(m) -> str:
    return m.get_body(("plain",)).get_content()


ASK = {
    "product": "village-2027",
    "name": "Kim Cook",
    "email": "kim@example.org",
    "offer": "I cook for 40 people on two evenings.",
}


def press(rsvc, message, button, presser):
    p = actions.Press(
        rsvc,
        {
            "id": "p" * 64,
            "pubkey": presser,
            "button": button,
            "message": message["id"],
            "group": "requests",
            "created_at": 0,
        },
        message,
        next(b for b in actions.parse_buttons(message["tags"]) if b["id"] == button),
    )
    import asyncio

    return asyncio.run(sales.offer_decide_press(p))


def test_no_requests_channel_no_offers(shop):
    rsvc = shop[0]
    with TestClient(create_app(rsvc)) as c:
        r = call(c, PrivateKey(), "/offers", ASK)
        assert r.status_code == 409 and r.json()["error"] == "no_requests_channel"
        got = c.get("/api/v1/products", headers={"Host": "belfry.belfry.test"}).json()
        assert got["offers"] is False


def test_an_offer_is_public_once_its_author_confirms(offers, monkeypatch):
    rsvc, relay, ada = offers
    sales.save_settings(rsvc, {"requests_channel": "requests"})
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        assert c.get("/api/v1/products", headers={"Host": "belfry.belfry.test"}).json()["offers"]
        r = call(c, me, "/offers", {**ASK, "percent": 20})
        assert r.status_code == 422
        r = call(c, me, "/offers", ASK)  # 50% unless asked otherwise
        assert r.status_code == 201 and r.json()["state"] == "confirm"
        fid = r.json()["offer"]
        assert not [e for e in relay.events if e["kind"] == 9]  # nothing public yet
        text = body(mailer.SENT[-1])
        assert "will be public" in text and "never is" in text
        link = re.search(r"https://belfry\.test(/requests/confirm/\S+)", text).group(1)
        r = c.get(link, headers={"Host": "belfry.test"}, follow_redirects=False)
        assert r.status_code == 303
        card = next(e for e in relay.events if e["kind"] == 9)
        tags = card["tags"]
        assert ["offer", fid] in tags and ["product", "village-2027"] in tags
        assert ["percent", "50"] in tags and ["p", pk(me)] in tags
        assert ["subject", "Kim Cook: 50% off Two-week pass in kind"] in tags
        assert [b["id"] for b in actions.parse_buttons(tags)] == [
            "offer-50",
            "offer-75",
            "offer-100",
            "offer-no",
        ]
        assert "kim@example.org" not in card["content"]
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["state"] == "open" and got["url"] == f"https://belfry.test/requests/{card['id']}"
        assert got["my_url"].startswith(got["url"] + "?k=")
        # nobody else sees it, let alone the author's link
        assert call(c, PrivateKey(), f"/offers/{fid}", method="GET").status_code == 404
        # the decision: admins only, on the offer's own card
        monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {pk(ada)})
        res = press(rsvc, card, "offer-75", pk(me))
        assert res.status == "error" and res.code == "not_allowed"
        forged = {**card, "id": "9" * 64}
        assert press(rsvc, forged, "offer-75", pk(ada)).code == "no_target"
        mailer.SENT.clear()
        res = press(rsvc, card, "offer-75", pk(ada))
        assert res.status == "ok" and res.closed and res.text == "Approved by Ada: 75% off."
        reply = [e for e in relay.events if e["kind"] == 1111][-1]
        assert (
            reply["content"] == "Approved: 75% off." and ["e", card["id"], "", GK] in reply["tags"]
        )
        assert "-" not in reply["content"]  # no code in public
        res = press(rsvc, card, "offer-no", pk(ada))
        assert res.status == "error" and res.code == "gone"
        # the code: only in the private mail, only for the requester
        code = re.search(r"code: ([A-Z0-9-]+)", body(mailer.SENT[-1])).group(1)
        assert code not in reply["content"]
        r = call(c, PrivateKey(), "/codes/check", {"product": "village-2027", "code": code})
        assert r.status_code == 404
        r = call(c, me, "/codes/check", {"product": "village-2027", "code": code})
        assert r.json() == {"percent": 75, "left": 1}
        # or an account proven with the requester's address
        kim, _ = person(rsvc, "kim@example.org", "Kim")
        r = call(c, kim, "/codes/check", {"product": "village-2027", "code": code})
        assert r.status_code == 200


def test_the_console_decides_and_links_to_the_thread(offers, world, monkeypatch):
    rsvc, relay, ada = offers
    rsvc.settings.pass_page_oidc_client_secret = "x"
    rsvc.settings.pass_page_secret = "p" * 32
    from .test_sales_console import CSRF, signed_in

    me = PrivateKey()
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, world[4])
        h = {"Host": "belfry.belfry.test"}
        page = c.get("/sales", headers=h).text
        assert "Requests (/requests)" in page
        c.post("/sales/requests", data={"csrf": CSRF, "channel": "requests"}, headers=h)
        assert sales.settings(rsvc)["requests_channel"] == "requests"
        fid = call(c, me, "/offers", ASK).json()["offer"]
        link = re.search(
            r"https://belfry\.test(/requests/confirm/\S+)", body(mailer.SENT[-1])
        ).group(1)
        c.get(link, headers={"Host": "belfry.test"}, follow_redirects=False)
        card = next(e for e in relay.events if e["kind"] == 9)
        page = c.get("/sales", headers=h).text
        assert "I cook for 40 people" in page
        assert f"https://chat.belfry.test/#/c/requests/thread/{card['id']}" in page
        c.post(
            "/sales/offer",
            data={"csrf": CSRF, "id": fid, "percent": "0", "note": "Full."},
            headers=h,
        )
    assert sales.store(rsvc).offer(fid)["state"] == "declined"
    reply = [e for e in relay.events if e["kind"] == 1111][-1]
    assert reply["content"] == "Not this time. Full."
