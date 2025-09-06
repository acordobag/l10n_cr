# -*- coding: utf-8 -*-
from odoo import api, fields, models, _

class FeDiscountCode(models.Model):
    _name = 'fe.discount.code'
    _description = 'CR FE Discount Code (CodigoDescuento v4.4)'
    _order = 'code'

    code = fields.Char(
        string='Código',
        required=True,
        help="Código FE (01–09, 99) según XSD de Factura Electrónica v4.4.",
    )
    name = fields.Char(
        string='Nombre',
        required=True,
        translate=True,
    )
    default_percentage = fields.Float(
        string='Porcentaje por defecto',
        help="Porcentaje sugerido para este tipo de descuento. Puede ser 0 si depende del documento.",
        digits=(16, 4),
    )
    active = fields.Boolean(default=True)
