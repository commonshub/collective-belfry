"""belfry.<domain> (console.py): overview, status, members, website; the placeholder homepage."""

from __future__ import annotations

import hashlib
import json

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import community, console, homepage, mypass
from belfry_onboarding.app import create_app
from belfry_onboarding.nip46check import sign_event

from .fakes import flash_of

CONSOLE = {"Host": "belfry.belfry.test"}


@pytest.fixture
def belfry(services, authentik):
    s = services.settings
    s.pass_page_oidc_client_secret = "pass-secret"
    s.pass_page_secret = "p" * 32
    s.belfry_nsec = PrivateKey().secret.hex()
    s.site_url = "https://belfry.test"
    s.public_url = "https://pass.belfry.test"
    s.console_url = "https://belfry.belfry.test"
    s.community_name = "Example Commons"
    authentik.groups["belfry-stewards"] = {"pk": "0000-stewards", "members": set()}
    community._cache["data"] = None
    console._admins["at"] = 0.0
    console._status["data"] = None
    return services


@pytest.fixture
def web(belfry, monkeypatch):
    async def fake_checks(settings):
        return [
            {"name": "Signer (Keycast)", "up": True, "ms": 3, "detail": "HTTP 200"},
            {"name": "Community relay", "up": False, "ms": 4000, "detail": "ConnectTimeout"},
        ]

    monkeypatch.setattr(console, "check_all", fake_checks)
    with TestClient(create_app(belfry), base_url="https://belfry.belfry.test") as c:
        yield c


def member(odoo, name="Alice", email=None) -> int:
    partner = odoo.add(
        "res.partner",
        name=name,
        email=email or f"{name.lower()}@example.org",
        belfry_member_number=f"M-{name}",
    )
    odoo.add(
        "belfry.pass", partner_id=partner, tier="week", state="active", date_start="2026-11-01"
    )
    return partner


def make_admin(authentik, email):
    from belfry_onboarding import steward

    del steward  # the group is enough for console.admin_emails
    pk = len(authentik.users) + 100
    authentik.users.append(
        {
            "pk": pk,
            "username": email,
            "email": email,
            "name": email,
            "attributes": {},
            "path": "users",
            "groups": [],
        }
    )
    authentik.groups["belfry-stewards"]["members"].add(pk)
    console._admins["at"] = 0.0


def sign_in(web, services, partner) -> str:
    csrf = "c" * 16
    cookie = mypass._sign(services, {"kind": "console", "partner": partner, "csrf": csrf}, 3600)
    web.cookies.set(console.COOKIE, cookie)
    return csrf


# ── host mapping and the overview ───────────────────────────────────────────────────────────
def test_overview_summarises_status_members_and_website(web, belfry, odoo):
    member(odoo, "Alice")
    member(odoo, "Bob")
    r = web.get("/", headers=CONSOLE)
    assert r.status_code == 200
    assert "Example Commons" in r.text and 'href="/login"' in r.text
    assert "1 of 2 services answering" in r.text  # the fake checks: one down
    assert "Alice" not in r.text and "Your belfry" not in r.text  # names only for members


def test_home_for_a_member_and_for_an_admin(web, belfry, odoo, authentik):
    alice = member(odoo, "Alice")
    member(odoo, "Bob")
    sign_in(web, belfry, alice)
    r = web.get("/", headers=CONSOLE)
    assert "Welcome, Alice" in r.text and "Your pass" in r.text and "Bob" in r.text
    assert "Your belfry" not in r.text and 'href="/status">Manage' not in r.text
    make_admin(authentik, "alice@example.org")
    r = web.get("/", headers=CONSOLE)
    assert "Your belfry" in r.text and 'href="/status">Manage' in r.text
    assert "1 of 2 services working" in r.text and "Placeholder" in r.text
    assert "Set up e-mail" in r.text  # no mail provider in the tests
    assert "Invite your first members" not in r.text  # two members already
    sub = web.get("/members", headers=CONSOLE).text
    assert 'class="manage"' in sub and 'href="/setup"' in sub


