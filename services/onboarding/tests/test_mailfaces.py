"""Faces for e-mail correspondents: GET /api/avatar/<hash> (mailfaces.py, docs/one-shape.md)."""

from __future__ import annotations

import hashlib
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from belfry_onboarding import mailfaces
from belfry_onboarding.app import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 40
MARIE = hashlib.sha256(b"marie@example.org").hexdigest()
OTHER = hashlib.sha256(b"other@example.org").hexdigest()
HOST = {"Host": "belfry.belfry.test"}


@pytest.fixture
def faces(services, settings, monkeypatch):
    settings.console_url = "https://belfry.belfry.test"
    settings.nip05_domain = "belfry.test"
    mailfaces._failed.clear()
    mailfaces._flights.clear()
    mailfaces._counts.clear()
    mailfaces.PER_IP._seen.clear()
    asked: list[str] = []
    answers: dict[str, object] = {MARIE: (200, "image/png", PNG)}

    def fetch(digest):
        asked.append(digest)
        got = answers.get(digest, (404, "", b""))
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(mailfaces, "fetch", fetch)
    services.asked = asked  # type: ignore[attr-defined]
    services.answers = answers  # type: ignore[attr-defined]
    return services


def age(faces, digest, seconds):
    """Make what is kept for DIGEST look SECONDS old."""
    ext, path, _ = mailfaces._held(mailfaces.folder(faces), digest)
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_a_picture_is_served_from_the_belfry_and_kept_for_a_month(faces):
    with TestClient(create_app(faces)) as c:
        r = c.get(f"/api/v1/avatar/{MARIE}", headers=HOST)
        assert r.status_code == 200 and r.content == PNG
        assert r.headers["content-type"] == "image/png"
        assert r.headers["cache-control"] == "public, max-age=2592000, immutable"
        assert r.headers["x-content-type-options"] == "nosniff"
        # the second time Gravatar is not asked (pass.<domain>/api still answers too)
        old = c.get(f"/api/avatar/{MARIE}", headers={"Host": "pass.belfry.test"})
        assert old.content == PNG
    assert faces.asked == [MARIE]
    assert (mailfaces.folder(faces) / f"{MARIE}.png").read_bytes() == PNG
    assert mailfaces.folder(faces).parent == mailfaces.Path(faces.settings.register_path).parent


def test_the_chat_may_read_it_from_its_own_host(faces):
    with TestClient(create_app(faces)) as c:
        r = c.get(f"/api/v1/avatar/{MARIE}", headers={**HOST, "Origin": "https://chat.belfry.test"})
    assert r.headers["access-control-allow-origin"] == "https://chat.belfry.test"
    assert r.headers["cross-origin-resource-policy"] == "cross-origin"


def test_nobody_at_gravatar_is_a_404_remembered_for_a_week(faces):
    with TestClient(create_app(faces)) as c:
        r = c.get(f"/api/v1/avatar/{OTHER}", headers=HOST)
        assert r.status_code == 404 and r.content == b""
        assert r.headers["cache-control"] == "public, max-age=604800"
        assert c.get(f"/api/v1/avatar/{OTHER}", headers=HOST).status_code == 404
    assert faces.asked == [OTHER]
    # after a week it is asked again; a picture, after a month
    age(faces, OTHER, mailfaces.MISS_SECONDS + 5)
    assert mailfaces.face(faces, OTHER)[0] == ""
    assert faces.asked == [OTHER, OTHER]


def test_a_picture_is_asked_again_after_a_month_and_may_change(faces):
    assert mailfaces.face(faces, MARIE) == ("image/png", PNG, mailfaces.FOUND_SECONDS)
    age(faces, MARIE, mailfaces.FOUND_SECONDS - 60)
    mailfaces.face(faces, MARIE)
    assert faces.asked == [MARIE]
    age(faces, MARIE, mailfaces.FOUND_SECONDS + 60)
    faces.answers[MARIE] = (200, "image/jpeg", JPG)
    assert mailfaces.face(faces, MARIE) == ("image/jpeg", JPG, mailfaces.FOUND_SECONDS)
    root = mailfaces.folder(faces)
    assert sorted(p.name for p in root.iterdir()) == [f"{MARIE}.jpg"]


