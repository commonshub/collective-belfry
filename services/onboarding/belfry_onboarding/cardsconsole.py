"""``belfry.<domain>/cards``: printed cards, for the belfry's admins (docs/cards.md).

- Make 1–40 cards of a kind (group or topic) in a named batch.
- The batches and their cards: state and the name of the group each one started.
- Reset a card: unclaimed again, its link revoked, the group it started left as it is.
- A print view per batch (``/cards/print/<batch>``): four A6 cards on each A4 sheet, group cards
  upright, topic cards on their side, black on white. Each card has the community's name and
  logo, one fixed line, the QR code of ``https://chat.<domain>/g/<id>`` and the address without
  the id.

Every form carries the console's csrf and answers with a fixed message (flash.py). A batch is
found by the number in its address (the second it was made); nothing from the address is shown.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import secrets as pysecrets
import time
from typing import Any

import segno
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import cards, console, flash, salesconsole
from .core import Services

log = logging.getLogger(__name__)
E = html.escape
PATH = "/cards"
MAX = 40
BATCH_MAX = 60
PER_SHEET = 4
LINE = {"group": "Scan to start a group here", "topic": "Scan to start a topic here"}
KIND_NAME = {"group": "Group", "topic": "Topic"}
STATE_NAME = {"unclaimed": "Not used yet", "claimed": "In use", "retired": "Retired"}

MSG = {
    "made": "The cards are made. Print them from the batch below.",
    "reset": "The card is free again. The group it started stays as it is.",
    "count": f"Make 1 to {MAX} cards at a time.",
    "kind": "Choose group cards or topic cards.",
    "nothing": "That card was not in use.",
    "unsupported": "Printed cards need the belfry's own register.",
    "expired": "The form expired; try again.",
}

STYLE = """<style>
.cardsp .batch{border:1px solid var(--border);border-radius:14px;padding:6px 16px 14px;margin:14px 0}
.cardsp .batch h3{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:12px 0 4px}
.cardsp .batch h3 a{margin-left:auto;font-size:15px;font-weight:500}
.cardsp code{font-size:14px;letter-spacing:.06em}
.cardsp td form{display:inline}
</style>"""

PRINT_STYLE = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:#000;font-family:system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.bar{padding:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #000}
.bar button{font:inherit;font-size:16px;padding:8px 16px;border:1px solid #000;border-radius:8px;background:#000;color:#fff}
.bar a{color:#000}
.sheet{display:grid;overflow:hidden;break-after:page;page-break-after:always;margin:0 auto}
.sheet:last-child{break-after:auto;page-break-after:auto}
.card{display:flex;align-items:center;justify-content:space-between;text-align:center;overflow:hidden;outline:.2mm dashed #999;outline-offset:-.1mm}
.card .who{display:flex;align-items:center;justify-content:center;gap:3mm;font-weight:700;font-size:13pt}
.card .who img{height:11mm;max-width:30mm;object-fit:contain;filter:grayscale(1)}
.card .line{font-size:17pt;font-weight:700;line-height:1.2;margin:0}
.card .qr svg{display:block;width:100%;height:100%}
.card .url{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10pt;margin:0}
.portrait .sheet{width:210mm;height:296mm;grid-template-columns:105mm 105mm;grid-template-rows:148mm 148mm}
.portrait .card{width:105mm;height:148mm;padding:11mm 10mm;flex-direction:column}
.portrait .card .qr{width:68mm;height:68mm}
.landscape .sheet{width:296mm;height:210mm;grid-template-columns:148mm 148mm;grid-template-rows:105mm 105mm}
.landscape .card{width:148mm;height:105mm;padding:10mm 11mm;flex-direction:row;gap:8mm}
.landscape .card .qr{width:66mm;height:66mm;flex:none}
.landscape .card .text{display:flex;flex-direction:column;justify-content:space-between;align-self:stretch;flex:1}
.landscape .card .who{flex-direction:column}
@media print{.bar{display:none}.sheet{margin:0}}
@media screen{body{background:#eee}.sheet{background:#fff;margin:16px auto;box-shadow:0 0 0 1px #ccc}}
"""


def qr_svg(url: str) -> str:
    """The QR code as inline SVG: black on white, error correction Q, its quiet zone (4)."""
    return segno.make(url, error="q", micro=False).svg_inline(
        scale=4, border=4, dark="#000", light="#fff", omitsize=True
    )


def _when(at: int) -> str:
    return dt.datetime.fromtimestamp(int(at), dt.UTC).strftime("%-d %b %Y")


def batches(svc: Services) -> list[dict[str, Any]]:
    """The batches, newest first: ``{"key", "name", "kind", "at", "cards"}`` (key: the second it
    was made, which is what its address carries)."""
    out: dict[int, dict[str, Any]] = {}
    for c in svc.odoo.card_list():
        b = out.setdefault(
            int(c["made_at"]),
            {
                "key": int(c["made_at"]),
                "name": c["batch"],
                "kind": c["kind"],
                "at": c["made_at"],
                "cards": [],
            },
        )
        b["cards"].append(c)
    return sorted(out.values(), key=lambda b: -b["key"])


