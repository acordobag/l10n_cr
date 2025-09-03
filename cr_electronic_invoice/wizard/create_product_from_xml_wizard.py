# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

class CreateProductsFromXMLWizard(models.TransientModel):
    _name = 'create.products.from.xml.wizard'
    _description = 'Create products from imported XML lines'

    move_id = fields.Many2one('account.move', required=True, ondelete='cascade')
    line_ids = fields.One2many('create.products.from.xml.wizard.line', 'wizard_id', string='Lines')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        move_id_ctx = self.env.context.get('default_move_id')
        if not move_id_ctx:
            raise UserError(_('No se recibió la factura en el contexto.'))
        move = self.env['account.move'].browse(move_id_ctx)
        if not move or not move.exists():
            raise UserError(_('La factura indicada no existe.'))

        lines_vals = []
        Product = self.env['product.product']
        Template = self.env['product.template']

        # Solo líneas sin producto y que no sean secciones/notas
        for ml in move.invoice_line_ids.filtered(lambda l: not l.product_id and not l.display_type):
            candidate = False

            # 1) por código / barcode
            xml_code = getattr(ml, 'xml_code', False)
            if xml_code:
                candidate = Product.search(['|',
                                            ('default_code', '=', xml_code),
                                            ('barcode', '=', xml_code)],
                                           limit=1)

            # 2) por CABYS en template (si tu DB tiene campo)
            if not candidate:
                xml_cabys = getattr(ml, 'xml_cabys', False)
                if xml_cabys:
                    for f in ('cabys_code', 'codigo_cabys', 'cabys', 'l10n_cr_cabys'):
                        if f in Template._fields:
                            tmpl = Template.search([(f, '=', xml_cabys)], limit=1)
                            if tmpl:
                                candidate = tmpl.product_variant_id
                                break

            # 3) (opcional) por nombre
            if not candidate and ml.name:
                # si prefieres no usar nombre, comenta estas 2 líneas
                candidate = Product.search([('name', 'ilike', ml.name)], limit=1)

            # UoM segura
            uom_id = ml.product_uom_id.id if ml.product_uom_id else self.env.ref('uom.product_uom_unit').id

            lines_vals.append((0, 0, {
                'move_line_id': ml.id,
                'name': ml.name or '',
                'code': xml_code or '',
                'cabys': getattr(ml, 'xml_cabys', '') or '',
                'uom_id': uom_id,
                'qty': ml.quantity or 0.0,
                'price_unit': ml.price_unit or 0.0,
                'product_candidate_id': candidate.id if candidate else False,
                'to_create': not bool(candidate),
            }))

        res['line_ids'] = lines_vals
        res['move_id'] = move.id
        return res

    def action_create_and_link(self):
        self.ensure_one()
        move = self.move_id

        if move.state != 'draft':
            raise UserError(_('Solo puede vincular/crear productos en facturas en borrador.'))

        Template = self.env['product.template']

        for wline in self.line_ids:
            product = wline.product_candidate_id
            if not product and wline.to_create:

                vals = {
                    'name': wline.name,
                    # Referencia interna: prioriza código; si no, usa CABYS
                    'default_code': wline.code or wline.cabys or False,
                    # UoM de venta y compra
                    'uom_id': wline.uom_id.id,
                    'uom_po_id': wline.uom_id.id,
                    # Tipo producto (ajústalo si en tu flujo deben ser consumibles)
                    'type': 'product',
                    'purchase_ok': True,
                    'sale_ok': False,
                    # Compañía de la factura
                    'company_id': self.move_id.company_id.id,
                    # Categoría (si quieres forzar una por defecto)
                    'categ_id': self.env.ref('product.product_category_all').id,
                    'cabys_code': wline.cabys,
                    'standard_price' : wline.price_unit or 0.0,
                    'taxes_id': [(6, 0, wline.move_line_id.tax_ids.ids)]
                }
                # sudo por si el usuario no tiene permisos de creación
                tmpl = Template.sudo().create(vals)
                product = tmpl.product_variant_id

            if product:
                # Vincular producto a la línea (sin validar el asiento en ese write)
                wline.move_line_id.with_context(check_move_validity=False).write({
                    'product_id': product.id
                })
                # Si quisieras recalcular automáticamente cuentas/impuestos/descripcion de la línea
                # podrías invocar el onchange y escribir los campos oportunos. Ejemplo:
                # wline.move_line_id._onchange_product_id()
                # wline.move_line_id.with_context(check_move_validity=False).write({
                #     'name': wline.move_line_id.name,
                #     'account_id': wline.move_line_id.account_id.id,
                #     'tax_ids': [(6, 0, wline.move_line_id.tax_ids.ids)],
                # })

        return {'type': 'ir.actions.act_window_close'}


class CreateProductsFromXMLWizardLine(models.TransientModel):
    _name = 'create.products.from.xml.wizard.line'
    _description = 'Create products from XML lines - line'

    wizard_id = fields.Many2one('create.products.from.xml.wizard', required=True, ondelete='cascade')
    move_line_id = fields.Many2one('account.move.line', ondelete='cascade')
    name = fields.Char(string='Name')
    code = fields.Char(string='Code')
    cabys = fields.Char(string='CABYS')
    uom_id = fields.Many2one('uom.uom', string='UoM')
    qty = fields.Float(string='Qty')
    price_unit = fields.Float(string='Unit Price')
    product_candidate_id = fields.Many2one('product.product', string='Existing Product')
    to_create = fields.Boolean(string='Create new', default=True)
