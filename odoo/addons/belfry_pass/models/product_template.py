from odoo import api, fields, models

VAT_RATE = 21.0

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

    # ------------------------------------------------------------------
    # VAT (decision 2026-10-26): 21%, included in the price for individuals, on top of the
    # (higher) company price for organisations. Bootstrap calls belfry_setup_vat on every run.
    # ------------------------------------------------------------------

    @api.model
    def belfry_vat_taxes(self):
        """(tax included in the price, the same rate on top), or empty records before setup."""
        Tax = self.env['account.tax'].sudo()
        params = self.env['ir.config_parameter'].sudo()
        included = Tax.browse(int(params.get_param('belfry.vat_included_tax_id') or 0)).exists()
        excluded = Tax.browse(int(params.get_param('belfry.vat_excluded_tax_id') or 0)).exists()
        return included, excluded

    @api.model
    def belfry_setup_vat(self, rate=VAT_RATE):
        """Find the company's sale tax of ``rate`` percent (the chart's, e.g. l10n_be's "21%"),
        make a price-included twin of it, and put the included one on the pass products. Returns
        the two tax ids. Idempotent."""
        company = self.env.company
        Tax = self.env['account.tax'].sudo().with_company(company)
        domain = [
            ('company_id', '=', company.id), ('type_tax_use', '=', 'sale'),
            ('amount_type', '=', 'percent'), ('amount', '=', rate),
        ]
        included, excluded = self.belfry_vat_taxes()
        if not excluded:
            excluded = Tax.search(domain + [('price_include', '=', False)], order='sequence, id', limit=1)
        if not excluded:
            excluded = Tax.create({
                'name': "%g%%" % rate, 'amount': rate, 'amount_type': 'percent',
                'type_tax_use': 'sale', 'company_id': company.id,
                'price_include_override': 'tax_excluded',
            })
        if not included:
            included = excluded.copy({
                'name': "%s incl." % excluded.name,
                'price_include_override': 'tax_included',
            })
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('belfry.vat_included_tax_id', included.id)
        params.set_param('belfry.vat_excluded_tax_id', excluded.id)
        passes = self.sudo().with_context(active_test=False).search([('belfry_tier', '!=', False)])
        passes.write({'taxes_id': [(6, 0, included.ids)]})
        return [included.id, excluded.id]
