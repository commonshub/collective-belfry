"""Link previews: ``GET /api/link-preview?url=…`` (belfry.<domain>/api/v1/link-preview), NIP-98.

The apps show a box under a message for an outside https link: its title, description and
picture (Open Graph). An app that fetched the page itself would tell the site who reads that
message, from which address; so the belfry fetches, once a day per page, and hands the picture
on through its own address. Contract for the apps: docs/link-previews.md.

- Who may ask: a key with an active pass (members, agents, guests) or a website session
  (sitesessions.py). 60 a minute per caller.
- ``url``: an ``https://`` address on the public internet (port 443). Refused before anything is
  fetched: other schemes, credentials in it, IP literals, single-label and private names, the
  belfry's own domain and its subdomains (remoteapps.own_hosts). Every name is looked up first
  and the connection goes to the address checked (no DNS rebinding); an address that is not a
  public one (private, loopback, link-local, multicast, reserved, CGNAT) or one of the belfry's
  own is refused. At most 3 redirects, each checked the same way.
- The page: ``text/html`` (or XHTML) only, 5 s, the first 512 kB. ``og:*``, then ``twitter:*``,
  then ``<title>`` and ``<meta name=description>``; tags stripped, spaces collapsed, title 200
  and description 300 characters.
- → 200 ``{"url" (after redirects), "title", "description", "site_name", "image",
  "image_width"?, "image_height"?}`` (a missing text is null), or 404 ``{"error": "none"}``
  when the page has no title and no description (also when it can't be fetched). 422
  ``invalid`` for an address that is not allowed. Kept 24 h; nothing usable, 1 h.
- ``image`` is the belfry's own address, ``…/api/v1/link-preview/image/<hash>``, never the
  site's: ``<hash>`` = HMAC-SHA256 of the picture's address with the belfry's secret, so it
  opens only pictures a preview named (no open proxy). No sign-in (it is loaded as a picture).
  PNG, JPEG, WebP or GIF (checked by their first bytes), at most 2 MB, kept on disk next to
  the register (``link-previews/``) for 7 days.
- At most 4 fetches at once in all, one per page or picture at a time.

The network is only touched in ``resolve`` and ``_get``: tests replace them.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import logging
import re
import socket
import threading
import time
import unicodedata
import urllib.parse
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .core import Services
from .events import Limiter
from .holder_invites import ApiError, authenticate

log = logging.getLogger(__name__)

PATH = "/api/link-preview"
IMAGE_PATH = PATH + "/image"
HASH = re.compile(r"^[0-9a-f]{64}$")
# says what it is and nothing about who asks
AGENT = "Mozilla/5.0 (compatible; Belfry link preview; +https://belfry.sh)"
TIMEOUT = 5.0
URL_MAX = 2000
HTML_MAX = 512 * 1024
IMAGE_MAX = 2 * 1024 * 1024
REDIRECTS = 3
TITLE_MAX, DESCRIPTION_MAX, SITE_MAX = 200, 300, 100
FOUND_SECONDS = 86400
NONE_SECONDS = 3600
IMAGE_SECONDS = 7 * 86400
IMAGE_FAIL_SECONDS = 3600
ENTRIES = 2000  # previews kept in memory
IMAGE_FILES = 3000  # pictures kept on disk
HTML_TYPES = ("text/html", "application/xhtml+xml")
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
BY_EXT = {ext: ctype for ctype, ext in IMAGE_TYPES.items()}
CALLS = Limiter(60, 60)  # per caller
IMAGES = Limiter(300, 60)  # picture loads per client address (a feed shows several)
UPSTREAM = threading.BoundedSemaphore(4)
OWN_IPS_SECONDS = 600


class Refused(Exception):
    """An address the belfry does not fetch (the reason is for the log only)."""


# ── the address ─────────────────────────────────────────────────────────────────────────────
def _host_ascii(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise Refused("bad name") from exc


def check_url(svc: Services, raw: Any) -> tuple[str, str]:
    """(the address as it will be asked, its host); Refused for anything not allowed."""
    from .remoteapps import _public_host, own_hosts

    if not isinstance(raw, str) or not raw or len(raw) > URL_MAX or raw != raw.strip():
        raise Refused("not an address")
    if any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise Refused("spaces or controls")
    try:
        u = urllib.parse.urlsplit(raw)
        port = u.port
    except ValueError as exc:
        raise Refused("unreadable") from exc
    if u.scheme.lower() != "https" or not u.hostname:
        raise Refused("not https")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise Refused("credentials")
    if port not in (None, 443):
        raise Refused("port")
    host = _host_ascii(u.hostname)
    if not _public_host(host):
        raise Refused("not a public name")
    own = own_hosts(svc.settings)
    domain = own.domain
    if (domain and (host == domain or host.endswith("." + domain))) or host in own.api:
        raise Refused("the belfry's own")
    if host in own.relays or host in own.blossom:
        raise Refused("the belfry's own")
    netloc = host if port is None else f"{host}:{port}"
    clean = urllib.parse.urlunsplit(("https", netloc, u.path or "/", u.query, ""))
    return clean, host


def resolve(host: str) -> list[str]:
    """The addresses of HOST (tests replace this)."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def _public_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
    )


