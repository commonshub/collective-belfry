"""@mentions by e-mail: a daily (or weekly) digest of the messages that name you.

A mention is a message (kind 9) or a thread reply (1111) in a channel of the groups relay with
the member's key in a ``p`` tag (the chat writes ``nostr:npub…`` in the text and adds the tag,
NIP-27). Once a day, at DIGEST_HOUR in DIGEST_TZ, every member whose digest is due gets one mail
listing the mentions since their last one, with a link to each message, leaving out what they
saw already (they opened the channel since: its ``seen:<channel>`` event) and their own posts.
Daily is the default; the footer switches to weekly (Mondays) or stops, through a small page
with a button (mail scanners follow links; a POST changes it). The belfry's own register keeps
the choice and the last digest (register params). Without it (Odoo), nothing is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import html
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from . import flash, theme
from .core import Services, m2o_id

log = logging.getLogger(__name__)
E = html.escape
CHOICES = ("daily", "weekly", "off")
SAID = {"daily": "every day", "weekly": "once a week, on Mondays", "off": "no more"}
LOOP_SECONDS = 600
MAX_PER_MAIL = 25


def own(svc: Services) -> bool:
    from .register import Register

    return isinstance(svc.odoo, Register)


def _key(partner: int, part: str) -> str:
    return f"belfry.mentions.{int(partner)}.{part}"


def prefs(svc: Services, partner: int) -> dict[str, Any]:
    """The member's choice and their last digest, kept apart: the morning job writes only
    ``last``, the footer page only ``f``, so neither overwrites the other."""
    f = svc.odoo.param(_key(partner, "f"), "")
    last = svc.odoo.param(_key(partner, "last"), "0")
    return {"f": f if f in CHOICES else "daily", "last": int(last or 0) if last.isdigit() else 0}


def save(svc: Services, partner: int, **change: Any) -> None:
    if "f" in change:
        svc.odoo.set_param(_key(partner, "f"), str(change["f"]))
    if "last" in change:
        svc.odoo.set_param(_key(partner, "last"), str(int(change["last"])))


def token(svc: Services, partner: int) -> str:
    secret = (svc.settings.pass_page_secret or svc.settings.hook_token or "").encode()
    mac = hmac.new(secret, f"mentions:{int(partner)}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{int(partner)}.{mac}"


def partner_of(svc: Services, tok: str) -> int | None:
    pid, _, _mac = (tok or "").partition(".")
    if not pid.isdigit():
        return None
    return int(pid) if hmac.compare_digest(token(svc, int(pid)), tok) else None


def members(svc: Services) -> list[dict[str, Any]]:
    """Everyone with an active pass and a key (guests too: they can be mentioned)."""
    rows = svc.odoo.search_read(
        "belfry.pass",
        [["state", "=", "active"], ["keycast_pubkey", "!=", False]],
        ["partner_id", "keycast_pubkey"],
    )
    out, seen = [], set()
    for r in rows:
        pid = m2o_id(r.get("partner_id"))
        if not pid or pid in seen:
            continue
        seen.add(pid)
        p = svc.odoo.read_one("res.partner", pid, ["name", "email"]) or {}
        if p.get("email"):
            out.append(
                {
                    "partner": pid,
                    "pubkey": r["keycast_pubkey"],
                    "email": p["email"],
                    "name": p.get("name") or "",
                }
            )
    return out


def _tag(e: dict[str, Any], k: str) -> str:
    return next((t[1] for t in e.get("tags", []) if t and t[0] == k and len(t) > 1), "")


def _text(svc: Services, e: dict[str, Any], names: dict[str, str]) -> str:
    """The message as plain text: nostr:npub… mentions as @name, at most 280 characters."""
    import re

    from .nostr import to_hex_pubkey as npub_to_hex

    def name(m: re.Match) -> str:
        try:
            return "@" + (names.get(npub_to_hex(m.group(1))) or m.group(1)[:12] + "…")
        except Exception:  # noqa: BLE001 — a broken npub stays as it was
            return m.group(0)

    text = re.sub(r"nostr:(npub1[02-9ac-hj-np-z]+)", name, e.get("content") or "")
    text = re.sub(r"\*\*|__|~~|`", "", text)  # Markdown marks: a snippet is plain text
    text = " ".join(text.split())
    return text if len(text) <= 280 else text[:277] + "…"


def collect(
    svc: Services, people: list[dict[str, Any]], since: int
) -> dict[str, list[dict[str, Any]]]:
    """For each member key, the mentions of it since SINCE they didn't see or write."""
    from . import followers

    got = followers._relay(svc)
    if got is None or not people:
        return {}
    relay, sign = got
    keys = [p["pubkey"] for p in people]
    events = relay.query([{"kinds": [9, 1111], "#p": keys, "since": since, "limit": 2000}], sign)
    seen = relay.query([{"kinds": [30078], "authors": keys, "since": since, "limit": 5000}], sign)
    last_seen: dict[tuple[str, str], int] = {}
    for s in seen:
        d = _tag(s, "d")
        if d.startswith("seen:"):
            k = (s["pubkey"], d[5:])
            last_seen[k] = max(last_seen.get(k, 0), s["created_at"])
    out: dict[str, list[dict[str, Any]]] = {}
    wanted = set(keys)
    for e in sorted(events, key=lambda x: x["created_at"]):
        channel = _tag(e, "h")
        for t in e.get("tags", []):
            if t[0] != "p" or len(t) < 2 or t[1] not in wanted or t[1] == e["pubkey"]:
                continue
            if last_seen.get((t[1], channel), 0) >= e["created_at"]:
                continue  # they opened the channel after it
            out.setdefault(t[1], []).append(e)
    return out


