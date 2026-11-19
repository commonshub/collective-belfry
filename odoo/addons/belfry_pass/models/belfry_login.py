import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

CODE_MINUTES = 10  # the link and the code work this long
MAX_TRIES = 5  # wrong codes before the code ends
SENDS_PER_EMAIL = 5  # mails to one address within ...
SEND_WINDOW_MINUTES = 30  # ... this window


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Stewards are Belfry's admins: they sign in to the steward pages (pass.<domain>/steward)
    # with Belfry's own sign-in and get the stewards' mails. Until 2026-11-19 this was the
    # Authentik group belfry-stewards. Set by `python -m belfry_onboarding add-steward`.
    belfry_steward = fields.Boolean(string="Belfry steward", copy=False, index=True)

    @api.model
    def belfry_make_steward(self, email, name=False):
        """Mark the contact with this e-mail address a steward, creating the contact when there
        is none (the one with passes first). Returns ``{'partner_id', 'created'}``."""
        _check_service_caller(self.env)
        email = (email or '').strip()
        if '@' not in email:
            raise UserError("A valid e-mail address is required.")
        Partner = self.sudo()
        partners = Partner.search([('email', '=ilike', email)], order='id')
        partner = partners.filtered('belfry_pass_ids')[:1] or partners[:1]
        created = not partner
        if created:
            partner = Partner.create({'name': (name or '').strip()[:100] or email.split('@')[0], 'email': email})
        partner.belfry_steward = True
        _logger.info("belfry: %s is a steward (partner %s)", email, partner.id)
        return {'partner_id': partner.id, 'created': created}

    @api.model
    def belfry_steward_emails(self):
        """The stewards' e-mail addresses (report and application mails)."""
        _check_service_caller(self.env)
        return sorted({p.email for p in self.sudo().search([('belfry_steward', '=', True)]) if p.email})


