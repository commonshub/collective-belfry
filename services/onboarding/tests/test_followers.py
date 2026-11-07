"""Public announcements by e-mail (followers.py): the follow pages, the mails, the digest."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import followers
from belfry_onboarding.app import create_app


class Store:
    """belfry.follower's methods, in memory."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.mails: list[dict] = []
        self.days: set[str] = set()

    def __call__(self, svc, method, *args):
        return getattr(self, method)(*args)

    def by_token(self, token):
        return next((r for r in self.rows.values() if r["token"] == token), None)

    def belfry_follow_request(self, email):
        row = next((r for r in self.rows.values() if r["email"] == email), None)
        if row is None:
            i = len(self.rows) + 1
            row = self.rows[i] = {
                "id": i,
                "email": email,
                "state": "pending",
                "frequency": "monthly",
                "token": f"tok{i}",
                "since": 0,
            }
        if row["state"] == "active":
            return {"state": "following", "id": row["id"], "token": row["token"]}
        return {"state": "send", "id": row["id"], "token": row["token"]}

    def belfry_follow_get(self, token):
        r = self.by_token(token)
        return (
            {k: r[k] for k in ("id", "state", "frequency", "email")} if r else {"state": "unknown"}
        )

    def belfry_follow_set(self, token, choice):
        r = self.by_token(token)
        if not r:
            return {"state": "unknown"}
        if choice == "stop":
            r["state"] = "stopped"
        else:
            if r["state"] != "active":
                r["since"] = 1000
            r.update(state="active", frequency=choice)
        return self.belfry_follow_get(token)

    def belfry_follow_mail(self, fid, subject, body, until=0, headers=None):
        self.mails.append(
            {"id": fid, "subject": subject, "body": body, "until": until, "headers": headers}
        )
        if until:
            self.rows[fid]["since"] = until
        return len(self.mails)

    def belfry_follow_due(self, frequency):
        return [
            dict(r)
            for r in self.rows.values()
            if r["state"] == "active" and r["frequency"] == frequency
        ]

    def belfry_follow_claim_day(self, day):
        if day in self.days:
            return False
        self.days.add(day)
        return True


@pytest.fixture
def store(monkeypatch):
    s = Store()
    monkeypatch.setattr(followers, "odoo", s)
    return s


@pytest.fixture
def web(services, store):
    services.settings.site_url = "https://belfry.test"
    services.settings.public_url = "https://pass.belfry.test"
    services.settings.pass_page_secret = "p" * 32
    with TestClient(create_app(services), base_url="https://belfry.test") as c:
        yield c


