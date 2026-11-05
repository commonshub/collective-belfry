"""``belfry.<domain>/setup``: set what the belfry still needs (mail, payments, accounting, name,
theme, homepage, code) over HTTPS, without SSH.

Who may use it: an admin signed in on ``belfry.<domain>`` (the stewards group), or anyone with
the setup code. Before mail works nobody can sign in, so the installer prints a code
(``belfry setup-code`` prints a new one at any time over SSH); it is valid for 24 hours and only
its SHA-256 is stored (``code.json`` in SETUP_DIR).

How a value gets to the server, with no Docker socket and no write access to ``infra/.env`` in
this container: the page writes ``pending.env`` (BELFRY_* lines, the format of ``belfry config``)
into SETUP_DIR (``infra/data/setup``); a systemd path unit on the host sees it and runs
``belfry setup-apply``, which takes only the keys in FIELDS, runs ``belfry config`` (it writes
``.env`` and ``infra/secrets/``, records the change in the changelog and restarts what changed) and
writes ``status.json``: which settings are set (never their values for secrets), the non-secret
values, and the result of the last apply. The page shows that file; secrets are write-only.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import console, mypass
from .core import Services

log = logging.getLogger(__name__)
E = html.escape

COOKIE = "belfry_setup"
SESSION_SECONDS = 3600

# (key, label, secret, help); keys are the names `belfry config` takes. Must match SETUP_KEYS in
# infra/scripts/belfry, which filters pending.env again on the host.
GROUPS: list[tuple[str, str, list[tuple[str, str, bool, str]]]] = [
    (
        "Mail",
        "Sign-in links and notices go out by e-mail; nobody can sign in until this works.",
        [
            ("BELFRY_RESEND_API_KEY", "Resend API key", True, "re_…, from resend.com/api-keys"),
            (
                "BELFRY_MAIL_FROM",
                "Sender address",
                False,
                "e.g. hello@yourdomain.org, on a domain verified in Resend",
            ),
        ],
    ),
    (
        "Community",
        "How the belfry names itself and what it shows.",
        [
            ("BELFRY_NAME", "Community name", False, "on every page and in the mails"),
            ("BELFRY_THEME", "Theme", False, "belfry or osv; colours and logo: belfry theme set"),
            (
                "BELFRY_ENV_HOMEPAGE",
                "Homepage",
                False,
                "nsite (published with the belfry key), nsite:npub…, or an https:// site to proxy",
            ),
            (
                "BELFRY_GIT_REPO",
                "Code shown at /git",
                False,
                "nostr://npub…/relay.example/repo (ngit), or empty for no /git",
            ),
        ],
    ),
    (
        "Payments",
        "Only for paid passes.",
        [
            (
                "BELFRY_STRIPE_PUBLISHABLE_KEY",
                "Stripe publishable key",
                False,
                "pk_test_… / pk_live_…",
            ),
            ("BELFRY_STRIPE_SECRET_KEY", "Stripe secret key", True, "sk_… or a restricted rk_…"),
            ("BELFRY_MOLLIE_API_KEY", "Mollie API key", True, "test_… / live_…"),
        ],
    ),
    (
        "Accounting",
        "Book paid passes in your organisation's own Odoo (the bundled Odoo needs nothing).",
        [
            ("BELFRY_ACCOUNTING_ODOO_URL", "Odoo URL", False, "https://yourorg.odoo.com"),
            ("BELFRY_ACCOUNTING_ODOO_DB", "Odoo database", False, ""),
            ("BELFRY_ACCOUNTING_ODOO_ISSUER", "Issuer (company) name", False, ""),
            ("BELFRY_ACCOUNTING_ODOO_API_KEY", "Odoo API key", True, ""),
        ],
    ),
]
FIELDS = {k: (label, secret) for _, _, fields in GROUPS for k, label, secret, _ in fields}
MAX_VALUE = 1000


def setup_dir(settings: Any) -> Path | None:
    d = getattr(settings, "setup_dir", "") or ""
    return Path(d) if d and Path(d).is_dir() else None


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def code_ok(settings: Any, code: str) -> bool:
    folder = setup_dir(settings)
    if folder is None:
        return False
    data = read_json(folder / "code.json")
    want, until = str(data.get("sha256", "")), data.get("expires", 0)
    norm = re.sub(r"[^A-Z0-9]", "", code.upper())
    if not want or not norm or not isinstance(until, (int, float)) or until < time.time():
        return False
    return hmac.compare_digest(hashlib.sha256(norm.encode()).hexdigest(), want)


def clean(value: str) -> str:
    value = value.strip()
    if len(value) > MAX_VALUE or any(c in value for c in "\r\n\0"):
        raise ValueError("a value is too long or has a line break")
    if "'" in value and '"' in value:
        raise ValueError("a value can't hold both kinds of quotes")
    return value


def line(key: str, value: str) -> str:
    quote = '"' if "'" in value else "'"
    return f"{key}={quote}{value}{quote}\n"


def write_pending(settings: Any, values: dict[str, str], by: str) -> list[str]:
    """Merge VALUES into pending.env (atomically, mode 660); returns the keys written."""
    folder = setup_dir(settings)
    if folder is None:
        raise ValueError("this belfry has no setup folder (SETUP_DIR); use belfry config over SSH")
    pending = folder / "pending.env"
    merged: dict[str, str] = {}
    if pending.is_file():
        for raw in pending.read_text().splitlines():
            m = re.match(r"^(BELFRY_[A-Z0-9_]+)=(['\"])(.*)\2$", raw)
            if m and m.group(1) in FIELDS:
                merged[m.group(1)] = m.group(3)
    for k, v in values.items():
        if k not in FIELDS:
            raise ValueError(f"unknown setting {k}")
        merged[k] = clean(v)
    if not merged:
        return []
    by = clean(by)[:120] or "the setup page"
    text = "".join(line(k, v) for k, v in merged.items()) + line("BELFRY_BY", by)
    tmp = folder / f".pending.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, pending)
    return sorted(merged)


def state(settings: Any) -> dict[str, Any]:
    folder = setup_dir(settings)
    if folder is None:
        return {"available": False}
    status = read_json(folder / "status.json")
    return {
        "available": True,
        "pending": (folder / "pending.env").is_file(),
        "set": status.get("set") or {},
        "values": status.get("values") or {},
        "last": status.get("last") or {},
        "applying": bool(status.get("applying")),
    }


# ── pages ───────────────────────────────────────────────────────────────────────────────────
STYLE = """<style>
fieldset{border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin:0 0 14px}
legend{font-weight:600;padding:0 6px} label{display:block;margin:10px 0 4px;font-weight:500}
input[type=password]{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;border-radius:8px;
border:1px solid var(--border);background:var(--surface-deep);color:inherit}
.set{color:var(--ok,var(--accent))} .missing{color:var(--accent-2)}
.note{padding:10px 12px;border-radius:8px;background:var(--surface-deep);margin:0 0 14px}
</style>"""


def code_form(err: str = "") -> str:
    error = f'<p class="note bad">{E(err)}</p>' if err else ""
    return f"""{error}<h1>Set up this belfry</h1>
