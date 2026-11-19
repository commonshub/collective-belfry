"""The public requests and an event's public activity as JSON (webrequests.py,
docs/requests.md): the list, one thread with its timeline, the author's comment, the activity
log with its consent rule, and a channel's page handed over to the community's website."""

# ruff: noqa: F811
from __future__ import annotations

import json
import re
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import events, mail, mailer, sales, webrequests, webthreads, whois
from belfry_onboarding.app import create_app
from belfry_onboarding.nostr import hex_to_npub as npub_of

from .test_events import GROUP, D
from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import nip98
from .test_roles import API, _tick, call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_offers import ASK, SITE, body, offers  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_testers import TESTER, envs, make_tester  # noqa: F401 — fixtures
from .test_ticket_sales import fair, order, pay_card, tk  # noqa: F401 — fixtures
from .test_webthreads import GK

H = {"Host": "belfry.belfry.test"}
APEX = {"Host": "belfry.test"}
MEMBER = "b" * 64
_ip = iter(range(1, 250))


@pytest.fixture
def reqs(offers, monkeypatch):
    rsvc, relay, ada = offers
    real = whois.kinds
    monkeypatch.setattr(whois, "kinds", lambda svc: {**real(svc), MEMBER: "member"})  # Mia
    sales.save_settings(rsvc, {"requests_channel": "requests"})
    webrequests.forget()
    webrequests.PER_IP.reset()
    relay.events.append(
        {
            "id": "k0",
            "kind": 0,
            "pubkey": MEMBER,
            "created_at": 1,
            "tags": [],
            "content": '{"name":"Mia"}',
        }
    )
    yield rsvc, relay, ada
    webrequests.forget()


def ask(c, relay, key, **more):
    """An offer through the API and its mailed confirmation link: (offer id, the card, the
    thread's own key). Each from another address (the limits count per IP)."""
    at = int(time.time()) - 50 + next(_tick) % 50
    data = json.dumps({**ASK, **more}).encode()
    r = c.post(
        "/api/v1/offers",
        content=data,
        headers={
            **H,
            "Content-Type": "application/json",
            "X-Forwarded-For": f"10.0.0.{next(_ip)}",
            "Authorization": nip98(key, API + "/offers", "POST", data, created_at=at),
        },
    )
    assert r.status_code == 201, r.text
    link = re.search(r"https://belfry\.test(/requests/confirm/\S+)", body(mailer.SENT[-1]))
    r = c.get(link.group(1), headers=APEX, follow_redirects=False)
    assert r.status_code == 303, r.text
    card = [e for e in relay.events if e["kind"] == 9][-1]
    card["created_at"] = 1_790_000_000 + next(_tick)  # one after the other, whatever the clock
    k = re.search(r"[?#]k=([0-9a-f]{24})", r.headers["location"]).group(1)
    webrequests.forget()
    return next(t[1] for t in card["tags"] if t[0] == "offer"), card, k


def plain(relay, n: int, text: str, by: str = MEMBER, at: int | None = None) -> dict:
    e = {
        "id": f"{n:064x}",
        "kind": 9,
        "pubkey": by,
        "created_at": at or 1_700_000_000 + n,
        "tags": [["h", "requests"]],
        "content": text,
    }
    relay.events.append(e)
    webrequests.forget()
    return e


def reply(relay, root: dict, by: str, text: str, n: int) -> dict:
    e = {
        "id": f"{n:064x}",
        "kind": 1111,
        "pubkey": by,
        "created_at": int(time.time()) - 1000 + n,
        "tags": [["h", "requests"], ["E", root["id"], "", root["pubkey"]], ["K", "9"]],
        "content": text,
    }
    relay.events.append(e)
    webrequests.forget()
    return e


def label(relay, root: dict, lab: str, by: str, n: int) -> None:
    relay.events.append(
        {
            "id": f"{n:064x}",
            "kind": 1985,
            "pubkey": by,
            "created_at": int(time.time()) + 1000 + n,
            "tags": [
                ["h", "requests"],
                ["L", "belfry/inbox"],
                ["l", lab, "belfry/inbox"],
                ["e", root["id"]],
            ],
            "content": "",
        }
    )
    webrequests.forget()


