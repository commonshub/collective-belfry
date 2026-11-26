"""``belfry.<domain>/cards``: printed cards, for the belfry's admins (docs/cards.md §5).

The page follows the canvas "Belfry · Group cards" (board cards/print): its layout, words and
sizes are copied here. The cards themselves are drawn into a PDF by cardspdf.py.

- **Make cards**: Groups or Topics, picked from two tiles that each show the printed card;
  cards per page (1 on A4, 2 A5, 4 A6); how many pages, with the number of cards that makes; a
  batch name ("Batch N" unless typed); who the people who join with these cards become, Member
  (the default) or Guest (§6); **Generate PDF**: the batch is made and its PDF opens
  (``/cards/batch/<batch>.pdf``) in a new tab.
- **Cards made**: Card · For · Batch · People become · State · Name · Started by, and Reset on
  a used card (unclaimed again, its link revoked, the group it started left as it is). A
  batch's name opens its PDF again.

Every form carries the console's csrf and answers with a fixed message (flash.py). A batch is
found by the number in its address (the second it was made); nothing from the address is shown.
"""

from __future__ import annotations

import html
import logging
import secrets as pysecrets
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import cards, cardspdf, console, flash
from .core import Services

log = logging.getLogger(__name__)
E = html.escape
PATH = "/cards"
PER_PAGE = (1, 2, 4)
PAGES_MAX = 50
BATCH_MAX = 60
SIZE = {1: "A4", 2: "A5", 4: "A6"}
KIND_NAME = {"group": "Group", "topic": "Topic"}
JOIN_AS = ("member", "guest")
JOIN_AS_NAME = {"member": "Members", "guest": "Guests"}
STATE = {
    "unclaimed": ("Not scanned yet", ""),
    "claimed": ("Claimed", " on"),
    "retired": ("Retired", " off"),
}
# the words on the printed cards, for the tiles' small pictures (cardspdf.py draws the cards)
SCAN = {"group": "Scan to join this group", "topic": "Scan to contribute to this topic"}
FINE = {"group": "", "topic": "No account required"}

MSG = {
    "reset": "The card is free again. The group it started stays as it is.",
    "pages": f"Make 1 to {PAGES_MAX} pages at a time.",
    "per_page": "Choose 1, 2 or 4 cards per page.",
    "kind": "Choose group cards or topic cards.",
    "join_as": "Choose whether people join as members or as guests.",
    "nothing": "That card was not in use.",
    "unsupported": "Printed cards need the belfry's own register.",
    "expired": "The form expired; try again.",
}


def on_each(per_page: int) -> str:
    """ "4 A6 on each A4", as the board says it under the total."""
    return "1 on each A4" if per_page == 1 else f"{per_page} {SIZE[per_page]} on each A4"


