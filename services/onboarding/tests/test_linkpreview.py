"""Link previews (linkpreview.py, docs/link-previews.md): the page's Open Graph tags and their
fallbacks, the caps, redirects, the addresses never fetched, the picture through the belfry,
the caches, who may ask and how often. The network is a fake: ``resolve`` and ``_get``."""

from __future__ import annotations

import itertools
import time

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import linkpreview as lp
from belfry_onboarding.app import create_app
from belfry_onboarding.events import Limiter

from .test_holder_invites import nip98

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
PAGE = b"""<!doctype html><html><head>
<meta charset="utf-8">
<title>Fallback title</title>
<meta property="og:title" content="Bread &amp; Butter <b>caf\xc3\xa9</b>">
<meta property="og:description" content="  Fresh   bread
 every   morning &lt;3 ">
<meta property="og:site_name" content="The Bakery">
<meta property="og:image" content="/img/loaf.png">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
</head><body>x</body></html>"""


class Net:
    """The internet as the tests see it: names → addresses, addresses → answers."""

    def __init__(self):
        self.names = {"example.org": ["93.184.216.34"], "img.example.org": ["93.184.216.35"]}
        self.pages: dict[str, tuple[int, dict, bytes]] = {}
        self.asked: list[tuple[str, str]] = []

    def resolve(self, host):
        if host not in self.names:
            raise OSError("no such name")
        return self.names[host]

    def get(self, url, host, ip, accept, max_bytes):
        self.asked.append((url, ip))
        status, headers, body = self.pages.get(url, (404, {}, b""))
        if isinstance(body, Exception):
            raise body
        return status, headers, body[: max_bytes + 1]


@pytest.fixture
def net(services, monkeypatch):
    services.settings.nip05_domain = "belfry.test"
    services.settings.console_url = "https://belfry.belfry.test"
    services.settings.pass_page_secret = "s" * 32
    n = Net()
    n.names["belfry.test"] = ["203.0.113.9"]
    monkeypatch.setattr(lp, "resolve", n.resolve)
    monkeypatch.setattr(lp, "_get", n.get)
    lp.forget()
    lp.CALLS._seen.clear()
    lp.IMAGES._seen.clear()
    yield n
    lp.forget()


TICK = itertools.count()
HTML = {"content-type": "text/html; charset=utf-8"}


# ── the page ────────────────────────────────────────────────────────────────────────────────
def test_open_graph_with_entities_tags_and_spaces():
    got = lp.parse(PAGE, "https://example.org/a/b")
    assert got["title"] == "Bread & Butter café"
    assert got["description"] == "Fresh bread every morning <3"
    assert got["site_name"] == "The Bakery"
    assert got["image_url"] == "https://example.org/img/loaf.png"
    assert got["image_width"] == 1200 and got["image_height"] == 630


def test_twitter_then_title_and_meta_description():
    page = b"""<title> Plain &amp; simple </title>
<meta name="twitter:title" content="Tweet title">
<meta name="twitter:image" content="https://img.example.org/t.jpg">
<meta name="description" content="Meta description">"""
    got = lp.parse(page, "https://example.org/")
    assert got["title"] == "Tweet title" and got["description"] == "Meta description"
    assert got["image_url"] == "https://img.example.org/t.jpg" and got["site_name"] is None
    got = lp.parse(b"<title> Plain &amp; simple </title>", "https://example.org/")
    assert got["title"] == "Plain & simple" and got["description"] is None
    assert got["image_url"] is None
    # a picture over http is no picture (the proxy fetches https only)
    got = lp.parse(b'<meta property="og:image" content="http://x.org/a.png">', "https://e.org/")
    assert got["image_url"] is None


