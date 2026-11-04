"""The apex homepage (homepage.py): start page, proxy, nsite; publishing."""

from __future__ import annotations

import hashlib
import io
import tarfile

import httpx
import pytest
from coincurve import PrivateKey

from belfry_onboarding import blossom, homepage, nostr

from .test_blossom import NOW, header, signed_event

SITE = "belfry.test"
APEX = {"Host": SITE, "Accept": "text/html"}


@pytest.fixture
def apex(services):
    services.settings.site_url = f"https://{SITE}"
    services.settings.public_url = f"https://pass.{SITE}"
    return services


def use(client, services, value: str) -> homepage.Homepage:
    services.settings.homepage = value
    hp = homepage.Homepage(services.settings, homepage.parse_config(services.settings))
    client.app.state.homepage = hp
    return hp


# ── configuration ───────────────────────────────────────────────────────────────────────────
def test_parse_config(settings):
    key = PrivateKey()
    pub = key.public_key_xonly.format().hex()
    npub = nostr.hex_to_npub(pub)
    settings.homepage = ""
    assert homepage.parse_config(settings).mode == "start"
    settings.homepage = "https://example.org/"
    c = homepage.parse_config(settings)
    assert (c.mode, c.proxy_url) == ("proxy", "https://example.org")
    settings.homepage = f"nsite:{npub}"
    assert (homepage.parse_config(settings).pubkey, homepage.parse_config(settings).name) == (
        pub,
        "",
    )
    settings.homepage = f"nsite:{npub}/blog"
    assert homepage.parse_config(settings).name == "blog"
    settings.homepage = "nsite"
    settings.belfry_nsec = key.secret.hex()
    assert homepage.parse_config(settings).pubkey == pub
    for bad in ("nsite:npub1nope", f"nsite:{npub}/Not_Valid", "ftp://x", "nsite:" + "ab" * 10):
        settings.homepage = bad
        with pytest.raises(homepage.HomepageError):
            homepage.parse_config(settings)


def test_aggregate_hash_follows_nip_5a():
    paths = {
        "/index.html": "186ea5fd14e88fd1ac49351759e7ab906fa94892002b60bf7f5a428f28ca1c99",
        "/favicon.ico": "fedcba0987654321fedcba0987654321fedcba0987654321fedcba0987654321",
    }
    lines = sorted(f"{s} {p}\n" for p, s in paths.items())
    assert homepage.aggregate_hash(paths) == hashlib.sha256("".join(lines).encode()).hexdigest()
    assert homepage.aggregate_hash(dict(reversed(list(paths.items())))) == homepage.aggregate_hash(
        paths
    )


def test_candidate_paths():
    assert homepage.candidate_paths("/") == ["/index.html"]
    assert homepage.candidate_paths("/blog/") == ["/blog/index.html"]
    assert homepage.candidate_paths("/about") == ["/about", "/about/index.html", "/about.html"]
    assert homepage.candidate_paths("/a/../style.css") == ["/style.css"]


# ── routing on the apex ─────────────────────────────────────────────────────────────────────
def test_without_homepage_the_apex_shows_the_start_page(apex, client):
    r = client.get("/", headers=APEX)
    assert r.status_code == 200 and "How to join" in r.text


