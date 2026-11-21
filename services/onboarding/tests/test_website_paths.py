"""A website version may change only some paths (website.py, "scopes"): path tags on the
proposal, ``paths`` in the API, path tags on a button's message; a scoped publish keeps the rest
of the live site by hash; every proposal version gets a note saying what it would change."""

from __future__ import annotations

import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest
from coincurve import PrivateKey

from belfry_onboarding import (
    actions,
    apps,
    community,
    followers,
    homepage,
    relay,
    website,
    webthreads,
)

from .test_actions import PUBLISH, Relay, make, message, press
from .test_actions import world as aworld  # noqa: F401 — fixture

ADMIN, AUTHOR = "a" * 64, "b" * 64
COORD = f"30023:{AUTHOR}:proposal-x"


def h(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# ── validation ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path",
    ["/", "/ios/", "/ios/screenshots/", "/android/index.html", "/a_b-c/x.1.png", "/robots.txt"],
)
def test_valid_paths(path):
    assert website.valid_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "ios/",
        "/ios//x/",
        "/./",
        "/../etc/",
        "/a/../b/",
        "/.git/",
        "/a/.env",
        "/android",  # a folder without its / (or a file without an extension): ambiguous
        "/a b/",
        "/a/*",
        "/" + "a" * 200 + "/",
        None,
        3,
    ],
)
def test_invalid_paths(path):
    assert not website.valid_path(path)


def test_parse_paths():
    assert website.parse_paths(None) is None and website.parse_paths([]) is None
    assert website.parse_paths(["/"]) is None  # the whole site
    assert website.parse_paths(["/b/", "/a/x.html", "/b/"]) == ["/a/x.html", "/b/"]
    for bad in (["/ok/", "/.no/"], [f"/p{i}/" for i in range(11)], "/ios/", [None]):
        with pytest.raises(website.PathsError):
            website.parse_paths(bad)
    assert website.path_tags({"tags": [["path", "/a/"], ["t", "x"], ["path"]]}) == ["/a/", None]


# ── the change and how it's said ────────────────────────────────────────────────────────────
LIVE = {
    "/index.html": "1" * 64,
    "/ios/index.html": "2" * 64,
    "/ios/screenshots/a.png": "3" * 64,
    "/ios/screenshots/b.png": "4" * 64,
    "/android/index.html": "5" * 64,
}


def test_diff_and_summary_scoped():
    proposed = {
        "/ios/screenshots/a.png": "3" * 64,  # same
        "/ios/screenshots/b.png": "9" * 64,  # changed
        "/ios/screenshots/c.png": "8" * 64,  # added
        "/index.html": "7" * 64,  # outside the scope: ignored
    }
    c = website.diff(LIVE, proposed, ["/ios/screenshots/"])
    assert (c.added, c.changed, c.removed) == (
        ["/ios/screenshots/c.png"],
        ["/ios/screenshots/b.png"],
        [],
    )
    assert c.kept == {p: s for p, s in LIVE.items() if not p.startswith("/ios/screenshots/")}
    assert website.summary(c, ["/ios/screenshots/"]) == (
        "This changes only /ios/screenshots/: 1 file added, 1 changed, 0 removed. "
        "The rest of the website stays as it is."
    )
    gone = website.diff(LIVE, {"/ios/screenshots/c.png": "8" * 64}, ["/ios/screenshots/"])
    assert gone.removed == ["/ios/screenshots/a.png", "/ios/screenshots/b.png"]
    same = website.diff(LIVE, LIVE, ["/ios/screenshots/", "/android/index.html"])
    assert website.summary(same, ["/android/index.html", "/ios/screenshots/"]) == (
        "This changes nothing: the files in /android/index.html and /ios/screenshots/ "
        "are the same as on the website now."
    )