# The console page, after the board cards/print (and -dark): its sizes and radii, in the
# theme's colours (the board's black and white are the base theme's own).
STYLE = """<style>
.pc{font-size:15px;line-height:21px}
.pc h1{font-size:28px;line-height:34px;margin:0 0 6px}
.pc .lead{font-size:16px;line-height:22px;color:var(--fg-muted);margin:0 0 20px;max-width:720px}
.pc .cpanel{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:20px 22px;margin-bottom:18px}
.pc .cpanel h2{font-size:17px;margin:0 0 12px}
.pc .sr{position:absolute;opacity:0;width:1px;height:1px;pointer-events:none}
.pc .pick{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}
.pc .opt{display:flex;gap:18px;align-items:center;padding:16px 18px;border-radius:16px;border:2px solid var(--border);background:var(--surface);position:relative;cursor:pointer}
.pc .opt:has(input:checked){border-color:var(--fg)}
.pc .opt:has(input:focus-visible),.pc .ppo:has(input:focus-visible){outline:2px solid var(--fg);outline-offset:2px}
.pc .opt .chk{position:absolute;top:12px;right:12px;width:24px;height:24px;border-radius:12px;border:2px solid var(--border-strong);box-sizing:border-box;display:flex;align-items:center;justify-content:center}
.pc .opt .chk svg{display:none;width:14px;height:14px;stroke:var(--bg);fill:none;stroke-width:3}
.pc .opt:has(input:checked) .chk{background:var(--fg);border-color:var(--fg)}
.pc .opt:has(input:checked) .chk svg{display:block}
.pc .opt .ot{display:flex;flex-direction:column;gap:4px;min-width:0}
.pc .opt .ot b{font-size:19px;line-height:24px}
.pc .opt .ot span{font-size:15px;line-height:20px;color:var(--fg-muted)}
.pc .opt .ot .acct{font-size:14px;line-height:19px;font-weight:600;margin-top:4px}
.pc .opt .ot em{font-style:normal;font-size:13px;color:var(--fg-faint)}
.pc .prev{flex:none;overflow:hidden;background:#fff;border:1px solid #D1D1D6;border-radius:6px;box-shadow:0 2px 6px rgba(0,0,0,.08);padding:9px;box-sizing:border-box;display:flex;color:#000;letter-spacing:0}
.pc .prev.pt{width:124px;height:176px;flex-direction:column;gap:4px}
.pc .prev.ls{width:176px;height:124px;gap:7px}
.pc .prev span{display:block}
.pc .prev .pb{font-size:7px;line-height:9px;font-weight:700;display:flex;align-items:center;gap:3px;white-space:nowrap;overflow:hidden}
.pc .prev .pb i{width:7px;height:8px;background:#000;border-radius:1px;display:block;flex:none}
.pc .prev .pk{font-size:6.5px;line-height:8px;color:#3A3A3C}
.pc .prev .pk b{color:#000}
.pc .prev .bx{border:1px solid #000;border-radius:4px;padding:2px 4px;min-height:0;overflow:hidden;font-size:6.5px;line-height:9px;font-weight:700}
.pc .prev .bx i{display:block;border-bottom:.5px solid #C7C7CC;height:6px}
.pc .prev .pq{width:30px;height:30px;flex:none;background:repeating-conic-gradient(#000 0 25%,#fff 0 50%) 0 0/8px 8px;border:2px solid #000;box-sizing:border-box}
.pc .prev .pl{font-size:6px;line-height:7px;font-weight:700}
.pc .prev .pf{font-size:5.5px;line-height:7px;color:#6C6C70}
.pc .mk2{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}
.pc .fl{display:flex;flex-direction:column;gap:6px;margin:0;width:260px;max-width:100%}
.pc .fl>label,.pc .fl>.lb{font-size:15px;font-weight:600}
.pc .fl .in{height:50px;border-radius:14px;background:var(--surface-deep);border:0;padding:0 16px;font:inherit;font-size:17px;color:var(--fg);box-sizing:border-box;width:100%}
.pc .fl .help{font-size:13px;color:var(--fg-faint)}
.pc .ppg{display:flex;gap:8px;flex-wrap:wrap}
.pc .ppo{display:flex;align-items:center;gap:10px;height:50px;padding:0 14px 0 10px;border-radius:14px;border:2px solid var(--border);background:var(--surface);font-size:15px;font-weight:600;white-space:nowrap;cursor:pointer;box-sizing:border-box;position:relative}
.pc .ppo:has(input:checked){border-color:var(--fg)}
.pc .ppo .pgi{width:22px;height:31px;border:1.5px solid currentColor;border-radius:2px;display:grid;box-sizing:border-box;gap:1.5px;padding:1.5px}
.pc .ppo .pgi i{background:var(--border-strong);border-radius:1px}
.pc .ppo:has(input:checked) .pgi i{background:var(--fg)}
.pc .ppo small{font-size:13px;font-weight:500;color:var(--fg-faint)}
.pc .tot{display:flex;flex-direction:column;margin-top:30px;gap:2px}
.pc .tot b{font-size:19px;line-height:24px}
.pc .tot span{font-size:13px;color:var(--fg-faint)}
.pc button.gen{height:50px;border-radius:25px;border:0;background:var(--accent);color:var(--on-accent);font:inherit;font-size:17px;font-weight:600;display:flex;align-items:center;justify-content:center;white-space:nowrap;flex:none;padding:0 24px;margin-top:27px;width:auto;cursor:pointer}
.pc table{width:100%;border-collapse:collapse;font-size:15px}
.pc th{text-align:left;font-size:13px;font-weight:600;color:var(--fg-faint);padding:8px 10px;border-bottom:1px solid var(--border-strong)}
.pc td{padding:11px 10px;border-bottom:1px solid var(--border)}
.pc .cid{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-weight:600;letter-spacing:.06em}
.pc .kindp{display:inline-flex;align-items:center;gap:5px;font-size:14px}
.pc .kindp i{display:block;border:1.5px solid currentColor;border-radius:2px}
.pc .pill{display:inline-flex;height:24px;align-items:center;padding:0 10px;border-radius:12px;font-size:13px;font-weight:600;background:#EFEFF4;color:#3A3A3C;white-space:nowrap}
.pc .pill.on{background:#EAF4EC;color:#14532D}
.pc .pill.off{background:#F2F2F7;color:#6C6C70}
.pc td form{display:inline;margin:0}
.pc .tlk,.pc button.tlk{color:#0A5FC4;font:inherit;font-weight:500;background:none;border:0;padding:0;margin:0;cursor:pointer;text-decoration:none;box-shadow:none}
.pc td a.bn{color:inherit;text-decoration:none}
.pc td a.bn:hover{text-decoration:underline}
.pc .tw{overflow-x:auto}
@media (prefers-color-scheme:dark){
.pc .pill{background:#2C2C2E;color:#D1D1D6}
.pc .pill.on{background:#0F2E1A;color:#BFE6CB}
.pc .pill.off{background:#1C1C1E;color:#98989F}
.pc .tlk,.pc button.tlk{color:#5AA9FF}
}
@media (max-width:760px){.pc .pick{grid-template-columns:1fr}.pc button.gen{margin-top:6px}.pc .tot{margin-top:6px}.pc .cpanel{padding:16px 14px}}
</style>"""

