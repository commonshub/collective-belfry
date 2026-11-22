"""Archived groups (docs/archive.md): /group-meta's ``archived``, what an archived group refuses."""
# ruff: noqa: F811 — the fixtures come from test_events / test_checkin

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from belfry_onboarding import checkin, guests, mail
from belfry_onboarding.app import create_app

from .test_checkin import cw, meta_post, roles_post  # noqa: F401 — fixtures
from .test_events import World, member, pk, rsvc, world  # noqa: F401 — fixtures

POD = "pod-garden"


def garden(cw: World, ada, bea, cy, group: str = POD) -> None:
    """A group made by Ada; Bea is an admin by role, Cy a member."""
    cw.put(ada, 9007, [["h", group]])
    cw.put(
        cw.gkey,
        39000,
        [["d", group], ["name", "Garden"], ["about", "Roof"], ["closed"], ["prompt", "Why?"]],
    )
    cw.put(cw.gkey, 9000, [["h", group], ["p", pk(bea), "admin"]])
    cw.put(cw.gkey, 9000, [["h", group], ["p", pk(cy)]])


def test_archive_and_unarchive_through_group_meta(rsvc, cw, keycast):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    garden(cw, ada, bea, cy)
    with TestClient(create_app(rsvc)) as c:
        assert checkin.is_archived(rsvc, POD) is False
        # a member can't; true / false only
        r = meta_post(c, cy, {"channel": POD, "archived": True})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        assert meta_post(c, bea, {"channel": POD, "archived": "yes"}).status_code == 422
        # an admin by role archives: a 9002 by the groups key, every other tag kept
        r = meta_post(c, bea, {"channel": POD, "archived": True})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "channel": POD,
            "name": "Garden",
            "about": "Roof",
            "closed": True,
            "private": False,
            "archived": True,
        }
        e = cw.grp.events[-1]
        assert e["kind"] == 9002 and e["pubkey"] == pk(cw.gkey)
        for t in (
            ["h", POD],
            ["name", "Garden"],
            ["about", "Roof"],
            ["closed"],
            ["prompt", "Why?"],
            ["archived", "1"],
        ):
            assert t in e["tags"], t
        assert checkin.is_archived(rsvc, POD) is True
        # archived: every other change is refused, with or without the flag, and archiving again
        n = len(cw.grp.events)
        for body in (
            {"channel": POD, "name": "New"},
            {"channel": POD, "about": "x"},
            {"channel": POD, "closed": False},
            {"channel": POD, "archived": False, "name": "New"},
            {"channel": POD, "archived": True},
            {"channel": POD},
        ):
            r = meta_post(c, bea, body)
            assert r.status_code == 409 and r.json()["error"] == "archived", body
        # roles too
        r = roles_post(c, ada, {"channel": POD, "pubkey": pk(cy), "role": "admin"})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        r = roles_post(c, ada, {"channel": POD, "pubkey": pk(cy), "role": "removed"})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        assert len(cw.grp.events) == n
        # a client that doesn't know the tag edits the group (its 9002 says nothing about it,
        # and the relay's mirror loses the tag): still archived
        time.sleep(1.05)
        cw.put(ada, 9002, [["h", POD], ["name", "Garden"], ["about", "Edited"]])
        assert checkin.is_archived(rsvc, POD) is True
        # only {"channel", "archived": false} passes
        r = meta_post(c, bea, {"channel": POD, "archived": False})
        assert r.status_code == 200 and r.json()["archived"] is False
        e = cw.grp.events[-1]
        assert e["kind"] == 9002 and ["archived", "0"] in e["tags"] and ["closed"] in e["tags"]
        assert not any(t == ["archived", "1"] for t in e["tags"])
        assert checkin.is_archived(rsvc, POD) is False
        # and it can be changed again
        r = meta_post(c, bea, {"channel": POD, "name": "Roof garden"})
        assert r.status_code == 200 and r.json()["archived"] is False
        r = roles_post(c, ada, {"channel": POD, "pubkey": pk(cy), "role": "removed"})
        assert r.status_code == 200, r.text


