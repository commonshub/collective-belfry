"""An event's conversation, photos, links and participants for the website (eventchat.py,
docs/website-accounts.md): who reads, what a message looks like, paging, a message posted as
the person the way the web chat writes one, and that no answer names anybody by a key."""

# ruff: noqa: F811
from __future__ import annotations

import itertools
import json
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import (
    apps,
    checkin,
    community,
    eventchat,
    mail,
    nostr,
    sitesessions,
    usernames,
    villagers,
)
from belfry_onboarding.app import create_app

from .test_events import GROUP, D
from .test_guests import _code, published, rsvc  # noqa: F401 — fixtures
from .test_roles import call, person, pk, world  # noqa: F401 — fixtures
from .test_sales import shop  # noqa: F401 — fixtures
from .test_sales_odoo import seller  # noqa: F401 — fixtures
from .test_sales_stripe import stripe  # noqa: F401 — fixtures
from .test_site_sessions import KIM, LEO, guest, login, sign_as, site  # noqa: F401
from .test_testers import envs  # noqa: F401 — fixtures
from .test_ticket_sales import fair, tk  # noqa: F401 — fixtures

H = {"Host": "belfry.belfry.test"}
BLOSSOM = "https://blossom.belfry.test"
PIC = f"{BLOSSOM}/{'1' * 64}.jpg"
PIC2 = f"{BLOSSOM}/{'2' * 64}.png"
PDF = f"{BLOSSOM}/{'3' * 64}.pdf"
CHAT = f"/events/{D}/chat"


class Room:
    """The event's group on the fake relay, and the people of these tests."""

    def __init__(self, rsvc, w, ada):
        self.rsvc, self.w, self.ada = rsvc, w, ada
        self.at = itertools.count(int(time.time()) - 5000)
        self.secrets: list[str] = [pk(ada), nostr.hex_to_npub(pk(ada)), "admin@belfry.test"]

    def knows(self, pubkey: str, *more: str) -> None:
        self.secrets += [pubkey, nostr.hex_to_npub(pubkey), *more]

    def put(self, pubkey: str, kind: int, tags: list[list[str]], content: str = "") -> dict:
        """An event in the group by PUBKEY (a key the signer holds), a second after the last."""
        e = sign_as(pubkey)(
            {
                "kind": kind,
                "created_at": next(self.at),
                "tags": [["h", GROUP], *tags],
                "content": content,
            }
        )
        self.w.grp.events.append(e)
        eventchat.forget()
        return e

    def admit(self, pubkey: str, *more: list[str], role: str = "member") -> None:
        self.put(pk(self.w.gkey), 9000, [["p", pubkey, role], *more])

    def label(self, ns: str, value: str, pubkey: str) -> None:
        self.put(pk(self.w.gkey), 1985, [["L", ns], ["l", value, ns], ["p", pubkey]])

    def clean(self, *answers) -> None:
        """No answer names anybody by a key, an npub or an address."""
        text = "\n".join(a.text for a in answers)
        for secret in self.secrets:
            assert secret not in text, secret[:12]
        assert "npub1" not in text and "nip05" not in text and "pubkey" not in text


@pytest.fixture
def room(site, monkeypatch):
    rsvc, w, ada = site
    monkeypatch.setattr(checkin, "groups_pubkey", lambda svc: pk(w.gkey))
    monkeypatch.setattr(mail, "channel_exists", lambda svc, ch: ch in ("general", "requests"))
    monkeypatch.setattr(mail, "channel_by_slug", lambda svc, slug: None)
    usernames._taken.clear()
    villagers.forget_names()
    rsvc.settings.blossom_public_url = BLOSSOM
    eventchat.forget()
    eventchat.forget_people()
    tk(w, ada, "day", "Day ticket")
    r = Room(rsvc, w, ada)
    yield r
    eventchat.forget()
    eventchat.forget_people()