def _names(svc: Services, keys: set[str]) -> dict[str, str]:
    from . import followers

    got = followers._relay(svc)
    if got is None or not keys:
        return {}
    relay, sign = got
    names: dict[str, tuple[int, str]] = {}
    for e in relay.query([{"kinds": [0], "authors": sorted(keys), "limit": 1000}], sign):
        try:
            c = json.loads(e.get("content") or "{}")
        except ValueError:
            continue
        n = str(c.get("display_name") or c.get("name") or "").strip()
        if n and names.get(e["pubkey"], (0, ""))[0] < e["created_at"]:
            names[e["pubkey"]] = (e["created_at"], n[:80])
    return {k: v[1] for k, v in names.items()}


def link(svc: Services, e: dict[str, Any]) -> str:
    channel = _tag(e, "h")
    root = _tag(e, "E") if e.get("kind") == 1111 else ""
    base = f"https://{svc.settings.nip05_domain}/chat/#/c/{channel}"
    return f"{base}/thread/{root}" if root else f"{base}/m/{e['id']}"


def send_digests(svc: Services, now: dt.datetime) -> dict[str, int]:
    """Mail every member whose digest is due (daily; weekly on Mondays)."""
    from . import followers

    people = [p for p in members(svc)]
    due = []
    for p in people:
        pr = prefs(svc, p["partner"])
        if pr["f"] == "off" or (pr["f"] == "weekly" and now.weekday() != 0):
            continue
        window = 8 * 86400 if pr["f"] == "weekly" else 2 * 86400
        due.append({**p, "since": max(pr["last"], int(now.timestamp()) - window)})
    if not due:
        return {"due": 0, "sent": 0}
    found = collect(svc, due, min(p["since"] for p in due))
    authors = {e["pubkey"] for evs in found.values() for e in evs}
    names = _names(svc, authors | {p["pubkey"] for p in due})
    for p in people:  # no profile on the relay yet: the register's name
        if p["name"]:
            names.setdefault(p["pubkey"], p["name"])
    community = theme.current().name
    public = svc.settings.public_url.rstrip("/")
    sent = 0
    for p in due:
        evs = [e for e in found.get(p["pubkey"], []) if e["created_at"] > p["since"]]
        if not evs:
            save(svc, p["partner"], last=int(now.timestamp()))
            continue
        lines = []
        for e in evs[-MAX_PER_MAIL:]:
            who = names.get(e["pubkey"]) or "Someone"
            where = followers.channel_label(svc, _tag(e, "h"))
            lines.append(f"{who} in {where}: {_text(svc, e, names)}\n{link(svc, e)}")
        if len(evs) > MAX_PER_MAIL:
            lines.append(f"…and {len(evs) - MAX_PER_MAIL} more in the chat.")
        pr = prefs(svc, p["partner"])
        other = "weekly" if pr["f"] == "daily" else "daily"
        tok = token(svc, p["partner"])
        footer = (
            f"You get these {SAID[pr['f']]}. {other.capitalize()} instead: "
            f"{public}/mentions/{tok}?f={other} · Stop them: {public}/mentions/{tok}?f=off"
        )
        n = len(evs)
        ok = svc.odoo.mailer.send(
            p["email"],
            f"{n} {'mention' if n == 1 else 'mentions'} on {community}",
            [
                f"Hello {p['name'] or ''}".strip() + ",",
                f"You were mentioned {n} {'time' if n == 1 else 'times'}:",
                *lines,
            ],
            (f"https://{svc.settings.nip05_domain}/chat/", "Open the chat"),
            footer,
            {"List-Unsubscribe": f"<{public}/mentions/{tok}?f=off>"},
        )
        if ok:
            sent += 1
            save(svc, p["partner"], last=int(now.timestamp()))
    log.info("mentions: %d due, %d mailed", len(due), sent)
    return {"due": len(due), "sent": sent}


