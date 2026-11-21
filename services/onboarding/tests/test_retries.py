"""Three rules of the website's API (docs/website-accounts.md "Retries", docs/requests.md):
the ``workshop`` request type; ``client_id`` on writes (a retry is answered again, never done
twice; retries.py); a relay that can't be read is 503 ``relay``, never an empty 200."""

# ruff: noqa: F811
from __future__ import annotations

import contextlib
import itertools
import json
import threading
import time

import httpx
import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    community,
    eventchat,
    events,
    followers,
    retries,
    sales,
    sitesessions,
    villagers,
    webrequests,
    webthreads,
)
from belfry_onboarding.app import create_app
from belfry_onboarding.register import Register
from belfry_onboarding.relay import RelayError

from .test_event_chat import CHAT, Room, kim_in, room  # noqa: F401 — fixtures
from .test_events import D
from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_holder_invites import nip98
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_offers import ASK as CARD_ASK
from .test_sales_offers import offers  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_site_sessions import ASK, LEO, guest, login, site  # noqa: F401
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair, tk  # noqa: F401 — fixtures
from .test_webrequests import H, ask, get, reqs  # noqa: F401

APEX = "https://belfry.test"
_tick = itertools.count(1)
_cid = itertools.count(1)


def cid() -> str:
    return f"try-{next(_cid):08d}"


def signed(c, key, path, data=None, method="POST", origin=APEX):
    """A signed request from the website (its Origin): what a browser would send."""
    c.app.state.api_seen._seen.clear()  # these tests sign more often than there are seconds
    at = int(time.time()) - 60 + next(_tick) % 50
    body = b"" if method == "GET" else json.dumps(data or {}).encode()
    headers = {
        **H,
        "Origin": origin,
        "Authorization": nip98(
            key, "https://belfry.belfry.test/api/v1" + path, method, body, created_at=at
        ),
    }
    if method == "GET":
        return c.get("/api/v1" + path, headers=headers)
    return c.post(
        "/api/v1" + path, content=body, headers={**headers, "Content-Type": "application/json"}
    )


def kinds(relay, kind: int) -> list[dict]:
    return [e for e in relay.events if e["kind"] == kind]


@pytest.fixture(autouse=True)
def _fresh():
    retries.forget()
    yield
    retries.forget()


# ── 1. the workshop type ────────────────────────────────────────────────────────────────────
def test_a_workshop_is_a_plain_request_and_a_talk_is_a_talk(reqs):
    rsvc, relay, ada = reqs
    assert sales.REQUEST_TYPES == (
        "enquiry",
        "sponsoring",
        "talk",
        "workshop",
        "meetup",
        "in_kind",
        "sponsored_ticket",
    )
    with TestClient(create_app(rsvc)) as c:
        me = PrivateKey()
        for field, value in (("product", "village-2027"), ("ticket", "x"), ("percent", 50)):
            r = call(
                c,
                me,
                "/offers",
                {**CARD_ASK, "product": None, "type": "workshop", field: value},
            )
            assert r.status_code == 422 and r.json()["error"] == "invalid", field
        fid, card, k = ask(c, relay, me, product=None, type="workshop")
        assert ["request-type", "workshop"] in card["tags"]
        assert ["subject", "Kim Cook: Workshop"] in card["tags"]  # the default title
        assert card["content"] == f"**Kim Cook: Workshop**\n\n_Workshop_\n\n{CARD_ASK['offer']}"
        assert not [t for t in card["tags"] if t[0] in ("percent", "product", "ticket")]
        assert sales.closable(rsvc, card) == "requests"  # the Close button works on it
        one = get(c, f"/requests/{card['id']}").json()
        assert (one["type"], one["kind"], one["state"]) == ("workshop", "other", "open")
        assert one["body"] == CARD_ASK["offer"]
        assert one["percent_asked"] is None and one["percent_approved"] is None
        got = call(c, me, f"/offers/{fid}", method="GET").json()
        assert (got["type"], got["kind"], got["product"], got["percent"]) == (
            "workshop",
            None,
            None,
            None,
        )
        # a talk is "Talk" now
        _f, talk, _k = ask(c, relay, PrivateKey(), product=None, type="talk", email="t@example.org")
        assert talk["content"].startswith("**Kim Cook: Talk**\n\n_Talk_\n\n")
        listed = get(c, "/requests?type=workshop").json()
        assert [i["id"] for i in listed["items"]] == [card["id"]]
        assert listed["counts"] == {"open": 1, "approved": 0, "closed": 0}
        assert [i["id"] for i in get(c, "/requests?type=talk").json()["items"]] == [talk["id"]]
        assert get(c, "/requests?type=workshops").status_code == 422
        # organisers close it like any plain request
        assert sales.close_request(rsvc, "requests", card["id"], True, pk(ada)) is True
        webrequests.forget()
        assert get(c, f"/requests/{card['id']}").json()["state"] == "closed"
    assert sales.message_type({"tags": [["request-type", "workshop"]]}) == "workshop"