def test_caps_cut_at_a_word():
    long = ("word " * 100).encode()
    page = b'<meta property="og:title" content="' + long + b'"><meta name="description" content="'
    page += long * 2 + b'">'
    got = lp.parse(page, "https://example.org/")
    assert len(got["title"]) <= lp.TITLE_MAX and got["title"].endswith("word…")
    assert len(got["description"]) <= lp.DESCRIPTION_MAX


def test_a_latin1_page():
    page = '<meta charset="iso-8859-1"><title>Caf\xe9</title>'.encode("latin-1")
    assert lp.parse(page, "https://e.org/", lp._charset("text/html", page))["title"] == "Café"


# ── addresses never fetched ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/",
        "ftp://example.org/",
        "https://user:pw@example.org/",
        "https://example.org:8443/",
        "https://93.184.216.34/",
        "https://[::1]/",
        "https://localhost/",
        "https://intranet/",
        "https://printer.local/",
        "https://belfry.test/",
        "https://chat.belfry.test/#/c/general",
        "https://anything.belfry.test/x",
        "https://example.org/ with space",
        "javascript:alert(1)",
        "",
        "x" * 3000,
    ],
)
def test_refused_before_anything_is_fetched(services, net, url):
    with pytest.raises(lp.Refused):
        lp.preview(services, url)
    assert not net.asked


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:10.0.0.1",
        "203.0.113.9",  # the belfry's own (its domain resolves there)
    ],
)
def test_names_that_lead_inside_are_never_fetched(services, net, address):
    net.names["sneaky.example.com"] = ["93.184.216.40", address]
    net.pages["https://sneaky.example.com/"] = (200, HTML, PAGE)
    assert lp.preview(services, "https://sneaky.example.com/") is None
    assert not net.asked


def test_the_connection_goes_to_the_address_checked(services, net):
    net.pages["https://example.org/a"] = (200, HTML, PAGE)
    lp.preview(services, "https://example.org/a")
    assert net.asked == [("https://example.org/a", "93.184.216.34")]


# ── fetching ────────────────────────────────────────────────────────────────────────────────
def test_a_preview_and_its_picture(services, net):
    net.pages["https://example.org/a"] = (200, HTML, PAGE)
    got = lp.preview(services, "https://example.org/a")
    assert got["url"] == "https://example.org/a" and got["title"] == "Bread & Butter café"
    assert got["image_width"] == 1200
    digest = lp.image_hash(services, "https://example.org/img/loaf.png")
    assert got["image"] == f"https://belfry.belfry.test/api/v1/link-preview/image/{digest}"
    assert "example.org/img" not in got["image"]


def test_redirects_each_checked_at_most_three(services, net):
    hop = lambda to: (302, {"location": to}, b"")  # noqa: E731
    net.pages["https://example.org/1"] = hop("/2")
    net.pages["https://example.org/2"] = hop("https://img.example.org/3")
    net.pages["https://img.example.org/3"] = (200, HTML, PAGE)
    assert lp.preview(services, "https://example.org/1")["url"] == "https://img.example.org/3"
    # a redirect into the belfry or to a private name is not followed
    net.pages["https://example.org/in"] = hop("https://belfry.test/admin")
    assert lp.preview(services, "https://example.org/in") is None
    net.names["inside.example.net"] = ["10.1.2.3"]
    net.pages["https://example.org/lan"] = hop("https://inside.example.net/")
    assert lp.preview(services, "https://example.org/lan") is None
    assert not any("inside" in u or "belfry.test" in u for u, _ in net.asked)
    net.pages["https://example.org/plain"] = hop("http://example.org/1")
    assert lp.preview(services, "https://example.org/plain") is None
    # four redirects: none
    for i in range(4):
        net.pages[f"https://example.org/r{i}"] = hop(f"/r{i + 1}")
    net.pages["https://example.org/r4"] = (200, HTML, PAGE)
    assert lp.preview(services, "https://example.org/r0") is None