def get(c, path, **headers):
    return c.get("/api/v1" + path, headers={**H, **headers})


def clean(*answers, keys=(), addresses=("kim@example.org", "ada@example.org")) -> None:
    """No key (hex or npub) and no e-mail address in what the API said."""
    text = "\n".join(a if isinstance(a, str) else json.dumps(a) for a in answers)
    for k in (GK, MEMBER, *keys):
        assert k not in text
        assert npub_of(k) not in text
    assert "npub1" not in text
    for a in addresses:
        assert a not in text
    assert not re.search(r"[^@\s\"]+@[^@\s\"]+\.[a-z]{2,}", text)


def test_no_requests_channel_no_requests(shop):
    rsvc = shop[0]
    webrequests.forget()
    webrequests.PER_IP.reset()
    with TestClient(create_app(rsvc)) as c:
        for path in ("/requests", "/requests/1", "/requests/" + "a" * 64):
            r = get(c, path)
            assert r.status_code == 404 and r.json()["error"] == "none", path


def test_the_list_filters_counts_searches_and_pages(reqs):
    rsvc, relay, ada = reqs
    me = PrivateKey()
    seen = []
    with TestClient(create_app(rsvc)) as c:
        a = plain(relay, 1, "**Garden tools**\n\nWho has a spade?")
        b = plain(relay, 2, "Crème brûlée for everyone")
        d = plain(relay, 3, "Old question")
        label(relay, d, "closed", MEMBER, 1)
        fid, card, _k = ask(c, relay, me, label="food", days=["2027-01-27", "2027-01-26"])
        fid2, card2, _k2 = ask(c, relay, PrivateKey(), email="lou@example.org", name="Lou")
        sales.decide_offer(rsvc, fid, 75, "Welcome.", "Ada")
        sales.decide_offer(rsvc, fid2, 0, "", "Ada")
        r = get(c, "/requests")
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "public, max-age=15"
        got = r.json()
        seen.append(got)
        assert got["slug"] == "requests" and got["channel"] == "Requests"
        assert got["counts"] == {"open": 2, "approved": 1, "closed": 2}
        assert got["next"] is None
        # newest first; numbers in creation order
        assert [i["id"] for i in got["items"]] == [
            card2["id"],
            card["id"],
            d["id"],
            b["id"],
            a["id"],
        ]
        assert [i["number"] for i in got["items"]] == [5, 4, 3, 2, 1]
        mine = got["items"][1]
        assert mine["title"] == "Kim Cook: 50% off Two-week pass in kind"
        assert mine["kind"] == "in_kind" and mine["label"] == "food"
        assert mine["days"] == ["2027-01-26", "2027-01-27"] and mine["percent_asked"] == 50
        assert mine["state"] == "approved" and mine["percent_approved"] == 75
        assert mine["approved_by"] == {"name": "Ada"}
        assert mine["author"] == {"name": "Kim Cook", "role": "guest"}
        assert mine["comments"] == 0  # the decision is not a comment
        assert mine["last_at"] >= mine["created_at"] == card["created_at"]
        other = got["items"][0]
        assert other["state"] == "declined" and other["percent_approved"] is None
        assert other["approved_by"] is None and other["label"] is None and other["days"] is None
        first = got["items"][-1]
        assert first["title"] == "Garden tools" and first["kind"] == "other"
        assert first["percent_asked"] is None and first["state"] == "open"
        assert first["author"] == {"name": "Mia", "role": "member"}
        # the filters
        for state, ids in (
            ("open", [b["id"], a["id"]]),
            ("approved", [card["id"]]),
            ("closed", [card2["id"], d["id"]]),
            ("all", [card2["id"], card["id"], d["id"], b["id"], a["id"]]),
        ):
            got = get(c, f"/requests?state={state}").json()
            seen.append(got)
            assert [i["id"] for i in got["items"]] == ids, state
            assert got["counts"] == {"open": 2, "approved": 1, "closed": 2}
        assert get(c, "/requests?state=nope").status_code == 422
        # q: the title and the body, whatever the case or the accents
        got = get(c, "/requests?q=CREME%20brulee").json()
        assert [i["id"] for i in got["items"]] == [b["id"]]
        assert got["counts"] == {"open": 1, "approved": 0, "closed": 0}
        assert [i["id"] for i in get(c, "/requests?q=spade").json()["items"]] == [a["id"]]
        assert [i["id"] for i in get(c, "/requests?q=40%20people").json()["items"]] == [
            card2["id"],
            card["id"],
        ]
        assert get(c, "/requests?q=" + "x" * 101).status_code == 422
        # paging
        got = get(c, "/requests?limit=2").json()
        assert [i["id"] for i in got["items"]] == [card2["id"], card["id"]]
        assert got["next"] == card["created_at"]
        got = get(c, f"/requests?limit=2&before={got['next']}").json()
        assert [i["id"] for i in got["items"]] == [d["id"], b["id"]] and got["next"]
        got = get(c, f"/requests?limit=2&before={got['next']}").json()
        assert [i["id"] for i in got["items"]] == [a["id"]] and got["next"] is None
        for bad in ("limit=0", "limit=101", "limit=x", "before=x"):
            assert get(c, "/requests?" + bad).status_code == 422, bad
    clean(*seen, keys=(pk(me), pk(ada)))