class BelfryLoginCode(models.Model):
    """Signing in to Open Source Village by e-mail (replaces Authentik's magic-link flow,
    2026-11-19). The login page (onboarding service, ``/login``) asks for an address; Odoo mails a
    one-time link and a 6-digit code. The link signs in whichever browser opens it, the code is
    typed in the browser that asked. Both end at ``next_url`` (the app or page that wanted the
    sign-in). Only contacts that may sign in get a mail (an active pass, or a steward); the
    answer is the same for any address. Odoo keeps only hashes of the link's secret and the
    code; both live in the queued mail only."""

    _name = 'belfry.login.code'
    _description = "Sign-in link and code by e-mail"
    _order = 'id desc'

    email = fields.Char(required=True, index=True, readonly=True)
    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade', readonly=True)
    code_hash = fields.Char(required=True, readonly=True)
    link_hash = fields.Char(required=True, index=True, readonly=True)
    next_url = fields.Char(readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True)
    tries = fields.Integer(default=0, readonly=True)
    used_at = fields.Datetime(readonly=True)

    @api.model
    def _belfry_signin_partner(self, email):
        """The contact this address signs in as: one with an active pass first, then a steward."""
        partners = self.env['res.partner'].sudo().search([('email', '=ilike', email)], order='id')
        Pass = self.env['belfry.pass'].sudo()
        for partner in partners:
            if Pass.search_count([('partner_id', '=', partner.id), ('state', '=', 'active')], limit=1):
                return partner
        return partners.filtered('belfry_steward')[:1]

    @api.model
    def belfry_may_sign_in(self, partner_id):
        """Whether the contact may (still) sign in: an active pass or a steward."""
        _check_service_caller(self.env)
        partner = self.env['res.partner'].sudo().browse(int(partner_id)).exists()
        if not partner:
            return False
        return partner.belfry_steward or bool(self.env['belfry.pass'].sudo().search_count(
            [('partner_id', '=', partner.id), ('state', '=', 'active')], limit=1))

    @api.model
    def belfry_login_send(self, email, next_url=False):
        """Mail a sign-in link and code to ``email``. Answers ``sent`` for any address (nothing is
        mailed to one that may not sign in) or ``too_many``."""
        _check_service_caller(self.env)
        email = (email or '').strip()
        if not re.match(r'^[^@\s]+@[^@\s]+$', email) or len(email) > 254:
            raise UserError("A valid e-mail address is required.")
        now = fields.Datetime.now()
        codes = self.sudo()
        since = now - timedelta(minutes=SEND_WINDOW_MINUTES)
        if codes.search_count([('email', '=ilike', email), ('create_date', '>=', since)]) >= SENDS_PER_EMAIL:
            return {'state': 'too_many', 'retry_after': SEND_WINDOW_MINUTES * 60}
        partner = self._belfry_signin_partner(email)
        if not partner:
            _logger.info("belfry: sign-in asked for an address without an active pass")
            return {'state': 'sent'}
        # only the newest link and code for this address work
        codes.search([('email', '=ilike', email), ('used_at', '=', False),
                      ('expires_at', '>', now)]).write({'expires_at': now})
        code = f"{secrets.randbelow(10 ** 6):06d}"
        secret = secrets.token_urlsafe(32)
        record = codes.create({
            'email': email,
            'partner_id': partner.id,
            'code_hash': '-',
            'link_hash': _hash(secret),
            'next_url': (next_url or '')[:2000] or False,
            'expires_at': now + timedelta(minutes=CODE_MINUTES),
        })
        record.code_hash = _hash(f"{record.id}:{code}")
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        template = self.env.ref('belfry_pass.mail_template_login_code', raise_if_not_found=False)
        if template:
            # rendered now, so the code and the link live only in the queued mail
            template.sudo().with_context(
                belfry_code=code, belfry_link=f"{base}/login/link/{secret}",
            ).send_mail(record.id, force_send=False)
            cron = self.env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()
        _logger.info("belfry: sign-in mail %s for partner %s", record.id, partner.id)
        return {'state': 'sent'}

    def _belfry_result(self):
        self.used_at = fields.Datetime.now()
        if not self.belfry_may_sign_in(self.partner_id.id):
            return {'state': 'not_active'}
        return {'state': 'ok', 'partner_id': self.partner_id.id, 'next_url': self.next_url or False}

    @api.model
    def belfry_login_verify(self, email, code):
        """Check the typed ``code``. ``ok`` with ``partner_id`` and ``next_url``, ``wrong_code``
        with ``tries_left``, ``expired`` or ``not_active``."""
        _check_service_caller(self.env)
        email = (email or '').strip()
        now = fields.Datetime.now()
        record = self.sudo().search([('email', '=ilike', email), ('used_at', '=', False)], limit=1)
        if not record or record.expires_at <= now or record.tries >= MAX_TRIES:
            return {'state': 'expired'}
        record.tries += 1
        code = re.sub(r'\s', '', str(code or ''))
        if not hmac.compare_digest(record.code_hash, _hash(f"{record.id}:{code}")):
            return {'state': 'wrong_code', 'tries_left': MAX_TRIES - record.tries}
        return record._belfry_result()

    @api.model
    def belfry_login_use_link(self, secret):
        """Spend the mail's link. ``ok`` with ``partner_id`` and ``next_url``, ``used``,
        ``expired``, ``not_found`` or ``not_active``."""
        _check_service_caller(self.env)
        if not secret or len(secret) < 20:
            return {'state': 'not_found'}
        record = self.sudo().search([('link_hash', '=', _hash(secret))], limit=1)
        if not record:
            return {'state': 'not_found'}
        self.env.cr.execute("SELECT id FROM belfry_login_code WHERE id = %s FOR UPDATE", (record.id,))
        record.invalidate_recordset()
        if record.used_at:
            return {'state': 'used'}
        if record.expires_at <= fields.Datetime.now():
            return {'state': 'expired'}
        return record._belfry_result()

    @api.autovacuum
    def _gc_old_codes(self):
        self.sudo().search([('create_date', '<', fields.Datetime.now() - timedelta(days=2))]).unlink()
