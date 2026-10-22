"""Install-time setup that cannot be expressed as plain XML data."""

import logging

from odoo import fields

_logger = logging.getLogger(__name__)

EARLY_BIRD_PRICES = {
    'belfry_pass.product_day': 40.0,
    'belfry_pass.product_week': 80.0,
    'belfry_pass.product_full': 160.0,
}


def _enable_pricelists(env):
    """Turn on the "Pricelists" feature (Sales > Settings > Pricing).

    Without the feature group, partners get no pricelist and list prices apply,
    so early-bird and company prices would never be used.
    """
    group_user = env.ref('base.group_user')
    group_pricelist = env.ref('product.group_product_pricelist')
    if group_pricelist not in group_user.all_implied_ids:
        group_user._apply_group(group_pricelist)


def _create_early_bird_items(env):
    """Early-bird fixed prices on the public pricelist, valid until ``belfry.early_bird_until``.

    XML data cannot read a config parameter, so the rules are created here.
    Idempotent: each item gets an xml id (``belfry_pass.early_bird_<product>``)
    and is skipped when it already exists.
    """
    until = env['ir.config_parameter'].sudo().get_param('belfry.early_bird_until', '2026-12-31')
    date_end = fields.Datetime.to_datetime(until).replace(hour=23, minute=59, second=59)
    pricelist = env.ref('belfry_pass.pricelist_public')
    for product_xmlid, price in EARLY_BIRD_PRICES.items():
        item_xmlid = 'belfry_pass.early_bird_%s' % product_xmlid.split('.')[1]
        if env.ref(item_xmlid, raise_if_not_found=False):
            continue
        template = env.ref(product_xmlid)
        item = env['product.pricelist.item'].create({
            'pricelist_id': pricelist.id,
            'applied_on': '1_product',
            'product_tmpl_id': template.id,
            'compute_price': 'fixed',
            'fixed_price': price,
            'date_end': date_end,
        })
        env['ir.model.data']._update_xmlids([{
            'xml_id': item_xmlid,
            'record': item,
            'noupdate': True,
        }])
        _logger.info("Early-bird rule %s: %s until %s", item_xmlid, price, date_end)


def post_init_hook(env):
    _enable_pricelists(env)
    _create_early_bird_items(env)
    env['ir.config_parameter'].belfry_sync_webhook_urls()
