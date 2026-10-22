import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

WEBHOOK_PARAMS = ('belfry.onboarding_url', 'belfry.hook_token')

# server action xml id -> path on the onboarding service
WEBHOOK_ACTIONS = {
    'belfry_pass.action_webhook_invoice_paid': '/hooks/odoo/invoice-paid',
    'belfry_pass.action_webhook_refund': '/hooks/odoo/refund',
}


class IrConfigParameter(models.Model):
    """The webhook URL of an ``ir.actions.server`` is a stored string, so it is rewritten
    from ``belfry.onboarding_url`` and ``belfry.hook_token`` whenever one of them changes
    (and once more in the post-init hook)."""

    _inherit = 'ir.config_parameter'

    @api.model_create_multi
    def create(self, vals_list):
        params = super().create(vals_list)
        if any(vals.get('key') in WEBHOOK_PARAMS for vals in vals_list):
            self.belfry_sync_webhook_urls()
        return params

    def write(self, vals):
        keys_before = set(self.mapped('key'))
        res = super().write(vals)
        if (keys_before | set(self.mapped('key'))) & set(WEBHOOK_PARAMS):
            self.belfry_sync_webhook_urls()
        return res

    def unlink(self):
        touched = set(self.mapped('key')) & set(WEBHOOK_PARAMS)
        res = super().unlink()
        if touched:
            self.belfry_sync_webhook_urls()
        return res

    @api.model
    def belfry_webhook_url(self, path):
        base = (self.sudo().get_param('belfry.onboarding_url') or '').rstrip('/')
        token = self.sudo().get_param('belfry.hook_token') or ''
        return "%s%s?token=%s" % (base, path, token)

    @api.model
    def belfry_sync_webhook_urls(self):
        """Rewrite the webhook URLs of the two Belfry server actions from the parameters.
        Safe to call at any time (also over RPC, e.g. from the bootstrap script)."""
        for xmlid, path in WEBHOOK_ACTIONS.items():
            action = self.env.ref(xmlid, raise_if_not_found=False)
            if not action:
                continue  # module data not loaded yet
            url = self.belfry_webhook_url(path)
            if action.webhook_url != url:
                action.sudo().write({'webhook_url': url})
                _logger.info("Belfry webhook %s now posts to %s", xmlid, url.split('?')[0])
        return True