def kim_in(c, room: Room) -> tuple[PrivateKey, str]:
    """Kim, a guest logged in on the website, admitted with a day ticket."""
    key = guest(c)
    kim = sitesessions.site_account(room.rsvc, pk(key))["pubkey"]
    room.knows(kim, KIM)
    room.admit(kim, ["ticket", f"{D}:day"])
    return key, kim


def get(c, key, path):
    return call(c, key, path, method="GET")


# ── who reads ───────────────────────────────────────────────────────────────────────────────
def test_only_the_people_of_the_event_read_it(room, monkeypatch):
    rsvc, w, ada = room.rsvc, room.w, room.ada
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    cy, _ = person(rsvc, "cy@belfry.test", "Cy")
    hana, _ = person(rsvc, "hana@belfry.test", "Hana")
    dan, _ = person(rsvc, "dan@belfry.test", "Dan")
    room.admit(pk(bob))  # a member, in without a ticket
    room.admit(pk(hana), role="admin")  # a co-host
    paths = [CHAT, f"/events/{D}/photos", f"/events/{D}/links", f"/events/{D}/participants"]
    with TestClient(create_app(rsvc)) as c:
        kim, _kim_pk = kim_in(c, room)
        leo = guest(c, LEO, "Leo")  # a guest of the requests channel only
        for path in paths:
            for key in (kim, bob, hana, ada):
                r = get(c, key, path)
                assert r.status_code == 200, (path, r.text)
                assert r.headers["cache-control"] == "no-store"
            for key in (leo, cy, PrivateKey()):
                r = get(c, key, path)
                assert r.status_code == 403 and r.json()["error"] == "not_in", (path, r.text)
            r = c.get("/api/v1" + path, headers=H)
            assert r.status_code == 401 and r.json()["error"] == "unauthorized"
            r = get(c, kim, path.replace(D, "2026-zzzzzz"))
            assert r.status_code == 404 and r.json()["error"] == "unknown"
        # an admin of the belfry who is not in the group
        assert get(c, dan, CHAT).status_code == 403
        admins = apps.admin_pubkeys(rsvc)
        monkeypatch.setattr(apps, "admin_pubkeys", lambda svc: {*admins, pk(dan)})
        assert get(c, dan, CHAT).status_code == 200
        # their website session too
        dk = PrivateKey()
        assert login(c, dk, "dan@belfry.test").json()["role"] == "admin"
        assert get(c, dk, CHAT).status_code == 200
        # taken out of the event: no longer
        room.put(pk(w.gkey), 9001, [["p", pk(bob)]])
        assert get(c, bob, CHAT).status_code == 403
        # 120 reads a minute per account
        c.app.state.event_chat_reads.__init__(1, 60)
        assert get(c, kim, CHAT).status_code == 200
        r = get(c, kim, CHAT)
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        assert get(c, hana, CHAT).status_code == 200


def test_without_the_own_register_it_is_unsupported(room, monkeypatch):
    monkeypatch.setattr(sitesessions, "own", lambda svc: False)
    with TestClient(create_app(room.rsvc)) as c:
        r = get(c, room.ada, CHAT)
        assert r.status_code == 409 and r.json()["error"] == "unsupported"


