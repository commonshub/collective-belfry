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
- **Messages** (Xavier, 2026-11-21): every line the belfry posts here is folded: a short first
  line, screenshots (API ``screenshots``, else the proposal's / button message's ``imeta``),
  ``---``, the details; preview links are the short ``preview.<domain>/v/<code>`` (``short_code``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import threading
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


def long_preview_url(svc: Services, pubkey: str, name: str = "") -> str:
    """The preview link that names the site: https://preview.<domain>/p/<npub>[@<name>]."""
    return f"https://preview.{domain(svc)}/p/{nostr.hex_to_npub(pubkey)}" + (
        f"@{name}" if name else ""
    )


def preview_url(svc: Services, pubkey: str, name: str = "") -> str:
    """The short preview link the belfry writes: https://preview.<domain>/v/<code> (one code per
    site, kept in the register); the long link when the register can't be written."""
    try:
        return f"https://preview.{domain(svc)}/v/{short_code(svc, pubkey, name)}"
    except Exception as exc:  # noqa: BLE001 — a long link still works
        log.warning("website: no short preview link: %r", exc)
        return long_preview_url(svc, pubkey, name)


# ── short preview links: preview.<domain>/v/<code> ──────────────────────────────────────────
# Xavier, 2026-11-21: "/p/<npub>@<name>" is a long noisy link in #website. A code is 8 lowercase
# base32 characters, made once per (pubkey, site name) and kept in the register param
# ``belfry.preview_links`` ({code: [pubkey hex, name]}); the preview host serves /v/<code>
# exactly like /p/<npub>@<name> (preview below) and parse_ref reads it back.
PREVIEW_LINKS_PARAM = "belfry.preview_links"
CODE = r"[a-z2-7]{8}"
CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
MAX_LINKS = 5000  # the oldest codes go first beyond this
_links_lock = threading.Lock()


def _links(svc: Services) -> dict[str, list[str]]:
    from .webthreads import _param

    try:
        got = json.loads(_param(svc, PREVIEW_LINKS_PARAM) or "{}")
    except ValueError:
        return {}
    if not isinstance(got, dict):
        return {}
    return {
        c: v
        for c, v in got.items()
        if isinstance(c, str)
        and re.fullmatch(CODE, c)
        and isinstance(v, list)
        and len(v) == 2
        and isinstance(v[0], str)
        and re.fullmatch(r"[0-9a-f]{64}", v[0])
        and isinstance(v[1], str)
        and (v[1] == "" or re.fullmatch(NAME, v[1]))
    }


def short_code(svc: Services, pubkey: str, name: str = "") -> str:
    """The code of that site's short preview link, made the first time it is asked for."""
    from .webthreads import _set_param

    with _links_lock:
        links = _links(svc)
        for code, v in links.items():
            if v == [pubkey, name]:
                return code
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            if code not in links:
                break
        links[code] = [pubkey, name]
        while len(links) > MAX_LINKS:
            links.pop(next(iter(links)))
        _set_param(svc, PREVIEW_LINKS_PARAM, json.dumps(links))
        return code


def resolve_code(svc: Services, code: str) -> tuple[str, str] | None:
    """(pubkey hex, site name) of a short preview link's code, or None."""
    if not re.fullmatch(CODE, code or ""):
        return None
    got = _links(svc).get(code)
    return (got[0], got[1]) if got else None


def parse_ref(text: str, dom: str = "", svc: Services | None = None) -> tuple[str, str] | None:
    """The first nsite a text names: nsite:<npub>[/<name>], a preview link
    (preview.<domain>/p/<npub>[@<name>], or with ``svc`` the short preview.<domain>/v/<code>)
    or a gateway link (https://<npub>.<gateway>/). Returns (pubkey hex, site name or "")."""
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
    if svc is not None:
        # a short link, on this belfry's own preview host only
        host = re.escape(f"preview.{dom or domain(svc)}")
        for m in re.finditer(rf"https://{host}/v/({CODE})(?![A-Za-z0-9])", text or ""):
            got = resolve_code(svc, m.group(1))
            if got:
                return got
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


def changelog(svc: Services, text: str, tags: list[list[str]] | None = None) -> None:
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
                    "tags": [["h", CHANNEL], ["client", "belfry-website"], *(tags or [])],
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


def counts(change: Change, scope: list[str] | None, files: int) -> str:
    """'2 files: 1 added, 1 changed, 1 removed, only /ios/screenshots/' (the folded details)."""
    text = (
        f"{_files(files)}: {len(change.added)} added, {len(change.changed)} changed, "
        f"{len(change.removed)} removed"
    )
    return text + (f", only {scope_words(scope)}" if scope else "")


# ── the messages in #website: short, screenshots, then the details folded ────────────────────
# Xavier, 2026-11-21 ("a wall of text with a long noisy preview link"): a line the belfry posts
# about a version is a short first line (``**Website updated** by <admin>: <summary>``), the
# screenshots' URLs (one per line, NIP-92 ``imeta`` tags on the event too), a line with only
# ``---``, then the details (counts, whose version, Live / Preview links). Apps show the part
# before ``---`` with "Show more" for the rest; older apps show it all.
FOLD = "---"
MAX_SHOTS = 6
MAX_SUMMARY = 200
MAX_ALT = 200
IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
SHOT_KEYS = {"url", "alt", "dim", "m"}
DIM = re.compile(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}")
DEFAULT_ALT = "Screenshot of the website"
INVALID_SCREENSHOTS = (
    f"screenshots are at most {MAX_SHOTS} entries {{url, alt?, dim?, m?}}; each url is an image "
    "on this belfry's Blossom server (https://blossom.<domain>/<sha256>.png) or the path of an "
    "image in the version (/ios/screenshots/home.png); m is image/png, image/jpeg, image/webp or "
    "image/gif; dim is <width>x<height>; alt is one line of at most "
    f"{MAX_ALT} characters"
)
INVALID_SUMMARY = f"the summary is one line of at most {MAX_SUMMARY} characters"


class ScreenshotsError(ValueError):
    pass


class SummaryError(ValueError):
    pass


def _one_line(value: Any, most: int) -> str | None:
    """A stripped single line of at most ``most`` characters, None when empty; ValueError else."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("not text")
    v = value.strip()
    if not v:
        return None
    if len(v) > most or any(ord(ch) < 32 or ord(ch) == 127 or ch in "\u2028\u2029" for ch in v):
        raise ValueError("not one short line")
    return v


def clean_summary(value: Any) -> str | None:
    try:
        return _one_line(value, MAX_SUMMARY)
    except ValueError as exc:
        raise SummaryError(INVALID_SUMMARY) from exc


def blossom_base(svc: Services) -> str:
    public = (getattr(svc.settings, "blossom_public_url", "") or "").strip().rstrip("/")
    return public if public.startswith("https://") else f"https://blossom.{domain(svc)}"


def _blossom_hosts(svc: Services) -> set[str]:
    hosts = {f"blossom.{domain(svc)}".lower()}
    m = re.fullmatch(r"https://([a-z0-9.-]+)", blossom_base(svc).lower())
    if m:
        hosts.add(m.group(1))
    return hosts


BLOB_URL = re.compile(r"https://([a-z0-9.-]+)/([0-9a-f]{64})(?:\.([a-z0-9]{1,5}))?")


def _ext(path: str) -> str:
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def screenshot(svc: Services, entry: Any, allow_paths: bool = False) -> dict[str, str]:
    """One screenshot, checked: {url, m, dim, alt} ("" when not known). With ``allow_paths``, a
    path in the version (``/ios/screenshots/home.png``) stays a path, made a Blossom URL by
    ``resolve_screenshots`` once the version's files are known. ScreenshotsError otherwise."""
    if not isinstance(entry, dict) or not set(entry) <= SHOT_KEYS:
        raise ScreenshotsError(INVALID_SCREENSHOTS)
    url = entry.get("url")
    if not isinstance(url, str):
        raise ScreenshotsError(INVALID_SCREENSHOTS)
    url = url.strip()
    m = BLOB_URL.fullmatch(url)
    if m and m.group(1) in _blossom_hosts(svc):
        ext = m.group(3) or ""
    elif allow_paths and url.startswith("/") and not url.endswith("/") and valid_path(url):
        ext = _ext(url)
        if not ext:
            raise ScreenshotsError(INVALID_SCREENSHOTS)
    else:
        raise ScreenshotsError(INVALID_SCREENSHOTS)
    if ext and ext not in IMAGE_TYPES:
        raise ScreenshotsError(INVALID_SCREENSHOTS)
    mime = entry.get("m")
    if mime is not None and mime not in IMAGE_TYPES.values():
        raise ScreenshotsError(INVALID_SCREENSHOTS)
    dim = entry.get("dim")
    if dim is not None and not (isinstance(dim, str) and DIM.fullmatch(dim)):
        raise ScreenshotsError(INVALID_SCREENSHOTS)
    try:
        alt = _one_line(entry.get("alt"), MAX_ALT)
    except ValueError as exc:
        raise ScreenshotsError(INVALID_SCREENSHOTS) from exc
    return {"url": url, "m": mime or IMAGE_TYPES.get(ext, ""), "dim": dim or "", "alt": alt or ""}


def parse_screenshots(
    svc: Services, value: Any, allow_paths: bool = False
) -> list[dict[str, str]] | None:
    """The API's / a caller's ``screenshots`` (None when not given); ScreenshotsError."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_SHOTS:
        raise ScreenshotsError(INVALID_SCREENSHOTS)
    out: list[dict[str, str]] = []
    for entry in value:
        shot = screenshot(svc, entry, allow_paths)
        if shot["url"] not in [s["url"] for s in out]:
            out.append(shot)
    return out


def resolve_screenshots(
    svc: Services, shots: list[dict[str, str]], site: dict[str, str]
) -> list[dict[str, str]]:
    """Paths in the version (``site``: path → sha256, the website after the publish) → the URL of
    that file on the belfry's own Blossom (publish copies every file there)."""
    out = []
    for s in shots:
        if s["url"].startswith("/"):
            sha = site.get(s["url"])
            if not sha:
                raise ScreenshotsError(
                    f"{INVALID_SCREENSHOTS} (a path must be a file of the website once published)"
                )
            s = {**s, "url": f"{blossom_base(svc)}/{sha}.{_ext(s['url'])}"}
        out.append(s)
    return out


def summary_of(event: dict[str, Any] | None) -> str | None:
    """A proposal's (or a button message's) ``["summary", …]`` tag, when it is one short line."""
    try:
        return clean_summary(_tagv(event or {}, "summary"))
    except SummaryError:
        return None


def screenshots_of(svc: Services, event: dict[str, Any] | None) -> list[dict[str, str]]:
    """The screenshots a proposal or message carries: its ``imeta`` tags, then image URLs on the
    belfry's Blossom in its text; at most MAX_SHOTS, anything else left out."""
    out: list[dict[str, str]] = []

    def add(entry: dict[str, Any]) -> None:
        if len(out) >= MAX_SHOTS:
            return
        try:
            shot = screenshot(svc, entry)
        except ScreenshotsError:
            return
        if shot["url"] not in [s["url"] for s in out]:
            out.append(shot)

    for t in (event or {}).get("tags", []):
        if not (isinstance(t, list) and t and t[0] == "imeta"):
            continue
        entry: dict[str, Any] = {}
        for item in t[1:]:
            if isinstance(item, str) and " " in item:
                k, v = item.split(" ", 1)
                if k in SHOT_KEYS and k not in entry:
                    entry[k] = v
        if "url" in entry:
            add(entry)
    for m in BLOB_URL.finditer((event or {}).get("content") or ""):
        if m.group(3) and m.group(3) in IMAGE_TYPES:
            add({"url": m.group(0)})
    return out


def imeta_tag(shot: dict[str, str]) -> list[str]:
    return [
        "imeta",
        f"url {shot['url']}",
        *([f"m {shot['m']}"] if shot.get("m") else []),
        *([f"dim {shot['dim']}"] if shot.get("dim") else []),
        f"alt {shot.get('alt') or DEFAULT_ALT}",
    ]


def folded(
    head: str, summary: str | None, shots: list[dict[str, str]], details: list[str]
) -> tuple[str, list[list[str]]]:
    """(content, imeta tags) of a message in the folded shape (see above)."""
    first = head + (f": {summary}" if summary else "")
    lines = [first, *[s["url"] for s in shots], FOLD, *[d for d in details if d]]
    return "\n".join(lines), [imeta_tag(s) for s in shots]


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
    svc: Services,
    pubkey: str,
    name: str,
    admin: str,
    paths: list[str] | None = None,
    *,
    summary: str | None = None,
    screenshots: list[dict[str, Any]] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy that version as the website; with ``paths`` (a validated scope, parse_paths) only
    those paths of the live website change. Then the "Website updated" line in #website, with
    ``summary`` (one line, ≤ MAX_SUMMARY) and ``screenshots`` (≤ MAX_SHOTS {url, alt?, dim?,
    m?}: Blossom URLs of this belfry, or paths of images in the version) when given, else what
    ``source`` (the proposal's 30023 version or the button's message) carries. A bad summary or
    screenshot raises SummaryError / ScreenshotsError before anything is copied."""
    from . import community

    summary = clean_summary(summary) if summary is not None else summary_of(source)
    shots = parse_screenshots(svc, screenshots, allow_paths=True)
    if shots is None:
        shots = screenshots_of(svc, source)
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
    hashes = {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}
    change = diff(live or {}, hashes, scope)
    keep = change.kept if scope is not None else None
    if scope is not None:
        if len(change.kept) + len(files) > MAX_SITE_FILES:
            raise homepage.HomepageError(
                f"The website would have more than {MAX_SITE_FILES} files."
            )
        if "/index.html" not in files and "/index.html" not in change.kept:
            raise homepage.HomepageError("The website would have no /index.html.")
    shots = resolve_screenshots(svc, shots, {**(keep or {}), **hashes})
    author = (community.names_for(svc, [pubkey]) or {}).get(pubkey) or community.UNKNOWN
    note = f"Website updated by {admin}, from {author}'s version" + (
        f", only {scope_words(scope)}." if scope else "."
    )
    out = await run_in_threadpool(homepage.publish, svc.settings, files, "", admin, note, keep)
    preview = preview_url(svc, pubkey, name)
    text, tags = folded(
        f"**Website updated** by {admin}",
        summary,
        shots,
        [
            f"{counts(change, scope, len(files))}. Version by {author}.",
            f"Live: https://{domain(svc)} · Preview: {preview}",
        ],
    )
    changelog(svc, text, tags=tags)
    return {
        "files": len(files),
        "paths": scope or [],
        "added": len(change.added),
        "changed": len(change.changed),
        "removed": len(change.removed),
        "preview": preview,
        "preview_long": long_preview_url(svc, pubkey, name),
        "summary": summary or "",
        "screenshots": [s["url"] for s in shots],
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
        ref = parse_ref(_tagv(latest, "website"), domain(svc), svc)
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
                "version": latest,
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


CANT = "⚠️ This can't be published"


def _head(change: Change, scope: list[str] | None) -> str:
    if change.none:
        return "**This changes nothing**"
    if scope is None:
        return "**This replaces the whole website**"
    return f"**This changes only {scope_words(scope)}**"


async def describe(svc: Services, ref: tuple[str, str], scope: list[str] | None) -> tuple[str, str]:
    """The note posted under a proposal version: (its short first line, the sentence that says
    what publishing it would change, from the two manifests: hashes only). Raises HomepageError
    when the version's preview can't be read."""
    proposed = await manifest_of(svc, ref[0], ref[1])
    live = await live_paths(svc)
    if scope is not None:
        if live is None:
            return CANT + " yet", (
                f"This can't be published yet: it changes only {scope_words(scope)}, "
                "and there is no website yet. Publish a whole site first."
            )
        change = diff(live, proposed, scope)
        if not any(in_scope(p, scope) for p in proposed):
            return CANT, (
                f"This version has no files in {scope_words(scope)}, so it can't be published."
            )
        merged = {**change.kept, **{p: h for p, h in proposed.items() if in_scope(p, scope)}}
        if "/index.html" not in merged:
            return CANT, "This can't be published: the website would have no /index.html."
        return _head(change, scope), summary(change, scope)
    if "/index.html" not in proposed:
        return CANT, "This version can't be published: it has no /index.html."
    if live is None:
        return "**This would be the first website**", (
            f"This would be the first website: {_files(len(proposed))}."
        )
    change = diff(live, proposed, None)
    return _head(change, None), summary(change, None)


def note_for(svc: Services, p: dict[str, Any], head: str, body: str) -> tuple[str, list[list[str]]]:
    """A proposal version's note in the folded shape: the short line (with the proposal's
    summary when the version can be published), its screenshots, then the details and the
    preview link."""
    ok = not head.startswith("⚠️")
    preview = preview_url(svc, *p["ref"]) if p.get("ref") else ""
    return folded(
        head,
        summary_of(p.get("version")) if ok else None,
        screenshots_of(svc, p.get("version")) if ok else [],
        [body, f"Preview: {preview}" if preview else ""],
    )


def _post(
    svc: Services, group: str, text: str, got: Any = None, tags: list[list[str]] | None = None
) -> None:
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
                    "tags": [["h", group], ["client", "belfry-website"], *(tags or [])],
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
            head, body = "⚠️ This proposal can't be published", f"Not published: {INVALID_PATHS}."
        else:
            try:
                head, body = await describe(svc, p["ref"], p["scope"])
            except Exception as exc:  # noqa: BLE001 — the preview may not be published yet
                young = time.time() - p["version"].get("created_at", 0) < ANNOUNCE_GRACE
                if young:
                    continue
                log.info("website: proposal %s: preview not read: %r", key, exc)
                head, body = "⚠️ " + UNREADABLE, ""
        _add_to_param(svc, ANNOUNCED_PARAM, key)
        text, tags = note_for(svc, p, head, body)
        await run_in_threadpool(_post, svc, p["group"], text, got, tags)
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
            await publish_from(svc, p["ref"][0], p["ref"][1], name, scope, source=p.get("version"))
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
            await run_in_threadpool(parse_ref, str((data or {}).get("ref") or ""), domain(s), s)
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
        try:
            summary = clean_summary(data.get("summary"))
        except SummaryError as exc:
            raise ApiError(422, "invalid_summary", f"Not published: {exc}.") from exc
        try:
            shots = parse_screenshots(s, data.get("screenshots"), allow_paths=True)
        except ScreenshotsError as exc:
            raise ApiError(422, "invalid_screenshots", f"Not published: {exc}.") from exc
        name = (s.odoo.read_one("res.partner", who["partner_id"], ["name"]) or {}).get(
            "name"
        ) or "an admin"
        try:
            out = await publish_from(
                s, ref[0], ref[1], name, scope, summary=summary, screenshots=shots
            )
        except ScreenshotsError as exc:  # a path that isn't a file of the version
            raise ApiError(422, "invalid_screenshots", f"Not published: {exc}.") from exc
        except homepage.HomepageError as exc:
            raise ApiError(409, "not_published", str(exc)) from exc
        hp = getattr(request.app.state, "homepage", None)
        if hp is not None:
            hp._site = None  # serve the new version at once
        return JSONResponse({"ok": True, **out}, headers={"Cache-Control": "no-store"})


def _no_site() -> Response:
    return Response(
        "Open a preview link: https://preview.<domain>/p/<npub> (a site published as an nsite).",
        status_code=404,
        media_type="text/plain",
    )


async def preview(request: Request, svc: Services) -> Response:
    """preview.<domain>: /p/<npub>[@<name>] picks the site (cookie), every other path is that
    site's own (absolute links work)."""
    path = request.url.path
    m = re.fullmatch(rf"/p/({NPUB})(?:@({NAME}))?((?:/[A-Za-z0-9._-]+)*/?)", path)
    chosen_site = (m.group(1) + (f"@{m.group(2)}" if m.group(2) else "")) if m else ""
    land_path = m.group(3) if m else ""
    if not m:
        # the short link: /v/<code>[/<where to land>], exactly like the /p/ link it stands for
        v = re.fullmatch(rf"/v/({CODE})((?:/[A-Za-z0-9._-]+)*/?)", path)
        got = await run_in_threadpool(resolve_code, svc, v.group(1)) if v else None
        if v and got is None:
            return _no_site()
        if got:
            chosen_site = nostr.hex_to_npub(got[0]) + (f"@{got[1]}" if got[1] else "")
            land_path = v.group(2)
    if chosen_site:
        # the link may name where to land (…/p/<npub>@<name>/ios/screenshots/): a proposal for
        # one part of the site has no home page of its own
        land = land_path if land_path and ".." not in land_path else "/"
        resp = RedirectResponse(land, status_code=302)
        resp.set_cookie(
            PREVIEW_COOKIE,
            chosen_site,
            max_age=86400,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return resp
    chosen = request.cookies.get(PREVIEW_COOKIE, "")
    c = re.fullmatch(rf"({NPUB})(?:@({NAME}))?", chosen)
    if not c:
        return _no_site()
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
