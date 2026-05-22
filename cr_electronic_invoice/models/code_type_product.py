
from odoo import models, fields


class CodeTypeProduct(models.Model):
    _name = "code.type.product"
    _description = "Costa Rica Electronic Invoice Product Code Type"

    code = fields.Char()
    name = fields.Char()
