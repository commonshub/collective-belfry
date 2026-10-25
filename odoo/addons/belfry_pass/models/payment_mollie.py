import logging

from odoo import _, fields, models

from odoo.addons.payment.const import CURRENCY_MINOR_UNITS

_logger = logging.getLogger(__name__)

# Mollie refund statuses (https://docs.mollie.com/reference/get-refund): a refund Mollie accepted
# counts as done (the pass is revoked at once; Mollie usually pays it out the next business day,
# and test mode leaves refunds pending); queued ones wait for balance. A refund canceled later
# leaves the pass revoked, a steward reactivates it.
REFUND_DONE = {'pending', 'processing', 'refunded'}
REFUND_PENDING = {'queued'}
REFUND_CANCELED = {'failed', 'canceled'}


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    def _compute_feature_support_fields(self):
        """Odoo's Mollie module cannot refund; Belfry adds refunds (payment_transaction below), so
        a refund from the invoice reaches Mollie and revokes the pass like a Stripe refund."""
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'mollie').update({'support_refund': 'partial'})


class PaymentTransaction(models.Model):
    """Mollie for Belfry: refunds and chargebacks.

    Mollie calls the payment's webhook (set by Odoo's Mollie module to /payment/mollie/webhook)
    for every change of the payment, refunds and chargebacks included; the module then reads the
    payment from Mollie's API and only updates its status. Here, on every such update of a paid
    payment, the refunds and chargebacks are read as well:

    - every Mollie refund becomes a refund transaction (child of the payment, keyed on the refund
      id), done once Mollie accepted it, so the automation "refund done" notifies the onboarding
      service (refunds made in Mollie's dashboard as well as from Odoo);
    - an open chargeback sets ``belfry_chargeback_state`` to ``open``, a reversed one to
      ``reversed``; the automation "chargeback" notifies the onboarding service, which revokes the
      pass or gives it back (like Stripe's disputes, which Belfry receives on its own endpoint).

    Webhook calls carry only the payment id, and everything is read back from Mollie's API with
    the provider's key, so a forged call changes nothing.
    """

    _inherit = 'payment.transaction'

    belfry_chargeback_id = fields.Char(string="Chargeback", readonly=True, copy=False)
    belfry_chargeback_state = fields.Selection(
        [('open', "Open"), ('reversed', "Reversed")],
        string="Chargeback state", readonly=True, copy=False,
        help="Set from the payment provider (Mollie); the onboarding service revokes the pass "
             "while a chargeback is open and gives it back when it is reversed.",
    )

    def _send_refund_request(self):
        """Send a refund to Mollie (POST /payments/<id>/refunds)."""
        if self.provider_code != 'mollie':
            return super()._send_refund_request()
        source = self.source_transaction_id
        places = CURRENCY_MINOR_UNITS.get(self.currency_id.name, self.currency_id.decimal_places)
        refund = self._send_api_request('POST', f'/payments/{source.provider_reference}/refunds', json={
            'amount': {'currency': self.currency_id.name, 'value': f"{-self.amount:.{places}f}"},
            'description': self.reference,
        })
        self.provider_reference = refund.get('id')
        self._belfry_mollie_apply_refund_status(refund.get('status'))

    def _apply_updates(self, payment_data):
        super()._apply_updates(payment_data)
        if self.provider_code != 'mollie' or self.operation == 'refund' or self.state != 'done':
            return
        links = payment_data.get('_links') or {}

        def amount(key):
            try:
                return float((payment_data.get(key) or {}).get('value') or 0)
            except (TypeError, ValueError):
                return 0.0

        if 'refunds' in links or amount('amountRefunded') > 0:
            self._belfry_mollie_sync_refunds()
        if 'chargebacks' in links or amount('amountChargedBack') > 0:
            self._belfry_mollie_sync_chargebacks()

    def _belfry_mollie_sync_refunds(self):
        data = self._send_api_request('GET', f'/payments/{self.provider_reference}/refunds')
        for refund in (data.get('_embedded') or {}).get('refunds') or []:
            refund_id = refund.get('id')
            if not refund_id:
                continue
            child = self.child_transaction_ids.filtered(
                lambda t: t.operation == 'refund' and t.provider_reference == refund_id
            )[:1]
            if not child:
                if refund.get('status') in REFUND_CANCELED:
                    continue
                value = float((refund.get('amount') or {}).get('value') or 0)
                child = self._create_child_transaction(
                    value, is_refund=True, provider_reference=refund_id
                )
                _logger.info("Mollie refund %s of %s recorded as %s", refund_id, self.reference, child.reference)
            child._belfry_mollie_apply_refund_status(refund.get('status'))

    def _belfry_mollie_apply_refund_status(self, status):
        if status in REFUND_DONE and self.state != 'done':
            self._set_done()
        elif status in REFUND_PENDING and self.state not in ('pending', 'done'):
            self._set_pending()
        elif status in REFUND_CANCELED and self.state not in ('cancel', 'done'):
            self._set_canceled(_("Mollie refund %s", status))

    def _belfry_mollie_sync_chargebacks(self):
        data = self._send_api_request('GET', f'/payments/{self.provider_reference}/chargebacks')
        chargebacks = (data.get('_embedded') or {}).get('chargebacks') or []
        open_ones = [c for c in chargebacks if not c.get('reversedAt')]
        if open_ones:
            vals = {'belfry_chargeback_state': 'open', 'belfry_chargeback_id': open_ones[0].get('id')}
        elif chargebacks:
            vals = {'belfry_chargeback_state': 'reversed', 'belfry_chargeback_id': chargebacks[0].get('id')}
        else:
            return
        if self.belfry_chargeback_state != vals['belfry_chargeback_state']:
            _logger.info("Mollie chargeback on %s: %s", self.reference, vals['belfry_chargeback_state'])
            self.write(vals)
