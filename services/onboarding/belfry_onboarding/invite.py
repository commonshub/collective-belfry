"""The belfry's own invite page, for a belfry without Odoo (REGISTER=belfry): membership is free,
so accepting an invite is a name, an e-mail address and a click in the mail that proves it.

- ``GET  /invite/<token>``: the form (who invites, name, e-mail; the address an invite was mailed
  to is filled in already);
- ``POST /invite/<token>``: the person and a pending pass (the invite is reserved for it), and a
  mail to the address with a confirmation link;
- ``GET  /invite/<token>/confirm/<secret>``: the address is theirs: the membership opens
  (core.activate_free_pass: accounts, key, sign-in link) and the page offers to sign in.

Messages after a post ride in the signed flash cookie (flash.py), never in the URL. With Odoo the
invite links point at Odoo's own checkout page instead, and these routes answer 404."""

from __future__ import annotations

import hmac
import html
import logging
import re
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from . import core, flash, theme
from .clients.odoo import OdooError
from .core import Services

log = logging.getLogger(__name__)
E = html.escape
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def own(svc: Services) -> bool:
    from .register import Register

    return isinstance(svc.odoo, Register)


INVITE_STYLE = """<style>
.invite-form{display:flex;flex-direction:column;gap:14px;margin:20px 0 8px}
.invite-form label{display:flex;flex-direction:column;gap:6px;font-weight:500}
.invite-form input{font:inherit;font-size:16px;padding:12px 14px;border-radius:10px;
border:1px solid var(--border);background:var(--surface,var(--bg));color:var(--fg);width:100%;
box-sizing:border-box}
.invite-form input:focus{outline:2px solid var(--accent);outline-offset:1px}
.invite-form .btn{width:100%;padding:13px 18px;font-size:16px;cursor:pointer;margin-top:4px}
.invite-again{margin-top:18px}
.invite-again summary{cursor:pointer;color:var(--accent);font-weight:500}
.error{color:var(--accent-2);font-weight:500}
</style>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(theme.frame(title, body), status_code=status)


def _closed(state: str) -> HTMLResponse:
    why = {
        "used": "This invite was used already.",
        "expired": "This invite has expired. Ask the person who invited you for a new one.",
        "revoked": "This invite was withdrawn.",
    }.get(state, "This invite does not exist.")
    return _page("Invite", f"<h1>Invite</h1><p>{E(why)}</p>", 410 if state else 404)


def register(app: FastAPI) -> None:
    def svc(request: Request) -> Services:
        return request.app.state.services

    @app.get("/invite/{token}", response_class=HTMLResponse)
    async def invite_page(request: Request, token: str):
        services = svc(request)
        if not own(services):
            return Response(status_code=404)
        # an agent reads the same invite as Markdown: /invite/<token>.md or Accept: text/markdown
        as_md = token.endswith(".md") or "text/markdown" in request.headers.get("accept", "")
        token = token.removesuffix(".md")
        inv = await run_in_threadpool(services.odoo.invite_by_token, token)
        if as_md and inv is not None and inv.get("guest_channel"):
            from . import guests

            if inv["state"] != "open":
                return Response(
                    "# This invite was used or has expired.\n", 410, media_type="text/markdown"
                )
            return Response(
                guests.agent_instructions(services, inv, token),
                media_type="text/markdown; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
        msg, err = flash.peek(request, services)
        if inv is None:
            return _closed("")
        name = theme.current().name
        if inv["state"] == "reserved" and not err:
            body = (
                "<h1>Check your mail</h1>"
                f"<p>{E(msg or 'We sent you a link to confirm your address.')}</p>"
                "<p>Open it on this device to finish joining. Nothing arrived? Look in the spam "
                "folder, or send it again below.</p>"
            )
            body += _form(token, inv, again=True)
            return flash.clear(_page(f"Join {name}", body), request)
        if inv["state"] not in ("open", "reserved"):
            return _closed(inv["state"])
        who = inv.get("inviter_name") or "A member"
        note = f'<p class="muted">“{E(inv["note"])}”</p>' if inv.get("note") else ""
        error = f'<p class="error" role="alert">{E(err)}</p>' if err else ""
        if inv.get("guest_channel"):  # a channel's guest (guests.py)
            days = int(inv.get("guest_days") or 30)
            body = (
                f"<h1>{E(who)} invites you to #{E(inv['guest_channel'])}</h1>"
                f"<p>You join this channel of {E(name)} as a guest for {days} days: you read and "
                "write there, nowhere else. Tell us your name and e-mail address: we send you a "
                "link to confirm it, and you are in.</p>"
                f"{error}{_form(token, inv)}"
                f'<p class="muted" style="margin-top:20px">Are you an AI agent? Read '
                f'<a href="/invite/{E(token)}.md">the instructions for agents</a>: accept with '
                "your own Nostr key, no e-mail needed.</p>"
            )
        else:
            body = (
                f"<h1>{E(who)} invites you to {E(name)}</h1>{note}"
                "<p>Membership is free. Tell us your name and e-mail address: we send you a link "
                "to confirm it, and you are in.</p>"
                f"{error}{_form(token, inv)}"
            )
        return flash.clear(_page(f"Join {name}", body), request)

    def _form(token: str, inv: dict, again: bool = False) -> str:
        email = E(inv.get("invitee_email") or "")
        name = E(inv.get("invitee_name") or "")
        label = "Send it again" if again else "Send me the link"
        form = (
            f'{INVITE_STYLE}<form method="post" action="/invite/{E(token)}" class="invite-form">'
            f'<label>Your name<input name="name" required maxlength="100" value="{name}" '
            'autocomplete="name"></label>'
            '<label>Your e-mail address<input type="email" name="email" required maxlength="254" '
            f'value="{email}" autocomplete="email" inputmode="email"></label>'
            f'<button class="btn" type="submit">{label}</button>'
            "</form>"
        )
        if again:  # after a send: the form folds away, there if the mail never came
            return f'<details class="invite-again"><summary>Send it again</summary>{form}</details>'
        return form

    @app.post("/invite/{token}")
    async def invite_post(
        request: Request, token: str, name: str = Form(""), email: str = Form("")
    ):
        services = svc(request)
        if not own(services):
            return Response(status_code=404)
        back = f"/invite/{token}"
        email, name = email.strip(), name.strip()
        if not EMAIL.match(email) or len(email) > 254:
            return flash.redirect(services, back, err="That is not an e-mail address.")
        if not name:
            return flash.redirect(services, back, err="Please tell us your name.")
        reg = services.odoo
        try:
            made = await run_in_threadpool(reg.accept_invite, token, name, email)
        except OdooError as exc:
            return flash.redirect(services, back, err=_message(exc))
        secret = secrets.token_urlsafe(32)
        await run_in_threadpool(
            reg.write, "belfry.pass", [made["pass_id"]], {"confirm_token": secret}
        )
        base = (services.settings.console_url or services.settings.public_url).rstrip("/")
        link = f"{base}/invite/{token}/confirm/{secret}"
        community = theme.current().name
        await run_in_threadpool(
            reg.mailer.send,
            email,
            f"Confirm your address to join {community}",
            [
                f"Hello {name},",
                f"One click and you are a guest of #{inv['guest_channel']} on {community}."
                if (inv := reg.invite_by_token(token) or {}).get("guest_channel")
                else f"One click and you are a member of {community}.",
            ],
            (link, "Confirm and join"),
            "If you did not ask to join, ignore this mail: nothing happens without the click.",
        )
        return flash.redirect(services, back, msg=f"We sent a link to {email}.")

    @app.get("/invite/{token}/confirm/{secret}", response_class=HTMLResponse)
    async def invite_confirm(request: Request, token: str, secret: str):
        services = svc(request)
        if not own(services):
            return Response(status_code=404)
        reg = services.odoo
        inv = await run_in_threadpool(reg.invite_by_token, token)
        if inv is None or not inv.get("pass_id"):
            return _closed(inv["state"] if inv else "")
        row = await run_in_threadpool(
            reg.read_one,
            "belfry.pass",
            inv["pass_id"],
            ["confirm_token", "state", "login_setup_url"],
        )
        if not row or not hmac.compare_digest(str(row.get("confirm_token") or ""), secret):
            return _closed("")
        if row.get("state") != "active":
            try:
                result = await run_in_threadpool(core.activate_free_pass, services, inv["pass_id"])
            except Exception:
                log.exception("invite: activating pass %s failed", inv["pass_id"])
                return _page(
                    "Invite",
                    "<h1>Something went wrong</h1><p>Your membership could not "
                    "be opened just now. Try the link again in a minute.</p>",
                    500,
                )
            log.info("invite %s: pass %s %s", inv["id"], inv["pass_id"], result)
            row = await run_in_threadpool(
                reg.read_one, "belfry.pass", inv["pass_id"], ["state", "login_setup_url"]
            )
        name = theme.current().name
        if not row or row.get("state") != "active":
            return _page("Invite", "<h1>Not yet</h1><p>Your membership is not open yet.</p>", 409)
        if inv.get("guest_channel"):
            chat = f"https://chat.{services.settings.nip05_domain}/#/c/{E(inv['guest_channel'])}"
            return _page(
                f"Welcome to #{inv['guest_channel']}",
                f"<h1>Welcome to #{E(inv['guest_channel'])}</h1><p>You are a guest of this channel "
                f"of {E(name)} for {int(inv.get('guest_days') or 30)} days. Open it and sign in "
                "with your e-mail address: we send you a code.</p>"
                f'<p><a class="button" href="{chat}">Open #{E(inv["guest_channel"])}</a></p>',
            )
        url = row.get("login_setup_url") or "/me/login"
        return _page(
            f"Welcome to {name}",
            f"<h1>Welcome to {E(name)}</h1><p>You are a member now. We also mailed you a sign-in "
            "link.</p>"
            f'<p><a class="button" href="{E(url)}">Sign in now</a></p>',
        )


def _message(exc: OdooError) -> str:
    text = (exc.data.get("message") or exc.message or "").strip()
    return text.splitlines()[0][:200] if text else "That did not work."
