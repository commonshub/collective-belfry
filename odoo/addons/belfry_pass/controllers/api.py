"""Pass API: the invite page as JSON, for an agent buying a pass on someone's behalf. The invite
token is the credential, as on the page; the checkout is the page's own (InviteCheckout). The
pass, its sign-in link and certificate go to the attendee's e-mail, never to the caller.

    GET  /belfry/api/invites/<token>              passes and prices, or the order waiting for payment
    POST /belfry/api/invites/<token>/order        order a pass: 201 with the invoice and payment link
    POST /belfry/api/invites/<token>/start-over   release an unpaid order, the invite is open again

Errors are ``{"error": code, "message": text}``: 404 ``not_found``, 410 ``used`` / ``expired`` /
``revoked``, 409 ``reserved`` (an order waits already) or ``payment_under_way``, 400
``invalid_json``, 422 ``invalid`` (the message says what to fix)."""

import json
import logging

from odoo import _, http
from odoo.http import request

from .invite import ERROR_STATUS, InviteCheckout, InviteFormError

_logger = logging.getLogger(__name__)

API_PATH = '/belfry/api/invites/%s'
# string fields of an order, as on the invite page's form
ORDER_FIELDS = ('name', 'email', 'company_name', 'vat', 'promo_code', 'nostr_pubkey', 'nostr_proof')


class BelfryPassApi(InviteCheckout, http.Controller):

    @http.route('/belfry/api/invites/<string:token>', type='http', auth='public', methods=['GET'],
                csrf=False, sitemap=False)
    def invite(self, token):
        invite, error = self._invite(token)
        if error == 'reserved':
            return self._json(self._reserved_values(invite))
        if error:
            return self._error(error)
        return self._json(self._offer(invite))

    @http.route('/belfry/api/invites/<string:token>/order', type='http', auth='public', methods=['POST'],
                csrf=False, sitemap=False)
    def order(self, token):
        invite, error = self._invite(token)
        if error:
            return self._error(error, invite)
        try:
            body = request.get_json_data()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._error('invalid_json', message=_("Send the order as a JSON object."), status=400)
        try:
            form = self._order_form(body)
            with request.env.cr.savepoint():
                link = self._checkout(invite, form, path=API_PATH % token + '/order')
        except InviteFormError as exc:
            return self._error('invalid', message=str(exc), status=422)
        invite.invalidate_recordset()
        return self._json(self._reserved_values(invite, link), status=201)

    @http.route('/belfry/api/invites/<string:token>/start-over', type='http', auth='public',
                methods=['POST'], csrf=False, sitemap=False)
    def start_over(self, token):
        invite, error = self._invite(token)
        if error != 'reserved':
            return self._error(error or 'open', invite,
                               message=None if error else _("No order waits for payment."))
        if not invite.belfry_start_over():
            return self._error('payment_under_way', message=_(
                "A payment for this order is under way or done; it cannot be released."))
        invite.invalidate_recordset()
        return self._json(self._offer(invite))

    # ------------------------------------------------------------------

    def _invite(self, token):
        invite = request.env['belfry.invite'].sudo().search([('token', '=', token)], limit=1)
        return invite, invite._belfry_usability_error() if invite else 'not_found'

    def _offer(self, invite):
        values = self._page_values(invite)
        currency = values['currency']
        return {
            'state': 'open',
            'expires_at': invite.expires_at.isoformat() + 'Z',
            'currency': currency.name,
            'early_bird_until': values['early_bird_until'] or None,
            # individuals' prices include VAT, organisations (a company_name) pay theirs plus VAT
            'vat_rate': values['vat_rate'] or None,
            'passes': [{
                'tier': row['template'].belfry_tier,
                'product_id': row['template'].id,
                'name': row['template'].name,
                'list_price': currency.round(row['list_price']),
                'price': currency.round(row['price']),
                'company_price': currency.round(row['company_price']),
            } for row in values['rows']],
            # how to help a visitor through all of it, for agents (skills/open-source-village)
            'skill_url': self._public_url() + '/skill.md',
            'order_fields': {
                'required': ['tier or product_id', 'name', 'email'],
                'optional': ['company_name', 'vat', 'promo_code', 'nostr_pubkey', 'nostr_proof'],
            },
        }

    def _public_url(self):
        return (request.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')

    def _reserved_values(self, invite, link=None):
        payment = invite._belfry_payment_state()
        belfry_pass = invite.sudo().pass_id
        invoice = belfry_pass.invoice_id
        values = {
            'state': 'reserved',
            'payment': payment,
            'pass': {'tier': belfry_pass.tier, 'name': belfry_pass.tier_name},
            'invoice': {
                'number': invoice.name,
                'amount_total': invoice.amount_total,
                'amount_tax': invoice.amount_tax,
                'currency': invoice.currency_id.name,
            } if invoice else None,
            'payment_url': None,
        }
        if payment == 'unpaid':
            values['payment_url'] = link or invite.belfry_payment_link()
        return values

    def _order_form(self, body):
        """The invite page's form from a JSON order: ``tier`` picks the pass by its tier,
        ``nostr_proof`` may be the signed event itself."""
        post = {}
        for key in ORDER_FIELDS:
            value = body.get(key)
            if key == 'nostr_proof' and isinstance(value, dict):
                value = json.dumps(value)
            if value is not None and not isinstance(value, str):
                raise InviteFormError(_("%s must be text.", key))
            post[key] = value
        tier = body.get('tier')
        product_id = body.get('product_id')
        if tier and not product_id:
            template = self._pass_products().filtered(lambda t: t.belfry_tier == tier)[:1]
            product_id = template.id
        if product_id is not None and (isinstance(product_id, bool) or not isinstance(product_id, int)):
            raise InviteFormError(_("product_id must be a number."))
        post['product_id'] = product_id
        post['own_key'] = bool(post['nostr_pubkey'])
        return self._clean_form(post)

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status, headers=[('Cache-Control', 'no-store')])

    def _error(self, code, invite=None, message=None, status=None):
        if code == 'reserved':
            return self._json({'error': 'reserved', 'message': _("An order for this invite waits for payment."),
                               **self._reserved_values(invite)}, status=409)
        messages = {
            'not_found': _("This invite link does not exist."),
            'used': _("This invite link has been used."),
            'expired': _("This invite link has expired. Ask your steward for a new one."),
            'revoked': _("This invite link was revoked."),
        }
        return self._json({'error': code, 'message': message or messages.get(code, code)},
                          status=status or ERROR_STATUS.get(code, 409))
