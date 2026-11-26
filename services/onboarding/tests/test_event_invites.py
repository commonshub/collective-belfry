"""People a host invites to an event (the ``["p", <key>, "", "invited"]`` tags on its NIP-52
version) are told once by the watcher: a push and an e-mail (events.invite, docs/checkin.md)."""
# ruff: noqa: F811 — the fixtures come from test_events and test_cohost_invites

from __future__ import annotations

import json
import time

from coincurve import PrivateKey

from belfry_onboarding import community, events, mailer, push
from belfry_onboarding.nip46check import nip44_decrypt

from .test_cohost_invites import FakeGateway, phones, tier  # noqa: F401 — fixtures
from .test_events import (  # noqa: F401 — fixtures
    GROUP,
    D,
    World,
    make_event,
    member,
    pk,
    rsvc,
    world,
)


def version(
    w: World, by: PrivateKey, people: list[PrivateKey], at=None, start_in=7 * 86400, **tags
):
    """A version of the event by BY that invites PEOPLE."""
    start = int(time.time()) + start_in
    return w.put(
        by,
        31923,
        [
            ["d", D],
            ["title", "Garden day"],
            ["start", str(start)],
            ["end", str(start + 3 * 3600)],
            ["start_tzid", "Europe/Brussels"],
            ["location", "The garden"],
            ["h", GROUP],
            *[[k, v] for k, v in tags.items()],
            *[["p", pk(p), "", "invited"] for p in people],
        ],
        at=at,
    )


def subjects() -> list[str]:
    return [m["Subject"] for m in mailer.SENT]


def named(monkeypatch, **who) -> None:
    monkeypatch.setattr(
        events, "names", lambda svc, pks: {k: v for k, v in who.items() if k in pks}
    )


def test_an_invited_person_is_told_once_by_push_and_by_mail(rsvc, world, phones, monkeypatch):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    named(monkeypatch, **{pk(ada): "Ada"})
    make_event(world, ada)
    devkey = phones(bea)
    assert events.watch(rsvc)["invited"] == 0  # nobody invited yet
    version(world, ada, [bea], at=int(time.time()) + 1)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 1
    # the mail: who, what, when in the event's own time zone, where, a button to the event
    [m] = mailer.SENT
    assert m["To"] == "bea@belfry.test" and m["Subject"] == "Ada invited you to Garden day"
    text = m.get_body(preferencelist=("plain",)).get_content()
    assert "(Europe/Brussels)" in text and "Where: The garden." in text
    assert f"https://chat.belfry.test/#/events/{D}" in text
    assert pk(ada) not in text and pk(bea) not in text
    # the push: the new type, to her phone
    [(dev, payload, collapse, urgent)] = FakeGateway.sent
    plain = json.loads(nip44_decrypt(devkey, payload))
    assert plain["type"] == "event_invite" and plain["title"] == "Ada invited you"
    assert plain["body"].startswith("Garden day · ") and plain["channel"] == GROUP
    assert plain["url"] == f"https://chat.belfry.test/#/events/{D}"
    assert collapse == GROUP and urgent is True
    # the next rounds, and a new version that still invites her: nothing more
    assert events.watch(rsvc)["invited"] == 0
    version(world, ada, [bea], at=int(time.time()) + 2, summary="New words")
    assert events.watch(rsvc)["invited"] == 0
    assert len(mailer.SENT) == 1 and len(FakeGateway.sent) == 1
    # also after a restart: the record is in events.sqlite
    events._stores.clear()
    assert events.watch(rsvc)["invited"] == 0 and len(mailer.SENT) == 1


