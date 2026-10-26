import re
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from .common import ensure_chart_of_accounts


@tagged('post_install', '-at_install', 'belfry')
class TestInviteFlow(HttpCase):

    def setUp(self):
        super().setUp()
        ensure_chart_of_accounts(self.env)
        self.steward = self.env['res.users'].create({
            'name': "Steward Sam",
            'login': 'steward-http@belfry.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id, self.env.ref('belfry_pass.group_steward').id])],
        })
        invite = self.env['belfry.invite'].with_user(self.steward).create({'note': "for alice"})
        # re-browse as the test superuser: the steward cannot read orders and invoices
        self.invite = self.env['belfry.invite'].browse(invite.id)
        self.product_full = self.env.ref('belfry_pass.product_full')
        self.product_week = self.env.ref('belfry_pass.product_week')

    def _get(self, token):
        return self.url_open('/belfry/invite/%s' % token)

    def _post(self, token, **form):
        page = self._get(token)
        self.assertEqual(page.status_code, 200)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        self.assertTrue(match, "csrf token on the page")
        self._csrf = match.group(1)
        data = {'csrf_token': self._csrf, **form}
        return self.url_open('/belfry/invite/%s' % token, data=data, allow_redirects=False)

    def test_invite_model(self):
        self.assertEqual(self.invite.state, 'open')
        self.assertEqual(self.invite.steward_id, self.steward)
        self.assertGreaterEqual(len(self.invite.token), 40)
        self.assertTrue(self.invite.url.endswith('/belfry/invite/%s' % self.invite.token))
        self.assertGreater(self.invite.expires_at, fields.Datetime.now() + timedelta(days=13))

    def test_invite_urls_of_several_records(self):
        # The list view computes the link for all rows at once (failed on the test deployment
        # with "Expected singleton" once there were three invites).
        Invite = self.env['belfry.invite'].with_user(self.steward)
        Invite.create({'note': "for bob"})
        invites = Invite.search([])
        self.assertEqual(len(invites), 2)
        rows = invites.web_search_read([], {'url': {}, 'token': {}})['records']
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row['url'].endswith('/belfry/invite/%s' % row['token']))

    def test_steward_sees_only_own_invites(self):
        other = self.env['res.users'].create({
            'name': "Other Steward",
            'login': 'other-steward@belfry.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id, self.env.ref('belfry_pass.group_steward').id])],
        })
        self.env['belfry.invite'].with_user(other).create({'note': "other"})
        self.assertEqual(self.env['belfry.invite'].with_user(self.steward).search([]), self.invite)
        self.assertEqual(len(self.env['belfry.invite'].with_user(self.env.ref('base.user_admin')).search([])), 2)

    def test_get_shows_passes_and_prices(self):
        response = self._get(self.invite.token)
        self.assertEqual(response.status_code, 200)
        for name in ('Day pass', 'Week pass', 'Full access'):
            self.assertIn(name, response.text)
        self.assertIn('40.00', response.text)
        self.assertIn('160.00', response.text)
        self.assertIn('240.00', response.text, "company price shown")
        self.assertIn('csrf_token', response.text)

    def test_share_preview_names_the_village(self):
        # link previews (Signal, WhatsApp) read the title and the Open Graph tags
        self.invite.inviter_name = "Steward Sam"
        page = self._get(self.invite.token).text
        self.assertIn('<title>You are invited to Open Source Village</title>', page)
        self.assertNotIn('<title>Odoo</title>', page)
        self.assertIn('property="og:title" content="You are invited to Open Source Village"', page)
        self.assertIn('property="og:description" content="Steward Sam invited you.', page)
        self.assertRegex(page, r'property="og:image" content="[^"]*/brand/web-app-manifest-512x512\.png"')
        gone = self._get('no-such-token').text
        self.assertIn('<title>You are invited to Open Source Village</title>', gone)
        self.assertIn('content="An invitation to Open Source Village.', gone)

    def test_invoice_page_links_lead_to_belfry(self):
        # participants open the invoice by its access token, without an Odoo login
        self.env['ir.config_parameter'].sudo().set_param('belfry.public_url', 'https://pass.belfry.test')
        partner = self.env['res.partner'].create({'name': "Alice", 'email': 'alice@belfry.test'})
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {'product_id': self.product_week.product_variant_id.id, 'product_uom_qty': 1})],
        })
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        page = self.url_open(invoice.get_portal_url())
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn('href="https://pass.belfry.test/me"', html)
        self.assertIn('>Your pass</a>', html)
        self.assertRegex(html, r'class="navbar-brand logo"[^>]*href="https://pass\.belfry\.test/"|href="https://pass\.belfry\.test/"[^>]*class="navbar-brand logo"')
        self.assertRegex(html, r'<a aria-label="Open Source Village"[^>]*href="https://pass\.belfry\.test/"><i class="fa fa-home">')
        # the PDF download (/my/invoices/<id>?access_token=…) works without a login and stays
        for odoo_link in ('href="/web/login', 'href="/my/home"', 'href="/my/invoices?', 'href="/"'):
            self.assertNotIn(odoo_link, html)
        self.assertRegex(html, r'<li name="invoices_breadcrumb"[^>]*>\s*Invoices & Bills\s*</li>')
        self.assertNotIn('Salesperson', html)

    def test_unknown_token(self):
        self.assertEqual(self._get('no-such-token').status_code, 404)

    def test_expired_invite_refused(self):
        self.invite.write({'expires_at': fields.Datetime.now() - timedelta(minutes=1)})
        response = self._get(self.invite.token)
        self.assertEqual(response.status_code, 410)
        self.assertIn('expired', response.text)
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'expired')

    def test_revoked_invite_refused(self):
        self.invite.action_revoke()
        self.assertEqual(self.invite.state, 'revoked')
        self.assertEqual(self._get(self.invite.token).status_code, 410)

    def test_post_creates_order_invoice_pass_and_redirects(self):
        response = self._post(
            self.invite.token,
            product_id=self.product_full.id,
            name="Alice Normal",
            email="Alice@Example.org",
            promo_code="OSV10",
        )
        self.assertEqual(response.status_code, 303, response.text[:500])
        location = response.headers.get('Location', '')
        self.assertIn('payment', location)

        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'reserved', "used only once the pass is paid")
        partner = self.invite.used_by_partner_id
        self.assertEqual(partner.email, 'Alice@Example.org')
        self.assertFalse(partner.is_company)

        belfry_pass = self.invite.pass_id
        self.assertEqual(belfry_pass.state, 'pending')
        self.assertEqual(belfry_pass.tier, 'full')
        self.assertEqual(belfry_pass.partner_id, partner)
        order = belfry_pass.sale_order_id
        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.user_id, self.steward)
        self.assertAlmostEqual(order.amount_total, 144.0, msg="early bird 160 minus 10%")
        invoice = belfry_pass.invoice_id
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.move_type, 'out_invoice')
        self.assertAlmostEqual(invoice.amount_total, 144.0)
        self.assertIn('move_id=%s' % invoice.id, location)

        # a second submission (refresh, back button) offers the payment again and creates nothing
        again = self._post_without_get(self.invite.token, product_id=self.product_week.id, name="X", email="x@example.org")
        self.assertEqual(again.status_code, 200)
        self.assertIn('belfry_continue_payment', again.text)
        self.assertEqual(self.env['belfry.pass'].search_count([('invite_id', '=', self.invite.id)]), 1)

    def _post_without_get(self, token, **form):
        # same session, so the token of the first page is still valid
        return self.url_open('/belfry/invite/%s' % token, data={'csrf_token': self._csrf, **form}, allow_redirects=False)

    def test_post_company_billing(self):
        response = self._post(
            self.invite.token,
            product_id=self.product_week.id,
            name="Bob Acme",
            email="bob@acme.example",
            company_name="ACME Corp",
            vat="BE0123456789",
        )
        self.assertEqual(response.status_code, 303, response.text[:500])
        self.invite.invalidate_recordset()
        belfry_pass = self.invite.pass_id
        person = belfry_pass.partner_id
        company = person.parent_id
        self.assertTrue(company.is_company)
        self.assertEqual(company.name, "ACME Corp")
        self.assertEqual(company.vat, "BE0123456789")
        self.assertEqual(company.property_product_pricelist, self.env.ref('belfry_pass.pricelist_company'))
        order = belfry_pass.sale_order_id
        self.assertEqual(order.partner_id, company)
        self.assertEqual(order.partner_invoice_id, company)
        self.assertEqual(order.partner_shipping_id, person)
        self.assertAlmostEqual(order.amount_total, 120.0, msg="early bird 80 +50%")
        self.assertEqual(belfry_pass.invoice_id.partner_id, company)

    def test_post_existing_partner_is_reused(self):
        existing = self.env['res.partner'].create({'name': "Carol", 'email': 'carol@example.org'})
        response = self._post(self.invite.token, product_id=self.product_week.id, name="Carol X", email="CAROL@example.org")
        self.assertEqual(response.status_code, 303)
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.used_by_partner_id, existing)

    def test_post_bad_promo_code_keeps_invite_open(self):
        response = self._post(self.invite.token, product_id=self.product_week.id, name="Dan", email="dan@example.org", promo_code="NOPE")
        self.assertEqual(response.status_code, 200)
        self.assertIn('belfry_form_error', response.text)
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'open')
        self.assertFalse(self.env['sale.order'].search([('origin', '=', "Belfry invite #%s" % self.invite.id)]))

    def test_post_missing_email(self):
        response = self._post(self.invite.token, product_id=self.product_week.id, name="Eve", email="")
        self.assertEqual(response.status_code, 200)
        self.assertIn('belfry_form_error', response.text)
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'open')
