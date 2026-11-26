"""Who hosts an event (Xavier, 2026-11-26): "Only the person who created the event and the
person invited as co-hosts should be admins of the event and seen as co-hosts. Any co-host can
remove any co-host." The belfry's admins (the relay's 39001) host only what they made or were
invited to; the creator can't be removed; the hosts migration keeps former hosts' edits."""
# ruff: noqa: F811 — the fixtures come from test_events / test_checkin

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from belfry_onboarding import checkin, events, mail
from belfry_onboarding.app import create_app

from .test_checkin import cw, hold, meta_post, roles_post  # noqa: F401 — fixtures
from .test_cohost_invites import phones  # noqa: F401 — fixtures
from .test_cohost_invites import post as invite_post
from .test_events import (  # noqa: F401 — fixtures
    GROUP,
    D,
    call,
    make_event,
    member,
    pk,
    rsvc,
    world,
)


def cohost(w, key) -> None:
    w.put(w.gkey, 9000, [["h", GROUP], ["p", pk(key), "admin"], ["role", "admin"]])


def test_hosts_are_the_creator_and_co_hosts_by_role_only(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    boss = member(rsvc, "boss@belfry.test", "Boss")
    make_event(cw, ada)
    # the relay lists the belfry's admin, its own key and the creator: only the creator hosts
    cw.admins(GROUP, [pk(ada), pk(boss), pk(cw.gkey)])
    cw.belfry_admins = {pk(boss)}
    admins, creator = events.hosts_of(rsvc, GROUP)
    assert creator == pk(ada) and admins == {pk(ada)}
    assert events.load(rsvc, D).hosts == {pk(ada)}
    assert checkin.group_state(rsvc, GROUP).admins == {pk(ada)}
    assert mail.group_admins(rsvc, GROUP) == {pk(ada)}
    cohost(cw, bea)
    ev = events.load(rsvc, D)
    assert ev.hosts == {pk(ada), pk(bea)}
    assert events.shown_hosts(rsvc, ev) == [pk(ada), pk(bea)]
    with TestClient(create_app(rsvc)) as c:
        got = call(c, ada, "GET", f"/{D}").json()
        assert {h["pubkey"] for h in got["hosts"]} == {pk(ada), pk(bea)}
        assert [h["pubkey"] for h in got["hosted_by"]] == [pk(ada), pk(bea)]
        me = call(c, boss, "GET", f"/{D}").json()["me"]
        assert me["host"] is False and me["roles"] == [] and me["may_check_in"] is False
        assert call(c, bea, "GET", f"/{D}").json()["me"]["roles"] == ["cohost"]


def test_a_card_event_made_by_the_groups_key_names_its_creator(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    cw.put(cw.gkey, 9007, [["h", GROUP], ["by", pk(ada)]])
    assert events.hosts_of(rsvc, GROUP) == ({pk(ada)}, pk(ada))


def test_a_belfry_admin_who_doesnt_host_has_no_edit_powers(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    boss = member(rsvc, "boss@belfry.test", "Boss")
    v1 = make_event(cw, ada)
    hold(cw, bea)
    cw.admins(GROUP, [pk(ada), pk(boss)])
    cw.belfry_admins = {pk(boss)}
    # a version by the belfry's admin doesn't make the event
    tags = [t for t in v1["tags"] if t[0] != "title"] + [["title", "Moved by boss"]]
    cw.put(boss, 31923, tags, "x", at=int(time.time()) + 5)
    assert events.load(rsvc, D).title == "Garden day"
    cw.put(cw.gkey, 39000, [["d", GROUP], ["name", "Garden day"]])  # for /group-meta
    with TestClient(create_app(rsvc)) as c:
        r = call(c, boss, "PUT", f"/{D}/slug", {"slug": "garden-boss-k3x9"})
        assert r.status_code == 403 and r.json()["error"] == "not_host"
        r = roles_post(c, boss, {"channel": GROUP, "pubkey": pk(bea), "role": "steward"})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        r = meta_post(c, boss, {"channel": GROUP, "name": "Boss day"})
        assert r.status_code == 403
        assert call(c, boss, "POST", f"/{D}/attendees", {}).status_code == 403
        assert call(c, ada, "PUT", f"/{D}/slug", {"slug": "garden-ada-k3x9"}).status_code == 200


def test_any_co_host_removes_any_co_host_but_never_the_creator(rsvc, cw, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    make_event(cw, ada)
    cohost(cw, bea)
    cohost(cw, cy)
    with TestClient(create_app(rsvc)) as c:
        # a co-host removes another co-host (not the creator's doing)
        r = roles_post(c, cy, {"channel": GROUP, "pubkey": pk(bea), "role": "removed"})
        assert r.status_code == 200 and r.json() == {"role": "removed"}
        assert events.load(rsvc, D).hosts == {pk(ada), pk(cy)}
        # and can remove themself; the creator is always left
        r = roles_post(c, cy, {"channel": GROUP, "pubkey": pk(cy), "role": "member"})
        assert r.status_code == 200
        assert events.load(rsvc, D).hosts == {pk(ada)}
        cohost(cw, bea)
        for role in ("removed", "member", "steward"):
            r = roles_post(c, bea, {"channel": GROUP, "pubkey": pk(ada), "role": role})
            assert r.status_code == 409 and r.json()["error"] == "always_admin"


def test_a_belfry_admin_who_accepts_an_invitation_becomes_a_co_host(rsvc, cw, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    boss = member(rsvc, "boss@belfry.test", "Boss")
    make_event(cw, ada)
    cw.admins(GROUP, [pk(ada), pk(boss)])
    cw.belfry_admins = {pk(boss)}
    with TestClient(create_app(rsvc)) as c:
        assert invite_post(c, ada, "", {"pubkey": pk(boss)}).status_code == 200
        assert invite_post(c, boss, "/answer", {"accept": True}).status_code == 200
    made = cw.grp.events[-1]
    assert made["kind"] == 9000 and ["p", pk(boss), "admin"] in made["tags"]
    assert events.load(rsvc, D).hosts == {pk(ada), pk(boss)}


def test_the_migration_keeps_a_former_hosts_edit(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    boss = member(rsvc, "boss@belfry.test", "Boss")
    eve = member(rsvc, "eve@belfry.test", "Eve")
    v1 = make_event(cw, ada)
    cw.admins(GROUP, [pk(ada), pk(boss), pk(cw.gkey)])
    cw.belfry_admins = {pk(boss)}
    tags = [t for t in v1["tags"] if t[0] != "title"] + [["title", "Edited by boss"]]
    cw.put(eve, 31923, tags, "x", at=int(time.time()) + 20)  # never a host: never counted
    cw.put(boss, 31923, tags, "x", at=int(time.time()) + 10)
    assert events.load(rsvc, D).title == "Garden day"  # dropped by the new rule…
    assert checkin.migrate_event_hosts(rsvc) == [(D, pk(boss))]
    made = cw.grp.events[-1]
    assert made["kind"] == 9000 and made["pubkey"] == pk(cw.gkey)
    assert ["p", pk(boss), "admin"] in made["tags"] and ["role", "admin"] in made["tags"]
    ev = events.load(rsvc, D)
    assert ev.title == "Edited by boss" and pk(boss) in ev.hosts  # …and kept by the migration
    assert pk(eve) not in ev.hosts
    # once
    n = len(cw.grp.events)
    assert checkin.migrate_event_hosts(rsvc) == []
    assert len(cw.grp.events) == n
    assert events.store(rsvc).param(checkin.HOSTS_MIGRATION)


def test_the_migration_leaves_events_edited_by_their_hosts(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    boss = member(rsvc, "boss@belfry.test", "Boss")
    v1 = make_event(cw, ada)
    cw.admins(GROUP, [pk(ada), pk(boss)])
    tags = [t for t in v1["tags"] if t[0] != "title"] + [["title", "Old"]]
    cw.put(boss, 31923, tags, "x", at=int(time.time()) - 100)  # older than the creator's
    n = len(cw.grp.events)
    assert checkin.migrate_event_hosts(rsvc) == []
    assert len(cw.grp.events) == n


def test_channels_keep_their_rules(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    boss = member(rsvc, "boss@belfry.test", "Boss")
    cw.put(ada, 9007, [["h", "garden"]])
    cw.admins("garden", [pk(ada), pk(boss)])
    st = checkin.group_state(rsvc, "garden")
    assert st.admins == {pk(ada), pk(boss)}
    cw.belfry_admins = {pk(boss)}
    cw.put(cw.gkey, 39000, [["d", "garden"], ["name", "Garden"]])
    with TestClient(create_app(rsvc)) as c:
        r = meta_post(c, boss, {"channel": "garden", "name": "Garden 2"})
        assert r.status_code == 200, r.text
