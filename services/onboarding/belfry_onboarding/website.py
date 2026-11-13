"""The website as a conversation: the #website channel, previews, and publishing from the chat.

- ``ensure_channel``: the belfry creates ``#website`` (like ``#general``), described with a link
  to the site and how to propose a version. Anyone chats there; anyone (or their agent, invited
  as a guest) publishes a version as an nsite (NIP-5A) with their own key and shares its preview.
- Previews: ``https://preview.<domain>/p/<npub>[@<name>]`` remembers that site for the browser
  (a cookie) and serves it at the root of ``preview.<domain>``, so the site's own absolute links
  work; a host of its own, apart from the website and the chat. Manifests come from the
  community relay and a few public relays (where agents publish), blobs from the manifest's and
  the author's servers, each checked against its sha256.
- ``POST /api/website/publish`` (NIP-98, the belfry's admins; also ``/api/v1/…`` on
  belfry.<domain>): ``{"ref": "<a message's text or a link>", "message": "<event id>"}``. The
  belfry copies that exact version (every file onto its own Blossom server, a root manifest and
  a snapshot under its key), so the author keeps iterating on their preview and the live site
  only changes when an admin publishes again; the site is served anew at once.
- Every update posts a short changelog in #website: who triggered it, from whose version.
- **Proposals** (#issues, 2026-11-13: "each website update starts as a proposal in the #website
  channel … once the approval is accepted (within the chat interface), it should go to prod"):
  the chat's "Propose as the website" makes a proposal (NIP-23 kind 30023 on the community
  relay, ``t`` = ``website``, ``website`` = the nsite ref) with its 📝 card in #website. ``watch``
  (every minute) publishes the ref of a proposal once its latest status label (kind 1985,
  ``belfry/proposal-status``, in its own chat) is ``passed``, set by someone who is a belfry admin
  now, and no newer version of the proposal came after it. The ref comes from the author's signed
  30023, never from the card; each (proposal, version) goes live once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import homepage, nostr
from .core import Services

log = logging.getLogger(__name__)

CHANNEL = "website"
PUBLIC_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://relay.nsite.lol",
]
PREVIEW_COOKIE = "belfry_preview"
MAX_FILES = 400
MAX_TOTAL = 60_000_000
NAME = r"[a-z0-9-]{1,13}"
NPUB = r"npub1[02-9ac-hj-np-z]{58}"


def domain(svc: Services) -> str:
    return svc.settings.nip05_domain


def preview_url(svc: Services, pubkey: str, name: str = "") -> str:
    return f"https://preview.{domain(svc)}/p/{nostr.hex_to_npub(pubkey)}" + (
        f"@{name}" if name else ""
    )


def parse_ref(text: str, dom: str = "") -> tuple[str, str] | None:
    """The first nsite a text names: nsite:<npub>[/<name>], a preview link
    (preview.<domain>/p/<npub>[@<name>]) or a gateway link (https://<npub>.<gateway>/).
    Returns (pubkey hex, site name or "")."""
    pats = [
        rf"nsite:({NPUB})(?:/({NAME}))?",
        rf"https://preview\.[^/\s]+/p/({NPUB})(?:@({NAME}))?",
        rf"https://({NPUB})\.[a-z0-9.-]+",
    ]
    for pat in pats:
        m = re.search(pat, text or "")
        if m:
            try:
                return nostr.to_hex_pubkey(m.group(1)), (
                    m.group(2) if m.lastindex and m.lastindex > 1 and m.group(2) else ""
                )
            except ValueError:
                continue
    return None


# ── the channel ─────────────────────────────────────────────────────────────────────────────
def about(svc: Services) -> str:
    d = domain(svc)
    return (
        f"Our website, https://{d}: talk about its content and how to improve it. "
        f"To propose a new version, publish it as an nsite with your own key (or invite your agent "
        f"here as a guest to do it), then share its preview link, "
        f"https://preview.{d}/p/<your npub>, with what changed. "
        "An admin long-presses a preview to make it the new version; every update is posted here."
    )


def ensure_channel(svc: Services) -> dict[str, Any]:
    from . import followers

    got = followers._relay(svc)
    if got is None:
        return {}
    relay, sign = got
    if relay.query([{"kinds": [39000], "#d": [CHANNEL]}], sign):
        return {"made": []}
    followers._publish(relay, sign, 9007, [["h", CHANNEL]])
    followers._publish(
        relay, sign, 9002, [["h", CHANNEL], ["name", CHANNEL], ["about", about(svc)], ["open"]]
    )
    log.info("website: made #%s", CHANNEL)
    return {"made": [CHANNEL]}


def changelog(svc: Services, text: str) -> None:
    """A line in #website, posted by the groups key; best effort."""
    from . import followers

    got = followers._relay(svc)
    if got is None:
        return
    relay, sign = got
    try:
        relay.publish(
            sign(
                {
                    "kind": 9,
                    "created_at": int(time.time()),
                    "tags": [["h", CHANNEL], ["client", "belfry-website"]],
                    "content": text,
                }
            ),
            sign,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("website: changelog not posted: %s", exc)


# ── fetching a version ──────────────────────────────────────────────────────────────────────
def site_of(svc: Services, pubkey: str, name: str) -> homepage.Homepage:
    return homepage.Homepage(
        svc.settings,
        homepage.Config("nsite", pubkey=pubkey, name=name),
        extra_relays=list(PUBLIC_RELAYS),
    )


async def files_of(svc: Services, pubkey: str, name: str) -> tuple[dict[str, bytes], str]:
    """Every file of that nsite version (checked against its hash), and its title."""
    hp = site_of(svc, pubkey, name)
    site = await hp.site()
    if "/index.html" not in site.paths:
        raise homepage.HomepageError("That site has no /index.html.")
    if len(site.paths) > MAX_FILES:
        raise homepage.HomepageError(f"That site has more than {MAX_FILES} files.")
    files: dict[str, bytes] = {}
    total = 0
    for path, sha in site.paths.items():
        body, _ = await hp.blob(sha, site.servers)
        total += len(body)
        if total > MAX_TOTAL:
            raise homepage.HomepageError("That site is too large to copy.")
        files[path] = body
    return files, ""


async def publish_from(svc: Services, pubkey: str, name: str, admin: str) -> dict[str, Any]:
    from . import community

    if homepage.parse_config(svc.settings).mode != "nsite":
        raise homepage.HomepageError(
            "The website isn't served from the belfry's nsite (HOMEPAGE): "
            "set it on the setup page first."
        )
    files, _ = await files_of(svc, pubkey, name)
    author = (community.names_for(svc, [pubkey]) or {}).get(pubkey) or nostr.hex_to_npub(pubkey)[
        :16
    ] + "…"
    note = f"Website updated by {admin}, from {author}'s version."
    out = await run_in_threadpool(homepage.publish, svc.settings, files, "", admin, note)
    changelog(
        svc,
        f"**Website updated** by {admin}, from {author}'s version ({len(files)} files): "
        f"https://{domain(svc)} · preview: {preview_url(svc, pubkey, name)}",
    )
    return {
        "files": len(files),
        **{k: v for k, v in (out or {}).items() if k in ("manifest", "uploaded")},
    }


# ── proposals: a passed one goes live ───────────────────────────────────────────────────────
PROPOSAL_NS = "belfry/proposal-status"
PUBLISHED_PARAM = "belfry.website_published"
WATCH_SECONDS = 60


def _tagv(e: dict[str, Any], k: str) -> str:
    return next((t[1] for t in e.get("tags", []) if t and t[0] == k and len(t) > 1), "")


def passed_proposals(svc: Services) -> list[dict[str, Any]]:
    """The website proposals that may go live now: [{key, ref, admin, group, title}]."""
    from . import apps, community, followers
    from .relay import RelayPublisher

    got = followers._relay(svc)
    if got is None or not svc.settings.relay_url:
        return []
    groups, gsign = got
    cards = groups.query([{"kinds": [9], "#h": [CHANNEL], "limit": 1000}], gsign) or []
    cards = [c for c in cards if _tagv(c, "proposal") and _tagv(c, "a").startswith("30023:")]
    if not cards:
        return []
    admins = set(apps.admin_pubkeys(svc))
    main = RelayPublisher(svc.settings.relay_url)
    msign = community.belfry_signer(svc)
    done = set(filter(None, _published(svc)))
    out: list[dict[str, Any]] = []
    for c in cards:
        group, coord = _tagv(c, "proposal"), _tagv(c, "a")
        _k, author, d = (coord.split(":", 2) + ["", ""])[:3]
        if not re.fullmatch(r"[0-9a-f]{64}", author) or not d:
            continue
        versions = main.query([{"kinds": [30023], "authors": [author], "#d": [d]}], msign) or []
        versions = [v for v in versions if v.get("pubkey") == author]
        if not versions:
            continue
        latest = max(versions, key=lambda v: v.get("created_at", 0))
        if ["t", "website"] not in [t[:2] for t in latest.get("tags", [])]:
            continue
        if _tagv(latest, "h") != group:
            continue
        ref = parse_ref(_tagv(latest, "website"), domain(svc))
        if ref is None:
            continue
        key = f"{coord}:{latest['id']}"
        if key in done:
            continue
        labels = groups.query([{"kinds": [1985], "#h": [group], "limit": 500}], gsign) or []
        labels = [
            x
            for x in labels
            if ["L", PROPOSAL_NS] in [t[:2] for t in x.get("tags", [])] and _tagv(x, "a") == coord
        ]
        if not labels:
            continue
        last = max(labels, key=lambda x: x.get("created_at", 0))
        status = next(
            (t[1] for t in last["tags"] if t[:1] == ["l"] and len(t) > 2 and t[2] == PROPOSAL_NS),
            "",
        )
        # passed, by a belfry admin (now), and the proposal not changed since
        if status != "passed" or last.get("pubkey") not in admins:
            continue
        if last.get("created_at", 0) < latest.get("created_at", 0):
            continue
        out.append(
            {
                "key": key,
                "ref": ref,
                "admin": last["pubkey"],
                "group": group,
                "title": _tagv(latest, "title"),
            }
        )
    return out


def _published(svc: Services) -> list[str]:
    from .webthreads import _param

    try:
        return json.loads(_param(svc, PUBLISHED_PARAM) or "[]")
    except ValueError:
        return []


def _mark_published(svc: Services, key: str) -> None:
    from .webthreads import _set_param

    _set_param(svc, PUBLISHED_PARAM, json.dumps([*_published(svc), key][-200:]))


async def watch(svc: Services, app: Any = None) -> int:
    """Publish every website proposal that passed (see the module's docstring)."""
    from . import community, followers

    n = 0
    for p in await run_in_threadpool(passed_proposals, svc):
        _mark_published(svc, p["key"])  # once, even if publishing fails (an admin can redo it)
        name = (community.names_for(svc, [p["admin"]]) or {}).get(p["admin"]) or "an admin"
        try:
            await publish_from(svc, p["ref"][0], p["ref"][1], name)
        except homepage.HomepageError as exc:
            log.warning("website: proposal %s passed but not published: %s", p["key"], exc)
            note = f"This proposal passed, but the website could not be updated: {exc}"
        else:
            n += 1
            note = (
                f"✅ Live now at https://{domain(svc)}: "
                "this proposal passed, so its version is the website."
            )
            if app is not None and getattr(app.state, "homepage", None) is not None:
                app.state.homepage._site = None
        got = followers._relay(svc)
        if got is not None:
            relay, sign = got
            with contextlib.suppress(Exception):
                relay.publish(
                    sign(
                        {
                            "kind": 9,
                            "created_at": int(time.time()),
                            "tags": [["h", p["group"]], ["client", "belfry-website"]],
                            "content": note,
                        }
                    ),
                    sign,
                )
    return n


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    async def loop() -> None:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            try:
                await watch(app.state.services, app)
            except Exception as exc:  # noqa: BLE001
                log.warning("website: watching proposals failed: %r", exc)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


# ── routes ──────────────────────────────────────────────────────────────────────────────────
def register(app: FastAPI) -> None:
    @app.post("/api/website/publish")
    async def api_publish(request: Request) -> JSONResponse:
        from . import console
        from .holder_invites import ApiError, authenticate, holder

        s = request.app.state.services
        body = await request.body()
        pubkey = authenticate(
            s,
            request.app.state.api_seen,
            request.headers.get("authorization"),
            "POST",
            "/api/website/publish",
            body,
        )
        who = await run_in_threadpool(holder, s, pubkey)
        admins = await run_in_threadpool(console.admin_emails, s)
        if not who.get("steward") and (who.get("email") or "").lower() not in admins:
            raise ApiError(403, "not_admin", "Only the belfry's admins publish the website.")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        ref = (
            parse_ref(str((data or {}).get("ref") or ""), domain(s))
            if isinstance(data, dict)
            else None
        )
        if ref is None:
            raise ApiError(
                422, "no_site", "No nsite in that message (nsite:<npub> or a preview link)."
            )
        name = (s.odoo.read_one("res.partner", who["partner_id"], ["name"]) or {}).get(
            "name"
        ) or "an admin"
        try:
            out = await publish_from(s, ref[0], ref[1], name)
        except homepage.HomepageError as exc:
            raise ApiError(409, "not_published", str(exc)) from exc
        hp = getattr(request.app.state, "homepage", None)
        if hp is not None:
            hp._site = None  # serve the new version at once
        return JSONResponse({"ok": True, **out}, headers={"Cache-Control": "no-store"})


async def preview(request: Request, svc: Services) -> Response:
    """preview.<domain>: /p/<npub>[@<name>] picks the site (cookie), every other path is that
    site's own (absolute links work)."""
    path = request.url.path
    m = re.fullmatch(rf"/p/({NPUB})(?:@({NAME}))?/?", path)
    if m:
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(
            PREVIEW_COOKIE,
            m.group(1) + (f"@{m.group(2)}" if m.group(2) else ""),
            max_age=86400,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return resp
    chosen = request.cookies.get(PREVIEW_COOKIE, "")
    c = re.fullmatch(rf"({NPUB})(?:@({NAME}))?", chosen)
    if not c:
        return Response(
            "Open a preview link: https://preview.<domain>/p/<npub> (a site published as "
            "an nsite).",
            status_code=404,
            media_type="text/plain",
        )
    # one Homepage per previewed site, kept: it asks the relays again by itself (MANIFEST_TTL),
    # and making a new one every minute left its HTTP client and connections open for good
    hp = _previews.pop(chosen, None)
    if hp is None:
        hp = site_of(svc, nostr.to_hex_pubkey(c.group(1)), c.group(2) or "")
    _previews[chosen] = hp  # the most recently used last
    while len(_previews) > MAX_PREVIEWS:
        old = _previews.pop(next(iter(_previews)))
        client = getattr(old, "_client", None)
        if client is not None:
            await client.aclose()
    resp = await hp.nsite(request, path)
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


MAX_PREVIEWS = 50
_previews: dict[str, homepage.Homepage] = {}
