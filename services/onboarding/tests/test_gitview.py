"""/git (gitview.py): GIT_REPO, the signed state, the files, README and commits."""

from __future__ import annotations

import subprocess

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from belfry_onboarding import gitview, nostr
from belfry_onboarding.app import create_app

PK = PrivateKey().public_key_xonly.format().hex()


def make_repo(path):
    path.mkdir()

    def run(*a):
        subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "ana@example.org")
    run("config", "user.name", "Ana")
    (path / "README.md").write_text("# Tool library\n\nShare <drills>.\n")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('<hi>')\n")
    run("add", ".")
    run("commit", "-q", "-m", "First")
    (path / "src" / "app.py").write_text("print('<hello>')\n")
    run("commit", "-qam", "Say hello")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(gitview, "CLONE_SCHEMES", ("https://", "file://"))
    monkeypatch.setattr(gitview, "CACHE_ROOT", tmp_path / "cache")
    src = tmp_path / "src"
    sha = make_repo(src)
    return f"file://{src}", sha


def test_parse():
    npub = nostr.hex_to_npub(PK)
    r = gitview.parse(f"nostr://{npub}/relay.ngit.dev/collective-belfry")
    assert (r.pubkey, r.relay, r.identifier) == (PK, "wss://relay.ngit.dev", "collective-belfry")
    assert r.nostr_url == f"nostr://{npub}/relay.ngit.dev/collective-belfry"
    hrp, _ = nostr.bech32_decode(r.naddr)
    assert hrp == "naddr"
    assert gitview.parse("") is None
    assert gitview.parse("https://example.org/x.git").url == "https://example.org/x.git"
    for bad in ("ftp://x", "nostr://npub1nope/r/x", f"nostr://{npub}/r/bad name", "file:///etc"):
        with pytest.raises(gitview.GitViewError):
            gitview.parse(bad)


def test_without_git_repo_there_is_no_git_page(client):
    assert client.get("/git").status_code == 404


def test_nostr_repo_shows_the_signed_state(services, repo, monkeypatch):
    url, sha = repo
    info = gitview.Info(
        name="Tool library",
        description="Lend & borrow",
        clone=[url],
        maintainers=[PK],
        refs={"refs/heads/main": sha},
        head="refs/heads/main",
    )
    monkeypatch.setattr(gitview, "nostr_info", lambda r, extra, sign: info)
    services.settings.git_repo = f"nostr://{nostr.hex_to_npub(PK)}/relay.example/tools"
    with TestClient(create_app(services)) as c:
        page = c.get("/git").text
        assert "<h1>Tool library</h1>" in page and "Lend &amp; borrow" in page
        assert "signed state" in page and sha[:12] in page
        assert f"git clone nostr://{nostr.hex_to_npub(PK)}/relay.example/tools" in page
        assert "gitworkshop.dev/naddr1" in page
        assert 'href="/git/tree/src"' in page and 'href="/git/blob/README.md"' in page
        assert "Share &lt;drills&gt;." in page and "Say hello" in page
        assert "&lt;hello&gt;" in c.get("/git/blob/src/app.py").text
        raw = c.get("/git/blob/src/app.py?raw=1")
        assert (
            raw.text == "print('<hello>')\n"
            and "default-src 'none'" in raw.headers["content-security-policy"]
        )
        assert 'href="/git/blob/src/app.py"' in c.get("/git/tree/src").text
        commits = c.get("/git/commits").text
        assert "Say hello" in commits and "First" in commits and "Ana" in commits
        assert c.get("/git/blob/nothing").status_code == 404
        assert c.get("/git/tree/README.md").status_code == 404


def test_a_server_ahead_of_the_signed_state_is_not_called_signed(services, repo, monkeypatch):
    url, sha = repo
    info = gitview.Info(
        name="t", clone=[url], refs={"refs/heads/main": "f" * 40}, head="refs/heads/main"
    )
    monkeypatch.setattr(gitview, "nostr_info", lambda r, extra, sign: info)
    services.settings.git_repo = f"nostr://{nostr.hex_to_npub(PK)}/relay.example/tools"
    with TestClient(create_app(services)) as c:
        r = c.get("/git")
        assert r.status_code == 503 and "signed state" in r.text


def test_plain_git_url(services, repo):
    url, sha = repo
    services.settings.git_repo = url
    with TestClient(create_app(services)) as c:
        page = c.get("/git").text
        assert sha[:12] in page and "unsigned" in page and "gitworkshop" not in page


def test_git_on_the_apex_with_a_homepage(services, repo, router):
    url, _ = repo
    s = services.settings
    s.git_repo = url
    s.site_url, s.public_url = "https://belfry.test", "https://pass.belfry.test"
    s.homepage = "https://site.example"
    with TestClient(create_app(services)) as c:
        r = c.get("/git", headers={"Host": "belfry.test"}, follow_redirects=False)
        assert r.status_code == 200 and "Say hello" in r.text