_own_ips: tuple[float, frozenset[str]] = (float("-inf"), frozenset())


def own_ips(svc: Services) -> frozenset[str]:
    """The belfry's own public addresses (its domain and belfry.<domain>), looked up again every
    OWN_IPS_SECONDS."""
    global _own_ips
    at, held = _own_ips
    if time.monotonic() - at < OWN_IPS_SECONDS:
        return held
    domain = (getattr(svc.settings, "nip05_domain", "") or "").strip().lower()
    found: set[str] = set()
    for name in (domain, f"belfry.{domain}", f"chat.{domain}") if "." in domain else ():
        try:
            found |= set(resolve(name))
        except OSError:
            continue
    _own_ips = (time.monotonic(), frozenset(found))
    return _own_ips[1]


def address_of(svc: Services, host: str) -> str:
    """The one address of HOST to connect to: every address it has must be public and none the
    belfry's own (a name that answers both ways is refused)."""
    try:
        found = resolve(host)
    except OSError as exc:
        raise Refused("no such name") from exc
    if not found:
        raise Refused("no address")
    mine = own_ips(svc)
    for a in found:
        if not _public_ip(a) or a in mine:
            raise Refused("not a public address")
    return next((a for a in found if ":" not in a), found[0])  # IPv4 first: the boxes' v6 varies


# ── fetching ────────────────────────────────────────────────────────────────────────────────
def _get(url: str, host: str, ip: str, accept: str, max_bytes: int) -> tuple[int, dict, bytes]:
    """One GET of URL, connected to IP (the name checked), the certificate checked for HOST:
    (status, headers in lower case, at most MAX_BYTES of the body). Tests replace this."""
    u = urllib.parse.urlsplit(url)
    literal = f"[{ip}]" if ":" in ip else ip
    target = urllib.parse.urlunsplit(("https", literal, u.path or "/", u.query, ""))
    headers = {"Host": u.netloc, "User-Agent": AGENT, "Accept": accept, "Accept-Language": "en"}
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        req = client.build_request("GET", target, headers=headers)
        req.extensions["sni_hostname"] = host
        r = client.send(req, stream=True)
        try:
            body = b""
            if r.status_code == 200:
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        break
            return r.status_code, {k.lower(): v for k, v in r.headers.items()}, body
        finally:
            r.close()


def fetch(svc: Services, url: str, accept: str, max_bytes: int) -> tuple[str, str, bytes, bool]:
    """GET URL with every hop checked: (final address, content type, body, cut). ``cut``: the
    body was longer than MAX_BYTES (only its first bytes are here). Refused, or raises on the
    network; a status other than 200 is Refused too."""
    started = time.monotonic()
    for _ in range(REDIRECTS + 1):
        url, host = check_url(svc, url)
        ip = address_of(svc, host)
        if time.monotonic() - started > TIMEOUT * 2:
            raise Refused("too slow")
        status, headers, body = _get(url, host, ip, accept, max_bytes)
        if status in (301, 302, 303, 307, 308):
            nxt = headers.get("location", "")
            if not nxt:
                raise Refused("redirect without a location")
            url = urllib.parse.urljoin(url, nxt)
            continue
        if status != 200:
            raise Refused(f"answer {status}")
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        cut = len(body) > max_bytes
        return url, ctype, body[:max_bytes], cut
    raise Refused("too many redirects")


