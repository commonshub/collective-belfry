"""A request's shorter links, its author's own title and the author's edits (docs/requests.md
§3–§5): the title on POST /offers, POST /requests/<id or number>/edit as a kind 1010 the chat
reads, and what the JSON and the HTML page show after an edit."""

# ruff: noqa: F811
from __future__ import annotations

import json
import re

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import checkin, community, mail, mailer, sales, webrequests, webthreads
from belfry_onboarding.app import create_app

from .test_guests import published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_console import signed_in
from .test_sales_offers import ASK, SITE, body, offers  # noqa: F401 — fixtures
from .test_webrequests import APEX, MEMBER, H, ask, clean, get, reply, reqs  # noqa: F401
from .test_webthreads import EVE, GK
from .test_webthreads import world as pages  # noqa: F401 — the channel's HTML pages

GENERATED = "Kim Cook: 50% off Two-week pass in kind"
TEXT = "I cook for 40 people on two evenings."


def edit(c, ref, data, k="", key=None):
    if key is not None:
        return call(c, key, f"/requests/{ref}/edit", data)
    return c.post(
        f"/api/v1/requests/{ref}/edit",
        json=data,
        headers={**H, **({"X-Thread-Key": k} if k else {})},
    )


def edits(relay) -> list[dict]:
    return [e for e in relay.events if e["kind"] == 1010]


def subjects() -> list[str]:
    return [str(m["Subject"]) for m in mailer.SENT]