@pytest.mark.parametrize(
    "answer",
    [
        TimeoutError("slow"),
        (503, "", b""),
        (200, "text/html", b"<html>"),
        (200, "image/svg+xml", b"<svg/>"),
        (200, "image/png", b"<html>not a png"),
        (200, "image/png", PNG + b"\0" * mailfaces.MAX_BYTES),
    ],
)
def test_a_failure_is_a_short_404_and_not_asked_again_for_ten_minutes(faces, answer, monkeypatch):
    faces.answers[OTHER] = answer
    with TestClient(create_app(faces)) as c:
        r = c.get(f"/api/v1/avatar/{OTHER}", headers=HOST)
        assert r.status_code == 404
        assert r.headers["cache-control"] == "public, max-age=600"
        c.get(f"/api/v1/avatar/{OTHER}", headers=HOST)
    assert faces.asked == [OTHER]
    assert not mailfaces._held(mailfaces.folder(faces), OTHER)  # nothing on disk
    # ten minutes later it is asked again
    mailfaces._failed[OTHER] -= mailfaces.FAIL_SECONDS + 1
    faces.answers[OTHER] = (200, "image/png", PNG)
    assert mailfaces.face(faces, OTHER)[0] == "image/png"


def test_an_old_picture_is_still_shown_while_gravatar_is_down(faces):
    mailfaces.face(faces, MARIE)
    age(faces, MARIE, mailfaces.FOUND_SECONDS + 60)
    faces.answers[MARIE] = TimeoutError("slow")
    assert mailfaces.face(faces, MARIE) == ("image/png", PNG, mailfaces.FAIL_SECONDS)
    with TestClient(create_app(faces)) as c:
        r = c.get(f"/api/v1/avatar/{MARIE}", headers=HOST)
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=600"
    assert faces.asked == [MARIE, MARIE]


def test_what_is_no_hash_is_never_asked(faces):
    with TestClient(create_app(faces)) as c:
        for bad in ("abc", MARIE.upper(), MARIE + "0", MARIE[:-1] + "g", "..%2f" + MARIE[5:]):
            assert c.get(f"/api/v1/avatar/{bad}", headers=HOST).status_code == 404
        assert c.get(f"/api/v1/avatar/{MARIE}.png", headers=HOST).status_code == 404
    assert faces.asked == []


def test_turned_off_it_answers_404_and_asks_nobody(faces):
    faces.settings.avatars = False
    with TestClient(create_app(faces)) as c:
        r = c.get(f"/api/v1/avatar/{MARIE}", headers=HOST)
    assert r.status_code == 404 and r.headers["cache-control"] == "public, max-age=600"
    assert faces.asked == []
    assert not mailfaces.folder(faces).exists()


def test_the_setting_is_on_unless_said_otherwise(settings):
    from belfry_onboarding.settings import Settings

    assert settings.avatars is True
    assert Settings(**{**settings.model_dump(by_alias=True), "AVATARS": "0"}).avatars is False