SCRIPT = """<script>
(function(){var f=document.getElementById('pc-make');if(!f)return;
var t=document.getElementById('pc-total'),e=document.getElementById('pc-each'),b=document.getElementById('pc-btn');
var S={1:'1 on each A4',2:'2 A5 on each A4',4:'4 A6 on each A4'};
function up(){var p=+(f.querySelector('input[name=per_page]:checked')||{}).value||4,
n=Math.max(1,Math.min(%d,parseInt(f.pages.value,10)||0)),c=p*n;
t.textContent='= '+c+(c===1?' card':' cards');e.textContent=S[p];}
f.addEventListener('input',up);f.addEventListener('change',up);up();
f.addEventListener('submit',function(){setTimeout(function(){location.reload()},1500)});
})();
</script>""".replace("%d", str(PAGES_MAX))


def batches(svc: Services) -> list[dict[str, Any]]:
    """The batches, newest first: ``{"key", "name", "kind", "per_page", "at", "cards"}`` (key:
    the second it was made, which is what its address carries)."""
    out: dict[int, dict[str, Any]] = {}
    for c in svc.odoo.card_list():
        b = out.setdefault(
            int(c["made_at"]),
            {
                "key": int(c["made_at"]),
                "name": c["batch"],
                "kind": c["kind"],
                "per_page": int(c.get("per_page") or 4),
                "join_as": c.get("join_as") or "member",
                "at": c["made_at"],
                "cards": [],
            },
        )
        b["cards"].append(c)
    return sorted(out.values(), key=lambda b: -b["key"])


def next_batch_name(svc: Services) -> str:
    """ "Batch N": N counts every batch this belfry made, plus one."""
    return f"Batch {len(batches(svc)) + 1}"


