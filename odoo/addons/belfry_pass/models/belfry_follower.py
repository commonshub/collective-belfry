import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

FREQUENCIES = [('asap', 'As soon as possible'), ('monthly', 'Monthly')]
ASKS_PER_DAY = 3  # confirmation mails to one address within 24 hours
CHANNEL = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')  # a NIP-29 group id
EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
DIGEST_DAY_PARAM = 'belfry.announcements_digest_day'


class ResPartner(models.Model):
    _inherit = 'res.partner'

    belfry_follower_ids = fields.One2many('belfry.follower', 'partner_id', string="Announcements")


class BelfryFollower(models.Model):
    """Someone who follows the announcements of one channel (a group on the groups relay: a
    channel, pod or event) by e-mail, like following an account: a contact in the registry and how
    often they get them. Members follow from the chat (their contact's address, no confirmation);
    anyone may follow the public channel (``general``) from the website, which mails a link to
    confirm (the contact is created for the address when there is none). The onboarding service
    (followers.py) sends the mails and changes the frequency from the links in their footer; the
    token in those links is the follow's only credential."""

    _name = 'belfry.follower'
    _description = "Follower of the public announcements"
    _order = 'id desc'
    _rec_name = 'email'

    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade', index=True, readonly=True)
    channel = fields.Char(required=True, default='general', index=True, readonly=True,
                          help="The group id on the groups relay (a channel, a pod or an event).")
    email = fields.Char(related='partner_id.email', store=True, readonly=True)
    state = fields.Selection(
        [('pending', 'Not confirmed'), ('active', 'Following'), ('stopped', 'Unsubscribed')],
        default='pending', required=True, readonly=True,
    )
    frequency = fields.Selection(FREQUENCIES, default='monthly', required=True)
    token = fields.Char(required=True, copy=False, index=True, readonly=True,
                        default=lambda self: secrets.token_urlsafe(24), groups='belfry_pass.group_admin')
    confirmed_at = fields.Datetime(readonly=True)
    stopped_at = fields.Datetime(readonly=True)
    sent_until = fields.Integer(
        string="Mailed up to (unix time)", readonly=True,
        help="Announcements posted up to this time were in a digest already (or came before "
             "the follower confirmed).",
    )
    last_sent_at = fields.Datetime(string="Last digest", readonly=True)
    asks = fields.Integer(readonly=True, help="Confirmation mails asked for since asked_at.")
    asked_at = fields.Datetime(readonly=True)

    _partner_channel_unique = models.Constraint('UNIQUE(partner_id, channel)', "One follow per contact and channel.")
    _token_unique = models.Constraint('UNIQUE(token)', "Tokens are unique.")

    # ── called by the onboarding service ─────────────────────────────────────────────────────
    @api.model
    def belfry_follow_request(self, email, channel='general'):
        """Someone typed EMAIL on the website to follow CHANNEL. Answers whether to mail a confirmation link
        (``send`` with ``token``) or not (``following``: already confirmed; ``too_many``)."""
        _check_service_caller(self.env)
        email = (email or '').strip()
        if len(email) > 254 or not EMAIL.match(email):
            raise UserError("A valid e-mail address is required.")
        Partner = self.env['res.partner'].sudo()
        partners = Partner.search([('email', '=ilike', email)], order='id')
        partner = partners.filtered('belfry_pass_ids')[:1] or partners[:1]
        if not partner:
            partner = Partner.create({'name': email, 'email': email})
            _logger.info("belfry: new contact %s for the announcements", partner.id)
        channel = self._belfry_channel(channel)
        follower = self.sudo().search([('partner_id', '=', partner.id), ('channel', '=', channel)], limit=1)
        if not follower:
            follower = self.sudo().create({'partner_id': partner.id, 'channel': channel})
        if follower.state == 'active':
            return {'state': 'following', 'id': follower.id, 'token': follower.token}
        now = fields.Datetime.now()
        if follower.asked_at and follower.asked_at > now - timedelta(days=1):
            if follower.asks >= ASKS_PER_DAY:
                return {'state': 'too_many'}
            follower.write({'asks': follower.asks + 1})
        else:
            follower.write({'asks': 1, 'asked_at': now})
        return {'state': 'send', 'id': follower.id, 'token': follower.token}

    @api.model
    def _belfry_channel(self, channel):
        channel = (channel or '').strip()
        if not CHANNEL.match(channel):
            raise UserError("Unknown channel.")
        return channel

    def _belfry_by_token(self, token):
        token = (token or '').strip()
        if not token or len(token) > 100:
            return self.browse()
        return self.sudo().search([('token', '=', token)], limit=1)

    def _belfry_public(self):
        self.ensure_one()
        return {'id': self.id, 'state': self.state, 'frequency': self.frequency, 'email': self.email or '',
                'channel': self.channel}

    @api.model
    def belfry_follow_get(self, token):
        """The follower behind a link's token, or ``{'state': 'unknown'}``."""
        _check_service_caller(self.env)
        follower = self._belfry_by_token(token)
        return follower._belfry_public() if follower else {'state': 'unknown'}

    @api.model
    def belfry_follow_set(self, token, choice):
        """CHOICE is a frequency (confirms a pending follower, resumes a stopped one) or
        ``stop``. Announcements posted before a confirmation are never mailed."""
        _check_service_caller(self.env)
        follower = self._belfry_by_token(token)
        if not follower:
            return {'state': 'unknown'}
        now = fields.Datetime.now()
        if choice == 'stop':
            if follower.state != 'stopped':
                follower.write({'state': 'stopped', 'stopped_at': now})
        elif choice in dict(FREQUENCIES):
            vals = {'frequency': choice}
            if follower.state != 'active':
                vals.update(state='active', confirmed_at=now, stopped_at=False,
                            sent_until=int(datetime.now().timestamp()))
            follower.write(vals)
        else:
            raise UserError("Unknown choice.")
        return follower._belfry_public()

    @api.model
    def belfry_follow_due(self, frequency):
        """Active followers of FREQUENCY (``asap``: mailed a few minutes after a post;
        ``monthly``: on the 1st). ``since``: what they got already."""
        _check_service_caller(self.env)
        if frequency not in dict(FREQUENCIES):
            raise UserError("Unknown frequency.")
        rows = self.sudo().search([('state', '=', 'active'), ('frequency', '=', frequency)])
        return [{'id': f.id, 'email': f.email or '', 'frequency': f.frequency, 'token': f.token,
                 'channel': f.channel, 'since': f.sent_until} for f in rows if f.email]

    @api.model
    def belfry_follow_member_list(self, partner_id):
        """A member's follows (the chat's Follow buttons): channel, frequency, state."""
        _check_service_caller(self.env)
        rows = self.sudo().search([('partner_id', '=', int(partner_id))])
        return [{'channel': f.channel, 'frequency': f.frequency, 'state': f.state} for f in rows]

    @api.model
    def belfry_follow_member_set(self, partner_id, channel, choice):
        """A member follows CHANNEL at a frequency, or stops (``stop``). Their contact's address
        is theirs already: no confirmation."""
        _check_service_caller(self.env)
        partner = self.env['res.partner'].sudo().browse(int(partner_id)).exists()
        if not partner or not partner.email:
            raise UserError("This member has no e-mail address.")
        channel = self._belfry_channel(channel)
        follower = self.sudo().search([('partner_id', '=', partner.id), ('channel', '=', channel)], limit=1)
        if not follower:
            if choice == 'stop':
                return {'channel': channel, 'state': 'stopped', 'frequency': 'monthly'}
            follower = self.sudo().create({'partner_id': partner.id, 'channel': channel})
        result = self.belfry_follow_set(follower.token, choice)
        return {'channel': channel, 'state': result['state'], 'frequency': result['frequency']}

    @api.model
    def belfry_follow_claim_day(self, day):
        """True once per DAY: the first caller sends that morning's digests."""
        _check_service_caller(self.env)
        params = self.env['ir.config_parameter'].sudo()
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [DIGEST_DAY_PARAM])
        if (params.get_param(DIGEST_DAY_PARAM) or '') >= day:
            return False
        params.set_param(DIGEST_DAY_PARAM, day)
        return True

    @api.model
    def belfry_follow_mail(self, follower_id, subject, body_html, until=0, headers=None):
        """Queue one mail to a follower (the confirmation link, or a digest of the announcements
        up to UNTIL, which then counts as sent). HEADERS: extra mail headers (List-Unsubscribe)."""
        _check_service_caller(self.env)
        follower = self.sudo().browse(int(follower_id)).exists()
        if not follower or not follower.email:
            return False
        company = self.env.company
        mail = self.env['mail.mail'].sudo().create({
            'subject': (subject or '')[:200],
            'body_html': body_html or '',
            'email_to': follower.email,
            'email_from': company.email_formatted or self.env.user.email_formatted,
            'headers': json.dumps({str(k): str(v) for k, v in (headers or {}).items()}),
            'auto_delete': True,
        })
        if until:
            follower.write({'sent_until': max(follower.sent_until, int(until)),
                            'last_sent_at': fields.Datetime.now()})
        cron = self.env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return mail.id
