from odoo import fields, models


class InvoiceQrFields(models.Model):
    _name = 'invoice.qr.fields'
    _description = "Costa Rica Electronic Invoice QR Field"
    _order = 'sequence'

    sequence = fields.Integer()
    field_id = fields.Many2one('ir.model.fields', domain=[
        ('model_id.model', '=', 'account.move'),
        ('ttype', 'not in', ['many2many', 'one2many', 'binary'])
        ])
    company_id = fields.Many2one('res.company')