def test_summary_unscoped_names_areas_but_not_odd_names():
    proposed = {**LIVE, "/index.html": "0" * 64, "/android/new.html": "0" * 64}
    del proposed["/ios/index.html"]
    c = website.diff(LIVE, proposed, None)
    assert website.summary(c, None) == (
        "This replaces the whole website: 1 changed, 1 added, 1 removed, in /, /android/ and /ios/."
    )
    assert website.summary(website.diff(LIVE, LIVE, None), None) == (
        "This changes nothing: it is the same as the website now."
    )
    many = [f"/d{i}/x.html" for i in range(9)] + ["/<script>/x.html"]
    assert website.areas(many) == "/d0/, /d1/, /d2/, /d3/, /d4/, /d5/ and 4 more"
    assert "<" not in website.areas(["/<b>hi</b>/x.html"])


# ── homepage.publish keeps files by hash ────────────────────────────────────────────────────
def test_publish_keeps_files_by_hash_without_uploading_them(settings, router, monkeypatch):
    key = PrivateKey()
    settings.belfry_nsec = key.secret.hex()
    settings.relay_url = "ws://relay:8080"
    kept = {"/index.html": "1" * 64, "/android/index.html": "2" * 64}
    for sha in kept.values():
        router.head(f"http://blossom-gate/{sha}").respond(200)
    router.head(url__regex=r"http://blossom-gate/[0-9a-f]{64}").respond(404)
    upload = router.put("http://blossom-gate/upload").respond(200, json={})
    published = []
    monkeypatch.setattr(
        homepage.RelayPublisher, "publish", lambda self, e, sign: published.append(e)
    )
    out = homepage.publish(settings, {"/ios/a.png": b"png"}, keep=kept)
    assert out["files"] == 3 and out["uploaded"] == 1 and upload.call_count == 1
    paths = homepage.manifest_paths(published[0])
    assert paths == {**kept, "/ios/a.png": h(b"png")}
    # a kept file the server lost: nothing is published
    published.clear()
    with pytest.raises(homepage.HomepageError, match="missing"):
        homepage.publish(settings, {"/ios/a.png": b"png"}, keep={**kept, "/x.css": "3" * 64})
    assert published == []
    with pytest.raises(homepage.HomepageError, match="index"):
        homepage.publish(settings, {"/ios/a.png": b"png"}, keep={"/a.css": "1" * 64})


# ── a scoped publish ────────────────────────────────────────────────────────────────────────
class FakeSite:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.fetched: list[str] = []
        self._client = None

    async def site(self):
        return SimpleNamespace(
            paths={p: h(b) for p, b in self.files.items()}, servers=["http://blossom:3000"]
        )

    async def blob(self, sha, servers):
        path = next(p for p, b in self.files.items() if h(b) == sha)
        self.fetched.append(path)
        return self.files[path], ""


@pytest.fixture
def pub(monkeypatch):
    svc = SimpleNamespace(
        settings=SimpleNamespace(homepage="nsite", belfry_nsec="1" * 64, nip05_domain="b.test"),
        odoo=SimpleNamespace(),
    )
    webthreads._pending_mem.clear()
    state: dict = {"live": dict(LIVE), "logged": [], "published": {}}
    proposal = FakeSite(
        {
            "/ios/screenshots/a.png": b"new a",
            "/ios/screenshots/c.png": b"c",
            "/index.html": b"someone's stale home page",  # outside the scope
        }
    )
    state["proposal"] = proposal
    monkeypatch.setattr(website, "site_of", lambda s, pk, name: proposal)
    monkeypatch.setattr(website, "live_paths", lambda s: _value(state["live"]))
    monkeypatch.setattr(community, "names_for", lambda s, keys: {AUTHOR: "Bo"})
    monkeypatch.setattr(
        website, "changelog", lambda s, text, tags=None: state["logged"].append(text)
    )

    def publish(settings, files, title, by, note, keep=None):
        state["published"] = {"files": files, "keep": keep, "note": note}
        return {"manifest": "m1", "uploaded": len(files)}

    monkeypatch.setattr(homepage, "publish", publish)
    return svc, state