def test_console_paths_move_to_the_console_host(web):
    r = web.get("/console/status", headers={"Host": "pass.belfry.test"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://belfry.belfry.test/status"


def test_status_page_and_json(web, belfry, tmp_path):
    path = tmp_path / "changelog.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "at": "2026-11-04T10:00:00+00:00",
                        "type": "code",
                        "summary": "Add the console",
                        "by": ["Xavier"],
                        "ref": "abcdef1234567890",
                    }
                ]
            }
        )
    )
    belfry.settings.changelog_path = str(path)
    r = web.get("/status", headers=CONSOLE)
    assert "1 of 2 services answer" in r.text and "ConnectTimeout" in r.text
    assert "abcdef123456" in r.text and "Add the console" in r.text
    j = web.get("/status.json", headers=CONSOLE).json()
    assert j["up"] == 1 and j["total"] == 2 and j["version"] == "abcdef123456"


def test_checks_list_the_configured_services(belfry):
    s = belfry.settings
    s.relay_url = "ws://relay:8080"
    s.git_public_url = "https://git.belfry.test"
    names = [c[0] for c in console.checks(s)]
    assert "Community relay" in names and "Git server" in names and "Files (Blossom)" in names
    relay = next(c for c in console.checks(s) if c[0] == "Community relay")
    assert relay[1] == "http://relay:8080" and relay[2]["Accept"] == "application/nostr+json"