def make(
    svc: Services,
    kind: str,
    name: str,
    count: int,
    by: int,
    per_page: int = 4,
    join_as: str = "member",
) -> int:
    """COUNT new cards of KIND in a batch called NAME, printed PER_PAGE to an A4, whose people
    become JOIN_AS (``member`` or ``guest``); returns the batch's key."""
    name = " ".join(name.split())[:BATCH_MAX] or next_batch_name(svc)
    at = int(time.time())
    taken = {int(c["made_at"]) for c in svc.odoo.card_list()}
    while at in taken:  # a batch is the cards of one second
        at += 1
    made: list[str] = []
    for _ in range(count * 20):
        if len(made) >= count:
            break
        made += svc.odoo.card_make([cards.new_id()], kind, name, by, at, per_page, join_as)
    log.info("cards: %d %s cards (%s) made by partner %s", len(made), kind, join_as, by)
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


def _starters(svc: Services, rows: list[dict[str, Any]]) -> dict[str, str]:
    """card id → who started it, by name; a guest is said to be one ("Lou (guest steward)")."""
    claimed = [c for c in rows if c.get("claimed_by")]
    if not claimed:
        return {}
    members = {
        p.get("keycast_pubkey")
        for p in svc.odoo.search_read(
            "belfry.pass", [["state", "=", "active"]], ["keycast_pubkey", "tier"]
        )
        if p.get("tier") != "guest"
    }
    out = {}
    for c in claimed:
        partner = svc.odoo.read_one("res.partner", int(c["claimed_by"]), ["name"]) or {}
        name = " ".join(str(partner.get("name") or "").split())[:100] or "Somebody"
        out[c["id"]] = name if c.get("claimed_key") in members else f"{name} (guest steward)"
    return out


def _tile_preview(kind: str, community: str) -> str:
    """The small picture of the printed card on a tile (the board's own drawing)."""
    brand = f'<span class="pb"><i></i>{E(community)}</span>'
    if kind == "group":
        return f"""<span class="prev pt" aria-hidden="true">{brand}<span class="pk"><b>Group</b> · people you get to know</span><span class="bx">Group name:<i></i></span><span class="bx" style="flex:1">Description:<i></i><i></i><i></i></span><span style="display:flex;gap:5px;align-items:flex-end"><span class="pq"></span><span class="pl">{E(SCAN["group"])}</span></span></span>"""
    return f"""<span class="prev ls" aria-hidden="true"><span style="flex:1;display:flex;flex-direction:column;gap:5px;min-width:0">{brand}<span class="pk"><b>Topic</b> · share what you know</span><span class="bx" style="font-size:11px;line-height:12px">#<i style="height:0;margin-top:-2px"></i></span><span class="bx" style="flex:1">About:<i></i></span></span><span style="width:56px;flex:none;display:flex;flex-direction:column;justify-content:flex-end;gap:3px"><span class="pq" style="width:40px;height:40px"></span><span class="pl">{E(SCAN["topic"])}</span><span class="pf">{E(FINE["topic"])}</span></span></span>"""