def test_an_older_talk_card_still_reads_as_a_talk(reqs):
    rsvc, relay, _ada = reqs
    with TestClient(create_app(rsvc)) as c:
        _fid, card, k = ask(
            c, relay, PrivateKey(), product=None, type="talk", title="A talk about bees"
        )
        # as it was written before workshops were a type of their own
        card["content"] = card["content"].replace("_Talk_", "_Talk or workshop_")
        assert "_Talk or workshop_" in card["content"]
        webrequests.forget()
        one = get(c, f"/requests/{card['id']}").json()
        assert one["type"] == "talk" and one["title"] == "A talk about bees"
        assert one["body"] == CARD_ASK["offer"]  # the old line is not the text either
        assert get(c, "/requests?type=talk").json()["items"][0]["id"] == card["id"]
        # an edit keeps the line the card has
        r = c.post(
            f"/api/v1/requests/{card['id']}/edit",
            json={"body": "Thirty minutes."},
            headers={**H, "X-Thread-Key": k},
        )
        assert r.status_code == 200 and r.json()["changed"] is True, r.text
        edit = kinds(relay, 1010)[-1]
        assert edit["content"] == "**A talk about bees**\n\n_Talk or workshop_\n\nThirty minutes."
        webrequests.forget()
        assert get(c, f"/requests/{card['id']}").json()["body"] == "Thirty minutes."


# ── 2. client_id ────────────────────────────────────────────────────────────────────────────
def village(c, r: Room):
    """Kim (a guest with a day ticket, her request #1, a public profile) and Leo (a guest)."""
    kim, kim_pk = kim_in(c, r)
    leo = guest(c, LEO, "Leo")
    assert call(c, kim, "/offers", ASK).status_code == 201
    assert call(c, kim, "/site/profile", {"visibility": "public"}).status_code == 200
    slug = call(c, kim, "/site/me", method="GET").json()["slug"]
    assert slug
    forget_all()
    return kim, kim_pk, leo, slug


def forget_all() -> None:
    webrequests.forget()
    webrequests.PER_IP.reset()
    eventchat.forget()
    eventchat.forget_people()
    villagers.forget()
    villagers.forget_names()
    villagers.READS.__init__(10000, 60)
    sitesessions._ticket.clear()
    events._slug_miss.clear()
    events.forget_reads()


def twice(c, key, path, data):
    """The same call with the same client_id, twice: (first answer, second answer)."""
    body = {**data, "client_id": cid()}
    first = signed(c, key, path, body)
    second = signed(c, key, path, body)
    assert retries.REPLAYED not in first.headers, path
    assert second.headers.get(retries.REPLAYED) == "1", (path, second.text)
    assert second.status_code == first.status_code and second.json() == first.json(), path
    # the page's script may read the header
    assert "X-Belfry-Replayed" in second.headers["access-control-expose-headers"]
    assert second.headers["access-control-allow-origin"] == APEX
    return first, second


