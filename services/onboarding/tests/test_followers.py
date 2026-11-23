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

    def belfry_follow_request(self, email, channel="general"):
        row = next(
            (r for r in self.rows.values() if r["email"] == email and r["channel"] == channel), None
        )
        if row is None:
            i = len(self.rows) + 1
            row = self.rows[i] = {
                "channel": channel,
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
            {k: r[k] for k in ("id", "state", "frequency", "email", "channel")}
            if r
            else {"state": "unknown"}
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

    def belfry_follow_member_list(self, partner):
        return [
            {k: r[k] for k in ("channel", "frequency", "state")}
            for r in self.rows.values()
            if r["email"] == f"member{partner}@example.org"
        ]

    def belfry_follow_member_set(self, partner, channel, choice):
        email = f"member{partner}@example.org"
        got = self.belfry_follow_request(email, channel)
        r = self.belfry_follow_set(got["token"], choice)
        return {"channel": channel, "state": r["state"], "frequency": r["frequency"]}

    def belfry_follow_claim_day(self, day, what="monthly"):
        if (day, what) in self.days:
            return False
        self.days.add((day, what))
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


def test_confirm_link_confirms_weekly_then_change_then_unsubscribe(web, store):
    web.post("/follow", data={"email": "ada@example.org"})
    page = web.get("/follow?t=tok1").text
    assert "Confirmed. You will receive a summary once a week." in page
    assert "You follow belfry.test" in page and "every Monday" in page
    assert store.rows[1]["state"] == "active" and store.rows[1]["frequency"] == "weekly"
    # a footer link only offers the change: nothing happens on opening it
    page = web.get("/follow?t=tok1&f=asap").text
    assert "Receive them as soon as possible instead" in page
    assert store.rows[1]["frequency"] == "weekly"
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
    assert (
        'href="https://belfry.test/follow?t=tok&amp;f=asap"' in html
        and ">as soon as possible</a>" in html
    )
    assert "f=monthly" not in html and "f=stop" in html and "f=weekly" in html
    assert "because you follow belfry.test." in html
    html = followers.footer(services, "tok", "asap")
    assert "subscribed to the announcements of belfry.test." in html


def test_text_html_escapes_and_links():
    out = followers.text_html("Hi <b>all</b>\nsee https://x.org/a. and https://x.org/p.png")
    assert "&lt;b&gt;" in out and "<br>" in out
    assert '<a href="https://x.org/a"' in out and '<img src="https://x.org/p.png"' in out


def posts_of(monkeypatch, posts):
    monkeypatch.setattr(
        followers,
        "announcements",
        lambda svc, channel, since, until: (
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
    store.rows[1]["partner_id"] = 7
    t = dt.datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo(services.settings.digest_tz)).timestamp()
    channel_of(monkeypatch, [msg("b", int(t), "Hi")], readers_all=True)
    tz = ZoneInfo(services.settings.digest_tz)
    assert followers.morning(services, dt.datetime(2026, 11, 30, 9, 0, tzinfo=tz)) is None
    assert followers.morning(services, dt.datetime(2026, 12, 1, 6, 0, tzinfo=tz)) is None
    at = dt.datetime(2026, 12, 1, 8, 30, tzinfo=tz)
    assert followers.morning(services, at) == {"due": 1, "sent": 1, "posts": 1}
    assert "<strong>monthly</strong>" in store.mails[0]["body"]
    assert followers.morning(services, at) is None  # once that day


def test_a_member_follows_any_channel_from_the_chat(services, odoo, store, monkeypatch):
    """GET and POST /api/me/follows, NIP-98 with the member's key; a pod's mails name it."""
    import time

    from coincurve import PrivateKey

    from .test_holder_invites import get, post

    services.settings.site_url = "https://belfry.test"
    services.settings.public_url = "https://pass.belfry.test"
    key = PrivateKey()
    pk = key.public_key_xonly.format().hex()
    partner = odoo.add("res.partner", name="Cleo", email="cleo@example.org")
    odoo.add("belfry.pass", partner_id=partner, tier="week", state="active", keycast_pubkey=pk)
    with TestClient(create_app(services), base_url="https://pass.belfry.test") as client:
        body = {"channel": "pod-garden-1a2b", "frequency": "asap"}
        r = post(client, key, "/api/me/follows", body)
        assert r.status_code == 200, r.text
        assert r.json() == {"channel": "pod-garden-1a2b", "state": "active", "frequency": "asap"}
        time.sleep(1.1)  # a NIP-98 authorization is single use
        r = get(client, key, "/api/me/follows")
        assert r.json()["follows"] == [
            {"channel": "pod-garden-1a2b", "frequency": "asap", "state": "active"}
        ]
        assert r.json()["email"] == "cleo@example.org"
        time.sleep(1.1)
        bad = post(client, key, "/api/me/follows", {"channel": "no spaces", "frequency": "asap"})
        assert bad.status_code == 422
    monkeypatch.setattr(followers, "channel_label", lambda svc, c: "Garden crew")
    html = followers.footer(services, "tok", "asap", "pod-garden-1a2b")
    assert "the announcements of Garden crew on belfry.test." in html


def test_only_the_admins_announcements_count(services, monkeypatch):
    admin, member = "a" * 64, "b" * 64
    events = [
        {"id": "1", "kind": 9, "pubkey": admin, "created_at": 10, "content": "Real", "tags": []},
        {"id": "2", "kind": 9, "pubkey": member, "created_at": 11, "content": "Fake", "tags": []},
        {"id": "3", "kind": 9, "pubkey": admin, "created_at": 12, "content": "Gone", "tags": []},
        {"id": "4", "kind": 9007, "pubkey": admin, "created_at": 1, "content": "", "tags": []},
        {
            "id": "5",
            "kind": 9005,
            "pubkey": admin,
            "created_at": 13,
            "content": "",
            "tags": [["e", "3"]],
        },
        {
            "id": "6",
            "kind": 0,
            "pubkey": admin,
            "created_at": 1,
            "content": '{"name":"Ana"}',
            "tags": [],
        },
    ]

    class Relay:
        def query(self, filters, sign):
            return [e for e in events if e["kind"] in filters[0]["kinds"]]

    monkeypatch.setattr(followers, "_relay", lambda svc: (Relay(), None))
    posts, names = followers.announcements(services, "general", 0, 100)
    assert [e["content"] for e in posts] == ["Real"] and names == {admin: "Ana"}


# ── weekly and monthly summaries ────────────────────────────────────────────────────────────
ANA, BEN, ADMIN = "a" * 64, "b" * 64, "c" * 64


def msg(i, at, text, author=ANA, channel="general", **tags):
    return {
        "id": str(i),
        "kind": 9,
        "pubkey": author,
        "created_at": at,
        "content": text,
        "tags": [["h", channel], *[[k, v] for k, v in tags.items()]],
    }


class Relay:
    """Answers each filter like a relay: kinds, #h, #d, #t, authors, since, until."""

    def __init__(self, events):
        self.events = events

    def query(self, filters, sign=None):
        out = []
        for f in filters:
            for e in self.events:
                if "kinds" in f and e["kind"] not in f["kinds"]:
                    continue
                if "authors" in f and e["pubkey"] not in f["authors"]:
                    continue
                if f.get("since") and e["created_at"] < f["since"]:
                    continue
                if f.get("until") and e["created_at"] > f["until"]:
                    continue
                ok = True
                for k, v in f.items():
                    if k.startswith("#"):
                        vals = {t[1] for t in e["tags"] if t[0] == k[1:] and len(t) > 1}
                        ok = ok and bool(vals & set(v))
                if ok and e not in out:
                    out.append(e)
        return out


def meta(channel="general", *flags):
    return {
        "id": f"m-{channel}",
        "kind": 39000,
        "pubkey": ADMIN,
        "created_at": 1,
        "content": "",
        "tags": [["d", channel], ["name", channel], *[[f] for f in flags]],
    }


def profile(pubkey, name):
    return {
        "id": f"p-{pubkey[:4]}",
        "kind": 0,
        "pubkey": pubkey,
        "created_at": 1,
        "content": '{"name": "%s"}' % name,
        "tags": [],
    }


def channel_of(monkeypatch, posts, *, readers_all=True, flags=(), extra=(), joined=0):
    channels = {t[1] for e in posts for t in e["tags"] if t[0] == "h"} or {"general"}
    events = [*posts, *(meta(c, *flags) for c in channels), profile(ANA, "Ana"), *extra]
    monkeypatch.setattr(followers, "_relay", lambda svc: (Relay(events), None))
    monkeypatch.setattr(
        followers,
        "readers",
        lambda svc, partners, channel: set(partners) if readers_all else set(),
    )
    monkeypatch.setattr(followers, "new_followers", lambda svc, channel, since: joined)
    return events


def follower(store, frequency="weekly", partner=7, channel="general"):
    got = store.belfry_follow_request(f"f{len(store.rows)}@example.org", channel)
    store.belfry_follow_set(got["token"], frequency)  # since = 1000
    store.rows[got["id"]]["partner_id"] = partner
    return store.rows[got["id"]]


TZ = ZoneInfo("Europe/Brussels")


def test_weekly_goes_out_on_monday_mornings_once(services, store, monkeypatch):
    services.settings.site_url = "https://belfry.test"
    services.settings.digest_tz, services.settings.digest_hour = "Europe/Brussels", 8
    follower(store, "weekly")
    at = int(dt.datetime(2026, 11, 20, 12, 0, tzinfo=TZ).timestamp())
    channel_of(monkeypatch, [msg("1", at, "Bread on Friday")])
    assert followers.monday(services, dt.datetime(2026, 11, 22, 9, 0, tzinfo=TZ)) is None  # Sun
    assert followers.monday(services, dt.datetime(2026, 11, 23, 7, 59, tzinfo=TZ)) is None
    # 23:30 UTC on Sunday is 00:30 on Monday in Brussels: Monday, but before the hour
    utc = ZoneInfo("UTC")
    assert followers.monday(services, dt.datetime(2026, 11, 22, 23, 30, tzinfo=utc)) is None
    # 07:30 UTC is 08:30 in Brussels (CET)
    got = followers.monday(services, dt.datetime(2026, 11, 23, 7, 30, tzinfo=utc))
    assert got == {"due": 1, "sent": 1, "posts": 1}
    assert followers.monday(services, dt.datetime(2026, 11, 23, 10, 0, tzinfo=TZ)) is None
    [mail] = store.mails
    assert mail["subject"] == "belfry.test: 1 new message this week"
    assert "This week in #general" in mail["body"] and "Bread on Friday" in mail["body"]
    assert "<strong>weekly</strong>" in mail["body"] and "Ana" in mail["body"]
    assert mail["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    # the monthly morning and the weekly one are claimed apart
    assert ("2026-11-23", "weekly") in store.days


def test_a_summary_shows_twenty_newest_first_and_names_people(services, store, monkeypatch):
    services.settings.site_url = "https://belfry.test"
    services.settings.nip05_domain = "belfry.test"
    f = follower(store, "weekly")
    posts = [msg(f"m{i:02d}", 2000 + i, f"Message number {i:02d}", author=ANA) for i in range(25)]
    posts.append(msg("nobody", 3000, "From someone without a profile", author=BEN))
    posts.append(msg("long", 3001, "word " * 200))
    gone = msg("gone", 3002, "Deleted later")
    rm = {"id": "rm", "kind": 9005, "pubkey": ADMIN, "created_at": 3003, "content": "",
          "tags": [["h", "general"], ["e", "gone"]]}  # fmt: skip
    card = msg("card", 3004, "Ana started this topic from a printed card", card="1")
    mention = msg("men", 3005, f"Thanks nostr:{npub(BEN)} for the bread")
    channel_of(monkeypatch, [*posts, gone, card, mention], extra=[rm], joined=3)
    got = followers.send_summaries(services, "weekly", 10_000)
    assert got["sent"] == 1
    body = store.mails[0]["body"]
    assert store.mails[0]["subject"] == "belfry.test: 28 new messages this week"
    # newest first: the mention, then the long one, then the one without a profile
    assert body.index("Thanks") < body.index("word word") < body.index("without a profile")
    assert "Message number 24" in body and "Message number 08" in body
    assert "Message number 07" not in body  # 21st and older: in "and N more"
    assert "and 8 more in #general" in body and "https://chat.belfry.test/#/c/general" in body
    assert "Deleted later" not in body and "printed card" not in body
    assert "Somebody" in body and "@Somebody" in body  # no profile: never a key
    assert BEN not in body and "npub1" not in body
    assert "word " * 41 not in body  # the snippet is cut near 200 characters
    assert "https://chat.belfry.test/#/c/general/m/men" in body
    assert "3 new people started following #general this week." in body
    assert f["since"] == 10_000  # mailed up to the round's end


def npub(hexkey):
    from belfry_onboarding.nostr import hex_to_npub

    return hex_to_npub(hexkey)


def test_a_quiet_week_mails_nobody(services, store, monkeypatch):
    services.settings.site_url = "https://belfry.test"
    follower(store, "weekly")
    channel_of(monkeypatch, [msg("old", 500, "Before they followed")])
    assert followers.send_summaries(services, "weekly", 10_000)["sent"] == 0
    assert not store.mails


def test_monthly_is_the_same_summary_over_a_month(services, store, monkeypatch):
    services.settings.site_url = "https://belfry.test"
    follower(store, "monthly")
    channel_of(monkeypatch, [msg("1", 2000, "Hello"), msg("2", 2001, "Again")])
    assert followers.send_summaries(services, "monthly", 10_000)["sent"] == 1
    mail = store.mails[0]
    assert mail["subject"] == "belfry.test: 2 new messages this month"
    assert "This month in #general" in mail["body"] and "<strong>monthly</strong>" in mail["body"]


def test_someone_who_cant_read_the_channel_gets_its_announcements_only(
    services, store, monkeypatch
):
    services.settings.site_url = "https://belfry.test"
    follower(store, "weekly", partner=99)  # from the website: no pass
    made = {"id": "made", "kind": 9007, "pubkey": ADMIN, "created_at": 1, "content": "",
            "tags": [["h", "general"]]}  # fmt: skip
    posts = [
        msg("1", 2000, "Members-only chatter"),
        msg("2", 2001, "Open day on Saturday", author=ADMIN, t="announcement"),
    ]
    channel_of(monkeypatch, posts, readers_all=False, extra=[made])
    assert followers.send_summaries(services, "weekly", 10_000)["sent"] == 1
    body = store.mails[0]["body"]
    assert "Open day on Saturday" in body and "Members-only chatter" not in body


def test_private_groups_get_no_summary(services, store, monkeypatch):
    services.settings.site_url = "https://belfry.test"
    follower(store, "weekly", channel="pod-secret-1a2b")
    channel_of(
        monkeypatch, [msg("1", 2000, "Our secret", channel="pod-secret-1a2b")], flags=("private",)
    )
    assert followers.send_summaries(services, "weekly", 10_000)["sent"] == 0
    assert not store.mails


def test_readers_are_members_and_guests_of_that_channel(services, tmp_path, settings):
    from belfry_onboarding.register import Register

    reg = services.odoo = Register(tmp_path / "register.sqlite", settings)

    def contact(name, **pass_):
        pid = reg.create("res.partner", {"name": name, "email": f"{name}@example.org"})
        if pass_:
            reg.create("belfry.pass", {"partner_id": pid, "state": "active", **pass_})
        return pid

    member = contact("m", tier="member")
    guest = contact("g", tier="guest", guest_channel="bread")
    elsewhere = contact("e", tier="guest", guest_channel="other")
    outsider = contact("o")
    got = followers.readers(services, {member, guest, elsewhere, outsider}, "bread")
    assert got == {member, guest}
    # the register's own: follows with weekly, claimed apart from monthly, due rows name the contact
    row = reg.call("belfry.follower", "belfry_follow_member_set", [member, "bread", "weekly"])
    assert row == {"channel": "bread", "state": "active", "frequency": "weekly"}
    [due] = reg.call("belfry.follower", "belfry_follow_due", ["weekly"])
    assert due["partner_id"] == member and due["channel"] == "bread"
    assert reg.call("belfry.follower", "belfry_follow_claim_day", ["2026-11-23", "weekly"])
    assert not reg.call("belfry.follower", "belfry_follow_claim_day", ["2026-11-23", "weekly"])
    assert reg.call("belfry.follower", "belfry_follow_claim_day", ["2026-11-23"])
    assert followers.new_followers(services, "bread", 0) == 1
    assert followers.new_followers(services, "bread", int(9e9)) == 0


def test_the_follow_form_and_the_api_take_weekly(web, store):
    r = web.post(
        "/follow", data={"email": "ada@example.org", "frequency": "monthly"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "f=monthly" in store.mails[0]["body"] and "once a month" in store.mails[0]["body"]
    page = web.get("/follow?t=tok1&f=monthly").text
    assert "Confirmed. You will receive a summary once a month." in page
    r = web.post("/follow/set", data={"t": "tok1", "f": "weekly"}, follow_redirects=False)
    page = web.get(r.headers["location"]).text
    assert "Saved. You will receive a summary once a week." in page
    assert "A summary reaches" in page and "every Monday" in page
    assert store.rows[1]["frequency"] == "weekly"


def test_summary_choice():
    from belfry_onboarding.holder_invites import ApiError

    assert followers.summary_choice({}) == "weekly"
    assert followers.summary_choice({"summary": "monthly"}) == "monthly"
    with pytest.raises(ApiError) as exc:
        followers.summary_choice({"summary": "daily"})
    assert exc.value.status == 422 and exc.value.extra["field"] == "summary"
