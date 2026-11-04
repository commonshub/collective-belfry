import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

# Passes whose holder may take free day passes (decision 2026-11-04: week passes).
FREE_DAY_PASS_TIERS = ('week',)


class BelfryPass(models.Model):
    """Free day passes (decided 2026-11-04): a participant with an active week pass takes a day
    pass for any event day on the pass page (pass.<domain>/me), at no cost and at most one per
    day. The day pass is the holder's own, valid that day only (date_start = date_end), without
    invoice or tokens; the onboarding service activates it like a paid one. Revoking the week
    pass revokes the day passes it gave."""

    _inherit = 'belfry.pass'

    free_of_pass_id = fields.Many2one(
        'belfry.pass', string="Free with", readonly=True, copy=False, index=True,
        ondelete='set null',
        help="The week pass this free day pass came with.",
    )

    @api.model
    def _belfry_event_days(self):
        params = self.env['ir.config_parameter'].sudo()
        start = fields.Date.to_date(params.get_param('belfry.event_start') or False)
        end = fields.Date.to_date(params.get_param('belfry.event_end') or False)
        if not start or not end or end < start:
            return []
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    @api.model
    def _belfry_free_day_parent(self, partner_id):
        """The partner's newest active pass that brings free day passes, or an empty record."""
        return self.sudo().search(
            [('partner_id', '=', int(partner_id)), ('state', '=', 'active'),
             ('tier', 'in', FREE_DAY_PASS_TIERS)], order='id desc', limit=1)

    @api.model
    def _belfry_day_passes_by_date(self, partner_id):
        """The partner's pending or active day passes (free or paid), by their first day."""
        passes = self.sudo().search(
            [('partner_id', '=', int(partner_id)), ('tier', '=', 'day'),
             ('state', 'in', ('pending', 'active'))])
        return {p.date_start: p for p in passes if p.date_start}

    @api.model
    def belfry_free_day_passes(self, partner_id, today=False):
        """For the pass page: whether the partner may take free day passes, and every event day
        with its state (available, taken, past) and the day pass that takes it."""
        _check_service_caller(self.env)
        parent = self._belfry_free_day_parent(partner_id)
        if not parent:
            return {'eligible': False, 'days': []}
        today = fields.Date.to_date(today) if today else fields.Date.today()
        taken = self._belfry_day_passes_by_date(partner_id)
        days = []
        for day in self._belfry_event_days():
            held = taken.get(day)
            state = 'taken' if held else ('past' if day < today else 'available')
            days.append({
                'date': fields.Date.to_string(day),
                'state': state,
                'pass_id': held.id if held else False,
                'pass_state': held.state if held else False,
            })
        return {'eligible': True, 'week_pass_id': parent.id, 'days': days}

    @api.model
    def belfry_free_day_pass_create(self, partner_id, day, today=False):
        """A pending free day pass for ``day`` (YYYY-MM-DD); the onboarding service activates it.
        Returns the new pass id."""
        _check_service_caller(self.env)
        parent = self._belfry_free_day_parent(partner_id)
        if not parent:
            raise UserError("Free day passes come with an active week pass.")
        try:
            day = fields.Date.to_date(day)
        except (TypeError, ValueError):
            day = None
        if not day or day not in self._belfry_event_days():
            raise UserError("Pick a day of the event.")
        today = fields.Date.to_date(today) if today else fields.Date.today()
        if day < today:
            raise UserError("That day is over.")
        # one request at a time per participant, so two clicks cannot take the same day twice
        self.env.cr.execute("SELECT id FROM res_partner WHERE id = %s FOR UPDATE", (int(partner_id),))
        if day in self._belfry_day_passes_by_date(partner_id):
            raise UserError("You have a day pass for that day already.")
        day_pass = self.sudo().create({
            'partner_id': int(partner_id),
            'tier': 'day',
            'state': 'pending',
            'date_start': day,
            'date_end': day,
            'free_of_pass_id': parent.id,
            'company_id': parent.company_id.id,
        })
        _logger.info("Belfry free day pass %s for partner %s on %s (with week pass %s)",
                     day_pass.id, partner_id, day, parent.id)
        return day_pass.id