def test_a_scoped_publish_merges_into_the_live_site(pub):
    svc, state = pub
    out = asyncio.run(website.publish_from(svc, AUTHOR, "website", "Ada", ["/ios/screenshots/"]))
    got = state["published"]
    # only the scope's files are downloaded; the proposal's /index.html is ignored
    assert sorted(state["proposal"].fetched) == ["/ios/screenshots/a.png", "/ios/screenshots/c.png"]
    assert set(got["files"]) == {"/ios/screenshots/a.png", "/ios/screenshots/c.png"}
    # outside the scope: the live files, by hash; b.png (live, absent from the proposal) is gone
    assert got["keep"] == {
        "/index.html": "1" * 64,
        "/ios/index.html": "2" * 64,
        "/android/index.html": "5" * 64,
    }
    assert got["note"] == "Website updated by Ada, from Bo's version, only /ios/screenshots/."
    assert out["paths"] == ["/ios/screenshots/"]
    assert (out["added"], out["changed"], out["removed"]) == (1, 1, 1)
    code = website.short_code(svc, AUTHOR, "website")
    assert state["logged"][-1] == (
        "**Website updated** by Ada\n---\n"
        "2 files: 1 added, 1 changed, 1 removed, only /ios/screenshots/. Version by Bo.\n"
        f"Live: https://b.test · Preview: https://preview.b.test/v/{code}"
    )


def test_a_scoped_publish_needs_a_live_site_and_files_in_its_scope(pub):
    svc, state = pub
    state["live"] = None
    with pytest.raises(homepage.HomepageError, match="publish a whole site first"):
        asyncio.run(website.publish_from(svc, AUTHOR, "website", "Ada", ["/ios/screenshots/"]))
    assert state["proposal"].fetched == [] and state["published"] == {}
    state["live"] = dict(LIVE)
    with pytest.raises(homepage.HomepageError, match="no files in /docs/"):
        asyncio.run(website.publish_from(svc, AUTHOR, "website", "Ada", ["/docs/"]))
    # the website must keep an /index.html
    state["live"] = {"/ios/screenshots/a.png": "3" * 64}
    with pytest.raises(homepage.HomepageError, match="no /index.html"):
        asyncio.run(website.publish_from(svc, AUTHOR, "website", "Ada", ["/ios/screenshots/"]))
    assert state["published"] == {}


def test_a_whole_site_publish_is_unchanged(pub):
    svc, state = pub
    out = asyncio.run(website.publish_from(svc, AUTHOR, "website", "Ada"))
    got = state["published"]
    assert got["keep"] is None and set(got["files"]) == set(state["proposal"].files)
    assert got["note"] == "Website updated by Ada, from Bo's version."
    assert out["paths"] == [] and out["files"] == 3
    assert "\n---\n3 files: 1 added, 2 changed, 3 removed. Version by Bo.\n" in state["logged"][-1]


# ── proposals: the note before approval, and publishing ─────────────────────────────────────
class Groups:
    def __init__(self):
        self.labels: list[dict] = []
        self.sent: list[dict] = []

    def query(self, filters, sign):
        f = filters[0]
        if f["kinds"] == [9]:
            return [{"kind": 9, "tags": [["h", "website"], ["proposal", "prop-x"], ["a", COORD]]}]
        if f["kinds"] == [1985]:
            return self.labels
        return []

    def publish(self, e, sign):
        self.sent.append(e)


class Main:
    versions: list[dict] = []

    def __init__(self, url):
        pass

    def query(self, filters, sign):
        return self.versions


def version(vid="v1", paths=(), at=None):
    return {
        "id": vid,
        "pubkey": AUTHOR,
        "created_at": int(time.time()) - 5 if at is None else at,
        "tags": [
            ["d", "proposal-x"],
            ["h", "prop-x"],
            ["t", "website"],
            ["website", "nsite:npub1" + "q" * 58],
            *[["path", p] for p in paths],
        ],
    }


def passed(at=None):
    return {
        "id": "pass1",
        "pubkey": ADMIN,
        "created_at": int(time.time()) if at is None else at,
        "tags": [
            ["h", "prop-x"],
            ["L", "belfry/proposal-status"],
            ["l", "passed", "belfry/proposal-status"],
            ["a", COORD],
        ],
    }