def test_the_persons_own_title_is_the_threads_title(reqs):
    rsvc, relay, _ada = reqs
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        for bad in (7, ["x"], "", "   ", "\x00\x07", "x" * 121, {"a": 1}):
            r = call(c, me, "/offers", {**ASK, "title": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        assert not mailer.SENT
        # one line, trimmed, control characters out; exactly 120 characters pass
        fid, card, _k = ask(c, relay, me, title="  Dinner\tfor\n forty \x07people  ", label="food")
        assert ["subject", "Dinner for forty people"] in card["tags"]
        assert card["content"] == f"**Dinner for forty people**\n\n{TEXT}"
        # no name, percent or ticket added: they stay their own tags and fields
        assert ["percent", "50"] in card["tags"] and ["product", "village-2027"] in card["tags"]
        assert subjects()[-2:] == [
            "Confirm your request: Dinner for forty people",
            "Your thread: Dinner for forty people",
        ]
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["title"] == "Dinner for forty people"
        one = get(c, "/requests/1").json()
        assert one["title"] == "Dinner for forty people" and one["body"] == TEXT
        assert one["percent_asked"] == 50 and one["label"] == "food" and one["kind"] == "in_kind"
        assert one["edited_at"] is None
        # the answer's mail says which request, and stays short
        sales.decide_offer(rsvc, fid, 75, "", "Ada")
        assert subjects()[-1] == "Your request is approved: Dinner for forty people"
        long = "A very long title " * 6 + "that goes on"
        assert len(long) == 120
        fid2, card2, _k2 = ask(c, relay, PrivateKey(), email="lou@example.org", title=long)
        assert ["subject", long] in card2["tags"]
        cut = long[:80].rstrip() + "…"
        assert f"Confirm your request: {cut}" in subjects()
        sales.decide_offer(rsvc, fid2, 0, "", "Ada")
        assert subjects()[-1] == f"Your request has an answer: {cut}"
        # without a title: the generated one, as before
        fid3, card3, _k3 = ask(c, relay, PrivateKey(), email="ann@example.org", title=None)
        assert ["subject", GENERATED] in card3["tags"]
        assert f"Confirm your request: {GENERATED}" in subjects()
        assert sales.store(rsvc).offer(fid3)["title"] == GENERATED
        assert get(c, "/requests/3").json()["title"] == GENERATED
    clean(one, got, keys=(pk(me),))


def test_the_author_edits_the_title_and_the_text(reqs):
    rsvc, relay, ada = reqs
    me = PrivateKey()
    seen = []
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me, title="Dinner for forty")
        _f2, card2, k2 = ask(c, relay, PrivateKey(), email="lou@example.org", name="Lou")
        # nobody, another key, an admin, another thread's key, the key in the query string
        r = edit(c, 1, {"title": "Mine now"})
        assert r.status_code == 403 and r.json()["error"] == "not_author"
        for key in (PrivateKey(), ada):
            r = edit(c, 1, {"title": "Mine now"}, key=key)
            assert r.status_code == 403 and r.json()["error"] == "not_author"
        assert edit(c, 1, {"title": "Mine now"}, k=k2).json()["error"] == "not_author"
        r = c.post(f"/api/v1/requests/1/edit?k={k}", json={"title": "Mine now"}, headers=H)
        assert r.status_code == 403
        r = c.post(
            "/api/v1/requests/1/edit",
            json={"title": "x"},
            headers={**H, "Authorization": "Nostr x"},
        )
        assert r.status_code == 401
        assert edit(c, 9, {"title": "x"}, k=k).status_code == 404
        assert edit(c, "0" * 64, {"title": "x"}, k=k).json()["error"] == "unknown"
        # what an edit is
        for bad in (
            {},
            {"title": None},
            {"title": ""},
            {"title": "x" * 121},
            {"title": 5},
            {"body": ""},
            {"body": "  "},
            {"body": 5},
            {"body": "x" * 8001},
            {"title": "Fine", "body": ""},
            {"title": "Fine", "comment": "a" * 64, "text": "x"},
            {"comment": "a" * 64},
            {"comment": "a" * 64, "text": " "},
        ):
            r = edit(c, 1, bad, k=k)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        r = c.post("/api/v1/requests/1/edit", content=b"[", headers={**H, "X-Thread-Key": k})
        assert r.status_code == 400
        assert not edits(relay)
        # nothing changes: no event
        for same in (
            {"title": "Dinner for forty"},
            {"body": TEXT},
            {"title": " Dinner for  forty ", "body": TEXT + "\n"},
        ):
            r = edit(c, 1, same, k=k)
            assert r.status_code == 200 and r.json() == {"ok": True, "changed": False}, same
        assert not edits(relay)
        # the title, by the thread's key and by the number
        r = edit(c, 1, {"title": "Dinner for fifty"}, k=k)
        assert r.status_code == 200 and r.json() == {"ok": True, "changed": True}
        assert r.headers["cache-control"] == "no-store"
        (x,) = edits(relay)
        # as the chat reads an edit (village-app src/core/model.ts `edits`, actions.ts
        # `editMessage`): kind 1010 in the group, `e` = the message (the first e tag), `k` =
        # its kind, by the key that signed the message, the new text as content
        assert x["kind"] == 1010 and x["pubkey"] == card["pubkey"] == GK
        assert x["tags"][:3] == [["h", "requests"], ["e", card["id"]], ["k", "9"]]
        assert x["content"] == f"**Dinner for fifty**\n\n{TEXT}"
        assert x["created_at"] > card["created_at"]
        # the tags readers need ride along: the title, whose thread, which offer; no buttons
        assert ["subject", "Dinner for fifty"] in x["tags"]
        assert [t for t in x["tags"] if t[0] == "subject"] == [["subject", "Dinner for fifty"]]
        for t in (["mail-from", "kim@example.org"], ["offer", fid], ["client", "belfry-web"]):
            assert t in x["tags"], t
        assert ["product", "village-2027"] in x["tags"] and ["percent", "50"] in x["tags"]
        assert not [t for t in x["tags"] if t[0] == "button"]
        assert [t for t in x["tags"] if t[0] == "e"] == [["e", card["id"]]]
        assert card["content"] == f"**Dinner for forty**\n\n{TEXT}"  # the first version stays
        # the text, by the offer's own key (NIP-98) and by the id
        r = edit(c, card["id"], {"body": "I cook for 50 people."}, key=me)
        assert r.status_code == 200 and r.json() == {"ok": True, "changed": True}
        y = edits(relay)[-1]
        assert y["content"] == "**Dinner for fifty**\n\nI cook for 50 people."
        assert y["created_at"] > x["created_at"] and ["subject", "Dinner for fifty"] in y["tags"]
        # both at once
        r = edit(c, 1, {"title": "Supper", "body": "Soup for 50."}, k=k)
        assert r.json() == {"ok": True, "changed": True} and len(edits(relay)) == 3
        z = edits(relay)[-1]
        # reading: the current title and text, when, and what happened
        got = get(c, "/requests/1").json()
        seen.append(got)
        assert got["title"] == "Supper" and got["body"] == "Soup for 50."
        assert got["edited_at"] == z["created_at"] and got["created_at"] == card["created_at"]
        line = [e for e in got["timeline"] if e["type"] == "edited"]
        assert [(e["field"], e["at"]) for e in line] == [
            ("title", x["created_at"]),
            ("body", y["created_at"]),
            ("title", z["created_at"]),
            ("body", z["created_at"]),
        ]
        assert [e.get("title") for e in line] == ["Dinner for fifty", None, "Supper", None]
        assert all(e["by"] == {"name": "Kim Cook"} for e in line)
        assert [e["at"] for e in got["timeline"]] == sorted(e["at"] for e in got["timeline"])
        assert "Dinner for forty" not in json.dumps(got)  # old versions are not in the JSON
        # the list: the title it has now; q looks in the current title and text
        rows = get(c, "/requests").json()
        seen.append(rows)
        row = next(i for i in rows["items"] if i["number"] == 1)
        assert row["title"] == "Supper" and row["edited_at"] == z["created_at"]
        assert next(i for i in rows["items"] if i["number"] == 2)["edited_at"] is None
        assert [i["number"] for i in get(c, "/requests?q=soup").json()["items"]] == [1]
        assert [i["number"] for i in get(c, "/requests?q=supper").json()["items"]] == [1]
        assert get(c, "/requests?q=forty").json()["items"] == []
        assert [i["number"] for i in get(c, "/requests?q=40%20people").json()["items"]] == [2]
        # the offer says what its thread says
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert got["title"] == "Supper" and sales.store(rsvc).offer(fid)["offer"] == "Soup for 50."
        # someone else's edit event is nothing (only the key that signed the message edits it)
        relay.events.append(
            {
                "id": "9" * 64,
                "kind": 1010,
                "pubkey": MEMBER,
                "created_at": z["created_at"] + 50,
                "tags": [["h", "requests"], ["e", card["id"]], ["k", "9"], ["subject", "Hijacked"]],
                "content": "**Hijacked**\n\nNo.",
            }
        )
        webrequests.forget()
        assert get(c, "/requests/1").json()["title"] == "Supper"
        # an edit made in the chat (no subject tag): the bold first line is the title
        relay.events.append(
            {
                "id": "8" * 64,
                "kind": 1010,
                "pubkey": GK,
                "created_at": z["created_at"] + 60,
                "tags": [["h", "requests"], ["e", card["id"]], ["k", "9"]],
                "content": "**From the chat**\n\nStew.",
            }
        )
        webrequests.forget()
        got = get(c, "/requests/1").json()
        assert got["title"] == "From the chat" and got["body"] == "Stew."
        # an archived channel takes no edit
        webrequests.forget()
        import pytest

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(checkin, "is_archived", lambda svc, group: True)
            r = edit(c, 1, {"title": "Later"}, k=k)
            assert r.status_code == 409 and r.json()["error"] == "archived"
        # 20 edits an hour per thread; an edit that changes nothing is not counted
        mail._counts.clear()
        for i in range(20):
            assert edit(c, 1, {"title": f"Title {i}"}, k=k).json()["changed"] is True, i
        assert edit(c, 1, {"title": "Title 19"}, k=k).json() == {"ok": True, "changed": False}
        r = edit(c, 1, {"title": "One more"}, key=me)
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        assert edit(c, 2, {"title": "Lou's own"}, k=k2).status_code == 200  # per thread
    clean(*seen, keys=(pk(me), pk(ada)))


def test_the_author_edits_their_own_comments_only(reqs):
    rsvc, relay, ada = reqs
    me = PrivateKey()
    with TestClient(create_app(rsvc)) as c:
        fid, card, k = ask(c, relay, me)
        _f2, card2, k2 = ask(c, relay, PrivateKey(), email="lou@example.org", name="Lou")
        theirs = reply(relay, card, MEMBER, "Which evenings?", 1)
        ok = {**H, "X-Thread-Key": k}
        r = c.post("/api/v1/requests/1/comments", json={"text": "Tuesday and Fryday."}, headers=ok)
        assert r.status_code == 201
        mine = [e for e in relay.events if e["kind"] == 1111][-1]
        r = c.post(
            "/api/v1/requests/2/comments", json={"text": "Hello"}, headers={**H, "X-Thread-Key": k2}
        )
        lous = [e for e in relay.events if e["kind"] == 1111][-1]
        sales.decide_offer(rsvc, fid, 50, "", "Ada")
        decision = [e for e in relay.events if e["kind"] == 1111][-1]
        assert decision["pubkey"] == GK and decision["id"] != mine["id"]
        # only the proven author sees which comments are theirs; every comment has its id
        got = get(c, "/requests/1").json()
        comments = [e for e in got["timeline"] if e["type"] == "comment"]
        assert [e["id"] for e in comments] == [theirs["id"], mine["id"]]
        assert all("mine" not in e and e["edited_at"] is None for e in comments)
        for proof in ({"X-Thread-Key": k},):
            got = get(c, "/requests/1", **proof).json()
            comments = [e for e in got["timeline"] if e["type"] == "comment"]
            assert [e.get("mine") for e in comments] == [None, True]
        got = call(c, me, "/requests/1", method="GET").json()
        assert [e.get("mine") for e in got["timeline"] if e["type"] == "comment"] == [None, True]
        got = get(c, "/requests/1", **{"X-Thread-Key": k2}).json()  # another thread's key
        assert not [e for e in got["timeline"] if e.get("mine")]
        # a member's comment, the belfry's own reply, another thread's comment, none
        for other in (theirs, decision):
            r = edit(c, 1, {"comment": other["id"], "text": "Changed"}, k=k)
            assert r.status_code == 403 and r.json()["error"] == "not_yours", other["content"]
        for gone in (lous["id"], "0" * 64, card["id"], "nonsense", 5):
            r = edit(c, 1, {"comment": gone, "text": "Changed"}, k=k)
            assert r.status_code == 404 and r.json()["error"] == "unknown", gone
        r = edit(c, 1, {"comment": mine["id"], "text": "Changed"}, key=PrivateKey())
        assert r.status_code == 403 and r.json()["error"] == "not_author"
        r = edit(c, 1, {"comment": mine["id"], "text": "Changed"})
        assert r.status_code == 403 and r.json()["error"] == "not_author"
        assert not [e for e in relay.events if e["kind"] == 1010]
        # their own
        r = edit(c, 1, {"comment": mine["id"], "text": "Tuesday and Fryday."}, k=k)
        assert r.json() == {"ok": True, "changed": False}
        r = edit(c, 1, {"comment": mine["id"], "text": " Tuesday and Friday. "}, key=me)
        assert r.status_code == 200 and r.json() == {"ok": True, "changed": True}
        (x,) = [e for e in relay.events if e["kind"] == 1010]
        assert x["pubkey"] == mine["pubkey"] and x["content"] == "Tuesday and Friday."
        assert x["tags"][:3] == [["h", "requests"], ["e", mine["id"]], ["k", "1111"]]
        assert ["E", card["id"], "", GK] in x["tags"] and ["K", "9"] in x["tags"]
        assert ["mail-from", "kim@example.org"] in x["tags"]
        assert [t for t in x["tags"] if t[0] == "e"] == [["e", mine["id"]]]
        assert not [t for t in x["tags"] if t[0] == "subject"]
        got = get(c, "/requests/1", **{"X-Thread-Key": k}).json()
        line = got["timeline"]
        (c2,) = [e for e in line if e["type"] == "comment" and e.get("mine")]
        assert c2["text"] == "Tuesday and Friday." and c2["edited_at"] == x["created_at"]
        assert c2["at"] == mine["created_at"] and c2["id"] == mine["id"]
        (e,) = [e for e in line if e["type"] == "edited"]
        assert e == {
            "type": "edited",
            "field": "comment",
            "by": {"name": "Kim Cook"},
            "at": x["created_at"],
            "comment": mine["id"],
        }
        assert got["edited_at"] is None and got["comments"] == 2  # the opening post is as it was
        assert got["state"] == "approved"
        assert "Fryday" not in json.dumps(got)
        public = get(c, "/requests/1").json()
        assert "Tuesday and Friday." in json.dumps(public) and '"mine"' not in json.dumps(public)
    clean(got, public, keys=(pk(me), pk(ada)))


def test_an_approved_request_edited_afterwards_stays_approved(reqs, world):
    rsvc, relay, _ada = reqs
    rsvc.settings.pass_page_oidc_client_secret = "x"
    rsvc.settings.pass_page_secret = "p" * 32
    me = PrivateKey()
    with TestClient(create_app(rsvc), base_url="https://belfry.belfry.test") as c:
        signed_in(c, rsvc, world[4])
        fid, card, k = ask(c, relay, me, title="Dinner for forty")
        assert edit(c, 1, {"body": "Before the answer."}, k=k).json()["changed"] is True
        sales.decide_offer(rsvc, fid, 100, "", "Ada")
        page = c.get("/sales", headers=H).text
        assert "Dinner for forty" in page and "Before the answer." in page
        assert "edited after approval" not in page
        f = sales.store(rsvc).offer(fid)
        f["decided_at"] = "2026-01-01T00:00:00Z"  # the answer came some time ago
        sales.store(rsvc).put_offer(f)
        assert edit(c, 1, {"title": "Dinner for sixty"}, k=k).json()["changed"] is True
        f = sales.store(rsvc).offer(fid)
        assert f["state"] == "accepted" and f["code"] and f["decided_percent"] == 100
        page = c.get("/sales", headers=H).text
        assert "Dinner for sixty" in page and "edited after approval" in page
        got = get(c, "/requests/1").json()
        assert got["state"] == "approved" and got["title"] == "Dinner for sixty"


def test_links_with_a_page_say_the_number(reqs, monkeypatch):
    rsvc, relay, _ada = reqs
    page = "https://belfry.test/in-kind"
    with TestClient(create_app(rsvc)) as c:
        # without a page: as they were
        fid, card, k = ask(c, relay, PrivateKey())
        f = sales.store(rsvc).offer(fid)
        assert f["url"] == f"https://belfry.test/requests/{card['id']}"
        assert f["my_url"] == f"{f['url']}?k={k}"
        monkeypatch.setattr(
            webthreads, "sites", lambda svc, fresh=False: {"requests": {**SITE, "page": page}}
        )
        webrequests.forget()
        fid2, card2, k2 = ask(c, relay, PrivateKey(), email="lou@example.org", name="Lou")
        f2 = sales.store(rsvc).offer(fid2)
        assert f2["url"] == f"{page}?id=2" and f2["my_url"] == f"{page}?id=2#k={k2}"
        assert f"{page}?id=2#k={k2}" in body(mailer.SENT[-1])
        # the older shapes the belfry serves move to the new one
        r = c.get(f"/requests/{card['id']}?k={k}", headers=APEX, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"{page}?id=1#k={k}"
        r = c.get(f"/requests/{card2['id']}", headers=APEX, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"{page}?id=2"
        # the number is the one the API answers to, with that key
        got = get(c, "/requests/2", **{"X-Thread-Key": k2}).json()
        assert got["id"] == card2["id"] and got["me"] == {"author": True}
        # a page on a channel that is not the requests channel: no number, the thread's id
        other = {**SITE, "channel": "ideas", "page": "https://belfry.test/ideas"}
        assert (
            webthreads.thread_url(rsvc, "ideas", other, "a" * 64, "b" * 24)
            == f"https://belfry.test/ideas?id={'a' * 64}#k={'b' * 24}"
        )


def test_the_html_page_and_an_authors_own_account(pages, monkeypatch):
    """The thread opened by an account of its own (the usual case): the edit is signed by that
    account's key through the signer, like its comments; the belfry's own page shows the new
    title and text with a small "edited" mark."""
    from belfry_onboarding import core, guests

    from .test_webthreads import WEB_IDENTITY, link_in, setting

    svc, relay = pages
    monkeypatch.setattr(webthreads, "web_identity", WEB_IDENTITY)
    monkeypatch.setattr(core, "_ensure_keycast_user", lambda *a, **kw: (EVE, "npub1eve", ""))
    monkeypatch.setattr(community, "member_signer", lambda svc, pid: (EVE, relay.sign_as(EVE)))
    monkeypatch.setattr(guests, "admitted", lambda svc, pass_id: True)
    setting(relay, MEMBER)
    with TestClient(create_app(svc), base_url="https://belfry.test") as c:
        c.post(
            "/ideas",
            data={
                "title": "A tool library",
                "text": "Let's.",
                "name": "Eve",
                "email": "eve@example.org",
            },
        )
        r = c.get(link_in(mailer.SENT[-1]), follow_redirects=False)
        mine = r.headers["location"].split("&")[0]
        card = next(e for e in relay.events if e["kind"] == 9)
        k = mine.split("?k=")[1]
        site = webthreads.sites(svc, fresh=True)["ideas"]
        assert "edited" not in c.get(mine).text
        c.post(mine, data={"text": "Thanks!"}, follow_redirects=False)
        comment = [e for e in relay.events if e["kind"] == 1111][-1]
        assert webthreads.edit(svc, site, card["id"], "0" * 24, title="No")[0] == "not_author"
        got = webthreads.edit(svc, site, card["id"], k, title="A tool shed", body="Let's build.")
        assert got == ("ok", {"title": "A tool shed", "body": "Let's build."})
        x = [e for e in relay.events if e["kind"] == 1010][-1]
        assert x["pubkey"] == card["pubkey"] == EVE  # hers: the chat counts it
        assert x["tags"][:4] == [
            ["h", "props"],
            ["e", card["id"]],
            ["k", "9"],
            ["subject", "A tool shed"],
        ]
        assert x["content"] == "**A tool shed**\n\nLet's build."
        assert webthreads.edit(svc, site, card["id"], k, title="A tool shed")[0] == "same"
        got = webthreads.edit(svc, site, card["id"], k, comment_id=comment["id"], text="Thank you!")
        assert got[0] == "ok"
        y = [e for e in relay.events if e["kind"] == 1010][-1]
        assert y["pubkey"] == EVE and y["tags"][:3] == [
            ["h", "props"],
            ["e", comment["id"]],
            ["k", "1111"],
        ]
        # the signer is down: nothing published
        monkeypatch.setattr(community, "member_signer", lambda svc, pid: None)
        assert webthreads.edit(svc, site, card["id"], k, title="Other")[0] == "unavailable"
        page = c.get(mine).text
        assert "<h1>A tool shed</h1>" in page and "Let&#x27;s build." in page
        assert "Thank you!" in page and "Thanks!" not in page and "A tool library" not in page
        assert page.count('<span class="wt-edited">edited</span>') == 2
        listed = c.get("/ideas").text
        assert "A tool shed" in listed and "A tool library" not in listed
        assert not re.search(r"npub1|[0-9a-f]{64}\W.*eve@", page)