def page_html(svc: Services, who: dict[str, Any], msg: str, err: str) -> str:
    csrf = E(who["csrf"])
    note = (f'<p class="ok">{E(msg)}</p>' if msg else "") + (
        f'<p class="bad">{E(err)}</p>' if err else ""
    )
    head = """<h1>Printed cards</h1><p class="lead">Print cards with a QR code and pin them up. The first person to scan a card starts it; everyone who scans it after joins or follows.</p>"""
    if not cards.own(svc):
        body = f'{STYLE}<div class="pc">{head}<div class="cpanel"><p>{E(MSG["unsupported"])}</p></div></div>'
        return console.frame("Printed cards", PATH, who, body, panels=True)
    _logo, community = console.brand_mark()
    found = batches(svc)
    rows = [c for b in found for c in b["cards"]]
    names = _names(svc, rows)
    starters = _starters(svc, rows)
    check = '<span class="chk" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></span>'
    tiles = f"""<div class="pick" role="radiogroup" aria-label="What the cards are for">
<label class="opt"><input class="sr" type="radio" name="kind" value="group" checked>{_tile_preview("group", community)}<span class="ot"><b>Groups</b><span>People who get to know each other. They join and say hi.</span><strong class="acct">Account required</strong><em>Portrait</em></span>{check}</label>
<label class="opt"><input class="sr" type="radio" name="kind" value="topic">{_tile_preview("topic", community)}<span class="ot"><b>Topics</b><span>People come and go, share what they know and follow by e-mail.</span><strong class="acct">No account needed to post</strong><em>Landscape</em></span>{check}</label></div>"""
    grids = {1: "1fr/1fr", 2: "1fr 1fr/1fr", 4: "1fr 1fr/1fr 1fr"}
    per = "".join(
        f"""<label class="ppo"><input class="sr" type="radio" name="per_page" value="{n}"{" checked" if n == 4 else ""}><span class="pgi" style="grid-template:{grids[n]}" aria-hidden="true">{"<i></i>" * n}</span>{n} <small>{SIZE[n]}</small></label>"""
        for n in PER_PAGE
    )
    joins = "".join(
        f"""<label class="ppo"><input class="sr" type="radio" name="join_as" value="{v}"{" checked" if v == "member" else ""}>{label}</label>"""
        for v, label in (("member", "Member"), ("guest", "Guest"))
    )
    batch = E(next_batch_name(svc))
    form = f"""<form method="post" action="{PATH}/make" target="_blank" id="pc-make">
<input type="hidden" name="csrf" value="{csrf}">{tiles}
<div class="mk2"><div class="fl" style="width:auto;flex:none"><span class="lb" id="pc-ppl">Cards per page</span><div class="ppg" role="radiogroup" aria-labelledby="pc-ppl">{per}</div></div>
<div class="fl" style="width:110px"><label for="pc-pages">Pages</label><input class="in" id="pc-pages" type="number" name="pages" min="1" max="{PAGES_MAX}" value="2" required inputmode="numeric"><span class="help">1 to {PAGES_MAX}</span></div>
<div class="tot" aria-live="polite"><b id="pc-total">= 8 cards</b><span id="pc-each">{E(on_each(4))}</span></div></div>
<div class="mk2" style="margin-top:14px"><div class="fl" style="width:auto;flex:none"><span class="lb" id="pc-join">People who join with these cards become</span><div class="ppg" role="radiogroup" aria-labelledby="pc-join">{joins}</div><span class="help">Members get the town square at once</span></div></div>
<div class="mk2" style="margin-top:14px"><div class="fl"><label for="pc-batch">Batch name</label><input class="in" id="pc-batch" type="text" name="batch" maxlength="{BATCH_MAX}" value="{batch}"><span class="help">To find these cards in the list</span></div>
<button class="gen" id="pc-btn">Generate PDF</button></div></form>"""
    trs = ""
    for b in found:
        for c in b["cards"]:
            label, cls = STATE.get(c["state"], ("", ""))
            size = "width:11px;height:8px" if c["kind"] == "topic" else "width:8px;height:11px"
            name = ""
            if c.get("group"):
                # a topic is known by its hashtag, a group by its name (never its id)
                name = "#" + c["group"] if c["kind"] == "topic" else names.get(c["group"], "")
            reset = (
                f"""<form method="post" action="{PATH}/{E(c["id"])}/reset" onsubmit="return confirm('Reset this card? It can start something new; what it started stays as it is.')"><input type="hidden" name="csrf" value="{csrf}"><button class="tlk">Reset</button></form>"""
                if c["state"] != "unclaimed"
                else ""
            )
            trs += (
                f'<tr><td class="cid">{E(c["id"])}</td>'
                f'<td><span class="kindp"><i style="{size}"></i>{E(KIND_NAME.get(c["kind"], ""))}</span></td>'
                f'<td><a class="bn" href="{PATH}/batch/{b["key"]}.pdf" target="_blank" rel="noopener" title="Open the PDF of this batch again">{E(b["name"])}</a></td>'
                f"<td>{E(JOIN_AS_NAME.get(b['join_as'], ''))}</td>"
                f'<td><span class="pill{cls}">{E(label)}</span></td>'
                f"<td>{E(name)}</td><td>{E(starters.get(c['id'], ''))}</td><td>{reset}</td></tr>"
            )
    table = (
        f"""<div class="tw"><table><thead><tr><th>Card</th><th>For</th><th>Batch</th><th>People become</th><th>State</th><th>Name</th><th>Started by</th><th></th></tr></thead><tbody>{trs}</tbody></table></div>"""
        if trs
        else '<p class="lead" style="margin:0">No cards yet.</p>'
    )
    body = f"""{STYLE}<div class="pc">{head}{note}
<div class="cpanel"><h2>Make cards</h2>{form}</div>
<div class="cpanel"><h2>Cards made · {len(rows)}</h2>{table}</div></div>{SCRIPT}"""
    return console.frame("Printed cards", PATH, who, body, panels=True)


