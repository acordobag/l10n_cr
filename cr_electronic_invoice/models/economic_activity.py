
from odoo import models, fields


class EconomicActivity(models.Model):
    _name = "economic.activity"
    _description = 'Economic activities listed by Ministerio de Hacienda'
    _order = "code"

    active = fields.Boolean(default=True)
    code = fields.Char()
    ciiu3 = fields.Char()
    name = fields.Char()
    name_ciiu3 = fields.Char()
    description = fields.Char()
    description_ciiu3 = fields.Char()

    sale_type = fields.Selection(selection=[('goods', 'Goods'), ('services', 'Services')],
                                 default='goods',
                                 required=True)
