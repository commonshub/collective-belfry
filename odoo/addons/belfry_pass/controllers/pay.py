"""Straight to the payment provider's checkout: ``GET /belfry/pay/<invite token>[?method=<code>]``.

The pass API's ``payment_url`` (controllers/api.py). Odoo's payment link opens the invoice with a
Pay dialog where the payer picks a method and presses Pay before the provider's page appears.
This route does those steps in the payer's browser instead: it creates the transaction for the
order's invoice, monitors it in the browser's session (so /payment/status polls it and forwards
to the landing page after paying, payment_status.py) and redirects to the provider's checkout.
Without ``method``, Mollie's checkout shows its own list of methods (models/payment_mollie.py).

Only providers that pay on their own page (Mollie) go straight there; with any other provider
(Stripe pays inside Odoo's page), an unknown method or anything failing, the payer gets Odoo's
payment link as before. A paid order goes to its landing page, an invite without an order to the
invite page (which says why)."""

import logging
from urllib.parse import urlencode

from lxml import html as lxml_html

from odoo import http
from odoo.fields import Command
from odoo.http import request

from odoo.addons.account_payment.controllers.payment import PaymentPortal

_logger = logging.getLogger(__name__)

# providers whose checkout is a page of their own, reached by a GET redirect
REDIRECT_PROVIDERS = ('mollie',)


class BelfryPay(http.Controller):

    @http.route('/belfry/pay/<string:token>', type='http', auth='public', methods=['GET'], sitemap=False)
    def pay(self, token, method=None, **kwargs):
        invite = request.env['belfry.invite'].sudo().search([('token', '=', token)], limit=1)
        belfry_pass = invite.pass_id
        if not belfry_pass or invite.state not in ('reserved', 'used'):
            return request.redirect('/belfry/invite/%s' % token, code=303)
        if invite._belfry_payment_state() != 'unpaid':
            return request.redirect(belfry_pass.belfry_landing_url() or '/belfry/invite/%s' % token,
                                    code=303, local=False)
        try:
            with request.env.cr.savepoint():
                url = self._checkout_url(belfry_pass.invoice_id, method)
        except Exception:
            _logger.exception("belfry pay: no checkout for invite %s, falling back to the payment link", invite.id)
            url = None
        return request.redirect(url or invite.belfry_payment_link(), code=303, local=False)

    def _checkout_url(self, invoice, method):
        """The provider's checkout page for a new transaction on the invoice, or None."""
        invoice = invoice.sudo()
        if invoice.state != 'posted' or invoice.payment_state not in ('not_paid', 'partial'):
            return None
        partner, currency, amount = invoice.partner_id, invoice.currency_id, invoice.amount_residual
        providers = request.env['payment.provider'].sudo()._get_compatible_providers(
            invoice.company_id.id, partner.id, amount, currency_id=currency.id,
        ).filtered(lambda p: p.code in REDIRECT_PROVIDERS)
        if not providers:
            return None
        provider = providers[0]
        methods = request.env['payment.method'].sudo()._get_compatible_payment_methods(
            provider.ids, partner.id, currency_id=currency.id)
        chosen = methods.filtered(lambda m: m.code == method)[:1] if method else methods[:1]
        if not chosen:
            return None
        tx = PaymentPortal()._create_transaction(
            provider_id=provider.id, payment_method_id=chosen.id, token_id=None, amount=amount,
            currency_id=currency.id, partner_id=partner.id, flow='redirect', tokenization_requested=False,
            landing_route='%s?access_token=%s' % (invoice.access_url, invoice._portal_ensure_token()),
            custom_create_values={'invoice_ids': [Command.set(invoice.ids)]},
        )
        values = tx.with_context(belfry_any_method=not method)._get_processing_values()
        if tx.state == 'error' or not values.get('redirect_form_html'):
            return None
        form = lxml_html.fragment_fromstring(str(values['redirect_form_html']))
        if form.tag != 'form' or (form.get('method') or 'get').lower() != 'get' or not form.get('action'):
            return None
        # as a browser submits a GET form: the inputs replace the action's own query
        params = [(i.get('name'), i.get('value') or '') for i in form.iter('input') if i.get('name')]
        action = form.get('action')
        return action.split('?')[0] + '?' + urlencode(params) if params else action