# ── what a message looks like ───────────────────────────────────────────────────────────────
def test_the_messages_as_text_and_pictures(room):
    rsvc, w, ada = room.rsvc, room.w, room.ada
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    room.knows(pk(bob), "bob@belfry.test")
    w.put(bob, 0, [], json.dumps({"name": "Bob", "picture": "https://pics.example/bob.png"}))
    room.admit(pk(bob))
    stray = PrivateKey()  # someone who opened a link and never joined
    room.knows(pk(stray))
    room.admit(pk(stray), ["unsaved", "1"], ["reading", "1"], role="member")
    with TestClient(create_app(rsvc)) as c:
        kim, kim_pk = kim_in(c, room)
        first = room.put(pk(bob), 9, [], "Who brings the beans?")
        typo = room.put(kim_pk, 9, [], "I brnig them")
        room.put(kim_pk, 1010, [["e", typo["id"]], ["k", "9"]], "I bring them")
        room.put(pk(bob), 1010, [["e", typo["id"]], ["k", "9"]], "Hacked")  # not the author's
        gone = room.put(pk(bob), 9, [], "Never mind")
        room.put(pk(bob), 5, [["e", gone["id"]]])
        kept = room.put(pk(bob), 9, [], "Stays")
        room.put(kim_pk, 5, [["e", kept["id"]]])  # somebody else's delete counts for nothing
        removed = room.put(kim_pk, 9, [], "Spam")
        room.put(pk(ada), 9005, [["e", removed["id"]]])
        npubs = [nostr.hex_to_npub(pk(k)) for k in (bob, stray)]
        hi = f"Thanks nostr:{npubs[0]} and nostr:{npubs[1]}!"
        mention = room.put(kim_pk, 9, [["p", pk(bob)]], hi)
        both = room.put(
            pk(bob),
            9,
            [
                ["imeta", f"url {PIC}", "m image/jpeg", "dim 480x360", "alt beans.jpg"],
                ["imeta", f"url {PDF}", "m application/pdf", "alt plan.pdf"],
            ],
            f"The plan\n{PIC}\n{PDF}",
        )
        bare = room.put(kim_pk, 9, [], f"Look {PIC2} and https://example.org/map.")
        reply = room.put(
            kim_pk,
            1111,
            [
                ["E", first["id"], "", pk(bob)],
                ["K", "9"],
                ["P", pk(bob)],
                ["e", first["id"], "", pk(bob)],
                ["k", "9"],
                ["p", pk(bob)],
            ],
            "I do",
        )
        # not messages: the chat's cards, a to-do, a poll, a reading guest's words
        room.put(pk(ada), 9, [["a", f"31923:{pk(ada)}:{D}"]], "📅 Garden day · tomorrow")
        room.put(pk(ada), 9, [["pod", "pod-diggers"]], "⬡ New group: Diggers")
        room.put(
            pk(ada), 9, [["proposal", "prop-x"], ["a", f"30023:{pk(ada)}:x"]], "📝 Proposal: X"
        )
        room.put(pk(ada), 2100, [], "Bring gloves")
        room.put(pk(ada), 1068, [["option", "a", "Yes"]], "Rain?")
        room.put(pk(stray), 9, [], "Anybody here?")
        r = get(c, kim, CHAT)
        got = r.json()
        assert got["more"] is False
        ms = got["messages"]
        assert [m["id"] for m in ms] == [
            first["id"],
            typo["id"],
            kept["id"],
            mention["id"],
            both["id"],
            bare["id"],
            reply["id"],
        ]
        assert set(ms[0]) == {
            "id", "at", "by", "mine", "text", "images", "files", "reply_to", "edited_at"
        }  # fmt: skip
        assert ms[0]["by"] == {
            "name": "Bob",
            "picture": "https://pics.example/bob.png",
            "slug": ms[0]["by"]["slug"],
            "role": "attendee",
        }
        assert ms[0]["at"] == first["created_at"] and ms[0]["mine"] is False
        assert (ms[0]["reply_to"], ms[0]["edited_at"], ms[0]["images"]) == (None, None, [])
        # the author's latest edit, and only theirs
        assert ms[1]["text"] == "I bring them" and ms[1]["edited_at"] == typo["created_at"] + 1
        assert ms[1]["mine"] is True
        assert ms[1]["by"] == {
            "name": "Kim Cook",
            "picture": None,
            "slug": "kim-cook",
            "role": "attendee",
        }
        # mentions by name; someone nobody can name is "Somebody"
        assert ms[3]["text"] == "Thanks @Bob and @Somebody!"
        # a picture and a file: out of the text
        assert ms[4]["text"] == "The plan"
        assert ms[4]["images"] == [{"url": PIC, "alt": "beans.jpg"}]
        assert ms[4]["files"] == [{"url": PDF, "type": "application/pdf", "name": "plan.pdf"}]
        # a bare picture address on the belfry's own file server
        assert ms[5]["text"] == "Look and https://example.org/map."
        assert ms[5]["images"] == [{"url": PIC2, "alt": ""}]
        assert ms[6]["reply_to"] == first["id"] and ms[6]["text"] == "I do"
        # Bob reads the same, his own marked
        theirs = get(c, bob, CHAT)
        assert [m["mine"] for m in theirs.json()["messages"]] == [
            True,
            False,
            True,
            False,
            True,
            False,
            False,
        ]

        # photos, newest first
        ph = get(c, kim, f"/events/{D}/photos")
        assert ph.json() == {
            "photos": [
                {
                    "url": PIC2,
                    "alt": "",
                    "at": bare["created_at"],
                    "by": ms[5]["by"],
                    "message": bare["id"],
                },
                {
                    "url": PIC,
                    "alt": "beans.jpg",
                    "at": both["created_at"],
                    "by": ms[4]["by"],
                    "message": both["id"],
                },
            ],
            "more": False,
        }
        one = get(c, kim, f"/events/{D}/photos?limit=1").json()
        assert [p["url"] for p in one["photos"]] == [PIC2] and one["more"] is True
        older = get(c, kim, f"/events/{D}/photos?limit=1&before={bare['created_at']}").json()
        assert [p["url"] for p in older["photos"]] == [PIC] and older["more"] is False

        # links: what was written, not what was attached
        room.put(
            pk(bob),
            9,
            [],
            "Map: https://example.org/map, and https://osm.org/x (again https://osm.org/x)",
        )
        ln = get(c, kim, f"/events/{D}/links")
        rows = ln.json()["links"]
        assert [(x["url"], x["by"]["name"]) for x in rows] == [
            ("https://example.org/map", "Bob"),
            ("https://osm.org/x", "Bob"),
            ("https://example.org/map", "Kim Cook"),
        ]
        assert rows[2]["message"] == bare["id"] and rows[2]["at"] == bare["created_at"]
        assert set(rows[0]) == {"url", "at", "by", "message"}
        room.clean(r, theirs, ph, ln)