def test_a_host_without_a_name_is_never_a_key(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    version(world, ada, [bea], at=int(time.time()) + 1)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 1
    assert subjects() == [f"{community.UNKNOWN} invited you to Garden day"]


def test_someone_added_later_is_told_and_the_others_are_not_told_again(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    make_event(world, ada)
    now = int(time.time())
    version(world, ada, [bea], at=now + 1)
    assert events.watch(rsvc)["invited"] == 1
    mailer.SENT.clear()
    version(world, ada, [bea, cy], at=now + 2)
    assert events.watch(rsvc)["invited"] == 1
    assert [m["To"] for m in mailer.SENT] == ["cy@belfry.test"]


def test_taken_off_and_put_back_is_not_told_again(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    now = int(time.time())
    version(world, ada, [bea], at=now + 1)
    assert events.watch(rsvc)["invited"] == 1
    version(world, ada, [], at=now + 2)
    assert events.watch(rsvc)["invited"] == 0
    version(world, ada, [bea], at=now + 3)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 0 and mailer.SENT == []


def test_not_the_author_and_not_someone_who_answered(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    make_event(world, ada)
    now = int(time.time())
    # Bea answered already (an RSVP on the community relay); Ada invites herself too
    world.put(bea, 31925, [["a", f"31923:{pk(ada)}:{D}"], ["status", "declined"]], at=now)
    version(world, ada, [ada, bea, cy], at=now + 1)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 1
    assert [m["To"] for m in mailer.SENT] == ["cy@belfry.test"]
    # Bea is not told later either, were her answer to go
    world.comm.events = [e for e in world.comm.events if e["kind"] != 31925]
    assert events.watch(rsvc)["invited"] == 0


def test_a_co_host_who_edits_is_not_told_about_their_own_version(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    make_event(world, ada)
    world.put(world.gkey, 9000, [["h", GROUP], ["p", pk(bea), "admin"], ["role", "admin"]])
    version(world, bea, [bea, cy], at=int(time.time()) + 1)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 1
    assert [m["To"] for m in mailer.SENT] == ["cy@belfry.test"]


def test_a_past_or_a_cancelled_event_tells_nobody(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    now = int(time.time())
    version(world, ada, [bea], at=now + 1, start_in=-2 * 86400)  # over since yesterday
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 0 and mailer.SENT == []
    version(world, ada, [bea], at=now + 2, status="cancelled")
    assert events.watch(rsvc)["invited"] == 0 and mailer.SENT == []
    # back on, in the future: now she is told (nothing was recorded while it could not be sent)
    version(world, ada, [bea], at=now + 3)
    assert events.watch(rsvc)["invited"] == 1


def test_a_version_by_someone_who_is_not_a_host_counts_for_nothing(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    mal = member(rsvc, "mal@belfry.test", "Mal")
    make_event(world, ada)
    version(world, mal, [bea], at=int(time.time()) + 5)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 0 and mailer.SENT == []


def test_people_invited_long_before_the_watcher_first_looks_are_not_flooded(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    make_event(world, ada)
    now = int(time.time())
    # a meeting that carried the tags two hours before this watcher ever ran
    world.comm.events = [e for e in world.comm.events if e["kind"] != 31923]
    version(world, ada, [bea], at=now - 7200)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 0 and mailer.SENT == []
    # Bea counts as told for good; someone invited from now on is told
    version(world, ada, [bea, cy], at=now + 1)
    assert events.watch(rsvc)["invited"] == 1
    assert [m["To"] for m in mailer.SENT] == ["cy@belfry.test"]


def test_a_version_of_the_last_hour_is_sent_on_the_first_look(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    make_event(world, ada)
    world.comm.events = [e for e in world.comm.events if e["kind"] != 31923]
    version(world, ada, [bea], at=int(time.time()) - 600)
    assert events.watch(rsvc)["invited"] == 1


def test_no_pass_no_word_and_an_agent_gets_no_mail(rsvc, world, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bot = member(rsvc, "bot@belfry.test", "Bot")
    tier(rsvc, bot, "agent")
    phones(bot)
    stranger = PrivateKey()
    make_event(world, ada)
    version(world, ada, [bot, stranger], at=int(time.time()) + 1)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 1  # the agent: a push, no mail
    assert mailer.SENT == [] and len(FakeGateway.sent) == 1


def test_only_a_proven_address_is_mailed(rsvc, world):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    row = rsvc.odoo.search_read("belfry.pass", [["keycast_pubkey", "=", pk(bea)]], ["partner_id"])
    partner = row[0]["partner_id"][0]
    # her address is not proven yet: it waits aside, the contact has none
    rsvc.odoo.write(
        "res.partner", [partner], {"email": False, "belfry_pending_email": "bea@belfry.test"}
    )
    make_event(world, ada)
    version(world, ada, [bea], at=int(time.time()) + 1)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 1 and mailer.SENT == []


def test_the_push_follows_the_mentions_switch_and_the_events_level(rsvc, world, phones):
    ada = member(rsvc, "ada@belfry.test", "Ada")
    bea = member(rsvc, "bea@belfry.test", "Bea")
    cy = member(rsvc, "cy@belfry.test", "Cy")
    phones(bea)
    phones(cy)
    for key, change in ((bea, {"mentions": False}), (cy, {"channels": {GROUP: "off"}})):
        row = rsvc.odoo.search_read(
            "belfry.pass", [["keycast_pubkey", "=", pk(key)]], ["partner_id"]
        )
        push.set_settings(rsvc, row[0]["partner_id"][0], change)
    make_event(world, ada)
    version(world, ada, [bea, cy], at=int(time.time()) + 1)
    mailer.SENT.clear()
    assert events.watch(rsvc)["invited"] == 2
    assert FakeGateway.sent == [] and len(mailer.SENT) == 2  # the e-mail still goes