def make(svc: Services, kind: str, name: str, count: int, by: int) -> int:
    """COUNT new cards of KIND in a batch called NAME; returns the batch's key."""
    at = int(time.time())
    taken = {int(c["made_at"]) for c in svc.odoo.card_list()}
    while at in taken:  # a batch is the cards of one second
        at += 1
    name = " ".join(name.split())[:BATCH_MAX] or f"{KIND_NAME[kind]} cards, {_when(at)}"
    made: list[str] = []
    for _ in range(count * 20):
        if len(made) >= count:
            break
        made += svc.odoo.card_make([cards.new_id()], kind, name, by, at)
    log.info("cards: %d %s cards made by partner %s", len(made), kind, by)
    return at


def _names(svc: Services, rows: list[dict[str, Any]]) -> dict[str, str]:
    """Each claimed card's group name as the relay has it now (the name it started with when the
    relay can't be read)."""
    from . import events

    groups = sorted({c["group"] for c in rows if c.get("group")})
    names = {c["group"]: str(c.get("title") or "") for c in rows if c.get("group")}
    try:
        found = events.groups(svc, [{"kinds": [39000], "#d": groups}]) if groups else []
    except Exception as exc:  # noqa: BLE001 — the names they started with
        log.info("cards: group names not read: %s", type(exc).__name__)
        found = []
    for e in sorted(found, key=lambda e: e.get("created_at", 0)):
        d, name = events.tag(e, "d"), events.tag(e, "name")
        if d in names and name:
            names[d] = name
    return names


def page_html(svc: Services, who: dict[str, Any], msg: str, err: str) -> str:
    csrf = E(who["csrf"])
    note = (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
        f'<p class="bad">{E(err)}</p>' if err else ""
    )
    if not cards.own(svc):
        body = f"""{salesconsole.STYLE}{STYLE}<div class="sales cardsp"><h1>Printed cards</h1>
<p class="muted">{E(MSG["unsupported"])}</p></div>"""
        return console.frame("Printed cards", PATH, who, body)
    found = batches(svc)
    names = _names(svc, [c for b in found for c in b["cards"]])
    parts = ""
    for b in found:
        rows = ""
        for c in b["cards"]:
            group = E(names.get(c.get("group") or "", "")) if c.get("group") else ""
            reset = (
                f"""<form method="post" action="{PATH}/{E(c["id"])}/reset" onsubmit="return confirm('Reset this card? It can start a new group; the group it started stays as it is.')">
<input type="hidden" name="csrf" value="{csrf}"><button class="ghost">Reset</button></form>"""
                if c["state"] != "unclaimed"
                else ""
            )
            rows += (
                f"<tr><td><code>{E(c['id'])}</code></td><td>{E(STATE_NAME.get(c['state'], ''))}</td>"
                f"<td>{group}</td><td>{reset}</td></tr>"
            )
        used = sum(1 for c in b["cards"] if c["state"] != "unclaimed")
        parts += f"""<section class="batch"><h3>{E(b["name"])}
<a href="{PATH}/print/{b["key"]}">Print this batch</a></h3>
<p class="hint">{E(KIND_NAME.get(b["kind"], ""))} cards · {len(b["cards"])} made on {E(_when(b["at"]))} · {used} used</p>
<table class="list"><tr><th>Card</th><th>State</th><th>Started</th><th></th></tr>{rows}</table></section>"""
    body = f"""{salesconsole.STYLE}{STYLE}<div class="sales cardsp"><h1>Printed cards</h1>
<p class="muted">A6 cards with a QR code. The first person who scans one starts a group (or a topic) and looks after it; everyone after that joins it.</p>
{note}
<h2>Make cards</h2>
<form method="post" action="{PATH}/make" class="f">
<input type="hidden" name="csrf" value="{csrf}">
<label>Kind <select name="kind"><option value="group">Group cards (upright)</option><option value="topic">Topic cards (on their side)</option></select></label>
<label>Name of the batch <input type="text" name="batch" maxlength="{BATCH_MAX}" placeholder="Spring fair"></label>
<label>How many <input type="number" name="count" min="1" max="{MAX}" value="8" required></label>
<p class="hint">Four cards fit on an A4 sheet.</p>
<div class="actions"><button>Make the cards</button></div></form>
<h2>Batches</h2>
{parts or '<p class="hint">No cards yet.</p>'}</div>"""
    return console.frame("Printed cards", PATH, who, body)