def test_a_repeated_write_is_answered_again_and_done_once(room):
    rsvc, w = room.rsvc, room.w
    with TestClient(create_app(rsvc)) as c:
        kim, kim_pk, leo, _slug = village(c, room)
        cards = len(kinds(w.grp, 9))
        # a comment
        first, _ = twice(c, leo, "/requests/1/comments", {"text": "Which evenings?"})
        assert first.status_code == 201 and first.json() == {"ok": True}
        assert len(kinds(w.grp, 1111)) == 1
        # a reaction
        first, _ = twice(c, leo, "/requests/1/react", {"emoji": "👍"})
        assert first.status_code == 200 and first.json()["reactions"][0]["count"] == 1
        assert len(kinds(w.grp, 7)) == 1
        # an edit: the second answer still says "changed" (it is the first one, again)
        first, _ = twice(c, kim, "/requests/1/edit", {"title": "Dinner for forty"})
        assert first.json() == {"ok": True, "changed": True}
        assert len(kinds(w.grp, 1010)) == 1
        # a message in the event's conversation
        first, second = twice(c, kim, CHAT, {"text": "See you there"})
        assert first.status_code == 201 and second.json()["id"] == first.json()["id"]
        assert len([e for e in kinds(w.grp, 9) if e["content"] == "See you there"]) == 1
        # the profile
        first, _ = twice(c, kim, "/site/profile", {"about": "I cook."})
        assert first.json() == {"ok": True, "changed": True}
        assert len([e for e in kinds(w.grp, 0) if e["pubkey"] == kim_pk]) == 1
        # the notification settings
        first, _ = twice(c, kim, "/site/notifications", {"events": "off"})
        assert first.status_code == 200 and first.json()["items"]
        # a request, by a website session …
        offers_before = len(sales.store(rsvc).offers())
        first, _ = twice(c, leo, "/offers", {**ASK, "product": None, "type": "workshop"})
        assert first.status_code == 201
        assert len(kinds(w.grp, 9)) == cards + 2  # the chat message and one new card
        # … and by a key with no account: the signing key is whose retry it is
        mails = len(_sent())
        first, _ = twice(
            c,
            PrivateKey(),
            "/offers",
            {**CARD_ASK, "product": None, "type": "talk", "email": "zoe@example.org"},
        )
        assert first.status_code == 201 and len(_sent()) == mails + 1
        made = sales.store(rsvc).offers()
        assert len(made) == offers_before + 2
        # the id is in no event and no record
        everything = json.dumps([w.grp.events, w.comm.events, made])
        assert "client_id" not in everything and "try-0" not in everything


def _sent():
    from belfry_onboarding import mailer

    return mailer.SENT


def test_whose_retry_it_is_what_is_kept_and_what_is_refused(room):
    rsvc, w = room.rsvc, room.w
    with TestClient(create_app(rsvc)) as c:
        kim, _kim_pk, leo, _slug = village(c, room)
        one = cid()
        # not 8–64 of A–Z a–z 0–9 and -: 422, nothing posted
        for bad in ("short", "x" * 65, "has space 123", "under_score1", 12345678, ""):
            r = signed(c, leo, "/requests/1/comments", {"text": "Hello", "client_id": bad})
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        assert not kinds(w.grp, 1111)
        # null is no client_id
        r = signed(c, leo, "/requests/1/comments", {"text": "Hello", "client_id": None})
        assert r.status_code == 201 and retries.REPLAYED not in r.headers
        # the same id by another account, and on another call: each runs for real
        assert (
            signed(c, leo, "/requests/1/comments", {"text": "A", "client_id": one}).status_code
            == 201
        )
        r = signed(c, kim, "/requests/1/comments", {"text": "B", "client_id": one})
        assert r.status_code == 201 and retries.REPLAYED not in r.headers
        r = signed(c, leo, "/requests/1/react", {"emoji": "🎉", "client_id": one})
        assert r.status_code == 200 and retries.REPLAYED not in r.headers
        assert len(kinds(w.grp, 1111)) == 3
        # bodies are not compared: the first answer, whatever is sent now
        r = signed(c, leo, "/requests/1/comments", {"text": "Something else", "client_id": one})
        assert r.status_code == 201 and r.headers[retries.REPLAYED] == "1"
        assert len(kinds(w.grp, 1111)) == 3
        # a replay is still signed: nothing without a signature, nothing with a bad one
        r = c.post("/api/v1/requests/1/comments", json={"text": "A", "client_id": one}, headers=H)
        assert r.status_code == 403 and retries.REPLAYED not in r.headers
        r = c.post(
            "/api/v1/requests/1/comments",
            json={"text": "A", "client_id": one},
            headers={**H, "Authorization": "Nostr bm9wZQ=="},
        )
        assert r.status_code == 401 and retries.REPLAYED not in r.headers
        # a replay uses up nothing: far more of them than comments an hour
        for _ in range(webrequests.COMMENTS_PER_HOUR + 5):
            r = signed(c, leo, "/requests/1/comments", {"text": "A", "client_id": one})
            assert r.status_code == 201 and r.headers[retries.REPLAYED] == "1"
        # an error is not kept: the retry runs for real
        two = cid()
        r = signed(c, leo, "/requests/1/comments", {"text": " ", "client_id": two})
        assert r.status_code == 422
        r = signed(c, leo, "/requests/1/comments", {"text": "Now with words", "client_id": two})
        assert r.status_code == 201 and retries.REPLAYED not in r.headers
        assert len(kinds(w.grp, 1111)) == 4
    # after a restart: the register still has it (a new register on the same file, a new app)
    rsvc.odoo = Register(rsvc.settings.register_path, rsvc.settings)
    retries.forget()
    with TestClient(create_app(rsvc)) as c:
        r = signed(c, leo, "/requests/1/comments", {"text": "A", "client_id": one})
        assert r.status_code == 201 and r.headers[retries.REPLAYED] == "1"
        assert len(kinds(w.grp, 1111)) == 4
        # … for a day: an older answer is gone, and pruned with the next one kept
        rsvc.odoo._db.execute("UPDATE retries SET at = at - ?", (retries.KEEP_SECONDS + 5,))
        r = signed(c, leo, "/requests/1/comments", {"text": "A day later", "client_id": one})
        assert r.status_code == 201 and retries.REPLAYED not in r.headers
        assert len(kinds(w.grp, 1111)) == 5
        assert rsvc.odoo._db.execute("SELECT count(*) FROM retries").fetchone()[0] == 1


