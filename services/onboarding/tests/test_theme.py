"""The page frame's theme (theme.py): base themes, the community's own files, /theme/*."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from belfry_onboarding import theme
from belfry_onboarding.app import create_app


def test_without_theme_the_pages_keep_the_osv_look(client):
    page = client.get("/").text
    assert "/brand/tokens.css" in page and "Open Source Village, run by Belfry" in page


def test_belfry_theme_with_the_community_name(services, tmp_path):
    s = services.settings
    s.theme, s.community_name, s.theme_dir = "belfry", "Example Commons", str(tmp_path / "none")
    s.console_url = "https://belfry.belfry.test"
    with TestClient(create_app(services)) as c:
        page = c.get("/").text
        assert "/theme/belfry/theme.css" in page and "/brand/tokens.css" not in page
        assert "Example Commons, run by Belfry" in page and "<h1>Example Commons</h1>" in page
        assert 'href="https://belfry.belfry.test/">This belfry</a>' in page
        assert "Open Source Village" not in page
        css = c.get("/theme/belfry/theme.css")
        assert css.status_code == 200 and "--font-title" in css.text
        assert c.get("/theme/belfry/fonts/inter-latin-wght-normal.woff2").status_code == 200
        assert c.get("/theme/../settings.py").status_code == 404
        assert c.get("/theme/custom/custom.css").status_code == 404


def test_community_files_override_the_base_and_survive_updates(services, tmp_path):
    folder = tmp_path / "theme"
    folder.mkdir()
    (folder / "custom.css").write_text(":root { --accent: #2a6f97; }")
    (folder / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (folder / ".env").write_text("secret")
    (folder / "notes.txt").write_text("x")
    (folder / "theme.json").write_text(
        json.dumps({"name": "Tool Library", "links": [["/help", "Help"]], "footer": "Since 1999"})
    )
    s = services.settings
    s.theme, s.theme_dir = "belfry", str(folder)
    with TestClient(create_app(services)) as c:
        page = c.get("/").text
        assert "/theme/custom/custom.css?v=" in page and "/theme/custom/logo.svg?v=" in page
        assert "<span>Tool Library</span>" in page and "Since 1999" in page
        assert '<a href="/help">Help</a>' in page
        assert page.index("/theme/belfry/theme.css") < page.index("/theme/custom/custom.css")
        assert "--accent: #2a6f97" in c.get("/theme/custom/custom.css").text
        assert c.get("/theme/custom/.env").status_code == 404
        assert c.get("/theme/custom/notes.txt").status_code == 404


def test_a_broken_theme_json_is_skipped(settings, tmp_path):
    (tmp_path / "theme.json").write_text("{nope")
    settings.theme, settings.theme_dir = "belfry", str(tmp_path)
    assert theme.load(settings).base == "belfry"
    settings.theme = "unknown"
    assert theme.load(settings).base == "belfry"


def test_the_base_stylesheets_are_not_swallowed_by_a_comment():
    from belfry_onboarding import theme as t

    for base in ("belfry", "osv"):
        import re

        css = (t.BUILTIN / base / "theme.css").read_text()
        assert css.count("/*") == css.count("*/")
        rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        assert ":root {" in rules and "--bg:" in rules
