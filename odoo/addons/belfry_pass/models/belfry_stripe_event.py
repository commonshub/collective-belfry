from odoo import fields, models


class BelfryStripeEvent(models.Model):
    """Processed Stripe events (disputes) of the onboarding service's own Stripe endpoint.
    Written only by that service; the table makes the endpoint idempotent on ``event.id``."""

    _name = 'belfry.stripe.event'
    _description = "Belfry Stripe event"
    _order = 'received_at desc, id desc'
    _rec_name = 'event_id'

    event_id = fields.Char(string="Stripe event id", required=True, index=True, copy=False)
    type = fields.Char(string="Event type", index=True)
    received_at = fields.Datetime(default=fields.Datetime.now, required=True)
    pass_id = fields.Many2one('belfry.pass', string="Pass", ondelete='set null')
    result = fields.Char(help="What the onboarding service did with the event.")

    _event_id_unique = models.Constraint(
        'UNIQUE(event_id)',
        'This Stripe event was already recorded.',
    )
