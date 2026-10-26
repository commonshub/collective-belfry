import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

DEFAULT_INVITES_PER_PARTICIPANT = 3


class BelfryInvite(models.Model):
    """Invites from participants: a participant with an active pass invites friends from the pass
    page (pass.<domain>/me), up to ``belfry.invites_per_participant`` invites that are open or
    used; revoked and expired ones give the slot back. The invite page names who invited."""

    _inherit = 'belfry.invite'

    inviter_partner_id = fields.Many2one(
        'res.partner', string="Invited by (participant)", readonly=True, copy=False, index=True,
        help="Set when a participant made the invite on the pass page.",
    )
    inviter_name = fields.Char(
        string="Invited by", readonly=True, copy=False,
        help="The name the invite page shows: the participant's, or the steward's from the "
             "steward pages.",
    )

    def _belfry_invite_row(self):
        self.ensure_one()
        self._belfry_usability_error()  # moves an open invite past its expiry to expired
        return {
            'id': self.id,
            'url': self.url,
            'state': self.state,
            'note': self.note or '',
            'expires_at': fields.Datetime.to_string(self.expires_at) if self.expires_at else False,
            'used_by': self.used_by_partner_id.name or '',
        }

    @api.model
    def _belfry_participant_limit(self):
        value = self.env['ir.config_parameter'].sudo().get_param('belfry.invites_per_participant')
        try:
            return max(0, int(value)) if value not in (None, False, '') else DEFAULT_INVITES_PER_PARTICIPANT
        except ValueError:
            return DEFAULT_INVITES_PER_PARTICIPANT

    @api.model
    def _belfry_participant(self, partner_id):
        partner = self.env['res.partner'].sudo().browse(int(partner_id)).exists()
        active = partner and self.env['belfry.pass'].sudo().search_count(
            [('partner_id', '=', partner.id), ('state', '=', 'active')])
        if not active:
            raise UserError("Only participants with an active pass can invite friends.")
        return partner

    @api.model
    def belfry_participant_invites(self, partner_id):
        """The participant's invites, newest first, with the limit and how many are left."""
        _check_service_caller(self.env)
        invites = self.sudo().search([('inviter_partner_id', '=', int(partner_id))], order='id desc')
        rows = [invite._belfry_invite_row() for invite in invites]
        limit = self._belfry_participant_limit()
        taken = sum(1 for r in rows if r['state'] in ('open', 'reserved', 'used'))
        return {'limit': limit, 'left': max(0, limit - taken), 'invites': rows}

    @api.model
    def belfry_participant_create(self, partner_id, note=False):
        """A new invite from the participant (active pass required, within the limit)."""
        _check_service_caller(self.env)
        partner = self._belfry_participant(partner_id)
        if not self.belfry_participant_invites(partner.id)['left']:
            raise UserError("You have no invites left. Revoke an unused one, or ask a steward.")
        invite = self.sudo().create({
            'inviter_partner_id': partner.id,
            'inviter_name': partner.name,
            'note': (note or '').strip()[:200] or False,
        })
        _logger.info("Belfry invite %s by participant %s", invite.id, partner.id)
        return invite._belfry_invite_row()

    @api.model
    def belfry_participant_revoke(self, partner_id, invite_id):
        """Revoke one of the participant's own open invites."""
        _check_service_caller(self.env)
        invite = self.sudo().search(
            [('id', '=', int(invite_id)), ('inviter_partner_id', '=', int(partner_id))], limit=1)
        if not invite:
            raise UserError("No such invite.")
        invite.action_revoke()
        return invite._belfry_invite_row()