def test_general_events_and_proposals_are_never_archived(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    for group in ("general", "ev-20261201-abcd", "prop-roof-1a2b"):
        cw.put(ada, 9007, [["h", group]])
        cw.put(cw.gkey, 39000, [["d", group], ["name", group]])
    with TestClient(create_app(rsvc)) as c:
        r = meta_post(c, ada, {"channel": "general", "archived": True})
        assert r.status_code == 409 and r.json()["error"] == "always_open"
        for group in ("ev-20261201-abcd", "prop-roof-1a2b"):
            r = meta_post(c, ada, {"channel": group, "archived": True})
            assert r.status_code == 422 and r.json()["error"] == "invalid"
        assert not any(e["kind"] == 9002 for e in cw.grp.events)
        # "archived": false on a group that is not archived just says so
        r = meta_post(c, ada, {"channel": "general", "archived": False})
        assert r.status_code == 200 and r.json()["archived"] is False


def test_is_archived_reads_the_latest_that_says_so(rsvc, cw):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    now = int(time.time())
    cw.put(ada, 9007, [["h", POD]], at=now - 50)
    cw.put(ada, 9002, [["h", POD], ["name", "G"], ["archived", "1"]], at=now - 40)
    assert checkin.is_archived(rsvc, POD) is True
    cw.put(ada, 9002, [["h", POD], ["name", "G"]], at=now - 30)  # says nothing
    assert checkin.is_archived(rsvc, POD) is True
    cw.put(ada, 9002, [["h", POD], ["name", "G"], ["archived", "0"]], at=now - 20)
    assert checkin.is_archived(rsvc, POD) is False
    # another group's tag never counts
    cw.put(ada, 9002, [["h", "pod-other"], ["archived", "1"]], at=now - 10)
    assert checkin.is_archived(rsvc, POD) is False
    assert checkin.is_archived(rsvc, "pod-other") is True
    assert checkin.is_archived(rsvc, "pod-unknown") is False


def test_no_guest_links_or_guests_for_an_archived_group(services, tmp_path, settings, monkeypatch):
    from belfry_onboarding.register import Register

    from .test_guests import admin_with_key
    from .test_holder_invites import BASE, post

    services.odoo = Register(tmp_path / "register.sqlite", settings)
    rsvc = services
    rsvc.settings.public_url = BASE
    monkeypatch.setattr(guests, "_closed", lambda svc, channel: False)
    monkeypatch.setattr(guests, "_channel_event", lambda svc, kind, tags: True)
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: True)
    monkeypatch.setattr(mail, "group_admins", lambda svc, ch: set())
    archived: set[str] = set()
    monkeypatch.setattr(checkin, "is_archived", lambda svc, ch: ch in archived)
    key, _ = admin_with_key(rsvc)
    with TestClient(create_app(rsvc)) as c:
        r = post(c, key, guests.API, {"channel": "garden"})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("/", 1)[-1]
        archived.add("garden")
        time.sleep(1.1)
        r = post(c, key, guests.API, {"channel": "garden"})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        r = post(c, key, guests.ADD, {"channel": "garden", "pubkey": "a" * 64})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        # a link made before brings nobody in
        r = post(c, key, f"/api/channel-invites/{token}/join", {})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        # another group is untouched
        time.sleep(1.1)
        assert post(c, key, guests.API, {"channel": "kitchen"}).status_code == 200


def delete_post(c, key, data):
    import json

    from .test_checkin import HOST, nip98  # noqa: F401

    body = json.dumps(data).encode()
    time.sleep(1.05)
    url = "https://belfry.belfry.test/api/v1/group-delete"
    return c.post(
        "/api/v1/group-delete",
        content=body,
        headers={**HOST, "Authorization": nip98(key, url, "POST", body)},
    )


def test_delete_an_empty_channel_archive_one_with_messages(rsvc, cw, keycast):
    """Xavier, 2026-11-17: Settings → Channels deletes an empty channel, archives the others."""
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    garden(cw, ada, bea, cy)
    with TestClient(create_app(rsvc)) as c:
        assert checkin.is_empty(rsvc, POD) is True
        r = delete_post(c, cy, {"channel": POD})
        assert r.status_code == 403 and r.json()["error"] == "not_allowed"
        assert delete_post(c, bea, {"channel": "general"}).json()["error"] == "always_open"
        # somebody wrote: not empty, archive it instead
        cw.put(cy, 9, [["h", POD]], "hello")
        assert checkin.is_empty(rsvc, POD) is False
        r = delete_post(c, bea, {"channel": POD})
        assert r.status_code == 409 and r.json()["error"] == "not_empty"
        assert not any(e["kind"] == 9008 for e in cw.grp.events)
        # an empty one goes: a 9008 by the groups key
        garden(cw, ada, bea, cy, "pod-empty")
        r = delete_post(c, bea, {"channel": "pod-empty"})
        assert r.status_code == 200 and r.json() == {"channel": "pod-empty", "deleted": True}
        e = cw.grp.events[-1]
        assert e["kind"] == 9008 and e["pubkey"] == pk(cw.gkey) and ["h", "pod-empty"] in e["tags"]