def test_only_html_and_only_its_first_bytes(services, net):
    net.pages["https://example.org/pdf"] = (200, {"content-type": "application/pdf"}, PAGE)
    assert lp.preview(services, "https://example.org/pdf") is None
    big = PAGE.replace(b"</head>", b"</head>" + b"x" * (lp.HTML_MAX * 2))
    net.pages["https://example.org/big"] = (200, HTML, big)
    assert lp.preview(services, "https://example.org/big")["title"] == "Bread & Butter café"
    late = b"<html>" + b" " * lp.HTML_MAX + b"<title>Too late</title>"
    net.pages["https://example.org/late"] = (200, HTML, late)
    assert lp.preview(services, "https://example.org/late") is None


def test_errors_and_slow_sites_give_no_preview(services, net):
    import httpx

    net.pages["https://example.org/slow"] = (200, HTML, httpx.ReadTimeout("slow"))
    assert lp.preview(services, "https://example.org/slow") is None
    net.pages["https://example.org/500"] = (500, HTML, b"")
    assert lp.preview(services, "https://example.org/500") is None
    assert lp.preview(services, "https://nowhere.example.org/") is None  # no such name


def test_kept_a_day_and_nothing_an_hour(services, net, monkeypatch):
    net.pages["https://example.org/a"] = (200, HTML, PAGE)
    assert lp.preview(services, "https://example.org/a")
    assert lp.preview(services, "https://example.org/a")
    net.pages["https://example.org/empty"] = (200, HTML, b"<p>nothing</p>")
    assert lp.preview(services, "https://example.org/empty") is None
    assert lp.preview(services, "https://example.org/empty") is None
    assert len(net.asked) == 2
    clock = [lp.time.monotonic() + lp.NONE_SECONDS + 1]
    monkeypatch.setattr(lp.time, "monotonic", lambda: clock[0])
    lp.preview(services, "https://example.org/a")
    lp.preview(services, "https://example.org/empty")
    assert len(net.asked) == 3  # the empty one asked again, the found one still kept
    clock[0] += lp.FOUND_SECONDS
    lp.preview(services, "https://example.org/a")
    assert len(net.asked) == 4


# ── the picture ─────────────────────────────────────────────────────────────────────────────
def _picture(services, net, body=PNG, ctype="image/png"):
    net.pages["https://example.org/a"] = (200, HTML, PAGE)
    net.pages["https://example.org/img/loaf.png"] = (200, {"content-type": ctype}, body)
    got = lp.preview(services, "https://example.org/a")
    return got["image"].rsplit("/", 1)[1]


def test_the_picture_through_the_belfry_and_kept_on_disk(services, net):
    digest = _picture(services, net)
    assert lp.image(services, digest) == ("image/png", PNG)
    asked = len(net.asked)
    assert lp.image(services, digest) == ("image/png", PNG)
    assert len(net.asked) == asked  # from the disk
    assert (lp.folder(services) / f"{digest}.png").read_bytes() == PNG


def test_no_open_proxy(services, net):
    _picture(services, net)
    other = lp.image_hash(services, "https://example.org/never-named.png")
    assert lp.image(services, other) is None  # never named by a preview
    assert lp.image(services, "0" * 64) is None and lp.image(services, "../etc") is None
    # a file that names an address with a hash that is not its own: never fetched
    (lp.folder(services) / f"{'a' * 64}.url").write_text("https://img.example.org/x.png")
    net.pages["https://img.example.org/x.png"] = (200, {"content-type": "image/png"}, PNG)
    assert lp.image(services, "a" * 64) is None
    assert not any("x.png" in u for u, _ in net.asked)


@pytest.mark.parametrize(
    "body,ctype",
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        (b"not a png at all", "image/png"),
        (PNG + b"\0" * lp.IMAGE_MAX, "image/png"),
        (b"<html>", "text/html"),
    ],
)
def test_pictures_only_of_four_kinds_and_two_megabytes(services, net, body, ctype):
    digest = _picture(services, net, body, ctype)
    assert lp.image(services, digest) is None