@pytest.fixture
def props(monkeypatch):
    g = Groups()
    state = {"proposed": {**LIVE, "/ios/screenshots/c.png": "8" * 64}, "live": dict(LIVE)}
    svc = SimpleNamespace(
        settings=SimpleNamespace(relay_url="wss://relay.test", nip05_domain="belfry.test"),
        odoo=SimpleNamespace(),
    )
    monkeypatch.setattr(
        followers, "_relay", lambda s: (g, lambda t: {**t, "id": "x", "pubkey": "f" * 64})
    )
    monkeypatch.setattr(relay, "RelayPublisher", Main)
    monkeypatch.setattr(community, "belfry_signer", lambda s: lambda t: t)
    monkeypatch.setattr(community, "names_for", lambda s, keys: {ADMIN: "Ada"})
    monkeypatch.setattr(apps, "admin_pubkeys", lambda s: {ADMIN})
    monkeypatch.setattr(
        website,
        "parse_ref",
        lambda text, dom="", svc=None: (AUTHOR, "") if "nsite:" in text else None,
    )

    async def manifest_of(s, pk, name):
        if state["proposed"] is None:
            raise homepage.HomepageError("no nsite manifest found")
        return state["proposed"]

    monkeypatch.setattr(website, "manifest_of", manifest_of)
    monkeypatch.setattr(website, "live_paths", lambda s: _value(state["live"]))
    webthreads._pending_mem.clear()
    return svc, g, state


def notes(g):
    """The details of each note (after its first line and the ``---``), without the preview."""
    out = []
    for e in g.sent:
        if ["h", "prop-x"] not in e["tags"]:
            continue
        text = e["content"]
        if "\n---\n" in text and not text.startswith("✅") and not text.startswith("⚠️ Couldn't"):
            head, rest = text.split("\n---\n", 1)
            body = [x for x in rest.split("\n") if not x.startswith("Preview: ")]
            text = "\n".join(body) or head
        out.append(text)
    return out


def test_each_version_says_what_it_would_change_once(props):
    svc, g, state = props
    Main.versions = [version(paths=["/ios/screenshots/"])]
    assert asyncio.run(website.announce(svc)) == 1
    assert notes(g) == [
        "This changes only /ios/screenshots/: 1 file added, 0 changed, 0 removed. "
        "The rest of the website stays as it is."
    ]
    assert ["client", "belfry-website"] in g.sent[-1]["tags"]
    assert asyncio.run(website.announce(svc)) == 0 and len(g.sent) == 1  # once per version
    # a new version (whole site now) gets its own note
    state["proposed"] = {**LIVE, "/index.html": "0" * 64}
    Main.versions = [version(paths=["/ios/screenshots/"]), version("v2", at=int(time.time()))]
    assert asyncio.run(website.announce(svc)) == 1
    assert notes(g)[-1] == ("This replaces the whole website: 1 changed, 0 added, 0 removed, in /.")
    # nothing would change
    state["proposed"] = dict(LIVE)
    Main.versions = [version("v3", paths=["/ios/"])]
    asyncio.run(website.announce(svc))
    assert notes(g)[-1] == (
        "This changes nothing: the files in /ios/ are the same as on the website now."
    )


def test_notes_for_odd_versions(props):
    svc, g, state = props
    Main.versions = [version("bad", paths=["/ios/../secret/"])]
    asyncio.run(website.announce(svc))
    assert notes(g)[-1].startswith("Not published: its path tags aren't")
    assert g.sent[-1]["content"].startswith("⚠️ This proposal can't be published\n---\n")
    assert "secret" not in notes(g)[-1]
    state["live"] = None
    Main.versions = [version("first")]
    asyncio.run(website.announce(svc))
    assert notes(g)[-1] == "This would be the first website: 6 files."
    Main.versions = [version("scoped-nolive", paths=["/ios/"])]
    asyncio.run(website.announce(svc))
    assert notes(g)[-1] == (
        "This can't be published yet: it changes only /ios/, and there is no website yet. "
        "Publish a whole site first."
    )
    # a preview not readable yet: wait (the agent may still be publishing it), then say so
    state["proposed"] = None
    Main.versions = [version("unread")]
    n = len(g.sent)
    assert asyncio.run(website.announce(svc)) == 0 and len(g.sent) == n
    Main.versions = [version("unread", at=int(time.time()) - website.ANNOUNCE_GRACE - 5)]
    asyncio.run(website.announce(svc))
    assert notes(g)[-1] == "⚠️ " + website.UNREADABLE
    # versions from before these notes are never described
    Main.versions = [version("ancient", at=1000)]
    assert asyncio.run(website.announce(svc)) == 0


