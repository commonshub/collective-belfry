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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import console, mypass, nostr
from .blossom import verify_signature
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


# ── the first admin ─────────────────────────────────────────────────────────────────────────
# On a new belfry the setup page creates the first admin: their e-mail address and username, and
# a session key made in their browser. They become a steward (free pass, Keycast account named
# <username>, so <username>@<domain> is their NIP-05), the browser key is their first NIP-46
# session on that account, and the address waits for a verification code until mail works.
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,29}$")
FIRST_ADMIN = "first-admin.json"


def first_admin(settings: Any) -> dict[str, Any]:
    folder = setup_dir(settings)
    return read_json(folder / FIRST_ADMIN) if folder else {}


def save_first_admin(settings: Any, data: dict[str, Any]) -> None:
    folder = setup_dir(settings)
    if folder is None:
        raise ValueError("no setup folder")
    tmp = folder / f".{FIRST_ADMIN}.{secrets.token_hex(4)}"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, folder / FIRST_ADMIN)


def has_admin(svc: Services) -> bool:
    if first_admin(svc.settings):
        return True
    try:
        return bool(svc.authentik.group_member_emails(svc.settings.steward_group))
    except Exception:  # noqa: BLE001 — Authentik not answering: don't offer a second first admin
        return True


def create_first_admin(
    svc: Services, email: str, username: str, session_pubkey: str
) -> dict[str, Any]:
    from . import steward

    email = email.strip()
    username = username.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        raise ValueError("That is not an e-mail address.")
    if not USERNAME.fullmatch(username):
        raise ValueError("A username is 1 to 30 letters, digits, dots, dashes or underscores.")
    if not re.fullmatch(r"[0-9a-f]{64}", session_pubkey or ""):
        raise ValueError("The browser did not make a session key.")
    if has_admin(svc):
        raise ValueError("This belfry has an admin already.")
    made = steward.add_steward(svc, email, username)
    row = svc.odoo.read_one(
        "belfry.pass", made["pass"]["pass_id"], ["partner_id", "keycast_pubkey", "state"]
    )
    if not row or row.get("state") != "active" or not row.get("keycast_pubkey"):
        raise ValueError("The admin's pass did not open; see the logs and try again.")
    partner = (
        row["partner_id"][0] if isinstance(row["partner_id"], list) else int(row["partner_id"])
    )
    result = svc.keycast.preload_user(
        vine_id=f"odoo-partner-{partner}", username=username, display_name=username
    )
    pubkey, token = result["pubkey"], result["token"]
    svc.keycast.set_username(token, username)
    bunker = svc.keycast.create_bunker(token, "Setup page (first session)")
    from .nip46check import parse_bunker_url

    bunker_pk, relays, secret = parse_bunker_url(bunker["bunker_url"])
    data = {
        "partner": partner,
        "email": email,
        "username": username,
        "pubkey": pubkey,
        "nip05": f"{username}@{svc.settings.nip05_domain}",
        "session_pubkey": session_pubkey,
        "bunker_pubkey": bunker_pk,
        "verified": False,
        "code_sent_at": 0,
        "at": int(time.time()),
    }
    save_first_admin(svc.settings, data)
    log.info("setup: first admin %s (%s), session key %s", username, email, session_pubkey[:12])
    return {
        **data,
        "npub": nostr.hex_to_npub(pubkey),
        "bunker_url": bunker["bunker_url"],
        "relays": relays,
        "secret": secret,
    }


