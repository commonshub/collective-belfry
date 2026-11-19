"""The website as a conversation: the #website channel, previews, and publishing from the chat.

- ``ensure_channel``: the belfry creates ``#website`` the first time it posts there (``changelog``),
  described in one short paragraph (``ABOUT``: what it's for, propose with a preview link, an admin
  approves with the button under the message, docs/actions.md); ``refresh_about`` (at start-up)
  gives an existing #website that text while its description is still an old default. Anyone
  chats there; anyone (or their agent, invited as a guest) publishes a version as an nsite
  (NIP-5A) with their own key and shares its preview.
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
  30023, never from the card; each (proposal, version, pass) is tried once, so a publish that
  failed is retried by marking it Passed again.
- **Paths** (Xavier, 2026-11-18: agents keep parts of the site, e.g. /ios/screenshots/): a
  proposal, the API (``paths``) or a button's message may say which paths it changes
  (``["path", "<prefix>"]``, "scopes" below); only those change, the rest of the live site is
  kept by hash. Before anyone approves, ``announce`` posts in each proposal version's chat what
  publishing it would change (counts from the two manifests, fixed wording), once per version.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
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
ABOUT = (
    "What goes on our public website and how we want to present ourselves. "
    "To propose a version, post its preview link here with what changed; an admin approves it "
    "with the button under the message."
)
# the defaults #website had before (with {d} = the domain): a channel whose description is still
# one of these gets ABOUT; a description someone wrote is never replaced
OLD_ABOUTS = (
    "Our website, https://{d}: talk about its content and how to improve it. "
    "To propose a new version, publish it as an nsite with your own key (or invite your agent "
    "here as a guest to do it), then share its preview link, "
    "https://preview.{d}/p/<your npub>, with what changed. "
    "An admin long-presses a preview to make it the new version; every update is posted here.",
)


def about(svc: Services) -> str:
    return ABOUT


def refresh_about(svc: Services, relay: Any = None, sign: Any = None) -> bool | None:
    """Give #website the current default description when it still has an old default one.
    None when there is no #website (it is never made here), else whether it changed."""
    from . import followers

    if relay is None:
        got = followers._relay(svc)
        if got is None:
            return None
        relay, sign = got
    found = [
        e
        for e in relay.query([{"kinds": [39000], "#d": [CHANNEL]}], sign) or []
        if _tagv(e, "d") == CHANNEL
    ]
    if not found:
        return None
    latest = max(found, key=lambda e: e.get("created_at", 0))
    if _tagv(latest, "about") not in {t.format(d=domain(svc)) for t in OLD_ABOUTS}:
        return False
    tags = [
        ["about", ABOUT] if t[0] == "about" else list(map(str, t))
        for t in latest.get("tags", [])
        if t and t[0] not in ("d", "-")
    ]
    followers._publish(relay, sign, 9002, [["h", CHANNEL], *tags])
    log.info("website: #%s has the new description", CHANNEL)
    return True


def ensure_channel(svc: Services) -> dict[str, Any]:
    from . import followers

    got = followers._relay(svc)
    if got is None:
        return {}
    relay, sign = got
    if refresh_about(svc, relay, sign) is not None:
        return {"made": []}
    followers._publish(relay, sign, 9007, [["h", CHANNEL]])
    followers._publish(
        relay, sign, 9002, [["h", CHANNEL], ["name", CHANNEL], ["about", about(svc)], ["open"]]
    )
    log.info("website: made #%s", CHANNEL)
    return {"made": [CHANNEL]}


def changelog(svc: Services, text: str) -> None:
    """A line in #website, posted by the groups key; best effort. #website is made then, the first
    time there is something to say (no longer on every round: Xavier, 2026-11-17)."""
    from . import followers

    try:
        ensure_channel(svc)
    except Exception as exc:  # noqa: BLE001 — the line is still worth trying
        log.warning("website: #%s not made: %s", CHANNEL, exc)
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