def print_html(svc: Services, batch: dict[str, Any]) -> str:
    """The batch as sheets of four A6 cards. A page of its own (no menu, no theme): black on
    white, sized in millimetres for the browser's Print."""
    logo, community = console.brand_mark()
    topic = batch["kind"] == "topic"
    line = LINE["topic" if topic else "group"]
    address = f"chat.{svc.settings.nip05_domain}/g/"
    who = f'<div class="who"><img src="{E(logo)}" alt=""><span>{E(community)}</span></div>'
    sheets = ""
    rows = batch["cards"]
    for i in range(0, len(rows), PER_SHEET):
        cells = ""
        for c in rows[i : i + PER_SHEET]:
            qr = f'<div class="qr">{qr_svg(cards.card_url(svc, c["id"]))}</div>'
            if topic:
                cells += f"""<div class="card">{qr}<div class="text">{who}<p class="line">{E(line)}</p><p class="url">{E(address)}</p></div></div>"""
            else:
                cells += f"""<div class="card">{who}<p class="line">{E(line)}</p>{qr}<p class="url">{E(address)}</p></div>"""
        sheets += f'<div class="sheet">{cells}</div>'
    side = "landscape" if topic else "portrait"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Printed cards</title>
<style>@page{{size:A4 {side};margin:0}}{PRINT_STYLE}</style></head>
<body class="{side}"><div class="bar"><button type="button" onclick="window.print()">Print</button>
<span>{len(rows)} {E(KIND_NAME[batch["kind"]].lower())} cards, four on each A4 sheet ({side}). Print at 100%, without margins.</span>
<a href="{PATH}">Back</a></div>
{sheets}</body></html>"""


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    async def who(request: Request) -> dict[str, Any] | None:
        s = svc(request)
        sess = console.read_session(s, request.cookies.get(console.COOKIE))
        return await run_in_threadpool(console.viewer, s, sess) if sess else None

    def back(request: Request, msg: str = "", err: str = "") -> RedirectResponse:
        return flash.redirect(svc(request), PATH, MSG.get(msg, ""), MSG.get(err, ""))

    def refused(me: dict[str, Any] | None) -> HTMLResponse | RedirectResponse | None:
        if me is None:
            return RedirectResponse(f"/login?to={PATH}", status_code=303)
        if not me["admin"]:
            body = "<h1>Printed cards</h1><p>Only the belfry's admins see this page.</p>"
            return HTMLResponse(console.frame("Printed cards", PATH, me, body), status_code=403)
        return None

    @app.get("/console/cards", response_class=HTMLResponse)
    async def cards_page(request: Request):
        s = svc(request)
        me = await who(request)
        if (no := refused(me)) is not None:
            return no
        msg, err = flash.peek(request, s)
        out = await run_in_threadpool(page_html, s, me, msg, err)
        return flash.clear(HTMLResponse(out, headers={"Cache-Control": "no-store"}), request)

    @app.get("/console/cards/print/{key}", response_class=HTMLResponse)
    async def cards_print(request: Request, key: int):
        s = svc(request)
        me = await who(request)
        if (no := refused(me)) is not None:
            return no
        found = await run_in_threadpool(batches, s) if cards.own(s) else []
        batch = next((b for b in found if b["key"] == key), None)
        if batch is None:
            body = "<h1>Printed cards</h1><p>No such batch.</p>"
            return HTMLResponse(console.frame("Printed cards", PATH, me, body), status_code=404)
        out = await run_in_threadpool(print_html, s, batch)
        return HTMLResponse(out, headers={"Cache-Control": "no-store"})

    async def admin_form(request: Request) -> tuple[dict[str, Any] | None, Any]:
        form = await request.form()
        me = await who(request)
        ok = (
            me is not None
            and me["admin"]
            and pysecrets.compare_digest(str(form.get("csrf") or ""), me["csrf"] or "x")
        )
        return (me if ok else None), form

    @app.post("/console/cards/make")
    async def cards_make(request: Request):
        me, form = await admin_form(request)
        if me is None:
            return back(request, err="expired")
        s = svc(request)
        if not cards.own(s):
            return back(request, err="unsupported")
        kind = str(form.get("kind") or "")
        if kind not in cards.KINDS:
            return back(request, err="kind")
        try:
            count = int(str(form.get("count") or ""))
        except ValueError:
            count = 0
        if not 1 <= count <= MAX:
            return back(request, err="count")
        name = str(form.get("batch") or "")
        await run_in_threadpool(make, s, kind, name, count, int(me["partner"]))
        return back(request, msg="made")

    @app.post("/console/cards/{card_id}/reset")
    async def cards_reset(request: Request, card_id: str):
        me, _form = await admin_form(request)
        if me is None:
            return back(request, err="expired")
        s = svc(request)
        if not cards.own(s):
            return back(request, err="unsupported")
        cid = card_id.upper()
        done = cards.ID.fullmatch(cid) and await run_in_threadpool(s.odoo.card_reset, cid)
        if not done:
            return back(request, err="nothing")
        log.info("cards: %s reset by partner %s", cid, me["partner"])
        return back(request, msg="reset")


__all__ = ["register", "page_html", "print_html", "batches", "make", "qr_svg"]