def test_numbers_stay_when_older_threads_are_deleted(reqs):
    rsvc, relay, ada = reqs
    with TestClient(create_app(rsvc)) as c:
        a = plain(relay, 1, "One")
        b = plain(relay, 2, "Two")
        got = get(c, "/requests").json()["items"]
        assert {i["id"]: i["number"] for i in got} == {a["id"]: 1, b["id"]: 2}
        relay.events.remove(a)  # deleted on the relay
        webrequests.forget()
        d = plain(relay, 3, "Three")
        got = get(c, "/requests").json()["items"]
        assert {i["id"]: i["number"] for i in got} == {b["id"]: 2, d["id"]: 3}
        relay.events.remove(d)
        e = plain(relay, 4, "Four")
        got = get(c, "/requests").json()["items"]
        assert {i["id"]: i["number"] for i in got} == {b["id"]: 2, e["id"]: 4}
        # by number, by id
        assert get(c, "/requests/4").json()["id"] == e["id"]
        # an admin of the belfry is an organiser; someone with no profile has no name, no key
        boss = plain(relay, 5, "From the team", by=pk(ada))
        got = get(c, "/requests/5").json()
        assert got["id"] == boss["id"] and got["author"] == {
            "name": "Somebody",
            "role": "organiser",
        }
        clean(got, keys=(pk(ada),))
        assert get(c, "/requests/" + b["id"]).json()["number"] == 2
        for gone in ("1", "3", "9", a["id"], "0", "x", "2x"):
            r = get(c, "/requests/" + gone)
            assert r.status_code == 404 and r.json()["error"] == "unknown", gone