# ── scopes: a version may change only some paths of the website ─────────────────────────────
# A proposal (its 30023), the publish API (``paths``) or a message with the publish button (its
# own tags) may name what it changes with ``["path", "<prefix>"]``: a folder (``/ios/screenshots/``,
# starts and ends with ``/``) or one file (``/android/index.html``). The new website is then the
# live one, with only those paths replaced by the version's files there (scoped replace: a live
# file there that the version lacks goes away); everything else stays, kept by its hash. No path
# (or ``/``) = the whole site, as before.
MAX_PATHS = 10
MAX_PATH_LEN = 200
MAX_SITE_FILES = 2000  # files in the website after a merge
SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")  # no empty, ".", ".." or dot-prefixed

INVALID_PATHS = (
    "its path tags aren't valid: each one starts with /, names a folder ending with / "
    "(like /ios/screenshots/) or one file (like /android/index.html), "
    f"and there are at most {MAX_PATHS}"
)


class PathsError(ValueError):
    pass


def valid_path(p: Any) -> bool:
    if not isinstance(p, str) or not p.startswith("/") or len(p) > MAX_PATH_LEN:
        return False
    if p == "/":
        return True
    segs = p[1:-1].split("/") if p.endswith("/") else p[1:].split("/")
    if not all(SEGMENT.fullmatch(x) for x in segs):
        return False
    return p.endswith("/") or "." in segs[-1]  # a file has an extension; "/android" is ambiguous


def parse_paths(values: Any) -> list[str] | None:
    """The validated scope (sorted prefixes), or None for the whole site. Raises PathsError."""
    if values is None or values == []:
        return None
    if not isinstance(values, list) or len(values) > MAX_PATHS:
        raise PathsError(INVALID_PATHS)
    if not all(valid_path(v) for v in values):
        raise PathsError(INVALID_PATHS)
    if "/" in values:
        return None
    return sorted(set(values))


def path_tags(event: dict[str, Any]) -> list[Any]:
    return [t[1] if len(t) > 1 else None for t in event.get("tags", []) if t and t[0] == "path"]


def in_scope(path: str, scope: list[str] | None) -> bool:
    if scope is None:
        return True
    return any(path.startswith(p) if p.endswith("/") else path == p for p in scope)


def scope_words(scope: list[str]) -> str:
    return scope[0] if len(scope) == 1 else ", ".join(scope[:-1]) + " and " + scope[-1]


@dataclass
class Change:
    added: list[str]
    changed: list[str]
    removed: list[str]
    kept: dict[str, str]  # the live files outside the scope (path → sha256)

    @property
    def none(self) -> bool:
        return not (self.added or self.changed or self.removed)


def diff(live: dict[str, str], proposed: dict[str, str], scope: list[str] | None) -> Change:
    """What publishing ``proposed`` (path → sha256) with that scope does to ``live``; the
    proposed files outside the scope are ignored."""
    new = {p: h for p, h in proposed.items() if in_scope(p, scope)}
    old = {p: h for p, h in live.items() if in_scope(p, scope)}
    return Change(
        added=sorted(set(new) - set(old)),
        changed=sorted(p for p in new if p in old and old[p] != new[p]),
        removed=sorted(set(old) - set(new)),
        kept={} if scope is None else {p: h for p, h in live.items() if not in_scope(p, scope)},
    )


def _files(n: int) -> str:
    return f"{n} file" + ("" if n == 1 else "s")


def areas(paths: list[str], most: int = 6) -> str:
    """The top-level places those paths are in ("/ and /android/"); names that aren't plain
    words are only counted (they come from someone's manifest)."""
    found: set[str] = set()
    other = 0
    for p in paths:
        segs = p[1:].split("/")
        if len(segs) == 1:
            found.add("/")
        elif SEGMENT.fullmatch(segs[0]):
            found.add(f"/{segs[0]}/")
        else:
            other += 1
    names = sorted(found, key=lambda a: (a != "/", a))
    more = max(0, len(names) - most) + (1 if other else 0)
    names = names[:most]
    if not names:
        return "other places" if other else ""
    if more:
        return ", ".join(names) + f" and {more} more"
    return scope_words(names)


