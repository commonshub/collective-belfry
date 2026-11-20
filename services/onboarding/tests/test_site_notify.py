"""Website accounts: which e-mails someone gets (sitenotify.py, docs/website-accounts.md §3),
set from the website and from the mails' own page."""

# ruff: noqa: F811
from __future__ import annotations

import datetime as dt
import time

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    events,
    mailer,
    mentions,
    sitenotify,
    sitesessions,
    webrequests,
    webthreads,
)
from belfry_onboarding.app import create_app

from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_site_sessions import (  # noqa: F401 — fixtures
    ASK,
    KIM,
    LEO,
    VILLAGE,
    H,
    admit,
    guest,
    login,
    sign_as,
    site,
    village,
)
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair  # noqa: F401 — fixtures

N = "/site/notifications"


def get(c, key):
    r = call(c, key, N, method="GET")
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


def text_of(msg) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def stamp(rsvc, w, back: int) -> None:
    """The last event on the groups relay, BACK seconds ago (rounds read by time)."""
    w.grp.events[-1]["created_at"] = int(time.time()) - back
    webrequests.forget()


# ── the settings ────────────────────────────────────────────────────────────────────────────
def test_the_settings_an_account_really_has(site, monkeypatch):
    rsvc, _w, _ada = site
    person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        assert call(c, PrivateKey(), N, method="GET").status_code == 401
        assert call(c, PrivateKey(), N, {"replies": "off"}).json()["error"] == "no_session"
        kim = guest(c)
        r = call(c, kim, N, method="GET")
        assert r.headers["cache-control"] == "no-store"
        assert r.json() == {
            "items": [
                {
                    "id": "replies",
                    "value": "asap",
                    "default": "asap",
                    "choices": ["asap", "daily", "weekly", "off"],
                },
                # someone who came in from the web has the mention digest off (webthreads.py)
                {
                    "id": "mentions",
                    "value": "off",
                    "default": "daily",
                    "choices": ["daily", "weekly", "off"],
                },
                {
                    "id": "requests",
                    "value": "off",
                    "default": "off",
                    "choices": ["asap", "daily", "off"],
                },
                {"id": "events", "value": "asap", "default": "asap", "choices": ["asap", "off"]},
            ]
        }
        # nothing the belfry doesn't send: no messages, no digest of the activity
        bk = PrivateKey()
        login(c, bk, "bob@belfry.test")
        mine = get(c, bk)
        assert list(mine) == ["replies", "mentions", "requests", "events"]
        assert mine["mentions"]["value"] == "daily", "a member's digest is what it was"
        acct = sitesessions.site_account(rsvc, pk(bk))
        assert rsvc.odoo.params_under("belfry.notify.") == {}, "reading stores nothing"
        # an unknown id, a choice the id doesn't take, nothing at all: nothing is changed
        for bad in (
            {},
            {"digest": "daily"},
            {"mentions": "asap"},
            {"replies": 1},
            {"events": "daily"},
        ):
            r = call(c, bk, N, {"requests": "asap", **bad} if bad else bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        assert get(c, bk)["requests"]["value"] == "off"
        # one store per setting: the mentions' own, which their footer page changes too
        r = call(c, bk, N, {"mentions": "weekly", "requests": "daily"})
        assert r.status_code == 200, r.text
        got = {i["id"]: i["value"] for i in r.json()["items"]}
        assert got == {
            "replies": "asap",
            "mentions": "weekly",
            "requests": "daily",
            "events": "asap",
        }
        assert mentions.prefs(rsvc, acct["partner"])["f"] == "weekly"
        tok = mentions.token(rsvc, acct["partner"])
        assert c.post(f"/mentions/{tok}", data={"f": "off"}, headers=H).status_code in (200, 303)
        assert get(c, bk)["mentions"]["value"] == "off"
        # a member who can't read the requests channel has no "requests"
        monkeypatch.setattr(sitesessions, "has_channel", lambda *a: False)
        assert "requests" not in get(c, bk)
        r = call(c, bk, N, {"requests": "asap"})
        assert r.status_code == 422
        # no public page anywhere: no "replies"
        monkeypatch.setattr(webthreads, "sites", lambda svc, fresh=False: {})
        assert list(get(c, bk)) == ["mentions", "events"]
        # no proven address: nothing at all
        rsvc.odoo.write("res.partner", [acct["partner"]], {"email": False})
        assert get(c, bk) == {}


def test_the_page_behind_the_mails_link(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        url = sitenotify.settings_url(rsvc, acct["partner"])
        assert url.startswith("https://belfry.belfry.test/notifications/")
        path = url.removeprefix("https://belfry.belfry.test")
        page = c.get(path, headers=H)
        assert page.status_code == 200 and "New requests" in page.text
        assert KIM not in page.text
        # opening it changes nothing (mail scanners follow links); the button does
        assert get(c, kim)["replies"]["value"] == "asap"
        r = c.post(path, data={"id": "replies", "value": "off"}, headers=H, follow_redirects=False)
        assert r.status_code == 303
        assert get(c, kim)["replies"]["value"] == "off"
        assert c.post(path, data={"id": "replies", "value": "x"}, headers=H).status_code == 404
        assert c.get(path[:-1] + ("0" if path[-1] != "0" else "1"), headers=H).status_code == 404
        # nothing from the address is shown
        assert "hello-there" not in c.get(path + "?msg=hello-there", headers=H).text


# ── replies ─────────────────────────────────────────────────────────────────────────────────
def test_replies_reach_the_author_and_who_commented_as_they_chose(site):
    rsvc, w, _ada = site
    person(rsvc, "bob@belfry.test", "Bob")
    with TestClient(create_app(rsvc)) as c:
        kim, leo = guest(c), guest(c, LEO, "Leo")
        bk = PrivateKey()
        login(c, bk, "bob@belfry.test")
        leo_id = sitesessions.site_account(rsvc, pk(leo))["partner"]
        assert call(c, kim, "/offers", {**ASK, "title": "Dinner for forty"}).status_code == 201
        stamp(rsvc, w, 900)
        assert call(c, leo, "/requests/1/comments", {"text": "Which evenings?"}).status_code == 201
        stamp(rsvc, w, 800)
        now = int(time.time())
        rsvc.odoo.set_param("belfry.web.notified", str(now - 4000))
        mailer.SENT.clear()
        # Leo's comment: the author hears of it; Leo doesn't hear of his own
        assert webthreads.notify(rsvc, now=now - 300) == 1
        [m] = mailer.SENT
        assert m["To"] == KIM and "Leo: Which evenings?" in text_of(m)
        assert "you opened a thread" in text_of(m) and "/notifications/" in text_of(m)
        assert "/notifications/" in m["List-Unsubscribe"]
        # a member answers: the author and the commenter each get one mail
        rsvc.odoo.set_param("belfry.web.notified", str(now - 4000))
        assert call(c, bk, "/requests/1/comments", {"text": "Welcome!"}).status_code == 201
        stamp(rsvc, w, 200)
        mailer.SENT.clear()
        assert webthreads.notify(rsvc, now=now - 100) == 2
        by = {m["To"]: text_of(m) for m in mailer.SENT}
        assert set(by) == {KIM, LEO}
        assert "Welcome!" in by[LEO] and "Which evenings?" not in by[LEO]
        assert "you opened or commented on a thread" in by[LEO]
        assert "#k=" not in by[LEO] and "?k=" not in by[LEO], "the author's link is the author's"
        assert "?k=" in by[KIM]
        # Bob commented too, and is a member with the default: he hears of the next one
        # Leo turns them off; Kim wants them once a day
        assert call(c, leo, N, {"replies": "off"}).status_code == 200
        assert call(c, kim, N, {"replies": "daily"}).status_code == 200
        assert call(c, bk, "/requests/1/comments", {"text": "Tuesday?"}).status_code == 201
        w.grp.events[-1]["created_at"] = now + 10
        webrequests.forget()
        mailer.SENT.clear()
        assert webthreads.notify(rsvc, now=now + 4000) == 0
        assert mailer.SENT == []
        # the morning's round carries Kim's
        morning = dt.datetime.fromtimestamp(now + 5000, dt.UTC)
        assert sitenotify.daily(rsvc, morning)["replies"] == 1
        [m] = mailer.SENT
        assert m["To"] == KIM and "Tuesday?" in text_of(m) and "Welcome!" not in text_of(m)
        assert "once a day" in text_of(m)
        mailer.SENT.clear()
        assert sitenotify.daily(rsvc, morning + dt.timedelta(days=1))["replies"] == 0
        later = now + 5000 + 86400  # that second morning
        # back to "asap" with something waiting for the next digest: the next round has it
        assert call(c, bk, "/requests/1/comments", {"text": "Or Wednesday."}).status_code == 201
        w.grp.events[-1]["created_at"] = later + 200
        webrequests.forget()
        rsvc.odoo.set_param("belfry.web.notified", str(later + 1000))
        assert call(c, kim, N, {"replies": "asap"}).status_code == 200
        assert webthreads.notify(rsvc, now=later + 4700) == 1
        assert "Or Wednesday." in text_of(mailer.SENT[-1])
        mailer.SENT.clear()
        assert webthreads.notify(rsvc, now=later + 8400) == 0, "once"
        assert leo_id and not mailer.SENT


def test_a_members_reply_in_the_chat_subscribes_them_to_nothing(site):
    rsvc, w, _ada = site
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    mia, _ = person(rsvc, "mia@belfry.test", "Mia")
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        assert call(c, kim, "/offers", ASK).status_code == 201
        card = next(e for e in w.grp.events if e["kind"] == 9)
        now = int(time.time())
        for i, who in enumerate((bob, mia)):  # written in the chat: no ``client`` tag
            w.grp.events.append(
                sign_as(pk(who))(
                    {
                        "kind": 1111,
                        "created_at": now - 100 + i,
                        "content": "From the chat",
                        "tags": [["h", "requests"], ["E", card["id"]], ["e", card["id"]]],
                    }
                )
            )
        rsvc.odoo.set_param("belfry.web.notified", str(now - 4000))
        mailer.SENT.clear()
        assert webthreads.notify(rsvc, now=now) == 1
        assert [m["To"] for m in mailer.SENT] == [KIM], "as before there was a setting"


# ── new requests ────────────────────────────────────────────────────────────────────────────
def test_new_requests_only_for_who_asked(site):
    rsvc, w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        kim, leo = guest(c), guest(c, LEO, "Leo")
        now = int(time.time())
        rsvc.odoo.set_param("belfry.web.notified", str(now - 4000))
        assert call(c, kim, "/offers", {**ASK, "title": "Dinner for forty"}).status_code == 201
        stamp(rsvc, w, 100)
        mailer.SENT.clear()
        webthreads.notify(rsvc, now=now)
        assert mailer.SENT == [], "off unless asked for"
        # Leo and Kim ask; Kim's own request is no news to Kim
        assert call(c, leo, N, {"requests": "asap"}).status_code == 200
        assert call(c, kim, N, {"requests": "asap"}).status_code == 200
        rsvc.odoo.set_param("belfry.web.notified", str(now - 4000))
        webthreads.notify(rsvc, now=now)
        [m] = mailer.SENT
        assert m["To"] == LEO and str(m["Subject"]) == "New request: Dinner for forty"
        assert "Kim Cook: Dinner for forty (https://belfry.test/requests/" in text_of(m)
        assert "/notifications/" in text_of(m) and KIM not in text_of(m)
        mailer.SENT.clear()
        webthreads.notify(rsvc, now=now + 3700)
        assert mailer.SENT == [], "once"
        # daily: with the morning's round, from the moment they chose it
        assert call(c, leo, N, {"requests": "daily"}).status_code == 200
        assert call(c, kim, "/offers", {**ASK, "title": "Bread"}).status_code == 201
        mailer.SENT.clear()
        w.grp.events[-1]["created_at"] = now + 50
        webrequests.forget()
        webthreads.notify(rsvc, now=now + 7400)
        assert mailer.SENT == []
        morning = dt.datetime.fromtimestamp(now + 8000, dt.UTC)
        assert sitenotify.daily(rsvc, morning)["requests"] == 1
        [m] = mailer.SENT
        assert m["To"] == LEO and "Bread" in text_of(m) and "Dinner" not in text_of(m)
        mailer.SENT.clear()
        assert sitenotify.daily(rsvc, morning + dt.timedelta(days=1))["requests"] == 0
        # someone who lost the channel gets nothing more
        assert call(c, leo, N, {"requests": "asap"}).status_code == 200
        acct = sitesessions.site_account(rsvc, pk(leo))
        for p in acct["passes"]:
            rsvc.odoo.write("belfry.pass", [p["id"]], {"state": "revoked"})
        assert call(c, kim, "/offers", {**ASK, "title": "Soup"}).status_code == 201
        mailer.SENT.clear()
        w.grp.events[-1]["created_at"] = now + 9000
        webrequests.forget()
        webthreads.notify(rsvc, now=now + 12000)
        assert mailer.SENT == []


# ── events ──────────────────────────────────────────────────────────────────────────────────
def follows(rsvc, partner):
    rows = rsvc.odoo.call("belfry.follower", "belfry_follow_member_list", [partner])
    return {f["channel"]: (f["state"], f["frequency"]) for f in rows}


def test_the_events_switch_is_the_follows_of_their_tickets(site):
    rsvc, w, ada = site
    village(w, ada)
    group = "ev-" + VILLAGE
    with TestClient(create_app(rsvc)) as c:
        kim = guest(c)
        acct = sitesessions.site_account(rsvc, pk(kim))
        ev = events.load(rsvc, VILLAGE)
        admit(w, acct["pubkey"])
        # a ticket follows the event's announcements, as before
        events._ticket_issued(rsvc, ev, acct["pubkey"], mailer.Mailer(rsvc.settings))
        assert follows(rsvc, acct["partner"]) == {group: ("active", "asap")}
        assert get(c, kim)["events"]["value"] == "asap"
        assert call(c, kim, N, {"events": "off"}).status_code == 200
        assert follows(rsvc, acct["partner"]) == {group: ("stopped", "asap")}
        # off: a new ticket follows nothing
        events._ticket_issued(rsvc, ev, acct["pubkey"], mailer.Mailer(rsvc.settings))
        assert follows(rsvc, acct["partner"])[group][0] == "stopped"
        # on again: the events they hold a ticket for
        assert call(c, kim, N, {"events": "asap"}).json()["items"][-1]["value"] == "asap"
        assert follows(rsvc, acct["partner"]) == {group: ("active", "asap")}


def test_the_website_and_its_previews_reach_the_settings(site):
    rsvc, _w, _ada = site
    with TestClient(create_app(rsvc)) as c:
        for origin in ("https://belfry.test", "https://preview.belfry.test"):
            r = c.options(
                "/api/v1" + N,
                headers={**H, "Origin": origin, "Access-Control-Request-Method": "POST"},
            )
            assert r.status_code == 204 and r.headers["access-control-allow-origin"] == origin
            assert "access-control-allow-credentials" not in r.headers