# ── sign-in handoff ─────────────────────────────────────────────────────────────────────────
def test_sign_in_goes_through_the_pass_page_and_back(web, belfry, odoo):
    partner = member(odoo)
    r = web.get("/login?to=/members", headers=CONSOLE, follow_redirects=False)
    assert r.headers["location"] == "https://pass.belfry.test/me/console?to=%2Fmembers"
    # not signed in on the pass page: sign in there first, then come back
    r = web.get(
        "/me/console?to=/members", headers={"Host": "pass.belfry.test"}, follow_redirects=False
    )
    assert r.headers["location"] == "/me/login" and "belfry_pass_next" in r.headers["set-cookie"]
    web.cookies.clear()
    web.cookies.set(
        mypass.SESSION_COOKIE, mypass._sign(belfry, {"kind": "session", "partner": partner}, 600)
    )
    r = web.get(
        "/me/console?to=/members", headers={"Host": "pass.belfry.test"}, follow_redirects=False
    )
    target = r.headers["location"]
    assert target.startswith("https://belfry.belfry.test/session?t=")
    web.cookies.clear()
    r = web.get(
        target.removeprefix("https://belfry.belfry.test"), headers=CONSOLE, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/members"
    assert console.COOKIE in r.headers["set-cookie"]
    # a forged ticket does nothing
    r = web.get("/session?t=nope", headers=CONSOLE, follow_redirects=False)
    assert r.headers["location"] == "/login"


# ── members ─────────────────────────────────────────────────────────────────────────────────
def test_members_list_is_for_members_and_emails_for_admins(web, belfry, odoo, authentik):
    alice = member(odoo, "Alice")
    member(odoo, "Bob")
    r = web.get("/members", headers=CONSOLE)
    assert "2 members" in r.text and "Bob" not in r.text
    sign_in(web, belfry, alice)
    r = web.get("/members", headers=CONSOLE)
    assert "Bob" in r.text and "bob@example.org" not in r.text and "Add a member" not in r.text
    make_admin(authentik, "alice@example.org")
    r = web.get("/members", headers=CONSOLE)
    assert "bob@example.org" in r.text and "Add a member" in r.text


def test_admin_adds_edits_and_removes_members(web, belfry, odoo, authentik):
    alice = member(odoo, "Alice")
    make_admin(authentik, "alice@example.org")
    csrf = sign_in(web, belfry, alice)
    r = web.post(
        "/members/add",
        headers=CONSOLE,
        data={"csrf": csrf, "email": "Carol@Example.org", "name": "Carol"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "carol@example.org is a member" in flash_of(r)
    carol_pass = next(
        p
        for p in odoo.tables["belfry.pass"].values()
        if p.get("partner_id")
        and odoo.tables["res.partner"][console.core.m2o_id(p["partner_id"])].get("email")
        == "carol@example.org"
    )
    assert carol_pass["state"] == "active"
    carol = console.core.m2o_id(carol_pass["partner_id"])
    # make Carol an admin and rename her
    web.post(
        f"/members/{carol}/edit",
        headers=CONSOLE,
        data={"csrf": csrf, "name": "Carol B", "admin": "1"},
    )
    assert odoo.tables["res.partner"][carol]["name"] == "Carol B"
    assert "carol@example.org" in console.admin_emails(belfry)
    # remove her: pass revoked, no longer admin
    web.post(f"/members/{carol}/remove", headers=CONSOLE, data={"csrf": csrf})
    assert carol_pass["state"] == "revoked"
    assert "carol@example.org" not in console.admin_emails(belfry)


def test_members_forms_need_an_admin_and_the_csrf(web, belfry, odoo, authentik):
    alice = member(odoo, "Alice")
    csrf = sign_in(web, belfry, alice)
    r = web.post(
        "/members/add",
        headers=CONSOLE,
        data={"csrf": csrf, "email": "x@example.org"},
        follow_redirects=False,
    )
    assert "Only admins" in flash_of(r)
    make_admin(authentik, "alice@example.org")
    r = web.post(
        "/members/add",
        headers=CONSOLE,
        data={"csrf": "wrong", "email": "x@example.org"},
        follow_redirects=False,
    )
    assert "Only admins" in flash_of(r)
    r = web.post(
        "/members/add",
        headers=CONSOLE,
        data={"csrf": csrf, "email": "not-an-address"},
        follow_redirects=False,
    )
    assert "e-mail" in flash_of(r)
    r = web.post(
        f"/members/{alice}/remove", headers=CONSOLE, data={"csrf": csrf}, follow_redirects=False
    )
    assert "yourself" in flash_of(r)


# ── website ─────────────────────────────────────────────────────────────────────────────────
def test_placeholder_until_the_website_is_published(web, belfry):
    belfry.settings.homepage = "nsite"
    web.app.state.homepage = homepage.Homepage(
        belfry.settings, homepage.parse_config(belfry.settings)
    )
    web.app.state.homepage._resolve = lambda: (_ for _ in ()).throw(homepage.HomepageError("none"))
    r = web.get("/", headers={"Host": "belfry.test", "Accept": "text/html"})
    assert r.status_code == 200 and "The website of Example Commons will be here" in r.text
    assert "https://pass.belfry.test/skills/belfry-homepage.md" in r.text
    assert 'href="https://belfry.belfry.test"' in r.text and "belfry.belfry.test/website" in r.text


def test_admin_approves_a_members_website(web, belfry, odoo, authentik, relay):
    alice = member(odoo, "Alice")
    make_admin(authentik, "alice@example.org")
    csrf = sign_in(web, belfry, alice)
    ben = PrivateKey()
    ben_pk = ben.public_key_xonly.format().hex()
    belfry_pk = homepage.belfry_pubkey(belfry.settings)
    index = hashlib.sha256(b"<h1>Ben's site</h1>").hexdigest()
    site = sign_event(
        ben.secret,
        {
            "kind": 35128,
            "created_at": 10,
            "content": "",
            "tags": [["d", "website"], ["path", "/index.html", index], ["title", "Commons"]],
        },
    )
    prop = sign_event(
        ben.secret,
        {
            "kind": 1,
            "created_at": 11,
            "content": "New website\n\n#proposal",
            "tags": [
                ["t", "proposal"],
                ["t", "website"],
                ["p", belfry_pk],
                ["subject", "New website"],
                ["a", f"35128:{ben_pk}:website"],
            ],
        },
    )
    relay.events += [site, prop]
    r = web.get("/website", headers=CONSOLE)
    assert "New website" in r.text and "Approve and publish" in r.text
    r = web.post(
        f"/website/{prop['id']}/approve",
        headers=CONSOLE,
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert "Published" in flash_of(r), flash_of(r)
    root = next(e for e in relay.events if e["kind"] == 15128)
    assert root["pubkey"] == belfry_pk and ["path", "/index.html", index] in root["tags"]
    assert ["a", f"35128:{ben_pk}:website"] in root["tags"] and [
        "A",
        f"35128:{ben_pk}:website",
    ] in root["tags"]
    snap = next(e for e in relay.events if e["kind"] == 5128)
    assert "approved by Alice" in snap["content"]
    # the changelog shows the website change
    assert "approved by Alice" in web.get("/changelog", headers={"Host": "pass.belfry.test"}).text
    # members can't approve
    console._admins["at"] = 0.0
    authentik.groups["belfry-stewards"]["members"].clear()
    r = web.post(
        f"/website/{prop['id']}/approve",
        headers=CONSOLE,
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert "Only admins" in flash_of(r)


def test_discovery_names_the_console_and_the_belfry_key(web, belfry):
    doc = web.get("/.well-known/belfry.json", headers={"Host": "belfry.test"}).json()
    assert doc["console"] == "https://belfry.belfry.test"
    assert doc["nostr"]["pubkey"] == homepage.belfry_pubkey(belfry.settings)
    assert doc["skills"]["website"].endswith("/skills/belfry-homepage.md")


def test_status_shows_the_version_and_admins_can_update(web, belfry, odoo, authentik, tmp_path):
    import json as _json

    belfry.settings.setup_dir = str(tmp_path)
    (tmp_path / "version.json").write_text(
        _json.dumps(
            {
                "branch": "belfry-sh",
                "commit": "abc1234def",
                "short": "abc1234",
                "date": "2026-11-08T10:00:00+00:00",
                "subject": "Shared inboxes",
                "latest": "fff9999",
                "behind": 2,
                "newer": ["Lettermint in the setup", "Version and updates"],
                "checked_at": "2026-11-08T12:00:00Z",
            }
        )
    )
    page = web.get("/status", headers=CONSOLE).text
    assert "abc1234" in page and "2 updates available" in page and "Lettermint in the setup" in page
    assert "Update now" not in page  # signed out
    from belfry_onboarding import discovery

    assert discovery.document(belfry.settings)["code"]["short"] == "abc1234"
    plain = member(odoo, "Bob", "bob@example.org")
    csrf = sign_in(web, belfry, plain)
    assert "Update now" not in web.get("/status", headers=CONSOLE).text
    r = web.post("/update", data={"csrf": csrf}, headers=CONSOLE, follow_redirects=False)
    assert r.status_code == 303 and not (tmp_path / "update.request").exists()
    admin = member(odoo, "Ada", "ada@example.org")
    make_admin(authentik, "ada@example.org")
    csrf = sign_in(web, belfry, admin)
    assert "Update now" in web.get("/status", headers=CONSOLE).text
    r = web.post("/update", data={"csrf": csrf}, headers=CONSOLE, follow_redirects=False)
    assert r.status_code == 303 and (tmp_path / "update.request").exists()
    page = web.get("/status", headers=CONSOLE).text
    assert "Updating" in page and "Update now" not in page
    (tmp_path / "update.request").unlink()
    (tmp_path / "update.json").write_text(
        _json.dumps(
            {
                "state": "failed",
                "from": "abc1234",
                "to": "abc1234",
                "message": "no space left",
                "at": "2026-11-08T13:00:00Z",
            }
        )
    )
    assert "The last update failed" in web.get("/status", headers=CONSOLE).text


def test_the_monitor_lives_on_the_console(web, belfry, odoo):
    r = web.get("/monitor", headers={"Host": "pass.belfry.test"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://belfry.belfry.test/monitor"
    page = web.get("/monitor", headers=CONSOLE)
    assert page.status_code == 200 and "sign in" in page.text and "EventSource" not in page.text
    assert 'href="/monitor"' in page.text  # in the console's tabs
    assert web.get("/monitor/stream", headers=CONSOLE).status_code == 401
    assert web.get("/monitor/names?p=" + "a" * 64, headers=CONSOLE).status_code == 401


def test_no_odoo_check_without_odoo(belfry):
    s = belfry.settings
    s.register_mode = "belfry"
    assert not any("Odoo" in name for name, _, _ in console.checks(s))
    s.register_mode = "odoo"
    assert any("Odoo" in name for name, _, _ in console.checks(s))


def test_apps_home_lists_the_installed_apps(web, belfry, tmp_path):
    apps = tmp_path / "apps"
    (apps / "_routes").mkdir(parents=True)
    for name in ("chat", "notes", "gone"):
        (apps / name).mkdir()
        (apps / name / "index.html").write_text("<p>app</p>")
    (apps / "chat" / "manifest.webmanifest").write_text(
        json.dumps({"name": "Open Source Village", "icons": [{"src": "icons/icon-192.png"}]})
    )
    (apps / "notes" / "manifest.webmanifest").write_text(
        json.dumps({"short_name": "<b>Notes</b>", "icons": [{"src": "https://x.org/i.png"}]})
    )
    (apps / "_routes" / "chat.caddy").write_text("")
    (apps / "_routes" / "notes.caddy").write_text("")
    belfry.settings.apps_dir = str(apps)
    belfry.settings.nip05_domain = "belfry.test"
    r = web.get("/apps-home", headers={"Host": "apps.belfry.test"})
    assert r.status_code == 200
    # served from the community's domain: one origin for all its apps
    assert 'href="https://belfry.test/chat/"' in r.text
    assert 'src="https://belfry.test/chat/icons/icon-192.png"' in r.text
    assert "Chat" in r.text and "&lt;b&gt;Notes" in r.text and "https://x.org" not in r.text
    assert "/gone/" not in r.text and "Open Source Village" not in r.text


def test_apps_home_shows_what_can_be_added_to_admins_only(web, belfry, odoo, authentik, tmp_path):
    """apps.<domain>/: your apps for everyone; the apps that can be added in a hidden part that
    the page opens for admins (the console's /apps/role); adding asks, then queues EXTRA_APPS."""
    s = belfry.settings
    s.osv_apps, s.extra_apps, s.nip05_domain = False, "vote", "belfry.test"
    s.apps_public_url, s.setup_dir = "https://apps.belfry.test", str(tmp_path)
    page = web.get("/apps-home", headers={"Host": "apps.belfry.test"}).text
    assert 'href="https://vote.belfry.test/"' in page  # added: under your apps
    more = page[page.index('<section id="more" hidden>') :]
    assert "/apps/add?app=calendar" in more and "/apps/add?app=vote" not in more
    assert "https://belfry.belfry.test/apps/role" in more

    r = web.get("/apps/role", headers=CONSOLE)
    assert r.json() == {"admin": False}
    assert r.headers["access-control-allow-origin"] == "https://apps.belfry.test"
    assert r.headers["access-control-allow-credentials"] == "true"
    alice = member(odoo, "Alice")
    csrf = sign_in(web, belfry, alice)
    assert web.get("/apps/role", headers=CONSOLE).json() == {"admin": False}
    assert "Only admins" in web.get("/apps/add?app=calendar", headers=CONSOLE).text
    r = web.post(
        "/apps/add", data={"csrf": csrf, "app": "calendar"}, headers=CONSOLE, follow_redirects=False
    )
    assert r.status_code in (302, 303) and not (tmp_path / "pending.env").exists()

    make_admin(authentik, "alice@example.org")
    assert web.get("/apps/role", headers=CONSOLE).json() == {"admin": True}
    ask = web.get("/apps/add?app=calendar", headers=CONSOLE).text
    assert "Add Calendar?" in ask and "https://calendar.belfry.test" in ask
    assert "Only admins" in web.get("/apps/add?app=<script>", headers=CONSOLE).text
    r = web.post(
        "/apps/add",
        data={"csrf": "wrong", "app": "calendar"},
        headers=CONSOLE,
        follow_redirects=False,
    )
    assert not (tmp_path / "pending.env").exists()
    r = web.post("/apps/add", data={"csrf": csrf, "app": "calendar"}, headers=CONSOLE)
    assert "Adding Calendar" in r.text
    pending = (tmp_path / "pending.env").read_text()
    assert (
        "BELFRY_EXTRA_APPS='calendar vote'" in pending
        or 'BELFRY_EXTRA_APPS="calendar vote"' in pending
    )
