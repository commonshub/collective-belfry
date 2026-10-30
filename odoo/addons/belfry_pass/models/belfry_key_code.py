import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller
from .nostr_proof import npub

_logger = logging.getLogger(__name__)

CODE_MINUTES = 10  # a code stays usable this long
MAX_TRIES = 5  # wrong codes before the code ends
SENDS_PER_EMAIL = 5  # codes mailed to one address ...
SENDS_PER_KEY = 10  # ... or asked for by one key ...
SEND_WINDOW_MINUTES = 30  # ... within this window
HEX64 = re.compile(r'^[0-9a-f]{64}$')


def _hash(code_id, code):
    return hashlib.sha256(f"{code_id}:{code}".encode()).hexdigest()


class ResPartner(models.Model):
    _inherit = 'res.partner'

    belfry_verified_pubkey = fields.Char(
        string="Key verified by e-mail (hex)", copy=False, readonly=True, index=True,
        help="The Nostr key of an app that proved this e-mail address with a code "
             "(pass.<domain>/api/email-code). Not the participant's key until a pass is "
             "ordered with it.",
    )
    belfry_email_verified_at = fields.Datetime(string="E-mail verified", copy=False, readonly=True)


class BelfryKeyCode(models.Model):
    """A 6-digit code mailed to an address so that an app holding a Nostr key of its own can
    prove the address (pass.<domain>/api/email-code, NIP-98 signed with that key). The right code
    records the key on the address's contact (``belfry_verified_pubkey``), creating the contact
    when there is none; it never replaces a key the contact holds already. The code itself is
    only in the mail: Odoo keeps its hash."""

    _name = 'belfry.key.code'
    _description = "E-mail code for an app's key"
    _order = 'id desc'

    email = fields.Char(required=True, index=True, readonly=True)
    pubkey = fields.Char(string="Key (hex)", required=True, index=True, readonly=True)
    app_name = fields.Char(readonly=True)
    code_hash = fields.Char(required=True, readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True)
    tries = fields.Integer(default=0, readonly=True)
    used_at = fields.Datetime(readonly=True)
    partner_id = fields.Many2one('res.partner', ondelete='set null', readonly=True)

    @api.model
    def _belfry_clean(self, email, pubkey):
        email = (email or '').strip()
        if not email or '@' not in email or email.startswith('@') or email.endswith('@') or len(email) > 254:
            raise UserError("A valid e-mail address is required.")
        pubkey = (pubkey or '').strip().lower()
        if not HEX64.match(pubkey):
            raise UserError("The key must be 64 hex characters.")
        return email, pubkey

    @api.model
    def belfry_key_code_send(self, email, pubkey, app_name=False):
        """Mail a new code to ``email`` for ``pubkey``. Answers the same whether the address is
        known or not: ``sent`` with the expiry, or ``too_many`` after too many codes."""
        _check_service_caller(self.env)
        email, pubkey = self._belfry_clean(email, pubkey)
        now = fields.Datetime.now()
        since = now - timedelta(minutes=SEND_WINDOW_MINUTES)
        codes = self.sudo()
        if (codes.search_count([('email', '=ilike', email), ('create_date', '>=', since)]) >= SENDS_PER_EMAIL
                or codes.search_count([('pubkey', '=', pubkey), ('create_date', '>=', since)]) >= SENDS_PER_KEY):
            return {'state': 'too_many', 'retry_after': SEND_WINDOW_MINUTES * 60}
        # only the newest code for this address and key works
        codes.search([('email', '=ilike', email), ('pubkey', '=', pubkey), ('used_at', '=', False),
                      ('expires_at', '>', now)]).write({'expires_at': now})
        code = f"{secrets.randbelow(10 ** 6):06d}"
        record = codes.create({
            'email': email,
            'pubkey': pubkey,
            'app_name': (app_name or '').strip()[:60] or False,
            'code_hash': '-',
            'expires_at': now + timedelta(minutes=CODE_MINUTES),
        })
        record.code_hash = _hash(record.id, code)
        template = self.env.ref('belfry_pass.mail_template_key_code', raise_if_not_found=False)
        if template:
            # rendered now, so the code lives only in the queued mail (deleted once sent)
            template.sudo().with_context(belfry_code=code).send_mail(record.id, force_send=False)
            # the queue cron runs hourly; the app is waiting for this code
            cron = self.env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()
        _logger.info("belfry: e-mail code %s for key %s", record.id, pubkey)
        return {'state': 'sent', 'expires_at': fields.Datetime.to_string(record.expires_at)}

    @api.model
    def belfry_key_code_verify(self, email, pubkey, code, name=False):
        """Check ``code`` for ``email`` and ``pubkey``. Answers ``state``: ``bound`` (the key is
        recorded on the address's contact), ``other_key`` (the contact holds another key: its
        ``npub``), ``key_taken`` (the key belongs to someone else), ``wrong_code`` (with
        ``tries_left``) or ``expired`` (no usable code: ask for a new one)."""
        _check_service_caller(self.env)
        email, pubkey = self._belfry_clean(email, pubkey)
        now = fields.Datetime.now()
        record = self.sudo().search([('email', '=ilike', email), ('pubkey', '=', pubkey),
                                     ('used_at', '=', False)], limit=1)
        if not record or record.expires_at <= now or record.tries >= MAX_TRIES:
            return {'state': 'expired'}
        record.tries += 1
        code = re.sub(r'\s', '', str(code or ''))
        if not hmac.compare_digest(record.code_hash, _hash(record.id, code)):
            return {'state': 'wrong_code', 'tries_left': MAX_TRIES - record.tries}
        record.used_at = now
        return record._belfry_bind(name)

    def _belfry_bind(self, name=False):
        self.ensure_one()
        Partner = self.env['res.partner'].sudo()
        key_npub = npub(self.pubkey)
        partners = Partner.search([('email', '=ilike', self.email)], order='id')
        # several contacts with the address: the one with passes (the participant) first
        partner = partners.filtered('belfry_pass_ids')[:1] or partners[:1]
        if partner.belfry_npub and partner.belfry_npub != key_npub:
            return {'state': 'other_key', 'email': partner.email, 'npub': partner.belfry_npub}
        others = [('id', '!=', partner.id)] if partner else []
        taken = Partner.search_count(
            ['|', ('belfry_npub', '=', key_npub), ('belfry_verified_pubkey', '=', self.pubkey)] + others, limit=1,
        ) or self.env['belfry.pass'].sudo().search_count(
            ['|', ('keycast_pubkey', '=', self.pubkey), ('own_pubkey', '=', self.pubkey),
             ('state', 'in', ('pending', 'active'))]
            + ([('partner_id', '!=', partner.id)] if partner else []), limit=1,
        )
        if taken:
            return {'state': 'key_taken'}
        if not partner:
            partner = Partner.create({'name': (name or '').strip()[:100] or self.email, 'email': self.email})
        partner.write({'belfry_verified_pubkey': self.pubkey, 'belfry_email_verified_at': fields.Datetime.now()})
        self.partner_id = partner
        member = bool(self.env['belfry.pass'].sudo().search_count(
            [('partner_id', '=', partner.id), ('state', '=', 'active')], limit=1))
        _logger.info("belfry: key %s verified for partner %s", self.pubkey, partner.id)
        return {'state': 'bound', 'email': partner.email, 'npub': key_npub, 'member': member}
