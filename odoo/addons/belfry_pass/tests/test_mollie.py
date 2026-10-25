from unittest.mock import patch

from odoo.fields import Domain
from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestMollie(BelfryCase):
    """Mollie refunds and chargebacks (models/payment_mollie.py), Mollie's API replaced by a dict."""

    def setUp(self):
        super().setUp()
        self.provider = self.env.ref('payment.payment_provider_mollie')
        self.provider.write({'state': 'test', 'mollie_api_key': 'test_dummy'})
        Method = self.env['payment.method'].with_context(active_test=False)
        method = Method.search([('code', '=', 'card')], limit=1)
        self.source = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': method.id,
            'reference': 'BELFRY-MOLLIE-1',
            'provider_reference': 'tr_1',
            'amount': 100.0,
            'currency_id': self.env.company.currency_id.id,
            'partner_id': self.alice.id,
            'operation': 'online_redirect',
            'state': 'done',
        })
        self.api = {}  # (method, endpoint) -> response
        self.calls = []

        def fake(tx, method, endpoint, *args, **kwargs):
            self.calls.append((method, endpoint, kwargs.get('json')))
            return self.api[(method, endpoint)]

        patcher = patch.object(
            self.env.registry['payment.transaction'], '_send_api_request', autospec=True, side_effect=fake
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _webhook(self, **extra):
        """What Odoo's Mollie module does on a webhook call: read the payment, apply it."""
        self.source._apply_updates({'id': 'tr_1', 'status': 'paid', 'method': '', **extra})

    def _refunds(self, *refunds):
        self.api[('GET', '/payments/tr_1/refunds')] = {'_embedded': {'refunds': list(refunds)}}

    def _matches(self, xmlid, record):
        rule = self.env.ref(xmlid)
        return bool(record.filtered_domain(Domain(safe_eval(rule.filter_domain))))

    def test_mollie_can_refund(self):
        self.assertEqual(self.provider.support_refund, 'partial')

    def test_plain_payment_reads_nothing_more(self):
        self._webhook(amountRefunded={'value': '0.00'}, _links={'self': {}})
        self.assertEqual(self.calls, [])

    def test_refund_from_mollies_dashboard_becomes_a_done_refund(self):
        self._refunds({'id': 're_1', 'status': 'queued', 'amount': {'value': '100.00'}})
        self._webhook(amountRefunded={'value': '100.00'}, _links={'refunds': {}})
        refund = self.source.child_transaction_ids
        self.assertEqual(len(refund), 1)
        self.assertEqual((refund.operation, refund.provider_reference, refund.amount), ('refund', 're_1', -100.0))
        self.assertEqual(refund.state, 'pending')
        self.assertFalse(self._matches('belfry_pass.automation_refund_done', refund))
        # Mollie accepts it: the same transaction is done, which notifies the onboarding service
        self._refunds({'id': 're_1', 'status': 'pending', 'amount': {'value': '100.00'}})
        self._webhook(amountRefunded={'value': '100.00'}, _links={'refunds': {}})
        self.assertEqual(self.source.child_transaction_ids, refund)
        self.assertEqual(refund.state, 'done')
        self.assertTrue(self._matches('belfry_pass.automation_refund_done', refund))

    def test_failed_refund_never_counts(self):
        self._refunds({'id': 're_9', 'status': 'failed', 'amount': {'value': '100.00'}})
        self._webhook(_links={'refunds': {}})
        self.assertFalse(self.source.child_transaction_ids)

    def test_refund_from_odoo_is_sent_to_mollie(self):
        self.api[('POST', '/payments/tr_1/refunds')] = {'id': 're_2', 'status': 'queued'}
        refund = self.source._refund(amount_to_refund=40.0)
        self.assertEqual(self.calls[0][2]['amount']['value'], '40.00')
        self.assertEqual((refund.provider_reference, refund.state), ('re_2', 'pending'))
        self._refunds({'id': 're_2', 'status': 'pending', 'amount': {'value': '40.00'}})
        self._webhook(amountRefunded={'value': '40.00'})
        self.assertEqual(self.source.child_transaction_ids, refund)
        self.assertEqual(refund.state, 'done')

    def test_chargeback_opens_and_reverses(self):
        rule = 'belfry_pass.automation_chargeback'
        self.assertFalse(self._matches(rule, self.source))
        self.api[('GET', '/payments/tr_1/chargebacks')] = {'_embedded': {'chargebacks': [{'id': 'chb_1'}]}}
        self._webhook(amountChargedBack={'value': '100.00'}, _links={'chargebacks': {}})
        self.assertEqual((self.source.belfry_chargeback_state, self.source.belfry_chargeback_id), ('open', 'chb_1'))
        self.assertTrue(self._matches(rule, self.source))
        self.api[('GET', '/payments/tr_1/chargebacks')] = {
            '_embedded': {'chargebacks': [{'id': 'chb_1', 'reversedAt': '2027-01-30T10:00:00+00:00'}]}
        }
        self._webhook(amountChargedBack={'value': '0.00'}, _links={'chargebacks': {}})
        self.assertEqual(self.source.belfry_chargeback_state, 'reversed')

    def test_chargeback_webhook_url_follows_parameters(self):
        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param('belfry.onboarding_url', 'http://glue.test:8100')
        Param.set_param('belfry.hook_token', 'secret-3')
        self.assertEqual(
            self.env.ref('belfry_pass.action_webhook_chargeback').webhook_url,
            'http://glue.test:8100/hooks/odoo/chargeback?token=secret-3',
        )
