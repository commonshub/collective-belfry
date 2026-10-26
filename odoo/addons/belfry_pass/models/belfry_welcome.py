import secrets
from datetime import timedelta

from odoo import api, fields, models

from .belfry_steward import _check_service_caller

# The welcome page signs in once, within this time after activation (the payer's browser comes
# back from the payment page within minutes; the link stays in its history afterwards).
WELCOME_SIGNIN_HOURS = 24


class BelfryPass(models.Model):
    """Sign-in without a stored authentik link (user test 2026-10-26).

    authentik keeps one recovery token per user, so a second link (the welcome page after paying)
    would kill the mail's. The mail and the welcome page therefore carry Belfry's own one-time
    secrets; the onboarding service (signin.py) mints the authentik link only when one of them is
    used, and redirects there."""

    _inherit = 'belfry.pass'

    signin_token = fields.Char(copy=False, readonly=True, index=True, groups='belfry_pass.group_admin',
                               help="Secret of the mail's sign-in link (pass.<domain>/signin/<token>).")
    signin_used_at = fields.Datetime(copy=False, readonly=True)
    # set per record in create, not as a field default: Odoo would fill the new column of existing
    # rows with one shared default value
    welcome_token = fields.Char(copy=False, readonly=True, index=True, groups='belfry_pass.group_admin',
                                help="Secret of the page the payer lands on after paying (pass.<domain>/welcome/<token>).")
    welcome_used_at = fields.Datetime(copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault('welcome_token', secrets.token_urlsafe(32))
        return super().create(vals_list)

    def belfry_welcome_url(self):
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        token = self.sudo().welcome_token
        return "%s/welcome/%s" % (base, token) if base and token else False

    @api.model
    def _belfry_by_token(self, kind, token):
        field = {'signin': 'signin_token', 'welcome': 'welcome_token'}.get(kind)
        if not field or not token or len(token) < 20:
            return self.browse()
        return self.sudo().search([(field, '=', token)], limit=1)

    @api.model
    def belfry_welcome_info(self, token):
        """What the welcome page shows: the pass's state and, once active, what comes next."""
        _check_service_caller(self.env)
        record = self._belfry_by_token('welcome', token)
        if not record:
            return {'result': 'not_found'}
        invoice = record.invoice_id
        paid = record.state == 'active' or invoice.payment_state in ('paid', 'in_payment') or \
            'done' in invoice.transaction_ids.mapped('state')
        processing = {'pending', 'authorized'} & set(invoice.transaction_ids.mapped('state'))
        return {
            'result': 'ok',
            'state': record.state,
            'payment': 'paid' if paid else ('processing' if processing else 'unpaid'),
            'first_name': (record.partner_id.name or '').split(' ')[0],
            'tier_name': record.tier_name,
            'date_start': fields.Date.to_string(record.date_start) if record.date_start else False,
            'date_end': fields.Date.to_string(record.date_end) if record.date_end else False,
            'claim_token': record.claim_token or False,
            'signin_available': record.state == 'active' and not record.welcome_used_at
                and record._belfry_welcome_fresh(),
        }

    def _belfry_welcome_fresh(self):
        return bool(self.activated_at) and \
            self.activated_at > fields.Datetime.now() - timedelta(hours=WELCOME_SIGNIN_HOURS)

    @api.model
    def belfry_use_signin(self, kind, token):
        """Spend a sign-in secret (``signin``: the mail's link; ``welcome``: the page after
        paying). Returns ``{'result': 'ok', 'authentik_user_id': …}`` or a result of
        ``not_found``, ``not_active``, ``used``, ``expired``, ``no_user``."""
        _check_service_caller(self.env)
        record = self._belfry_by_token(kind, token)
        if not record:
            return {'result': 'not_found'}
        self.env.cr.execute("SELECT id FROM belfry_pass WHERE id = %s FOR UPDATE", (record.id,))
        record.invalidate_recordset()
        if record.state != 'active':
            return {'result': 'not_active'}
        used_field = 'signin_used_at' if kind == 'signin' else 'welcome_used_at'
        if record[used_field]:
            return {'result': 'used'}
        if kind == 'signin':
            expired = record.login_setup_expires_at and record.login_setup_expires_at < fields.Datetime.now()
        else:
            expired = not record._belfry_welcome_fresh()
        if expired:
            return {'result': 'expired'}
        if not record.authentik_user_id:
            return {'result': 'no_user'}
        record.write({used_field: fields.Datetime.now()})
        return {'result': 'ok', 'authentik_user_id': record.authentik_user_id, 'pass_id': record.id}
