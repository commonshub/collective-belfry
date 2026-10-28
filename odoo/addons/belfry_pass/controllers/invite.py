"""Public invite page: GET shows the passes and the billing form, POST creates the order,
invoice, pending pass and redirects to Odoo's payment link. Until the pass is paid the invite is
reserved: the link offers the payment page again (or starting over) instead of the form."""

import logging

from odoo import Command, _, fields, http
from odoo.http import request

from ..models.nostr_proof import ProofError, check_proof, npub

_logger = logging.getLogger(__name__)

# invite usability error -> HTTP status of the error page
ERROR_STATUS = {
    'not_found': 404,
    'used': 410,
    'expired': 410,
    'revoked': 410,
}


class InviteFormError(Exception):
    """A validation problem with the submitted form; the message is shown to the visitor."""


class InviteCheckout:
    """Prices and checkout of an invite, shared by the invite page and the pass API
    (controllers/api.py)."""

    def _pass_products(self):
        return request.env['product.template'].sudo().search(
            [('belfry_tier', '!=', False), ('sale_ok', '=', True)],
            order='list_price asc, id asc',
        )

    def _page_values(self, invite, form=None, error=None):
        env = request.env
        public = env.ref('belfry_pass.pricelist_public').sudo()
        company = env.ref('belfry_pass.pricelist_company').sudo()
        now = fields.Datetime.now()
        rows = []
        for template in self._pass_products():
            variant = template.product_variant_id
            rows.append({
                'template': template,
                'list_price': template.list_price,
                'price': public._get_product_price(variant, 1.0, date=now),
                'company_price': company._get_product_price(variant, 1.0, date=now),
            })
        early_bird = any(row['price'] < row['list_price'] for row in rows)
        included, excluded = env['product.template'].sudo().belfry_vat_taxes()
        form = dict(form or {})
        if not form.get('product_id') and rows:
            form['product_id'] = rows[0]['template'].id
        return {
            'invite': invite,
            'rows': rows,
            'currency': public.currency_id,
            'early_bird': early_bird,
            'early_bird_until': env['ir.config_parameter'].sudo().get_param('belfry.early_bird_until'),
            'form': form,
            'error': error,
            'vat_rate': excluded.amount if included and excluded else False,
            'no_header': True,
            'no_footer': False,
        }

    # ------------------------------------------------------------------
    # checkout
    # ------------------------------------------------------------------

    def _clean_form(self, post):
        def clean(key):
            return (post.get(key) or '').strip()

        return {
            'product_id': int(post.get('product_id') or 0),
            'name': clean('name'),
            'email': clean('email'),
            'company_name': clean('company_name'),
            'vat': clean('vat'),
            'promo_code': clean('promo_code'),
            # joining with a key of one's own: filled by the page's script from the signer
            'own_key': bool(post.get('own_key')),
            'nostr_pubkey': clean('nostr_pubkey').lower(),
            'nostr_proof': clean('nostr_proof'),
        }

    def _validate_form(self, form):
        if not form['name']:
            raise InviteFormError(_("Please enter your name."))
        email = form['email']
        if not email or '@' not in email or email.startswith('@') or email.endswith('@'):
            raise InviteFormError(_("Please enter a valid email address."))
        template = self._pass_products().filtered(lambda t: t.id == form['product_id'])
        if not template:
            raise InviteFormError(_("Please choose a pass."))
        return template

    def _own_key(self, invite, form, person, path):
        """The checked public key (hex) when the visitor joins with a key of their own, else
        None. The proof is a signed POST to ``path``, the page the form was sent to. The key
        must not belong to anyone else, and a returning participant who has a key already keeps
        it (they can move to another one on their account page)."""
        if not form['own_key']:
            return None
        if not form['nostr_pubkey'] or not form['nostr_proof']:
            raise InviteFormError(_(
                "To join with your own Nostr key, allow your signer to sign when you send the form."
            ))
        try:
            pubkey = check_proof(form['nostr_proof'], form['nostr_pubkey'], path)
        except ProofError as exc:
            raise InviteFormError(str(exc)) from None
        key_npub = npub(pubkey)
        env = request.env
        if person.belfry_npub and person.belfry_npub != key_npub:
            raise InviteFormError(_(
                "Your account has a Nostr key already. Join with that one; you can move to this key "
                "on your account page afterwards."
            ))
        taken = env['res.partner'].sudo().search_count(
            [('belfry_npub', '=', key_npub), ('id', '!=', person.id)], limit=1,
        ) or env['belfry.pass'].sudo().search_count(
            ['|', ('keycast_pubkey', '=', pubkey), ('own_pubkey', '=', pubkey),
             ('partner_id', '!=', person.id), ('state', 'in', ('pending', 'active'))], limit=1,
        )
        if taken:
            raise InviteFormError(_("This Nostr key belongs to another participant."))
        return pubkey

    def _find_or_create_partners(self, form):
        """Return (person, billing partner). The billing partner is the company when a company
        name was given, else the person."""
        Partner = request.env['res.partner'].sudo()
        person = Partner.search(
            [('email', '=ilike', form['email']), ('is_company', '=', False)],
            order='id asc',
            limit=1,
        )
        if not person:
            person = Partner.create({
                'name': form['name'],
                'email': form['email'],
                'type': 'contact',
            })
        if not form['company_name']:
            if form['vat'] and not person.vat:
                person.write({'vat': form['vat']})
            return person, person

        company = Partner.search(
            [('is_company', '=', True), ('name', '=ilike', form['company_name'])],
            order='id asc',
            limit=1,
        )
        if not company:
            company = Partner.create({
                'name': form['company_name'],
                'is_company': True,
                'vat': form['vat'] or False,
            })
        elif form['vat'] and not company.vat:
            company.write({'vat': form['vat']})
        company_pricelist = request.env.ref('belfry_pass.pricelist_company').sudo()
        if company.property_product_pricelist != company_pricelist:
            company.write({'property_product_pricelist': company_pricelist.id})
        if person.parent_id != company:
            person.write({'parent_id': company.id})
        return person, company

    def _tax_excluded(self, order):
        """Swap the pass products' VAT (included in the price) for the same rate on top."""
        excluded = request.env['product.template'].sudo().belfry_vat_taxes()[1]
        if not excluded:
            return
        for line in order.order_line:
            if line.tax_ids and line.tax_ids != excluded:
                line.tax_ids = excluded

    def _apply_promo_code(self, order, code):
        result = order._try_apply_code(code)
        if result.get('error'):
            raise InviteFormError(result['error'])
        applied = False
        for coupon, rewards in result.items():
            reward = rewards[:1]
            if reward:
                order._apply_program_reward(reward, coupon)
                applied = True
        if not applied:
            raise InviteFormError(_("This code does not apply to the selected pass."))
        order._update_programs_and_rewards()

    def _checkout(self, invite, form, path=None):
        """Create partner, order, invoice, payment link and pending pass. Runs inside a
        savepoint; raising InviteFormError rolls everything back. ``path``: where the form was
        sent (the invite page unless given), for the own key's proof."""
        env = request.env
        template = self._validate_form(form)

        # take the invite row lock so two submissions of the same link cannot both pass
        env.cr.execute("SELECT id FROM belfry_invite WHERE id = %s FOR UPDATE NOWAIT", (invite.id,))
        invite.invalidate_recordset(['state', 'expires_at'])
        if invite._belfry_usability_error():
            raise InviteFormError(_("This invite link can no longer be used."))

        person, billing_partner = self._find_or_create_partners(form)
        own_pubkey = self._own_key(invite, form, person, path or '/belfry/invite/%s' % invite.token)
        product = template.product_variant_id

        order = env['sale.order'].sudo().create({
            'partner_id': billing_partner.id,
            'partner_invoice_id': billing_partner.id,
            'partner_shipping_id': person.id,
            'user_id': invite.steward_id.id,
            'origin': "Belfry invite #%s" % invite.id,
            'order_line': [Command.create({
                'product_id': product.id,
                'product_uom_qty': 1,
            })],
        })
        if billing_partner.is_company:
            # organisations pay the company price plus VAT; individuals' prices include it
            self._tax_excluded(order)
        if form['promo_code']:
            self._apply_promo_code(order, form['promo_code'])

        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()

        wizard = env['payment.link.wizard'].sudo().with_context(
            active_model='account.move', active_id=invoice.id, active_ids=invoice.ids,
        ).create({})
        link = wizard.link

        belfry_pass = env['belfry.pass'].sudo().create({
            'partner_id': person.id,
            'tier': template.belfry_tier,
            'product_id': product.id,
            'sale_order_id': order.id,
            'invoice_id': invoice.id,
            'invite_id': invite.id,
            'company_id': order.company_id.id,
            'own_pubkey': own_pubkey or False,
            'own_pubkey_proof': form['nostr_proof'] if own_pubkey else False,
        })
        invite.sudo().write({
            'state': 'reserved',
            'used_by_partner_id': person.id,
            'pass_id': belfry_pass.id,
        })
        _logger.info(
            "Belfry invite %s reserved by partner %s: order %s, invoice %s, pass %s",
            invite.id, person.id, order.name, invoice.name, belfry_pass.id,
        )
        return link