def test_the_authors_own_link_retries_too(reqs):
    """An author with no account, proven by the thread's own key: the key is whose retry."""
    rsvc, relay, _ada = reqs
    with TestClient(create_app(rsvc)) as c:
        _fid, card, k = ask(c, relay, PrivateKey())
        body = {"text": "One more thing.", "client_id": cid()}
        path = f"/api/v1/requests/{card['id']}/comments"
        first = c.post(path, json=body, headers={**H, "X-Thread-Key": k})
        again = c.post(path, json=body, headers={**H, "X-Thread-Key": k})
        assert first.status_code == again.status_code == 201
        assert retries.REPLAYED not in first.headers and again.headers[retries.REPLAYED] == "1"
        assert len(kinds(relay, 1111)) == 1
        # another key: no answer of the author's, and no comment
        other = c.post(path, json=body, headers={**H, "X-Thread-Key": "0" * 24})
        assert other.status_code == 403 and retries.REPLAYED not in other.headers


def test_a_repeat_while_the_first_still_runs_is_in_progress(room):
    rsvc, w = room.rsvc, room.w
    with TestClient(create_app(rsvc)) as c:
        _kim, _kim_pk, leo, _slug = village(c, room)
        entered, release = threading.Event(), threading.Event()
        real = w.grp.publish

        def slow(event, sign):
            entered.set()
            assert release.wait(10)
            real(event, sign)

        w.grp.publish = slow
        body = {"text": "Hello", "client_id": cid()}
        got: list = []
        t = threading.Thread(
            target=lambda: got.append(signed(c, leo, "/requests/1/comments", body))
        )
        t.start()
        try:
            assert entered.wait(10)
            r = signed(c, leo, "/requests/1/comments", body)
            assert r.status_code == 409 and r.json()["error"] == "in_progress"
            assert retries.REPLAYED not in r.headers
        finally:
            release.set()
            t.join(15)
            del w.grp.publish
        assert got[0].status_code == 201 and retries.REPLAYED not in got[0].headers
        r = signed(c, leo, "/requests/1/comments", body)
        assert r.status_code == 201 and r.headers[retries.REPLAYED] == "1"
        assert len(kinds(w.grp, 1111)) == 1


def test_without_the_own_register_answers_are_kept_in_memory():
    class Svc:
        odoo = object()  # Odoo: no table of ours

    now = int(time.time())
    assert retries._get(Svc, "k", now) is None
    retries._put(Svc, "k", now, 201, '{"ok": true}')
    assert retries._get(Svc, "k", now) == (201, '{"ok": true}')
    assert retries._get(Svc, "k", now + retries.KEEP_SECONDS + 1) is None
    retries._put(Svc, "k2", now + retries.KEEP_SECONDS + 1, 200, "{}")  # the old one goes
    assert set(retries._memory) == {"k2"}