def summary(change: Change, scope: list[str] | None) -> str:
    """One sentence for people, from counts and validated paths only."""
    if scope is None:
        if change.none:
            return "This changes nothing: it is the same as the website now."
        where = areas(change.added + change.changed + change.removed)
        return (
            f"This replaces the whole website: {len(change.changed)} changed, "
            f"{len(change.added)} added, {len(change.removed)} removed, in {where}."
        )
    if change.none:
        return (
            f"This changes nothing: the files in {scope_words(scope)} "
            "are the same as on the website now."
        )
    return (
        f"This changes only {scope_words(scope)}: {_files(len(change.added))} added, "
        f"{len(change.changed)} changed, {len(change.removed)} removed. "
        "The rest of the website stays as it is."
    )


def short_summary(change: Change, scope: list[str] | None, files: int) -> str:
    counts = (
        f"{len(change.added)} added, {len(change.changed)} changed, {len(change.removed)} removed"
    )
    if scope is None:
        return f"{_files(files)}; {counts}"
    return f"only {scope_words(scope)} ({_files(files)}: {counts})"


# ── fetching a version ──────────────────────────────────────────────────────────────────────
def site_of(svc: Services, pubkey: str, name: str) -> homepage.Homepage:
    return homepage.Homepage(
        svc.settings,
        homepage.Config("nsite", pubkey=pubkey, name=name),
        extra_relays=list(PUBLIC_RELAYS),
    )


async def _close(hp: homepage.Homepage) -> None:
    client = getattr(hp, "_client", None)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.aclose()


async def manifest_of(svc: Services, pubkey: str, name: str) -> dict[str, str]:
    """That version's manifest (path → sha256), nothing downloaded."""
    hp = site_of(svc, pubkey, name)
    try:
        return dict((await hp.site()).paths)
    finally:
        await _close(hp)


async def live_paths(svc: Services) -> dict[str, str] | None:
    """The website a publish replaces: the belfry's own root site (path → sha256), or None when
    there is none (or the relays don't say)."""
    pubkey = homepage.belfry_pubkey(svc.settings)
    if not pubkey:
        return None
    hp = homepage.Homepage(svc.settings, homepage.Config("nsite", pubkey=pubkey))
    try:
        return dict((await hp.site()).paths)
    except homepage.HomepageError:
        return None
    finally:
        await _close(hp)


async def files_of(
    svc: Services, pubkey: str, name: str, scope: list[str] | None = None
) -> tuple[dict[str, bytes], str]:
    """Every file of that nsite version (checked against its hash), and its title. With a
    scope, only its files there (it needs no /index.html of its own)."""
    hp = site_of(svc, pubkey, name)
    try:
        site = await hp.site()
        wanted = {p: h for p, h in site.paths.items() if in_scope(p, scope)}
        if scope is None and "/index.html" not in wanted:
            raise homepage.HomepageError("That site has no /index.html.")
        if scope is not None and not wanted:
            raise homepage.HomepageError(f"That version has no files in {scope_words(scope)}.")
        if len(wanted) > MAX_FILES:
            raise homepage.HomepageError(f"That site has more than {MAX_FILES} files.")
        files: dict[str, bytes] = {}
        total = 0
        for path, sha in wanted.items():
            body, _ = await hp.blob(sha, site.servers)
            total += len(body)
            if total > MAX_TOTAL:
                raise homepage.HomepageError("That site is too large to copy.")
            files[path] = body
        return files, ""
    finally:
        await _close(hp)


