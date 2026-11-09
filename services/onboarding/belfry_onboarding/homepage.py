"""The community's homepage on the apex (https://<domain>/): Belfry's start page, another website
behind a proxy, or a static site published as an nsite (NIP-5A) on the community's own relay and
Blossom server.

``HOMEPAGE`` picks it:

- empty: Belfry's start page (how to join), as before;
- ``https://…``: the apex forwards every request there (a site hosted elsewhere: Vercel, Netlify,
  a WordPress …) and passes the answer back; the visitor stays on https://<domain>;
- ``nsite``: the root site (kind 15128) of the belfry's own key (secret ``belfry_nsec``),
  published with ``python -m belfry_onboarding homepage-publish`` (``belfry homepage publish
  <dir>`` on the server);
- ``nsite:<npub>`` or ``nsite:<npub>/<name>``: the root site or the named site (kind 35128,
  ``d`` = name) of another key, published with any nsite tool (nsyte, nsite-cli …).

With a homepage set, the apex keeps Belfry's discovery files (``/.well-known/*``) and the short
names (``/chat`` …, in the proxy), sends the onboarding pages it used to answer to
``pass.<domain>`` (so another site never shares their origin), and gives everything else to the
homepage. ``pass.<domain>/`` then shows the start page itself instead of forwarding to the apex.

Manifests come from ``HOMEPAGE_RELAYS`` (default: the community relay, read with the belfry's
key, which the relay whitelist carries) and are checked against their signature. Blobs come from
the community's Blossom server first, then the manifest's ``server`` tags and the author's
BUD-03 list (kind 10063); every blob is checked against its sha256 and kept in memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes
import posixpath
import tarfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx
from coincurve import PrivateKey
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import nostr
from .blossom import event_id, verify_signature
from .nip46check import sign_event
from .relay import RelayError, RelayPublisher

log = logging.getLogger(__name__)

ROOT_KIND = 15128
NAMED_KIND = 35128
SERVERS_KIND = 10063
BLOSSOM_AUTH_KIND = 24242

MANIFEST_TTL = 60.0  # seconds a manifest is reused before asking the relays again
MANIFEST_STALE = 3600.0  # a known manifest keeps serving this long when the relays fail
BLOB_CACHE_BYTES = 64 * 1024 * 1024
BLOB_MAX_BYTES = 32 * 1024 * 1024

# A default policy for nsite pages: the site's own files, inline code (static site generators
# emit it), images and media from anywhere, connections to any https/wss server (Nostr clients).
NSITE_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline' https:",
        "img-src 'self' https: data: blob:",
        "media-src 'self' https: blob:",
        "font-src 'self' https: data:",
        "connect-src 'self' https: wss:",
        "frame-src https:",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ]
)
# Proxied sites bring their own policy; without one only framing is restricted.
PROXY_DEFAULT_CSP = "frame-ancestors 'self'"

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class HomepageError(RuntimeError):
    pass


@dataclass
class Config:
    mode: str  # "start" | "proxy" | "nsite"
    proxy_url: str = ""
    pubkey: str = ""  # nsite author (hex)
    name: str = ""  # named site's d tag; "" for the root site


def belfry_key(settings: Any) -> PrivateKey | None:
    raw = (getattr(settings, "belfry_nsec", "") or "").strip()
    if not raw:
        return None
    try:
        return PrivateKey(bytes.fromhex(raw))
    except ValueError as exc:
        raise HomepageError("belfry_nsec is not a 64-character hex key") from exc


def belfry_pubkey(settings: Any) -> str:
    """The belfry key's pubkey (hex), or ""; the relay whitelist and the Blossom gate add it."""
    try:
        key = belfry_key(settings)
    except HomepageError:
        log.warning("belfry_nsec is not a hex key; the belfry's key is not whitelisted")
        return ""
    return key.public_key_xonly.format().hex() if key else ""


def parse_config(settings: Any) -> Config:
    value = (getattr(settings, "homepage", "") or "").strip()
    if not value:
        return Config("start")
    if value.startswith(("https://", "http://")):
        return Config("proxy", proxy_url=value.rstrip("/"))
    if value == "nsite":
        pubkey = belfry_pubkey(settings)
        if not pubkey:
            raise HomepageError("HOMEPAGE=nsite needs the belfry's key (infra/secrets/belfry_nsec)")
        return Config("nsite", pubkey=pubkey)
    if value.startswith("nsite:"):
        who, _, name = value[len("nsite:") :].partition("/")
        try:
            pubkey = nostr.to_hex_pubkey(who)
        except ValueError as exc:
            raise HomepageError(f"HOMEPAGE: {who!r} is not an npub or hex pubkey") from exc
        if name and not (
            len(name) <= 13 and name.replace("-", "").isalnum() and name == name.lower()
        ):
            raise HomepageError(f"HOMEPAGE: {name!r} is not a site name (a-z, 0-9, -, at most 13)")
        return Config("nsite", pubkey=pubkey, name=name)
    raise HomepageError(
        f"HOMEPAGE: {value!r} is neither empty, an https:// URL, nsite nor nsite:<npub>"
    )