def test_paging(room):
    rsvc = room.rsvc
    with TestClient(create_app(rsvc)) as c:
        kim, kim_pk = kim_in(c, room)
        sent = [room.put(kim_pk, 9, [], f"Message {i}") for i in range(7)]
        got = get(c, kim, CHAT + "?limit=3").json()
        assert [m["text"] for m in got["messages"]] == ["Message 4", "Message 5", "Message 6"]
        assert got["more"] is True
        got = get(c, kim, f"{CHAT}?limit=3&before={got['messages'][0]['at']}").json()
        assert [m["text"] for m in got["messages"]] == ["Message 1", "Message 2", "Message 3"]
        assert got["more"] is True
        got = get(c, kim, f"{CHAT}?limit=3&before={sent[1]['created_at']}").json()
        assert [m["text"] for m in got["messages"]] == ["Message 0"] and got["more"] is False
        assert len(get(c, kim, CHAT).json()["messages"]) == 7
        for bad in ("limit=0", "limit=101", "limit=x", "before=-1"):
            r = get(c, kim, f"{CHAT}?{bad}")
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad


def test_what_was_read_is_kept_ten_seconds(room):
    with TestClient(create_app(room.rsvc)) as c:
        kim, kim_pk = kim_in(c, room)
        room.put(kim_pk, 9, [], "One")
        assert len(get(c, kim, CHAT).json()["messages"]) == 1
        e = sign_as(kim_pk)({"kind": 9, "tags": [["h", GROUP]], "content": "Two"})
        room.w.grp.events.append(e)  # written elsewhere: seen when the read is made again
        assert len(get(c, kim, CHAT).json()["messages"]) == 1
        from belfry_onboarding import relay

        for told in relay.PUBLISHED:  # anything this server publishes
            told()
        assert len(get(c, kim, CHAT).json()["messages"]) == 2


