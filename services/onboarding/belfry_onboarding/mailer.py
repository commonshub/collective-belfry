"""The belfry's own system mails (sign-in links, codes, invites, alerts), sent through the mail
provider in the settings (SMTP_*, MAIL_FROM). Used by the register (register.py) when the belfry
runs without Odoo; with Odoo, Odoo's mail templates send them.

Plain, small HTML with a text part: the community's name, a few paragraphs and at most one
button. Every value that comes from a person is escaped here."""

from __future__ import annotations

import email.utils
import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from typing import Any

log = logging.getLogger(__name__)

SENT: list[EmailMessage] = []  # tests: mails are kept here when there is no SMTP host


class Mailer:
    def __init__(self, settings: Any):
        self.s = settings

    @property
    def name(self) -> str:
        return self.s.community_name or "Belfry"

    def sender(self) -> str:
        frm = getattr(self.s, "mail_from", "") or f"belfry@{self.s.nip05_domain or 'localhost'}"
        return email.utils.formataddr((self.name, frm))

    def send(
        self,
        to: str | list[str],
        subject: str,
        paragraphs: list[str],
        button: tuple[str, str] | None = None,
        footer: str = "",
        headers: dict[str, str] | None = None,
    ) -> bool:
        """PARAGRAPHS and FOOTER are plain text (escaped here); BUTTON is (url, label)."""
        rcpts = [to] if isinstance(to, str) else list(to)
        rcpts = [r for r in rcpts if r and "@" in r]
        if not rcpts:
            return False
        msg = EmailMessage()
        msg["From"] = self.sender()
        msg["To"] = ", ".join(rcpts)
        msg["Subject"] = subject[:200]
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.s.nip05_domain or "localhost")
        for k, v in (headers or {}).items():
            msg[k] = v
        text = "\n\n".join(paragraphs)
        if button:
            text += f"\n\n{button[1]}: {button[0]}"
        if footer:
            text += f"\n\n-- \n{footer}"
        msg.set_content(text + "\n")
        msg.add_alternative(self._html(paragraphs, button, footer), subtype="html")
        if not self.s.smtp_host:
            SENT.append(msg)
            log.warning("mailer: no mail provider; mail to %s not sent (%s)", rcpts, subject)
            return False
        try:
            self._smtp(msg, rcpts)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("mailer: mail to %s failed: %r", rcpts, exc)
            return False

    def send_html(self, to: str, subject: str, body_html: str, headers: dict[str, str]) -> bool:
        """A mail whose HTML is made already (the followers' digests, followers.py)."""
        import re

        msg = EmailMessage()
        msg["From"] = self.sender()
        msg["To"] = to
        msg["Subject"] = subject[:200]
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.s.nip05_domain or "localhost")
        for k, v in headers.items():
            msg[str(k)] = str(v)
        text = re.sub(r"<[^>]+>", "", re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", body_html))
        msg.set_content(re.sub(r"\n{3,}", "\n\n", text).strip() + "\n")
        msg.add_alternative(body_html, subtype="html")
        if not self.s.smtp_host:
            SENT.append(msg)
            return False
        try:
            self._smtp(msg, [to])
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("mailer: mail to %s failed: %r", to, exc)
            return False

    def _smtp(self, msg: EmailMessage, rcpts: list[str]) -> None:
        s = self.s
        enc = (s.smtp_encryption or "").lower()
        cls = smtplib.SMTP_SSL if enc == "ssl" else smtplib.SMTP
        with cls(s.smtp_host, s.smtp_port, timeout=30) as conn:
            if enc == "starttls":
                conn.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                conn.login(s.smtp_user, s.smtp_password)
            conn.send_message(msg, to_addrs=rcpts)

    def _html(self, paragraphs: list[str], button: tuple[str, str] | None, footer: str) -> str:
        p = "".join(
            f'<p style="margin:0 0 14px;">{escape(x).replace(chr(10), "<br>")}</p>'
            for x in paragraphs
        )
        if button:
            p += (
                f'<p style="margin:20px 0;"><a href="{escape(button[0])}" '
                'style="display:inline-block;'
                "background:#18181b;color:#fafafa;font-weight:600;padding:12px 20px;"
                f'text-decoration:none;border-radius:10px;">{escape(button[1])}</a></p>'
            )
        foot = (
            f'<p style="margin:24px 0 0;color:#71717a;font-size:13px;">{escape(footer)}</p>'
            if footer
            else ""
        )
        return (
            '<div style="background:#fafafa;padding:28px 12px;'
            "font-family:-apple-system,'Segoe UI',"
            'Arial,sans-serif;font-size:15px;line-height:1.55;color:#09090b;">'
            '<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e4e4e7;'
            f'border-radius:12px;padding:26px;"><p style="margin:0 0 18px;font-weight:700;">'
            f"{escape(self.name)}</p>{p}{foot}</div></div>"
        )