async def publish_from(
    svc: Services, pubkey: str, name: str, admin: str, paths: list[str] | None = None
) -> dict[str, Any]:
    """Copy that version as the website; with ``paths`` (a validated scope, parse_paths) only
    those paths of the live website change."""
    from . import community

    if homepage.parse_config(svc.settings).mode != "nsite":
        raise homepage.HomepageError(
            "The website isn't served from the belfry's nsite (HOMEPAGE): "
            "set it on the setup page first."
        )
    scope = paths or None
    live = await live_paths(svc)
    if scope is not None and live is None:
        raise homepage.HomepageError(
            "There is no website yet to change only part of: publish a whole site first."
        )
    files, _ = await files_of(svc, pubkey, name, scope)
    change = diff(live or {}, {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}, scope)
    keep = change.kept if scope is not None else None
    if scope is not None:
        if len(change.kept) + len(files) > MAX_SITE_FILES:
            raise homepage.HomepageError(
                f"The website would have more than {MAX_SITE_FILES} files."
            )
        if "/index.html" not in files and "/index.html" not in change.kept:
            raise homepage.HomepageError("The website would have no /index.html.")
    author = (community.names_for(svc, [pubkey]) or {}).get(pubkey) or nostr.hex_to_npub(pubkey)[
        :16
    ] + "…"
    note = f"Website updated by {admin}, from {author}'s version" + (
        f", only {scope_words(scope)}." if scope else "."
    )
    out = await run_in_threadpool(homepage.publish, svc.settings, files, "", admin, note, keep)
    changelog(
        svc,
        f"**Website updated** by {admin}, from {author}'s version "
        f"({short_summary(change, scope, len(files))}): "
        f"https://{domain(svc)} · preview: {preview_url(svc, pubkey, name)}",
    )
    return {
        "files": len(files),
        "paths": scope or [],
        "added": len(change.added),
        "changed": len(change.changed),
        "removed": len(change.removed),
        **{k: v for k, v in (out or {}).items() if k in ("manifest", "uploaded")},
    }


# ── proposals: a passed one goes live ───────────────────────────────────────────────────────
PROPOSAL_NS = "belfry/proposal-status"
PUBLISHED_PARAM = "belfry.website_published"
WATCH_SECONDS = 60


def _tagv(e: dict[str, Any], k: str) -> str:
    return next((t[1] for t in e.get("tags", []) if t and t[0] == k and len(t) > 1), "")


def proposal_versions(svc: Services) -> tuple[list[dict[str, Any]], Any]:
    """The latest version of every website proposal carded in #website:
    ([{coord, version, group, ref, scope, bad_paths, title}], (groups relay, signer) or None)."""
    from . import community, followers
    from .relay import RelayPublisher

    got = followers._relay(svc)
    if got is None or not svc.settings.relay_url:
        return [], None
    groups, gsign = got
    cards = groups.query([{"kinds": [9], "#h": [CHANNEL], "limit": 1000}], gsign) or []
    cards = [c for c in cards if _tagv(c, "proposal") and _tagv(c, "a").startswith("30023:")]
    if not cards:
        return [], got
    main = RelayPublisher(svc.settings.relay_url)
    msign = community.belfry_signer(svc)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in cards:
        group, coord = _tagv(c, "proposal"), _tagv(c, "a")
        if coord in seen:
            continue
        seen.add(coord)
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
        try:
            scope, bad = parse_paths(path_tags(latest)), False
        except PathsError:
            scope, bad = None, True
        out.append(
            {
                "coord": coord,
                "version": latest,
                "group": group,
                "ref": ref,
                "scope": scope,
                "bad_paths": bad,
                "title": _tagv(latest, "title"),
            }
        )
    return out, got


def passed_proposals(svc: Services) -> list[dict[str, Any]]:
    """The website proposals that may go live now:
    [{key, ref, admin, group, title, scope, bad_paths}]."""
    from . import apps

    found, got = proposal_versions(svc)
    if not found:
        return []
    groups, gsign = got
    admins = set(apps.admin_pubkeys(svc))
    done = set(filter(None, _published(svc)))
    out: list[dict[str, Any]] = []
    for p in found:
        group, coord, latest = p["group"], p["coord"], p["version"]
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
        # once per (version, pass): marking it Passed again retries a publish that failed
        key = f"{coord}:{latest['id']}:{last.get('id', '')}"
        if key in done:
            continue
        out.append(
            {
                "key": key,
                "ref": p["ref"],
                "admin": last["pubkey"],
                "group": group,
                "title": p["title"],
                "scope": p["scope"],
                "bad_paths": p["bad_paths"],
            }
        )
    return out