# ── writing ─────────────────────────────────────────────────────────────────────────────────
def test_a_message_is_posted_as_the_person(room, monkeypatch):
    rsvc, w, ada = room.rsvc, room.w, room.ada
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    room.knows(pk(bob), "bob@belfry.test")
    room.admit(pk(bob))
    answers = []
    with TestClient(create_app(rsvc)) as c:
        kim, kim_pk = kim_in(c, room)
        leo = guest(c, LEO, "Leo")
        r = call(c, leo, CHAT, {"text": "Hi"})
        assert r.status_code == 403 and r.json()["error"] == "not_in"
        r = c.post("/api/v1" + CHAT, json={"text": "Hi"}, headers=H)
        assert r.status_code == 401
        # plain, as the web chat writes it
        r = call(c, kim, CHAT, {"text": "  Hello #Garden people  "})
        assert r.status_code == 201 and r.headers["cache-control"] == "no-store", r.text
        answers.append(r)
        stored = w.grp.events[-1]
        assert (stored["kind"], stored["pubkey"], stored["content"]) == (
            9,
            kim_pk,
            "Hello #Garden people",
        )
        assert stored["tags"] == [["h", GROUP], ["t", "garden"]]
        one = r.json()
        assert one == {
            "id": stored["id"],
            "at": stored["created_at"],
            "by": {"name": "Kim Cook", "picture": None, "slug": "kim-cook", "role": "attendee"},
            "mine": True,
            "text": "Hello #Garden people",
            "images": [],
            "files": [],
            "reply_to": None,
            "edited_at": None,
        }
        assert get(c, kim, CHAT).json()["messages"][-1] == one  # read at once
        # with pictures, and a mention
        npub = nostr.hex_to_npub(pk(bob))
        r = call(c, kim, CHAT, {"text": f"For nostr:{npub}", "images": [PIC, PIC2, PIC]})
        assert r.status_code == 201, r.text
        answers.append(r)
        stored = w.grp.events[-1]
        assert stored["content"] == f"For nostr:{npub}\n{PIC}\n{PIC2}"
        assert stored["tags"] == [
            ["h", GROUP],
            ["p", pk(bob)],
            ["imeta", f"url {PIC}", "m image/jpeg", f"x {'1' * 64}"],
            ["imeta", f"url {PIC2}", "m image/png", f"x {'2' * 64}"],
        ]
        assert r.json()["text"] == "For @Somebody"  # Bob has no profile
        assert r.json()["images"] == [{"url": PIC, "alt": ""}, {"url": PIC2, "alt": ""}]
        # pictures alone
        r = call(c, kim, CHAT, {"text": "", "images": [PIC]})
        assert r.status_code == 201 and w.grp.events[-1]["content"] == PIC
        assert r.json()["text"] == ""
        # a reply to a message, tagged as the web chat tags a thread reply
        first = room.put(pk(bob), 9, [], "Who brings the beans?")
        r = call(c, kim, CHAT, {"text": "I do", "reply_to": first["id"]})
        assert r.status_code == 201, r.text
        answers.append(r)
        reply = w.grp.events[-1]
        assert (reply["kind"], reply["pubkey"], reply["content"]) == (1111, kim_pk, "I do")
        assert reply["tags"] == [
            ["h", GROUP],
            ["E", first["id"], "", pk(bob)],
            ["K", "9"],
            ["P", pk(bob)],
            ["e", first["id"], "", pk(bob)],
            ["k", "9"],
            ["p", pk(bob)],
        ]
        assert r.json()["reply_to"] == first["id"]
        # a reply to a reply stays in the same thread
        r = call(c, bob, CHAT, {"text": "Thanks", "reply_to": reply["id"]})
        assert r.status_code == 201, r.text
        again = w.grp.events[-1]
        assert again["pubkey"] == pk(bob) and again["tags"] == [
            ["h", GROUP],
            ["E", first["id"], "", pk(bob)],
            ["K", "9"],
            ["P", pk(bob)],
            ["e", reply["id"], "", kim_pk],
            ["k", "1111"],
            ["p", kim_pk],
        ]
        assert r.json()["reply_to"] == reply["id"]
        # what is refused
        n = len(w.grp.events)
        for bad in (
            {"text": ""},
            {"text": "   "},
            {"text": "x" * 4001},
            {"text": 5},
            {"text": "Hi", "images": ["https://evil.example/" + "1" * 64 + ".jpg"]},
            {"text": "Hi", "images": [BLOSSOM + "/upload"]},
            {"text": "Hi", "images": [PIC.replace("https://", "http://")]},
            {"text": "Hi", "images": [PDF]},
            {"text": "Hi", "images": [PIC + "?x=1"]},
            {"text": "Hi", "images": [PIC] * 7},
            {"text": "Hi", "images": PIC},
            {"text": "Hi", "reply_to": "f" * 64},
            {"text": "Hi", "reply_to": "nope"},
        ):
            r = call(c, kim, CHAT, bad)
            assert r.status_code == 422 and r.json()["error"] == "invalid", bad
        assert call(c, kim, CHAT, {"text": "x" * 4000}).status_code == 201
        assert len(w.grp.events) == n + 1
        # someone who holds their own key: the belfry can't sign for them
        with monkeypatch.context() as m:
            m.setattr(community, "member_signer", lambda svc, pid: None)
            r = call(c, kim, CHAT, {"text": "Hi"})
            assert r.status_code == 409 and r.json()["error"] == "own_key"

            def down(svc, pid):
                raise RuntimeError("signer down")

            m.setattr(community, "member_signer", down)
            r = call(c, kim, CHAT, {"text": "Hi"})
            assert r.status_code == 503 and r.json()["error"] == "signer"
        # the relay not taking it
        w.grp.fail = True
        r = call(c, kim, CHAT, {"text": "Hi"})
        w.grp.fail = False
        assert r.status_code == 503 and r.json()["error"] == "unavailable"
        # someone who only reads does not write
        room.admit(kim_pk, ["unsaved", "1"], ["reading", "1"])
        r = call(c, kim, CHAT, {"text": "Hi"})
        assert r.status_code == 403 and r.json()["error"] == "not_in"
        room.admit(kim_pk, ["ticket", f"{D}:day"])
        # 30 an hour per account
        monkeypatch.setattr(eventchat, "POSTS_PER_HOUR", 1)
        mail._counts.clear()
        assert call(c, kim, CHAT, {"text": "One"}).status_code == 201
        r = call(c, kim, CHAT, {"text": "Two"})
        assert r.status_code == 429 and r.json()["error"] == "too_many"
        assert call(c, bob, CHAT, {"text": "Mine"}).status_code == 201
        # an archived group takes nothing
        room.put(pk(ada), 9002, [["archived", "1"]])
        r = call(c, bob, CHAT, {"text": "Still?"})
        assert r.status_code == 409 and r.json()["error"] == "archived"
        room.clean(*answers)


