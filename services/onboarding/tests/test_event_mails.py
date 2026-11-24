"""What people going to an event are told (eventmails.py, docs/checkin.md): the Event website
on the event, the RSVP mail, the change mail of a version with ``["notify", "1"]`` and the Join
online link an hour before."""
# ruff: noqa: F811 — the fixtures come from test_events

from __future__ import annotations

import time

from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import events, mailer
from belfry_onboarding.app import create_app

from .test_events import (  # noqa: F401 — fixtures
    HOST,
    D,
    World,
    call,
    make_event,
    member,
    pk,
    rsvc,
    world,
)

SITE = "https://garden.example/day-2027"


def text_of(m) -> str:
    return m.get_body(preferencelist=("plain",)).get_content()


def html_of(m) -> str:
    return m.get_body(preferencelist=("html",)).get_content()


def revise(w: World, by: PrivateKey, base: dict, at: int, **changes) -> dict:
    """A new version of BASE by BY: tags replaced by CHANGES (None drops the tag), others kept."""
    tags = [list(t) for t in base["tags"] if t[0] not in changes]
    for k, v in changes.items():
        if v is not None:
            tags.append([k, v])
    return w.put(by, base["kind"], tags, base.get("content", ""), at=at)


def guest_joins(c, email="gus@example.org", name="Gus"):
    got = call(c, PrivateKey(), "POST", f"/{D}/join", {"name": name, "email": email})
    assert got.status_code == 200, got.text


def test_the_event_website_is_shown_to_whoever_may_read_the_event(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada, website=SITE)
    with TestClient(create_app(rsvc)) as c:
        j = c.get(f"/api/v1/events/{D}", headers=HOST).json()
        assert j["website"] == SITE and j["links"] == []  # the call link stays hidden
        page = c.get(f"/events/{D}").text
        assert "Event website" in page and f'href="{SITE}"' in page
        assert 'rel="noopener nofollow"' in page and "garden.example/day-2027" in page
        assert "meet.jit.si" not in page


def test_only_an_https_website_is_read():
    for bad in (
        "http://garden.example",
        "javascript:alert(1)",
        "https://",
        "https://localhost",
        'https://x.example/"><script>',
        "https://x.example/a b",
        "https://x.example/" + "a" * 2000,
    ):
        assert events.website_of({"tags": [["website", bad]]}) == "", bad
    assert events.website_of({"tags": [["website", " " + SITE + " "]]}) == SITE
    assert events.website_of({"tags": []}) == ""


def test_no_website_means_null(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada)
    with TestClient(create_app(rsvc)) as c:
        assert c.get(f"/api/v1/events/{D}", headers=HOST).json()["website"] is None
        assert "Event website" not in c.get(f"/events/{D}").text


def test_the_rsvp_mail_has_the_three_links_each_where_it_belongs(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada, website=SITE)
    with TestClient(create_app(rsvc)) as c:
        guest_joins(c)
        mailer.SENT.clear()
        events.watch(rsvc)
    [m] = [m for m in mailer.SENT if m["Subject"] == "You're going to Garden day"]
    text = text_of(m)
    assert "You're going to Garden day." in text and "When: " in text
    assert "Where: The garden" in text
    assert "Join online: we'll e-mail you the link 1 hour before." in text
    assert "meet.jit.si" not in text  # the link itself comes an hour before
    assert f"Event website: garden.example/day-2027 ({SITE})" in text
    assert f"Open the event page: https://belfry.test/events/{D}" in text
    assert "Add to calendar (.ics attached)" in text
    [ics] = [p for p in m.iter_attachments()]
    assert ics.get_filename() == "event.ics" and "SUMMARY:Garden day" in ics.get_content()


