import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)


class BelfryInvite(models.Model):
    """Invites sent by e-mail for a pass holder: the holder, or an agent signing with the
    holder's Nostr key, names the invitee's e-mail; Belfry mails the invite link there. The
    onboarding service checks the signature (pass.<domain>/api/invites) and says whether the
    holder is a steward; the holder's rights apply: a participant's invites count against
    ``belfry.invites_per_participant``, a steward's do not."""

    _inherit = 'belfry.invite'

    requested_by_partner_id = fields.Many2one(
        'res.partner', string="Requested by", readonly=True, copy=False, index=True,
        help="The pass holder whose key asked for this invite by e-mail.",
    )
    invitee_email = fields.Char(string="Invitee e-mail", readonly=True, copy=False)
    invitee_name = fields.Char(string="Invitee name", readonly=True, copy=False)
    invite_mail_sent_at = fields.Datetime(string="Invite mailed", readonly=True, copy=False)

    def _belfry_holder_row(self):
        self.ensure_one()
        self._belfry_usability_error()  # moves an open invite past its expiry to expired
        belfry_pass = self.sudo().pass_id
        active = belfry_pass.state == 'active'
        return {
            'id': self.id,
            'state': self.state,
            'email': self.invitee_email or '',
            'name': self.invitee_name or '',
            'note': self.note or '',
            'url': self.url,
            'expires_at': fields.Datetime.to_string(self.expires_at) if self.expires_at else False,
            'pass_state': belfry_pass.state or False,
            'npub': (belfry_pass.partner_id.belfry_npub or belfry_pass.npub or False) if active else False,
        }

    @api.model
    def _belfry_holder(self, partner_id):
        partner = self.env['res.partner'].sudo().browse(int(partner_id)).exists()
        active = partner and self.env['belfry.pass'].sudo().search_count(
            [('partner_id', '=', partner.id), ('state', '=', 'active')])
        if not active:
            raise UserError("Only participants with an active pass can invite.")
        return partner

    @api.model
    def belfry_holder_invite(self, partner_id, email, name=False, note=False, steward_email=False):
        """Invite ``email`` for the holder and mail the link. ``steward_email``: the holder is a
        steward (the onboarding service checked), so no invite limit applies. Someone with an
        active pass already gets no invite (``state`` ``member`` and their npub); an open or
        reserved invite of the same holder for the same address is returned again, not doubled."""
        _check_service_caller(self.env)
        holder = self._belfry_holder(partner_id)
        email = (email or '').strip()
        if not email or '@' not in email or email.startswith('@') or email.endswith('@') or len(email) > 254:
            raise UserError("A valid e-mail address is required.")
        name = (name or '').strip()[:100] or False
        member = self.env['belfry.pass'].sudo().search(
            [('partner_id.email', '=ilike', email), ('state', '=', 'active')], limit=1)
        if member:
            return {'state': 'member', 'email': email,
                    'npub': member.partner_id.belfry_npub or member.npub or False}
        existing = self.sudo().search([
            ('requested_by_partner_id', '=', holder.id), ('invitee_email', '=ilike', email),
            ('state', 'in', ('open', 'reserved')),
        ], limit=1)
        if existing:
            existing._belfry_usability_error()  # an open one past its expiry becomes expired
            if existing.state in ('open', 'reserved'):
                return existing._belfry_holder_row()
        if steward_email:
            row = self.belfry_steward_create(steward_email, note, steward_name=holder.name)
        else:
            row = self.belfry_participant_create(holder.id, note)
        invite = self.sudo().browse(row['id'])
        invite.write({
            'requested_by_partner_id': holder.id,
            'invitee_email': email,
            'invitee_name': name,
            'inviter_name': invite.inviter_name or holder.name,
        })
        invite._belfry_send_invite_mail()
        _logger.info("Belfry invite %s mailed for holder %s (steward: %s)",
                     invite.id, holder.id, bool(steward_email))
        return invite._belfry_holder_row()

    def _belfry_send_invite_mail(self):
        template = self.env.ref('belfry_pass.mail_template_invite_by_mail', raise_if_not_found=False)
        if not template:
            return
        for invite in self.sudo():
            template.send_mail(invite.id, force_send=False)
            invite.write({'invite_mail_sent_at': fields.Datetime.now()})
        # the queue cron runs hourly; the invitee is waiting for this mail
        cron = self.env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def belfry_holder_invites(self, partner_id):
        """The invites the holder asked for by e-mail, newest first."""
        _check_service_caller(self.env)
        invites = self.sudo().search([('requested_by_partner_id', '=', int(partner_id))], order='id desc')
        return [invite._belfry_holder_row() for invite in invites]

    @api.model
    def belfry_holder_invite_get(self, partner_id, invite_id):
        _check_service_caller(self.env)
        invite = self.sudo().search(
            [('id', '=', int(invite_id)), ('requested_by_partner_id', '=', int(partner_id))], limit=1)
        return invite._belfry_holder_row() if invite else False

    @api.model
    def belfry_holder_invite_revoke(self, partner_id, invite_id):
        """Revoke one of the holder's open invites; a used or reserved one stays as it is."""
        _check_service_caller(self.env)
        invite = self.sudo().search(
            [('id', '=', int(invite_id)), ('requested_by_partner_id', '=', int(partner_id))], limit=1)
        if not invite:
            raise UserError("No such invite.")
        invite.action_revoke()
        return invite._belfry_holder_row()