# ── who is there ────────────────────────────────────────────────────────────────────────────
def test_the_participants(room, keycast):
    rsvc, w, ada = room.rsvc, room.w, room.ada
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    hana, _ = person(rsvc, "hana@belfry.test", "Hana")
    zoe, _ = person(rsvc, "zoe@belfry.test", "Zoe")
    for key, name in ((ada, "Ada"), (bob, "Bob"), (hana, "Hana"), (zoe, "Zoe")):
        w.put(key, 0, [], json.dumps({"name": name}))
        room.knows(pk(key), f"{name.lower()}@belfry.test")
    stray, bot, left = PrivateKey(), PrivateKey(), PrivateKey()
    room.knows(pk(stray))
    room.knows(pk(bot))
    room.knows(pk(left))
    room.admit(pk(ada), role="admin")  # the creator, as the relay lists them
    room.admit(pk(zoe), ["ticket", f"{D}:day"])
    room.admit(pk(bob))
    room.admit(pk(hana), role="admin")
    room.label(checkin.ROLE_NS, "steward", pk(bob))
    room.label(checkin.TICKET_NS, "checked-in", pk(zoe))
    room.admit(pk(stray), ["unsaved", "1"], ["reading", "1"])
    room.admit(pk(bot), ["agent", "1"])
    room.admit(pk(left))
    room.put(pk(w.gkey), 9001, [["p", pk(left)]])
    with TestClient(create_app(rsvc)) as c:
        kim, _kim_pk = kim_in(c, room)
        r = get(c, kim, f"/events/{D}/participants")
        got = r.json()
        assert got["count"] == 5
        assert [
            (p["name"], p["role"], p["ticket_title"], p["checked_in"]) for p in got["participants"]
        ] == [
            ("Ada", "host", None, False),
            ("Hana", "host", None, False),
            ("Bob", "door", None, False),
            ("Kim Cook", "attendee", "Day ticket", False),
            ("Zoe", "attendee", "Day ticket", True),
        ]
        assert set(got["participants"][0]) == {
            "name",
            "picture",
            "slug",
            "role",
            "ticket_title",
            "checked_in",
        }
        assert got["participants"][3]["slug"] == "kim-cook"
        room.clean(r)


