"""The text pages (/privacy, /terms, /refunds, /rules, /help) and their Markdown renderer."""

from __future__ import annotations

import re

import pytest

from belfry_onboarding import pages


def test_renderer_covers_the_subset_and_escapes():
    title, body = pages.render(
        "# Title <x>\n\n> Draft [TO FILL: who]\n\nA **bold** `code` [link](/rules) and "
        "[bad](javascript:alert(1)) <script>.\n\n- one\n  continued\n- two\n\n"
        "1. first\n2. second\n\n"
        "| A | B |\n| --- | --- |\n| 1 | **2** |\n\n## Part {{who}}\n\n{{block}}\n",
        {"who": "<b>", "block__html": "<ul><li>raw</li></ul>"},
    )
    assert title == "Title <x>"
    assert "<h1>Title &lt;x&gt;</h1>" in body
    assert '<p class="note">Draft <mark class="fill">[TO FILL: who]</mark></p>' in body
    assert "<strong>bold</strong>" in body and "<code>code</code>" in body
    assert '<a href="/rules">link</a>' in body
    assert 'href="javascript' not in body
    assert "&lt;script&gt;" in body and "<script>" not in body
    assert "<ul><li>one continued</li><li>two</li></ul>" in body
    assert "<ol><li>first</li><li>second</li></ol>" in body
    assert "<th>A</th>" in body and "<td><strong>2</strong></td>" in body
    assert "<h2>Part &lt;b&gt;</h2>" in body  # deployment values are escaped too
    assert "<ul><li>raw</li></ul>" in body  # a block value is inserted as built


def test_community_apps_list_keeps_only_web_urls():
    assert pages.community_apps("Chat|https://chat.x|Talk;Bad|javascript:x|no;Plain|http://p") == [
        ("Chat", "https://chat.x", "Talk"),
        ("Plain", "http://p", ""),
    ]
    assert pages.community_apps("") == []


@pytest.mark.parametrize("name", list(pages.PAGES))
def test_every_page_renders_with_its_title(name, services, client, monkeypatch):
    monkeypatch.setenv(
        "COMMUNITY_APPS",
        "Chat|https://chat.belfry.test|Talk;Market|https://market.belfry.test|Offers",
    )
    services.settings.apps_public_url = "https://apps.belfry.test"
    services.settings.public_url = "https://pass.belfry.test"
    r = client.get(f"/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert f"<title>{pages.PAGES[name]}</title>" in r.text
    assert "{{" not in r.text  # every placeholder was filled
    assert re.search(r'<nav><a href="/help">Help</a>', r.text)  # the footer links the pages


def test_help_lists_the_apps_and_links_the_pass(services, client, monkeypatch):
    monkeypatch.setenv(
        "COMMUNITY_APPS",
        "Chat|https://chat.belfry.test|Talk;Market|https://market.belfry.test|Offers",
    )
    services.settings.apps_public_url = "https://apps.belfry.test"
    services.settings.public_url = "https://pass.belfry.test"
    r = client.get("/help")
    assert '<a href="https://chat.belfry.test"><strong>Chat</strong></a> · Talk' in r.text
    assert 'href="https://pass.belfry.test/me"' in r.text
    assert 'href="https://apps.belfry.test"' in r.text


def test_legal_drafts_show_what_is_missing(client):
    for name in ("privacy", "terms", "refunds"):
        r = client.get(f"/{name}")
        assert "Draft for legal review" in r.text
        assert '<mark class="fill">[TO FILL' in r.text


def test_home_page_links_help(client):
    r = client.get("/")
    assert 'href="/help"' in r.text


def test_skill_is_served_with_the_domain(services, client):
    services.settings.public_url = "https://pass.belfry.test"
    r = client.get("/skill.md")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.text.startswith("---\nname: open-source-village\n")
    assert "https://shop.belfry.test/belfry/api/invites/<token>" in r.text
    assert "<domain>" not in r.text


def test_skill_domain():
    class S:
        site_url = ""
        public_url = "http://localhost:8100"

    assert pages.site_domain(S) == ""  # local: the skill keeps <domain>
    S.public_url = "https://pass.example.org"
    assert pages.site_domain(S) == "example.org"
    S.site_url = "https://village.example"
    assert pages.site_domain(S) == "village.example"