<p class="muted">Type the setup code the installer printed. On the server, <code>belfry setup-code</code>
prints a new one (valid 24 hours). Admins who can already sign in can <a href="/login?to=/setup">sign in</a>
instead.</p>
<form method="post" action="/setup/code" class="row">
<input type="text" name="code" autocomplete="one-time-code" placeholder="XXXX-XXXX-XXXX-XXXX" required style="max-width:280px">
<button class="btn" type="submit">Continue</button></form>"""


def settings_form(st: dict[str, Any], csrf: str, msg: str, err: str) -> str:
    notes = ""
    if err:
        notes += f'<p class="note bad">{E(err)}</p>'
    if msg:
        notes += f'<p class="note">{E(msg)}</p>'
    if st["pending"] or st["applying"]:
        notes += (
            '<p class="note">Applying the changes: the belfry restarts what changed, which can take '
            "a few minutes. This page reloads by itself.</p>"
        )
    last = st["last"]
    if last and not st["pending"] and not st["applying"]:
        ok = "ok" if last.get("ok") else "bad"
        what = ", ".join(last.get("keys") or []) or "settings"
        notes += (
            f'<p class="note"><span class="{ok}">{"Applied" if last.get("ok") else "Failed"}</span>'
            f" {E(what)} · {E(str(last.get('at', '')))}"
            + (
                f"<br><span class='meta'>{E(str(last.get('message', '')))}</span>"
                if not last.get("ok")
                else ""
            )
            + "</p>"
        )
    groups = ""
    for title, about, fields in GROUPS:
        rows = ""
        for key, label, secret, help_ in fields:
            is_set = bool(st["set"].get(key))
            badge = (
                '<span class="set">set</span>' if is_set else '<span class="missing">missing</span>'
            )
            if secret:
                hint = "set: leave empty to keep it" if is_set else help_
                field = (
                    f'<input type="password" name="{key}" autocomplete="off" '
                    f'placeholder="{E(hint)}">'
                )
            else:
                current = str(st["values"].get(key, ""))
                field = f'<input type="text" name="{key}" value="{E(current)}" placeholder="{E(help_)}">'
            rows += f"<label>{E(label)} · {badge}</label>{field}"
            if help_ and not secret:
                rows += f'<div class="meta">{E(help_)}</div>'
        groups += f'<fieldset><legend>{E(title)}</legend><div class="meta">{E(about)}</div>{rows}</fieldset>'
    return f"""{notes}<h1>Setup</h1>
