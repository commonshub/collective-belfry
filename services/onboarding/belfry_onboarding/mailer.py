"""The belfry's own system mails (sign-in links, codes, invites, alerts), sent through the mail
provider in the settings (SMTP_*, MAIL_FROM). Used by the register (register.py) when the belfry
runs without Odoo; with Odoo, Odoo's mail templates send them.

Plain, small HTML with a text part: the community's name, a few paragraphs and at most one
button. Every value that comes from a person is escaped here. A paragraph is a string, or a list
of strings and ``(label, url)`` links (the text part then reads "label (url)")."""

from __future__ import annotations

import email.policy
import email.utils
import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from typing import Any

log = logging.getLogger(__name__)

Paragraph = str | list[Any]  # a text, or parts: texts and (label, url) links


def _text(p: Paragraph) -> str:
    if isinstance(p, str):
        return p
    return "".join(x if isinstance(x, str) else f"{x[0]} ({x[1]})" for x in p)


def _para(p: Paragraph) -> str:
    if isinstance(p, str):
        return escape(p).replace(chr(10), "<br>")
    return "".join(
        escape(x)
        if isinstance(x, str)
        else f'<a href="{escape(x[1])}" style="color:inherit;">{escape(x[0])}</a>'
        for x in p
    )


class _Policy(email.policy.EmailPolicy):
    """Python's own folding of a long Subject with a non-ASCII character can leave an empty
    encoded word in the middle of it (``=?utf-8?q??=``, shown as such by mail apps: seen on a
    request's confirmation mail, "…Day pass · Open Source Village · …"). The Subject is folded
    by ``email.header.Header`` instead, which splits it into whole encoded words."""

    def _fold(self, name, value, refold_binary=False):  # noqa: ANN001
        if name.lower() == "subject" and not str(value).isascii():
            from email.header import Header

            folded = Header(str(value), "utf-8", header_name=name).encode(linesep=self.linesep)
            return f"{name}: {folded}{self.linesep}"
        return super()._fold(name, value, refold_binary)


POLICY = _Policy()


def text_on(color: str) -> str:
    """Black or white, whichever reads better on COLOR (``#rrggbb``): a button in a light
    brand colour (a lime, a yellow) gets dark words."""
    c = (color or "").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = (int(c[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return "#fff"

    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return "#111" if (lum + 0.05) / 0.05 > 1.05 / (lum + 0.05) else "#fff"


SENT: list[EmailMessage] = []  # tests: mails are kept here when there is no SMTP host
SENT_MAX = 200  # …and only the last ones: a belfry without a mail provider must not grow


def _keep(msg: EmailMessage) -> None:
    SENT.append(msg)
    del SENT[:-SENT_MAX]


def one_line(text: str) -> str:
    """A header's value: a name with a line break in it would make the message unbuildable."""
    return " ".join(str(text).split())


def masked(rcpts: list[str]) -> str:
    """Addresses for the logs: the first letter and the domain only."""
    out = []
    for r in rcpts:
        local, _, domain = str(r).partition("@")
        out.append(f"{local[:1]}•••@{domain}" if domain else "•••")
    return ", ".join(out)


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
        paragraphs: list[Paragraph],
        button: tuple[str, str] | None = None,
        footer: str = "",
        headers: dict[str, str] | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
        after: list[Paragraph] | None = None,
    ) -> bool:
        """PARAGRAPHS and FOOTER are plain text (escaped here); BUTTON is (url, label); AFTER:
        more paragraphs, under the button; ATTACHMENTS are (file name, bytes, media type)."""
        rcpts = [to] if isinstance(to, str) else list(to)
        rcpts = [r for r in rcpts if r and "@" in r]
        if not rcpts:
            return False
        msg = EmailMessage(policy=POLICY)
        msg["From"] = self.sender()
        msg["To"] = ", ".join(rcpts)
        msg["Subject"] = one_line(subject)[:200]
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.s.nip05_domain or "localhost")
        for k, v in (headers or {}).items():
            msg[k] = one_line(v)
        text = "\n\n".join(_text(x) for x in paragraphs)
        if button:
            text += f"\n\n{button[1]}: {button[0]}"
        for x in after or []:
            text += "\n\n" + _text(x)
        if footer:
            text += f"\n\n-- \n{footer}"
        msg.set_content(text + "\n")
        msg.add_alternative(self._html(paragraphs, button, footer, after), subtype="html")
        for fname, data, mime in attachments or []:
            main, _, sub = mime.partition("/")
            msg.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=fname)
        if not self.s.smtp_host:
            _keep(msg)
            log.warning(
                "mailer: no mail provider; mail to %s not sent (%s)", masked(rcpts), subject
            )
            return False
        try:
            self._smtp(msg, rcpts)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("mailer: mail to %s failed: %r", masked(rcpts), exc)
            return False

    def send_html(
        self, to: str, subject: str, body_html: str, headers: dict[str, str], text: str = ""
    ) -> bool:
        """A mail whose HTML is made already (the followers' and mentions' digests); TEXT is its
        plain part, else the HTML without its tags."""
        import re

        msg = EmailMessage(policy=POLICY)
        msg["From"] = self.sender()
        msg["To"] = to
        msg["Subject"] = one_line(subject)[:200]
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.s.nip05_domain or "localhost")
        for k, v in headers.items():
            msg[str(k)] = one_line(v)
        if not text:
            text = re.sub(
                r"<[^>]+>", "", re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", body_html)
            )
        msg.set_content(re.sub(r"\n{3,}", "\n\n", text).strip() + "\n")
        msg.add_alternative(body_html, subtype="html")
        if not self.s.smtp_host:
            _keep(msg)
            return False
        try:
            self._smtp(msg, [to])
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("mailer: mail to %s failed: %r", masked([to]), exc)
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
            refused = conn.send_message(msg, to_addrs=rcpts)
        # every mail leaves a line: who (masked), the provider's answer; never the subject (a
        # code mail's subject holds the code) nor the body
        log.info(
            "mailer: mail %s to %s accepted by %s%s",
            str(msg.get("Message-ID", "")).strip("<>").split("@")[0][:16],
            masked([r for r in rcpts if r not in (refused or {})]) or "nobody",
            s.smtp_host,
            f"; refused for {masked(list(refused))}" if refused else "",
        )

    def _html(
        self,
        paragraphs: list[Paragraph],
        button: tuple[str, str] | None,
        footer: str,
        after: list[Paragraph] | None = None,
    ) -> str:
        from . import branding

        color = branding.primary()
        p = "".join(f'<p style="margin:0 0 14px;">{_para(x)}</p>' for x in paragraphs)
        if button:
            p += (
                f'<p style="margin:20px 0;"><a href="{escape(button[0])}" '
                'style="display:inline-block;'
                f"background:{color};color:{text_on(color)};font-weight:600;padding:12px 20px;"
                f'text-decoration:none;border-radius:10px;">{escape(button[1])}</a></p>'
            )
        p += "".join(f'<p style="margin:0 0 14px;">{_para(x)}</p>' for x in after or [])
        foot = (
            f'<p style="margin:24px 0 0;color:#71717a;font-size:13px;">{escape(footer)}</p>'
            if footer
            else ""
        )
        # no grey page and no framed card (Xavier, 2026-11-11): the belfry's logo and name, the text
        return (
            '<div style="padding:16px 18px;max-width:600px;'
            "font-family:-apple-system,'Segoe UI',"
            'Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;color:#18181b;">'
            f"{branding.mail_header(self.name)}{p}{foot}</div>"
        )