# ── 3. a relay that can't be read ───────────────────────────────────────────────────────────
DOWN = (
    ConnectionRefusedError(111, "Connection refused"),
    TimeoutError("timed out"),
    RelayError("query closed: error: could not connect to the database"),
    RelayError("timed out waiting for the relay's EOSE"),
)


@contextlib.contextmanager
def down(w, exc: Exception, which=("comm", "grp"), what=("query",)):
    """The relays don't answer (reads; ``what=("publish",)``: writes)."""

    def boom(*_a, **_k):
        raise exc

    relays = [getattr(w, name) for name in which]
    for r in relays:
        for attr in what:
            setattr(r, attr, boom)
    try:
        yield
    finally:
        for r in relays:
            for attr in what:
                delattr(r, attr)


def is_relay_503(r, cors: bool = True) -> None:
    assert r.status_code == 503, r.text
    assert r.json()["error"] == "relay", r.text
    assert r.headers["retry-after"] == "5"
    assert r.headers["cache-control"] == "no-store"
    if cors:
        assert r.headers["access-control-allow-origin"] == APEX
        assert "Retry-After" in r.headers["access-control-expose-headers"]


def test_every_read_is_503_relay_while_the_relays_are_down_and_never_cached(room):
    rsvc, w = room.rsvc, room.w
    with TestClient(create_app(rsvc)) as c:
        kim, _kim_pk, leo, slug = village(c, room)
        assert (
            signed(c, leo, "/requests/1/comments", {"text": "Which evenings?"}).status_code == 201
        )
        assert signed(c, kim, CHAT, {"text": "See you there"}).status_code == 201

        def anon(path):
            return lambda: c.get("/api/v1" + path, headers={**H, "Origin": APEX})

        def as_kim(path):
            return lambda: signed(c, kim, path, method="GET")

        reads = {
            "/requests": anon("/requests"),
            "/requests?type=in_kind": anon("/requests?type=in_kind"),
            "/requests/1": anon("/requests/1"),
            "/requests/1 (signed)": as_kim("/requests/1"),
            "/events": anon("/events"),
            f"/events/{D}": anon(f"/events/{D}"),
            f"/events/{D} (signed)": as_kim(f"/events/{D}"),
            f"/events/{D}/chat": as_kim(CHAT),
            f"/events/{D}/photos": as_kim(f"/events/{D}/photos"),
            f"/events/{D}/links": as_kim(f"/events/{D}/links"),
            f"/events/{D}/participants": as_kim(f"/events/{D}/participants"),
            f"/events/{D}/activity": anon(f"/events/{D}/activity"),
            "/villagers": anon("/villagers"),
            f"/villagers/{slug}": anon(f"/villagers/{slug}"),
            f"/villagers/{slug} (signed)": as_kim(f"/villagers/{slug}"),
            "/site/me": as_kim("/site/me"),
        }
        forget_all()
        good = {}
        for name, read in reads.items():
            r = read()
            assert r.status_code == 200, (name, r.text)
            good[name] = r.json()
        assert good["/requests"]["items"] and good["/events"]["events"]
        assert good["/villagers"]["villagers"] and good[f"/events/{D}/chat"]["messages"]
        assert good["/site/me"]["tickets"] and good["/site/me"]["requests"]
        assert good[f"/events/{D}/participants"]["count"] == 2  # Ada, who hosts, and Kim
        for exc in DOWN:
            for name, read in reads.items():
                forget_all()  # nothing good is kept: the relay has to be asked
                with down(w, exc):
                    is_relay_503(read())
                # the relay is back: the failure was not kept, the answer is whole
                r = read()
                assert r.status_code == 200, (name, exc, r.text)
                assert r.json() == good[name], (name, exc)
        # a good answer may still be served while it is kept (the requests: 15 seconds)
        forget_all()
        assert reads["/requests"]().status_code == 200
        with down(w, DOWN[0]):
            assert reads["/requests"]().json() == good["/requests"]