def aggregate_hash(paths: dict[str, str]) -> str:
    lines = sorted(f"{sha} {path}\n" for path, sha in paths.items())
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def manifest_paths(event: dict[str, Any]) -> dict[str, str]:
    paths: dict[str, str] = {}
    for tag in event.get("tags", []):
        if len(tag) >= 3 and tag[0] == "path" and tag[1].startswith("/") and len(tag[2]) == 64:
            paths[tag[1]] = tag[2].lower()
    return paths


def candidate_paths(path: str) -> list[str]:
    """Where a request path may live in a manifest: the file, or the index of a directory."""
    path = posixpath.normpath("/" + path.lstrip("/")) + (
        "/" if path.endswith("/") and path != "/" else ""
    )
    if path.endswith("/"):
        return [path + "index.html"]
    base = posixpath.basename(path)
    if "." in base:
        return [path]
    return [path, path + "/index.html", path + ".html"]


@dataclass
class Site:
    paths: dict[str, str]
    servers: list[str]
    fetched_at: float
    event_id: str


@dataclass
class Homepage:
    """One per process: the configuration, the manifest cache and the blob cache."""

    settings: Any
    config: Config
    _site: Site | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _blobs: OrderedDict[str, tuple[bytes, str]] = field(default_factory=OrderedDict)
    _blob_bytes: int = 0
    _client: httpx.AsyncClient | None = None
    # previews (website.py) also read manifests from public relays, where anyone publishes
    extra_relays: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.config.mode != "start"

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False
            )
        return self._client

    # ── relays ──────────────────────────────────────────────────────────────────────────────
    def relays(self) -> list[str]:
        extra = (getattr(self.settings, "homepage_relays", "") or "").replace(",", " ").split()
        own = getattr(self.settings, "relay_url", "") or ""
        return [r for r in dict.fromkeys([own, *extra, *self.extra_relays]) if r]

    def signer(self):
        key = belfry_key(self.settings)
        secret = key.secret if key else PrivateKey().secret  # reads elsewhere need no whitelist

        def sign(event: dict[str, Any]) -> dict[str, Any]:
            return sign_event(secret, event)

        return sign

    def _query(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        sign = self.signer()
        for url in self.relays():
            try:
                events += RelayPublisher(url, timeout=8.0).query(filters, sign)
            except (RelayError, OSError, TimeoutError) as exc:
                log.info("homepage: relay %s: %s", url, exc)
        return [e for e in events if event_id(e) == e.get("id") and verify_signature(e)]

    def _resolve(self) -> Site:
        c = self.config
        flt: dict[str, Any] = {
            "authors": [c.pubkey],
            "kinds": [NAMED_KIND if c.name else ROOT_KIND],
        }
        if c.name:
            flt["#d"] = [c.name]
        events = self._query([flt, {"authors": [c.pubkey], "kinds": [SERVERS_KIND]}])
        manifests = [
            e for e in events if e["kind"] in (ROOT_KIND, NAMED_KIND) and e["pubkey"] == c.pubkey
        ]
        if not manifests:
            raise HomepageError("no nsite manifest found on " + ", ".join(self.relays()))
        manifest = max(manifests, key=lambda e: (e["created_at"], e["id"]))
        servers = [getattr(self.settings, "homepage_blossom_url", "") or "http://blossom:3000"]
        servers += [t[1] for t in manifest["tags"] if len(t) > 1 and t[0] == "server"]
        lists = [e for e in events if e["kind"] == SERVERS_KIND and e["pubkey"] == c.pubkey]
        if lists:
            newest = max(lists, key=lambda e: e["created_at"])
            servers += [t[1] for t in newest["tags"] if len(t) > 1 and t[0] == "server"]
        # our own server over http inside Compose; anything else only over https
        servers = [
            s.rstrip("/")
            for s in dict.fromkeys(servers)
            if s.startswith("https://") or s == servers[0]
        ]
        return Site(manifest_paths(manifest), servers, time.monotonic(), manifest["id"])

    async def site(self) -> Site:
        async with self._lock:
            now = time.monotonic()
            if self._site and now - self._site.fetched_at < MANIFEST_TTL:
                return self._site
            try:
                self._site = await run_in_threadpool(self._resolve)
            except HomepageError:
                if self._site and now - self._site.fetched_at < MANIFEST_STALE:
                    log.warning("homepage: relays gave no manifest; serving the last one")
                    return self._site
                raise
            return self._site

    # ── blobs ───────────────────────────────────────────────────────────────────────────────
    async def blob(self, sha: str, servers: list[str]) -> tuple[bytes, str]:
        if sha in self._blobs:
            self._blobs.move_to_end(sha)
            return self._blobs[sha]
        for server in servers:
            try:
                r = await self.client().get(f"{server}/{sha}")
            except httpx.HTTPError as exc:
                log.info("homepage: %s/%s: %s", server, sha, exc)
                continue
            if r.status_code != 200 or len(r.content) > BLOB_MAX_BYTES:
                continue
            if hashlib.sha256(r.content).hexdigest() != sha:
                log.warning("homepage: %s served a blob that does not match %s", server, sha)
                continue
            item = (r.content, r.headers.get("content-type", ""))
            self._blobs[sha] = item
            self._blob_bytes += len(r.content)
            while self._blob_bytes > BLOB_CACHE_BYTES and len(self._blobs) > 1:
                _, (old, _) = self._blobs.popitem(last=False)
                self._blob_bytes -= len(old)
            return item
        raise HomepageError(f"blob {sha} not found on {', '.join(servers)}")

    # ── serving ─────────────────────────────────────────────────────────────────────────────
    async def serve(self, request: Request) -> Response:
        if self.config.mode == "proxy":
            return await self.proxy(request)
        return await self.nsite(request)

    async def nsite(self, request: Request, path: str | None = None) -> Response:
        if request.method not in ("GET", "HEAD"):
            return Response("Method not allowed", status_code=405, headers={"Allow": "GET, HEAD"})
        try:
            site = await self.site()
        except HomepageError as exc:
            log.warning("homepage: %s", exc)
            if self.config.pubkey == belfry_pubkey(self.settings):
                # the belfry's own site, not published yet: the placeholder (console.py)
                from .console import placeholder_html

                return Response(
                    placeholder_html(self.settings),
                    media_type="text/html",
                    headers={"Cache-Control": "no-store"},
                )
            return Response(
                "This site is not published yet.", status_code=503, media_type="text/plain"
            )
        status = 200
        wanted = request.url.path if path is None else path
        path = next((p for p in candidate_paths(wanted) if p in site.paths), None)
        if path is None:
            status, path = 404, "/404.html"
            if path not in site.paths:
                return Response("Not found", status_code=404, media_type="text/plain")
        sha = site.paths[path]
        if status == 200 and request.headers.get("if-none-match") == f'"{sha}"':
            return Response(status_code=304, headers={"ETag": f'"{sha}"'})
        try:
            body, served_type = await self.blob(sha, site.servers)
        except HomepageError as exc:
            log.warning("homepage: %s", exc)
            return Response(
                "A file of this site is missing.", status_code=502, media_type="text/plain"
            )
        guessed = mimetypes.guess_type(path)[0]
        ctype = guessed or (
            served_type
            if served_type and "octet-stream" not in served_type
            else "application/octet-stream"
        )
        if ctype.startswith("text/") or ctype in (
            "application/javascript",
            "application/json",
            "image/svg+xml",
        ):
            ctype += "; charset=utf-8" if "charset" not in ctype else ""
        headers = {
            "ETag": f'"{sha}"',
            "Cache-Control": "public, max-age=60"
            if path.endswith(".html")
            else "public, max-age=300",
            "Content-Security-Policy": NSITE_CSP,
            "X-Content-Type-Options": "nosniff",
        }
        return Response(
            b"" if request.method == "HEAD" else body,
            status_code=status,
            media_type=ctype,
            headers=headers,
        )

    async def proxy(self, request: Request) -> Response:
        upstream = httpx.URL(self.config.proxy_url)
        target = upstream.copy_with(
            path=(upstream.path.rstrip("/") + request.url.path) or "/",
            query=request.url.query.encode() or None,
        )
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["host"] = upstream.netloc.decode()
        site = getattr(self.settings, "site_url", "") or ""
        headers["x-forwarded-host"] = (
            httpx.URL(site).netloc.decode() if site else request.headers.get("host", "")
        )
        headers["x-forwarded-proto"] = "https"
        if request.client:
            headers["x-forwarded-for"] = request.client.host
        body = await request.body()
        req = self.client().build_request(
            request.method, target, headers=headers, content=body or None
        )
        try:
            r = await self.client().send(req, stream=True)
        except httpx.HTTPError as exc:
            log.warning("homepage: proxy to %s: %s", target, exc)
            return Response(
                "The website is not reachable right now.", status_code=502, media_type="text/plain"
            )
        out = {
            k: v
            for k, v in r.headers.multi_items()
            if k.lower() not in HOP_BY_HOP | {"content-encoding"}
        }
        location = r.headers.get("location")
        if location:
            # the upstream's own absolute links back to the visitor's address
            loc = httpx.URL(location)
            if loc.host == upstream.host:
                out["location"] = str(loc.copy_with(scheme=None, host=None, port=None)) or "/"
        if not any(k.lower() == "content-security-policy" for k in out):
            out["content-security-policy"] = PROXY_DEFAULT_CSP
        # a list of pairs keeps repeated headers (Set-Cookie)
        response = StreamingResponse(
            r.aiter_raw()
            if r.headers.get("content-encoding") in (None, "identity")
            else r.aiter_bytes(),
            status_code=r.status_code,
            background=BackgroundTask(r.aclose),
        )
        response.raw_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (list(out.items()) if isinstance(out, dict) else out)
        ]
        return response