def morning(svc: Services, now: dt.datetime | None = None) -> dict[str, int] | None:
    """Once a day, once the morning hour has come."""
    if not own(svc):
        return None
    tz = ZoneInfo(svc.settings.digest_tz)
    local = (now or dt.datetime.now(tz)).astimezone(tz)
    if local.hour < svc.settings.digest_hour:
        return None
    day = local.date().isoformat()
    if svc.odoo.param("belfry.mentions_day") >= day:
        return None
    svc.odoo.set_param("belfry.mentions_day", day)
    return send_digests(svc, local)


@contextlib.asynccontextmanager
async def running(app: FastAPI):
    async def loop() -> None:
        while True:
            try:
                await run_in_threadpool(morning, app.state.services)
            except Exception as exc:  # noqa: BLE001 — retried on the next round
                log.warning("mentions: %s", exc)
            await asyncio.sleep(LOOP_SECONDS)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def register(app: FastAPI) -> None:
    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(theme.frame(title, body), status_code=status)

    @app.get("/mentions/{tok}", response_class=HTMLResponse)
    async def mentions_page(request: Request, tok: str, f: str = ""):
        s = request.app.state.services
        partner = partner_of(s, tok) if own(s) else None
        if partner is None:
            return page(
                "Mentions", "<h1>Mentions</h1><p>This link does not work any more.</p>", 404
            )
        msg, _ = flash.peek(request, s)
        cur = (await run_in_threadpool(prefs, s, partner))["f"]
        want = f if f in CHOICES else ""
        note = f'<p class="ok">{E(msg)}</p>' if msg else ""
        label = {"daily": "Daily", "weekly": "Weekly", "off": "Stop"}
        buttons = "".join(
            f'<form method="post" action="/mentions/{E(tok)}" class="pick">'
            f'<input type="hidden" name="f" value="{c}">'
            f'<button class="btn{"" if c == want else " ghost"}" type="submit">{label[c]}</button>'
            "</form>"
            for c in CHOICES
        )
        body = (
            "<style>.pick{display:inline-block;margin:0 8px 8px 0}</style>"
            f"<h1>Mention e-mails</h1>{note}<p>When someone mentions you in the chat, we e-mail "
            f"you a digest. Now: <b>{E(SAID[cur])}</b>.</p>{buttons}"
        )
        return flash.clear(page("Mention e-mails", body), request)

    @app.post("/mentions/{tok}")
    async def mentions_set(request: Request, tok: str, f: str = Form("")):
        s = request.app.state.services
        partner = partner_of(s, tok) if own(s) else None
        if partner is None or f not in CHOICES:
            return Response(status_code=404)
        await run_in_threadpool(save, s, partner, f=f)
        return flash.redirect(s, f"/mentions/{tok}", msg=f"Saved: mention e-mails {SAID[f]}.")
