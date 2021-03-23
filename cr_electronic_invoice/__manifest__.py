# -*- coding: utf-8 -*-

{
    'name': 'Facturación electrónica Costa Rica',
    'version': '12.0.2.0.5',
    'author': 'Odoo CR Community',
    'license': 'AGPL-3',
    'website': 'https://github.com/odoocr',
    'category': 'Account',
    'description':
        '''
        Facturación electronica Costa Rica.
        ''',
    'depends': [
        'base',
        'product',
        'uom',
        'sale_management',
        'sales_team',
        'account',
        'l10n_cr_country_codes',
        'account_cancel',
        'res_currency_cr_adapter',
        ],
    'data': [
        'data/account_tax_data.xml',
        'data/aut_ex_data.xml',
        'data/code_type_product_data.xml',
        'data/identification_type_data.xml',
        'data/ir_cron_data.xml',
        'data/mail_template_data.xml',
        'data/payment_methods_data.xml',
        'data/reference_code_data.xml',
        'data/reference_document_data.xml',
        'data/sale_conditions_data.xml',
        'data/product_category_data.xml',
        'data/product_data.xml',
        'data/uom_data.xml',
        'data/economic_activity_data.xml',
        'data/sequence.xml',
        'data/res.currency.xml',
        'data/decimal_precision.xml',
        'views/uom_views.xml',
        'views/account_invoice_views.xml',
        'views/account_journal_views.xml',
        'views/account_payment_views.xml',
        'views/code_type_product_views.xml',
        'views/identification_type_views.xml',
        'views/product_views.xml',
        'views/reference_document_views.xml',
        'views/res_company_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_partner_views.xml',
        'views/resolution_views.xml',
        'views/sale_condition_views.xml',
        'views/account_tax_views.xml',
        'views/account_invoice_import_wizard_view.xml',
        'views/economic_activity_views.xml',
        'views/menu_views.xml',
        'security/ir.model.access.csv',
    ],
    'external_dependencies': {
        "python": [
            'xmlsig',
            'OpenSSL',
        ],
    },
    'post_init_hook': 'post_init_check',
    'installable': True,
}