def test_a_thread_with_its_timeline_and_its_author(reqs):
    rsvc, relay, ada = reqs
    me = PrivateKey()
    seen = []
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me, label="volunteer")
        fid2, card2, _k2 = ask(c, relay, PrivateKey(), email="lou@example.org", name="Lou")
        reply(relay, card, MEMBER, "Which evenings?", 1)
        # the author answers from the web, with the key of their own link
        r = c.post(
            f"/api/v1/requests/{card['id']}/comments",
            json={"text": "Tuesday and Friday."},
            headers={**H, "X-Thread-Key": k},
        )
        assert r.status_code == 201 and r.json() == {"ok": True}
        sales.decide_offer(rsvc, fid, 100, "See you there.", "Ada")
        sales.decide_offer(rsvc, fid2, 0, "", "Ada")
        label(relay, card, "closed", MEMBER, 1)
        label(relay, card, "closed", MEMBER, 2)  # twice says it once
        label(relay, card, "open", MEMBER, 3)
        r = get(c, f"/requests/{card['id']}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        got = r.json()
        seen.append(got)
        assert got["id"] == card["id"] and got["number"] == 1
        assert got["body"] == "I cook for 40 people on two evenings."
        assert got["state"] == "approved" and got["comments"] == 2
        line = got["timeline"]
        assert [x["type"] for x in line] == ["comment", "comment", "approved", "closed", "reopened"]
        assert [x["at"] for x in line] == sorted(x["at"] for x in line)
        assert line[0]["author"] == {"name": "Mia", "role": "member"}
        assert line[0]["text"] == "Which evenings?"
        assert line[1]["author"] == {"name": "Kim Cook", "role": "author"}
        assert line[1]["text"] == "Tuesday and Friday."
        assert line[2]["by"] == {"name": "Ada"} and line[2]["percent"] == 100
        assert line[2]["note"] == "See you there."
        assert line[3]["by"] == {"name": "Mia"} and line[4]["by"] == {"name": "Mia"}
        assert "me" not in got
        declined = get(c, "/requests/2").json()
        seen.append(declined)
        assert declined["id"] == card2["id"] and declined["state"] == "declined"
        assert [x["type"] for x in declined["timeline"]] == ["declined"]
        assert "percent" not in declined["timeline"][0]
        # me: the key that made the offer (NIP-98), or the thread's key in a header
        got = call(c, me, f"/requests/{card['id']}", method="GET").json()
        assert got["me"] == {"author": True}
        got = get(c, f"/requests/{card['id']}", **{"X-Thread-Key": k}).json()
        assert got["me"] == {"author": True}
        seen.append(got)
        # where the approved author goes on: one checkout page for everything, when set
        sales.save_settings(rsvc, {"checkout_url": "https://belfry.test/tickets?t={id}"})
        assert "ticket_url" not in get(c, "/requests/1", **{"X-Thread-Key": k}).json()["me"]
        sales.save_settings(rsvc, {"checkout_url": "https://belfry.test/tickets"})
        got = get(c, "/requests/1", **{"X-Thread-Key": k}).json()
        assert got["me"] == {"author": True, "ticket_url": "https://belfry.test/tickets"}
        code = sales.store(rsvc).offer(fid)["code"]
        assert code and code not in json.dumps(got)  # the code is only in the private mail
        # a declined author has nowhere to go on to
        got = get(c, "/requests/2", **{"X-Thread-Key": _k2}).json()
        assert got["me"] == {"author": True}
        # anyone else, another thread's key, or the key in the query string: nobody
        assert "me" not in call(c, PrivateKey(), "/requests/1", method="GET").json()
        assert "me" not in call(c, ada, "/requests/1", method="GET").json()
        assert "me" not in get(c, "/requests/1", **{"X-Thread-Key": _k2}).json()
        assert "me" not in get(c, "/requests/1", **{"X-Thread-Key": "0" * 24}).json()
        assert "me" not in get(c, f"/requests/1?k={k}").json()
        assert "me" not in get(c, f"/requests/1?X-Thread-Key={k}").json()
    clean(*seen, keys=(pk(me), pk(ada)), addresses=("kim@example.org", "lou@example.org"))


def test_only_the_author_comments(reqs):
    rsvc, relay, ada = reqs
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me)
        _f2, card2, k2 = ask(c, relay, PrivateKey(), email="lou@example.org", name="Lou")
        path = f"/requests/{card['id']}/comments"
        # with the offer's own key
        r = call(c, me, path, {"text": "One more thing."})
        assert r.status_code == 201, r.text
        posted = [e for e in relay.events if e["kind"] == 1111][-1]
        assert posted["content"] == "One more thing."
        assert ["mail-from", "kim@example.org"] in posted["tags"]
        assert ["E", card["id"], "", GK] in posted["tags"]
        # nobody, another key, an admin, another thread's key
        r = c.post("/api/v1" + path, json={"text": "Hi"}, headers=H)
        assert r.status_code == 403 and r.json()["error"] == "not_author"
        for key in (PrivateKey(), ada):
            r = call(c, key, path, {"text": "Hi"})
            assert r.status_code == 403 and r.json()["error"] == "not_author"
        r = c.post("/api/v1" + path, json={"text": "Hi"}, headers={**H, "X-Thread-Key": k2})
        assert r.status_code == 403
        r = c.post(f"/api/v1{path}?k={k}", json={"text": "Hi"}, headers=H)
        assert r.status_code == 403  # the key in the query string is nothing
        r = c.post("/api/v1" + path, json={"text": "Hi"}, headers={**H, "Authorization": "Nostr x"})
        assert r.status_code == 401
        assert len([e for e in relay.events if e["kind"] == 1111]) == 1
        # what a comment is
        ok = {**H, "X-Thread-Key": k}
        for bad in ({}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "x" * 8001}):
            r = c.post("/api/v1" + path, json=bad, headers=ok)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        r = c.post("/api/v1" + path, content=b"[", headers=ok)
        assert r.status_code == 400
        # only by its id, only a thread there is
        assert (
            c.post("/api/v1/requests/1/comments", json={"text": "Hi"}, headers=ok).status_code
            == 404
        )
        r = c.post(f"/api/v1/requests/{'0' * 64}/comments", json={"text": "Hi"}, headers=ok)
        assert r.status_code == 404 and r.json()["error"] == "unknown"
        # the page's own limit: 20 an hour
        mail._counts.clear()
        for i in range(20):
            r = c.post("/api/v1" + path, json={"text": f"n{i}"}, headers=ok)
            assert r.status_code == 201, i
        r = c.post("/api/v1" + path, json={"text": "again"}, headers=ok)
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        assert get(c, f"/requests/{card['id']}").json()["comments"] == 21


