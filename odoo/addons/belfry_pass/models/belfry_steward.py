import logging

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


def _check_service_caller(env):
    """The steward pages (onboarding service, /steward) call these methods as the service user,
    which is a Belfry admin; the steward has signed in through Authentik there. Stewards with an
    Odoo login use the back office instead."""
    if not env.user.has_group('belfry_pass.group_admin'):
        raise AccessError("Only Belfry admins (the onboarding service) may act for stewards.")


class BelfryInvite(models.Model):
    _inherit = 'belfry.invite'

    steward_email = fields.Char(
        string="Steward e-mail",
        readonly=True,
        copy=False,
        help="Set when the invite was made on Belfry's steward pages: who admitted the participant. "
             "steward_id is then the matching Odoo user, or the service user when the steward has none.",
    )

    @api.model
    def belfry_steward_create(self, steward_email, note=False, steward_name=False):
        """Create an invite for the steward signed in on the steward pages. Returns id, url, token."""
        _check_service_caller(self.env)
        steward_email = (steward_email or '').strip()
        if not steward_email:
            raise UserError("A steward e-mail is required.")
        vals = {'steward_email': steward_email, 'note': note or False,
                'inviter_name': (steward_name or '').strip()[:100] or False}
        user = self.env['res.users'].sudo().search([('login', '=ilike', steward_email)], limit=1)
        if user:
            vals['steward_id'] = user.id
        invite = self.create(vals)
        return {'id': invite.id, 'url': invite.url, 'token': invite.token}


class BelfryPass(models.Model):
    _inherit = 'belfry.pass'

    manual_paid_by = fields.Char(
        string="Marked paid by",
        readonly=True,
        copy=False,
        help="Steward who registered the payment by hand on the steward pages. The onboarding "
             "service accepts a manual payment only with this mark (or ALLOW_MANUAL_PAYMENTS).",
    )
    manual_paid_at = fields.Datetime(string="Marked paid at", readonly=True, copy=False)

    def belfry_steward_mark_paid(self, steward_email):
        """Register the full payment of a pending pass's invoice by hand (bank transfer, cash at
        the door) for the steward signed in on the steward pages. The mark goes on the pass
        first, so the invoice-paid webhook that the payment fires finds it."""
        _check_service_caller(self.env)
        self.ensure_one()
        steward_email = (steward_email or '').strip()
        if not steward_email:
            raise UserError("A steward e-mail is required.")
        if self.state != 'pending':
            raise UserError("Only a pending pass can be marked paid (this one is %s)." % self.state)
        invoice = self.invoice_id.sudo()
        if not invoice or invoice.state != 'posted':
            raise UserError("The pass has no posted invoice.")
        if invoice.payment_state in ('paid', 'in_payment'):
            raise UserError("The invoice is already paid.")
        self.write({'manual_paid_by': steward_email, 'manual_paid_at': fields.Datetime.now()})
        wizard = self.env['account.payment.register'].sudo().with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({'communication': "%s (marked paid by %s)" % (invoice.name, steward_email)})
        wizard.action_create_payments()
        _logger.info("Belfry pass %s: invoice %s marked paid by steward %s",
                     self.id, invoice.name, steward_email)
        return invoice.payment_state

    steward_pass = fields.Boolean(
        string="Steward pass", readonly=True, copy=False,
        help="Given free to a steward (decided 2026-10-30): a zero-amount invoice, activated like "
             "a paid one.",
    )

    @api.model
    def belfry_steward_pass(self, email, name=False):
        """The free pass of a steward (onboarding ``add-steward`` and ``steward-passes``): the
        ``belfry.steward_pass_tier`` pass (default full access) on an invoice with a 100 %
        discount, posted, hence paid, and marked like a steward's hand payment so activation
        accepts it. Idempotent: a person who has a pending or active pass gets that one back.
        Returns ``{'pass_id', 'invoice_id', 'state', 'created'}``."""
        _check_service_caller(self.env)
        email = (email or '').strip()
        if '@' not in email:
            raise UserError("A steward e-mail is required.")
        Partner = self.env['res.partner'].sudo()
        person = Partner.search([('email', '=ilike', email), ('is_company', '=', False)],
                                order='id asc', limit=1)
        if not person:
            person = Partner.create({'name': (name or '').strip() or email, 'email': email, 'type': 'contact'})
        existing = self.sudo().search(
            [('partner_id', '=', person.id), ('state', 'in', ('pending', 'active'))], order='id desc', limit=1)
        if existing:
            return {'pass_id': existing.id, 'invoice_id': existing.invoice_id.id or False,
                    'state': existing.state, 'created': False}
        tier = self.env['ir.config_parameter'].sudo().get_param('belfry.steward_pass_tier') or 'full'
        template = self.env['product.template'].sudo().search([('belfry_tier', '=', tier)], limit=1)
        if not template:
            raise UserError("No pass product for the steward pass tier %r." % tier)
        product = template.product_variant_id
        order = self.env['sale.order'].sudo().create({
            'partner_id': person.id,
            'origin': "Belfry steward pass",
            'order_line': [Command.create({'product_id': product.id, 'product_uom_qty': 1, 'discount': 100.0})],
        })
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        if invoice.payment_state not in ('paid', 'in_payment'):
            raise UserError("The steward pass invoice %s is not settled (%s)." % (invoice.name, invoice.payment_state))
        record = self.sudo().create({
            'partner_id': person.id,
            'tier': tier,
            'product_id': product.id,
            'sale_order_id': order.id,
            'invoice_id': invoice.id,
            'company_id': order.company_id.id,
            'steward_pass': True,
            'manual_paid_by': "steward pass (%s)" % email,
            'manual_paid_at': fields.Datetime.now(),
        })
        _logger.info("Belfry steward pass %s for %s (invoice %s)", record.id, email, invoice.name)
        return {'pass_id': record.id, 'invoice_id': invoice.id, 'state': record.state, 'created': True}
