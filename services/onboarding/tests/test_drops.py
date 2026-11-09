"""Drop box links (drops.py): upload anonymously, add a name, link to a member."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import community, drops, followers
from belfry_onboarding.app import create_app

TOKEN = "T" * 22
AUTHOR = "a" * 64
API = "f" * 64


class Relay:
    def __init__(self):
        self.events: list[dict] = []
        self.n = 0

    def add(self, **e):
        self.n += 1
        e.setdefault("id", f"{self.n:064x}")
        e.setdefault("created_at", int(time.time()))
        e.setdefault("content", "")
        self.events.append(e)
        return e

    def sign_as(self, pubkey):
        return lambda t: {
            **t,
            "pubkey": pubkey,
            "id": f"{(self.n + 1000 + len(self.events)):064x}",
            "sig": "s",
        }

    def publish(self, event, sign):
        self.events.append(event)

    def query(self, filters, sign):
        f = filters[0]
        out = []
        for e in self.events:
            if "kinds" in f and e["kind"] not in f["kinds"]:
                continue
            if "ids" in f and e["id"] not in f["ids"]:
                continue
            if "authors" in f and e["pubkey"] not in f["authors"]:
                continue
            ok = True
            for k, vals in f.items():
                if k.startswith("#"):
                    have = {t[1] for t in e["tags"] if t[0] == k[1:] and len(t) > 1}
                    ok = ok and bool(have & set(vals))
            if ok:
                out.append(e)
        return out


@pytest.fixture
def relay(monkeypatch):
    r = Relay()
    monkeypatch.setattr(followers, "_relay", lambda svc: (r, r.sign_as(API)))
    monkeypatch.setattr(followers, "channel_label", lambda svc, c: "#garden")
    monkeypatch.setattr(
        drops,
        "upload",
        lambda svc, files: [
            ["imeta", f"url https://blossom.test/{i}.jpg", "m image/jpeg"]
            for i, _ in enumerate(files)
        ],
    )
    drops._seen.clear()
    return r


def link(relay, login=False, exp=None):
    tags = [
        ["d", f"drop:{TOKEN}"],
        ["h", "garden"],
        ["expiration", str(exp or int(time.time()) + 3600)],
    ]
    if login:
        tags.append(["login"])
    return relay.add(kind=30078, pubkey=AUTHOR, tags=tags)


@pytest.fixture
def web(services, monkeypatch):
    from belfry_onboarding import theme

    services.settings.theme = "belfry"
    services.settings.osv_apps, services.settings.pass_signin = False, "belfry"
    services.settings.community_name = ""
    services.settings.nip05_domain = "belfry.test"
    monkeypatch.setattr(theme, "CURRENT", theme.load(services.settings))
    services.settings.site_url = "https://belfry.test"
    services.settings.public_url = "https://pass.belfry.test"
    services.settings.pass_page_secret = "p" * 32
    with TestClient(create_app(services), base_url="https://pass.belfry.test") as c:
        yield c


def hidden(page, name):
    return re.search(rf'name="{name}" value="([^"]+)"', page).group(1)


def test_unknown_expired_or_ended_links_do_not_work(web, relay):
    assert "does not work" in web.get(f"/drop/{TOKEN}").text
    link(relay, exp=int(time.time()) - 5)
    drops._seen.clear()
    assert "does not work" in web.get(f"/drop/{TOKEN}").text
    relay.events.clear()
    e = link(relay)
    relay.add(kind=5, pubkey=AUTHOR, tags=[["e", e["id"]]])
    drops._seen.clear()
    assert "does not work" in web.get(f"/drop/{TOKEN}").text
    assert web.get(f"/drop/{TOKEN}/qr.svg").status_code == 404


def test_anonymous_upload_then_a_name_then_a_member(web, relay, services, monkeypatch):
    link(relay)
    page = web.get(f"/drop/{TOKEN}").text
    assert "Upload as a guest" in page and 'type="email"' in page and 'type="file"' not in page
    assert 'href="https://belfry.test/chat/#/c/garden"' in page
    page = web.get(f"/drop/{TOKEN}", params={"as": "guest"}).text
    assert "Send files to" in page and 'type="file"' in page
    assert web.get(f"/drop/{TOKEN}/qr.svg").headers["content-type"].startswith("image/svg")
    r = web.post(
        f"/drop/{TOKEN}",
        files=[("files", ("a.jpg", b"x", "image/jpeg")), ("files", ("b.jpg", b"y", "image/jpeg"))],
    )
    assert "2 files sent to" in r.text and "Add my name" in r.text and "Open #garden" in r.text
    [msg] = [e for e in relay.events if e["kind"] == 9]
    assert msg["pubkey"] == API and msg["content"].startswith("📥 Dropped anonymously")
    assert ["h", "garden"] in msg["tags"] and sum(t[0] == "imeta" for t in msg["tags"]) == 2
    u, k = hidden(r.text, "u"), hidden(r.text, "k")
    # a forged proof changes nothing
    web.post(f"/drop/{TOKEN}/name", data={"u": u, "k": "0" * 32, "name": "Mallory"})
    assert not any("Mallory" in e["content"] for e in relay.events)
    r = web.post(f"/drop/{TOKEN}/name", data={"u": u, "k": k, "name": "Ana <b>"})
    assert "Thanks, Ana &lt;b&gt;" in r.text
    named = [e for e in relay.events if e["kind"] == 9][-1]
    assert named["content"].startswith("📥 From Ana <b>")
    assert any(e["kind"] == 9005 and ["e", u] in e["tags"] for e in relay.events)
    # sign in, then link it: posted again, signed by the member
    u2, k2 = named["id"], drops.proof(services, named["id"])
    monkeypatch.setattr(drops, "member_name", lambda svc, p: "Ana")
    monkeypatch.setattr(community, "member_partner", lambda svc, cookie: 7 if cookie else None)
    monkeypatch.setattr(
        community, "member_signer", lambda svc, p: ("b" * 64, relay.sign_as("b" * 64))
    )
    r = web.get(f"/drop/{TOKEN}/signin", params={"u": u2, "k": k2}, follow_redirects=False)
    assert r.headers["location"] == "/me/login" and "/drop/" in r.cookies.get(
        "belfry_pass_next", ""
    )
    web.cookies.set("belfry_pass", "session")
    assert "Yes, post it as me" in web.get(f"/drop/{TOKEN}/link", params={"u": u2, "k": k2}).text
    r = web.post(f"/drop/{TOKEN}/link", data={"u": u2, "k": k2})
    assert "Posted as Ana" in r.text
    mine = [e for e in relay.events if e["kind"] == 9][-1]
    assert mine["pubkey"] == "b" * 64 and sum(t[0] == "imeta" for t in mine["tags"]) == 2


def test_sign_in_required(web, relay, monkeypatch):
    link(relay, login=True)
    page = web.get(f"/drop/{TOKEN}").text
    assert "Sign in to upload" in page and 'type="file"' not in page
    r = web.post(
        f"/drop/{TOKEN}", files=[("files", ("a.jpg", b"x", "image/jpeg"))], follow_redirects=False
    )
    assert r.status_code == 303 and not [e for e in relay.events if e["kind"] == 9]
    monkeypatch.setattr(drops, "member_name", lambda svc, p: "Ana")
    monkeypatch.setattr(community, "member_partner", lambda svc, cookie: 7)
    monkeypatch.setattr(
        community, "member_signer", lambda svc, p: ("b" * 64, relay.sign_as("b" * 64))
    )
    assert "Signed in as Ana" in web.get(f"/drop/{TOKEN}").text
    r = web.post(f"/drop/{TOKEN}", files=[("files", ("a.jpg", b"x", "image/jpeg"))])
    assert "Posted as Ana" in r.text
    assert [e["pubkey"] for e in relay.events if e["kind"] == 9] == ["b" * 64]


def test_too_many_files(web, relay):
    link(relay)
    files = [("files", (f"{i}.jpg", b"x", "image/jpeg")) for i in range(drops.MAX_FILES + 1)]
    r = web.post(f"/drop/{TOKEN}", files=files)
    assert f"At most {drops.MAX_FILES} files" in r.text


def test_sign_in_with_an_e_mail_code_from_the_drop_page(web, relay, services, monkeypatch):
    from belfry_onboarding import connect, mypass

    link(relay)
    sent = {}

    def send(svc, f, email):
        if "@" not in email:
            raise connect.ConnectError(404, "No member of belfry.test has this address.")
        f["pending"] = {"email": email, "partner": 7, "pubkey": "b" * 64}
        sent["session"] = f["session"]
        return {"to": email}

    monkeypatch.setattr(connect, "send_code", send)
    monkeypatch.setattr(
        connect, "verify_code", lambda svc, f, code: f["pending"] if code == "123456" else None
    )
    r = web.post(f"/drop/{TOKEN}/signin", data={"email": "nope"})
    assert "No member of belfry.test" in r.text
    r = web.post(f"/drop/{TOKEN}/signin", data={"email": "ana@x.org"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/me/login"
    page = web.get("/me/login").text
    assert 'data-start="code"' in page and "ana@x.org" in page and "Open Source Village" not in page
    fid = re.search(r'data-flow="([^"]+)"', page).group(1)
    csrf = re.search(r'data-csrf="([^"]+)"', page).group(1)
    assert (
        web.post("/me/login/verify", json={"flow": fid, "csrf": "x", "code": "1"}).status_code
        == 410
    )
    r = web.post("/me/login/verify", json={"flow": fid, "csrf": csrf, "code": "123456"})
    assert r.json() == {"next": "in", "to": f"/drop/{TOKEN}"}
    assert r.cookies.get(mypass.SESSION_COOKIE)