def test_a_name_that_is_a_key_or_an_address_is_nobodys(room):
    rsvc, w = room.rsvc, room.w
    bob, _ = person(rsvc, "bob@belfry.test", "Bob")
    room.knows(pk(bob), "bob@belfry.test")
    w.put(bob, 0, [], json.dumps({"name": "bob@belfry.test", "picture": "javascript:alert(1)"}))
    room.admit(pk(bob))
    room.put(pk(bob), 9, [], "Hello")
    with TestClient(create_app(rsvc)) as c:
        r = get(c, bob, CHAT)
        by = r.json()["messages"][0]["by"]
        assert (by["name"], by["picture"]) == ("Somebody", None)
        room.clean(r)


# ── CORS ────────────────────────────────────────────────────────────────────────────────────
def test_the_website_reaches_it_and_its_previews_do_not(room):
    def preflight(c, path, origin, method):
        return c.options(
            "/api/v1" + path,
            headers={
                **H,
                "Origin": origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )

    with TestClient(create_app(room.rsvc)) as c:
        for name in ("chat", "photos", "links", "participants"):
            path = f"/events/{D}/{name}"
            for method in ("GET", "POST") if name == "chat" else ("GET",):
                r = preflight(c, path, "https://belfry.test", method)
                assert r.status_code == 204, (path, method)
                assert r.headers["access-control-allow-origin"] == "https://belfry.test"
                assert method in r.headers["access-control-allow-methods"]
                allowed = r.headers["access-control-allow-headers"].lower()
                assert "authorization" in allowed and "content-type" in allowed
                assert "access-control-allow-credentials" not in r.headers
                for origin in ("https://preview.belfry.test", "https://evil.example"):
                    assert preflight(c, path, origin, method).status_code == 403, (path, origin)
        # an answer itself: the apex may read it, a preview may not
        kim, _ = kim_in(c, room)
        for origin, ok in (("https://belfry.test", True), ("https://preview.belfry.test", False)):
            at = int(time.time())
            from .test_holder_invites import nip98

            auth = nip98(kim, f"https://belfry.belfry.test/api/v1{CHAT}", "GET", b"", created_at=at)
            r = c.get("/api/v1" + CHAT, headers={**H, "Origin": origin, "Authorization": auth})
            assert (r.headers.get("access-control-allow-origin") == origin) is ok
