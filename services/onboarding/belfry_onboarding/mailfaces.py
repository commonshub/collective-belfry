"""Faces for e-mail correspondents: ``GET /api/avatar/<hash>`` (docs/one-shape.md).

Someone who writes to a channel by e-mail has no profile here. The apps show their Gravatar
picture when they have one, else initials. An app that asked Gravatar itself would tell Gravatar
who reads whose mail, and from which address of the network; so the belfry asks, and keeps what
it learned for a long time: Gravatar sees the belfry, once a month per correspondent.

- ``<hash>`` = 64 lowercase hex, the SHA-256 of the trimmed, lowercased address (Gravatar takes
  it as it is). No sign-in: it is loaded as a picture.
- 200 with the picture (``immutable``, 30 days), or 404 = draw initials (7 days when Gravatar
  has none; 10 minutes when Gravatar did not answer, or with ``AVATARS=0``).
- The cache is a folder next to the register (``faces/``): ``<hash>.jpg|png|webp|gif`` for a
  picture, ``<hash>.none`` for none, the file's time = when it was asked. At most ENTRIES files,
  the oldest go first. Failures are only remembered in memory.
- One request to Gravatar per hash at a time, UPSTREAM at once in all.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .core import Services
from .events import Limiter

log = logging.getLogger(__name__)

PATH = "/api/avatar"
HASH = re.compile(r"^[0-9a-f]{64}$")
UPSTREAM_URL = "https://gravatar.com/avatar/{hash}?s=160&d=404&r=g"
HOSTS = ("gravatar.com",)  # and its own subdomains: a redirect anywhere else is not followed
AGENT = "Mozilla/5.0 (compatible; avatar-cache)"  # says nothing about who asks
TIMEOUT = 5.0
MAX_BYTES = 200_000
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
BY_EXT = {ext: ctype for ctype, ext in TYPES.items()}
NONE = "none"
FOUND_SECONDS = 30 * 86400
MISS_SECONDS = 7 * 86400
FAIL_SECONDS = 600
ENTRIES = 5000
PER_IP = Limiter(600, 60)  # a screen of mail threads asks for a few dozen at once
UPSTREAM = threading.BoundedSemaphore(4)

_lock = threading.Lock()
_flights: dict[str, list] = {}  # hash → [lock, how many wait on it]
_failed: dict[str, float] = {}  # hash → when (monotonic) Gravatar did not answer
_counts: dict[str, int] = {}  # cache folder → files in it, counted once then kept in step


def folder(svc: Services) -> Path:
    return Path(svc.settings.register_path).with_name("faces")


def _magic(ctype: str, body: bytes) -> bool:
    """BODY really is what CTYPE says (the answer is served from the belfry's own host)."""
    if ctype == "image/jpeg":
        return body[:3] == b"\xff\xd8\xff"
    if ctype == "image/png":
        return body[:8] == b"\x89PNG\r\n\x1a\n"
    if ctype == "image/gif":
        return body[:6] in (b"GIF87a", b"GIF89a")
    return body[:4] == b"RIFF" and body[8:12] == b"WEBP"


def _gravatar(url: httpx.URL) -> bool:
    host = (url.host or "").lower()
    return url.scheme == "https" and any(host == h or host.endswith("." + h) for h in HOSTS)


def fetch(digest: str) -> tuple[int, str, bytes]:
    """Ask Gravatar: (status, content type, body). Tests replace this; nothing else in the
    module touches the network. No cookies, no redirect to another host, never more than
    MAX_BYTES read. Raises on a network error or an answer that is too big."""
    url = httpx.URL(UPSTREAM_URL.format(hash=digest))
    with httpx.Client(
        timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": AGENT}, trust_env=False
    ) as client:
        for _ in range(3):
            with client.stream("GET", url, cookies=None) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = url.join(r.headers.get("location", ""))
                    if not _gravatar(url):
                        raise ValueError("redirect to another host")
                    client.cookies.clear()
                    continue
                if r.status_code != 200:
                    return r.status_code, "", b""
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise ValueError("too big")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                return 200, ctype, body
    raise ValueError("too many redirects")


# ── the folder ──────────────────────────────────────────────────────────────────────────────
def _held(root: Path, digest: str) -> tuple[str, Path, float] | None:
    """(extension, file, age in seconds) of what is kept for DIGEST."""
    for ext in (*BY_EXT, NONE):
        path = root / f"{digest}.{ext}"
        try:
            return ext, path, time.time() - path.stat().st_mtime
        except OSError:
            continue
    return None


def _keep(root: Path, digest: str, ext: str, body: bytes) -> None:
    """Write the answer, replacing what was kept; over ENTRIES files the oldest go. A full disk
    is no reason to fail: the answer is still given, just asked again next time."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        for other in (*BY_EXT, NONE):
            if other != ext:
                (root / f"{digest}.{other}").unlink(missing_ok=True)
        tmp = root / f".{digest}.tmp"
        tmp.write_bytes(body)
        tmp.replace(root / f"{digest}.{ext}")
        with _lock:
            known = _counts.get(str(root))
            if known is not None:
                _counts[str(root)] = known + 1  # a replaced file counted twice: trimmed early
        _trim(root)
    except OSError as exc:
        log.warning("faces: not kept: %s", type(exc).__name__)


def _trim(root: Path) -> None:
    with _lock:
        known = _counts.get(str(root))
    if known is not None and known <= ENTRIES:
        return
    files = [(p.stat().st_mtime, p) for p in root.iterdir() if not p.name.startswith(".")]
    files.sort(key=lambda f: f[0])
    extra = len(files) - ENTRIES
    for _, path in files[: max(extra, 0)]:
        path.unlink(missing_ok=True)
    with _lock:
        _counts[str(root)] = min(len(files), ENTRIES)


# ── one face ────────────────────────────────────────────────────────────────────────────────
def face(svc: Services, digest: str) -> tuple[str, bytes, int]:
    """(content type, picture, seconds to keep) for DIGEST; content type "" = none (draw
    initials). Never raises."""
    root = folder(svc)
    held = _held(root, digest)
    if held and held[2] < (MISS_SECONDS if held[0] == NONE else FOUND_SECONDS):
        return _read(held)
    with _lock:
        failed = _failed.get(digest)
    if failed is not None and time.monotonic() - failed < FAIL_SECONDS:
        return _stale(held)
    with _lock:
        flight = _flights.setdefault(digest, [threading.Lock(), 0])
        flight[1] += 1  # how many wait on it: the last one out takes it away
    try:
        with flight[0]:
            again = _held(root, digest)  # someone asked while we waited
            if again and again[2] < (MISS_SECONDS if again[0] == NONE else FOUND_SECONDS):
                return _read(again)
            with _lock:
                failed = _failed.get(digest)
            if failed is not None and time.monotonic() - failed < FAIL_SECONDS:
                return _stale(again)
            return _ask(root, digest, again)
    finally:
        with _lock:
            flight[1] -= 1
            if not flight[1]:
                _flights.pop(digest, None)


def _read(held: tuple[str, Path, float]) -> tuple[str, bytes, int]:
    ext, path, _ = held
    if ext == NONE:
        return "", b"", MISS_SECONDS
    try:
        return BY_EXT[ext], path.read_bytes(), FOUND_SECONDS
    except OSError:
        return "", b"", FAIL_SECONDS


def _stale(held: tuple[str, Path, float] | None) -> tuple[str, bytes, int]:
    """Gravatar is not answering: a picture kept from before is better than initials."""
    if held and held[0] != NONE:
        ctype, body, _ = _read(held)
        if ctype:
            return ctype, body, FAIL_SECONDS
    return "", b"", FAIL_SECONDS


def _ask(root: Path, digest: str, held: tuple[str, Path, float] | None) -> tuple[str, bytes, int]:
    why = ""
    if not UPSTREAM.acquire(timeout=TIMEOUT):
        why = "busy"
    else:
        try:
            status, ctype, body = fetch(digest)
            if status == 404:
                _keep(root, digest, NONE, b"")
                return "", b"", MISS_SECONDS
            if status == 200 and ctype in TYPES and body and _magic(ctype, body):
                if len(body) <= MAX_BYTES:
                    _keep(root, digest, TYPES[ctype], body)
                    return ctype, body, FOUND_SECONDS
                why = "too big"
            else:
                why = f"answer {status}" if status != 200 else "not a picture"
        except Exception as exc:  # noqa: BLE001 — a timeout, no network: initials for now
            why = type(exc).__name__
        finally:
            UPSTREAM.release()
    log.info("faces: no answer for %s…: %s", digest[:8], why)
    with _lock:
        if len(_failed) > 10_000:
            _failed.clear()
        _failed[digest] = time.monotonic()
    return _stale(held)


def _ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?"
    )


def _none(seconds: int) -> Response:
    return Response(status_code=404, headers={"Cache-Control": f"public, max-age={seconds}"})


def register(app: FastAPI) -> None:
    @app.get(PATH + "/{digest}")
    async def mail_face(request: Request, digest: str) -> Response:
        s: Services = request.app.state.services
        if not HASH.fullmatch(digest):
            return _none(MISS_SECONDS)  # never a hash: nothing to ask, ever
        if not s.settings.avatars:
            return _none(FAIL_SECONDS)
        if not PER_IP.allow("ip:" + _ip(request)):
            return Response(status_code=429, headers={"Retry-After": "60"})
        ctype, body, seconds = await run_in_threadpool(face, s, digest)
        if not ctype:
            return _none(seconds)
        cache = f"public, max-age={seconds}" + (", immutable" if seconds == FOUND_SECONDS else "")
        return Response(
            body,
            media_type=ctype,
            headers={
                "Cache-Control": cache,
                "X-Content-Type-Options": "nosniff",
                # shown by the chat, which lives on another host of the belfry
                "Cross-Origin-Resource-Policy": "cross-origin",
            },
        )


__all__ = ["register", "face", "fetch", "folder"]
