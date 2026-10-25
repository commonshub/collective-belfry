from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'belfry')
class TestErrorPages(HttpCase):

    def test_not_found_is_branded(self):
        self.env['ir.config_parameter'].sudo().set_param('belfry.public_url', 'https://pass.belfry.test')
        response = self.url_open('/belfry-no-such-page')
        self.assertEqual(response.status_code, 404)
        self.assertIn('Page not found', response.text)
        self.assertIn('href="https://pass.belfry.test"', response.text)
        self.assertIn('https://pass.belfry.test/brand/osv-logo-horizontal-white.svg', response.text)
        # no Odoo header with its "Sign in" link to the back office
        self.assertNotIn('/web/login', response.text)
