{
    "name": "Consultar Información de Clientes en Hacienda Costa Rica",
    "version": '19.0.1.0.0',
    "author": "Odoo Community Association (OCA), Odoo CR, Factura Sempai, FSS Solutions",
    "license": 'LGPL-3',
    "website": "https://github.com/odoocr/l10n_cr",
    "category": "API",
    "summary": """Consultar Nombre de Clientes en Hacienda Costa Rica""",
    "depends": [
        'base',
        'contacts',
        'base_setup',
    ],
    "data": [
        'data/res_config_settings.xml',
        'views/res_config_settings_views.xml'
    ],
    "installable": True
}
