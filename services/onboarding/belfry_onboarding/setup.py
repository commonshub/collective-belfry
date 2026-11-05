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

# Providers, each folded until opened: (id, name, what it is for, fields). A field is (key, label,
# secret, help); keys are the names `belfry config` takes. Must match SETUP_KEYS in
# infra/scripts/belfry, which filters pending.env again on the host.
PROVIDERS: list[tuple[str, str, str, list[tuple[str, str, bool, str]]]] = [
    (
        "resend",
        "Resend",
        "E-mail: sign-in links and notices. Nobody can sign in until this works.",
        [
            ("BELFRY_RESEND_API_KEY", "API key", True, "re_…, from resend.com/api-keys"),
            (
                "BELFRY_MAIL_FROM",
                "Sender address",
                False,
                "e.g. hello@yourdomain.org, on a domain verified in Resend",
            ),
        ],
    ),
    (
        "name",
        "Community name",
        "On every page and in the mails.",
        [("BELFRY_NAME", "Name", False, "")],
    ),
    (
        "theme",
        "Theme",
        "The pages' base look; your own colours and logo: belfry theme set.",
        [("BELFRY_THEME", "Theme", False, "belfry or osv")],
    ),
    (
        "homepage",
        "Homepage",
        "What https://<domain> shows.",
        [
            (
                "BELFRY_ENV_HOMEPAGE",
                "Homepage",
                False,
                "nsite (published with the belfry key), nsite:npub…, or an https:// site to proxy",
            )
        ],
    ),
    (
        "git",
        "Code (/git)",
        "The repository shown at /git.",
        [
            (
                "BELFRY_GIT_REPO",
                "Repository",
                False,
                "nostr://npub…/relay.example/repo, or https://…",
            )
        ],
    ),
    (
        "stripe",
        "Stripe",
        "Card payments for paid passes.",
        [
            ("BELFRY_STRIPE_PUBLISHABLE_KEY", "Publishable key", False, "pk_test_… / pk_live_…"),
            ("BELFRY_STRIPE_SECRET_KEY", "Secret key", True, "sk_… or a restricted rk_…"),
        ],
    ),
    (
        "mollie",
        "Mollie",
        "Payments for paid passes (Bancontact, iDEAL, cards).",
        [("BELFRY_MOLLIE_API_KEY", "API key", True, "test_… / live_…")],
    ),
    (
        "odoo",
        "Odoo accounting",
        "Book paid passes in your organisation's own Odoo (the bundled Odoo needs nothing).",
        [
            ("BELFRY_ACCOUNTING_ODOO_URL", "Odoo URL", False, "https://yourorg.odoo.com"),
            ("BELFRY_ACCOUNTING_ODOO_DB", "Database", False, ""),
            ("BELFRY_ACCOUNTING_ODOO_ISSUER", "Issuer (company) name", False, ""),
            ("BELFRY_ACCOUNTING_ODOO_API_KEY", "API key", True, ""),
        ],
    ),
]
FIELDS = {k: (label, secret) for _, _, _, fields in PROVIDERS for k, label, secret, _ in fields}
MAX_NAME = 120
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


def match_code(settings: Any, code: str) -> dict[str, Any] | None:
    """The unexpired code entry ({"sha256", "expires", "for"}) CODE matches, or None. code.json
    holds {"codes": [...]}, one per person it was made for (`belfry setup-code NAME`)."""
    folder = setup_dir(settings)
    if folder is None:
        return None
    data = read_json(folder / "code.json")
    entries = data.get("codes") if isinstance(data.get("codes"), list) else [data]
    norm = re.sub(r"[^A-Z0-9]", "", code.upper())
    if not norm:
        return None
    digest = hashlib.sha256(norm.encode()).hexdigest()
    found = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        want, until = str(entry.get("sha256", "")), entry.get("expires", 0)
        if not want or not isinstance(until, (int, float)) or until < time.time():
            continue
        if hmac.compare_digest(digest, want):
            found = entry
    return found


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
.providers{border:1px solid var(--border);border-radius:10px;overflow:hidden;margin:16px 0}
.providers details{border-top:1px solid var(--border)} .providers details:first-child{border-top:0}
.providers summary{list-style:none;cursor:pointer;display:flex;gap:12px;align-items:center;padding:12px 14px}
.providers summary::-webkit-details-marker{display:none}
.providers summary .what{flex:1;min-width:0} .providers summary strong{display:block}
.providers summary .act{font-size:.9rem;color:var(--accent);white-space:nowrap}
.providers details[open] summary .act{visibility:hidden}
.providers form{padding:0 14px 14px} label{display:block;margin:10px 0 4px;font-weight:500}
input[type=password]{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;border-radius:8px;
border:1px solid var(--border);background:var(--surface-deep);color:inherit}
.set{color:var(--ok,var(--accent));font-size:.85rem;white-space:nowrap}
.missing{color:var(--fg-faint);font-size:.85rem;white-space:nowrap}
.note{padding:10px 12px;border-radius:8px;background:var(--surface-deep);margin:0 0 14px}
</style>"""


def code_form(err: str = "") -> str:
    error = f'<p class="note bad">{E(err)}</p>' if err else ""
    return f"""{error}<h1>Set up this belfry</h1>