def test_offers_take_a_label_days_and_the_consent(reqs):
    rsvc, relay, _ada = reqs
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        for bad in (
            {"label": "party"},
            {"label": 3},
            {"label": ["food"]},
            {"days": "2027-01-26"},
            {"days": ["26/01/2027"]},
            {"days": ["2027-02-30"]},
            {"days": [20270126]},
            {"days": [f"2027-03-{i:02d}" for i in range(1, 32)] + ["2027-04-01"]},
            {"public_name": "yes"},
            {"public_name": 1},
            {"public_name": None},
        ):
            r = call(c, me, "/offers", {**ASK, **bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        assert not sales.store(rsvc).offers()
        r = call(c, me, "/offers", ASK)
        assert r.status_code == 201
        got = r.json()
        assert got["label"] is None and got["days"] is None and got["public_name"] is False
        fid, card, _k = ask(
            c,
            relay,
            PrivateKey(),
            email="lou@example.org",
            label="workshop",
            days=["2027-01-27", "2027-01-26", "2027-01-27"],
            public_name=True,
        )
        f = sales.store(rsvc).offer(fid)
        assert f["label"] == "workshop" and f["days"] == ["2027-01-26", "2027-01-27"]
        assert f["public_name"] is True
        assert ["offer-label", "workshop"] in card["tags"]
        assert ["offer-days", "2027-01-26", "2027-01-27"] in card["tags"]
        got = call(c, PrivateKey(), f"/offers/{fid}", method="GET")
        assert got.status_code == 404
        # the HTML thread says it too, next to the offer's text
        page = c.get(f"/requests/{card['id']}", headers=APEX).text
        assert "Workshop · 26 Jan 2027, 27 Jan 2027" in page


def test_the_owner_of_an_offer_reads_its_label_and_days(reqs):
    rsvc, relay, _ada = reqs
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fid, _card, _k = ask(c, relay, me, label="food", days=["2027-01-26"], public_name=True)
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["label"] == "food" and got["days"] == ["2027-01-26"]
        assert got["public_name"] is True and got["state"] == "open"


def test_a_page_on_the_website_takes_the_channel_over(reqs, monkeypatch):
    rsvc, relay, _ada = reqs
    page = "https://belfry.test/in-kind"
    monkeypatch.setattr(
        webthreads, "sites", lambda svc, fresh=False: {"requests": {**SITE, "page": page}}
    )
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        # the belfry answers no HTML for the channel any more: the website has the path
        for path in ("/requests", "/requests?state=closed", "/requests/thread", "/requests/x/y"):
            r = c.get(path, headers=APEX)
            assert "wt-head" not in r.text and "Open an issue" not in r.text, path
        # the preview host shows a proposed site's own page there too: no redirect to the apex
        r = c.get("/requests", headers={"host": "preview.belfry.test"}, follow_redirects=False)
        assert r.status_code != 301 and "location" not in r.headers
        r = c.post(
            "/requests",
            data={"title": "T", "text": "x", "name": "N", "email": "n@x.org"},
            headers=APEX,
        )
        assert "Check your e-mail" not in r.text and not mailer.SENT
        # the offers go on, and the channel stays the requests channel
        assert get(c, "/products").json()["offers"] is True
        r = call(c, me, "/offers", ASK)
        assert r.status_code == 201
        fid = r.json()["offer"]
        text = body(mailer.SENT[-1])
        assert f"will be public on {page}," in text
        # the mailed confirmation link is the belfry's still, and lands on the website's page
        link = re.search(r"https://belfry\.test(/requests/confirm/\S+)", text).group(1)
        r = c.get(link, headers=APEX, follow_redirects=False)
        assert r.status_code == 303
        card = next(e for e in relay.events if e["kind"] == 9)
        there = r.headers["location"]
        k = there.rsplit("#k=", 1)[1]
        # the key rides in the fragment: never in a query string
        assert there == f"{page}/thread?id={card['id']}#k={k}" and re.fullmatch("[0-9a-f]{24}", k)
        assert there in body(mailer.SENT[-1])
        assert "?k=" not in body(mailer.SENT[-1])
        again = c.get(link, headers=APEX, follow_redirects=False)
        assert again.status_code == 303 and again.headers["location"] == there
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["url"] == f"{page}/thread?id={card['id']}" and got["my_url"] == there
        # that key is the one the API takes
        webrequests.forget()
        assert get(c, f"/requests/{card['id']}", **{"X-Thread-Key": k}).json()["me"]
        # a thread link mailed before the page moved goes to the website's page
        r = c.get(f"/requests/{card['id']}?k={k}", headers=APEX, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == there
        r = c.get(f"/requests/{card['id']}?k=<script>", headers=APEX, follow_redirects=False)
        assert r.headers["location"] == f"{page}/thread?id={card['id']}"
        # the answer's mail and the hourly replies point there too
        mailer.SENT.clear()
        sales.decide_offer(rsvc, fid, 50, "", "Ada")
        reply(relay, card, MEMBER, "Welcome!", 1)
        assert webthreads.notify(rsvc, now=int(time.time()) + 100) == 1
        assert there in body(mailer.SENT[-1])
        assert webthreads.page_url(rsvc, "requests") == page


def test_an_events_activity_says_names_only_with_consent(fair, reqs, keycast, monkeypatch):
    rsvc, w, ada, _live, _test, _made = fair
    _r, relay, _a = reqs
    tk(w, ada, "day", "Day ticket", ["price", "50", "EUR"], ["vat", "21"], ["quantity", "50"])
    sales.save_settings(rsvc, {"tickets": True})
    make_tester(rsvc)
    yes, no, gone = PrivateKey(), PrivateKey(), PrivateKey()
    seen = []
    with TestClient(create_app(rsvc)) as c:
        for bad in ("yes", 1, None):
            r = order(c, yes, public_name=bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        a = order(c, yes, email="yes@example.org", public_name=True).json()
        assert a["public_name"] is True
        b = order(c, no, email="no@example.org").json()
        assert b["public_name"] is False
        e = order(c, gone, email="gone@example.org", public_name=True, erase_after_event=True)
        unpaid = order(c, PrivateKey(), email="unpaid@example.org", public_name=True).json()
        test = order(c, PrivateKey(), email=TESTER, public_name=True).json()
        assert test["test"] is True and unpaid["state"] == "pending"
        pay_card(c, a, 6050, "evt_a")
        pay_card(c, b, 6050, "evt_b")
        pay_card(c, e.json(), 6050, "evt_e")
        pay_card(c, test, 6050, "evt_t", test=True)
        st = sales.store(rsvc)
        assert st.order(test["order"])["state"] == "active"
        # two requests: one with the consent, one without
        f1, card1, _k1 = ask(c, relay, PrivateKey(), public_name=True, label="food")
        f2, card2, k2 = ask(c, relay, PrivateKey(), email="lou@example.org", name="Lou Reed")
        sales.decide_offer(rsvc, f1, 75, "", "Ada")
        reply(relay, card2, MEMBER, "Tell us more.", 1)
        r = c.post(
            f"/api/v1/requests/{card2['id']}/comments",
            json={"text": "Gladly."},
            headers={**H, "X-Thread-Key": k2},
        )
        assert r.status_code == 201
        webrequests.forget()
        r = get(c, f"/events/{D}/activity")
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "public, max-age=15"
        got = r.json()
        seen.append(got)
        assert got["stats"] == {
            "villagers": 3,
            "open_requests": 1,
            "in_kind_passes": 1,
            "sponsored_passes": 0,
        }
        assert got["next"] is None
        assert [x["at"] for x in got["items"]] == sorted(
            (x["at"] for x in got["items"]), reverse=True
        )
        tickets = [x for x in got["items"] if x["type"] == "ticket"]
        # the test order and the unpaid one never show; a name only with the consent
        assert sorted(str(x["who"]["name"]) for x in tickets) == ["Gus Guest", "Gus Guest", "None"]
        assert all(x["ticket"] == {"title": "Day ticket"} for x in tickets)
        assert all(set(x) == {"type", "at", "who", "ticket"} for x in tickets)
        by = {(x["type"], x["request"]["number"]): x for x in got["items"] if "request" in x}
        assert set(by) == {
            ("request_opened", 1),
            ("request_approved", 1),
            ("request_opened", 2),
            ("request_comment", 2),
        }
        one = by[("request_opened", 1)]
        assert one["who"] == {"name": "Kim Cook"}
        assert one["request"] == {
            "id": card1["id"],
            "number": 1,
            "title": "Kim Cook: 50% off Two-week pass in kind",
            "label": "food",
        }
        assert by[("request_approved", 1)]["percent"] == 75
        assert by[("request_approved", 1)]["who"] == {"name": "Kim Cook"}
        two = by[("request_opened", 2)]
        # no consent: no name, not in the title either
        assert two["who"] == {"name": None}
        assert two["request"]["title"] == "50% off Two-week pass in kind"
        assert two["request"]["label"] is None
        comments = [x for x in got["items"] if x["type"] == "request_comment"]
        assert sorted(str(x["who"]["name"]) for x in comments) == ["Mia", "None"]
        assert "Lou" not in json.dumps(got)
        # the filters and the pages
        only = get(c, f"/events/{D}/activity?type=tickets").json()
        assert {x["type"] for x in only["items"]} == {"ticket"} and len(only["items"]) == 3
        assert only["stats"] == got["stats"]
        only = get(c, f"/events/{D}/activity?type=requests").json()
        assert len(only["items"]) == 5 and all(
            x["type"].startswith("request_") for x in only["items"]
        )
        assert get(c, f"/events/{D}/activity?type=program").json()["items"] == []
        assert get(c, f"/events/{D}/activity?type=x").status_code == 422
        page = get(c, f"/events/{D}/activity?type=requests&limit=2").json()
        rest = get(c, f"/events/{D}/activity?type=requests&limit=100&before={page['next']}").json()
        assert page["next"] and rest["next"] is None
        assert len(page["items"]) + len(rest["items"]) == 5
        # once the event is over, whoever asked to be erased is not in the log any more
        over = st.order(e.json()["order"])
        assert over["erase_after_event"] and over["public_name"]
        monkeypatch.setattr(events.Event, "ended", lambda self, now=None: True)
        webrequests.forget()
        got = get(c, f"/events/{D}/activity?type=tickets").json()
        assert len(got["items"]) == 2 and got["stats"]["villagers"] == 3
    clean(
        *seen,
        keys=(pk(yes), pk(no), pk(gone), pk(ada), pk(w.gkey)),
        addresses=("yes@example.org", "no@example.org", "lou@example.org", TESTER),
    )


def test_day_events_count_on_the_village_event_and_members_only_is_unknown(fair, reqs, keycast):
    rsvc, w, ada, _live, _test, _made = fair
    _r, relay, _a = reqs
    sales.save_settings(rsvc, {"tickets": True})
    now = int(time.time())
    day, hidden = "day001", "hid001"
    for d, visibility, more in (
        (day, "public", [["a", f"31923:{pk(ada)}:{D}"]]),
        (hidden, "members", []),
    ):
        w.put(
            ada,
            31923,
            [
                ["d", d],
                ["title", "Day one"],
                ["start", str(now + 86400)],
                ["end", str(now + 90000)],
                ["h", "ev-" + d],
                ["visibility", visibility],
                *more,
            ],
        )
    w.put(
        ada,
        30402,
        [
            ["d", f"{day}:pass"],
            ["a", f"31923:{pk(ada)}:{day}"],
            ["h", "ev-" + day],
            ["title", "Day pass"],
            ["t", "ticket"],
            ["price", "10", "EUR"],
        ],
    )
    plain(relay, 1, "A question")
    buyer = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        o = order(c, buyer, ticket=f"{day}:pass", public_name=True)
        assert o.status_code == 201, o.text
        pay_card(c, o.json(), 1000, "evt_d")
        webrequests.forget()
        # the village event: its day events' tickets, and the requests
        got = get(c, f"/events/{D}/activity").json()
        assert got["stats"] == {
            "villagers": 1,
            "open_requests": 1,
            "in_kind_passes": 0,
            "sponsored_passes": 0,
        }
        assert [x["type"] for x in got["items"]] == ["request_opened", "ticket"] or [
            x["type"] for x in got["items"]
        ] == ["ticket", "request_opened"]
        t = next(x for x in got["items"] if x["type"] == "ticket")
        assert t["ticket"] == {"title": "Day pass"} and t["who"] == {"name": "Gus Guest"}
        # the day event: its own ticket; the requests belong to the event it points to
        got = get(c, f"/events/{day}/activity").json()
        assert got["stats"] == {
            "villagers": 1,
            "open_requests": 0,
            "in_kind_passes": 0,
            "sponsored_passes": 0,
        }
        assert [x["type"] for x in got["items"]] == ["ticket"]
        # a members-only event, or none: nothing to see
        for link in (hidden, "nope12", "x"):
            r = get(c, f"/events/{link}/activity")
            assert r.status_code == 404 and r.json()["error"] == "not_found", link
        assert GROUP  # (the village event's group: the fixture's)
    clean(got, keys=(pk(buyer), pk(ada)), addresses=("gus@example.org",))


def test_a_relay_that_is_down_is_a_503(reqs, monkeypatch):
    rsvc, _relay, _ada = reqs

    def down(svc, channel):
        raise RuntimeError("relay down")

    monkeypatch.setattr(webthreads, "_events", down)
    with TestClient(create_app(rsvc), raise_server_exceptions=False) as c:
        for path in ("/requests", "/requests/1"):
            r = get(c, path)
            assert r.status_code == 503 and r.json()["error"] == "unavailable", path


def test_too_many_questions_from_one_place(reqs):
    rsvc, _relay, _ada = reqs
    with TestClient(create_app(rsvc)) as c:
        for _ in range(120):
            assert get(c, "/requests").status_code == 200
        r = get(c, "/requests")
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        assert get(c, "/requests", **{"X-Forwarded-For": "10.9.9.9"}).status_code == 200
