

{
    'name': 'Facturas proveedor Costa Rica',
    'version': '14.0.1.0.0',
    'author': 'Odoo CR',
    'license': 'AGPL-3',
    'website': 'https://github.com/odoocr',
    'category': 'Account',
    'description':
        '''
        Importar Facturación proveedor Costa Rica.
        ''',
    'depends': [
        'base',
        'product',
        'uom',
        'sale_management',
        'sales_team',
        'account',
        ],
    'data': [
        'data/edi-format.xml',
    ],
    'external_dependencies': {
        "python": [
            'cryptography',
            'xmlsig',
            'OpenSSL',
            'phonenumbers',
            'jsonschema',
            'qrcode',
        ],
    },
    'installable': True,
}
