"""Request types, closing a plain request, and reactions (docs/requests.md; sales.py,
webrequests.py, actions.py): ``type`` on POST /offers and on every request, plain requests
(enquiry, sponsoring, talk, meetup) that are open or closed, ``belfry:request-close`` and the
console's Close / Reopen, kind 7 reactions counted and made as the person."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import itertools

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    actions,
    apps,
    community,
    mail,
    sales,
    sitesessions,
    webrequests,
)
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_offers import ASK, offers  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_site_sessions import LEO, guest, site  # noqa: F401 — fixtures
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair  # noqa: F401 — fixtures
from .test_webrequests import MEMBER, H, ask, get, label, plain, reply, reqs  # noqa: F401
from .test_webthreads import GK

PLAIN = {"product": None, "title": "A talk about bees", "offer": "Forty minutes, with slides."}
_n = itertools.count(1000)


def press(rsvc, message, presser, button="close"):
    p = actions.Press(
        rsvc,
        {
            "id": f"{next(_n):064x}",
            "pubkey": presser,
            "button": button,
            "message": message["id"],
            "group": "requests",
            "created_at": 0,
        },
        message,
        next(b for b in actions.parse_buttons(message["tags"]) if b["id"] == button),
    )
    return asyncio.run(sales.request_close_press(p))


def react_event(relay, target: dict, by: str, content: str, at: int | None = None) -> dict:
    """A reaction as the chat makes it: not through the endpoint."""
    n = next(_n)
    e = {
        "id": f"{n:064x}",
        "kind": 7,
        "pubkey": by,
        "created_at": at or 1_790_100_000 + n,
        "tags": [
            ["h", "requests"],
            ["e", target["id"]],
            ["p", target["pubkey"]],
            ["k", str(target["kind"])],
        ],
        "content": content,
    }
    relay.events.append(e)
    webrequests.forget()
    return e


def delete_event(relay, what: dict, by: str) -> None:
    n = next(_n)
    relay.events.append(
        {
            "id": f"{n:064x}",
            "kind": 5,
            "pubkey": by,
            "created_at": 1_790_200_000 + n,
            "tags": [["h", "requests"], ["e", what["id"]], ["k", "7"]],
            "content": "",
        }
    )
    webrequests.forget()


# ── 1–3: types ──────────────────────────────────────────────────────────────────────────────
def test_every_request_has_a_type_and_old_threads_get_one(reqs):
    rsvc, relay, _ada = reqs
    with TestClient(create_app(rsvc)) as c:
        # as before types existed: the kind says it
        f1, card1, _k = ask(c, relay, PrivateKey())
        f2, card2, _k = ask(c, relay, PrivateKey(), kind="sponsored", email="lou@example.org")
        assert ["request-type", "in_kind"] in card1["tags"]
        assert ["request-type", "sponsored_ticket"] in card2["tags"]
        # a thread from before: no tag on the message, no type on the record
        st = sales.store(rsvc)
        for fid, card in ((f1, card1), (f2, card2)):
            card["tags"] = [t for t in card["tags"] if t[0] != "request-type"]
            f = st.offer(fid)
            f.pop("type")
            st.put_offer(f)
        chat = plain(relay, 5, "Where do we park?")  # started in the chat
        webrequests.forget()
        items = {i["id"]: i for i in get(c, "/requests").json()["items"]}
        assert items[card1["id"]]["type"] == "in_kind" and items[card1["id"]]["kind"] == "in_kind"
        assert items[card2["id"]]["type"] == "sponsored_ticket"
        assert items[card2["id"]]["kind"] == "sponsored"
        assert items[chat["id"]]["type"] == "enquiry" and items[chat["id"]]["kind"] == "other"
        assert get(c, f"/requests/{card2['id']}").json()["type"] == "sponsored_ticket"
        # the type named, with what it allows
        f3, card3, _k = ask(
            c, relay, PrivateKey(), type="in_kind", label="food", email="a@example.org"
        )
        assert st.offer(f3)["type"] == "in_kind" and st.offer(f3)["label"] == "food"
        me = PrivateKey()
        f4, _c4, _k = ask(c, relay, me, type="sponsored_ticket", email="b@example.org")
        assert st.offer(f4)["kind"] == "sponsored"
        got = call(c, me, f"/offers/{f4}", method="GET").json()
        assert got["type"] == "sponsored_ticket" and got["kind"] == "sponsored"
        for bad in (
            {"type": "nope"},
            {"type": 5},
            {"type": "sponsored_ticket", "label": "food"},
            {"type": "sponsored_ticket", "kind": "in_kind"},
            {"type": "in_kind", "kind": "sponsored"},
        ):
            r = call(c, PrivateKey(), "/offers", {**ASK, **bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
    # without the register the message's own tags say it
    assert sales.message_type({"tags": [["offer", "f_x"], ["offer-kind", "sponsored"]]}) == (
        "sponsored_ticket"
    )
    assert sales.message_type({"tags": [["offer", "f_x"]]}) == "in_kind"
    assert sales.message_type({"tags": [["offer", "f_x"], ["request-type", "talk"]]}) == "talk"
    assert sales.message_type({"tags": [["request-type", "meetup"]]}) == "meetup"
    assert sales.message_type({"tags": [["request-type", "in_kind"]]}) == "enquiry"  # no offer
    assert sales.message_type({"tags": []}) == "enquiry"


def test_a_plain_request_has_no_percent_no_approval_and_reads_well_in_the_chat(reqs):
    rsvc, relay, ada = reqs
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        for field, value in (
            ("product", "village-2027"),
            ("ticket", "x"),
            ("percent", 50),
            ("kind", "in_kind"),
            ("label", "workshop"),
        ):
            r = call(c, me, "/offers", {**ASK, **PLAIN, "type": "talk", field: value})
            assert r.status_code == 422 and r.json()["error"] == "invalid", field
        fid, card, k = ask(
            c, relay, me, **PLAIN, type="talk", days=["2027-01-26"], public_name=True
        )
        tags = card["tags"]
        assert ["request-type", "talk"] in tags and ["offer", fid] in tags
        assert ["offer-days", "2027-01-26"] in tags
        assert not [t for t in tags if t[0] in ("percent", "product", "ticket", "offer-kind")]
        assert actions.parse_buttons(tags) == [
            {
                "id": "close",
                "label": "Close",
                "style": "",
                "action": "belfry:request-close",
            }
        ]
        # the chat: the title in bold, the type's plain name, the text; never an id
        assert card["content"] == (
            "**A talk about bees**\n\n_Talk or workshop_\n\nForty minutes, with slides."
        )
        one = get(c, f"/requests/{card['id']}").json()
        assert (one["type"], one["kind"], one["state"]) == ("talk", "other", "open")
        assert one["title"] == "A talk about bees"
        assert one["body"] == "Forty minutes, with slides."  # the type's line is not the text
        assert one["percent_asked"] is None and one["label"] is None
        assert one["days"] == ["2027-01-26"] and one["percent_approved"] is None
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert (got["type"], got["state"], got["kind"], got["product"]) == (
            "talk",
            "open",
            None,
            None,
        )
        # every percent field is null for a plain request: never 0, never left out
        assert "percent" in got and got["percent"] is None
        assert "percent_asked" in one and "percent_approved" in one
        item = get(c, "/requests?type=talk").json()["items"][0]
        assert item["percent_asked"] is None and item["percent_approved"] is None
        # a title of its own when none is given: the name and the type's name
        _f, card2, _k = ask(
            c, relay, PrivateKey(), product=None, type="enquiry", email="lou@example.org"
        )
        assert ["subject", "Kim Cook: General enquiry"] in card2["tags"]
        assert card2["content"].startswith("**Kim Cook: General enquiry**\n\n_General enquiry_\n\n")
        # nothing to approve
        r = call(c, ada, f"/offers/{fid}/decide", {"percent": 100})
        assert r.status_code == 409 and r.json()["error"] == "no_decision"
        # an edit of the text keeps the type's line in the chat
        r = c.post(
            f"/api/v1/requests/{card['id']}/edit",
            json={"body": "Thirty minutes."},
            headers={**H, "X-Thread-Key": k},
        )
        assert r.status_code == 200 and r.json()["changed"] is True, r.text
        edit = [e for e in relay.events if e["kind"] == 1010][-1]
        assert edit["content"] == "**A talk about bees**\n\n_Talk or workshop_\n\nThirty minutes."
        assert get(c, f"/requests/{card['id']}").json()["body"] == "Thirty minutes."
        # open plain requests don't use up the three that may wait for an answer
        for i in range(3):
            st = sales.store(rsvc)
            f = st.offer(fid)
            st.put_offer({**f, "id": f"f_{i:016d}"})
        mail._counts.clear()
        assert call(c, me, "/offers", {**ASK, "email": "kim2@example.org"}).status_code == 201


def test_the_list_filters_by_type(reqs):
    rsvc, relay, _ada = reqs
    with TestClient(create_app(rsvc)) as c:
        _f, card1, _k = ask(c, relay, PrivateKey())
        _f, card2, _k = ask(c, relay, PrivateKey(), **PLAIN, type="talk", email="a@example.org")
        _f, card3, _k = ask(c, relay, PrivateKey(), **PLAIN, type="meetup", email="b@example.org")
        chat = plain(relay, 7, "Where do we park?")
        label(relay, chat, "closed", MEMBER, 8)

        def ids(query):
            r = get(c, "/requests" + query)
            assert r.status_code == 200, r.text
            return {i["id"] for i in r.json()["items"]}, r.json()["counts"]

        assert ids("?type=talk") == ({card2["id"]}, {"open": 1, "approved": 0, "closed": 0})
        assert ids("?type=meetup")[0] == {card3["id"]}
        assert ids("?type=in_kind")[0] == {card1["id"]}
        assert ids("?type=enquiry") == ({chat["id"]}, {"open": 0, "approved": 0, "closed": 1})
        assert ids("?type=sponsoring")[0] == set()
        assert ids("?type=enquiry&state=open")[0] == set()
        assert len(ids("")[0]) == 4
        for bad in ("?type=nope", "?type=talk,meetup"):
            r = get(c, "/requests" + bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid"


# ── 4: closing ──────────────────────────────────────────────────────────────────────────────
def test_admins_close_with_the_button_and_reopen_in_the_console(reqs, world, monkeypatch):
    rsvc, relay, ada = reqs
    rsvc.settings.pass_page_oidc_client_secret = "x"
    rsvc.settings.pass_page_secret = "p" * 32
    monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {pk(ada)})
    relay.events.append(
        {
            "id": "k1",
            "kind": 0,
            "pubkey": pk(ada),
            "created_at": 1,
            "tags": [],
            "content": '{"name":"Ada"}',
        }
    )
    from .test_sales_console import CSRF, signed_in

    assert actions.HANDLERS["request-close"] is actions.request_close
    me = PrivateKey()
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        fid, card, k = ask(c, relay, me, **PLAIN, type="talk")
        _f2, offer_card, _k2 = ask(c, relay, PrivateKey(), email="lou@example.org")
        # only admins; only a plain request's own opening message
        res = press(rsvc, card, pk(me))
        assert res.status == "error" and res.code == "not_allowed"
        button = ["button", "close", "Close", "", "belfry:request-close"]
        forged = {**offer_card, "tags": [*offer_card["tags"][:-4], button]}
        assert press(rsvc, forged, pk(ada)).code == "no_target"  # an offer is decided
        a_reply = reply(relay, card, MEMBER, "Nice.", 40)
        assert press(rsvc, {**a_reply, "tags": [*a_reply["tags"], button]}, pk(ada)).code == (
            "no_target"
        )
        assert not [e for e in relay.events if e["kind"] == 1985]
        res = press(rsvc, card, pk(ada))
        assert res.status == "ok" and res.closed and res.text == "Closed by Ada."
        lab = [e for e in relay.events if e["kind"] == 1985][-1]
        assert lab["pubkey"] == GK and ["l", "closed", "belfry/inbox"] in lab["tags"]
        assert ["e", card["id"]] in lab["tags"] and ["h", "requests"] in lab["tags"]
        assert ["by", pk(ada)] in lab["tags"]
        res = press(rsvc, card, pk(ada))
        assert res.status == "error" and res.code == "gone"
        assert len([e for e in relay.events if e["kind"] == 1985]) == 1
        one = get(c, f"/requests/{card['id']}").json()
        assert one["state"] == "closed"
        assert [(x["type"], x["by"]) for x in one["timeline"] if x["type"] != "comment"] == [
            ("closed", {"name": "Ada"})
        ]
        assert get(c, "/requests?state=closed").json()["items"][0]["id"] == card["id"]
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["state"] == "closed" and got["percent"] is None
        assert one["percent_asked"] is None and one["percent_approved"] is None  # closed too
        # closed requests still show, and take comments
        r = c.post(
            f"/api/v1/requests/{card['id']}/comments",
            json={"text": "One more thing."},
            headers={**H, "X-Thread-Key": k},
        )
        assert r.status_code == 201, r.text
        # the console: Reopen, then Close, with the csrf and fixed messages
        signed_in(c, rsvc, world[4])
        page = c.get("/sales", headers=H).text
        assert "Talk or workshop" in page and ">Reopen</button>" in page
        r = c.post("/sales/request", data={"id": fid, "do": "reopen"}, headers=H)
        assert get(c, f"/requests/{card['id']}").json()["state"] == "closed"  # no csrf: nothing
        post = {"csrf": CSRF, "id": fid}
        r = c.post("/sales/request", data={**post, "do": "reopen"}, headers=H)
        assert "Request reopened." in r.text  # (the redirect followed: the page with its flash)
        one = get(c, f"/requests/{card['id']}").json()
        assert one["state"] == "open"
        assert [x["type"] for x in one["timeline"] if x["type"] != "comment"] == [
            "closed",
            "reopened",
        ]
        assert one["timeline"][-1]["by"] == {"name": "Ada"}
        page = c.get("/sales", headers=H).text
        assert ">Close</button>" in page
        n = len([e for e in relay.events if e["kind"] == 1985])
        c.post("/sales/request", data={**post, "do": "reopen"}, headers=H)  # that already
        c.post("/sales/request", data={**post, "do": "delete"}, headers=H)
        offer_id = next(t[1] for t in offer_card["tags"] if t[0] == "offer")
        c.post("/sales/request", data={"csrf": CSRF, "id": offer_id, "do": "close"}, headers=H)
        assert len([e for e in relay.events if e["kind"] == 1985]) == n
        c.post("/sales/request", data={**post, "do": "close"}, headers=H)
        assert get(c, f"/requests/{card['id']}").json()["state"] == "closed"
        # a thread started in the chat is an enquiry: closed the same way
        chat = plain(relay, 60, "Where do we park?")
        res = press(rsvc, {**chat, "tags": [*chat["tags"], button]}, pk(ada))
        assert res.status == "ok" and res.text == "Closed by Ada."
        assert get(c, f"/requests/{chat['id']}").json()["state"] == "closed"
        # a member's own label in the chat still counts, under their name
        label(relay, chat, "open", MEMBER, 70)
        one = get(c, f"/requests/{chat['id']}").json()
        assert one["state"] == "open" and one["timeline"][-1] == {
            "type": "reopened",
            "by": {"name": "Mia"},
            "at": one["timeline"][-1]["at"],
        }


# ── 5: reactions ────────────────────────────────────────────────────────────────────────────
def test_reactions_made_in_the_chat_are_counted(reqs):
    rsvc, relay, ada = reqs
    a, b, d = MEMBER, "c" * 64, "d" * 64
    with TestClient(create_app(rsvc)) as c:
        _f, card, _k = ask(c, relay, PrivateKey())
        com = reply(relay, card, MEMBER, "Which evenings?", 30)
        assert get(c, f"/requests/{card['id']}").json()["reactions"] == []
        react_event(relay, card, a, "🎉", at=100)
        react_event(relay, card, a, "+", at=110)  # "+" is a thumbs up
        react_event(relay, card, b, "👍", at=120)
        react_event(relay, card, b, "👍", at=125)  # twice: once
        react_event(relay, card, d, "👍", at=130)
        react_event(relay, card, d, "-", at=140)
        react_event(relay, card, d, "❤️", at=150)
        for junk in ("lol", "👍 ", ":custom:", "x" * 40, "🎉" * 9, "!!"):
            react_event(relay, card, d, junk)
        gone = react_event(relay, card, b, "🔥", at=160)
        delete_event(relay, gone, b)  # their own deletion counts
        kept = react_event(relay, card, b, "👀", at=170)
        delete_event(relay, kept, d)  # someone else's does not
        react_event(relay, com, a, "🙏", at=180)
        want = [
            {"emoji": "👍", "count": 3},
            {"emoji": "🎉", "count": 1},
            {"emoji": "👎", "count": 1},
            {"emoji": "❤️", "count": 1},
            {"emoji": "👀", "count": 1},
        ]
        one = get(c, f"/requests/{card['id']}").json()
        assert one["reactions"] == want
        [entry] = [x for x in one["timeline"] if x["type"] == "comment"]
        assert entry["reactions"] == [{"emoji": "🙏", "count": 1}]
        assert get(c, "/requests").json()["items"][0]["reactions"] == want
        # a thread key alone reads no "mine", and may not react
        r = c.post(
            f"/api/v1/requests/{card['id']}/react",
            json={"emoji": "👍"},
            headers={**H, "X-Thread-Key": _k},
        )
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        r = c.post(f"/api/v1/requests/{card['id']}/react", json={"emoji": "👍"}, headers=H)
        assert r.status_code == 401 and r.json()["error"] == "no_session"
        r = call(c, PrivateKey(), f"/requests/{card['id']}/react", {"emoji": "👍"})
        assert r.status_code == 401 and r.json()["error"] == "no_session"


def test_what_one_emoji_is():
    for ok in ("👍", "❤️", "👨‍👩‍👧", "🇧🇪", "👍🏽", "#️⃣", "🏴󠁧󠁢󠁳󠁣󠁴󠁿"):
        assert webrequests.emoji_of(ok, one=True) == ok, ok
    assert webrequests.emoji_of("+", one=True) == "👍"
    assert webrequests.emoji_of("-", one=True) == "👎"
    for bad in ("", "a", "7", "👍👍", "👍 ", "ok👍", "🇧🇪🇫🇷", "!", "👨‍👩‍👧‍👦‍👨‍👩‍👧‍👦", None, 5):
        assert webrequests.emoji_of(bad, one=True) is None, bad
    assert webrequests.emoji_of("") == "👍"  # read: an empty reaction is a like (NIP-25)


def test_someone_logged_in_reacts_as_themselves(site, monkeypatch):
    rsvc, w, ada = site
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")

    def sevens():
        return [e for e in w.grp.events if e["kind"] == 7]

    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        leo = guest(c, LEO, "Leo")
        assert call(c, kim, "/offers", ASK).status_code == 201
        assert call(c, leo, "/requests/1/comments", {"text": "Which evenings?"}).status_code == 201
        card = next(e for e in w.grp.events if e["kind"] == 9)
        com = next(e for e in w.grp.events if e["kind"] == 1111)
        leo_pk = sitesessions.site_account(rsvc, pk(leo))["pubkey"]
        kim_pk = sitesessions.site_account(rsvc, pk(kim))["pubkey"]
        for bad in ({}, {"emoji": ""}, {"emoji": "ok"}, {"emoji": "👍👍"}, {"emoji": 5}):
            r = call(c, leo, "/requests/1/react", bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        r = call(c, leo, "/requests/1/react", {"emoji": "👍", "remove": "yes"})
        assert r.status_code == 422
        r = call(c, leo, "/requests/1/react", {"emoji": "👍", "comment": "0" * 64})
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        assert call(c, leo, "/requests/9/react", {"emoji": "👍"}).status_code == 404
        assert sevens() == []
        # a kind 7 signed as the person, tagged as the chat tags a reaction in a group
        r = call(c, leo, "/requests/1/react", {"emoji": "👍"})
        assert r.status_code == 200 and r.json() == {
            "reactions": [{"emoji": "👍", "count": 1, "mine": True}]
        }, r.text
        [e] = sevens()
        assert e["pubkey"] == leo_pk and e["content"] == "👍"
        assert e["tags"] == [
            ["h", "requests"],
            ["e", card["id"]],
            ["p", card["pubkey"]],
            ["k", "9"],
        ]
        # the same again: unchanged, no second event ("+" is the same thumbs up)
        for same in ("👍", "+"):
            r = call(c, leo, "/requests/1/react", {"emoji": same})
            assert r.status_code == 200 and r.json()["reactions"][0]["count"] == 1
        assert len(sevens()) == 1
        # the author too, and on a comment
        r = call(c, kim, "/requests/1/react", {"emoji": "👍"})
        assert r.json() == {"reactions": [{"emoji": "👍", "count": 2, "mine": True}]}
        r = call(c, kim, "/requests/1/react", {"emoji": "🙏", "comment": com["id"]})
        assert r.json() == {"reactions": [{"emoji": "🙏", "count": 1, "mine": True}]}
        assert sevens()[-1]["tags"] == [
            ["h", "requests"],
            ["e", com["id"]],
            ["p", leo_pk],
            ["k", "1111"],
        ]
        # "mine" only for who signed the read
        one = call(c, leo, "/requests/1", method="GET").json()
        assert one["reactions"] == [{"emoji": "👍", "count": 2, "mine": True}]
        [entry] = [x for x in one["timeline"] if x["type"] == "comment"]
        assert entry["reactions"] == [{"emoji": "🙏", "count": 1}]
        lst = call(c, kim, "/requests", method="GET").json()
        assert lst["items"][0]["reactions"] == [{"emoji": "👍", "count": 2, "mine": True}]
        pub = c.get("/api/v1/requests/1", headers=H).json()
        assert pub["reactions"] == [{"emoji": "👍", "count": 2}]
        assert "mine" not in str(c.get("/api/v1/requests", headers=H).json()["items"])
        # an account's own key that may comment (a member), with no website session
        r = call(c, bob, "/requests/1/react", {"emoji": "🎉"})
        assert r.status_code == 200, r.text
        assert r.json()["reactions"] == [
            {"emoji": "👍", "count": 2},
            {"emoji": "🎉", "count": 1, "mine": True},
        ]
        assert sevens()[-1]["pubkey"] == pk(bob)
        # taking it away: a kind 5 by the person on their own reaction
        r = call(c, leo, "/requests/1/react", {"emoji": "👍", "remove": True})
        # (one each, made within the same second here: either may come first)
        both = [{"emoji": "🎉", "count": 1}, {"emoji": "👍", "count": 1}]
        assert r.status_code == 200
        assert sorted(r.json()["reactions"], key=lambda x: x["emoji"]) == sorted(
            both, key=lambda x: x["emoji"]
        )
        five = [e for e in w.grp.events if e["kind"] == 5][-1]
        assert five["pubkey"] == leo_pk
        assert five["tags"] == [["h", "requests"], ["e", sevens()[0]["id"]], ["k", "7"]]
        n = len(w.grp.events)
        r = call(c, leo, "/requests/1/react", {"emoji": "👍", "remove": True})  # none: unchanged
        assert r.status_code == 200 and len(w.grp.events) == n
        r = call(c, leo, "/requests/1/react", {"emoji": "🎉", "remove": True})  # Bob's: not his
        assert r.status_code == 200 and len(w.grp.events) == n
        got = c.get("/api/v1/requests/1", headers=H).json()["reactions"]
        assert sorted(got, key=lambda x: x["emoji"]) == sorted(both, key=lambda x: x["emoji"])
        assert kim_pk != leo_pk
        # 60 an hour per person
        monkeypatch.setattr(webrequests, "REACTIONS_PER_HOUR", 1)
        mail._counts.clear()
        assert call(c, leo, "/requests/1/react", {"emoji": "🔥"}).status_code == 200
        r = call(c, leo, "/requests/1/react", {"emoji": "👀"})
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        r = call(c, leo, "/requests/1/react", {"emoji": "🔥"})  # nothing to do: never counted
        assert r.status_code == 200
        mail._counts.clear()
        # someone who holds their own key: the belfry can't sign for them
        monkeypatch.setattr(community, "member_signer", lambda svc, pid: None)
        r = call(c, kim, "/requests/1/react", {"emoji": "🌱"})
        assert r.status_code == 409 and r.json()["error"] == "own_key"

        # the website reaches it; its previews never do
        def preflight(path, origin):
            return c.options(
                "/api/v1" + path,
                headers={**H, "Origin": origin, "Access-Control-Request-Method": "POST"},
            )

        assert preflight("/requests/1/react", "https://belfry.test").status_code == 204
        assert preflight("/requests/1/react", "https://preview.belfry.test").status_code == 403