# ── the endpoints ───────────────────────────────────────────────────────────────────────────
def _ask(c, key, url):
    import urllib.parse

    path = "/api/v1/link-preview?" + urllib.parse.urlencode({"url": url})
    # a second apart each: the same request twice in one second is a replay
    auth = nip98(key, "https://belfry.belfry.test" + path, created_at=int(time.time()) - next(TICK))
    return c.get(
        path,
        headers={
            "Host": "belfry.belfry.test",
            "Authorization": auth,
            "Origin": "https://chat.belfry.test",
        },
    )


def _member(odoo):
    key = PrivateKey()
    partner = odoo.add("res.partner", name="Ada", email="ada@example.org")
    pk = key.public_key_xonly.format().hex()
    odoo.add("belfry.pass", partner_id=partner, tier="guest", state="active", keycast_pubkey=pk)
    return key


def test_the_api(services, odoo, net, monkeypatch):
    net.pages["https://example.org/a"] = (200, HTML, PAGE)
    net.pages["https://example.org/img/loaf.png"] = (200, {"content-type": "image/png"}, PNG)
    net.pages["https://example.org/empty"] = (200, HTML, b"<p>x</p>")
    key = _member(odoo)
    with TestClient(create_app(services)) as c:
        r = _ask(c, key, "https://example.org/a")
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["title"] == "Bread & Butter café" and got["site_name"] == "The Bakery"
        assert r.headers["access-control-allow-origin"] == "https://chat.belfry.test"
        img = c.get(got["image"].removeprefix("https://belfry.belfry.test"), headers={
            "Host": "belfry.belfry.test"
        })  # fmt: skip
        assert img.status_code == 200 and img.content == PNG
        assert img.headers["content-type"] == "image/png"
        assert img.headers["cross-origin-resource-policy"] == "cross-origin"
        assert "max-age" in img.headers["cache-control"]
        assert c.get("/api/v1/link-preview/image/" + "b" * 64, headers={
            "Host": "belfry.belfry.test"
        }).status_code == 404  # fmt: skip
        r = _ask(c, key, "https://example.org/empty")
        assert r.status_code == 404 and r.json()["error"] == "none"
        r = _ask(c, key, "http://example.org/")
        assert r.status_code == 422 and r.json()["error"] == "invalid"
        r = _ask(c, PrivateKey(), "https://example.org/a")  # nobody's key
        assert r.status_code == 403 and r.json()["error"] == "no_pass"
        r = c.get("/api/v1/link-preview?url=https://example.org/a", headers={
            "Host": "belfry.belfry.test"
        })  # fmt: skip
        assert r.status_code == 401
        monkeypatch.setattr(lp, "CALLS", Limiter(2, 60))
        assert _ask(c, key, "https://example.org/a").status_code == 200
        assert _ask(c, key, "https://example.org/a").status_code == 200
        r = _ask(c, key, "https://example.org/a")
        assert r.status_code == 429 and r.json()["error"] == "too_many"


def test_busy_when_every_slot_is_taken(services, net, monkeypatch):
    import threading

    monkeypatch.setattr(lp, "UPSTREAM", threading.BoundedSemaphore(1))
    monkeypatch.setattr(lp, "TIMEOUT", 0.05)
    lp.UPSTREAM.acquire()
    try:
        with pytest.raises(lp.Busy):
            lp.preview(services, "https://example.org/a")
    finally:
        lp.UPSTREAM.release()


def test_a_website_session_may_ask_too(services, odoo, monkeypatch):
    from belfry_onboarding import sitesessions

    key = "d" * 64
    monkeypatch.setattr(sitesessions, "site_account", lambda svc, k: {"pubkey": "e" * 64})
    assert lp.caller_allowed(services, key)
    monkeypatch.setattr(sitesessions, "site_account", lambda svc, k: None)
    assert not lp.caller_allowed(services, key)