<p class="muted">Secrets are sent over HTTPS, written to the server's secret files and never shown
again. Each change is recorded in the <a href="/changelog">changelog</a> with who made it.</p>
<form method="post" action="/setup">
<input type="hidden" name="csrf" value="{E(csrf)}">
{groups}
<label>Who is making this change</label><input type="text" name="by" placeholder="your name (shown in the changelog)">
<p><button class="btn" type="submit">Save and apply</button></p></form>"""


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    async def admin(request: Request) -> dict[str, Any] | None:
        services = svc(request)
        sess = console.read_session(services, request.cookies.get(console.COOKIE))
        if not sess:
            return None
        who = await run_in_threadpool(console.viewer, services, sess)
        return who if who and who.get("admin") else None

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        return not origin or origin == mypass._origin(console.console_url(svc(request).settings))

    def secure(request: Request) -> bool:
        return console.console_url(svc(request).settings).startswith("https://")

    def code_session(request: Request) -> dict[str, Any] | None:
        services = svc(request)
        if not mypass.enabled(services):
            return None
        return mypass._verify(services, request.cookies.get(COOKIE), "setup")

    async def allowed(request: Request) -> tuple[str, str] | None:
        """(csrf, who) when this browser may change settings."""
        who = await admin(request)
        if who is not None:
            return str(who.get("csrf", "")), who.get("name") or who.get("email") or "an admin"
        sess = code_session(request)
        if sess is not None:
            return str(sess.get("csrf", "")), "the setup code"
        return None

    def page_(body: str, refresh: bool = False) -> HTMLResponse:
        head = '<meta http-equiv="refresh" content="10">' if refresh else ""
        return HTMLResponse(
            console.frame("Setup", "/setup", None, STYLE + head + body),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/console/setup", response_class=HTMLResponse)
    async def setup_page(request: Request, msg: str = "", err: str = ""):
        services = svc(request)
        st = state(services.settings)
        if not st["available"]:
            return page_(
                "<h1>Setup</h1><p>This belfry has no setup folder. Use <code>belfry config</code> on "
                "the server.</p>"
            )
        ok = await allowed(request)
        if ok is None:
            return page_(code_form(err[:200]))
        return page_(
            settings_form(st, ok[0], msg[:200], err[:200]),
            refresh=st["pending"] or st["applying"],
        )

    @app.post("/console/setup/code")
    async def setup_code(request: Request, code: str = Form("")):
        services = svc(request)
        if not same_origin(request) or not code_ok(services.settings, code[:64]):
            log.warning(
                "setup: wrong or expired code from %s",
                request.client.host if request.client else "?",
            )
            return RedirectResponse("/setup?err=That+code+is+wrong+or+expired.", status_code=303)
        cookie = mypass._sign(
            services, {"kind": "setup", "csrf": secrets.token_urlsafe(24)}, SESSION_SECONDS
        )
        response = RedirectResponse("/setup", status_code=303)
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure(request),
            samesite="strict",
            path="/",
        )
        log.info("setup: signed in with the setup code")
        return response

    @app.post("/console/setup")
    async def setup_save(request: Request) -> Response:
        services = svc(request)
        ok = await allowed(request)
        form = await request.form()
        if ok is None or not same_origin(request):
            return RedirectResponse("/setup", status_code=303)
        csrf, who = ok
        if not csrf or not hmac.compare_digest(str(form.get("csrf", "")), csrf):
            return RedirectResponse("/setup?err=The+form+expired%3B+try+again.", status_code=303)
        st = state(services.settings)
        values: dict[str, str] = {}
        for key, (_, secret) in FIELDS.items():
            raw = form.get(key)
            if not isinstance(raw, str):
                continue
            raw = raw.strip()
            if secret and not raw:
                continue  # empty secret field: keep what is set
            if not secret and raw == str(st["values"].get(key, "")):
                continue  # unchanged
            if not secret and not raw:
                continue  # clearing a value is done with belfry config
            values[key] = raw
        if not values:
            return RedirectResponse("/setup?msg=Nothing+changed.", status_code=303)
        by = str(form.get("by", "") or "").strip()
        by = f"{by} via {who}" if by else who
        try:
            keys = write_pending(services.settings, values, by)
        except ValueError as exc:
            return RedirectResponse(
                "/setup?" + urllib.parse.urlencode({"err": str(exc)}),
                status_code=303,
            )
        log.info("setup: %s queued %s", who, ", ".join(keys))
        return RedirectResponse("/setup?msg=Saved.", status_code=303)
