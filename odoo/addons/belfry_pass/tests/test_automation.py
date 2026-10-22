from odoo.fields import Domain
from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestAutomation(BelfryCase):

    def _rule_matches(self, rule, record):
        domain = Domain(safe_eval(rule.filter_domain))
        return bool(record.filtered_domain(domain))

    def test_rules_exist(self):
        invoice_rule = self.env.ref('belfry_pass.automation_invoice_paid')
        refund_rule = self.env.ref('belfry_pass.automation_refund_done')
        self.assertTrue(invoice_rule.active and refund_rule.active)
        self.assertEqual(invoice_rule.model_id.model, 'account.move')
        self.assertEqual(invoice_rule.trigger, 'on_create_or_write')
        self.assertIn('payment_state', invoice_rule.trigger_field_ids.mapped('name'))
        self.assertEqual(refund_rule.model_id.model, 'payment.transaction')
        self.assertIn('state', refund_rule.trigger_field_ids.mapped('name'))
        for rule in (invoice_rule, refund_rule):
            self.assertEqual(len(rule.action_server_ids), 1)
            self.assertEqual(rule.action_server_ids.state, 'webhook')

    def test_webhook_urls_follow_config_parameters(self):
        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param('belfry.onboarding_url', 'http://glue.test:8100/')
        Param.set_param('belfry.hook_token', 'secret-1')
        self.assertEqual(
            self.env.ref('belfry_pass.action_webhook_invoice_paid').webhook_url,
            'http://glue.test:8100/hooks/odoo/invoice-paid?token=secret-1',
        )
        self.assertEqual(
            self.env.ref('belfry_pass.action_webhook_refund').webhook_url,
            'http://glue.test:8100/hooks/odoo/refund?token=secret-1',
        )
        Param.set_param('belfry.hook_token', 'secret-2')
        self.assertTrue(
            self.env.ref('belfry_pass.action_webhook_refund').webhook_url.endswith('?token=secret-2')
        )

    def test_invoice_rule_matches_paid_invoice_only(self):
        rule = self.env.ref('belfry_pass.automation_invoice_paid')
        _order, invoice = self._make_posted_invoice(self.alice, self.product_week)
        self.assertEqual(invoice.payment_state, 'not_paid')
        self.assertFalse(self._rule_matches(rule, invoice))
        self._register_payment(invoice)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertTrue(self._rule_matches(rule, invoice))
        # a draft or supplier invoice never matches
        draft = invoice.copy()
        self.assertFalse(self._rule_matches(rule, draft))

    def test_refund_rule_matches_done_refund_only(self):
        rule = self.env.ref('belfry_pass.automation_refund_done')
        provider = self.env.ref('payment.payment_provider_stripe')
        Method = self.env['payment.method'].with_context(active_test=False)
        method = Method.search([('code', '=', 'card')], limit=1) or Method.search([], limit=1)
        self.assertTrue(method)
        currency = self.env.company.currency_id
        source = self.env['payment.transaction'].create({
            'provider_id': provider.id,
            'payment_method_id': method.id,
            'reference': 'BELFRY-TEST-SRC',
            'amount': 100.0,
            'currency_id': currency.id,
            'partner_id': self.alice.id,
            'operation': 'online_direct',
            'state': 'done',
        })
        self.assertFalse(self._rule_matches(rule, source))
        refund = self.env['payment.transaction'].create({
            'provider_id': provider.id,
            'payment_method_id': method.id,
            'reference': 'R-BELFRY-TEST-SRC',
            'amount': -100.0,
            'currency_id': currency.id,
            'partner_id': self.alice.id,
            'operation': 'refund',
            'source_transaction_id': source.id,
            'state': 'draft',
        })
        self.assertFalse(self._rule_matches(rule, refund))
        refund.write({'state': 'done'})
        self.assertTrue(self._rule_matches(rule, refund))