def test_one_relay_down_is_enough(room):
    """Each relay holds a part of an answer: without it the answer would only look complete."""
    rsvc, w = room.rsvc, room.w
    with TestClient(create_app(rsvc)) as c:
        kim, _kim_pk, _leo, slug = village(c, room)
        groups_only = ("/requests", "/requests/1", CHAT, f"/events/{D}/participants")
        community_only = ("/events", f"/events/{D}/activity")
        both = (f"/events/{D}", "/site/me", f"/villagers/{slug}", "/villagers")
        for which, paths in (
            (("grp",), (*groups_only, *both)),
            (("comm",), (*community_only, *both)),
        ):
            for path in paths:
                forget_all()
                with down(w, DOWN[0], which):
                    is_relay_503(signed(c, kim, path, method="GET"))
                assert signed(c, kim, path, method="GET").status_code == 200, path


def test_a_write_that_cant_reach_the_relay_is_503_relay_and_not_remembered(room):
    rsvc, w = room.rsvc, room.w
    with TestClient(create_app(rsvc)) as c:
        kim, kim_pk, leo, _slug = village(c, room)
        writes = (
            (leo, "/requests/1/comments", {"text": "Which evenings?"}, 201, 1111),
            (leo, "/requests/1/react", {"emoji": "👍"}, 200, 7),
            (kim, "/requests/1/edit", {"body": "Two evenings, forty people."}, 200, 1010),
            (kim, CHAT, {"text": "See you there"}, 201, 9),
            (kim, "/site/profile", {"about": "I cook."}, 200, 0),
            (leo, "/offers", {**ASK, "product": None, "type": "workshop"}, 201, 9),
        )
        for key, path, data, status, kind in writes:
            body = {**data, "client_id": cid()}
            # (a request counts towards its author's three a day before it is posted)
            for exc in DOWN[:1] if path == "/offers" else DOWN[:3]:
                forget_all()
                before = len(w.grp.events)
                with down(w, exc, what=("publish",)):
                    is_relay_503(signed(c, key, path, body))
                assert len(w.grp.events) == before, path
            # the same client_id again, the relay back: done for real, once, and then kept
            forget_all()
            r = signed(c, key, path, body)
            assert r.status_code == status and retries.REPLAYED not in r.headers, (path, r.text)
            assert len(w.grp.events) == before + 1 and w.grp.events[-1]["kind"] == kind, path
            r = signed(c, key, path, body)
            assert r.status_code == status and r.headers[retries.REPLAYED] == "1", path
            assert len(w.grp.events) == before + 1, path
        # a write whose reads fail: the same answer (nothing is written on a guess)
        assert signed(c, kim, "/site/notifications", {"events": "off"}).status_code == 200
        forget_all()
        with down(w, DOWN[1]):
            for key, path, data, _status, _kind in writes:
                # (a request reads only the channel's page setting, which this belfry of the
                # tests has by heart: test_the_channels_pages_are_never_believed…)
                if path != "/offers":
                    is_relay_503(signed(c, key, path, data))
            # the events switch asks the relay whose tickets they hold
            is_relay_503(signed(c, kim, "/site/notifications", {"events": "asap"}))
        r = signed(c, kim, "/site/notifications", {"events": "asap"})
        assert r.status_code == 200 and r.json()["items"][-1]["value"] == "asap", r.text


def test_the_signer_failing_is_not_the_relay(room, monkeypatch):
    rsvc = room.rsvc
    with TestClient(create_app(rsvc)) as c:
        _kim, _kim_pk, leo, slug = village(c, room)

        def gone(svc, pid):
            raise httpx.ConnectError("signer down")

        monkeypatch.setattr(community, "member_signer", gone)
        r = signed(c, leo, "/requests/1/comments", {"text": "Hello", "client_id": cid()})
        assert r.status_code == 503 and r.json()["error"] == "signer", r.text
        assert "retry-after" not in r.headers
        # reading goes on: who has a username is the signer's to say, a name degrades
        monkeypatch.setattr(
            rsvc.keycast,
            "pubkey_for_name",
            lambda name: (_ for _ in ()).throw(httpx.ReadTimeout("x")),
        )
        forget_all()
        r = c.get(f"/api/v1/villagers/{slug}", headers=H)
        assert r.status_code == 200 and r.json()["name"] == "Kim Cook"
        assert c.get("/api/v1/requests/1", headers=H).status_code == 200