def test_many_readers_of_one_thread_make_one_request(faces, monkeypatch):
    started = threading.Event()
    go = threading.Event()

    def slow(digest):
        faces.asked.append(digest)
        started.set()
        go.wait(5)
        return 200, "image/png", PNG

    monkeypatch.setattr(mailfaces, "fetch", slow)
    got: list = []
    threads = [
        threading.Thread(target=lambda: got.append(mailfaces.face(faces, MARIE))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    assert started.wait(5)
    time.sleep(0.05)
    go.set()
    for t in threads:
        t.join(5)
    assert faces.asked == [MARIE]
    assert [g[0] for g in got] == ["image/png"] * 8
    assert not mailfaces._flights


def test_the_oldest_go_when_the_folder_is_full(faces, monkeypatch):
    monkeypatch.setattr(mailfaces, "ENTRIES", 3)
    hashes = [hashlib.sha256(f"p{i}@example.org".encode()).hexdigest() for i in range(5)]
    for i, h in enumerate(hashes):
        mailfaces.face(faces, h)
        age(faces, h, 1000 - i * 100)  # the first asked is the oldest
    mailfaces.face(faces, MARIE)
    kept = {p.name.split(".")[0] for p in mailfaces.folder(faces).iterdir()}
    assert len(kept) == 3 and MARIE in kept
    assert hashes[0] not in kept and hashes[1] not in kept and hashes[2] not in kept


def test_a_full_disk_still_gives_the_picture(faces, monkeypatch):
    def full(self, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mailfaces.Path, "write_bytes", full)
    assert mailfaces.face(faces, MARIE)[:2] == ("image/png", PNG)


def test_a_reader_cannot_ask_without_end(faces, monkeypatch):
    monkeypatch.setattr(mailfaces, "PER_IP", mailfaces.Limiter(2, 60))
    with TestClient(create_app(faces)) as c:
        codes = [c.get(f"/api/v1/avatar/{MARIE}", headers=HOST).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_a_redirect_is_only_followed_to_gravatar_itself():
    import httpx

    assert mailfaces._gravatar(httpx.URL("https://secure.gravatar.com/avatar/x"))
    assert mailfaces._gravatar(httpx.URL("https://gravatar.com/avatar/x"))
    assert not mailfaces._gravatar(httpx.URL("http://gravatar.com/avatar/x"))
    assert not mailfaces._gravatar(httpx.URL("https://gravatar.com.evil.example/avatar/x"))
    assert not mailfaces._gravatar(httpx.URL("https://notgravatar.com/avatar/x"))
    assert not mailfaces._gravatar(httpx.URL("https://169.254.169.254/latest"))


def test_what_gravatar_is_asked_and_how(router):
    """The real request (the only test of ``fetch`` itself; the network is respx's)."""
    import httpx

    seen: list = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png; x=1"})

    router.get(f"https://gravatar.com/avatar/{MARIE}").mock(side_effect=answer)
    assert mailfaces.fetch(MARIE) == (200, "image/png", PNG)
    (req,) = seen
    assert dict(req.url.params) == {"s": "160", "d": "404", "r": "g"}
    assert "cookie" not in req.headers and "belfry" not in req.headers["user-agent"].lower()
    router.get(f"https://gravatar.com/avatar/{OTHER}").mock(return_value=httpx.Response(404))
    assert mailfaces.fetch(OTHER) == (404, "", b"")


def test_a_redirect_elsewhere_and_a_huge_answer_are_refused(router):
    import httpx

    there = f"https://secure.gravatar.com/avatar/{MARIE}"
    router.get(f"https://gravatar.com/avatar/{MARIE}").mock(
        return_value=httpx.Response(302, headers={"Location": there, "Set-Cookie": "a=b"})
    )
    followed = router.get(there).mock(return_value=httpx.Response(200, content=JPG))
    assert mailfaces.fetch(MARIE)[2] == JPG
    assert "cookie" not in followed.calls[0].request.headers
    router.get(f"https://gravatar.com/avatar/{OTHER}").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.org/x.png"})
    )
    elsewhere = router.get("https://example.org/x.png").mock(return_value=httpx.Response(200))
    with pytest.raises(ValueError):
        mailfaces.fetch(OTHER)
    assert not elsewhere.called
    big = hashlib.sha256(b"big").hexdigest()
    router.get(f"https://gravatar.com/avatar/{big}").mock(
        return_value=httpx.Response(200, content=b"\0" * (mailfaces.MAX_BYTES + 1))
    )
    with pytest.raises(ValueError):
        mailfaces.fetch(big)