class BelfryInviteController(InviteCheckout, http.Controller):

    @http.route(
        '/belfry/invite/<string:token>',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        # website=True only tells http_routing (a portal dependency) to prepare the frontend
        # rendering environment (languages, portal layout); the website module is not needed
        website=True,
        sitemap=False,
    )
    def invite(self, token, **post):
        invite = request.env['belfry.invite'].sudo().search([('token', '=', token)], limit=1)
        error = invite._belfry_usability_error() if invite else 'not_found'
        if error == 'reserved':
            return self._reserved(invite, post)
        if error:
            return self._render_error(error)

        if request.httprequest.method == 'GET':
            return request.render('belfry_pass.invite_page', self._page_values(invite))

        form = self._clean_form(post)
        try:
            with request.env.cr.savepoint():
                link = self._checkout(invite, form)
        except InviteFormError as exc:
            values = self._page_values(invite, form=form, error=str(exc))
            return request.render('belfry_pass.invite_page', values)
        return request.redirect(link, code=303, local=False)

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _reserved(self, invite, post):
        """The form was sent and the order waits for payment (a refresh, the back button, a
        failed card attempt): offer the payment page again, or start over while nothing is paid.
        The link is the credential, so the page names the first name only."""
        payment = invite._belfry_payment_state()
        if request.httprequest.method == 'POST' and post.get('start_over'):
            invite.belfry_start_over()
            return request.redirect('/belfry/invite/%s' % invite.token, code=303)
        person = invite.used_by_partner_id
        values = {
            'invite': invite,
            'first_name': (person.name or '').split(' ')[0],
            'tier_name': invite.pass_id.tier_name,
            'payment': payment,
            'payment_link': invite.belfry_payment_link() if payment == 'unpaid' else False,
            'no_header': True,
            'no_footer': False,
        }
        return request.render('belfry_pass.invite_reserved', values)

    def _render_error(self, reason):
        messages = {
            'not_found': _("This invite link does not exist."),
            'used': _("This invite link has been used. If it was you, your pass is active: "
                      "the sign-in link is in your mail."),
            'expired': _("This invite link has expired. Ask your steward for a new one."),
            'revoked': _("This invite link was revoked."),
        }
        values = {'reason': reason, 'message': messages.get(reason, messages['not_found'])}
        return request.render('belfry_pass.invite_error', values, status=ERROR_STATUS.get(reason, 400))