def test_a_passed_scoped_proposal_publishes_its_scope(props, monkeypatch):
    svc, g, state = props
    Main.versions = [version(paths=["/ios/screenshots/"], at=int(time.time()) - 10)]
    g.labels = [passed()]
    calls = []

    async def publish(s, pk, name, admin, scope=None, **k):
        calls.append(scope)
        return {"files": 1}

    monkeypatch.setattr(website, "publish_from", publish)
    assert asyncio.run(website.watch(svc)) == 1
    assert calls == [["/ios/screenshots/"]]
    assert notes(g)[-1] == (
        "✅ Live now at https://belfry.test: this proposal passed, so /ios/screenshots/ on the "
        "website is its version."
    )


def test_a_passed_proposal_with_bad_paths_is_not_published(props, monkeypatch):
    svc, g, state = props
    Main.versions = [version(paths=["/ios", "/a/"], at=int(time.time()) - 10)]
    g.labels = [passed()]
    calls = []

    async def publish(*a, **k):
        calls.append(a)

    monkeypatch.setattr(website, "publish_from", publish)
    assert asyncio.run(website.watch(svc)) == 0 and calls == []
    assert notes(g)[-1].startswith("⚠️ Couldn't publish this version (its path tags aren't valid")


# ── the button and the API ──────────────────────────────────────────────────────────────────
def test_the_button_passes_the_messages_path_tags(aworld):  # noqa: F811
    svc, published = aworld
    site = ["a", f"35128:{'c' * 64}:website"]
    r = Relay([message(tags=[site, ["path", "/ios/screenshots/"], PUBLISH])])
    out = actions_run(make(svc, r), press())
    assert out.text == "Published by Ada: only /ios/screenshots/." and out.closed
    assert published[-1][-1] == ["/ios/screenshots/"]
    r = Relay([message(tags=[site, ["path", "/../x/"], PUBLISH])])
    out = actions_run(make(svc, r), press(pid="p2"))
    assert out.code == "failed" and "path tags aren't valid" in out.text
    assert len(published) == 1


def actions_run(answerer, e):
    return asyncio.run(answerer.handle(e))


def test_api_takes_paths(rsvc, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from belfry_onboarding import console
    from belfry_onboarding.app import create_app
    from belfry_onboarding.nostr import hex_to_npub

    from .test_holder_invites import BASE, nip98

    rsvc.settings.public_url = BASE
    rsvc.settings.nip05_domain = "belfry.test"
    key, _ = admin_with_key(rsvc)
    monkeypatch.setattr(console, "admin_emails", lambda svc: {"admin@belfry.test"})
    calls = []

    async def publish_from(s, pk, name, admin, paths=None, **k):
        calls.append(paths)
        return {"files": 1, "paths": paths or []}

    monkeypatch.setattr(website, "publish_from", publish_from)
    npub = hex_to_npub(PrivateKey().public_key_xonly.format().hex())
    url = BASE + "/api/website/publish"
    with TestClient(create_app(rsvc)) as c:

        def post(body: bytes):
            time.sleep(1.1)  # NIP-98: one request a second per key
            return c.post(
                "/api/website/publish",
                content=body,
                headers={
                    "Authorization": nip98(key, url, "POST", body),
                    "Content-Type": "application/json",
                },
            )

        r = post(f'{{"ref": "nsite:{npub}", "paths": ["/ios/screenshots/"]}}'.encode())
        assert r.status_code == 200 and r.json()["paths"] == ["/ios/screenshots/"]
        assert calls == [["/ios/screenshots/"]]
        r = post(f'{{"ref": "nsite:{npub}", "paths": ["/ios"]}}'.encode())
        assert r.status_code == 422 and r.json()["error"] == "invalid_paths"
        r = post(f'{{"ref": "nsite:{npub}"}}'.encode())
        assert r.status_code == 200 and calls[-1] is None and len(calls) == 2


from .test_guests import admin_with_key, rsvc  # noqa: E402, F401 — fixtures


async def _value(v):
    return v


_ = actions  # the module under test for the button