<p class="muted">Type the setup code you were given. On the server, <code>belfry setup-code "Your name"</code>
makes one for a person (valid 24 hours). Admins who can already sign in can
<a href="/login?to=/setup">sign in</a> instead.</p>
<form method="post" action="/setup/code">
<label>Setup code</label>
<input type="text" name="code" autocomplete="one-time-code" placeholder="XXXX-XXXX-XXXX-XXXX" required style="max-width:280px">
<label>Your name <span class="meta">(only if the code was made without one; the changelog shows it)</span></label>
<input type="text" name="name" autocomplete="name" maxlength="{MAX_NAME}" style="max-width:280px">
<p><button class="btn" type="submit">Continue</button></p></form>"""


def provider_state(st: dict[str, Any], fields: list[tuple[str, str, bool, str]]) -> str:
    done = [bool(st["set"].get(k)) for k, *_ in fields]
    if all(done):
        return "set"
    return "partly" if any(done) else "missing"


def settings_form(
    st: dict[str, Any], csrf: str, who: str, msg: str, err: str, open_: str = ""
) -> str:
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
    rows = ""
    for pid, name, about, fields in PROVIDERS:
        state_ = provider_state(st, fields)
        badge = {
            "set": '<span class="set">✓ set</span>',
            "partly": '<span class="missing">partly set</span>',
            "missing": '<span class="missing">not set</span>',
        }[state_]
        inputs = ""
        for key, label, secret, help_ in fields:
            is_set = bool(st["set"].get(key))
            if secret:
                hint = "set: leave empty to keep it" if is_set else help_
                inputs += (
                    f'<label>{E(label)}</label><input type="password" name="{key}" '
                    f'autocomplete="off" placeholder="{E(hint)}">'
                )
            else:
                current = str(st["values"].get(key, ""))
                inputs += (
                    f'<label>{E(label)}</label><input type="text" name="{key}" '
                    f'value="{E(current)}" placeholder="{E(help_)}">'
                )
        act = "Change" if state_ == "set" else "Set up"
        rows += (
            f'<details id="{pid}"{" open" if pid == open_ else ""}><summary>'
            f'<span class="what"><strong>{E(name)}</strong><span class="meta">{E(about)}</span></span>'
            f'{badge}<span class="act">{act}</span></summary>'
            f'<form method="post" action="/setup"><input type="hidden" name="csrf" value="{E(csrf)}">'
            f'<input type="hidden" name="provider" value="{pid}">{inputs}'
            f'<p><button class="btn" type="submit">Save {E(name)}</button></p></form></details>'
        )
    return f"""{notes}<h1>Setup</h1>
<p class="muted">Open a provider to set it up. Secrets go over HTTPS to the server's secret files
and are never shown again. Changes are recorded in the <a href="/changelog">changelog</a> as made
by <strong>{E(who)}</strong>.</p>
<div class="providers">{rows}</div>"""


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
            return str(sess.get("csrf", "")), str(sess.get("who") or "someone with the setup code")
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
            settings_form(
                st, ok[0], ok[1], msg[:200], err[:200], request.query_params.get("open", "")
            ),
            refresh=st["pending"] or st["applying"],
        )

    @app.post("/console/setup/code")
    async def setup_code(request: Request, code: str = Form(""), name: str = Form("")):
        services = svc(request)
        entry = match_code(services.settings, code[:64]) if same_origin(request) else None
        if entry is None:
            log.warning(
                "setup: wrong or expired code from %s",
                request.client.host if request.client else "?",
            )
            return RedirectResponse("/setup?err=That+code+is+wrong+or+expired.", status_code=303)
        who = " ".join(str(entry.get("for") or "").split())[:MAX_NAME]
        if not who:
            who = " ".join(name.split())[:MAX_NAME]
        if not who:
            return RedirectResponse(
                "/setup?err=This+code+was+made+without+a+name%3A+give+yours+with+it.",
                status_code=303,
            )
        cookie = mypass._sign(
            services,
            {"kind": "setup", "csrf": secrets.token_urlsafe(24), "who": who},
            SESSION_SECONDS,
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
        log.info("setup: %s signed in with a setup code", who)
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
        provider = str(form.get("provider", ""))
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
        try:
            keys = write_pending(services.settings, values, who)
        except ValueError as exc:
            return RedirectResponse(
                "/setup?" + urllib.parse.urlencode({"err": str(exc)}),
                status_code=303,
            )
        log.info("setup: %s queued %s", who, ", ".join(keys))
        name = next((n for pid, n, _, _ in PROVIDERS if pid == provider), "Settings")
        return RedirectResponse(
            "/setup?" + urllib.parse.urlencode({"msg": f"{name} saved."}), status_code=303
        )
