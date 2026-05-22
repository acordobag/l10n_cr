
from odoo import models, fields


class Resolution(models.Model):
    _name = "resolution"
    _description = "Costa Rica DGT Resolution"

    active = fields.Boolean(help='Set active or inactive DGT resolutions.')
    name = fields.Char(help='DGT resolution name.')
    date_resolution = fields.Date(help='DGT expiration resolution date.')
