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
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import claim, console, flash, mypass, nostr, theme
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
        "lettermint",
        "Lettermint",
        "E-mail through Lettermint instead (an EU service): one of the two is enough.",
        [
            (
                "BELFRY_LETTERMINT_API_KEY",
                "API token",
                True,
                "a project's API token, from dash.lettermint.co (SMTP turned on)",
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
    svc: Services, email: str, username: str, session_pubkey: str, community: str = ""
) -> dict[str, Any]:
    from . import steward

    community = " ".join(community.split())[:MAX_NAME]
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
    publish_profile(svc, partner, username)
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
        "community_name": community,
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


def publish_profile(svc: Services, partner: int, username: str) -> None:
    """The member's kind-0 profile with their chosen address (<username>@<domain>), signed with
    their own key, keeping what the profile already says (picture, about)."""
    from . import community

    try:
        signer = community.member_signer(svc, partner)
        if signer is None or svc.relay is None:
            return
        pubkey, sign = signer
        current: dict[str, Any] = {}
        found = community.query(svc, [{"kinds": [0], "authors": [pubkey], "limit": 1}])
        if found:
            try:
                current = json.loads(found[0].get("content") or "{}")
            except ValueError:
                current = {}
        if not isinstance(current, dict):
            current = {}
        content = {
            **current,
            "name": username,
            "display_name": current.get("display_name") or username,
            "nip05": f"{username}@{svc.settings.nip05_domain}",
        }
        event = sign(
            {"kind": 0, "created_at": int(time.time()), "tags": [], "content": json.dumps(content)}
        )
        svc.relay.publish(event, sign)
    except Exception as exc:  # noqa: BLE001 — the account works without it
        log.warning("setup: profile of %s not published: %s", username, exc)


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


RESEND_API = "https://api.resend.com"


def check_resend(key: str, domain: str) -> str:
    """Try KEY against Resend before saving it, and pick the sender: belfry@DOMAIN when DOMAIN is
    verified there, else belfry@ a related or the first verified domain. A sending-only key can't
    list domains: then belfry@DOMAIN. Raises ValueError with a message for the person."""
    key = key.strip()
    if not re.fullmatch(r"re_[A-Za-z0-9_]{8,200}", key):
        raise ValueError(
            "A Resend API key starts with re_. Copy it again from resend.com/api-keys."
        )
    default = f"belfry@{domain}"
    try:
        r = httpx.get(
            f"{RESEND_API}/domains", headers={"Authorization": f"Bearer {key}"}, timeout=10
        )
    except httpx.HTTPError as exc:
        log.warning("setup: could not reach Resend to check the key: %s", exc)
        return default
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code in (401, 403):
        if isinstance(body, dict) and body.get("name") == "restricted_api_key":
            return default
        raise ValueError("Resend did not accept this key. Copy it again from resend.com/api-keys.")
    if r.status_code >= 400:
        raise ValueError(f"Resend answered with an error ({r.status_code}). Try again in a moment.")
    rows = body.get("data") if isinstance(body, dict) else None
    verified = [
        str(d.get("name", "")).lower()
        for d in rows or []
        if isinstance(d, dict) and d.get("status") == "verified"
    ]
    if not verified:
        raise ValueError(
            f"This Resend account has no verified domain yet. Add {domain} at "
            "resend.com/domains, follow its DNS steps, then try again."
        )
    if domain in verified:
        return default
    near = [d for d in verified if domain.endswith("." + d) or d.endswith("." + domain)]
    return f"belfry@{(near or verified)[0]}"


LETTERMINT_SMTP = ("smtp.lettermint.co", 587)
MAIL_KEYS = ("BELFRY_RESEND_API_KEY", "BELFRY_LETTERMINT_API_KEY")


def check_lettermint(key: str, domain: str) -> str:
    """Try KEY (a Lettermint project's API token) by signing in to Lettermint's SMTP server, which
    sends nothing; the sender is belfry@DOMAIN, a domain verified in that Lettermint project.
    Raises ValueError with a message for the person."""
    import smtplib
    import ssl

    key = key.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:|=-]{16,300}", key):
        raise ValueError("That does not look like a Lettermint API token. Copy it again.")
    try:
        with smtplib.SMTP(*LETTERMINT_SMTP, timeout=15) as conn:
            conn.starttls(context=ssl.create_default_context())
            conn.login("lettermint", key)
    except smtplib.SMTPAuthenticationError as exc:
        raise ValueError(
            "Lettermint did not accept this token. Use a project's API token, and turn SMTP on "
            "for that project in Lettermint."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("setup: could not reach Lettermint to check the token: %s", exc)
    return f"belfry@{domain}"


SEND_TRIES = 20  # progress polls (every 3 s) before a code that won't go out counts as an error


def progress(svc: Services) -> dict[str, Any]:
    """Where the first run is: admin (no admin yet), sender (no mail), wait (applying), code
    (a code went to the admin), done, or mail_error (with a message). Sends the admin's code once
    mail works."""
    fa = first_admin(svc.settings)
    st = state(svc.settings)
    out: dict[str, Any] = {
        "email": fa.get("email", ""),
        "nip05": fa.get("nip05", ""),
        "name": fa.get("community_name")
        or st["values"].get("BELFRY_NAME")
        or svc.settings.community_name,
    }
    if not fa:
        return {**out, "state": "admin"}
    if fa.get("verified"):
        return {**out, "state": "done"}
    if st["pending"] or st["applying"]:
        return {**out, "state": "wait"}
    last = st["last"]
    if last and not last.get("ok") and set(MAIL_KEYS) & set(last.get("keys") or []):
        return {
            **out,
            "state": "mail_error",
            "message": "The belfry could not switch mail on. Check the key and try again.",
        }
    if not any(st["set"].get(k) for k in MAIL_KEYS):
        return {**out, "state": "sender"}
    if not fa.get("code_sent_at"):
        try:
            send_admin_code(svc)
        except Exception as exc:  # noqa: BLE001 — mail may still be restarting
            tries = int(fa.get("send_tries", 0)) + 1
            fa["send_tries"] = tries
            save_first_admin(svc.settings, fa)
            log.warning("setup: the first admin's code was not sent (%d): %s", tries, exc)
            if tries < SEND_TRIES:
                return {**out, "state": "wait"}
            return {
                **out,
                "state": "mail_error",
                "message": "Mail is set but the code could not be sent. Check the key and try again.",
            }
    return {**out, "state": "code"}


# ── pages ───────────────────────────────────────────────────────────────────────────────────
# Never show text taken from the URL: messages after a post ride in a signed cookie (flash.py).
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

WIZARD_STYLE = """<style>
main{max-width:520px}
.wiz{padding:32px 28px} @media (max-width:480px){.wiz{padding:24px 18px}}
.wiz .top{display:flex;align-items:center;gap:12px;margin:0 0 32px;font-size:.8rem;color:var(--fg-faint)}
.wiz .bar{flex:1;height:3px;background:var(--border);border-radius:3px;overflow:hidden}
.wiz .bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .35s ease}
.wiz h1{font-size:1.7rem;margin:0 0 8px}
.wiz section > p.muted{margin:0 0 22px}
.wiz input[type=text],.wiz input[type=email],.wiz input[type=password]{display:block;width:100%;box-sizing:border-box;
font:inherit;font-size:1.1rem;padding:12px 14px;border-radius:10px;border:1px solid var(--border);
background:var(--bg);color:inherit}
.wiz input:focus{outline:2px solid var(--accent);outline-offset:-1px}
.wiz .handle{display:flex;align-items:center;border:1px solid var(--border);border-radius:10px;background:var(--bg)}
.wiz .handle:focus-within{outline:2px solid var(--accent);outline-offset:-1px}
.wiz .handle input{border:0;background:transparent;outline:0;min-width:0;flex:1}
.wiz .handle span{padding:0 14px 0 0;color:var(--fg-muted);font-size:1.1rem;white-space:nowrap}
.wiz .actions{display:flex;align-items:center;gap:18px;margin-top:22px}
.wiz .btn{font:inherit;font-weight:600;font-size:1rem;padding:12px 24px;border-radius:10px;cursor:pointer}
.wiz .btn[disabled]{opacity:.55;cursor:default}
.wiz .quiet{background:none;border:0;padding:0;color:var(--fg-muted);font:inherit;font-size:.95rem;cursor:pointer}
.wiz .quiet:hover{color:var(--fg)}
.wiz .err{color:var(--accent-2);margin:12px 0 0;font-size:.95rem} .wiz .err:empty{display:none}
.wiz .option{display:flex;gap:14px;align-items:center;padding:16px;border:2px solid var(--accent);border-radius:12px;
background:var(--bg);cursor:pointer;font-weight:400;margin:0}
.wiz .option input{accent-color:var(--accent);width:18px;height:18px;margin:0}
.wiz .option strong{display:block} .wiz .option small{color:var(--fg-muted)}
.wiz .otp{display:flex;gap:8px}
.wiz .otp input{flex:1;min-width:0;max-width:58px;height:64px;padding:0;text-align:center;
font:600 1.6rem/1 var(--font-mono);border-radius:10px}
.wiz .otp .sep{width:6px;flex:none}
.wiz .spin{width:34px;height:34px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;
animation:wizspin 1s linear infinite;margin:6px 0 22px}
@keyframes wizspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.wiz .spin{animation-duration:4s}.wiz .bar i{transition:none}}
.wiz .done-mark{font-size:2.4rem;line-height:1;margin:0 0 12px;color:var(--ok,var(--accent))}
.wiz a.quiet{text-decoration:none}
</style>"""

WIZARD_JS = """<script src="/static/nostr-tools-2.25.2.js"></script>
<script>
(() => {
  const W = document.getElementById('wiz');
  if (!W) return;
  const csrf = W.dataset.csrf, domain = W.dataset.domain;
  const ORDER = ['name', 'email', 'username', 'sender', 'resend', 'code'];
  const data = { name: W.dataset.name || '', email: W.dataset.email || '', nip05: W.dataset.nip05 || '' };
  const $ = (q) => W.querySelector(q);
  const sec = (s) => $('section[data-step="' + s + '"]');
  const err = (s, t) => { sec(s).querySelector('.err').textContent = t || ''; };
  let polling = false;

  function show(step) {
    W.querySelectorAll('section').forEach((x) => { x.hidden = x.dataset.step !== step; });
    const at = ORDER.indexOf(step === 'wait' ? 'resend' : step);
    $('.bar i').style.width = step === 'done' ? '100%' : (Math.max(at, 0) / ORDER.length * 100) + '%';
    $('.count').textContent = step === 'done' ? 'Done' : 'Step ' + (at + 1) + ' of ' + ORDER.length;
    if (step === 'code') sec('code').querySelector('.to').textContent = data.email;
    if (step === 'done') sec('done').querySelector('.who').textContent = data.nip05;
    if (step === 'wait') poll();
    const first = sec(step).querySelector('input:not([type=radio]), button.btn');
    if (first) setTimeout(() => first.focus(), 40);
  }

  const post = (url, body) => fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
    body: JSON.stringify(Object.assign({ csrf }, body)),
  }).then(async (r) => {
    let d = {};
    try { d = await r.json(); } catch (_) {}
    if (!r.ok) { const e = new Error(d.message || 'Something went wrong. Try again.'); e.data = d; throw e; }
    return d;
  });

  function step(name, run) {
    const f = sec(name).querySelector('form');
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const b = f.querySelector('button[type=submit]');
      if (b.disabled) return;
      err(name, '');
      b.disabled = true;
      const label = b.textContent;
      try { await run(f, b); } catch (x) { err(name, x.message || String(x)); }
      finally { b.disabled = false; b.textContent = label; }
    });
  }
  W.querySelectorAll('[data-back]').forEach((b) => b.addEventListener('click', () => show(b.dataset.back)));

  const userInput = sec('username').querySelector('input');
  let userEdited = false;
  userInput.addEventListener('input', () => { userEdited = true; userInput.value = userInput.value.toLowerCase(); });
  const suggest = (email) => (email.split('@')[0] || '').toLowerCase().replace(/[^a-z0-9._-]/g, '').replace(/^[._-]+/, '').slice(0, 30);

  step('name', async (f) => {
    const v = f.elements.name.value.trim();
    if (!v) throw new Error('Give your community a name.');
    data.name = v; show('email');
  });
  step('email', async (f) => {
    const v = f.elements.email.value.trim();
    if (!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(v)) throw new Error('That does not look like an e-mail address.');
    data.email = v;
    if (!userEdited) userInput.value = suggest(v);
    show('username');
  });
  step('username', async (f, b) => {
    const u = f.elements.username.value.trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9._-]{0,29}$/.test(u)) throw new Error('Use 1 to 30 lowercase letters, digits, dots, dashes or underscores.');
    const T = window.NostrTools;
    if (!T) throw new Error('This page did not load completely. Reload it and try again.');
    b.textContent = 'Creating your account…';
    const sk = T.generateSecretKey(), pk = T.getPublicKey(sk);
    const keep = { nsec: T.nip19.nsecEncode(sk) };
    try { localStorage.setItem('belfry:session', JSON.stringify(keep)); } catch (_) {}
    const a = await post('/setup/first-admin', { name: data.name, email: data.email, username: u, session_pubkey: pk });
    data.nip05 = a.nip05;
    // this browser becomes the account's first session (NIP-46 connect, relayed by the server)
    let connected = false;
    try {
      const conv = T.nip44.getConversationKey(sk, a.bunker_pubkey);
      const id = Math.random().toString(36).slice(2);
      const ev = T.finalizeEvent({ kind: 24133, created_at: Math.floor(Date.now() / 1000), tags: [['p', a.bunker_pubkey]],
        content: T.nip44.encrypt(JSON.stringify({ id, method: 'connect', params: [a.bunker_pubkey, a.secret] }), conv) }, sk);
      const r = await post('/setup/first-admin/connect', { event: ev });
      connected = !JSON.parse(T.nip44.decrypt(r.reply.content, conv)).error;
    } catch (_) {}
    Object.assign(keep, { npub: a.npub, nip05: a.nip05, bunker: a.bunker_url, connected });
    try { localStorage.setItem('belfry:session', JSON.stringify(keep)); } catch (_) {}
    show('sender');
  });
  const PROVIDER = {
    resend: { name: 'Resend', title: 'Your Resend API key', link: 'https://resend.com/api-keys',
      where: 'resend.com/api-keys', ph: 're_…', label: 'Resend API key' },
    lettermint: { name: 'Lettermint', title: 'Your Lettermint API token', link: 'https://dash.lettermint.co',
      where: 'dash.lettermint.co (a project, its API tokens; turn SMTP on for it)', ph: 'Paste the token', label: 'Lettermint API token' },
  };
  let provider = 'resend';
  function useProvider(p) {
    provider = PROVIDER[p] ? p : 'resend';
    const P = PROVIDER[provider], s = sec('resend');
    s.querySelector('h1').textContent = P.title;
    const a = s.querySelector('[data-where]');
    a.href = P.link; a.textContent = P.where;
    const i = s.querySelector('input[name=key]');
    i.placeholder = P.ph; i.setAttribute('aria-label', P.label);
    sec('wait').querySelector('h1').textContent = 'Connecting ' + P.name;
  }
  step('sender', async (f) => { useProvider((f.elements.provider || {}).value); show('resend'); });
  step('resend', async (f, b) => {
    b.textContent = 'Checking the key…';
    await post('/setup/mail', { provider, api_key: f.elements.key.value.trim() });
    f.elements.key.value = '';
    show('wait');
  });

  async function poll() {
    if (polling) return;
    polling = true;
    const started = Date.now();
    while (true) {
      await new Promise((r) => setTimeout(r, 3000));
      let d;
      try { d = await (await fetch('/setup/progress', { credentials: 'same-origin', cache: 'no-store' })).json(); }
      catch (_) { continue; }
      if (d.state === 'wait' && Date.now() - started < 6 * 60 * 1000) continue;
      polling = false;
      if (d.state === 'code') { data.email = d.email || data.email; show('code'); }
      else if (d.state === 'done') { show('done'); }
      else { show('resend'); err('resend', d.message || 'That took too long. Try again.'); }
      return;
    }
  }

  const boxes = [...sec('code').querySelectorAll('.otp input')];
  const codeForm = sec('code').querySelector('form');
  const full = () => boxes.every((x) => /^[0-9]$/.test(x.value));
  function fill(text, from) {
    const d = (text || '').replace(/[^0-9]/g, '').slice(0, 6);
    if (!d) return;
    const start = d.length === 6 ? 0 : from;
    [...d].forEach((c, k) => { if (boxes[start + k]) boxes[start + k].value = c; });
    boxes[Math.min(start + d.length, 5)].focus();
    if (full()) codeForm.requestSubmit();
  }
  boxes.forEach((x, i) => {
    x.addEventListener('focus', () => x.select());
    x.addEventListener('paste', (e) => { e.preventDefault(); fill((e.clipboardData || window.clipboardData).getData('text'), i); });
    x.addEventListener('input', () => {
      const d = x.value.replace(/[^0-9]/g, '');
      if (d.length > 1) { x.value = ''; fill(d, i); return; }
      x.value = d;
      if (d && i < 5) boxes[i + 1].focus();
      if (full()) codeForm.requestSubmit();
    });
    x.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !x.value && i > 0) { e.preventDefault(); boxes[i - 1].value = ''; boxes[i - 1].focus(); }
      else if (e.key === 'ArrowLeft' && i > 0) { e.preventDefault(); boxes[i - 1].focus(); }
      else if (e.key === 'ArrowRight' && i < 5) { e.preventDefault(); boxes[i + 1].focus(); }
    });
  });
  step('code', async (f, b) => {
    if (!full()) throw new Error('Type the 6 digits from the e-mail.');
    b.textContent = 'Checking…';
    try {
      await post('/setup/verify', { code: boxes.map((x) => x.value).join('') });
    } catch (x) {
      boxes.forEach((y) => { y.value = ''; });
      boxes[0].focus();
      throw x;
    }
    show('done');
  });
  sec('code').querySelector('[data-resend]').addEventListener('click', async (e) => {
    const b = e.currentTarget;
    b.disabled = true; err('code', '');
    try { await post('/setup/verify/send', {}); b.textContent = 'A new code is on its way.'; }
    catch (x) { err('code', x.message); b.disabled = false; }
  });

  show(W.dataset.start);
})();
</script>"""


def wizard(start: str, csrf: str, settings: Any, info: dict[str, Any]) -> str:
    """The first run, one question at a time: name, e-mail, username, mail provider, API key,
    the code. START is where to resume (name, sender, wait, code)."""
    domain = settings.nip05_domain
    name = info.get("name") or settings.community_name or ""
    if name == "Open Source Village" and theme.current().base != "osv":
        name = ""
    boxes = "".join(
        ('<span class="sep"></span>' if i == 3 else "")
        + f'<input type="text" inputmode="numeric" pattern="[0-9]*" aria-label="Digit {i + 1}"'
        + (' autocomplete="one-time-code"' if i == 0 else ' autocomplete="off"')
        + ">"
        for i in range(6)
    )

    def actions(label: str, back: str = "") -> str:
        b = f'<button type="button" class="quiet" data-back="{back}">Back</button>' if back else ""
        return f'<div class="actions"><button class="btn" type="submit">{label}</button>{b}</div>'

    return f"""<div class="card wiz" id="wiz" data-start="{E(start)}" data-csrf="{E(csrf)}"
 data-domain="{E(domain)}" data-name="{E(name)}" data-email="{E(info.get("email", ""))}" data-nip05="{E(info.get("nip05", ""))}">
<div class="top"><div class="bar"><i></i></div><span class="count"></span></div>

<section data-step="name">
<h1>What is your community called?</h1>
<p class="muted">It appears on your belfry's pages and in the e-mails it sends.</p>
<form novalidate><input type="text" name="name" value="{E(name)}" maxlength="{MAX_NAME}" autocomplete="organization" aria-label="Community name">
<p class="err" role="alert"></p>{actions("Next")}</form>
</section>

<section data-step="email" hidden>
<h1>Who is the first admin?</h1>
<p class="muted">Your e-mail address. We will send a code there to confirm it.</p>
<form novalidate><input type="email" name="email" autocomplete="email" aria-label="E-mail address" placeholder="you@example.org">
<p class="err" role="alert"></p>{actions("Next", "name")}</form>
</section>

<section data-step="username" hidden>
<h1>Pick a username</h1>
<p class="muted">It is your address on this belfry. Apps and members will find you by it.</p>
<form novalidate><div class="handle"><input type="text" name="username" autocomplete="username" autocapitalize="none"
 spellcheck="false" maxlength="30" aria-label="Username"><span>@{E(domain)}</span></div>
<p class="err" role="alert"></p>{actions("Create my account", "email")}</form>
</section>

<section data-step="sender" hidden>
<h1>How will your belfry send e-mail?</h1>
<p class="muted">Members sign in with a code sent by e-mail, so this comes first.</p>
<form novalidate><label class="option"><input type="radio" name="provider" value="resend" checked>
<span><strong>Resend</strong><small>An e-mail service with a free plan, at resend.com.</small></span></label>
<label class="option"><input type="radio" name="provider" value="lettermint">
<span><strong>Lettermint</strong><small>A European e-mail service (Netherlands), at lettermint.co.</small></span></label>
<p class="err" role="alert"></p>{actions("Next")}</form>
</section>

<section data-step="resend" hidden>
<h1>Your Resend API key</h1>
<p class="muted">Create one at <a data-where href="https://resend.com/api-keys" target="_blank" rel="noopener">resend.com/api-keys</a>.
Your belfry sends from belfry@{E(domain)}; you can change that later.</p>
<form novalidate><input type="password" name="key" autocomplete="off" spellcheck="false" placeholder="re_…" aria-label="Resend API key">
<p class="err" role="alert">{E(info.get("message", "") if start == "resend" else "")}</p>{actions("Connect", "sender")}</form>
</section>

<section data-step="wait" hidden>
<h1>Connecting Resend</h1>
<div class="spin" aria-hidden="true"></div>
<p class="muted" role="status">Your belfry is switching e-mail on. This takes about a minute.</p>
</section>

<section data-step="code" hidden>
<h1>Check your inbox</h1>
<p class="muted">Type the 6-digit code we sent to <strong class="to"></strong>.</p>
<form novalidate><div class="otp">{boxes}</div>
<p class="err" role="alert"></p>
<div class="actions"><button class="btn" type="submit">Verify</button>
<button type="button" class="quiet" data-resend>Send a new code</button></div></form>
</section>

<section data-step="done" hidden>
<div class="done-mark" aria-hidden="true">✓</div>
<h1>Your belfry is ready</h1>
<p class="muted"><strong class="who"></strong> is its first admin. Members and apps can now sign in.</p>
<div class="actions"><a class="btn" href="/">Open your belfry</a><a class="quiet" href="/setup">More settings</a></div>
</section>
</div>
{WIZARD_JS}"""


def code_form(err: str = "") -> str:
    error = f'<p class="err" role="alert">{E(err)}</p>' if err else ""
    return f"""<div class="card wiz"><section>
<h1>Set up this belfry</h1>
<p class="muted">Open the setup link from the installer, or type its code. On the server,
<code>belfry setup-code "Your name"</code> makes a new one. Admins can <a href="/login?to=/setup">sign in</a> instead.</p>
<form method="post" action="/setup/code">
<input type="text" name="code" autocomplete="one-time-code" autocapitalize="characters" spellcheck="false"
 placeholder="XXXX-XXXX-XXXX-XXXX" required aria-label="Setup code">
<div style="margin-top:12px"><input type="text" name="name" autocomplete="name" maxlength="{MAX_NAME}"
 placeholder="Your name (if the code has none)" aria-label="Your name"></div>
{error}<div class="actions"><button class="btn" type="submit">Continue</button></div></form>
</section></div>"""


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
    if last and not last.get("ok") and not st["pending"] and not st["applying"]:
        notes += (
            f'<p class="note"><span class="bad">The last change failed</span> '
            f"({E(', '.join(last.get('keys') or []) or 'settings')}). "
            f"<span class='meta'>{E(str(last.get('message', '')))}</span></p>"
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
    return f"""{notes}<h1>Settings</h1>
<p class="muted">Secrets are stored on the server and never shown again. Changes are recorded in
the <a href="/changelog">changelog</a> as made by <strong>{E(who)}</strong>.</p>
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
            return str(sess.get("csrf", "")), str(sess.get("who") or "")
        return None

    NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    def page_(request: Request, body: str, refresh: bool = False) -> HTMLResponse:
        head = '<meta http-equiv="refresh" content="10">' if refresh else ""
        return flash.clear(
            HTMLResponse(
                console.frame("Setup", "/setup", None, STYLE + head + body), headers=NO_STORE
            ),
            request,
        )

    def bare(request: Request, title: str, body: str) -> HTMLResponse:
        """The first run: only the logo and one card, no console tabs or footer links."""
        return flash.clear(
            HTMLResponse(
                theme.frame(title, WIZARD_STYLE + body, claim.TEST_NOTICE, bare=True),
                headers=NO_STORE,
            ),
            request,
        )

    def set_setup_cookie(response: Response, request: Request, claims: dict[str, Any]) -> None:
        response.set_cookie(
            COOKIE,
            mypass._sign(svc(request), {"kind": "setup", **claims}, SESSION_SECONDS),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=secure(request),
            samesite="lax",
            path="/",
        )

    def start_session(request: Request, who: str, to: str) -> RedirectResponse:
        response = RedirectResponse(to, status_code=303)
        set_setup_cookie(response, request, {"csrf": secrets.token_urlsafe(24), "who": who})
        return response

    def first_run(services: Services) -> bool:
        """The wizard shows until the first admin's address is verified."""
        fa = first_admin(services.settings)
        if fa:
            return not fa.get("verified")
        return not has_admin(services)

    @app.get("/console/setup", response_class=HTMLResponse)
    async def setup_page(request: Request, code: str = ""):
        services = svc(request)
        st = state(services.settings)
        if not st["available"]:
            return page_(
                request,
                "<h1>Setup</h1><p>This belfry has no setup folder. Use <code>belfry config</code> on "
                "the server.</p>",
            )
        if code:
            # the link the installer prints: belfry.<domain>/setup?code=…
            entry = match_code(services.settings, code[:64])
            if entry is None:
                return flash.redirect(services, "/setup", err="That link is wrong or has expired.")
            who = " ".join(str(entry.get("for") or "").split())[:MAX_NAME]
            return start_session(request, who, "/setup")
        msg, err = flash.peek(request, services)
        ok = await allowed(request)
        fresh = await run_in_threadpool(first_run, services)
        if ok is None:
            return (
                bare(request, "Set up your belfry", code_form(err))
                if fresh
                else page_(request, f'<div class="wiz">{code_form(err)}</div>')
            )
        csrf, who = ok
        if fresh:
            info = await run_in_threadpool(progress, services)
            start = {"admin": "name", "mail_error": "resend"}.get(info["state"], info["state"])
            return bare(request, "Set up your belfry", wizard(start, csrf, services.settings, info))
        fa = first_admin(services.settings)
        return page_(
            request,
            settings_form(
                st,
                csrf,
                who or fa.get("username", "") or "the setup code",
                msg,
                err,
                request.query_params.get("open", ""),
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

    def refused() -> JSONResponse:
        return JSONResponse(
            {"message": "This page expired. Open the setup link again."}, status_code=403
        )

    @app.get("/console/setup/progress")
    async def setup_progress(request: Request):
        if await allowed(request) is None:
            return refused()
        info = await run_in_threadpool(progress, svc(request))
        return JSONResponse(
            {k: info.get(k, "") for k in ("state", "message", "email")}, headers=NO_STORE
        )

    @app.post("/console/setup/first-admin")
    async def setup_first_admin(request: Request):
        ok, body = await json_body(request)
        if ok is None:
            return refused()
        services = svc(request)
        try:
            made = await run_in_threadpool(
                create_first_admin,
                services,
                str(body.get("email", "")),
                str(body.get("username", "")),
                str(body.get("session_pubkey", "")),
                str(body.get("name", "")),
            )
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=422)
        except Exception:  # noqa: BLE001
            log.exception("setup: first admin failed")
            return JSONResponse(
                {"message": "Your account could not be created. Try again in a moment."},
                status_code=500,
            )
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
        # this browser is signed in to belfry.<domain> as the admin, with the page's csrf so its
        # next calls (connect, mail, code) pass as the admin's
        days = services.settings.pass_page_session_days
        response.set_cookie(
            console.COOKIE,
            mypass._sign(
                services,
                {"kind": "console", "partner": made["partner"], "csrf": ok[0]},
                days * 86400,
            ),
            max_age=days * 86400,
            httponly=True,
            secure=secure(request),
            samesite="lax",
            path="/",
        )
        set_setup_cookie(response, request, {"csrf": ok[0], "who": made["username"]})
        return response

    @app.post("/console/setup/first-admin/connect")
    async def setup_first_connect(request: Request):
        ok, body = await json_body(request)
        if ok is None:
            return refused()
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
            return JSONResponse({"message": "The signer did not answer."}, status_code=502)
        fa["session_connected"] = True
        save_first_admin(services.settings, fa)
        return JSONResponse({"reply": reply})

    @app.post("/console/setup/mail")
    async def setup_mail(request: Request):
        """The first run's mail step: check the Resend key, then queue it with the sender (and the
        community name chosen in the first step) for the host to apply."""
        ok, body = await json_body(request)
        if ok is None:
            return refused()
        services = svc(request)
        fa = first_admin(services.settings)
        if not fa:
            return JSONResponse({"message": "Create the admin account first."}, status_code=409)
        provider = body.get("provider", "resend")
        if provider not in ("resend", "lettermint"):
            return JSONResponse({"message": "Choose Resend or Lettermint."}, status_code=422)
        key = str(body.get("api_key", "")).strip()
        try:
            check = check_lettermint if provider == "lettermint" else check_resend
            sender = await run_in_threadpool(check, key, services.settings.nip05_domain)
            values = {
                "BELFRY_LETTERMINT_API_KEY"
                if provider == "lettermint"
                else "BELFRY_RESEND_API_KEY": key,
                "BELFRY_MAIL_FROM": sender,
            }
            name = str(fa.get("community_name") or "")
            if name and name != services.settings.community_name:
                values["BELFRY_NAME"] = name
            await run_in_threadpool(
                write_pending, services.settings, values, ok[1] or fa.get("username", "")
            )
        except ValueError as exc:
            return JSONResponse({"message": str(exc)}, status_code=422)
        fa.update(code_sent_at=0, send_tries=0)
        save_first_admin(services.settings, fa)
        log.info("setup: mail queued (%s, sender %s)", provider, sender)
        return JSONResponse({"sender": sender})

    @app.post("/console/setup/verify/send")
    async def setup_verify_send(request: Request):
        ok, _ = await json_body(request)
        if ok is None:
            return refused()
        try:
            await run_in_threadpool(send_admin_code, svc(request))
        except Exception as exc:  # noqa: BLE001
            log.warning("setup: new code not sent: %s", exc)
            status = getattr(exc, "status", 502)
            return JSONResponse(
                {
                    "message": getattr(exc, "message", "")
                    or "No code could be sent. Try again later."
                },
                status_code=status if isinstance(status, int) and 400 <= status < 600 else 502,
            )
        return JSONResponse({"state": "sent"})

    @app.post("/console/setup/verify")
    async def setup_verify(request: Request):
        ok, body = await json_body(request)
        if ok is None:
            return refused()
        code = re.sub(r"[^0-9]", "", str(body.get("code", "")))[:6]
        try:
            await run_in_threadpool(verify_admin_code, svc(request), code)
        except Exception as exc:  # noqa: BLE001
            message = getattr(exc, "message", "") or "That code could not be checked. Try again."
            left = (getattr(exc, "extra", None) or {}).get("tries_left")
            if isinstance(left, int):
                message += f" {left} tries left." if left != 1 else " 1 try left."
            return JSONResponse({"message": message}, status_code=422)
        return JSONResponse({"state": "verified"})

    @app.post("/console/setup/code")
    async def setup_code(request: Request, code: str = Form(""), name: str = Form("")):
        services = svc(request)
        entry = match_code(services.settings, code[:64]) if same_origin(request) else None
        if entry is None:
            log.warning(
                "setup: wrong or expired code from %s",
                request.client.host if request.client else "?",
            )
            return flash.redirect(services, "/setup", err="That code is wrong or has expired.")
        who = " ".join(str(entry.get("for") or "").split())[:MAX_NAME]
        if not who:
            who = " ".join(name.split())[:MAX_NAME]
        if not who and not await run_in_threadpool(first_run, services):
            return flash.redirect(
                services, "/setup", err="This code was made without a name: give yours with it."
            )
        log.info("setup: %s signed in with a setup code", who or "the first admin")
        return start_session(request, who, "/setup")

    @app.post("/console/setup")
    async def setup_save(request: Request) -> Response:
        services = svc(request)
        ok = await allowed(request)
        form = await request.form()
        if ok is None or not same_origin(request):
            return RedirectResponse("/setup", status_code=303)
        csrf, who = ok
        if not csrf or not hmac.compare_digest(str(form.get("csrf", "")), csrf):
            return flash.redirect(services, "/setup", err="The form expired; try again.")
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
            return flash.redirect(services, "/setup", msg="Nothing changed.")
        try:
            keys = write_pending(services.settings, values, who or "the setup code")
        except ValueError as exc:
            return flash.redirect(services, "/setup", err=str(exc))
        log.info("setup: %s queued %s", who, ", ".join(keys))
        name = next((n for pid, n, _, _ in PROVIDERS if pid == provider), "Settings")
        return flash.redirect(services, "/setup", msg=f"{name} saved.")
