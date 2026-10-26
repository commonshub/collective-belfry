import logging
from html import escape

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)


def mail_card(public_url, paragraphs):
    """The participant mails' dark card (palette of brand/tokens.css, inline for mail clients)."""
    brand = escape(public_url.rstrip('/') + '/brand/osv-logo-horizontal-dark.png')
    body = ''.join(paragraphs)
    return (
        '<div style="margin:0;padding:32px 12px;background-color:#2b3a31;font-family:Geist,'
        '-apple-system,\'Segoe UI\',Arial,sans-serif;font-size:15px;line-height:1.5;color:#ffffff;">'
        '<div style="max-width:560px;margin:0 auto;background-color:#1a231d;border:1px solid #44534a;'
        'border-radius:12px;padding:28px;">'
        '<p style="margin:0 0 20px;"><img src="%s" alt="Open Source Village" width="240" '
        'style="display:block;width:240px;max-width:100%%;height:auto;border:0;"/></p>%s'
        '<p style="margin:24px 0 0;">Open Source Village, run by Belfry</p></div></div>'
    ) % (brand, body)


def mail_button(url, label):
    return (
        '<p style="margin:16px 0 20px;"><a href="%s" style="display:inline-block;background-color:#d6ff52;'
        'color:#1a231d;font-weight:600;padding:12px 22px;text-decoration:none;border-radius:8px;'
        'font-size:16px;">%s</a></p>'
    ) % (escape(url), escape(label))