# ── publishing (python -m belfry_onboarding homepage-publish) ───────────────────────────────────
def files_from_tar(data: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = [p for p in member.name.split("/") if p not in ("", ".")]
            # no escapes, no dotfiles or dot-directories (.git, .env)
            if not parts or any(p.startswith(".") for p in parts):
                continue
            name = "/" + "/".join(parts)
            f = tar.extractfile(member)
            if f is not None:
                files[name] = f.read()
    return files


def blossom_auth(secret: bytes, verb: str, sha: str) -> str:
    import base64
    import json

    now = int(time.time())
    event = sign_event(
        secret,
        {
            "kind": BLOSSOM_AUTH_KIND,
            "created_at": now,
            "tags": [["t", verb], ["x", sha], ["expiration", str(now + 300)]],
            "content": f"Upload {sha}",
        },
    )
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


def publish(
    settings: Any, files: dict[str, bytes], title: str = "", by: str = "", note: str = ""
) -> dict[str, Any]:
    """Upload every file to the community's Blossom server (through its gate, as the belfry's key)
    and publish the root site manifest (kind 15128) on the community relay."""
    key = belfry_key(settings)
    if key is None:
        raise HomepageError("no belfry key (infra/secrets/belfry_nsec): run gen-secrets.sh")
    if "/index.html" not in files:
        raise HomepageError("the site has no index.html at its root")
    relay_url = getattr(settings, "relay_url", "") or ""
    if not relay_url:
        raise HomepageError("RELAY_URL is not set")
    upload = (getattr(settings, "homepage_upload_url", "") or "http://blossom-gate").rstrip("/")
    public = (getattr(settings, "blossom_public_url", "") or "").rstrip("/")
    paths: dict[str, str] = {}
    uploaded = 0
    with httpx.Client(timeout=60.0) as http:
        for path, body in sorted(files.items()):
            sha = hashlib.sha256(body).hexdigest()
            paths[path] = sha
            if http.head(f"{upload}/{sha}").status_code == 200:
                continue
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            r = http.put(
                f"{upload}/upload",
                content=body,
                headers={
                    "Authorization": blossom_auth(key.secret, "upload", sha),
                    "Content-Type": ctype,
                    "X-SHA-256": sha,
                },
            )
            if r.status_code >= 300:
                raise HomepageError(f"upload of {path} refused: {r.status_code} {r.text[:200]}")
            uploaded += 1
    tags: list[list[str]] = [["path", p, s] for p, s in sorted(paths.items())]
    tags.append(["x", aggregate_hash(paths), "aggregate"])
    if public:
        tags.append(["server", public])
    if title:
        tags.append(["title", title])
    event = sign_event(
        key.secret, {"kind": ROOT_KIND, "created_at": int(time.time()), "tags": tags, "content": ""}
    )
    relay = RelayPublisher(relay_url)
    relay.publish(event, lambda e: sign_event(key.secret, e))
    # a manifest snapshot (NIP-5A kind 5128): the history the changelog shows
    snapshot = sign_event(
        key.secret,
        {
            "kind": 5128,
            "created_at": event["created_at"],
            "content": note or f"Website published from the server by {by or 'an admin'}.",
            "tags": [
                *[t for t in tags if t[0] in ("path", "x")],
                ["a", f"{ROOT_KIND}:{event['pubkey']}:"],
            ],
        },
    )
    relay.publish(snapshot, lambda e: sign_event(key.secret, e))
    return {
        "files": len(paths),
        "uploaded": uploaded,
        "manifest": event["id"],
        "pubkey": event["pubkey"],
        "npub": nostr.hex_to_npub(event["pubkey"]),
    }
