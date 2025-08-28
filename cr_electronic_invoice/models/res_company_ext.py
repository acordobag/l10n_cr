from odoo import models, fields

class ResCompany(models.Model):
    _inherit = 'res.company'
    x_proveedor_sistemas = fields.Char(
        string='Proveedor de Sistemas',
        help='Identificación (cédula) del proveedor de software. Si desarrollo propio, usar la cédula de la empresa.'
    )
