from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .product_template import PASS_TIERS


class BelfryPass(models.Model):
    """One row per purchased pass. Created pending by the invite flow (or by the onboarding
    service for invoices made by hand) and activated by the onboarding service once the
    invoice is paid."""

    _name = 'belfry.pass'
    _description = "Belfry pass"
    _order = 'id desc'
    _rec_name = 'display_name'

    partner_id = fields.Many2one('res.partner', string="Pass holder", required=True, index=True, ondelete='restrict')
    tier = fields.Selection(PASS_TIERS, required=True)
    product_id = fields.Many2one('product.product', string="Pass product")
    sale_order_id = fields.Many2one('sale.order', string="Sales order", ondelete='set null')
    invoice_id = fields.Many2one('account.move', string="Invoice", index=True, ondelete='set null')
    transaction_id = fields.Many2one('payment.transaction', string="Payment transaction", ondelete='set null')
    stripe_payment_intent = fields.Char(
        string="Stripe PaymentIntent",
        copy=False,
        help="provider_reference of the done payment transaction (pi_...).",
    )
    date_start = fields.Date(string="Valid from")
    date_end = fields.Date(string="Valid until")
    state = fields.Selection(
        [
            ('pending', "Pending payment"),
            ('active', "Active"),
            ('expired', "Expired"),
            ('revoked', "Revoked"),
        ],
        default='pending',
        required=True,
        index=True,
        copy=False,
    )
    invite_id = fields.Many2one('belfry.invite', string="Invite", ondelete='set null')
    npub = fields.Char(string="npub at activation", copy=False)
    keycast_pubkey = fields.Char(string="Keycast pubkey (hex)", copy=False)
    authentik_user_id = fields.Integer(string="Authentik user id at activation", copy=False)
    activated_at = fields.Datetime(copy=False)
    revoked_at = fields.Datetime(copy=False)
    revoke_reason = fields.Selection(
        [
            ('refund', "Refund"),
            ('chargeback', "Chargeback"),
            ('steward', "Steward decision"),
            ('expiry', "Expiry"),
        ],
        copy=False,
    )
    stripe_dispute_id = fields.Char(copy=False)
    credential_status_index = fields.Integer(
        string="Status list index",
        copy=False,
        help="Reserved for Phase 3 (token status list bit of the pass certificate).",
    )
    member_number = fields.Char(related='partner_id.belfry_member_number', string="Member number")
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    amount_total = fields.Monetary(related='invoice_id.amount_total', string="Invoice total")
    payment_state = fields.Selection(related='invoice_id.payment_state', string="Invoice payment state")

    _invoice_unique = models.Constraint(
        'UNIQUE(invoice_id)',
        'There is already a pass for this invoice.',
    )
    _stripe_payment_intent_unique = models.Constraint(
        'UNIQUE(stripe_payment_intent)',
        'There is already a pass for this Stripe PaymentIntent.',
    )

    @api.depends('partner_id.name', 'tier', 'state')
    def _compute_display_name(self):
        tiers = dict(PASS_TIERS)
        for record in self:
            record.display_name = "%s - %s" % (record.partner_id.name or "?", tiers.get(record.tier, record.tier))

    def write(self, vals):
        res = super().write(vals)
        if vals.get('state') == 'active':
            # belt and braces: the service assigns the number before activation, but make
            # sure an active pass never belongs to a partner without one
            self.partner_id.filtered(lambda p: not p.belfry_member_number).belfry_ensure_member_number()
        return res

    @api.model
    def _belfry_partner_for_invoice(self, invoice):
        """The pass holder is the person, not the company that is billed."""
        shipping = invoice.partner_shipping_id
        if shipping and not shipping.is_company:
            return shipping
        partner = invoice.partner_id
        if partner.is_company:
            contact = partner.child_ids.filtered(lambda p: not p.is_company and p.type == 'contact')[:1]
            return contact or partner
        return partner

    @api.model
    def belfry_find_or_create_for_invoice(self, invoice_id):
        """Return the id of the pass for ``invoice_id``, creating a pending one when the
        invoice was not made through the invite flow (e.g. a steward made the order by hand).
        Partner and tier are derived from the invoice lines' product.

        Public so the onboarding service can call it over RPC.
        """
        invoice = self.env['account.move'].browse(int(invoice_id)).exists()
        if not invoice:
            raise UserError(_("Invoice %s does not exist.", invoice_id))
        existing = self.search([('invoice_id', '=', invoice.id)], limit=1)
        if existing:
            return existing.id
        if invoice.move_type != 'out_invoice':
            raise UserError(_("%s is not a customer invoice.", invoice.display_name))
        line = invoice.invoice_line_ids.filtered(lambda l: l.product_id.belfry_tier)[:1]
        if not line:
            raise UserError(_("Invoice %s has no pass product line.", invoice.display_name))
        product = line.product_id
        order = line.sale_line_ids.order_id[:1]
        new_pass = self.create({
            'partner_id': self._belfry_partner_for_invoice(invoice).id,
            'tier': product.belfry_tier,
            'product_id': product.id,
            'sale_order_id': order.id,
            'invoice_id': invoice.id,
            'company_id': invoice.company_id.id,
        })
        return new_pass.id