def test_a_change_with_notify_tells_everyone_going_once(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    base = make_event(world, ada)
    with TestClient(create_app(rsvc)) as c:
        guest_joins(c)
    events.watch(rsvc)  # the going mail; the watcher now knows this version
    now = int(time.time())
    start = int(events.tag(base, "start"))
    later = revise(
        world,
        ada,
        base,
        now + 1,
        start=str(start + 3600),
        r="https://meet.jit.si/garden-day-2",
        website=SITE,
        notify="1",
        changed="start",
    )
    mailer.SENT.clear()
    assert events.watch(rsvc)["changed"] == 1
    [m] = mailer.SENT
    assert m["To"] == "gus@example.org"
    assert m["Subject"].startswith("Garden day now starts at ")
    text = text_of(m)
    assert "Garden day changed:" in text and "Starts: " in text and "(was " in text
    assert "Join online: A new link: meet.jit.si/garden-day-2" in text
    assert "Event website: garden.example/day-2027" in text
    assert "Where" not in text and "Title" not in text  # only what changed
    assert "Sent to everyone going, by Somebody. The calendar file is updated." in text
    assert pk(ada) not in text
    assert [p.get_filename() for p in m.iter_attachments()] == ["event.ics"]
    mailer.SENT.clear()
    assert events.watch(rsvc)["changed"] == 0  # once per version

    # a version without notify: nobody is told, even of a new place
    quiet = revise(world, ada, later, now + 2, location="The barn", notify=None)
    assert events.watch(rsvc)["changed"] == 0
    # the next version that asks compares with the last one the watcher saw (the barn)
    revise(world, ada, quiet, now + 3, location="The orchard", notify="1")
    mailer.SENT.clear()
    assert events.watch(rsvc)["changed"] == 1
    text = text_of(mailer.SENT[-1])
    assert "Where: The orchard (was The barn)" in text and "Starts" not in text
    assert mailer.SENT[-1]["Subject"] == "Garden day has changed"


def test_a_new_description_alone_tells_nobody(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    base = make_event(world, ada)
    with TestClient(create_app(rsvc)) as c:
        guest_joins(c)
    events.watch(rsvc)
    v = dict(base, content="New words")
    revise(world, ada, v, int(time.time()) + 1, notify="1")
    mailer.SENT.clear()
    assert events.watch(rsvc)["changed"] == 0 and mailer.SENT == []


def test_an_old_version_at_first_sight_tells_nobody(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    base = make_event(world, ada)
    with TestClient(create_app(rsvc)) as c:
        guest_joins(c)
    # changed two hours ago, before this watcher ever ran (a restart after a long stop)
    for e in world.comm.events:
        if e["id"] == base["id"]:
            e["created_at"] -= 4 * 3600
    revise(world, ada, base, int(time.time()) - 2 * 3600, location="The barn", notify="1")
    events._stores.clear()
    mailer.SENT.clear()
    assert events.watch(rsvc)["changed"] == 0


def test_a_version_by_someone_not_a_host_is_not_the_event(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    base = make_event(world, ada)
    with TestClient(create_app(rsvc)) as c:
        guest_joins(c)
    events.watch(rsvc)
    revise(world, PrivateKey(), base, int(time.time()) + 1, location="Elsewhere", notify="1")
    mailer.SENT.clear()
    assert events.watch(rsvc)["changed"] == 0


def test_the_join_online_link_an_hour_before_to_people_going_only(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    member(rsvc, "bea@belfry.test", "Bea")  # a member, not going: never gets the link
    make_event(world, ada, start_in=40 * 60)
    with TestClient(create_app(rsvc)) as c:
        guest_joins(c)
    mailer.SENT.clear()
    assert events.watch(rsvc)["join_links"] == 1
    [m] = [m for m in mailer.SENT if m["Subject"] == "In 1 hour: Garden day"]
    assert m["To"] == "gus@example.org"
    text = text_of(m)
    assert "Join online: meet.jit.si/garden-day (https://meet.jit.si/garden-day)" in text
    assert "(Europe/Brussels)" in text
    assert 'href="https://meet.jit.si/garden-day"' in html_of(m)
    assert all("bea@" not in str(x["To"]) for x in mailer.SENT)
    mailer.SENT.clear()
    assert events.watch(rsvc)["join_links"] == 0  # once per person and event


def test_no_link_mail_before_the_hour_or_without_a_link(rsvc, world, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    make_event(world, ada, start_in=3 * 3600)
    with TestClient(create_app(rsvc)) as c:
        guest_joins(c)
    assert events.watch(rsvc)["join_links"] == 0