def _published(svc: Services) -> list[str]:
    return _list_param(svc, PUBLISHED_PARAM)


def _mark_published(svc: Services, key: str) -> None:
    _add_to_param(svc, PUBLISHED_PARAM, key)


def _list_param(svc: Services, name: str) -> list[str]:
    from .webthreads import _param

    try:
        return json.loads(_param(svc, name) or "[]")
    except ValueError:
        return []


def _add_to_param(svc: Services, name: str, key: str) -> None:
    from .webthreads import _set_param

    _set_param(svc, name, json.dumps([*_list_param(svc, name), key][-200:]))


# ── before anyone approves: what a version would change ─────────────────────────────────────
ANNOUNCED_PARAM = "belfry.website_announced"
ANNOUNCE_GRACE = 600  # seconds a version's preview may stay unreadable before we say so
ANNOUNCE_MAX_AGE = 3 * 86400  # older versions (from before these notes) are never described
UNREADABLE = "I couldn't read this version's preview, so I can't say what it would change."


async def describe(svc: Services, ref: tuple[str, str], scope: list[str] | None) -> str:
    """The note posted under a proposal version: what publishing it would change, from the two
    manifests (hashes only). Raises HomepageError when the version's preview can't be read."""
    proposed = await manifest_of(svc, ref[0], ref[1])
    live = await live_paths(svc)
    if scope is not None:
        if live is None:
            return (
                f"This can't be published yet: it changes only {scope_words(scope)}, "
                "and there is no website yet. Publish a whole site first."
            )
        change = diff(live, proposed, scope)
        if not any(in_scope(p, scope) for p in proposed):
            return f"This version has no files in {scope_words(scope)}, so it can't be published."
        merged = {**change.kept, **{p: h for p, h in proposed.items() if in_scope(p, scope)}}
        if "/index.html" not in merged:
            return "This can't be published: the website would have no /index.html."
        return summary(change, scope)
    if "/index.html" not in proposed:
        return "This version can't be published: it has no /index.html."
    if live is None:
        return f"This would be the first website: {_files(len(proposed))}."
    return summary(diff(live, proposed, None), None)


def _post(svc: Services, group: str, text: str, got: Any = None) -> None:
    from . import followers

    got = got or followers._relay(svc)
    if got is None:
        return
    relay, sign = got
    with contextlib.suppress(Exception):
        relay.publish(
            sign(
                {
                    "kind": 9,
                    "created_at": int(time.time()),
                    "tags": [["h", group], ["client", "belfry-website"]],
                    "content": text,
                }
            ),
            sign,
        )


async def announce(svc: Services, found: list[dict[str, Any]] | None = None) -> int:
    """Post, once per proposal version, what publishing it would change (``describe``)."""
    got = None
    if found is None:
        found, got = await run_in_threadpool(proposal_versions, svc)
    done = set(_list_param(svc, ANNOUNCED_PARAM))
    published = _published(svc)
    n = 0
    for p in found:
        key = f"{p['coord']}:{p['version'].get('id', '')}"
        if key in done:
            continue
        old = time.time() - p["version"].get("created_at", 0) > ANNOUNCE_MAX_AGE
        if old or any(k.startswith(key + ":") for k in published):
            _add_to_param(svc, ANNOUNCED_PARAM, key)  # from before these notes: not now
            continue
        if p["bad_paths"]:
            text = f"⚠️ This proposal can't be published: {INVALID_PATHS}."
        else:
            try:
                text = await describe(svc, p["ref"], p["scope"])
            except Exception as exc:  # noqa: BLE001 — the preview may not be published yet
                young = time.time() - p["version"].get("created_at", 0) < ANNOUNCE_GRACE
                if young:
                    continue
                log.info("website: proposal %s: preview not read: %r", key, exc)
                text = UNREADABLE
        _add_to_param(svc, ANNOUNCED_PARAM, key)
        await run_in_threadpool(_post, svc, p["group"], text, got)
        n += 1
    return n


