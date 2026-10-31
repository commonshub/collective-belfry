import logging

from odoo import api, fields, models

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

ACCOUNTING_STATES = [
    ('todo', "To book"),
    ('done', "Booked"),
    ('refunded', "Credit note booked"),
    ('skipped', "Not booked"),
    ('failed', "Failed"),
    ('attention', "Needs the accountant"),
]


class BelfryPass(models.Model):
    """Accounting elsewhere: another organisation sells the tickets and keeps the books in its
    own Odoo (``belfry.invoice_issuer``). Belfry's invoice then stays internal (the payment
    link and the pass hang on it) and the onboarding service books each paid ticket there as
    a customer invoice with its payment, and a refund as a credit note
    (``belfry_onboarding/accounting.py``). The result is kept on the pass."""

    _inherit = 'belfry.pass'

    accounting_state = fields.Selection(ACCOUNTING_STATES, string="Booked by the issuer",
                                        copy=False, readonly=True)
    accounting_ref = fields.Char(string="Issuer's invoice", copy=False, readonly=True)
    accounting_move_id = fields.Integer(string="Issuer's invoice id", copy=False, readonly=True)
    accounting_refund_ref = fields.Char(string="Issuer's credit note", copy=False, readonly=True)
    accounting_refund_move_id = fields.Integer(string="Issuer's credit note id", copy=False,
                                               readonly=True)
    accounting_error = fields.Char(string="Booking error", copy=False, readonly=True)
    accounting_synced_at = fields.Datetime(string="Booked at", copy=False, readonly=True)

    @api.model
    def belfry_invoice_issuer(self):
        """The organisation that invoices the tickets, or '' when Belfry's own invoice is it."""
        return self.env['ir.config_parameter'].sudo().get_param('belfry.invoice_issuer') or ''

    def belfry_accounting_export(self):
        """What the issuer's invoice needs, for the onboarding service (whose user may not read
        taxes or countries): the buyer, the lines with their tax kind, the payment and refunds."""
        _check_service_caller(self.env)
        self.ensure_one()
        record = self.sudo()
        invoice = record.invoice_id
        partner = invoice.partner_id
        lines = []
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            taxes = line.tax_ids
            lines.append({
                'name': line.name or line.product_id.display_name,
                'tier': line.product_id.belfry_tier or False,
                'quantity': line.quantity,
                'price_unit': line.price_unit,
                'discount': line.discount,
                'price_total': line.price_total,
                # 'included' (individuals), 'excluded' (organisations), 'none' or 'several'
                'tax': ('none' if not taxes else 'several' if len(taxes) > 1
                        else 'included' if taxes.price_include else 'excluded'),
                'tax_amount': taxes.amount if len(taxes) == 1 else False,
            })
        tx = record.transaction_id
        payments = invoice._get_reconciled_payments() if invoice else self.env['account.payment']
        refunds = (tx.child_transaction_ids.filtered(
            lambda t: t.operation == 'refund' and t.state == 'done') if tx else tx)
        return {
            'pass_id': record.id,
            'state': record.state,
            'revoke_reason': record.revoke_reason or False,
            'invoice': invoice and {
                'id': invoice.id,
                'name': invoice.name,
                'date': fields.Date.to_string(invoice.invoice_date),
                'state': invoice.state,
                'payment_state': invoice.payment_state,
                'amount_total': invoice.amount_total,
                'currency': invoice.currency_id.name,
            },
            'partner': {
                'name': partner.name or '',
                'email': partner.email or record.partner_id.email or '',
                'is_company': partner.is_company,
                'vat': partner.vat or False,
                'street': partner.street or False,
                'street2': partner.street2 or False,
                'zip': partner.zip or False,
                'city': partner.city or False,
                'country_code': partner.country_id.code or False,
            },
            'lines': lines,
            'payment': {
                'date': fields.Date.to_string(max(payments.mapped('date'))) if payments else False,
                'provider': tx.provider_id.code if tx else ('manual' if payments else False),
                'reference': (tx.provider_reference or tx.reference) if tx
                else (record.manual_paid_by or False),
            },
            'refunded': -sum(refunds.mapped('amount')) if refunds else 0.0,
            'refund_date': (fields.Date.to_string(max(refunds.mapped('last_state_change')).date())
                            if refunds else False),
        }
