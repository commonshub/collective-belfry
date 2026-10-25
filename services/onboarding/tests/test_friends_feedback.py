"""Inviting friends from the pass page (/me) and the feedback page (/feedback)."""

from __future__ import annotations

import base64

from .test_mypass import login, passing, web  # noqa: F401 - fixtures are used by name
from .test_steward import login as steward_login
from .test_steward import stewarding  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_participant_invites_friends_up_to_the_limit(web, passing, odoo):  # noqa: F811
    login(web)
    page = web.get("/me").text
    assert "Invite friends" in page and "3 invites left" in page
    r = web.post("/me/invites", data={"note": "for Carla"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/me?msg=created#invite"
    page = web.get("/me?msg=created").text
    assert "Invite link created" in page and "for Carla" in page and "2 invites left" in page
    invite = next(iter(odoo.tables["belfry.invite"].values()))
    assert invite["inviter_partner_id"] == passing["partner"] and invite["url"] in page
    web.post("/me/invites", data={})
    web.post("/me/invites", data={})
    assert "no invites left" in web.get("/me").text
    r = web.post("/me/invites", data={})
    assert r.status_code == 409 and "no invites left" in r.text
    # revoking an unused one gives the invite back
    r = web.post(f"/me/invites/{invite['id']}/revoke", follow_redirects=False)
    assert r.status_code == 303 and invite["state"] == "revoked"
    assert "1 invite left" in web.get("/me").text


def test_invites_need_a_session_and_this_site(web, passing, odoo):  # noqa: F811
    r = web.post("/me/invites", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/me/login"
    login(web)
    r = web.post("/me/invites", data={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not odoo.tables["belfry.invite"]
    # someone else's invite cannot be revoked
    other = odoo.add("res.partner", name="Bob")
    theirs = odoo.add(
        "belfry.invite", inviter_partner_id=other, state="open", url="u", expires_at="x"
    )
    assert web.post(f"/me/invites/{theirs}/revoke").status_code == 409
    assert odoo.tables["belfry.invite"][theirs]["state"] == "open"


def test_feedback_without_signing_in(web, odoo, authentik):  # noqa: F811
    authentik.users.append({"pk": 7, "username": "sam", "email": "sam@belfry.test"})
    authentik.groups["belfry-stewards"] = {"pk": "g", "members": {7}}
    page = web.get("/feedback?from=https://photos.belfry.test/%23/new").text
    assert "What happened?" in page and "https://photos.belfry.test/#/new" in page
    assert 'name="email"' in page
    r = web.post(
        "/feedback",
        data={
            "happened": "Upload hangs",
            "expected": "the photo",
            "email": "t@example.org",
            "from": "https://photos.belfry.test/#/new",
        },
        files={"screenshot": ("shot.png", PNG, "image/png")},
    )
    assert r.status_code == 200 and "Thank you" in r.text
    assert 'href="https://photos.belfry.test/#/new"' in r.text
    fb = next(iter(odoo.tables["belfry.feedback"].values()))
    assert fb["happened"] == "Upload hangs" and fb["email"] == "t@example.org"
    assert fb["page_url"] == "https://photos.belfry.test/#/new"
    assert base64.b64decode(fb["screenshot"]) == PNG and fb["mailed_to"] == ["sam@belfry.test"]
    assert "partner_id" not in fb or not fb["partner_id"]


def test_feedback_names_the_signed_in_participant(web, passing, odoo):  # noqa: F811
    login(web)
    page = web.get("/feedback").text
    assert "you are signed in" in page.lower() and 'name="email"' not in page
    web.post("/feedback", data={"happened": "Great", "from": "javascript:alert(1)"})
    fb = next(iter(odoo.tables["belfry.feedback"].values()))
    assert fb["partner_id"] == passing["partner"] and not fb.get("page_url")


def test_feedback_refuses_empty_text_bad_files_and_bots(web, odoo):  # noqa: F811
    r = web.post("/feedback", data={"happened": "  "})
    assert r.status_code == 400 and "write what happened" in r.text
    r = web.post(
        "/feedback",
        data={"happened": "x"},
        files={"screenshot": ("a.gif", b"GIF89a", "image/gif")},
    )
    assert r.status_code == 400 and "PNG, JPEG or WebP" in r.text
    r = web.post("/feedback", data={"happened": "buy pills", "website": "http://spam"})
    assert r.status_code == 200 and "Thank you" in r.text
    assert not odoo.tables["belfry.feedback"]


def test_stewards_read_and_close_feedback(web, odoo, stewarding):  # noqa: F811
    web.post(
        "/feedback",
        data={"happened": "The door page froze", "email": "t@example.org"},
        files={"screenshot": ("s.png", PNG, "image/png")},
    )
    fid = next(iter(odoo.tables["belfry.feedback"]))
    csrf = steward_login(web)
    page = web.get("/steward").text
    assert "id='feedback'" in page and "Feedback (1 new)" in page
    assert "The door page froze" in page and "t@example.org" in page
    shot = web.get(f"/steward/feedback/{fid}/screenshot")
    assert shot.status_code == 200 and shot.content == PNG
    assert shot.headers["content-type"] == "image/png"
    r = web.post(f"/steward/feedback/{fid}/done", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and odoo.tables["belfry.feedback"][fid]["state"] == "done"
    assert web.post(f"/steward/feedback/{fid}/delete", data={"csrf": csrf}).status_code == 404


def test_screenshot_needs_a_steward(web, odoo):  # noqa: F811
    fid = odoo.add("belfry.feedback", happened="x", screenshot=base64.b64encode(PNG).decode())
    assert web.get(f"/steward/feedback/{fid}/screenshot").status_code in (401, 503)


def test_steward_invite_carries_the_stewards_name(web, odoo, stewarding):  # noqa: F811
    csrf = steward_login(web)
    web.post("/steward/invites", data={"csrf": csrf, "note": "for Dan"})
    invite = next(iter(odoo.tables["belfry.invite"].values()))
    assert invite["inviter_name"] == "Steward Sam"
    assert "Invited by" in web.get("/steward").text