def test_proxy_homepage(apex, client, router):
    use(client, apex, "https://site.example")
    router.get("https://site.example/").respond(200, html="<h1>Our commons</h1>")
    router.get("https://site.example/about", params={"x": "1"}).respond(
        200, html="<p>About</p>", headers={"Content-Security-Policy": "default-src 'self'"}
    )
    router.get("https://site.example/old").respond(
        301, headers={"Location": "https://site.example/new"}
    )
    r = client.get("/", headers=APEX)
    assert r.status_code == 200 and "Our commons" in r.text
    assert r.headers["content-security-policy"] == homepage.PROXY_DEFAULT_CSP
    sent = router.calls.last.request
    assert sent.headers["host"] == "site.example" and sent.headers["x-forwarded-host"] == SITE
    r = client.get("/about?x=1", headers=APEX)
    assert "About" in r.text and r.headers["content-security-policy"] == "default-src 'self'"
    r = client.get("/old", headers=APEX, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/new"


def test_with_a_homepage_onboarding_pages_move_to_pass(apex, client, router):
    use(client, apex, "https://site.example")
    r = client.get("/me?x=1", headers=APEX, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"https://pass.{SITE}/me?x=1"
    # discovery stays on the apex
    r = client.get("/.well-known/belfry.json", headers={"Host": SITE})
    assert r.status_code == 200
    # pass.<domain>/ shows the start page instead of forwarding to the apex
    r = client.get("/", headers={"Host": f"pass.{SITE}"}, follow_redirects=False)
    assert r.status_code == 200 and "How to join" in r.text


def test_proxy_unreachable(apex, client, router):
    use(client, apex, "https://site.example")
    router.get("https://site.example/").mock(side_effect=httpx.ConnectError("down"))
    r = client.get("/", headers=APEX)
    assert r.status_code == 502


def nsite(
    client, services, router, files: dict[str, bytes], served: dict[str, bytes] | None = None
):
    hp = use(client, services, "nsite:" + nostr.hex_to_npub("11" * 32))
    paths = {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}
    hp._site = homepage.Site(paths, ["http://blossom:3000"], fetched_at=1e18, event_id="e" * 64)
    for p, sha in paths.items():
        router.get(f"http://blossom:3000/{sha}").respond(
            200, content=(served or files).get(p, files[p])
        )
    return paths


def test_nsite_homepage(apex, client, router):
    paths = nsite(
        client,
        apex,
        router,
        {
            "/index.html": b"<h1>Home</h1>",
            "/about/index.html": b"<p>About</p>",
            "/404.html": b"gone",
            "/app.js": b"1",
        },
    )
    r = client.get("/", headers=APEX)
    assert r.status_code == 200 and r.text == "<h1>Home</h1>"
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["etag"] == f'"{paths["/index.html"]}"'
    assert r.headers["content-security-policy"] == homepage.NSITE_CSP
    assert client.get("/about", headers=APEX).text == "<p>About</p>"
    assert "javascript" in client.get("/app.js", headers=APEX).headers["content-type"]
    r = client.get("/nothing", headers=APEX)
    assert r.status_code == 404 and r.text == "gone"
    r = client.get("/", headers={**APEX, "If-None-Match": f'"{paths["/index.html"]}"'})
    assert r.status_code == 304
    assert client.post("/", headers=APEX).status_code in (404, 405)


def test_nsite_blob_must_match_its_hash(apex, client, router):
    nsite(client, apex, router, {"/index.html": b"real"}, served={"/index.html": b"tampered"})
    assert client.get("/", headers=APEX).status_code == 502


def test_nsite_not_published(apex, client, monkeypatch):
    hp = use(client, apex, "nsite:" + nostr.hex_to_npub("22" * 32))

    def nothing():
        raise homepage.HomepageError("no manifest")

    monkeypatch.setattr(hp, "_resolve", nothing)
    assert client.get("/", headers=APEX).status_code == 503


def test_resolve_picks_the_newest_valid_manifest(settings, monkeypatch):
    key = PrivateKey()
    pub = key.public_key_xonly.format().hex()
    settings.homepage = "nsite:" + pub
    hp = homepage.Homepage(settings, homepage.parse_config(settings))

    def manifest(at: int, sha: str) -> dict:
        from belfry_onboarding.nip46check import sign_event

        return sign_event(
            key.secret,
            {
                "kind": 15128,
                "created_at": at,
                "content": "",
                "tags": [["path", "/index.html", sha], ["server", "https://cdn.example"]],
            },
        )

    old, new = manifest(100, "a" * 64), manifest(200, "b" * 64)
    forged = {**manifest(300, "c" * 64), "sig": "00" * 64}
    monkeypatch.setattr(homepage.RelayPublisher, "query", lambda self, f, s: [old, new, forged])
    settings.relay_url = "ws://relay:8080"
    site = hp._resolve()
    assert site.paths == {"/index.html": "b" * 64}
    assert site.servers == ["http://blossom:3000", "https://cdn.example"]


# ── publishing ──────────────────────────────────────────────────────────────────────────────
def tar_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def test_files_from_tar_skips_dotfiles_and_escapes():
    files = homepage.files_from_tar(
        tar_of(
            {
                "./index.html": b"i",
                "css/a.css": b"c",
                ".env": b"secret",
                "x/.git/config": b"g",
                "../up": b"u",
            }
        )
    )
    assert files == {"/index.html": b"i", "/css/a.css": b"c"}


def test_publish_uploads_and_signs_the_manifest(settings, router, monkeypatch):
    key = PrivateKey()
    settings.belfry_nsec = key.secret.hex()
    settings.relay_url = "ws://relay:8080"
    settings.blossom_public_url = "https://blossom.belfry.test"
    files = {"/index.html": b"<h1>Hi</h1>", "/style.css": b"body{}"}
    have = hashlib.sha256(b"body{}").hexdigest()
    router.head(f"http://blossom-gate/{have}").respond(200)
    router.head(url__regex=r"http://blossom-gate/[0-9a-f]{64}").respond(404)
    upload = router.put("http://blossom-gate/upload").respond(200, json={})
    published = []
    monkeypatch.setattr(
        homepage.RelayPublisher, "publish", lambda self, e, sign: published.append(e)
    )
    result = homepage.publish(settings, files, "Hi")
    assert result["files"] == 2 and result["uploaded"] == 1
    assert upload.call_count == 1
    auth = upload.calls.last.request.headers["authorization"]
    assert auth.startswith("Nostr ")
    event = published[0]
    assert event["kind"] == 15128 and blossom.verify_signature(event)
    assert ["path", "/style.css", have] in event["tags"]
    assert ["server", "https://blossom.belfry.test"] in event["tags"]
    assert [
        "x",
        homepage.aggregate_hash({p: hashlib.sha256(b).hexdigest() for p, b in files.items()}),
        "aggregate",
    ] in event["tags"]


def test_publish_needs_an_index(settings):
    settings.belfry_nsec = PrivateKey().secret.hex()
    settings.relay_url = "ws://relay:8080"
    with pytest.raises(homepage.HomepageError):
        homepage.publish(settings, {"/a.html": b"a"})


def test_blossom_gate_lets_the_belfry_key_upload(client, services, monkeypatch):
    monkeypatch.setattr(blossom.time, "time", lambda: NOW)
    key = PrivateKey()
    services.settings.belfry_nsec = key.secret.hex()
    r = client.get(
        "/blossom/auth",
        headers={"Authorization": header(signed_event(key)), "X-Forwarded-Method": "PUT"},
    )
    assert (
        r.status_code == 200 and r.headers["x-belfry-pubkey"] == key.public_key_xonly.format().hex()
    )