# ── the page ────────────────────────────────────────────────────────────────────────────────
class _Head(HTMLParser):
    """The meta tags (property/name → content, first one wins) and the title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.charset = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
            if a.get("charset") and not self.charset:
                self.charset = a["charset"].strip().lower()
        elif tag == "title" and not self.title:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and len(self.title) < 2000:
            self.title += data


TAGS = re.compile(r"<[^>]*>")


def clean(text: Any, limit: int) -> str | None:
    """Text as a person reads it: entities decoded, tags and controls out, spaces collapsed, at
    most LIMIT characters (cut at a word, with "…"); None when nothing is left."""
    if not isinstance(text, str):
        return None
    text = html.unescape(TAGS.sub(" ", html.unescape(text)))
    text = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > limit:
        cut = text[: limit - 1]
        if " " in cut[limit // 2 :]:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" .,;:-") + "…"
    return text


def _charset(ctype_header: str, body: bytes) -> str:
    m = re.search(r"charset=([\w-]+)", ctype_header or "", re.I)
    if m:
        return m.group(1).lower()
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    return m.group(1).decode("ascii").lower() if m else "utf-8"


def parse(page: bytes, base: str, charset: str = "utf-8") -> dict[str, Any]:
    """The preview of a page fetched from BASE: title, description, site_name, the picture's
    address (absolute, https) and its size when the page says it."""
    try:
        text = page.decode(charset, errors="replace")
    except LookupError:
        text = page.decode("utf-8", errors="replace")
    head = _Head()
    try:
        head.feed(text)
        head.close()
    except Exception:  # noqa: BLE001 — what was read before the parser gave up still counts
        pass
    m = head.meta

    def first(*keys: str) -> str | None:
        return next((m[k] for k in keys if m.get(k, "").strip()), None)

    image = first(
        "og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src"
    )
    if image:
        image = urllib.parse.urljoin(base, html.unescape(image.strip()))
        if not image.startswith("https://") or len(image) > URL_MAX:
            image = None
    out: dict[str, Any] = {
        "title": clean(first("og:title", "twitter:title") or head.title, TITLE_MAX),
        "description": clean(
            first("og:description", "twitter:description", "description"), DESCRIPTION_MAX
        ),
        "site_name": clean(first("og:site_name", "application-name"), SITE_MAX),
        "image_url": image,
    }
    for side in ("width", "height"):
        raw = (m.get(f"og:image:{side}") or "").strip()
        if image and raw.isdigit() and 0 < int(raw) <= 20000:
            out[f"image_{side}"] = int(raw)
    return out


# ── previews ────────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_previews: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()
_flights: dict[str, list] = {}  # key → [lock, how many wait on it]
_image_failed: dict[str, float] = {}


def forget() -> None:
    """Empty the caches (tests)."""
    global _own_ips
    with _lock:
        _previews.clear()
        _flights.clear()
        _image_failed.clear()
    _own_ips = (float("-inf"), frozenset())


def _secret(svc: Services) -> bytes:
    return (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()


def image_hash(svc: Services, image_url: str) -> str:
    return hmac.new(
        _secret(svc), f"link-preview-image:{image_url}".encode(), hashlib.sha256
    ).hexdigest()


def folder(svc: Services) -> Path:
    return Path(svc.settings.register_path).with_name("link-previews")


def api_base(svc: Services) -> str:
    """Where the apps load the picture: the belfry's API (belfry.<domain>/api/v1)."""
    console = (getattr(svc.settings, "console_url", "") or "").rstrip("/")
    if console:
        return console + "/api/v1"
    domain = getattr(svc.settings, "nip05_domain", "") or ""
    if "." in domain:
        return f"https://belfry.{domain}/api/v1"
    return (svc.settings.public_url or "").rstrip("/") + "/api"


def _flight(key: str) -> list:
    with _lock:
        f = _flights.setdefault(key, [threading.Lock(), 0])
        f[1] += 1
        return f


def _land(key: str, f: list) -> None:
    with _lock:
        f[1] -= 1
        if not f[1]:
            _flights.pop(key, None)