def test_follow_mails_a_confirmation_link(web, store):
    r = web.post("/follow", data={"email": "ada@example.org"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/follow/sent"
    [mail] = store.mails
    assert mail["until"] == 0 and "Confirm" in mail["subject"]
    assert "https://belfry.test/follow?t=tok1" in mail["body"]
    assert mail["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "Check your inbox" in web.get("/follow/sent").text


def test_follow_refuses_junk_and_bots(web, store):
    assert web.post("/follow", data={"email": "nope"}).status_code == 422
    r = web.post(
        "/follow", data={"email": "bot@example.org", "website": "x"}, follow_redirects=False
    )
    assert r.status_code == 303 and not store.rows and not store.mails


def test_confirm_link_confirms_monthly_then_change_then_unsubscribe(web, store):
    web.post("/follow", data={"email": "ada@example.org"})
    page = web.get("/follow?t=tok1").text
    assert "Confirmed. You will receive the announcements once a month." in page
    assert "You follow belfry.test" in page and "on the 1st of each month" in page
    assert store.rows[1]["state"] == "active" and store.rows[1]["frequency"] == "monthly"
    # a footer link only offers the change: nothing happens on opening it
    page = web.get("/follow?t=tok1&f=asap").text
    assert "Receive them as soon as possible instead" in page
    assert store.rows[1]["frequency"] == "monthly"
    r = web.post("/follow/set", data={"t": "tok1", "f": "asap"}, follow_redirects=False)
    page = web.get(r.headers["location"]).text
    assert "Saved. You will receive the announcements as soon as possible." in page
    assert "a few minutes after each post" in page
    web.post("/follow/set", data={"t": "tok1", "f": "stop"})
    assert store.rows[1]["state"] == "stopped"
    assert "Subscribe again" in web.get("/follow?t=tok1").text


def test_pages_never_echo_the_address(web, store):
    evil = "<script>alert(1)</script>"
    page = web.get("/follow?" + urllib.parse.urlencode({"t": evil, "f": evil})).text
    assert "This link does not work" in page and "alert(1)" not in page


def test_one_click_unsubscribe(web, store):
    web.post("/follow", data={"email": "ada@example.org"})
    store.belfry_follow_set("tok1", "asap")
    assert web.post("/follow/unsubscribe?t=tok1").status_code == 200
    assert store.rows[1]["state"] == "stopped"


def test_footer_bolds_the_current_frequency(services):
    services.settings.site_url = "https://belfry.test"
    html = followers.footer(services, "tok", "monthly")
    assert "<strong>monthly</strong>" in html
    assert 'href="https://belfry.test/follow?t=tok&amp;f=asap"' in html and ">as soon as possible</a>" in html
    assert "f=monthly" not in html and "f=stop" in html
    assert "subscribed to the public announcements of belfry.test" in html


def test_text_html_escapes_and_links():
    out = followers.text_html("Hi <b>all</b>\nsee https://x.org/a. and https://x.org/p.png")
    assert "&lt;b&gt;" in out and "<br>" in out
    assert '<a href="https://x.org/a"' in out and '<img src="https://x.org/p.png"' in out


def posts_of(monkeypatch, posts):
    monkeypatch.setattr(
        followers,
        "announcements",
        lambda svc, since, until: (
            [e for e in posts if since < e["created_at"] <= until],
            {"p" * 64: "Ana"},
        ),
    )


def test_asap_mails_posts_once_they_are_a_few_minutes_old(services, store, monkeypatch):
    services.settings.site_url = "https://belfry.test"
    store.belfry_follow_request("ada@example.org")
    store.belfry_follow_set("tok1", "asap")  # since = 1000
    posts = [
        {"id": "a", "pubkey": "p" * 64, "created_at": 900, "content": "old"},
        {"id": "b", "pubkey": "p" * 64, "created_at": 2000, "content": "Doors open at 9"},
    ]
    posts_of(monkeypatch, posts)
    assert followers.soon(services, now=2000 + 60)["sent"] == 0  # too fresh
    assert followers.soon(services, now=2000 + followers.ASAP_DELAY) == {
        "due": 1,
        "sent": 1,
        "posts": 1,
    }
    [mail] = store.mails
    assert mail["subject"] == "belfry.test: Doors open at 9"
    assert "Doors open at 9" in mail["body"] and "old" not in mail["body"] and "Ana" in mail["body"]
    assert "<strong>as soon as possible</strong>" in mail["body"]
    assert followers.soon(services, now=5000)["sent"] == 0  # mailed once


def test_monthly_on_the_first_morning_only(services, store, monkeypatch):
    services.settings.site_url = "https://belfry.test"
    store.belfry_follow_request("ada@example.org")
    store.belfry_follow_set("tok1", "monthly")
    t = dt.datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo(services.settings.digest_tz)).timestamp()
    posts_of(monkeypatch, [{"id": "b", "pubkey": "p" * 64, "created_at": int(t), "content": "Hi"}])
    tz = ZoneInfo(services.settings.digest_tz)
    assert followers.morning(services, dt.datetime(2026, 11, 30, 9, 0, tzinfo=tz)) is None
    assert followers.morning(services, dt.datetime(2026, 12, 1, 6, 0, tzinfo=tz)) is None
    at = dt.datetime(2026, 12, 1, 8, 30, tzinfo=tz)
    assert followers.morning(services, at) == {"due": 1, "sent": 1, "posts": 1}
    assert "<strong>monthly</strong>" in store.mails[0]["body"]
    assert followers.morning(services, at) is None  # once that day
