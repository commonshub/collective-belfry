import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .product_template import PASS_TIERS

_logger = logging.getLogger(__name__)


class BelfryPass(models.Model):
    """One row per purchased pass. Created pending by the invite flow (or by the onboarding
    service for invoices made by hand) and activated by the onboarding service once the
    invoice is paid."""

    _name = 'belfry.pass'
    _description = "Belfry pass"
    _order = 'id desc'
    _rec_name = 'display_name'

    partner_id = fields.Many2one('res.partner', string="Pass holder", required=True, index=True, ondelete='restrict')
    tier = fields.Selection(PASS_TIERS, required=True)
    tier_name = fields.Char(compute='_compute_tier_name')
    product_id = fields.Many2one('product.product', string="Pass product")
    sale_order_id = fields.Many2one('sale.order', string="Sales order", ondelete='set null')
    invoice_id = fields.Many2one('account.move', string="Invoice", index=True, ondelete='set null')
    transaction_id = fields.Many2one('payment.transaction', string="Payment transaction", ondelete='set null')
    stripe_payment_intent = fields.Char(
        string="Stripe PaymentIntent",
        copy=False,
        help="provider_reference of the done payment transaction (pi_...).",
    )
    date_start = fields.Date(string="Valid from")
    date_end = fields.Date(string="Valid until")
    state = fields.Selection(
        [
            ('pending', "Pending payment"),
            ('active', "Active"),
            ('expired', "Expired"),
            ('revoked', "Revoked"),
        ],
        default='pending',
        required=True,
        index=True,
        copy=False,
    )
    invite_id = fields.Many2one('belfry.invite', string="Invite", ondelete='set null')
    npub = fields.Char(string="npub at activation", copy=False)
    keycast_pubkey = fields.Char(string="Keycast pubkey (hex)", copy=False)
    # Joining with a key of one's own (invite page): the public key and the signed NIP-98 event
    # that proves control of it (models/nostr_proof.py). Activation then creates a signer account
    # without a key of Belfry's, and the partner holds their own key from the start.
    own_pubkey = fields.Char(string="Own Nostr key (hex)", copy=False, readonly=True)
    own_pubkey_proof = fields.Text(string="Proof of the own key", copy=False, readonly=True,
                                   groups='belfry_pass.group_admin')
    authentik_user_id = fields.Integer(string="Authentik user id at activation", copy=False)
    activated_at = fields.Datetime(copy=False)
    revoked_at = fields.Datetime(copy=False)
    revoke_reason = fields.Selection(
        [
            ('refund', "Refund"),
            ('chargeback', "Chargeback"),
            ('steward', "Steward decision"),
            ('expiry', "Expiry"),
        ],
        copy=False,
    )
    stripe_dispute_id = fields.Char(copy=False)
    # Phase 2 login: a one-time sign-in link into Authentik (participants have no password;
    # later sign-ins use the login page's magic link). Minted by the onboarding service at
    # activation and on request ("Send new login link"); writing it on an active pass mails the
    # activation template.
    login_setup_url = fields.Char(string="Sign-in link", copy=False, readonly=True)
    login_setup_expires_at = fields.Datetime(string="Login link valid until", copy=False, readonly=True)
    login_mail_sent_at = fields.Datetime(string="Login mail sent", copy=False, readonly=True)
    # Phase 3: the pass certificate. The claim token is the secret in the participant's claim
    # link (the onboarding service mints a wallet offer when the page is opened); the status
    # list index is the certificate's bit in Belfry's Token Status List (= the pass id).
    claim_token = fields.Char(string="Claim token", copy=False, readonly=True, index=True,
                              groups="belfry_pass.group_admin")
    claim_url = fields.Char(string="Claim link", compute='_compute_claim_url',
                            groups="belfry_pass.group_admin")
    # Tokens (decision 2026-10-24): 50 per pass day, minted by the onboarding service at
    # activation at Belfry's mint (no monetary value). The cashuB token is a bearer secret until
    # the participant redeems it into a wallet through the tokens link (/tokens/<claim token>).
    cashu_token = fields.Text(string="Token bundle", copy=False, readonly=True,
                              groups="belfry_pass.group_admin")
    cashu_amount = fields.Integer(string="Tokens", copy=False, readonly=True)
    tokens_url = fields.Char(string="Tokens link", compute='_compute_tokens_url',
                             groups="belfry_pass.group_admin")
    credential_status_index = fields.Integer(
        string="Status list index",
        copy=False,
        readonly=True,
        help="Bit of this pass in Belfry's Token Status List (status_list.idx of the certificate).",
    )
    member_number = fields.Char(related='partner_id.belfry_member_number', string="Member number")
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    amount_total = fields.Monetary(related='invoice_id.amount_total', string="Invoice total")
    payment_state = fields.Selection(related='invoice_id.payment_state', string="Invoice payment state")

    _invoice_unique = models.Constraint(
        'UNIQUE(invoice_id)',
        'There is already a pass for this invoice.',
    )
    _stripe_payment_intent_unique = models.Constraint(
        'UNIQUE(stripe_payment_intent)',
        'There is already a pass for this Stripe PaymentIntent.',
    )

    @api.depends('partner_id.name', 'tier', 'state')
    def _compute_display_name(self):
        tiers = dict(PASS_TIERS)
        for record in self:
            record.display_name = "%s - %s" % (record.partner_id.name or "?", tiers.get(record.tier, record.tier))

    @api.depends('claim_token')
    def _compute_claim_url(self):
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        for record in self:
            record.claim_url = "%s/claim/%s" % (base, record.claim_token) if record.claim_token and base else False

    @api.depends('claim_token', 'cashu_token')
    def _compute_tokens_url(self):
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        for record in self:
            record.tokens_url = ("%s/tokens/%s" % (base, record.claim_token)
                                 if record.claim_token and record.cashu_token and base else False)

    @api.depends('tier')
    def _compute_tier_name(self):
        tiers = dict(PASS_TIERS)
        for record in self:
            record.tier_name = tiers.get(record.tier, record.tier or "")

    def write(self, vals):
        res = super().write(vals)
        if vals.get('state') == 'active':
            # belt and braces: the service assigns the number before activation, but make
            # sure an active pass never belongs to a partner without one
            self.partner_id.filtered(lambda p: not p.belfry_member_number).belfry_ensure_member_number()
        if vals.get('login_setup_url'):
            # at activation (state and link in one write) and when a steward asks for a new link
            self.filtered(lambda p: p.state == 'active')._belfry_send_login_mail()
        return res

    # ------------------------------------------------------------------
    # login link mail
    # ------------------------------------------------------------------

    @api.model
    def belfry_apps_url(self):
        """Where participants use the community apps (Keycast's web app for now)."""
        return self.env['ir.config_parameter'].sudo().get_param('belfry.apps_url') or ''

    def belfry_login_url(self):
        """Authentik's login page: e-mail magic link or the village pass."""
        return self.env['ir.config_parameter'].sudo().get_param('belfry.login_url') or ''

    def _belfry_send_login_mail(self):
        """Queue the activation mail (pass details and the one-time sign-in link) for every pass
        in ``self``. Odoo's mail queue sends it; failures stay visible in Settings > Technical >
        Emails because the template keeps its mails."""
        template = self.env.ref('belfry_pass.mail_template_pass_activated', raise_if_not_found=False)
        if not template:
            return
        queued = False
        for record in self:
            if not record.partner_id.email:
                _logger.warning("Belfry pass %s: holder %s has no e-mail, login link not mailed",
                                record.id, record.partner_id.id)
                continue
            template.sudo().send_mail(record.id, force_send=False)
            record.write({'login_mail_sent_at': fields.Datetime.now()})
            queued = True
            _logger.info("Belfry pass %s: sign-in mail queued for %s", record.id, record.partner_id.email)
        if queued:
            # The queue cron runs hourly; wake it now, so the mail leaves right after this commit
            # (the sign-in link is what the participant waits for).
            cron = self.env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()

    def action_revoke(self):
        """Steward decision: the onboarding service revokes the pass (relay, signer, Authentik
        group and the certificate's status list bit all follow)."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(_("Only an active pass can be revoked."))
        url = self.env['ir.config_parameter'].sudo().belfry_webhook_url('/admin/passes/%d/revoke' % self.id)
        try:
            response = requests.post(url, timeout=20)
            response.raise_for_status()
            result = response.json().get('result')
        except (requests.RequestException, ValueError) as exc:
            raise UserError(_("The onboarding service did not answer: %s", exc)) from exc
        if result not in ('revoked', 'already_revoked'):
            raise UserError(_("The onboarding service answered '%s'.", result))
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def action_send_login_link(self):
        """Steward button: ask the onboarding service for a new link. The service mints it in
        Authentik and writes it on this pass, which queues the mail (see ``write``)."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(_("Only an active pass can get a login link."))
        if not self.partner_id.email:
            raise UserError(_("%s has no e-mail address.", self.partner_id.display_name))
        url = self.env['ir.config_parameter'].sudo().belfry_webhook_url('/admin/passes/%d/login-link' % self.id)
        try:
            response = requests.post(url, timeout=20)
            response.raise_for_status()
            result = response.json().get('result')
        except (requests.RequestException, ValueError) as exc:
            raise UserError(_("The onboarding service did not answer: %s", exc)) from exc
        if result != 'sent':
            raise UserError(_("The onboarding service answered '%s' instead of 'sent'.", result))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Login link sent"),
                'message': _("A new login link was mailed to %s.", self.partner_id.email),
                'type': 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    @api.model
    def _belfry_partner_for_invoice(self, invoice):
        """The pass holder is the person, not the company that is billed."""
        shipping = invoice.partner_shipping_id
        if shipping and not shipping.is_company:
            return shipping
        partner = invoice.partner_id
        if partner.is_company:
            contact = partner.child_ids.filtered(lambda p: not p.is_company and p.type == 'contact')[:1]
            return contact or partner
        return partner

    @api.model
    def belfry_find_or_create_for_invoice(self, invoice_id):
        """Return the id of the pass for ``invoice_id``, creating a pending one when the
        invoice was not made through the invite flow (e.g. a steward made the order by hand).
        Partner and tier are derived from the invoice lines' product.

        Public so the onboarding service can call it over RPC.
        """
        invoice = self.env['account.move'].browse(int(invoice_id)).exists()
        if not invoice:
            raise UserError(_("Invoice %s does not exist.", invoice_id))
        existing = self.search([('invoice_id', '=', invoice.id)], limit=1)
        if existing:
            return existing.id
        if invoice.move_type != 'out_invoice':
            raise UserError(_("%s is not a customer invoice.", invoice.display_name))
        line = invoice.invoice_line_ids.filtered(lambda l: l.product_id.belfry_tier)[:1]
        if not line:
            raise UserError(_("Invoice %s has no pass product line.", invoice.display_name))
        product = line.product_id
        order = line.sale_line_ids.order_id[:1]
        new_pass = self.create({
            'partner_id': self._belfry_partner_for_invoice(invoice).id,
            'tier': product.belfry_tier,
            'product_id': product.id,
            'sale_order_id': order.id,
            'invoice_id': invoice.id,
            'company_id': invoice.company_id.id,
        })
        return new_pass.id