async def watch(svc: Services, app: Any = None) -> int:
    """Say what each new proposal version would change, then publish every website proposal
    that passed (see the module's docstring)."""
    from . import community

    try:
        await announce(svc)
    except Exception as exc:  # noqa: BLE001 — publishing still goes on
        log.warning("website: announcing proposals failed: %r", exc)
    n = 0
    for p in await run_in_threadpool(passed_proposals, svc):
        _mark_published(svc, p["key"])  # once, even if publishing fails (an admin can redo it)
        name = (community.names_for(svc, [p["admin"]]) or {}).get(p["admin"]) or "an admin"
        scope = p.get("scope")
        try:
            if p.get("bad_paths"):
                raise homepage.HomepageError(INVALID_PATHS)
            await publish_from(svc, p["ref"][0], p["ref"][1], name, scope)
        except Exception as exc:  # noqa: BLE001 — said in the proposal's chat, retried on a new pass
            log.warning("website: proposal %s passed but not published: %r", p["key"], exc)
            why = (
                str(exc)
                if isinstance(exc, homepage.HomepageError)
                else "the files could not be copied"
            )
            note = (
                f"⚠️ Couldn't publish this version ({why}). "
                "An admin can mark it Passed again to retry."
            )
        else:
            n += 1
            note = f"✅ Live now at https://{domain(svc)}: this proposal passed, so " + (
                f"{scope_words(scope)} on the website is its version."
                if scope
                else "its version is the website."
            )
            if app is not None and getattr(app.state, "homepage", None) is not None:
                app.state.homepage._site = None
        await run_in_threadpool(_post, svc, p["group"], note)
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
        try:
            scope = parse_paths(data.get("paths"))
        except PathsError as exc:
            raise ApiError(422, "invalid_paths", f"Not published: {exc}.") from exc
        name = (s.odoo.read_one("res.partner", who["partner_id"], ["name"]) or {}).get(
            "name"
        ) or "an admin"
        try:
            out = await publish_from(s, ref[0], ref[1], name, scope)
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
    m = re.fullmatch(rf"/p/({NPUB})(?:@({NAME}))?((?:/[A-Za-z0-9._-]+)*/?)", path)
    if m:
        # the link may name where to land (…/p/<npub>@<name>/ios/screenshots/): a proposal for
        # one part of the site has no home page of its own
        land = m.group(3) if m.group(3) and ".." not in m.group(3) else "/"
        resp = RedirectResponse(land, status_code=302)
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
    if resp.status_code == 404:
        # a site for one part of the website (a proposal with a path): it has no page here.
        # At the root, go to its first page; elsewhere show the live site's page, so the
        # preview reads as the website would after approval.
        try:
            own = {"/" + k.lstrip("/") for k in (await hp.site()).paths}
        except Exception:  # noqa: BLE001 — the 404 stands
            own = set()
        if own and "/index.html" not in own:
            if path in ("", "/"):
                pages = sorted(
                    (k[: -len("index.html")] for k in own if k.endswith("/index.html")),
                    key=lambda k: (k.count("/"), k),
                )
                if pages:
                    return RedirectResponse(pages[0], status_code=302)
            live = getattr(request.app.state, "homepage", None)
            if live is not None and live.enabled and live is not hp:
                try:
                    shown = await live.nsite(request, path)
                except Exception:  # noqa: BLE001
                    shown = None
                if shown is not None and shown.status_code == 200:
                    resp = shown
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


MAX_PREVIEWS = 50
_previews: dict[str, homepage.Homepage] = {}