def _held(key: str) -> tuple[bool, dict[str, Any] | None]:
    with _lock:
        got = _previews.get(key)
        if got is None:
            return False, None
        at, value = got
        if time.monotonic() - at < (FOUND_SECONDS if value else NONE_SECONDS):
            _previews.move_to_end(key)
            return True, value
        _previews.pop(key, None)
        return False, None


def _keep_preview(key: str, value: dict[str, Any] | None) -> None:
    with _lock:
        _previews[key] = (time.monotonic(), value)
        _previews.move_to_end(key)
        while len(_previews) > ENTRIES:
            _previews.popitem(last=False)


def _note_image(svc: Services, digest: str, image_url: str) -> None:
    """Remember which address a picture's hash names (the image route reads it back)."""
    root = folder(svc)
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{digest}.url"
        if not path.exists():
            tmp = root / f".{digest}.url.tmp"
            tmp.write_text(image_url, encoding="utf-8")
            tmp.replace(path)
            _trim(root)
    except OSError as exc:
        log.warning("link previews: picture address not kept: %s", type(exc).__name__)


def _answer(svc: Services, found: dict[str, Any], final: str) -> dict[str, Any] | None:
    if not found.get("title") and not found.get("description"):
        return None
    image = None
    out: dict[str, Any] = {
        "url": final,
        "title": found.get("title"),
        "description": found.get("description"),
        "site_name": found.get("site_name"),
    }
    if found.get("image_url"):
        try:
            check_url(svc, found["image_url"])  # never the belfry's own nor a private name
            digest = image_hash(svc, found["image_url"])
            _note_image(svc, digest, found["image_url"])
            image = f"{api_base(svc)}/link-preview/image/{digest}"
        except Refused:
            image = None
    out["image"] = image
    if image:
        for side in ("image_width", "image_height"):
            if side in found:
                out[side] = found[side]
    return out


def preview(svc: Services, raw: Any) -> dict[str, Any] | None:
    """The preview of RAW (checked: Refused), or None for a page with nothing to show. Raises
    ``Busy`` when every upstream slot stays taken."""
    url, _host = check_url(svc, raw)
    key = hashlib.sha256(url.encode()).hexdigest()
    hit, value = _held(key)
    if hit:
        return value
    f = _flight(key)
    try:
        with f[0]:
            hit, value = _held(key)
            if hit:
                return value
            value = _fetch_preview(svc, url)
            _keep_preview(key, value)
            return value
    finally:
        _land(key, f)


class Busy(Exception):
    pass


