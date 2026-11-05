"""``/git``: the community's code, as its maintainers last published it with ngit (NIP-34).

GIT_REPO says which repository (``belfry config``: BELFRY_GIT_REPO; empty = no /git, and the path
falls through to the homepage):

- ``nostr://<npub>/<relay>/<identifier>``: the clone URL ngit prints. The page reads the
  repository announcement (kind 30617: name, description, clone URLs, maintainers) and the
  maintainers' signed state (kind 30618: branch tips) from the relay in the URL, the announced
  relays and this belfry's relay, then fetches those commits from a clone URL (a GRASP server such
  as relay.ngit.dev, or git.<domain>) and checks that the code shown is the signed state;
- ``https://…`` (a plain git URL): the code only, no Nostr.

Pages (public, on the apex and on pass.<domain>): ``/git`` (README, files, latest commit, how to
clone and propose changes), ``/git/tree/<path>``, ``/git/blob/<path>``, ``/git/commits``.
A shallow copy (``--depth 50``) lives in /tmp and is refreshed at most every five minutes.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from . import nostr, pages
from .blossom import verify_signature
from .claim import page
from .relay import RelayError, RelayPublisher

log = logging.getLogger(__name__)
E = html.escape

ANNOUNCE_KIND = 30617
STATE_KIND = 30618
REFRESH_SECONDS = 300.0
DEPTH = 50
MAX_BLOB = 512 * 1024
GIT_TIMEOUT = 90
CACHE_ROOT = Path("/tmp/belfry-git")
CLONE_SCHEMES = ("https://",)  # tests add file://


class GitViewError(RuntimeError):
    pass


@dataclass
class Repo:
    raw: str
    pubkey: str = ""
    relay: str = ""
    identifier: str = ""
    url: str = ""  # plain git URL mode

    @property
    def nostr_url(self) -> str:
        if not self.pubkey:
            return ""
        host = self.relay.removeprefix("wss://").removeprefix("ws://")
        return f"nostr://{nostr.hex_to_npub(self.pubkey)}/{host}/{self.identifier}"

    @property
    def naddr(self) -> str:
        if not self.pubkey:
            return ""
        tlv = b""
        tlv += bytes([0, len(self.identifier.encode())]) + self.identifier.encode()
        if self.relay:
            r = self.relay.encode()
            tlv += bytes([1, len(r)]) + r
        tlv += bytes([2, 32]) + bytes.fromhex(self.pubkey)
        tlv += bytes([3, 4]) + ANNOUNCE_KIND.to_bytes(4, "big")
        return nostr.bech32_encode("naddr", tlv)


def parse(value: str) -> Repo | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("nostr://"):
        parts = value.removeprefix("nostr://").strip("/").split("/")
        if len(parts) not in (2, 3):
            raise GitViewError("GIT_REPO: nostr://<npub>/<relay>/<identifier>")
        try:
            pubkey = nostr.to_hex_pubkey(parts[0])
        except ValueError as exc:
            raise GitViewError(f"GIT_REPO: {exc}") from exc
        relay = ""
        if len(parts) == 3:
            relay = parts[1] if parts[1].startswith(("ws://", "wss://")) else f"wss://{parts[1]}"
        ident = parts[-1].removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", ident):
            raise GitViewError("GIT_REPO: bad repository identifier")
        return Repo(raw=value, pubkey=pubkey, relay=relay, identifier=ident)
    if value.startswith(CLONE_SCHEMES):
        return Repo(raw=value, url=value)
    raise GitViewError("GIT_REPO: a nostr:// (ngit) or https:// git URL")


def tag(e: dict[str, Any], name: str) -> list[str]:
    return next((t[1:] for t in e.get("tags", []) if t and t[0] == name), [])


@dataclass
class Info:
    name: str = ""
    description: str = ""
    web: list[str] = field(default_factory=list)
    clone: list[str] = field(default_factory=list)
    maintainers: list[str] = field(default_factory=list)
    refs: dict[str, str] = field(default_factory=dict)  # signed state: ref → sha
    head: str = ""  # e.g. refs/heads/main
    state_at: int = 0


def nostr_info(repo: Repo, extra_relays: list[str], sign: Any) -> Info:
    relays = [r for r in [repo.relay, *extra_relays] if r]
    seen: set[str] = set()

    def fetch(filters: list[dict[str, Any]], urls: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for url in urls:
            if url in seen and filters[0].get("kinds") == [ANNOUNCE_KIND]:
                continue
            try:
                out += RelayPublisher(url, timeout=8.0).query(filters, sign)
            except (RelayError, OSError, TimeoutError) as exc:
                log.info("git: relay %s: %s", url, exc)
        return [e for e in out if verify_signature(e)]

    announcements = [
        e
        for e in fetch(
            [{"kinds": [ANNOUNCE_KIND], "authors": [repo.pubkey], "#d": [repo.identifier]}], relays
        )
        if e["pubkey"] == repo.pubkey
    ]
    if not announcements:
        raise GitViewError("no repository announcement found on the relays")
    ann = max(announcements, key=lambda e: e["created_at"])
    info = Info(
        name=(tag(ann, "name") or [repo.identifier])[0],
        description=(tag(ann, "description") or [""])[0],
        web=tag(ann, "web"),
        clone=[u for u in tag(ann, "clone") if u.startswith(CLONE_SCHEMES)],
        maintainers=[repo.pubkey]
        + [m for m in tag(ann, "maintainers") if re.fullmatch(r"[0-9a-f]{64}", m)],
    )
    more = [r for r in tag(ann, "relays") if r.startswith(("wss://", "ws://"))]
    states = [
        e
        for e in fetch(
            [{"kinds": [STATE_KIND], "authors": info.maintainers, "#d": [repo.identifier]}],
            list(dict.fromkeys(relays + more)),
        )
        if e["pubkey"] in info.maintainers
    ]
    if states:
        st = max(states, key=lambda e: e["created_at"])
        info.state_at = st["created_at"]
        for t in st.get("tags", []):
            if len(t) >= 2 and t[0].startswith("refs/") and re.fullmatch(r"[0-9a-f]{40}", t[1]):
                info.refs[t[0]] = t[1]
            elif len(t) >= 2 and t[0] == "HEAD" and t[1].startswith("ref: "):
                info.head = t[1].removeprefix("ref: ")
    return info


def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=off", "-c", "protocol.file.allow=user", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        env={"GIT_TERMINAL_PROMPT": "0", "HOME": "/tmp", "PATH": "/usr/bin:/bin"},
    )
    if check and r.returncode != 0:
        raise GitViewError((r.stderr or r.stdout).strip()[-300:] or "git failed")
    return r.stdout


@dataclass
class Snapshot:
    repo: Repo
    info: Info
    path: Path
    branch: str
    sha: str
    verified: bool  # the sha is the maintainers' signed state for that branch
    source: str
    fetched_at: float


class GitView:
    def __init__(self, settings: Any, repo: Repo):
        self.settings = settings
        self.repo = repo
        self._snap: Snapshot | None = None
        self._lock = threading.Lock()
        self.error = ""

    def signer(self) -> Any:
        """Signs NIP-42 AUTH for relays that ask (this belfry's relay whitelists its key)."""
        from coincurve import PrivateKey

        from . import homepage
        from .nip46check import sign_event

        try:
            key = homepage.belfry_key(self.settings)
        except homepage.HomepageError:
            key = None
        secret = key.secret if key else PrivateKey().secret
        return lambda e: sign_event(secret, e)

    def snapshot(self) -> Snapshot:
        snap = self._snap
        if snap and time.monotonic() - snap.fetched_at < REFRESH_SECONDS:
            return snap
        with self._lock:
            snap = self._snap
            if snap and time.monotonic() - snap.fetched_at < REFRESH_SECONDS:
                return snap
            try:
                self._snap = self._refresh()
                self.error = ""
            except (GitViewError, subprocess.TimeoutExpired, OSError) as exc:
                self.error = str(exc)
                log.warning("git: refresh failed: %s", exc)
                if self._snap is None:
                    raise GitViewError(str(exc)) from exc
                self._snap.fetched_at = time.monotonic() - REFRESH_SECONDS + 60  # retry in 1 min
            return self._snap

    def _refresh(self) -> Snapshot:
        repo = self.repo
        if repo.pubkey:
            extra = [self.settings.relay_url] if getattr(self.settings, "relay_url", "") else []
            info = nostr_info(repo, extra, self.signer())
        else:
            info = Info(name=Path(urllib.parse.urlparse(repo.url).path).stem, clone=[repo.url])
        if not info.clone:
            raise GitViewError("the announcement names no https clone URL")
        path = CACHE_ROOT / hashlib.sha256(repo.raw.encode()).hexdigest()[:16]
        if not (path / "HEAD").is_file():
            path.mkdir(parents=True, exist_ok=True)
            git(path, "init", "--bare", "-q")
        branch = (info.head or "").removeprefix("refs/heads/")
        if not branch:
            branch = next(
                (b for b in ("main", "master") if f"refs/heads/{b}" in info.refs),
                "",
            ) or next(
                (r.removeprefix("refs/heads/") for r in info.refs if r.startswith("refs/heads/")),
                "",
            )
        want = info.refs.get(f"refs/heads/{branch}", "") if branch else ""
        last = ""
        for url in info.clone:
            try:
                if branch:
                    git(
                        path,
                        "fetch",
                        "-q",
                        "--depth",
                        str(DEPTH),
                        "--no-tags",
                        url,
                        f"+refs/heads/{branch}:refs/heads/{branch}",
                    )
                else:
                    out = git(path, "ls-remote", "--symref", url, "HEAD")
                    m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
                    branch = m.group(1) if m else "main"
                    git(
                        path,
                        "fetch",
                        "-q",
                        "--depth",
                        str(DEPTH),
                        "--no-tags",
                        url,
                        f"+refs/heads/{branch}:refs/heads/{branch}",
                    )
                sha = git(path, "rev-parse", f"refs/heads/{branch}").strip()
                if want and sha != want:
                    # the server is behind or ahead of the signed state: show the signed commit
                    # when the server has it
                    try:
                        git(path, "fetch", "-q", "--depth", str(DEPTH), url, want)
                        sha = want
                    except GitViewError:
                        last = f"{url} does not have the signed state {want[:12]}"
                        continue
                return Snapshot(
                    repo=repo,
                    info=info,
                    path=path,
                    branch=branch,
                    sha=sha,
                    verified=bool(want) and sha == want,
                    source=url,
                    fetched_at=time.monotonic(),
                )
            except GitViewError as exc:
                last = f"{url}: {exc}"
                log.info("git: %s", last)
        raise GitViewError(last or "no clone URL answered")

    # ── reading ──
    def tree(self, snap: Snapshot, rel: str) -> list[tuple[str, str, str]]:
        """(type, name, path) of a folder, folders first."""
        spec = f"{snap.sha}:{rel}" if rel else snap.sha
        out = git(snap.path, "ls-tree", "-z", spec)
        items = []
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, name = entry.split("\t", 1)
            kind = meta.split()[1]
            items.append((kind, name, f"{rel}/{name}" if rel else name))
        return sorted(items, key=lambda i: (i[0] != "tree", i[1].lower()))

    def kind(self, snap: Snapshot, rel: str) -> str:
        return git(snap.path, "cat-file", "-t", f"{snap.sha}:{rel}", check=False).strip()

    def blob(self, snap: Snapshot, rel: str) -> bytes:
        size = int(git(snap.path, "cat-file", "-s", f"{snap.sha}:{rel}").strip() or 0)
        if size > MAX_BLOB:
            raise GitViewError(f"the file is {size // 1024} KB; too big to show here")
        r = subprocess.run(
            ["git", "cat-file", "blob", f"{snap.sha}:{rel}"],
            cwd=snap.path,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        if r.returncode != 0:
            raise GitViewError("no such file")
        return r.stdout

    def commits(self, snap: Snapshot, n: int = 30) -> list[dict[str, str]]:
        out = git(snap.path, "log", f"-{n}", "--format=%H%x1f%an%x1f%aI%x1f%s", snap.sha)
        rows = []
        for ln in out.splitlines():
            parts = ln.split("\x1f")
            if len(parts) == 4:
                rows.append(dict(zip(("sha", "author", "at", "subject"), parts, strict=True)))
        return rows


def make(settings: Any) -> GitView | None:
    try:
        repo = parse(getattr(settings, "git_repo", ""))
    except GitViewError as exc:
        log.error("git: %s", exc)
        return None
    return GitView(settings, repo) if repo else None


# ── pages ───────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
.crumbs{font-family:var(--font-mono);font-size:.9rem;margin:0 0 10px;word-break:break-all}
.files{list-style:none;padding:0;margin:0;border:1px solid var(--border);border-radius:10px;overflow:hidden}
.files li{border-top:1px solid var(--border);padding:7px 12px;font-family:var(--font-mono);font-size:.9rem}
.files li:first-child{border-top:0} .files a{text-decoration:none}
pre.code{background:var(--surface-deep);padding:12px 14px;border-radius:10px;overflow-x:auto;font:.84rem/1.5 var(--font-mono);margin:0}
.readme{border-top:1px solid var(--border);margin-top:18px;padding-top:12px}
.meta{font-size:.85rem;color:var(--fg-muted)} .commit{border-top:1px solid var(--border);padding:8px 0}
.clone code{display:block;margin:4px 0;padding:6px 10px}
</style>"""


def ago(iso: str) -> str:
    return iso[:10]


def header(snap: Snapshot, rel: str = "") -> str:
    info = snap.info
    crumbs = '<a href="/git">' + E(info.name) + "</a>"
    acc = ""
    for part in [p for p in rel.split("/") if p]:
        acc = f"{acc}/{part}" if acc else part
        crumbs += f' / <a href="/git/tree/{urllib.parse.quote(acc)}">{E(part)}</a>'
    badge = (
        '<span class="ok" title="matches the maintainers\' signed state (kind 30618)">signed state</span>'
        if snap.verified
        else '<span class="meta">unsigned</span>'
    )
    return (
        f'<p class="crumbs">{crumbs}</p><p class="meta">{E(snap.branch)} · '
        f'<code>{snap.sha[:12]}</code> · {badge} · <a href="/git/commits">commits</a></p>'
    )


def home_html(view: GitView, snap: Snapshot) -> str:
    info, repo = snap.info, snap.repo
    files = "".join(
        f'<li>{"📁" if k == "tree" else "📄"} <a href="/git/{"tree" if k == "tree" else "blob"}/'
        f'{urllib.parse.quote(p)}">{E(n)}</a></li>'
        for k, n, p in view.tree(snap, "")
    )
    commit = (view.commits(snap, 1) or [{}])[0]
    last = (
        f'<p class="meta">Latest: <a href="/git/commits">{E(commit.get("subject", ""))}</a> · '
        f"{E(commit.get('author', ''))} · {E(ago(commit.get('at', '')))}</p>"
        if commit
        else ""
    )
    clone = ""
    if repo.nostr_url:
        clone += f"<code>git clone {E(repo.nostr_url)}</code>"
    clone += f"<code>git clone {E(snap.source)}</code>"
    propose = ""
    if repo.naddr:
        propose = (
            f'<p class="meta">Propose a change with <a href="https://ngit.dev">ngit</a>: '
            f"<code>ngit send</code> from your clone. Open proposals and issues: "
            f'<a href="https://gitworkshop.dev/{repo.naddr}">gitworkshop.dev</a>.</p>'
        )
    readme = ""
    name = next(
        (
            n
            for k, n, _ in view.tree(snap, "")
            if k == "blob" and n.lower() in ("readme.md", "readme")
        ),
        "",
    )
    if name:
        try:
            text = view.blob(snap, name).decode("utf-8", "replace")
            if name.lower().endswith(".md"):
                title, body = pages.render(text)
                body = (f"<h2>{E(title)}</h2>" if title else "") + body
            else:
                body = f'<pre class="code">{E(text)}</pre>'
            readme = f'<div class="readme">{body}</div>'
        except GitViewError:
            pass
    stale = f'<p class="meta bad">Last refresh failed: {E(view.error)}</p>' if view.error else ""
    return (
        f'{STYLE}<div class="card"><h1>{E(info.name)}</h1>'
        f'<p class="muted">{E(info.description)}</p>{header(snap)}{stale}{last}'
        f'<div class="clone">{clone}</div>{propose}<ul class="files">{files}</ul>{readme}</div>'
    )


def register(app: FastAPI) -> None:
    def view(request: Request) -> GitView | None:
        return getattr(request.app.state, "gitview", None)

    async def snap_or_page(request: Request) -> tuple[GitView, Snapshot] | HTMLResponse:
        v = view(request)
        if v is None:
            return HTMLResponse(
                page(
                    "No code here",
                    '<div class="card"><h1>No code here</h1><p class="muted">This belfry shows no repository (GIT_REPO is not set).</p></div>',
                ),
                status_code=404,
            )
        try:
            return v, await run_in_threadpool(v.snapshot)
        except GitViewError as exc:
            return HTMLResponse(
                page(
                    "Code",
                    f'<div class="card"><h1>Code</h1><p class="bad">Not reachable right now: {E(str(exc))}</p></div>',
                ),
                status_code=503,
            )

    @app.get("/git", response_class=HTMLResponse)
    @app.get("/git/", response_class=HTMLResponse, include_in_schema=False)
    async def git_home(request: Request):
        got = await snap_or_page(request)
        if isinstance(got, HTMLResponse):
            return got
        v, snap = got
        return HTMLResponse(
            page(snap.info.name, await run_in_threadpool(home_html, v, snap), wide=True)
        )

    @app.get("/git/tree/{rel:path}", response_class=HTMLResponse)
    async def git_tree(rel: str, request: Request):
        got = await snap_or_page(request)
        if isinstance(got, HTMLResponse):
            return got
        v, snap = got
        rel = rel.strip("/")
        if await run_in_threadpool(v.kind, snap, rel) != "tree":
            return HTMLResponse(
                page("Not found", '<div class="card"><h1>Not found</h1></div>'), status_code=404
            )
        items = await run_in_threadpool(v.tree, snap, rel)
        files = "".join(
            f'<li>{"📁" if k == "tree" else "📄"} <a href="/git/{"tree" if k == "tree" else "blob"}/'
            f'{urllib.parse.quote(p)}">{E(n)}</a></li>'
            for k, n, p in items
        )
        return HTMLResponse(
            page(
                f"{rel} · {snap.info.name}",
                f'{STYLE}<div class="card">{header(snap, rel)}<ul class="files">{files}</ul></div>',
                wide=True,
            )
        )

    @app.get("/git/blob/{rel:path}")
    async def git_blob(rel: str, request: Request, raw: int = 0):
        got = await snap_or_page(request)
        if isinstance(got, HTMLResponse):
            return got
        v, snap = got
        rel = rel.strip("/")
        if await run_in_threadpool(v.kind, snap, rel) != "blob":
            return HTMLResponse(
                page("Not found", '<div class="card"><h1>Not found</h1></div>'), status_code=404
            )
        try:
            data = await run_in_threadpool(v.blob, snap, rel)
        except GitViewError as exc:
            body = f'<p class="muted">{E(str(exc))}</p>'
        else:
            if raw:
                return PlainTextResponse(
                    data.decode("utf-8", "replace"),
                    headers={
                        "Content-Security-Policy": "default-src 'none'",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            if b"\0" in data[:8000]:
                body = f'<p class="muted">A binary file ({len(data)} bytes).</p>'
            else:
                body = f'<pre class="code">{E(data.decode("utf-8", "replace"))}</pre>'
        raw_link = (
            f'<p class="meta"><a href="/git/blob/{urllib.parse.quote(rel)}?raw=1">raw</a></p>'
        )
        return HTMLResponse(
            page(
                f"{rel} · {snap.info.name}",
                f'{STYLE}<div class="card">{header(snap, rel)}{raw_link}{body}</div>',
                wide=True,
            )
        )

    @app.get("/git/commits", response_class=HTMLResponse)
    async def git_commits(request: Request):
        got = await snap_or_page(request)
        if isinstance(got, HTMLResponse):
            return got
        v, snap = got
        rows = "".join(
            f'<div class="commit"><strong>{E(c["subject"])}</strong><div class="meta">'
            f"<code>{c['sha'][:12]}</code> · {E(c['author'])} · {E(ago(c['at']))}</div></div>"
            for c in await run_in_threadpool(v.commits, snap, 30)
        )
        return HTMLResponse(
            page(
                f"Commits · {snap.info.name}",
                f'{STYLE}<div class="card">{header(snap)}<h1>Latest commits</h1>{rows}</div>',
                wide=True,
            )
        )