def test_what_a_relay_failure_is():
    from websockets.exceptions import InvalidURI

    for exc in (*DOWN, OSError("no route"), InvalidURI("x", "bad"), retries.RelayDown()):
        assert retries.is_relay(exc), exc
    for exc in (httpx.ConnectError("x"), httpx.ReadTimeout("x"), ValueError("x"), KeyError("x")):
        assert not retries.is_relay(exc), exc

    def boom():
        raise DOWN[0]

    def degrade():
        try:
            boom()
        except Exception as exc:  # noqa: BLE001
            retries.soft(exc)
            return []

    assert degrade() == []  # a page, the console, an hourly job: as before
    with pytest.raises(retries.RelayDown):
        retries.strictly(degrade)  # an API call
    with pytest.raises(retries.RelayDown):
        retries.strictly(boom)
    with pytest.raises(ValueError):
        retries.strictly(lambda: int("x"))
    assert retries.strict() is False  # and it is over afterwards


def test_the_channels_pages_are_never_believed_after_a_failed_read(world, monkeypatch):
    """webthreads.sites keeps the last known settings when the relay fails: fine for a page,
    but the API must not take "no pages" for an answer (it would say 404 ``none``)."""

    class Relay:
        fail: Exception | None = DOWN[0]
        asked = 0

        def query(self, filters, sign):
            self.asked += 1
            if self.fail:
                raise self.fail
            return []

    rsvc = world[0]
    relay = Relay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (relay, None))
    monkeypatch.setattr(sales, "own", lambda svc: True)
    monkeypatch.setattr(sales, "settings", lambda svc, *a: {"requests_channel": "requests"})
    webthreads._cache.update(at=0.0, sites={}, ok=True)
    try:
        assert webthreads.sites(rsvc) == {}  # a page: the last known ones (none), kept a while
        assert webthreads.sites(rsvc) == {} and relay.asked == 1
        assert sales.requests_site(rsvc) is None
        for fn in (webthreads.sites, sales.requests_site, sitesessions.requests_channel):
            with pytest.raises(retries.RelayDown):  # the API asks again, and says so
                retries.strictly(fn, rsvc)
        asked = relay.asked
        assert asked >= 3
        relay.fail = None
        assert retries.strictly(webthreads.sites, rsvc) == {}  # asked again: an answer now
        assert relay.asked == asked + 1
        assert retries.strictly(webthreads.sites, rsvc) == {} and relay.asked == asked + 1
        with TestClient(create_app(rsvc)) as c:
            relay.fail = DOWN[1]
            webthreads._cache.update(at=0.0, sites={}, ok=True)
            webrequests.forget()
            webrequests.PER_IP.reset()
            is_relay_503(c.get("/api/v1/requests", headers={**H, "Origin": APEX}), cors=False)
            relay.fail = None
            r = c.get("/api/v1/requests", headers=H)
            assert r.status_code == 404 and r.json()["error"] == "none"  # the relay said so
    finally:
        webthreads._cache.update(at=0.0, sites={}, ok=True)


def test_a_shared_read_that_fails_is_not_kept(monkeypatch):
    monkeypatch.setattr(events, "READ_SECONDS", 60.0)
    monkeypatch.setattr(events.time, "sleep", lambda s: None)
    events.forget_reads()
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) <= 2:
            raise TimeoutError("timed out")
        return [{"id": "e1"}]

    with pytest.raises(TimeoutError):
        events._shared("q", fetch)  # tried twice, then it fails
    assert len(calls) == 2
    assert events._shared("q", fetch) == [{"id": "e1"}]  # asked again: nothing was kept
    assert events._shared("q", fetch) == [{"id": "e1"}] and len(calls) == 3  # the good one is
    events.forget_reads()


def test_an_address_is_not_nobodys_because_the_relay_was_down(fair, monkeypatch):
    rsvc, w, ada, *_ = fair
    events._slug_miss.clear()
    w.comm.events[0]["tags"].append(["slug", "garden-day-a1b2"])
    with down(w, DOWN[0]), pytest.raises(ConnectionRefusedError):
        events.find_by_slug(rsvc, "garden-day-a1b2")
    ev = events.find_by_slug(rsvc, "garden-day-a1b2")  # not "asked a minute ago: nobody"
    assert ev is not None and ev.d == D
