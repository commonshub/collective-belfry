"""The pass API (controllers/api.py): the invite page's offer and checkout as JSON."""

from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from .common import ensure_chart_of_accounts
from .test_own_key import make_key, proof_for


@tagged('post_install', '-at_install', 'belfry')
class TestPassApi(HttpCase):

    def setUp(self):
        super().setUp()
        ensure_chart_of_accounts(self.env)
        self.invite = self.env['belfry.invite'].create({'note': "for alice"})
        self.url = '/belfry/api/invites/%s' % self.invite.token
        self.product_week = self.env.ref('belfry_pass.product_week')

    def _order(self, **body):
        return self.url_open(self.url + '/order', json=body, allow_redirects=False)

    def test_offer_lists_the_passes_and_prices(self):
        response = self.url_open(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], 'application/json; charset=utf-8')
        offer = response.json()
        self.assertEqual(offer['state'], 'open')
        self.assertEqual(offer['currency'], self.env.ref('belfry_pass.pricelist_public').currency_id.name)
        passes = {p['tier']: p for p in offer['passes']}
        self.assertEqual(set(passes), {'day', 'week', 'full'})
        self.assertEqual(passes['week']['product_id'], self.product_week.id)
        self.assertAlmostEqual(passes['week']['price'], 80.0, msg="early bird")
        self.assertAlmostEqual(passes['full']['company_price'], 240.0)
        self.assertIn('email', offer['order_fields']['required'])

    def test_invite_page_links_the_api_and_the_skill(self):
        self.env['ir.config_parameter'].sudo().set_param('belfry.public_url', 'https://pass.belfry.test')
        page = self.url_open('/belfry/invite/%s' % self.invite.token).text
        self.assertIn('<link rel="alternate" type="application/json" href="%s"/>' % self.url, page)
        self.assertIn('<link rel="help" type="text/markdown" href="https://pass.belfry.test/skill.md"/>', page)
        self.assertEqual(self.url_open(self.url).json()['skill_url'], 'https://pass.belfry.test/skill.md')

    def test_order_reserves_the_invite(self):
        response = self._order(tier='week', name="Alice Normal", email="alice@example.org", promo_code="OSV10")
        self.assertEqual(response.status_code, 201, response.text[:500])
        body = response.json()
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'reserved')
        belfry_pass = self.invite.pass_id
        self.assertEqual(belfry_pass.tier, 'week')
        self.assertEqual(belfry_pass.partner_id.email, 'alice@example.org')
        self.assertEqual(body['state'], 'reserved')
        self.assertEqual(body['payment'], 'unpaid')
        self.assertEqual(body['pass']['tier'], 'week')
        self.assertEqual(body['invoice']['number'], belfry_pass.invoice_id.name)
        self.assertAlmostEqual(body['invoice']['amount_total'], 72.0, msg="early bird 80 minus 10%")
        self.assertIn('move_id=%s' % belfry_pass.invoice_id.id, body['payment_url'])

        # the order waits: GET shows it, a second order is refused and creates nothing
        status = self.url_open(self.url).json()
        self.assertEqual(status['state'], 'reserved')
        self.assertIn('move_id=', status['payment_url'])
        again = self._order(tier='day', name="X", email="x@example.org")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()['error'], 'reserved')
        self.assertEqual(self.env['belfry.pass'].search_count([('invite_id', '=', self.invite.id)]), 1)

    def test_company_order(self):
        response = self._order(product_id=self.product_week.id, name="Bob Acme", email="bob@acme.example",
                               company_name="ACME Corp", vat="BE0123456789")
        self.assertEqual(response.status_code, 201, response.text[:500])
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.pass_id.invoice_id.partner_id.name, "ACME Corp")
        self.assertAlmostEqual(response.json()['invoice']['amount_total'], 120.0)

    def test_invalid_orders_create_nothing(self):
        cases = {
            'no email': {'tier': 'week', 'name': "Eve"},
            'unknown tier': {'tier': 'year', 'name': "Eve", 'email': 'eve@example.org'},
            'bad promo code': {'tier': 'week', 'name': "Eve", 'email': 'eve@example.org', 'promo_code': 'NOPE'},
            'name not text': {'tier': 'week', 'name': 42, 'email': 'eve@example.org'},
            'product_id not a number': {'product_id': "7", 'name': "Eve", 'email': 'eve@example.org'},
        }
        for case, body in cases.items():
            with self.subTest(case):
                response = self._order(**body)
                self.assertEqual(response.status_code, 422, response.text[:500])
                self.assertEqual(response.json()['error'], 'invalid')
                self.assertTrue(response.json()['message'])
        response = self.url_open(self.url + '/order', data='not json', headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'open')
        self.assertFalse(self.env['sale.order'].search([('origin', '=', "Belfry invite #%s" % self.invite.id)]))

    def test_own_key_proof_names_the_api(self):
        d, pubkey = make_key()
        page_proof = proof_for(d, pubkey, '/belfry/invite/%s' % self.invite.token)
        refused = self._order(tier='week', name="Kim", email="kim@example.org", nostr_pubkey=pubkey,
                              nostr_proof=page_proof)
        self.assertEqual(refused.status_code, 422)
        proof = proof_for(d, pubkey, self.url + '/order')
        response = self._order(tier='week', name="Kim", email="kim@example.org", nostr_pubkey=pubkey,
                               nostr_proof=proof)
        self.assertEqual(response.status_code, 201, response.text[:500])
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.pass_id.own_pubkey, pubkey)

    def test_start_over(self):
        self.assertEqual(self.url_open(self.url + '/start-over', method='POST').status_code, 409, "nothing to release")
        self._order(tier='week', name="Alice", email="alice@example.org")
        self.invite.invalidate_recordset()
        invoice = self.invite.pass_id.invoice_id
        response = self.url_open(self.url + '/start-over', data='', method='POST')
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(response.json()['state'], 'open')
        self.assertEqual(invoice.payment_state, 'reversed')

    def test_paid_order_is_not_released(self):
        self._order(tier='week', name="Alice", email="alice@example.org")
        self.invite.invalidate_recordset()
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=self.invite.pass_id.invoice_id.ids).create({})
        wizard.action_create_payments()
        status = self.url_open(self.url).json()
        self.assertEqual(status['payment'], 'paid')
        self.assertIsNone(status['payment_url'])
        response = self.url_open(self.url + '/start-over', data='', method='POST')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'payment_under_way')

    def test_unusable_invites(self):
        self.assertEqual(self.url_open('/belfry/api/invites/no-such-token').status_code, 404)
        self.invite.write({'expires_at': fields.Datetime.now() - timedelta(minutes=1)})
        response = self.url_open(self.url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()['error'], 'expired')
        self.assertEqual(self._order(tier='week', name="A", email="a@example.org").status_code, 410)