def _fetch_preview(svc: Services, url: str) -> dict[str, Any] | None:
    if not UPSTREAM.acquire(timeout=TIMEOUT):
        raise Busy
    try:
        try:
            final, ctype, body, _cut = fetch(
                svc, url, "text/html,application/xhtml+xml;q=0.9", HTML_MAX
            )
        except Refused as exc:
            log.info("link previews: nothing for a page: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — a timeout, a bad certificate: no preview
            log.info("link previews: a page did not answer: %s", type(exc).__name__)
            return None
        if ctype not in HTML_TYPES:
            return None
        found = parse(body, final, _charset(ctype, body))
        return _answer(svc, found, final)
    finally:
        UPSTREAM.release()


# ── pictures ────────────────────────────────────────────────────────────────────────────────
def _trim(root: Path) -> None:
    try:
        files = [p for p in root.iterdir() if not p.name.startswith(".")]
    except OSError:
        return
    if len(files) <= IMAGE_FILES * 2:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files[: len(files) - IMAGE_FILES * 2]:
        p.unlink(missing_ok=True)


def _image_held(root: Path, digest: str) -> tuple[str, bytes] | None:
    for ext, ctype in BY_EXT.items():
        path = root / f"{digest}.{ext}"
        try:
            if time.time() - path.stat().st_mtime < IMAGE_SECONDS:
                return ctype, path.read_bytes()
        except OSError:
            continue
    return None


def image(svc: Services, digest: str) -> tuple[str, bytes] | None:
    """(content type, bytes) of the picture DIGEST names, or None (404)."""
    from .mailfaces import _magic

    if not HASH.fullmatch(digest):
        return None
    root = folder(svc)
    held = _image_held(root, digest)
    if held:
        return held
    try:
        image_url = (root / f"{digest}.url").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not hmac.compare_digest(image_hash(svc, image_url), digest):
        return None  # a file not written by this belfry, or another secret: never fetched
    with _lock:
        failed = _image_failed.get(digest)
    if failed is not None and time.monotonic() - failed < IMAGE_FAIL_SECONDS:
        return None
    f = _flight("img:" + digest)
    try:
        with f[0]:
            held = _image_held(root, digest)
            if held:
                return held
            got = _fetch_image(svc, image_url)
            if got is None:
                with _lock:
                    if len(_image_failed) > 10_000:
                        _image_failed.clear()
                    _image_failed[digest] = time.monotonic()
                return None
            ctype, body = got
            if not _magic(ctype, body):
                return None
            try:
                tmp = root / f".{digest}.tmp"
                tmp.write_bytes(body)
                tmp.replace(root / f"{digest}.{IMAGE_TYPES[ctype]}")
                _trim(root)
            except OSError as exc:  # a full disk: served, asked again next time
                log.warning("link previews: picture not kept: %s", type(exc).__name__)
            return ctype, body
    finally:
        _land("img:" + digest, f)


def _fetch_image(svc: Services, image_url: str) -> tuple[str, bytes] | None:
    if not UPSTREAM.acquire(timeout=TIMEOUT):
        return None
    try:
        _final, ctype, body, cut = fetch(svc, image_url, ",".join(IMAGE_TYPES), IMAGE_MAX)
    except Exception as exc:  # noqa: BLE001 — Refused, a timeout: no picture
        log.info("link previews: no picture: %s", type(exc).__name__)
        return None
    finally:
        UPSTREAM.release()
    if cut or ctype not in IMAGE_TYPES or not body:
        return None
    return ctype, body


# ── who asks ────────────────────────────────────────────────────────────────────────────────
def caller_allowed(svc: Services, key: str) -> bool:
    """A key with an active pass, or a live website session."""
    rows = svc.odoo.search_read(
        "belfry.pass", [["state", "=", "active"], ["keycast_pubkey", "=", key]], ["id"], limit=1
    )
    if rows:
        return True
    from . import sitesessions

    return sitesessions.site_account(svc, key) is not None


def _ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?"
    )


def register(app: FastAPI) -> None:
    @app.get(PATH)
    async def link_preview(request: Request) -> JSONResponse:
        s: Services = request.app.state.services
        auth = request.headers.get("authorization")
        if not auth:
            raise ApiError(401, "unauthorized", "Sign the request (NIP-98).")
        path = request.url.path + ("?" + request.url.query if request.url.query else "")
        key = authenticate(s, request.app.state.api_seen, auth, "GET", path, b"")
        if not await run_in_threadpool(caller_allowed, s, key):
            raise ApiError(403, "no_pass", "This key holds no active pass.")
        if not CALLS.allow("k:" + key):
            raise ApiError(429, "too_many", "Too many previews for now. Try again in a minute.")
        raw = request.query_params.get("url")
        try:
            got = await run_in_threadpool(preview, s, raw)
        except Refused as exc:
            raise ApiError(422, "invalid", "url: an https address on the public internet.") from exc
        except Busy as exc:
            raise ApiError(503, "busy", "Previews are busy just now. Try again.") from exc
        if got is None:
            return JSONResponse(
                {"error": "none", "message": "This page has nothing to show."},
                status_code=404,
                headers={"Cache-Control": f"private, max-age={NONE_SECONDS}"},
            )
        return JSONResponse(got, headers={"Cache-Control": "private, max-age=3600"})

    @app.get(IMAGE_PATH + "/{digest}")
    async def link_preview_image(request: Request, digest: str) -> Response:
        s: Services = request.app.state.services
        if not HASH.fullmatch(digest):
            return Response(status_code=404)
        if not IMAGES.allow("ip:" + _ip(request)):
            return Response(status_code=429, headers={"Retry-After": "60"})
        got = await run_in_threadpool(image, s, digest)
        if got is None:
            return Response(status_code=404, headers={"Cache-Control": "public, max-age=600"})
        ctype, body = got
        return Response(
            body,
            media_type=ctype,
            headers={
                "Cache-Control": f"public, max-age={IMAGE_SECONDS}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                # shown by the chat and the website, other hosts of the belfry
                "Cross-Origin-Resource-Policy": "cross-origin",
            },
        )


__all__ = ["register", "preview", "image", "check_url", "parse", "fetch", "forget"]
