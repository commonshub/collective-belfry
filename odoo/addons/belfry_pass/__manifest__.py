# Part of Belfry. See LICENSE file for full copyright and licensing details.
{
    'name': 'Belfry passes',
    'summary': 'Open Source Village passes: products, pricing, steward invites, pass registry and onboarding webhooks',
    'description': """
Belfry pass registry for Open Source Village (slice 1, Phase 1 onboarding).

- Pass products (day / week / full access) with early-bird and company pricing
- Example discount code OSV10
- Steward invite links with a public billing page that ends on an Odoo payment link
- belfry.pass registry (one row per purchased pass), belfry.stripe.event log
- Automation rules that notify the onboarding service when an invoice is paid or a refund is done
""",
    'version': '19.0.1.0.0',
    'category': 'Sales/Sales',
    'license': 'LGPL-3',
    'author': 'Belfry',
    'website': 'https://github.com/commonshub/collective-belfry',
    'depends': [
        'sale_management',
        'account_payment',
        'payment_stripe',
        'loyalty',
        'sale_loyalty',
        'base_automation',
        'portal',
    ],
    'data': [
        'security/res_groups.xml',
        'security/ir.model.access.csv',
        'security/ir_rules.xml',
        'data/ir_sequence_data.xml',
        'data/ir_config_parameter_data.xml',
        'data/product_data.xml',
        'data/pricelist_data.xml',
        'data/loyalty_data.xml',
        'data/base_automation_data.xml',
        'views/belfry_pass_views.xml',
        'views/belfry_invite_views.xml',
        'views/belfry_stripe_event_views.xml',
        'views/product_views.xml',
        'views/res_partner_views.xml',
        'views/menus.xml',
        'views/templates.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': True,
}