class BelfryApplication(models.Model):
    """Applications from the start page's form (<domain>/apply, decision 2026-10-26): people
    without an invite ask to join, with a short motivation, as a visitor or as a volunteer.
    Stewards answer on the steward pages: an invite (mailed to the applicant, who then picks and
    pays a pass) or a friendly no."""

    _name = 'belfry.application'
    _description = "Application to join Open Source Village"
    _order = 'create_date desc, id desc'

    name = fields.Char(required=True, readonly=True)
    email = fields.Char(required=True, readonly=True, index=True)
    motivation = fields.Text(required=True, readonly=True)
    kind = fields.Selection([('visitor', "Visitor"), ('volunteer', "Volunteer")], default='visitor',
                            required=True, readonly=True)
    state = fields.Selection([('new', "New"), ('invited', "Invited"), ('declined', "Declined")],
                             default='new', required=True, readonly=True, index=True)
    invite_id = fields.Many2one('belfry.invite', readonly=True, ondelete='set null')
    handled_by = fields.Char(readonly=True)
    handled_at = fields.Datetime(readonly=True)
    duplicate_note = fields.Char(compute='_compute_duplicate_note',
                                 help="Earlier applications or a pass with the same e-mail.")

    @api.depends('email')
    def _compute_duplicate_note(self):
        for record in self:
            notes = []
            if record.email:
                earlier = self.sudo().search_count([('email', '=ilike', record.email), ('id', '!=', record.id or 0)])
                if earlier:
                    notes.append("%d other application%s" % (earlier, "s" if earlier > 1 else ""))
                passes = self.env['belfry.pass'].sudo().search(
                    [('partner_id.email', '=ilike', record.email), ('state', 'in', ('pending', 'active'))])
                if passes:
                    notes.append("has a %s pass" % passes[0].state)
            record.duplicate_note = ", ".join(notes) or False

    # ------------------------------------------------------------------
    # the form (onboarding service, as the service user)
    # ------------------------------------------------------------------

    @api.model
    def belfry_application_create(self, vals, steward_emails=None):
        """Store an application, confirm it to the applicant and tell the stewards. Returns the id."""
        _check_service_caller(self.env)
        data = {k: str((vals or {}).get(k) or '').strip() for k in ('name', 'email', 'motivation', 'kind')}
        if not data['name'] or not data['motivation']:
            raise UserError("Please enter your name and a few words about why you want to join.")
        email = data['email']
        if not email or '@' not in email or email.startswith('@') or email.endswith('@'):
            raise UserError("Please enter a valid e-mail address.")
        application = self.sudo().create({
            'name': data['name'][:200],
            'email': email[:200],
            'motivation': data['motivation'][:4000],
            'kind': 'volunteer' if data['kind'] == 'volunteer' else 'visitor',
        })
        application._mail_applicant_received()
        application._mail_stewards([e for e in (steward_emails or []) if e and '@' in e])
        _logger.info("Belfry application %s (%s)", application.id, application.kind)
        return application.id

    # ------------------------------------------------------------------
    # stewards (steward pages, as the service user)
    # ------------------------------------------------------------------

    def belfry_application_invite(self, steward_email, steward_name=False, note=False):
        """Invite the applicant: an invite in the steward's name, mailed to the applicant."""
        _check_service_caller(self.env)
        self.ensure_one()
        if self.state != 'new':
            raise UserError("This application was answered already (%s)." % self.state)
        created = self.env['belfry.invite'].belfry_steward_create(
            steward_email, note or ("Application: %s" % self.name)[:200], steward_name)
        invite = self.env['belfry.invite'].sudo().browse(created['id'])
        self.sudo().write({'state': 'invited', 'invite_id': invite.id, 'handled_by': steward_email,
                           'handled_at': fields.Datetime.now()})
        self._mail_applicant_invited(invite)
        return created

    def belfry_application_decline(self, steward_email):
        _check_service_caller(self.env)
        self.ensure_one()
        if self.state != 'new':
            raise UserError("This application was answered already (%s)." % self.state)
        self.sudo().write({'state': 'declined', 'handled_by': steward_email,
                           'handled_at': fields.Datetime.now()})
        self._mail_applicant_declined()
        return self.state

    # ------------------------------------------------------------------
    # mails (text lives here, not in the Odoo UI)
    # ------------------------------------------------------------------

    def _public_url(self):
        return (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')

    def _send(self, to, subject, body_html, attachment=None):
        company = self.env.company
        self.env['mail.mail'].sudo().create({
            'subject': subject,
            'email_from': company.email_formatted or company.email,
            'email_to': to,
            'body_html': body_html,
            'auto_delete': False,
        })
        cron = self.env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def _p(self, text):
        return '<p style="margin:0 0 12px;">%s</p>' % text

    def _mail_applicant_received(self):
        self._send(self.email, "We received your application for Open Source Village", mail_card(self._public_url(), [
            self._p("Hello %s," % escape(self.name)),
            self._p("Thank you for applying%s. The stewards read every application and "
                    "answer by mail, usually within a few days." %
                    (" as a volunteer" if self.kind == 'volunteer' else "")),
        ]))

    def _mail_applicant_invited(self, invite):
        volunteer = self.kind == 'volunteer'
        self._send(self.email, "You are invited to Open Source Village", mail_card(self._public_url(), [
            self._p("Hello %s," % escape(self.name)),
            self._p("Good news: the stewards invite you to Open Source Village. Choose your pass "
                    "and pay on the next page; your account is set up right after the payment."
                    if not volunteer else
                    "Good news: the stewards welcome you as a volunteer. Open the link to sign up "
                    "for your shifts."),
            mail_button(invite.url, "Accept the invite"),
            self._p('<span style="color:#c9d1cb;">The link is for you only and works until %s.</span>'
                    % escape(fields.Date.to_string(invite.expires_at.date()))),
        ]))

    def _mail_applicant_declined(self):
        self._send(self.email, "Your application for Open Source Village", mail_card(self._public_url(), [
            self._p("Hello %s," % escape(self.name)),
            self._p("Thank you for your interest in Open Source Village. We cannot offer you a "
                    "place this time. If you know someone in the community, they can invite you."),
        ]))

    def _mail_stewards(self, emails):
        if not emails:
            return
        lines = [
            "<p><strong>%s</strong> (%s) applies as a %s.</p>" % (escape(self.name), escape(self.email), self.kind),
            "<p>%s</p>" % escape(self.motivation).replace("\n", "<br/>"),
            '<p>Answer on the <a href="%s/steward#applications">steward pages</a> (over the admin network).</p>'
            % escape(self._public_url()),
        ]
        self._send(', '.join(sorted(set(emails))), "Open Source Village: application from %s" % self.name,
                   ''.join(lines))
