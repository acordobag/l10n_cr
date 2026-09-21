# -*- coding: utf-8 -*-
#
# Reporte de conciliación de IVA para Costa Rica (D-150 / TRIBU-CR).
#
# Este módulo NO presenta la declaración ante Hacienda ni sustituye a un
# contador: solo agrupa lo que ya está en Odoo (facturas, notas de crédito/
# débito y facturas de compra) por clasificación de IVA, replicando la misma
# lógica de "iva_tax_code" que usa cr_electronic_invoice para generar el XML
# de cada comprobante, para que el resultado se pueda comparar contra lo que
# TRIBU-CR prellena para el Formulario 150.
#
# Clasificación usada (tomada de cr_electronic_invoice/data/account_tax_data.xml,
# que son los códigos reales que ya están configurados en esta base de datos):
#   iva_tax_code '08'            -> Tarifa general 13%
#   iva_tax_code '02'/'03'/'04'/'09' -> Tarifas reducidas 1% / 2% / 4% / 0.5%
#   iva_tax_code '05'/'06'/'07'  -> Tarifas transitorias (histórico)
#   iva_tax_code '01'            -> Tarifa 0% / "no sujeto" (incluye exportación
#                                    de servicios cuando el cliente tiene marcado
#                                    "export" y el comprobante es FEE)
#   iva_tax_code '10'            -> Exento
#   iva_tax_code '11'            -> 0% sin derecho a crédito
#   has_exoneration = True       -> Exonerado (usa tax_root + percentage_exoneration,
#                                    igual que cr_electronic_invoice)
#
# La clasificación de "no sujeto" para exportación de servicios se dejó tal
# cual la implementa el módulo de facturación electrónica ya instalado; no es
# una interpretación nueva. Aun así, confirme con su contador el tratamiento
# de crédito fiscal antes de depender de este número.

from odoo import models, fields, api
from odoo.exceptions import UserError


IVA_TAX_FAMILY_CODE = '01'  # tax_code (no confundir con iva_tax_code) que usa
                            # cr_electronic_invoice para identificar impuestos de IVA

RATE_LABELS = {
    '08': 'Gravada 13%',
    '02': 'Gravada 1%',
    '03': 'Gravada 2%',
    '04': 'Gravada 4%',
    '09': 'Gravada 0.5%',
    '05': 'Gravada 0% (transitorio)',
    '06': 'Gravada 4% (transitorio)',
    '07': 'Gravada 8% (transitorio)',
    '01': 'No sujeta / Exportación 0%',
    '10': 'Exenta',
    '11': '0% sin derecho a crédito',
}