def pdf_of(svc: Services, batch: dict[str, Any]) -> bytes:
    """The batch's cards as a PDF (cardspdf.py): one card per id, in the batch's order."""
    _logo, community = console.brand_mark()
    urls = [cards.card_url(svc, c["id"]) for c in batch["cards"]]
    data, _sheet, _layout = cardspdf.render(
        svc, batch["kind"], int(batch.get("per_page") or 4), urls, community, batch["name"]
    )
    return data


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

    @app.get("/console/cards/batch/{name}")
    async def cards_pdf(request: Request, name: str):
        """A batch as a PDF (cardspdf.py): ``/cards/batch/<batch>.pdf``, for admins."""
        s = svc(request)
        me = await who(request)
        if (no := refused(me)) is not None:
            return no
        key = name.removesuffix(".pdf")
        found = await run_in_threadpool(batches, s) if cards.own(s) else []
        batch = next(
            (b for b in found if name.endswith(".pdf") and key.isdigit() and b["key"] == int(key)),
            None,
        )
        if batch is None:
            body = "<h1>Printed cards</h1><p>No such batch.</p>"
            return HTMLResponse(console.frame("Printed cards", PATH, me, body), status_code=404)
        data = await run_in_threadpool(pdf_of, s, batch)
        return Response(
            data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{cardspdf.filename(batch["name"])}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def admin_form(request: Request) -> tuple[dict[str, Any] | None, Any]:
        form = await request.form()
        me = await who(request)
        ok = (
            me is not None
            and me["admin"]
            and pysecrets.compare_digest(str(form.get("csrf") or ""), me["csrf"] or "x")
        )
        return (me if ok else None), form

    def number(form: Any, name: str) -> int:
        try:
            return int(str(form.get(name) or ""))
        except ValueError:
            return 0

    @app.post("/console/cards/make")
    async def cards_make(request: Request):
        """Generate PDF: the batch is made and its PDF opens (in the form's own new tab)."""
        me, form = await admin_form(request)
        if me is None:
            return back(request, err="expired")
        s = svc(request)
        if not cards.own(s):
            return back(request, err="unsupported")
        kind = str(form.get("kind") or "")
        if kind not in cards.KINDS:
            return back(request, err="kind")
        per_page = number(form, "per_page")
        if per_page not in PER_PAGE:
            return back(request, err="per_page")
        pages = number(form, "pages")
        if not 1 <= pages <= PAGES_MAX:
            return back(request, err="pages")
        join_as = str(form.get("join_as") or "member")  # an older form sends none
        if join_as not in JOIN_AS:
            return back(request, err="join_as")
        name = str(form.get("batch") or "")
        key = await run_in_threadpool(
            make, s, kind, name, pages * per_page, int(me["partner"]), per_page, join_as
        )
        return RedirectResponse(f"{PATH}/batch/{key}.pdf", status_code=303)

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


__all__ = ["register", "page_html", "pdf_of", "batches", "make"]