def relay_connect(settings: Any, event: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """Publish the browser's NIP-46 connect request on the bunker relay and return the bunker's
    answer (kind 24133 for the session key), so the page needs no WebSocket of its own."""
    from websockets.sync.client import connect

    url = getattr(settings, "session_relay_url", "")
    if not url:
        raise ValueError("no bunker relay (SESSION_RELAY_URL)")
    bunker = next((t[1] for t in event.get("tags", []) if t[:1] == ["p"]), "")
    with connect(url, open_timeout=timeout) as ws:
        ws.send(
            json.dumps(
                [
                    "REQ",
                    "c",
                    {
                        "kinds": [24133],
                        "authors": [bunker],
                        "#p": [event["pubkey"]],
                        "since": int(time.time()) - 10,
                    },
                ]
            )
        )
        ws.send(json.dumps(["EVENT", event]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if msg[0] == "EVENT" and msg[1] == "c":
                return msg[2]
            if msg[0] == "OK" and msg[1] == event["id"] and not msg[2]:
                raise ValueError(f"the relay refused the request: {msg[3] if len(msg) > 3 else ''}")
    raise ValueError("the bunker did not answer")


def send_admin_code(svc: Services) -> dict[str, Any]:
    from . import email_code

    fa = first_admin(svc.settings)
    r = email_code.send(svc, fa["pubkey"], {"email": fa["email"], "app": "Setup"})
    fa["code_sent_at"] = int(time.time())
    save_first_admin(svc.settings, fa)
    return r


def verify_admin_code(svc: Services, code: str) -> None:
    from . import email_code

    fa = first_admin(svc.settings)
    email_code.verify(svc, fa["pubkey"], {"email": fa["email"], "code": code})
    fa["verified"] = True
    fa["verified_at"] = int(time.time())
    save_first_admin(svc.settings, fa)
    log.info("setup: the first admin's address %s is verified", fa["email"])


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
input[type=email]{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;border-radius:8px;
border:1px solid var(--border);background:var(--surface-deep);color:inherit}
.linkish{background:none;border:0;padding:0;color:var(--accent);text-decoration:underline;font:inherit;font-size:.9rem}
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


FIRST_ADMIN_JS = """<script src="/static/nostr-tools-2.25.2.js"></script>
<script>
(() => {
  const f = document.getElementById('first');
  if (!f) return;
  const email = f.elements.email, user = f.elements.username, out = document.getElementById('first-msg');
  let edited = false;
  user.addEventListener('input', () => { edited = true; });
  email.addEventListener('input', () => {
    if (!edited) user.value = (email.value.split('@')[0] || '').toLowerCase().replace(/[^a-z0-9._-]/g, '').slice(0, 30);
  });
  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    const T = window.NostrTools, btn = f.querySelector('button');
    btn.disabled = true; out.textContent = 'Making your session key and your account…';
    try {
      const sk = T.generateSecretKey(), pk = T.getPublicKey(sk);
      const keep = { nsec: T.nip19.nsecEncode(sk) };
      try { localStorage.setItem('belfry:session', JSON.stringify(keep)); } catch (_) {}
      const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ csrf: f.elements.csrf.value, ...body }) }).then(async r => {
          const d = await r.json(); if (!r.ok) throw new Error(d.message || 'failed'); return d; });
      const a = await post('/setup/first-admin', { email: email.value, username: user.value, session_pubkey: pk });
      out.textContent = 'Connecting this browser as your first session…';
      const conv = T.nip44.getConversationKey(sk, a.bunker_pubkey);
      const id = Math.random().toString(36).slice(2);
      const ev = T.finalizeEvent({ kind: 24133, created_at: Math.floor(Date.now() / 1000), tags: [['p', a.bunker_pubkey]],
        content: T.nip44.encrypt(JSON.stringify({ id, method: 'connect', params: [a.bunker_pubkey, a.secret] }), conv) }, sk);
      let connected = false;
      try {
        const r = await post('/setup/first-admin/connect', { event: ev });
        const reply = JSON.parse(T.nip44.decrypt(r.reply.content, conv));
        connected = !reply.error;
      } catch (_) {}
      Object.assign(keep, { npub: a.npub, nip05: a.nip05, bunker: a.bunker_url, connected });
      try { localStorage.setItem('belfry:session', JSON.stringify(keep)); } catch (_) {}
      location.href = '/setup?msg=' + encodeURIComponent('You are the first admin: ' + a.nip05 + '.') + '&open=resend';
    } catch (err) {
      out.textContent = String(err.message || err); btn.disabled = false;
    }
  });
})();
</script>"""


def first_admin_form(csrf: str, domain: str) -> str:
    return f"""<h1>Welcome to your belfry</h1>
<p class="muted">First, become its admin. Your browser makes a key for this session; your account
is <em>username</em>@{E(domain)}, a Nostr address members and apps can find you by.</p>
<form id="first">
<input type="hidden" name="csrf" value="{E(csrf)}">
<label>Your e-mail address</label><input type="email" name="email" required autocomplete="email">
<label>Username</label><div class="row" style="flex-wrap:nowrap"><input type="text" name="username" required pattern="[a-z0-9][a-z0-9._-]{{0,29}}" autocomplete="username"><span class="meta">@{E(domain)}</span></div>
<p><button class="btn" type="submit">Create my admin account</button></p>
<p id="first-msg" class="meta" role="status"></p></form>
{FIRST_ADMIN_JS}"""


def admin_banner(fa: dict[str, Any], st: dict[str, Any], csrf: str) -> str:
    if not fa or fa.get("verified"):
        return ""
    who = f"<strong>{E(fa.get('nip05', ''))}</strong> ({E(fa.get('email', ''))})"
    if not st["set"].get("BELFRY_RESEND_API_KEY"):
        return (
            '<div class="note"><strong>One step before your belfry can be used: add a Resend API key.</strong> '
            "Members sign in with a code sent by e-mail, so nothing works until mail does. "
            f"Your admin account {who} is ready; its address is verified as soon as mail works. "
            '<a href="/setup?open=resend#resend">Add Resend</a></div>'
        )
    sent = "We sent a code to your address. " if fa.get("code_sent_at") else ""
    return f"""<div class="note"><strong>Verify your address.</strong> {sent}Type the 6-digit code
that went to {E(fa.get("email", ""))}.
<form method="post" action="/setup/verify" class="row" style="margin-top:8px">
<input type="hidden" name="csrf" value="{E(csrf)}">
<input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{{6,7}}" required style="max-width:140px">
<button class="btn" type="submit">Verify</button></form>
<form method="post" action="/setup/verify/send" style="margin-top:6px"><input type="hidden" name="csrf" value="{E(csrf)}">
<button type="submit" class="linkish">Send a new code</button></form></div>"""


def settings_form(
    st: dict[str, Any],
    csrf: str,
    who: str,
    msg: str,
    err: str,
    open_: str = "",
    banner: str = "",
) -> str:
    notes = banner
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

    def start_session(request: Request, who: str, to: str) -> RedirectResponse:
        cookie = mypass._sign(
            svc(request),
            {"kind": "setup", "csrf": secrets.token_urlsafe(24), "who": who},
            SESSION_SECONDS,
        )
        response = RedirectResponse(to, status_code=303)
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure(request),
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/console/setup", response_class=HTMLResponse)
    async def setup_page(request: Request, msg: str = "", err: str = "", code: str = ""):
        services = svc(request)
        st = state(services.settings)
        if not st["available"]:
            return page_(
                "<h1>Setup</h1><p>This belfry has no setup folder. Use <code>belfry config</code> on "
                "the server.</p>"
            )
        if code:
            # the link the installer prints: belfry.<domain>/setup?code=…
            entry = match_code(services.settings, code[:64])
            if entry is None:
                return RedirectResponse(
                    "/setup?err=That+link+is+wrong+or+expired.", status_code=303
                )
            who = " ".join(str(entry.get("for") or "").split())[:MAX_NAME]
            return start_session(request, who, "/setup")
        ok = await allowed(request)
        if ok is None:
            return page_(code_form(err[:200]))
        csrf, who = ok
        if not await run_in_threadpool(has_admin, services):
            notes = f'<p class="note bad">{E(err[:200])}</p>' if err else ""
            return page_(notes + first_admin_form(csrf, services.settings.nip05_domain))
        fa = first_admin(services.settings)
        if (
            fa
            and not fa.get("verified")
            and not fa.get("code_sent_at")
            and st["set"].get("BELFRY_RESEND_API_KEY")
            and not st["pending"]
            and not st["applying"]
        ):
            # mail works now: the first admin's code goes out once by itself
            try:
                await run_in_threadpool(send_admin_code, services)
                fa = first_admin(services.settings)
            except Exception as exc:  # noqa: BLE001
                log.warning("setup: the first admin's code was not sent: %s", exc)
        return page_(
            settings_form(
                st,
                csrf,
                who or fa.get("username", "") or "the setup code",
                msg[:200],
                err[:200],
                request.query_params.get("open", ""),
                admin_banner(fa, st, csrf),
            ),
            refresh=st["pending"] or st["applying"],
        )

    async def json_body(request: Request) -> tuple[tuple[str, str] | None, dict[str, Any]]:
        ok = await allowed(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if (
            ok is None
            or not same_origin(request)
            or not ok[0]
            or not hmac.compare_digest(str(body.get("csrf", "")), ok[0])
        ):
            return None, body
        return ok, body

    @app.post("/console/setup/first-admin")
    async def setup_first_admin(request: Request):
        ok, body = await json_body(request)
        if ok is None:
            return JSONResponse({"message": "Open the setup link again."}, status_code=403)
        services = svc(request)
        try:
            made = await run_in_threadpool(
                create_first_admin,
                services,
                str(body.get("email", "")),
                str(body.get("username", "")),
                str(body.get("session_pubkey", "")),
            )
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=422)
        except Exception as exc:  # noqa: BLE001
            log.exception("setup: first admin failed")
            return JSONResponse({"message": f"That failed: {str(exc)[:200]}"}, status_code=500)
        response = JSONResponse(
            {
                k: made[k]
                for k in (
                    "npub",
                    "nip05",
                    "bunker_url",
                    "bunker_pubkey",
                    "secret",
                    "relays",
                    "username",
                )
            }
        )
        # this browser is signed in to belfry.<domain> as the admin, and its setup session
        # carries their name
        cookie = mypass._sign(
            services,
            # the page's token, so its next calls (connect) pass as the admin's
            {"kind": "console", "partner": made["partner"], "csrf": ok[0]},
            services.settings.pass_page_session_days * 86400,
        )
        response.set_cookie(
            console.COOKIE,
            cookie,
            max_age=services.settings.pass_page_session_days * 86400,
            httponly=True,
            secure=secure(request),
            samesite="lax",
            path="/",
        )
        setup_cookie = mypass._sign(
            services, {"kind": "setup", "csrf": ok[0], "who": made["username"]}, SESSION_SECONDS
        )
        response.set_cookie(
            COOKIE,
            setup_cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure(request),
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/console/setup/first-admin/connect")
    async def setup_first_connect(request: Request):
        ok, body = await json_body(request)
        if ok is None:
            return JSONResponse({"message": "Open the setup link again."}, status_code=403)
        services = svc(request)
        fa = first_admin(services.settings)
        event = body.get("event")
        if (
            not isinstance(event, dict)
            or event.get("kind") != 24133
            or event.get("pubkey") != fa.get("session_pubkey")
            or ["p", fa.get("bunker_pubkey")] not in event.get("tags", [])
            or not verify_signature(event)
        ):
            return JSONResponse(
                {"message": "not the first session's connect request"}, status_code=422
            )
        try:
            reply = await run_in_threadpool(relay_connect, services.settings, event)
        except Exception as exc:  # noqa: BLE001
            log.warning("setup: first session connect: %s", exc)
            return JSONResponse({"message": str(exc)[:200]}, status_code=502)
        fa["session_connected"] = True
        save_first_admin(services.settings, fa)
        return JSONResponse({"reply": reply})

    @app.post("/console/setup/verify/send")
    async def setup_verify_send(request: Request, csrf: str = Form("")):
        ok = await allowed(request)
        if ok is None or not same_origin(request) or not hmac.compare_digest(csrf, ok[0] or "x"):
            return RedirectResponse("/setup", status_code=303)
        try:
            await run_in_threadpool(send_admin_code, svc(request))
        except Exception as exc:  # noqa: BLE001
            return RedirectResponse(
                "/setup?"
                + urllib.parse.urlencode({"err": getattr(exc, "message", str(exc))[:200]}),
                status_code=303,
            )
        return RedirectResponse("/setup?msg=A+new+code+is+on+its+way.", status_code=303)

    @app.post("/console/setup/verify")
    async def setup_verify(request: Request, csrf: str = Form(""), code: str = Form("")):
        ok = await allowed(request)
        if ok is None or not same_origin(request) or not hmac.compare_digest(csrf, ok[0] or "x"):
            return RedirectResponse("/setup", status_code=303)
        try:
            await run_in_threadpool(verify_admin_code, svc(request), code.strip()[:12])
        except Exception as exc:  # noqa: BLE001
            return RedirectResponse(
                "/setup?"
                + urllib.parse.urlencode({"err": getattr(exc, "message", str(exc))[:200]}),
                status_code=303,
            )
        return RedirectResponse(
            "/setup?msg=Your+address+is+verified.+Your+belfry+is+ready%3A+members+and+apps+can+sign+in.",
            status_code=303,
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
