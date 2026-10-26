"""After paying for a pass, the payer lands on Belfry's welcome page (pass.<domain>/welcome/…),
which waits for the activation and signs the browser in, instead of on Odoo's invoice page."""

from odoo import http
from odoo.http import request

from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing


class BelfryPaymentPostProcessing(PaymentPostProcessing):

    @http.route()
    def poll_status(self, **kwargs):
        values = super().poll_status(**kwargs)
        if values.get('state') not in ('done', 'pending', 'authorized'):
            return values  # failed or cancelled: back to the invoice to try again
        tx = self._get_monitored_transaction()
        belfry_pass = request.env['belfry.pass'].sudo().search(
            [('invoice_id', 'in', tx.invoice_ids.ids)], limit=1) if tx else None
        url = belfry_pass.belfry_welcome_url() if belfry_pass else False
        if url:
            values['landing_route'] = url
        return values
