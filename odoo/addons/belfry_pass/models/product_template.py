from odoo import fields, models

PASS_TIERS = [
    ('day', "Day pass"),
    ('week', "Week pass"),
    ('full', "Full access"),
]


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    belfry_tier = fields.Selection(
        PASS_TIERS,
        string="Belfry pass tier",
        help="Set on pass products only. Copied to the pass when it is purchased.",
    )
    belfry_duration_days = fields.Integer(
        string="Pass duration (days)",
        help="1 for a day pass, 7 for a week pass, 0 for the whole event period.",
    )