class CrIvaReportD150Wizard(models.TransientModel):
    _name = 'cr.iva.report.d150.wizard'
    _description = 'Reporte de conciliación de IVA (D-150 / TRIBU-CR)'

    company_id = fields.Many2one('res.company', string='Compañía',
                                  default=lambda self: self.env.company, required=True)
    currency_id = fields.Many2one(related='company_id.currency_id', string='Moneda')
    date_from = fields.Date(string='Desde', required=True,
                             default=lambda self: fields.Date.today().replace(day=1))
    date_to = fields.Date(string='Hasta', required=True,
                           default=lambda self: fields.Date.today())

    computed = fields.Boolean(default=False)

    # Totales de ventas (débito fiscal)
    debito_fiscal = fields.Monetary(string='Total débito fiscal (IVA de ventas)', readonly=True)
    ventas_gravadas_13 = fields.Monetary(string='Base gravada 13%', readonly=True)
    ventas_gravadas_otras = fields.Monetary(string='Base gravada otras tarifas', readonly=True)
    ventas_no_sujetas = fields.Monetary(string='Base no sujeta / exportación', readonly=True)
    ventas_exentas = fields.Monetary(string='Base exenta', readonly=True)
    ventas_exoneradas = fields.Monetary(string='Base exonerada', readonly=True)

    # Totales de compras (crédito fiscal)
    credito_fiscal = fields.Monetary(string='Total crédito fiscal (IVA de compras)', readonly=True)
    compras_credito_13 = fields.Monetary(string='Base compras 13% con crédito', readonly=True)
    compras_credito_otras = fields.Monetary(string='Base compras otras tarifas con crédito', readonly=True)
    compras_no_deducible = fields.Monetary(string='Base compras marcadas no deducibles', readonly=True)

    iva_a_pagar = fields.Monetary(string='IVA a pagar (débito - crédito)', readonly=True)

    warning_count = fields.Integer(string='Advertencias', readonly=True)

    line_ids = fields.One2many('cr.iva.report.d150.line', 'wizard_id', string='Detalle', readonly=True)
    warning_ids = fields.One2many('cr.iva.report.d150.warning', 'wizard_id', string='Advertencias', readonly=True)

    def action_compute(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError('La fecha "Desde" no puede ser posterior a la fecha "Hasta".')

        self.line_ids.unlink()
        self.warning_ids.unlink()

        totals = {
            'debito_fiscal': 0.0,
            'ventas_gravadas_13': 0.0,
            'ventas_gravadas_otras': 0.0,
            'ventas_no_sujetas': 0.0,
            'ventas_exentas': 0.0,
            'ventas_exoneradas': 0.0,
            'credito_fiscal': 0.0,
            'compras_credito_13': 0.0,
            'compras_credito_otras': 0.0,
            'compras_no_deducible': 0.0,
        }

        detail_vals = []
        warning_vals = []

        sale_moves = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ])

        for move in sale_moves:
            sign = -1 if move.move_type == 'out_refund' else 1
            for line in move.invoice_line_ids.filtered(lambda l: not l.display_type):
                if not line.product_id or not (line.product_id.cabys_code or
                                                (line.product_id.categ_id and
                                                 line.product_id.categ_id.cabys_code)):
                    warning_vals.append((0, 0, {
                        'move_id': move.id,
                        'message': 'Línea "%s" sin código CABYS (producto o categoría). '
                                   'Sin CABYS, cr_electronic_invoice omite la línea del XML '
                                   'y este reporte tampoco puede clasificarla.' % (line.name or line.product_id.display_name),
                    }))
                    continue

                iva_taxes = line.tax_ids.filtered(lambda t: t.tax_code == IVA_TAX_FAMILY_CODE)
                if not iva_taxes:
                    warning_vals.append((0, 0, {
                        'move_id': move.id,
                        'message': 'Línea "%s" sin impuesto de IVA asignado.' % (line.name or line.product_id.display_name),
                    }))
                    continue

                base = sign * line.price_subtotal

                for tax in iva_taxes:
                    if tax.has_exoneration and tax.tax_root:
                        rate = tax.tax_root.amount
                        exo_rate = min(tax.percentage_exoneration, rate)
                        exo_fraction = (exo_rate / rate) if rate else 0.0
                        base_exonerada = base * exo_fraction
                        base_gravada_resto = base - base_exonerada
                        tax_amount = base_gravada_resto * rate / 100.0

                        totals['ventas_exoneradas'] += base_exonerada
                        bucket = 'Exonerada (%.0f%% de %.0f%%, resto gravado)' % (exo_rate, rate)
                        if base_gravada_resto:
                            if rate == 13:
                                totals['ventas_gravadas_13'] += base_gravada_resto
                            else:
                                totals['ventas_gravadas_otras'] += base_gravada_resto
                        totals['debito_fiscal'] += tax_amount

                        detail_vals.append((0, 0, {
                            'move_id': move.id,
                            'move_type': move.move_type,
                            'tipo_documento': move.tipo_documento,
                            'partner_id': move.partner_id.id,
                            'invoice_date': move.invoice_date,
                            'product_id': line.product_id.id,
                            'bucket': bucket,
                            'base_amount': base,
                            'tax_amount': tax_amount,
                        }))
                        continue

                    code = tax.iva_tax_code
                    tax_amount = base * (tax.amount or 0.0) / 100.0
                    label = RATE_LABELS.get(code, 'Código IVA %s (sin clasificar)' % code)

                    if code == '10' or code == '11':
                        totals['ventas_exentas'] += base
                    elif code == '01':
                        totals['ventas_no_sujetas'] += base
                    else:
                        if code == '08':
                            totals['ventas_gravadas_13'] += base
                        else:
                            totals['ventas_gravadas_otras'] += base
                        totals['debito_fiscal'] += tax_amount

                    detail_vals.append((0, 0, {
                        'move_id': move.id,
                        'move_type': move.move_type,
                        'tipo_documento': move.tipo_documento,
                        'partner_id': move.partner_id.id,
                        'invoice_date': move.invoice_date,
                        'product_id': line.product_id.id,
                        'bucket': label,
                        'base_amount': base,
                        'tax_amount': tax_amount,
                    }))

            # Advertencia si el cliente es de exportación pero el comprobante no quedó como FEE,
            # o viceversa: comprobante FEE cuyas líneas no se clasificaron como no sujetas/exentas.
            if move.partner_id.export and move.tipo_documento != 'FEE':
                warning_vals.append((0, 0, {
                    'move_id': move.id,
                    'message': 'El cliente está marcado como exportación pero el comprobante '
                               'quedó como "%s" en vez de FEE. Revíselo antes de declarar.' % (move.tipo_documento or '-'),
                }))

        purchase_moves = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('move_type', 'in', ('in_invoice', 'in_refund')),
            ('state', '=', 'posted'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ])

        for move in purchase_moves:
            sign = -1 if move.move_type == 'in_refund' else 1
            for line in move.invoice_line_ids.filtered(lambda l: not l.display_type):
                iva_taxes = line.tax_ids.filtered(lambda t: t.tax_code == IVA_TAX_FAMILY_CODE)
                if not iva_taxes:
                    continue

                base = sign * line.price_subtotal
                for tax in iva_taxes:
                    tax_amount = base * (tax.amount or 0.0) / 100.0
                    label = RATE_LABELS.get(tax.iva_tax_code, 'Código IVA %s (sin clasificar)' % tax.iva_tax_code)

                    if tax.non_tax_deductible:
                        totals['compras_no_deducible'] += base
                    else:
                        if tax.iva_tax_code == '08':
                            totals['compras_credito_13'] += base
                        else:
                            totals['compras_credito_otras'] += base
                        totals['credito_fiscal'] += tax_amount

                    detail_vals.append((0, 0, {
                        'move_id': move.id,
                        'move_type': move.move_type,
                        'tipo_documento': move.tipo_documento,
                        'partner_id': move.partner_id.id,
                        'invoice_date': move.invoice_date,
                        'product_id': line.product_id.id,
                        'bucket': ('%s (no deducible)' % label) if tax.non_tax_deductible else label,
                        'base_amount': base,
                        'tax_amount': tax_amount,
                    }))

        totals['iva_a_pagar'] = totals['debito_fiscal'] - totals['credito_fiscal']

        self.write(dict(totals, computed=True, warning_count=len(warning_vals),
                         line_ids=detail_vals, warning_ids=warning_vals))

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }


class CrIvaReportD150Line(models.TransientModel):
    _name = 'cr.iva.report.d150.line'
    _description = 'Detalle del reporte de conciliación de IVA'

    wizard_id = fields.Many2one('cr.iva.report.d150.wizard', ondelete='cascade')
    move_id = fields.Many2one('account.move', string='Comprobante')
    move_type = fields.Selection(related='move_id.move_type', string='Tipo de movimiento')
    tipo_documento = fields.Char(string='Tipo Hacienda')
    partner_id = fields.Many2one('res.partner', string='Cliente/Proveedor')
    invoice_date = fields.Date(string='Fecha')
    product_id = fields.Many2one('product.product', string='Producto/Servicio')
    bucket = fields.Char(string='Clasificación IVA')
    base_amount = fields.Monetary(string='Base')
    tax_amount = fields.Monetary(string='Monto IVA')
    currency_id = fields.Many2one(related='wizard_id.currency_id')


class CrIvaReportD150Warning(models.TransientModel):
    _name = 'cr.iva.report.d150.warning'
    _description = 'Advertencias del reporte de conciliación de IVA'

    wizard_id = fields.Many2one('cr.iva.report.d150.wizard', ondelete='cascade')
    move_id = fields.Many2one('account.move', string='Comprobante')
    message = fields.Char(string='Advertencia')
