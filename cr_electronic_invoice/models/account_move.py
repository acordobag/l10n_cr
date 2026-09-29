import base64
import datetime
import logging
import re
from xml.sax.saxutils import escape
import pytz
from lxml import etree
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools.misc import get_lang

from odoo.http import request
from .qr_generator import GenerateQrCode
from odoo.tools import html2plaintext

from . import api_facturae
from .. import extensions

_logger = logging.getLogger(__name__)


class InvoiceLineElectronic(models.Model):
    _inherit = "account.move.line"

    @api.model
    def _get_default_activity_id(self):
        for line in self:
            line.economic_activity_id = line.product_id and line.product_id.categ_id and \
                                        line.product_id.categ_id.economic_activity_id and line.product_id.categ_id.economic_activity_id.id

    discount_note = fields.Char()
    total_tax = fields.Float()
    third_party_id = fields.Many2one("res.partner", string="Third - other charges")
    tariff_head = fields.Char(string="Tariff item for export invoice")
    categ_name = fields.Char(related='product_id.categ_id.name')
    product_code = fields.Char(related='product_id.default_code')
    economic_activity_id = fields.Many2one("economic.activity", string="Economic activity",
                                           store=True,
                                           context={'active_test': False},
                                           default=False)
    non_tax_deductible = fields.Boolean(string='Indicates if this invoice is non-tax deductible', )

    fe_discount_code_id = fields.Many2one(
        'fe.discount.code',
        string='Cód. Descuento',
        help="Código de descuento según FE v4.4 (01–09, 99).",
        copy=True,
        index=True,
    )

    # def write(self, vals):
    #
    #     for line in self:
    #         rec = line._code_record_for_discount(line.discount)
    #         vals['fe_discount_code_id'] = rec.id
    #         vals['fe_discount_code_id'] = rec.id
    #     res = super().write(vals)
    #     return res

    def _get_fe_discount_code(self, code_str):
        """Return the fe.discount.code record for the given 2-digit code."""
        return self.env['fe.discount.code'].search([('code', '=', str(code_str).zfill(2))], limit=1)

    def _code_record_for_discount(self, discount):
        """Map a discount % to the fe.discount.code record or False."""
        d = float(discount or 0.0)
        if d <= 0.0:
            return False
        code = '04' if abs(d - 100.0) < 1e-6 else '07'
        return self.env['fe.discount.code'].search([('code', '=', code)], limit=1)

    @api.onchange('discount')
    def _onchange_fe_discount_code_reset(self):
        """If discount is 0, clear the discount code (rule in v4.4)."""
        if not self.discount or float(self.discount) == 0.0:
            self.fe_discount_code_id = False
        elif float(self.discount) > 0:
            self.fe_discount_code_id = self._code_record_for_discount(self.discount)

    @api.onchange('product_id')
    def product_changed(self):
        # Check if the product is non deductible to use a non_deductible tax
        if self.product_id.non_tax_deductible:
            taxes = []
            self.non_tax_deductible = True
            for tax in self.tax_ids:
                new_tax = self.env['account.tax'].search([('tax_code', '=', tax.tax_code),
                                                          ('amount', '=', tax.amount),
                                                          ('type_tax_use', '=', 'purchase'),
                                                          ('non_tax_deductible', '=', True),
                                                          ('active', '=', True)], limit=1)
                if new_tax:
                    taxes.append((3, tax.id))
                    taxes.append((4, new_tax.id))
                else:
                    raise UserError(_('There is no "Non tax deductible" tax with the tax percentage of this product'))
            self.tax_ids = taxes
        else:
            self.non_tax_deductible = False

        # Check for the economic activity in the product or
        # product category or company respectively (already set in the invoice when partner selected)
        if self.product_id and self.product_id.economic_activity_id:
            self.economic_activity_id = self.product_id.economic_activity_id
        elif self.product_id and self.product_id.categ_id and self.product_id.categ_id.economic_activity_id:
            self.economic_activity_id = self.product_id.categ_id.economic_activity_id
        else:
            self.economic_activity_id = self.move_id.economic_activity_id


class AccountInvoiceElectronic(models.Model):
    _inherit = "account.move"

    def _store_hacienda_response_xml(self, response_xml, filename=None):
        """Store Hacienda's XML response without empty or duplicate attachments."""
        self.ensure_one()
        if not response_xml:
            _logger.warning(
                'E-INV CR - Hacienda returned no response XML for document %s',
                self.number_electronic)
            return False

        filename = filename or 'AHC_' + self.number_electronic + '.xml'
        self.fname_xml_respuesta_tributacion = filename
        attachment_vals = {
            'name': filename,
            'type': 'binary',
            'datas': response_xml,
            'res_model': self._name,
            'res_id': self.id,
            'res_field': 'xml_respuesta_tributacion',
            'res_name': filename,
            'mimetype': 'text/xml',
        }
        attachments = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('res_field', '=', 'xml_respuesta_tributacion'),
        ])
        if attachments:
            attachments.write(attachment_vals)
            return attachments[0]
        return self.env['ir.attachment'].sudo().create(attachment_vals)

    number_electronic = fields.Char(string="Electronic number", copy=False, index=True)
    date_issuance = fields.Char(string="Date of issue", copy=False)
    consecutive_number_receiver = fields.Char(string="Consecutive Receiver Number",
                                              copy=False, readonly=True, index=True)
    state_send_invoice = fields.Selection([('aceptado', 'Aceptado'),
                                           ('rechazado', 'Rechazado'),
                                           ('error', 'Error'),
                                           ('na', 'No Aplica'),
                                           ('ne', 'No Encontrado'),
                                           ('firma_invalida', 'Firma Inválida'),
                                           ('procesando', 'Procesando')], 'Estado FE Proveedor')

    state_tributacion = fields.Selection([('aceptado', 'Aceptado'),
                                          ('rechazado', 'Rechazado'),
                                          ('recibido', 'Recibido'),
                                          ('firma_invalida', 'Firma Inválida'),
                                          ('error', 'Error'),
                                          ('procesando', 'Procesando'),
                                          ('na', 'No Aplica'),
                                          ('ne', 'No Encontrado')], 'Estado FE', copy=False)

    state_invoice_partner = fields.Selection([('1', 'Aceptado'),
                                              ('2', 'Aceptacion parcial'),
                                              ('3', 'Rechazado')], 'Respuesta del Cliente')

    reference_code_id = fields.Many2one("reference.code", string="Reference code")

    reference_document_id = fields.Many2one("reference.document", string="Reference Document Type")

    payment_methods_id = fields.Many2one("payment.methods", string="Payment methods", )

    invoice_id = fields.Many2one("account.move", string="Reference document", copy=False)

    xml_respuesta_tributacion = fields.Binary(string="XML Tributación Response", copy=False, attachment=True)

    electronic_invoice_return_message = fields.Char(string='Hacienda answer', readonly=True)

    # Portado de v19 (quicknet): trazabilidad del reintento de un documento
    # rechazado/con error. Ver action_retry_rejected_electronic_invoice.
    fe_retry_origin_id = fields.Many2one('account.move', string='Origen del reintento FE',
                                         copy=False, readonly=True)
    fe_retry_new_id = fields.Many2one('account.move', string='Documento nuevo del reintento FE',
                                      copy=False, readonly=True)
    fe_retry_reversal_id = fields.Many2one('account.move', string='Reversa interna del reintento FE',
                                           copy=False, readonly=True)

    fname_xml_respuesta_tributacion = fields.Char(string="XML File Name Tributación Response",
                                                  copy=False)
    xml_comprobante = fields.Binary(string="XML voucher", copy=False, attachment=True)
    fname_xml_comprobante = fields.Char(string="File name XML voucher", copy=False)
    xml_supplier_approval = fields.Binary(string="Vendor XML", copy=False, attachment=True)
    fname_xml_supplier_approval = fields.Char(string="Vendor XML voucher file name", copy=False)
    amount_tax_electronic_invoice = fields.Monetary(string='Total FE taxes', readonly=True)
    amount_total_iva_devuelto = fields.Monetary(string='IVA Devuelto', copy=False, readonly=True)
    amount_total_electronic_invoice = fields.Monetary(string='Total FE', readonly=True)
    tipo_documento = fields.Selection(
        selection=[('FE', 'Factura Electrónica'),
                   ('FEE', 'Factura Electrónica de Exportación'),
                   ('TE', 'Tiquete Electrónico'),
                   ('NC', 'Nota de Crédito'),
                   ('ND', 'Nota de Débito'),
                   ('CCE', 'MR Aceptación'),
                   ('CPCE', 'MR Aceptación Parcial'),
                   ('RCE', 'MR Rechazo'),
                   ('FEC', 'Factura Electrónica de Compra'),
                   ('CONT', 'Factura de Contingencia'),
                   ('disabled', 'Electronic Documents Disabled')],
        string="Voucher Type",
        default='FE',
        help='Indicates the type of document according to the classification of the Ministerio de Hacienda')

    sequence = fields.Char(string='Consecutive', readonly=True, copy=False)

    state_email = fields.Selection([('no_email', 'Sin cuenta de correo'),
                                    ('sent', 'Enviado'),
                                    ('fe_error', 'Error FE')], 'Estado email', copy=False)

    invoice_amount_text = fields.Char(string='Amount in Letters', readonly=True, copy=False)

    ignore_total_difference = fields.Boolean(string="Ignore Difference in Totals", default=False)

    error_count = fields.Integer(string="Number of errors", default="0", copy=False)

    economic_activity_id = fields.Many2one("economic.activity",
                                           string="Economic Activity", context={'active_test': False})

    economic_activities_ids = fields.Many2many('economic.activity', string='Economic activities',
                                               compute='_compute_economic_activities', context={'active_test': False})

    # Clave y fecha del comprobante original cuando no esta en Odoo (sistema
    # anterior): las NC/ND las escriben a mano.
    not_loaded_invoice = fields.Char(string='Original Invoice Number not loaded')

    not_loaded_invoice_date = fields.Date(string='Original Invoice Date not loaded')

    # La clave es unica por compania, pero un comprobante anulado la libera para
    # poder volver a importar/cargar el mismo XML. Reemplaza el unique total.
    def init(self):
        super().init()
        self.env.cr.execute("ALTER TABLE account_move DROP CONSTRAINT IF EXISTS account_move_number_electronic_uniq")
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS account_move_number_electronic_uniq_idx
            ON account_move (company_id, number_electronic)
            WHERE number_electronic IS NOT NULL AND state != 'cancel'
        """)

    # Por SQL y sin guardar antes: un search guardaba primero este documento y
    # el indice respondia con su error tecnico en vez de este mensaje. El
    # estado de los candidatos se lee del ORM, que incluye lo aun no guardado
    # (p. ej. una anulada en la misma transaccion).
    @api.constrains('number_electronic', 'company_id', 'state')
    def _check_number_electronic_unique(self):
        for move in self.filtered(lambda m: m.number_electronic and m.state != 'cancel'):
            self.env.cr.execute("""
                SELECT id FROM account_move
                WHERE company_id = %s AND number_electronic = %s AND id != %s
            """, [move.company_id.id, move.number_electronic, move.id])
            others = self.sudo().browse([row[0] for row in self.env.cr.fetchall()])
            if others.filtered(lambda m: m.state != 'cancel' and m.number_electronic == move.number_electronic):
                raise UserError(_('La clave de comprobante debe ser única'))

    qr_image = fields.Binary("QR Code", compute='_compute_qr_code')
    partner_vat = fields.Char(string='Partner Tax ID', related="partner_id.vat",
                              store=True, index=True, help="The Parnter Tax Identification Number.")
    company_vat = fields.Char(string='Company Tax ID', related="partner_id.vat",
                              store=True, index=True, help="Your Company Tax Identification Number.")
    comp_amount_untaxed = fields.Monetary(string='Total Untaxed', readonly=True, compute='_compute_amount_untaxed',
                                          currency_field='company_currency_id')
    comp_amount_total = fields.Monetary(string='Total', readonly=True, compute='_compute_amount_total',
                                        currency_field='company_currency_id')

    # Texto libre solicitado por MH cuando TipoMedioPago = 99
    # XSD 4.4: máx 100 caracteres
    payment_method_otros = fields.Char(
        string='Medio de pago (otros)',
        help="Requerido solo si el Tipo de Medio de Pago es 99 (OTRO). Máximo 100 caracteres.",
        size=100
    )

    # Banderita sólo para attrs de la vista
    is_payment_method_99 = fields.Boolean(
        compute='_compute_is_payment_method_99',
        store=False
    )

    @api.depends('payment_methods_id.sequence')
    def _compute_is_payment_method_99(self):
        for move in self:
            code = str(move.payment_methods_id.sequence or '')
            move.is_payment_method_99 = (code == '99')

    @api.onchange('payment_methods_id')
    def _onchange_payment_methods_id_clear_otros(self):
        # si deja de ser 99, limpiamos el texto
        for move in self:
            if (move.move_type == 'in_invoice'):
                move.payment_method_otros = 'Otros'
                return
            if not move.is_payment_method_99:
                move.payment_method_otros = False

    @api.constrains('payment_methods_id', 'payment_method_otros')
    def _check_medio_pago_99(self):
        for move in self:
            if (move.move_type == 'in_invoice'): return
            code = str(move.payment_methods_id.sequence or '')
            if code == '99':
                if not (move.payment_method_otros or '').strip():
                    raise UserError(
                        _("Cuando el tipo de medio de pago es '99 - OTRO', debe indicar el campo "
                          "'Medio de pago (otros)'.")
                    )
                if len(move.payment_method_otros) > 100:
                    raise UserError(
                        _("El texto de 'Medio de pago (otros)' supera los 100 caracteres permitidos por el XSD 4.4.")
                    )

    def _compute_amount_total(self):
        for rec in self:
            record = rec.currency_id
            local = rec.company_id.currency_id

            rec.comp_amount_total = record.compute(rec.amount_total, local)

    def _compute_amount_untaxed(self):
        for rec in self:
            record = rec.currency_id
            local = rec.company_id.currency_id

            rec.comp_amount_untaxed = record.compute(rec.amount_untaxed, local)

    def _compute_qr_code(self):
        qr_info = ''
        if self.env.user.company_id.invoice_qr_type != 'by_info':
            qr_info = request.env['ir.config_parameter'].sudo().get_param('web.base.url')
            qr_info += self.get_portal_url()
        else:
            if self.env.user.company_id.invoice_field_ids:
                # F841 local variable 'result' is assigned to but never used
                # result = self.search_read([('id', 'in', self.ids)],
                # self.env.user.company_id.invoice_field_ids.mapped('field_id.name'))
                dict_result = {}
                for ffild in self.env.user.company_id.invoice_field_ids.mapped('field_id'):
                    if ffild.ttype == 'many2one':
                        dict_result[ffild.field_description] = self[ffild.name].display_name
                    else:
                        dict_result[ffild.field_description] = self[ffild.name]
                for key, value in dict_result.items():
                    if str(key).__contains__('Partner') or str(key).__contains__(_('Partner')):
                        if self.move_type in ['out_invoice', 'out_refund']:
                            key = str(key).replace(_('Partner'), _('Customer'))
                        elif self.move_type in ['in_invoice', 'in_refund']:
                            key = str(key).replace(_('Partner'), _('Vendor'))
                    qr_info += f"{key} : {value} <br/>"
                qr_info = html2plaintext(qr_info)
        self.qr_image = GenerateQrCode.generate_qr_code(qr_info)

    @api.onchange('partner_id', 'company_id')
    def _compute_economic_activities(self):
        for inv in self:
            if inv.move_type in ('in_invoice', 'in_refund'):
                if inv.partner_id:
                    inv.economic_activities_ids = inv.partner_id.economic_activities_ids
                    inv.economic_activity_id = inv.partner_id.activity_id
            else:
                inv.economic_activities_ids = self.env['economic.activity'].search([('active', '=', False)])
                inv.economic_activity_id = inv.company_id.activity_id

    @api.onchange('partner_id')
    def _onchange_partner_id(self, validate_payment=True):
        super()._onchange_partner_id()
        if (validate_payment):
            self.payment_methods_id = self.partner_id.payment_methods_id

        if self.move_type in ('in_invoice', 'in_refund'):
            if self.partner_id:
                self.economic_activities_ids = self.partner_id.economic_activities_ids
                self.economic_activity_id = self.partner_id.activity_id
            else:
                self.economic_activity_id = False
                self.economic_activities_ids = []
        else:
            self.economic_activities_ids = self.env['economic.activity'].search([('active', '=', True)])
            self.economic_activity_id = self.company_id.activity_id

        # Solo aplica a documentos de venta: en una factura de proveedor el tipo
        # lo define la confirmacion (FEC) o la importacion del XML (CCE), y este
        # bloque pisaba con FE o TE lo elegido (CONT, disabled) al cambiar el
        # proveedor.
        if self.move_type in ('out_invoice', 'out_refund'):
            if self.partner_id and self.partner_id.export:
                self.tipo_documento = 'FEE'
            elif self.move_type == 'out_refund':
                self.tipo_documento = 'NC'
            elif self.partner_id and self.partner_id.vat:
                if self.partner_id.country_id and self.partner_id.country_id.code != 'CR':
                    self.tipo_documento = 'TE'
                elif self.partner_id.identification_id and self.partner_id.identification_id.code == '05':
                    self.tipo_documento = 'TE'
                else:
                    self.tipo_documento = 'FE'
            else:
                self.tipo_documento = 'TE'

    def _get_electronic_mail_attachments(self):
        """XML del comprobante y de la respuesta de Hacienda, de esta factura.

        Son los adjuntos del propio movimiento. Antes se copiaban a
        email_template.attachment_ids, un registro compartido por todos los
        envios, y un correo salia con el XML de otra factura.
        """
        self.ensure_one()
        Attachment = self.env['ir.attachment']
        if not self.number_electronic:
            return Attachment
        attachment_ids = []
        for field_name, fname in (('xml_comprobante', self.fname_xml_comprobante),
                                  ('xml_respuesta_tributacion', self.fname_xml_respuesta_tributacion)):
            candidates = Attachment.sudo().search([
                ('res_model', '=', self._name),
                ('res_id', '=', self.id),
                ('res_field', '=', field_name),
            ], order='id desc')
            if not candidates:
                continue
            # Un reenvio, una FEC rechazada o una respuesta guardada dos veces
            # dejan varios adjuntos en el mismo campo, alguno vacio. Se manda el
            # que lleva el nombre del comprobante actual y tiene contenido.
            current = (
                candidates.filtered(lambda a: a.name == fname and a.file_size)
                or candidates.filtered(lambda a: a.file_size)
                or candidates
            )[:1]
            attachment_ids += current.ids
        return Attachment.browse(attachment_ids)

    def _send_electronic_invoice_email(self, email_template):
        """Manda el comprobante al cliente y deja constancia en la factura.

        La plantilla tiene auto_delete: el correo se borra al salir y se lleva
        su mensaje, asi que sin esta nota no quedaria rastro del envio.
        """
        self.ensure_one()
        attachments = self._get_electronic_mail_attachments()
        email_template.with_context(type='binary', default_type='binary').send_mail(
            self.id,
            raise_exception=False,
            force_send=True,
            email_values={'attachment_ids': [(4, aid) for aid in attachments.ids]})
        # _message_log deja la nota sin notificar a los seguidores.
        self._message_log(body=_('Comprobante electrónico enviado por correo a %s. Adjuntos: %s') % (
            self.partner_id.email or '-',
            ', '.join(attachments.mapped('name')) or '-'))

    def action_invoice_sent_mass(self):

        if self.invoice_id.move_type in ['in_invoice', 'in_refund']:
            email_template = self.env.ref('cr_electronic_invoice.email_template_invoice_vendor',
                                          raise_if_not_found=False)
        else:
            email_template = self.env.ref('account.email_template_edi_invoice', raise_if_not_found=False)

        lang = False
        if email_template:
            lang = email_template._render_lang(self.ids)[self.id]
        if not lang:
            lang = get_lang(self.env).code

        if self.env.user.company_id.frm_ws_ambiente == 'disabled':
            pass
        elif self.partner_id and self.partner_id.email:

            domain = [('res_model', '=', 'account.move'),
                      ('res_id', '=', self.id),
                      ('res_field', '=', 'xml_comprobante'),
                      ('name', '=', self.tipo_documento + '_' + self.number_electronic + '.xml')]
            attachment = self.env['ir.attachment'].sudo().search(domain, limit=1)

            if attachment:
                # attachment.name = self.fname_xml_comprobante

                domain_resp = [('res_model', '=', 'account.move'),
                               ('res_id', '=', self.id),
                               ('res_field', '=', 'xml_respuesta_tributacion'),
                               ('name', '=', 'AHC_' + self.number_electronic + '.xml')]
                attachment_resp = self.env['ir.attachment'].sudo().search(domain_resp, limit=1)

                if attachment_resp:
                    self._send_electronic_invoice_email(email_template)
                else:
                    raise UserError(_('Response XML from Hacienda has not been received'))
            else:
                raise UserError(_('Invoice XML has not been generated for id:' + str(self.id)))

        else:
            raise UserError(_('Partner is not assigne to this invoice'))

    def action_invoice_sent(self):
        self.ensure_one()

        if self.invoice_id.move_type in ['in_invoice', 'in_refund']:
            email_template = self.env.ref('cr_electronic_invoice.email_template_invoice_vendor',
                                          raise_if_not_found=False)
        else:
            email_template = self.env.ref('account.email_template_edi_invoice', raise_if_not_found=False)

        lang = False
        if email_template:
            lang = email_template._render_lang(self.ids)[self.id]
        if not lang:
            lang = get_lang(self.env).code

        if self.env.user.company_id.frm_ws_ambiente == 'disabled':
            pass
        elif self.partner_id and self.partner_id.email:  # and not i.partner_id.opt_out:

            domain = [('res_model', '=', self._name),
                      ('res_id', '=', self.id),
                      ('res_field', '=', 'xml_comprobante'),
                      ('name', '=', self.tipo_documento + '_' + self.number_electronic + '.xml')]
            attachment = self.env['ir.attachment'].sudo().search(domain, limit=1)
            if attachment:
                # attachment.name = self.fname_xml_comprobante

                domain_resp = [('res_model', '=', self._name),
                               ('res_id', '=', self.id),
                               ('res_field', '=', 'xml_respuesta_tributacion'),
                               ('name', '=', 'AHC_' + self.number_electronic + '.xml')]
                attachment_resp = self.env['ir.attachment'].sudo().search(domain_resp, limit=1)

                # El asistente adjunta los XML de esta factura (ver
                # account.invoice.send.onchange_template_id); no se tocan los
                # adjuntos de la plantilla, que es un registro compartido.
                if not attachment_resp:
                    raise UserError(_('Response XML from Hacienda has not been received'))
            else:
                raise UserError(_('Invoice XML has not been generated for id:' + str(self.id)))

        else:
            raise UserError(_('Partner is not assigne to this invoice'))

        compose_form = self.env.ref('account.account_invoice_send_wizard_form', raise_if_not_found=False).sudo()
        ctx = dict(
            default_model='account.move',
            default_res_id=self.id,
            default_res_model='account.move',
            default_use_template=bool(email_template),
            default_template_id=email_template and email_template.id or False,
            default_composition_mode='comment',
            mark_invoice_as_sent=True,
            custom_layout="mail.mail_notification_paynow",
            model_description=self.with_context(lang=lang).type_name,
            force_email=True
        )

        return {
            'name': _('Send Invoice'),
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'account.invoice.send',
            'views': [(compose_form.id, 'form')],
            'view_id': compose_form.id,
            'target': 'new',
            'context': ctx,
        }

    @api.onchange('xml_supplier_approval')
    def _onchange_xml_supplier_approval(self):
        if self.xml_supplier_approval:
            xml_decoded = base64.b64decode(self.xml_supplier_approval)
            try:
                factura = etree.fromstring(xml_decoded)
            except Exception as e:
                _logger.info('E-INV CR - This XML file is not XML-compliant.  Exception %s', e)
                return {'status': 400,
                        'text': 'Excepción de conversión de XML'}

            pretty_xml_string = etree.tostring(
                factura, pretty_print=True,
                encoding='UTF-8', xml_declaration=True)
            _logger.error('E-INV CR - send_file XML: %s', pretty_xml_string)
            namespaces = factura.nsmap
            inv_xmlns = namespaces.pop(None)
            namespaces['inv'] = inv_xmlns
            if not factura.xpath("inv:Clave", namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Attention',
                                    'message': 'The xml file does not contain the Clave node. '
                                               'Please upload a file with the correct format.'}}

            if not factura.xpath("inv:FechaEmision", namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Attention',
                                    'message': 'The xml file does not contain the FechaEmision node. '
                                               'Please upload a file with the correct format.'}}

            if not factura.xpath("inv:Emisor/inv:Identificacion/inv:Numero",
                                 namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Attention',
                                    'message': 'The xml file does not contain the Emisor node. '
                                               'Please upload a file with the correct format.'}}

            if not factura.xpath("inv:ResumenFactura/inv:TotalComprobante",
                                 namespaces=namespaces):
                return {'value': {'xml_supplier_approval': False},
                        'warning': {'title': 'Attention',
                                    'message': 'The TotalComprobante node cannot be located. '
                                               'Please upload a file with the correct format.'}}

        else:
            self.state_tributacion = False
            self.xml_supplier_approval = False
            self.fname_xml_supplier_approval = False
            self.xml_respuesta_tributacion = False
            self.fname_xml_respuesta_tributacion = False
            self.date_issuance = False
            self.number_electronic = False
            self.state_invoice_partner = False

    def load_xml_data(self):
        try:
            account = False
            analytic_account = False
            product = False
            _logger.debug('Started process.')
            purchase_journal = self.journal_id if self.journal_id and self.journal_id.type == 'purchase' else False

            if not purchase_journal:
                purchase_journal = self.env['account.journal'].search([
                    ('type', '=', 'purchase'),
                    '|',
                    ('company_id', '=', self.company_id.id),
                    ('company_id', '=', False),
                ], limit=1)

            default_account_id = purchase_journal.expense_account_id.id

            if default_account_id:
                account = self.env['account.account'].search([
                    ('id', '=', default_account_id),
                    ('company_id', '=', self.company_id.id),
                ], limit=1)
                load_lines = purchase_journal.load_lines
            else:
                default_account_id = self.env['ir.config_parameter'].sudo().get_param('expense_account_id')
                load_lines = bool(self.env['ir.config_parameter'].sudo().get_param('load_lines'))
                if default_account_id:
                    account = self.env['account.account'].search([
                        ('id', '=', default_account_id),
                        ('company_id', '=', self.company_id.id),
                    ], limit=1)
            analytic_account_id = purchase_journal.expense_analytic_account_id.id
            if analytic_account_id:
                analytic_account = self.env['account.analytic.account'].search([('id', '=', analytic_account_id)],
                                                                               limit=1)
            else:
                analytic_account_id = self.env['ir.config_parameter'].sudo().get_param('expense_analytic_account_id')
                if analytic_account_id:
                    analytic_account = self.env['account.analytic.account'].search([('id', '=', analytic_account_id)],
                                                                                   limit=1)

            product_id = purchase_journal.expense_product_id.id
            if product_id:
                product = self.env['product.product'].search([('id', '=', product_id)], limit=1)
            else:
                product_id = self.env['ir.config_parameter'].sudo().get_param('expense_product_id')
                if product_id:
                    product = self.env['product.product'].search([('id', '=', product_id)], limit=1)

            # Sin una cuenta de gasto de esta compania las lineas no se pueden
            # crear: sin cuenta reventaba con "'bool' object has no attribute
            # 'id'". La cuenta de Ajustes es un solo parametro para todas las
            # companias; la del diario de compras es propia de cada una.
            if load_lines and not account:
                raise UserError(_(
                    'No se pueden cargar las líneas del XML: falta una cuenta de gasto válida '
                    'para la compañía "%(company)s". Configúrela en el diario de compras '
                    '"%(journal)s", pestaña Factura Electrónica.'
                ) % {'company': self.company_id.display_name,
                     'journal': purchase_journal.display_name if purchase_journal else ''})

            api_facturae.load_xml_data(self, load_lines, account, product, analytic_account)
        except Exception as e:
            if e.__class__.__name__ == "UniqueViolation":
                raise UserError("Duplicate XML invoice.")
            raise e

    def action_send_mrs_to_hacienda(self):
        if any(inv.state != 'posted' for inv in self):
            raise UserError(_('Confirme la factura antes de enviarla a Hacienda.'))
        if self.state_invoice_partner:
            self.state_tributacion = False
            self.send_mrs_to_hacienda()
        else:
            raise UserError(_('You must select the aceptance state: Accepted, Parcial Accepted or Rejected'))

    def send_mrs_to_hacienda(self):
        for inv in self:
            if inv.xml_supplier_approval:

                # Verificar si el MR ya fue enviado y estamos esperando la confirmación
                if inv.state_tributacion == 'procesando':

                    token_m_h = api_facturae.get_token_hacienda(
                        inv, inv.company_id.frm_ws_ambiente)

                    api_facturae.consulta_documentos(inv, inv,
                                                     inv.company_id.frm_ws_ambiente,
                                                     token_m_h,
                                                     api_facturae.get_time_hacienda(),
                                                     False)
                else:

                    if inv.state_tributacion and inv.state_tributacion in ('aceptado', 'rechazado', 'na'):
                        raise UserError(_('Warning!.\n The supplier invoice has already been confirmed'))
                    if not inv.amount_total_electronic_invoice and inv.xml_supplier_approval:
                        try:
                            inv.load_xml_data()
                        except UserError as error:
                            inv.state_tributacion = 'error'
                            inv.message_post(subject=_('Error'),
                                             body=_('Aviso!.\n Error en carga del XML del proveedor') + str(error))
                            continue

                    if abs(inv.amount_total_electronic_invoice - inv.amount_total) > 1:
                        inv.state_tributacion = 'error'
                        inv.message_post(subject=_('Error'),
                                         body=_('Warning!.\n Total amount does not match XML amount'))
                        continue

                    elif not inv.xml_supplier_approval:
                        inv.state_tributacion = 'error'
                        inv.message_post(subject=_('Error'),
                                         body=_('Warning!.\n XML file not loaded'))
                        continue

                    elif not inv.company_id.sucursal_MR or not inv.company_id.terminal_MR:
                        inv.state_tributacion = 'error'
                        inv.message_post(subject=_('Error'),
                                         body=_('Warning!.\n Please configure the purchase journal, ' +
                                                'terminal and branch'))
                        continue

                    if not inv.state_invoice_partner:
                        inv.state_tributacion = 'error'
                        msg_body = _("Warning!\nYou must first select the response type for the uploaded file.")
                        inv.message_post(subject=_('Error'),
                                         body=msg_body)
                        continue

                    if inv.company_id.frm_ws_ambiente != 'disabled' and inv.state_invoice_partner:

                        message_description = _("<p><b>Enviando Mensaje Receptor</b></p>")

                        # '''Si por el contrario es un documento nuevo, asignamos todos los valores'''
                        if not inv.xml_comprobante or inv.state_invoice_partner not in ['procesando', 'aceptado']:

                            if inv.state_invoice_partner == '1':
                                detalle_mensaje = 'Aceptado'
                                tipo = 1
                                tipo_documento = 'CCE'
                                sequence = inv.company_id.CCE_sequence_id.next_by_id()

                            elif inv.state_invoice_partner == '2':
                                detalle_mensaje = 'Aceptado parcial'
                                tipo = 2
                                tipo_documento = 'CPCE'
                                sequence = inv.company_id.CPCE_sequence_id.next_by_id()
                            else:
                                detalle_mensaje = 'Rechazado'
                                tipo = 3
                                tipo_documento = 'RCE'
                                sequence = inv.company_id.RCE_sequence_id.next_by_id()

                            # '''Si el mensaje fue rechazado, necesitamos generar un nuevo id'''
                            if inv.state_tributacion in ['rechazado', 'error']:
                                message_description += '<p><b>'
                                message_description += _('Consecutive Switching of Receiver Message</b><br/>')
                                message_description += '</b><br/>'
                                message_description += _('<b>Previous consecutive:</b>')
                                message_description += inv.consecutive_number_receiver
                                message_description += '<br/>'
                                message_description += _('<b>Previous state: </b>')
                                message_description += inv.state_tributacion
                                message_description += '</p>'

                            # '''Solicitamos la clave para el Mensaje Receptor'''
                            response_json = api_facturae.get_clave_hacienda(inv,
                                                                            tipo_documento,
                                                                            sequence,
                                                                            inv.company_id.sucursal_MR,
                                                                            inv.company_id.terminal_MR)

                            inv.consecutive_number_receiver = response_json.get(
                                'consecutivo')
                            # '''Generamos el Mensaje Receptor'''
                            if inv.amount_total_electronic_invoice is None or inv.amount_total_electronic_invoice == 0:
                                inv.state_tributacion = 'error'
                                msg_body = _('The Total amount of the Invoice for the Message Receiver is invalid')
                                inv.message_post(subject=_('Error'),
                                                 body=msg_body)
                                continue

                            xml = api_facturae.gen_xml_mr_44(
                                inv.number_electronic, inv.partner_id.vat,
                                inv.date_issuance,
                                tipo, detalle_mensaje, inv.company_id.vat,
                                inv.consecutive_number_receiver,
                                inv.amount_tax_electronic_invoice,
                                inv.amount_total_electronic_invoice,
                                inv.company_id.activity_id.code,
                                '01')

                            xml_firmado = api_facturae.sign_xml(
                                inv.company_id.signature,
                                inv.company_id.frm_pin, xml)

                            inv.fname_xml_comprobante = tipo_documento + '_' + inv.number_electronic + '.xml'
                            self.env['ir.attachment'].sudo().create({'name': inv.fname_xml_comprobante,
                                                                     'type': 'binary',
                                                                     # 'datas': base64.encodestring(xml_firmado),
                                                                     'datas': base64.b64encode(xml_firmado),
                                                                     'res_model': inv._name,
                                                                     'res_id': inv.id,
                                                                     'res_field': 'xml_comprobante',
                                                                     'res_name': inv.fname_xml_comprobante,
                                                                     'mimetype': 'text/xml'})
                            # inv.xml_comprobante = base64.encodestring(xml_firmado)
                            inv.tipo_documento = tipo_documento

                            if inv.state_tributacion != 'procesando':

                                env = inv.company_id.frm_ws_ambiente
                                token_m_h = api_facturae.get_token_hacienda(
                                    inv, inv.company_id.frm_ws_ambiente)

                                response_json = api_facturae.send_message(
                                    inv, api_facturae.get_time_hacienda(),
                                    xml_firmado,
                                    token_m_h, env)
                                status = response_json.get('status')

                                if 200 <= status <= 299:
                                    inv.state_tributacion = 'procesando'
                                else:
                                    inv.state_tributacion = 'error'
                                    _logger.error(
                                        'E-INV CR - Invoice: %s  Error sending Acceptance Message: %s',
                                        inv.number_electronic,
                                        response_json.get('text'))

                                if inv.state_tributacion == 'procesando':
                                    token_m_h = api_facturae.get_token_hacienda(
                                        inv, inv.company_id.frm_ws_ambiente)

                                    if not token_m_h:
                                        _logger.error(
                                            _('E-INV CR - Send Acceptance Message - HALTED - Failed to get token'))
                                        return

                                    _logger.error(
                                        _('E-INV CR - send_mrs_to_hacienda - 013'))

                                    response_json = api_facturae.consulta_clave(
                                        inv.number_electronic + '-' + inv.consecutive_number_receiver,
                                        token_m_h,
                                        inv.company_id.frm_ws_ambiente)
                                    status = response_json['status']

                                    if status == 200:
                                        inv.state_tributacion = response_json.get(
                                            'ind-estado')
                                        # inv.xml_respuesta_tributacion = response_json.get('respuesta-xml')
                                        file_name = 'ACH_' + inv.number_electronic + '-' + \
                                                    inv.consecutive_number_receiver + '.xml'
                                        if not inv._store_hacienda_response_xml(
                                                response_json.get('respuesta-xml'), file_name):
                                            inv.state_tributacion = 'procesando'

                                        _logger.error(
                                            'E-INV CR - Estado Documento:%s',
                                            inv.state_tributacion)

                                        message_description += _('<p><b>Ha enviado Mensaje de Receptor</b>') + \
                                                               _('<br /><b>Documento: </b>') + inv.number_electronic + \
                                                               _('<br /><b>Consecutivo de mensaje: </b>') + \
                                                               inv.consecutive_number_receiver + \
                                                               _('<br/><b>Mensaje indicado:</b>') \
                                                               + detalle_mensaje + '</p>'

                                        self.message_post(
                                            body=message_description)
                                        # subtype='mail.mt_note',
                                        # content_subtype='html')

                                        _logger.info(_(f'E-INV CR - Document Status:{inv.state_tributacion}'))

                                    elif status == 400:
                                        inv.state_tributacion = 'ne'
                                        _logger.error(
                                            _('E-INV CR - Document Acceptance:%s not found in Hacienda.'),
                                            inv.number_electronic + '-' + inv.consecutive_number_receiver)
                                    else:
                                        _logger.error(
                                            _('E-INV CR - Unexpected error in Send Acceptance File - Aborting'))
                                        return

    @api.model
    # cron Job that verifies if the invoices are Validated at Tributación
    def _check_hacienda_for_invoices(self, max_invoices=10):
        out_invoices = self.env['account.move'].search(
            [('move_type', 'in', ('out_invoice', 'out_refund')),
             ('state', '=', 'posted'),
             ('state_tributacion', 'in', ('recibido', 'procesando', 'ne'))],  # , 'error'
            limit=max_invoices)

        in_invoices = self.env['account.move'].search(
            [('move_type', '=', 'in_invoice'),
             ('tipo_documento', '=', 'FEC'),
             ('state', '=', 'posted'),
             ('state_tributacion', 'in', ('procesando', 'ne', 'error'))],
            limit=max_invoices)

        invoices = out_invoices | in_invoices

        total_invoices = len(invoices)
        current_invoice = 0

        _logger.info(_(f'E-INV CR - Inquiry Treasury - Invoices to Verify: {total_invoices}'))

        for i in invoices:
            try:
                current_invoice += 1
                _logger.info(_('E-INV CR - Consult Hacienda - Invoice %s / %s  -  number:%s'),
                             current_invoice, total_invoices, i.number_electronic)

                token_m_h = api_facturae.get_token_hacienda(i, i.company_id.frm_ws_ambiente)

                if not token_m_h:
                    _logger.error(_('E-INV CR - Consult Hacienda - HALTED - Failed to get token'))
                    return

                if not i.xml_comprobante:
                    i.state_tributacion = 'error'
                    _logger.warning(_('E-INV CR - Document:%s has no XML document. Status %s'),
                                    i.number_electronic, 'error')
                    continue

                if not i.number_electronic or len(i.number_electronic) != 50:
                    i.state_tributacion = 'error'
                    _logger.warning(_('E-INV CR - Document:%s does not comply with the format of ') +
                                    _('electronic number. Status: %s'), i.number, 'error')
                    continue

                response_json = api_facturae.consulta_clave(i.number_electronic,
                                                            token_m_h,
                                                            i.company_id.frm_ws_ambiente)
                status = response_json['status']

                if status == 200:
                    estado_m_h = response_json.get('ind-estado')
                    _logger.info(_(f'E-INV CR - Document Status:{estado_m_h}'))
                elif status == 400:
                    estado_m_h = response_json.get('ind-estado')
                    i.state_tributacion = 'ne'
                    _logger.warning(_('E-INV CR - Document:%s not found in') +
                                    _('Hacienda.  Status: %s'), i.number_electronic, estado_m_h)
                    continue
                else:
                    _logger.error(_('E-INV CR - Unexpected error in query Hacienda  - Aborting'))
                    return

                i.state_tributacion = estado_m_h

                if estado_m_h == 'aceptado':
                    if not i._store_hacienda_response_xml(response_json.get('respuesta-xml')):
                        i.state_tributacion = 'procesando'
                        continue

                    if i.tipo_documento != 'FEC' and i.partner_id and i.partner_id.email:
                        email_template = self.env.ref('account.email_template_edi_invoice', False)
                        domain = [('res_model', '=', i._name),
                                  ('res_id', '=', i.id),
                                  ('res_field', '=', 'xml_comprobante'),
                                  ('name', '=', i.tipo_documento + '_' + i.number_electronic + '.xml')]
                        attachment = self.env['ir.attachment'].sudo().search(domain, limit=1)
                        if attachment:
                            attachment.name = i.fname_xml_comprobante

                            domain_resp = [('res_model', '=', i._name),
                                           ('res_id', '=', i.id),
                                           ('res_field', '=', 'xml_respuesta_tributacion'),
                                           ('name', '=', 'AHC_' + i.number_electronic + '.xml')]
                            attachment_resp = self.env['ir.attachment'].sudo().search(domain_resp, limit=1)

                            if attachment_resp:
                                attachment_resp.name = i.fname_xml_respuesta_tributacion

                                # Los XML se adjuntan a este correo concreto:
                                # la plantilla es compartida y dejarselos puestos
                                # mandaba el XML de una factura con otra.
                                i._send_electronic_invoice_email(email_template)

                elif estado_m_h in ('firma_invalida'):
                    if i.error_count > 10:
                        if i._store_hacienda_response_xml(response_json.get('respuesta-xml')):
                            i.state_email = 'fe_error'
                            _logger.info(_('email not sent - invoice rejected'))
                        else:
                            i.state_tributacion = 'procesando'
                    else:
                        i.error_count += 1
                        i.state_tributacion = 'procesando'

                elif estado_m_h == 'rechazado':
                    i.state_email = 'fe_error'
                    i.state_tributacion = estado_m_h
                    response_xml = response_json.get('respuesta-xml')
                    if i._store_hacienda_response_xml(response_xml):
                        decoded_xml = base64.b64decode(response_xml).decode('utf-8')
                        xml_errors = decoded_xml.partition('<DetalleMensaje>')[2].partition('</DetalleMensaje>')[0]
                        # _logger.error(xml_errors)
                        i.message_post(subject='Error', body=xml_errors)
                    else:
                        i.state_tributacion = 'procesando'
                else:
                    if i.error_count > 10:
                        i.state_tributacion = 'error'
                    elif i.error_count < 4:
                        i.error_count += 1
                        i.state_tributacion = 'procesando'
                    else:
                        i.error_count += 1
                        i.state_tributacion = ''
                    # doc.state_tributacion = 'no_encontrado'
                    _logger.error('E-INV CR - Query Hacienda - Invoice not found: %s  -  '
                                  'Hacienda Status: %s', i.number_electronic, estado_m_h)
            except Exception as error:
                i.state_tributacion = 'error'
                i.message_post(subject=_('Error'),
                               body=_('Warning!.\n Error in _check_hacienda_for_invoices: ') + str(error))
                continue

    def action_check_hacienda(self):
        if self.company_id.frm_ws_ambiente != 'disabled':
            for inv in self:
                token_m_h = api_facturae.get_token_hacienda(inv, inv.company_id.frm_ws_ambiente)
                api_facturae.consulta_documentos(self, inv, self.company_id.frm_ws_ambiente, token_m_h, False, False)

    # ==================================================================
    # Reintento de documentos electronicos rechazados/con error
    # ==================================================================
    #
    # Portado de v19 (quicknet, models/account_move.py). Un documento
    # rechazado ya gasto un Clave real (Hacienda no revalida contenido nuevo
    # bajo el mismo Clave, ver docs/D150_CAMPOS_OFICIALES.md), asi que
    # "reintentar" no es reenviar el mismo registro: hay que dejar constancia
    # contable de que ese documento no sirvio, y crear uno nuevo en borrador
    # con Clave propia para que el usuario lo revise y confirme.
    #
    # Diferencia real con v19 que hubo que resolver aqui: el _reverse_moves
    # de este modulo (mas abajo en este archivo) llama action_post() sobre la
    # reversa automaticamente al crearla -v19 no lo hace-. Si esa reversa
    # llegara a pasar por la logica electronica de action_post, intentaria
    # sacarle un Clave real a Hacienda para una nota de credito que no debe
    # existir electronicamente (el documento original nunca fue aceptado).
    # Se evita marcando la reversa con tipo_documento='disabled' desde su
    # creacion: action_post ya sabe saltarse toda la logica de Hacienda para
    # ese caso (es el mismo mecanismo de cr_invoice_disabled_no_consecutive).

    def _get_fe_retry_clean_electronic_values(self):
        """Campos electronicos a limpiar en la copia/reversa del reintento."""
        clean_values = {
            'number_electronic': False,
            'sequence': False,
            'state_tributacion': False,
            'electronic_invoice_return_message': False,
            'error_count': 0,
            'xml_comprobante': False,
            'fname_xml_comprobante': False,
            'xml_respuesta_tributacion': False,
            'fname_xml_respuesta_tributacion': False,
            'date_issuance': False,
            'amount_tax_electronic_invoice': 0.0,
            'amount_total_electronic_invoice': 0.0,
            'amount_total_iva_devuelto': 0.0,
        }
        return {name: value for name, value in clean_values.items() if name in self._fields}

    def _cancel_fe_retry_payments(self):
        """No se puede reintentar con pagos ya conciliados sin revisarlos a mano."""
        self.ensure_one()
        statement_lines = self._get_reconciled_statement_lines()
        if statement_lines:
            raise UserError(_(
                'No se puede reintentar el documento electronico porque tiene pagos '
                'conciliados desde extractos bancarios. Revise o desconcilie esos pagos '
                'antes de reintentar.'
            ))
        for payment in self._get_reconciled_payments():
            if payment.state == 'cancel':
                continue
            try:
                payment.action_cancel()
            except Exception as error:
                raise UserError(_(
                    'No se pudo cancelar el pago %(payment)s asociado al documento '
                    '%(invoice)s.\nDetalle: %(error)s'
                ) % {
                    'payment': payment.display_name,
                    'invoice': self.display_name,
                    'error': error,
                }) from error

    def _reverse_fe_retry_accounting(self):
        """Reversa contable interna del documento rechazado, sin tocar Hacienda.

        tipo_documento='disabled' en los valores por defecto hace que, cuando
        _reverse_moves confirme esta reversa automaticamente, action_post la
        trate igual que cualquier comprobante deshabilitado: asiento contable
        normal, sin pedir Clave ni enviar nada a Hacienda.
        """
        self.ensure_one()
        reversal = self._reverse_moves(default_values_list=[{
            'date': fields.Date.context_today(self),
            'ref': _('Reversa interna por reintento de %s') % (self.name or self.sequence or self.number_electronic),
            'tipo_documento': 'disabled',
        }], cancel=True)
        reversal.write({
            **reversal._get_fe_retry_clean_electronic_values(),
            'state_tributacion': 'na',
            'electronic_invoice_return_message': _(
                'Reversa interna generada por un reintento de documento electronico. '
                'No se envia a Hacienda: el documento original nunca fue aceptado.'),
            'fe_retry_origin_id': self.id,
        })
        return reversal

    # Anexos y Estructuras v4.4, nota 10: el comprobante que sustituye a uno
    # rechazado por Hacienda lo referencia con el tipo 10.
    FE_RETRY_REFERENCE_DOCUMENT_CODE = '10'
    FE_RETRY_REFERENCE_CODE = '04'

    def _get_fe_retry_hacienda_state(self):
        """Estado real en Hacienda antes de reintentar: 'rechazado' o 'no_recibido'.

        Bloquea si Hacienda la acepto (reintentar duplicaria la venta), si aun
        la procesa o si no se pudo consultar."""
        self.ensure_one()
        if not self.number_electronic:
            return 'no_recibido'
        ambiente = self.company_id.frm_ws_ambiente
        respuesta = api_facturae.consulta_clave(
            self.number_electronic, api_facturae.get_token_hacienda(self, ambiente), ambiente)
        estado = respuesta.get('ind-estado')
        if respuesta.get('status') == 200:
            if estado == 'rechazado':
                return 'rechazado'
            if estado == 'aceptado':
                raise UserError(_(
                    'No se puede reintentar %s: Hacienda la tiene ACEPTADA. Consulte su estado para actualizarla.'
                ) % self.display_name)
            raise UserError(_(
                'No se puede reintentar %(doc)s: Hacienda todavia la reporta como "%(estado)s". '
                'Espere la respuesta final.'
            ) % {'doc': self.display_name, 'estado': estado})
        if 'no ha sido recibido' in (respuesta.get('error-cause') or ''):
            return 'no_recibido'
        raise UserError(_(
            'No se puede reintentar %(doc)s: no se pudo verificar su estado en Hacienda (%(detalle)s). '
            'Intente de nuevo.'
        ) % {'doc': self.display_name,
             'detalle': respuesta.get('error-cause') or respuesta.get('text') or respuesta.get('status')})

    def _copy_for_fe_retry(self, reversal, hacienda_state=False):
        """Copia limpia del documento rechazado, en borrador, con Clave propia."""
        self.ensure_one()
        reference = {
            'invoice_id': self.invoice_id.id,
            'reference_code_id': self.reference_code_id.id,
            'reference_document_id': self.reference_document_id.id,
        }
        if self.move_type == 'out_invoice' and hacienda_state == 'rechazado':
            reference = {
                'invoice_id': self.id,
                'reference_code_id': self.env['reference.code'].search(
                    [('code', '=', self.FE_RETRY_REFERENCE_CODE)], limit=1).id,
                'reference_document_id': self.env['reference.document'].search(
                    [('code', '=', self.FE_RETRY_REFERENCE_DOCUMENT_CODE)], limit=1).id,
            }
        new_invoice = self.copy({
            **self._get_fe_retry_clean_electronic_values(),
            **reference,
            'name': '/',
            'fe_retry_origin_id': self.id,
            'fe_retry_reversal_id': reversal.id,
            'fe_retry_new_id': False,
        })
        return new_invoice

    def action_retry_rejected_electronic_invoice(self):
        """Boton 'Reintentar': reversa interna + copia nueva en borrador.

        No reenvia nada a Hacienda por si sola -el usuario revisa la copia
        nueva y la confirma cuando este lista, igual que cualquier factura-.
        """
        new_invoices = self.env['account.move']
        for inv in self:
            if inv.move_type not in ('out_invoice', 'out_refund') or \
                    inv.state_tributacion not in ('rechazado', 'error'):
                raise UserError(_('Solo se pueden reintentar facturas y notas de credito de '
                                   'cliente rechazadas o con error.'))
            hacienda_state = inv._get_fe_retry_hacienda_state()
            inv._cancel_fe_retry_payments()
            reversal = inv._reverse_fe_retry_accounting()
            new_invoice = inv._copy_for_fe_retry(reversal, hacienda_state)
            inv.write({
                'fe_retry_new_id': new_invoice.id,
                'fe_retry_reversal_id': reversal.id,
            })
            inv.message_post(body=_(
                'Documento reintentado. Se creo una reversa interna y un nuevo documento '
                'en borrador: %s.') % new_invoice.display_name)
            reversal.message_post(body=_(
                'Reversa interna generada por el reintento del documento %s.') % inv.display_name)
            new_invoice.message_post(body=_(
                'Documento creado en borrador desde el reintento de %s. Revise los datos '
                '(incluido el numero de identificacion del receptor si es de exportacion) '
                'y confirme manualmente cuando corresponda.') % inv.display_name)
            new_invoices |= new_invoice

        if len(new_invoices) == 1:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Documento para reintentar'),
                'res_model': 'account.move',
                'view_mode': 'form',
                'res_id': new_invoices.id,
                'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _('Documentos para reintentar'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', new_invoices.ids)],
            'target': 'current',
        }

    @api.model
    def _check_hacienda_for_mrs(self, max_invoices=10):  # cron
        invoices = self.env['account.move'].search(
            [('move_type', 'in', ('in_invoice', 'in_refund')),
             ('tipo_documento', '!=', 'FEC'),
             ('state', '=', 'posted'),
             ('xml_supplier_approval', '!=', False),
             ('state_invoice_partner', '!=', False),
             ('state_tributacion', 'not in', ('aceptado', 'rechazado', 'error', 'na'))],
            limit=max_invoices)
        total_invoices = len(invoices)
        current_invoice = 0

        for inv in invoices:
            # CWong: esto no debe llamarse porque cargaría de nuevo los impuestos y ya se pusieron como debería
            # if not i.amount_total_electronic_invoice:
            #     i.charge_xml_data()
            current_invoice += 1
            _logger.info('_check_hacienda_for_mrs - Invoice %s / %s  -  number:%s',
                         current_invoice, total_invoices, inv.number_electronic)
            inv.send_mrs_to_hacienda()

    def action_create_fec(self):
        if any(inv.state != 'posted' for inv in self):
            raise UserError(_('Confirme la factura antes de enviarla a Hacienda.'))
        # Antes lanzaba este error siempre, aun despues de enviar: revertia lo
        # guardado del envio aunque Hacienda ya habia recibido la FEC.
        if not self.company_id.frm_ws_ambiente == 'disabled':
            return self.generate_and_send_invoices(self)

        raise UserError(_('Hacienda API is disabled in company'))

    @api.model
    def _send_invoices_to_hacienda(self, max_invoices=10):  # cron
        # La vigencia de la llave se revisa por factura, con la de su compania.
        # La FEC no entra: en 14 toda factura manual de un proveedor de Costa
        # Rica con cedula se vuelve FEC al confirmar, y se envia con su boton.
        invoices = self.env['account.move'].search([
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('number_electronic', '!=', False),
            ('invoice_date', '>=', '2019-07-01'),
            ('company_id.frm_ws_ambiente', 'in', ('api-stag', 'api-prod')),
            ('tipo_documento', '!=', 'disabled'),
            ('state_tributacion', 'in', (False, 'ne')),
        ], order='id asc', limit=max_invoices)
        self.generate_and_send_invoices(invoices)
        _logger.info('E-INV CR - _send_invoices_to_hacienda - Completed Successfully')

    def generate_and_send_invoice(self):
        days_left = self.env.user.company_id.get_days_left()
        if days_left >= 0:
            self.generate_and_send_invoices(self)
        else:
            message = self.env.user.company_id.get_message_to_send()
            self.message_post(body=message,
                              subject=_('IMPORTANT NOTICE!!'),
                              message_type='notification',
                              # subtype=None,
                              parent_id=False)
        _logger.info('E-INV CR - _send_invoices_to_hacienda - Completed Successfully')

    def generate_and_send_invoices(self, invoices):
        total_invoices = len(invoices)
        current_invoice = 0

        for inv in invoices:
            try:
                # Vigencia de la llave de la compania de la factura, no la del
                # usuario: el cron corre para todas las companias.
                days_left = inv.company_id.get_days_left()
                message = inv.company_id.get_message_to_send()
                if days_left < 0:
                    inv._set_electronic_invoice_error(message)
                    continue
                current_invoice += 1

                if days_left <= inv.company_id.range_days:
                    inv.message_post(
                        body=message,
                        subject=_('IMPORTANT NOTICE!!'),
                        message_type='notification',
                        # subtype=None,
                        parent_id=False,
                    )

                if not inv.sequence or not inv.sequence.isdigit():  # or (len(inv.number) == 10):
                    if inv.tipo_documento == 'FEC':
                        # Una FEC puede haberse confirmado sin consecutivo si al
                        # confirmar faltaban datos del proveedor o la secuencia de
                        # la compania. Se genera aqui si ya es posible; si no, se
                        # marca error recuperable en vez de 'na', que dejaba el
                        # documento sin boton y fuera de todas las colas.
                        sequence_record = inv.get_invoice_sequence()[1]
                        motivo = inv._get_fec_blocking_reason()
                        if not sequence_record or motivo:
                            inv._set_electronic_invoice_error(
                                motivo or _('No se pudo determinar el consecutivo de la '
                                            'Factura Electronica de Compra.'))
                            continue
                        response_json = api_facturae.get_clave_hacienda(
                            inv, 'FEC', sequence_record.next_by_id(),
                            inv.journal_id.sucursal or inv.company_id.sucursal_MR,
                            inv.journal_id.terminal or inv.company_id.terminal_MR)
                        inv.number_electronic = response_json.get('clave')
                        inv.sequence = response_json.get('consecutivo')
                        inv._set_fec_name_from_consecutive()
                        _logger.info('E-INV CR - FEC sin consecutivo, generado: %s', inv.sequence)
                    else:
                        inv.state_tributacion = 'na'
                        _logger.info('E-INV CR - Ignored invoice:%s', inv.name)
                        continue

                _logger.debug('generate_and_send_invoices - Invoice %s / %s  -  number:%s',
                              current_invoice, total_invoices, inv.number_electronic)

                if not inv.xml_comprobante or (inv.tipo_documento == 'FEC' and inv.state_tributacion == 'rechazado'):
                    # En una FEC el Emisor del XML es el proveedor, y ese bloque
                    # exige datos que en un res.partner pueden faltar. Se avisa
                    # cual falta en vez de reventar armando el XML.
                    if inv.tipo_documento == 'FEC':
                        motivo = inv._get_fec_blocking_reason()
                        if motivo:
                            inv._set_electronic_invoice_error(motivo)
                            continue

                    if inv.tipo_documento == 'FEC' and inv.state_tributacion == 'rechazado':
                        msg_body = _('Another FEC is being sent because the previous one was rejected by Hacienda. ')
                        msg_body += _('Attached the previous XMLs. Previous key: ')
                        # message_post espera (nombre, contenido). Se adjuntan los
                        # XML previos solo si existen: pasar el nombre como
                        # contenido, o False, rompia el reintento.
                        previos = []
                        if inv.fname_xml_comprobante and inv.xml_comprobante:
                            previos.append((inv.fname_xml_comprobante,
                                            base64.b64decode(inv.xml_comprobante)))
                        if inv.fname_xml_respuesta_tributacion and inv.xml_respuesta_tributacion:
                            previos.append((inv.fname_xml_respuesta_tributacion,
                                            base64.b64decode(inv.xml_respuesta_tributacion)))
                        inv.message_post(body=msg_body + inv.number_electronic,
                                         subject=_('Sending a second FEC'),
                                         message_type='notification',
                                         # subtype=None,
                                         parent_id=False,
                                         attachments=previos)

                        sequence = inv.company_id.FEC_sequence_id.next_by_id()
                        response_json = api_facturae.get_clave_hacienda(inv,
                                                                        inv.tipo_documento,
                                                                        sequence,
                                                                        inv.journal_id.sucursal or inv.company_id.sucursal_MR,
                                                                        inv.journal_id.terminal or inv.company_id.terminal_MR)

                        inv.number_electronic = response_json.get('clave')
                        inv.sequence = response_json.get('consecutivo')
                        inv._set_fec_name_from_consecutive()

                    now_utc = datetime.datetime.now(pytz.timezone('UTC'))
                    now_cr = now_utc.astimezone(pytz.timezone('America/Costa_Rica'))
                    dia = inv.number_electronic[3:5]  # '%02d' % now_cr.day,
                    mes = inv.number_electronic[5:7]  # '%02d' % now_cr.month,
                    anno = inv.number_electronic[7:9]  # str(now_cr.year)[2:4],

                    date_cr = now_cr.strftime("20" + anno + "-" + mes + "-" + dia + "T%H:%M:%S-06:00")

                    inv.date_issuance = date_cr

                    numero_documento_referencia = False
                    fecha_emision_referencia = False
                    codigo_referencia = False
                    tipo_documento_referencia = False
                    razon_referencia = False
                    currency = inv.currency_id
                    invoice_comments = escape(inv.narration) if inv.narration else ''

                    if (inv.invoice_id or inv.not_loaded_invoice) and \
                            inv.reference_code_id and inv.reference_document_id:
                        # La referencia es la clave del comprobante original: la
                        # del documento en Odoo si la tiene; si no (emitido por el
                        # sistema anterior o cargado como disabled), la que se
                        # escribio a mano. Sin clave ni fecha, el XML salia sin
                        # InformacionReferencia y Hacienda rechazaba la NC.
                        if inv.invoice_id.number_electronic:
                            numero_documento_referencia = inv.invoice_id.number_electronic
                            fecha_emision_referencia = inv.invoice_id.date_issuance
                        elif inv.not_loaded_invoice and inv.not_loaded_invoice_date:
                            numero_documento_referencia = inv._get_not_loaded_invoice_key()
                            fecha_emision_referencia = inv.not_loaded_invoice_date.strftime("%Y-%m-%d")
                            fecha_emision_referencia += "T12:00:00-06:00"
                        tipo_documento_referencia = inv.reference_document_id.code
                        codigo_referencia = inv.reference_code_id.code
                        razon_referencia = inv.reference_code_id.name

                    # El esquema 4.4 exige InformacionReferencia en la FEC, pero
                    # no hay comprobante electronico previo: se declara el
                    # documento fisico del proveedor de regimen simplificado.
                    if inv.tipo_documento == 'FEC' and not tipo_documento_referencia:
                        get_param = self.env['ir.config_parameter'].sudo().get_param
                        tipo_documento_referencia = get_param(
                            'cr_electronic_invoice.fec_reference_document_code', '14')
                        codigo_referencia = get_param(
                            'cr_electronic_invoice.fec_reference_code', '04')
                        razon_referencia = self.env['reference.code'].search(
                            [('code', '=', codigo_referencia)], limit=1).name or ''
                        numero_documento_referencia = (inv.not_loaded_invoice or inv.ref or '')[:50]
                        ref_date = inv.not_loaded_invoice_date or inv.invoice_date
                        fecha_emision_referencia = ref_date.strftime('%Y-%m-%d') + 'T12:00:00-06:00'

                    if inv.invoice_payment_term_id:
                        sale_conditions = inv.invoice_payment_term_id.sale_conditions_id and \
                                          inv.invoice_payment_term_id.sale_conditions_id.code or '01'
                    else:
                        sale_conditions = '01'

                    # Validate if invoice currency is the same as the company currency
                    if currency.name == self.company_id.currency_id.name:
                        currency_rate = 1
                    else:
                        currency_rate = round(1.0 / currency.rate, 5)

                    # Generamos las líneas de la factura
                    lines = dict([])
                    otros_cargos = dict([])
                    otros_cargos_id = 0
                    line_number = 0
                    total_otros_cargos = 0.0
                    total_iva_devuelto = 0.0
                    total_servicio_salon = 0.0
                    total_servicio_gravado = 0.0
                    total_servicio_exento = 0.0
                    total_servicio_exonerado = 0.0
                    total_servicio_no_sujeto = 0.0
                    total_mercaderia_gravado = 0.0
                    total_mercaderia_exento = 0.0
                    total_mercaderia_exonerado = 0.0
                    total_mercaderia_no_sujeta = 0.0
                    total_descuento = 0.0
                    total_impuestos = 0.0
                    total_impuestos_asumidos = 0.0
                    base_subtotal = 0.0
                    _old_rate_exoneration = False
                    _no_cabys_code = False

                    for inv_line in inv.invoice_line_ids:
                        _tax_from_factory = False
                        if inv_line.display_type:  # skip sections and notes
                            continue

                        # Revisamos si está línea es de Otros Cargos
                        env_iva_devuelto = self.env.ref('cr_electronic_invoice.product_iva_devuelto').id
                        if inv_line.product_id and inv_line.product_id.id == env_iva_devuelto:
                            total_iva_devuelto = -inv_line.price_total

                        elif inv_line.product_id and inv_line.product_id.categ_id.name == 'Otros Cargos':
                            otros_cargos_id += 1
                            otros_cargos[otros_cargos_id] = {
                                'TipoDocumento': inv_line.product_id.default_code,
                                'Detalle': escape(inv_line.name[:150]),
                                'MontoCargo': inv_line.price_total
                            }
                            if inv_line.third_party_id:
                                otros_cargos[otros_cargos_id]['NombreTercero'] = inv_line.third_party_id.name

                                if inv_line.third_party_id.vat:
                                    otros_cargos[otros_cargos_id]['NumeroIdentidadTercero'] = \
                                        inv_line.third_party_id.vat

                            total_otros_cargos += inv_line.price_total

                        else:
                            if inv_line.fe_discount_code_id.code == '01':
                                _tax_from_factory = True
                            line_number += 1
                            price = inv_line.price_unit
                            quantity = inv_line.quantity
                            if not quantity:
                                continue

                            line_taxes = inv_line.tax_ids.compute_all(
                                price, currency, 1,
                                product=inv_line.product_id,
                                partner=inv_line.move_id.partner_id)

                            price_unit = round(line_taxes['total_excluded'], 5)

                            # Usar los montos reales de Odoo para evitar diferencias
                            # de redondeo entre la factura y el XML.
                            subtotal_line = round(inv_line.price_subtotal, 5)
                            line_total_odoo = round(inv_line.price_total, 5)
                            line_tax_odoo = round(line_total_odoo - subtotal_line, 5)

                            if inv_line.discount:
                                base_line_original = round(price_unit * quantity, 5)
                                descuento = round(base_line_original - subtotal_line, 5)
                            else:
                                descuento = 0.0

                            base_line = round(subtotal_line + descuento, 5)
                            price_unit = round(base_line / quantity, 5)

                            # Corregir error cuando un producto trae en el nombre "", por ejemplo: "disco duro"
                            # Esto no debería suceder, pero, si sucede, lo corregimos
                            if inv_line.name[:156].find('"'):
                                detalle_linea = inv_line.name[:160].replace(
                                    '"', '')

                            line = {
                                "cantidad": quantity,
                                "detalle": escape(detalle_linea),
                                "precioUnitario": price_unit,
                                "montoTotal": base_line,
                                "subtotal": subtotal_line,
                                "BaseImponible": subtotal_line,
                                "unidadMedida": inv_line.product_uom_id and inv_line.product_uom_id.code or 'Sp'
                            }

                            if inv_line.product_id:
                                line["codigo"] = inv_line.product_id.default_code or ''
                                line["codigoProducto"] = inv_line.product_id.code or ''

                                if inv_line.product_id.cabys_code:
                                    line["codigoCabys"] = inv_line.product_id.cabys_code
                                elif inv_line.product_id.categ_id and inv_line.product_id.categ_id.cabys_code:
                                    line["codigoCabys"] = inv_line.product_id.categ_id.cabys_code
                                elif inv.tipo_documento != 'NC':
                                    _no_cabys_code = _(f'Warning!.\nLine without CABYS code: {inv_line.name}')
                                    continue
                            else:
                                _no_cabys_code = _(f'Warning!.\nLine without CABYS code: {inv_line.name}')
                                continue

                            if inv._requires_tariff_head() and inv_line.tariff_head:
                                line["partidaArancelaria"] = re.sub(r'[^0-9]', '', inv_line.tariff_head)

                            if inv_line.discount and price_unit > 0:
                                total_descuento += descuento
                                line["montoDescuento"] = descuento
                                line["codigoDescuento"] = inv_line.fe_discount_code_id.code or '07'
                                line["naturalezaDescuento"] = inv_line.fe_discount_code_id.name or 'Descuento Comercial'

                            # Se generan los impuestos
                            taxes = dict([])
                            _line_tax = 0.0
                            _tax_exoneration = False
                            _tax_no_subject = False
                            _percentage_exoneration = 0
                            _tax_exempt = False
                            xml_tax_lines = []
                            if inv_line.tax_ids:
                                tax_index = 0

                                taxes_lookup = {}
                                for i in inv_line.tax_ids:
                                    if i.has_exoneration:
                                        _tax_exoneration = True
                                        _tax_rate = i.tax_root.amount
                                        _tax_exoneration_rate = min(i.percentage_exoneration, _tax_rate)
                                        _percentage_exoneration = _tax_exoneration_rate / _tax_rate
                                        taxes_lookup[i.id] = {'tax_code': i.tax_root.tax_code,
                                                              'tarifa': _tax_rate,
                                                              'iva_tax_desc': i.tax_root.iva_tax_desc,
                                                              'iva_tax_code': i.tax_root.iva_tax_code,
                                                              'exoneration_percentage': _tax_exoneration_rate,
                                                              'amount_exoneration': i.amount}
                                    else:

                                        if i.iva_tax_code == '01':
                                            _tax_no_subject = True
                                        if i.iva_tax_code == '10':
                                            _tax_exempt = True
                                        taxes_lookup[i.id] = {'tax_code': i.tax_code,
                                                              'tarifa': i.amount,
                                                              'iva_tax_desc': i.iva_tax_desc,
                                                              'iva_tax_code': i.iva_tax_code}

                                xml_tax_lines = [
                                    tax_line for tax_line in line_taxes['taxes']
                                    if taxes_lookup[tax_line['id']]['tax_code'] not in ('00', 'service')
                                ]

                                for i in line_taxes['taxes']:
                                    if taxes_lookup[i['id']]['tax_code'] == 'service':
                                        total_servicio_salon += round(
                                            subtotal_line * taxes_lookup[i['id']]['tarifa'] / 100, 5)

                                    elif taxes_lookup[i['id']]['tax_code'] != '00':
                                        tax_index += 1
                                        _subtotal = base_line if _tax_from_factory else subtotal_line

                                        if len(xml_tax_lines) == 1 and not _tax_exoneration and not _tax_from_factory:
                                            tax_amount = line_tax_odoo
                                        else:
                                            tax_amount = round(_subtotal * taxes_lookup[i['id']]['tarifa'] / 100, 5)

                                        _line_tax += tax_amount
                                        tax = {
                                            'codigo': taxes_lookup[i['id']]['tax_code'],
                                            'tarifa': taxes_lookup[i['id']]['tarifa'],
                                            'monto': tax_amount,
                                            'iva_tax_desc': taxes_lookup[i['id']]['iva_tax_desc'],
                                            'iva_tax_code': taxes_lookup[i['id']]['iva_tax_code'],
                                        }
                                        # Se genera la exoneración si existe para este impuesto
                                        if _tax_exoneration:
                                            exoneration_percentage = taxes_lookup[i['id']]['exoneration_percentage']
                                            _tax_amount_exoneration = round(subtotal_line *
                                                                            exoneration_percentage / 100, 5)

                                            _line_tax -= _tax_amount_exoneration

                                            tax["exoneracion"] = {
                                                "montoImpuesto": _tax_amount_exoneration,
                                                "porcentajeCompra": int(exoneration_percentage)
                                            }
                                        if _tax_from_factory:
                                            _impuesto_asumido_fab = round(
                                                base_line * taxes_lookup[i['id']]['tarifa'] / 100, 5)
                                            line["impuesto_asumido_fab"] = _impuesto_asumido_fab
                                            total_impuestos_asumidos += _impuesto_asumido_fab
                                            _line_tax -= _impuesto_asumido_fab

                                        taxes[tax_index] = tax

                                line["impuesto"] = taxes
                                line["impuestoNeto"] = 0 if _tax_from_factory else round(_line_tax, 5)
                            elif inv.tipo_documento == 'FEE':
                                _tax_exempt = True
                                taxes[1] = {
                                    'codigo': '01',
                                    'tarifa': 0.0,
                                    'monto': 0.0,
                                    'iva_tax_desc': 'Tarifa 0%',
                                    'iva_tax_code': '01',
                                }
                                line["impuesto"] = taxes
                                line["impuestoNeto"] = 0.0
                            else:
                                # El esquema 4.4 exige Impuesto e ImpuestoNeto en
                                # toda linea de detalle. Una linea sin impuestos se
                                # declara con CodigoTarifaIVA 10 (Tarifa Exenta):
                                # el 01 es "Tarifa 0%" del art. 32 y Hacienda no lo
                                # cuenta ni como gravado ni como exento.
                                _tax_exempt = True
                                taxes[1] = {
                                    'codigo': '01',
                                    'tarifa': 0.0,
                                    'monto': 0.0,
                                    'iva_tax_desc': 'Tarifa Exenta',
                                    'iva_tax_code': '10',
                                }
                                line["impuesto"] = taxes
                                line["impuestoNeto"] = 0.0

                            # Si no hay product_uom_id se asume como Servicio
                            if inv_line.product_id.type == 'service' or \
                                    inv_line.product_uom_id.category_id.name in ('Service',
                                                                                 'Services',
                                                                                 'Servicio',
                                                                                 'Servicios'):
                                if taxes:
                                    if _tax_exoneration:
                                        if _percentage_exoneration < 1:
                                            total_servicio_gravado += (base_line * (1 - _percentage_exoneration))
                                        total_servicio_exonerado += (base_line * _percentage_exoneration)
                                    elif _tax_no_subject:
                                        total_servicio_no_sujeto += base_line
                                    elif _tax_exempt:
                                        total_servicio_exento += base_line
                                    else:
                                        total_servicio_gravado += base_line

                                    total_impuestos += _line_tax
                                else:
                                    total_servicio_exento += base_line
                            else:
                                if taxes:
                                    if _tax_exoneration:
                                        if _percentage_exoneration < 1:
                                            total_mercaderia_gravado += (base_line * (1 - _percentage_exoneration))
                                        total_mercaderia_exonerado += (base_line * _percentage_exoneration)

                                    elif _tax_no_subject:
                                        total_mercaderia_no_sujeta += base_line
                                    elif _tax_exempt:
                                        total_mercaderia_exento += base_line
                                    else:
                                        total_mercaderia_gravado += base_line

                                    total_impuestos += _line_tax
                                else:
                                    total_mercaderia_exento += base_line

                            base_subtotal += subtotal_line

                            if taxes and len(xml_tax_lines) == 1 and not _tax_exoneration and not _tax_from_factory:
                                line["montoTotalLinea"] = line_total_odoo
                            else:
                                line["montoTotalLinea"] = round(subtotal_line + _line_tax, 5)

                            lines[line_number] = line
                    if total_servicio_salon:
                        total_servicio_salon = round(total_servicio_salon, 5)
                        total_otros_cargos += total_servicio_salon
                        otros_cargos_id += 1
                        otros_cargos[otros_cargos_id] = {
                            'TipoDocumento': '06',
                            'Detalle': escape('Servicio salon 10%'),
                            'MontoCargo': total_servicio_salon
                        }

                    # TODO: CORREGIR BUG NUMERO DE FACTURA NO SE
                    # GUARDA EN LA REFERENCIA DE LA NC CUANDO SE CREA MANUALMENTE
                    if inv.invoice_id and not inv.invoice_origin:
                        inv.invoice_origin = inv.invoice_id.display_name

                    if _no_cabys_code and inv.tipo_documento != 'NC':  # CAByS is not required for financial NCs
                        inv.state_tributacion = 'error'
                        inv.message_post(subject=_('Error'), body=_no_cabys_code)
                        continue

                    if _old_rate_exoneration:
                        inv.state_tributacion = 'error'
                        inv.message_post(subject=_('Error'),
                                         body=_('Review definition of tax with exemption, ' +
                                                'is in base 100 and must be base 13.'))
                        continue

                    if abs(base_subtotal + (total_impuestos) +
                           total_otros_cargos - total_iva_devuelto - inv.amount_total) > 0.5:
                        inv.state_tributacion = 'error'
                        inv.message_post(
                            subject=_('Error'),
                            body=_('Invoice amount does not match amount for XML. '
                                   'Invoice: %s XML:%s base:%s VAT:%s otros_cargos:%s iva_devuelto:%s') % (
                                     inv.amount_total, (base_subtotal + total_impuestos +
                                                        total_otros_cargos - total_iva_devuelto),
                                     base_subtotal, total_impuestos, total_otros_cargos, total_iva_devuelto))
                        continue
                    total_servicio_gravado = round(total_servicio_gravado, 5)
                    total_servicio_exento = round(total_servicio_exento, 5)
                    total_servicio_exonerado = round(total_servicio_exonerado, 5)
                    total_mercaderia_gravado = round(total_mercaderia_gravado, 5)
                    total_mercaderia_exento = round(total_mercaderia_exento, 5)
                    total_mercaderia_exonerado = round(total_mercaderia_exonerado, 5)
                    total_otros_cargos = round(total_otros_cargos, 5)
                    total_iva_devuelto = round(total_iva_devuelto, 5)
                    base_subtotal = round(base_subtotal, 5)
                    total_impuestos = round(total_impuestos, 5)
                    total_descuento = round(total_descuento, 5)
                    total_impuestos_asumidos = round(total_impuestos_asumidos, 5)
                    # ESTE METODO GENERA EL XML DIRECTAMENTE DESDE PYTHON
                    xml_string_builder = api_facturae.gen_xml_v44(
                        inv, sale_conditions, total_servicio_gravado,
                        total_servicio_exento, total_servicio_exonerado,
                        total_mercaderia_gravado, total_mercaderia_exento,
                        total_mercaderia_exonerado, total_otros_cargos, total_iva_devuelto, base_subtotal,
                        total_impuestos, total_descuento, lines,
                        otros_cargos, currency_rate, invoice_comments,
                        tipo_documento_referencia, numero_documento_referencia,
                        fecha_emision_referencia, codigo_referencia, razon_referencia, total_mercaderia_no_sujeta,
                        total_servicio_no_sujeto, total_impuestos_asumidos)

                    xml_to_sign = str(xml_string_builder)
                    xml_firmado = api_facturae.sign_xml(
                        inv.company_id.signature,
                        inv.company_id.frm_pin,
                        xml_to_sign)

                    # Se valida localmente contra el XSD real de Hacienda antes
                    # de gastar un envio -y un consecutivo real- en un documento
                    # que se sabe de antemano que va a rechazar. No reemplaza la
                    # validacion de Hacienda (su copia del esquema es la que
                    # manda), solo evita el viaje redondo cuando el error es
                    # detectable aqui mismo.
                    error_local = api_facturae.validar_contra_esquema_hacienda(
                        inv.tipo_documento, xml_firmado)
                    if error_local:
                        raise UserError(_(
                            'El XML no pasa la validacion local contra el esquema de '
                            'Hacienda (%s): %s') % (inv.tipo_documento, error_local))

                    # inv.xml_comprobante = base64.encodestring(xml_firmado)
                    inv.fname_xml_comprobante = inv.tipo_documento + '_' + inv.number_electronic + '.xml'
                    self.env['ir.attachment'].sudo().create({'name': inv.fname_xml_comprobante,
                                                             'type': 'binary',
                                                             # 'datas': base64.encodestring(xml_firmado),
                                                             'datas': base64.b64encode(xml_firmado),
                                                             'res_model': self._name,
                                                             'res_id': inv.id,
                                                             'res_field': 'xml_comprobante',
                                                             'res_name': inv.fname_xml_comprobante,
                                                             'mimetype': 'text/xml'})

                    _logger.info('E-INV CR - SIGNED XML:%s', inv.fname_xml_comprobante)
                else:
                    xml_firmado = inv.xml_comprobante

                # Get token from Hacienda
                token_m_h = api_facturae.get_token_hacienda(inv, inv.company_id.frm_ws_ambiente)

                response_json = api_facturae.send_xml_fe(inv, token_m_h, inv.date_issuance,
                                                         xml_firmado, inv.company_id.frm_ws_ambiente)

                response_status = response_json.get('status')
                response_text = response_json.get('text')

                if 200 <= response_status <= 299:
                    if inv.tipo_documento == 'FEC':
                        inv.state_tributacion = 'procesando'
                    else:
                        inv.state_tributacion = 'procesando'
                    inv.electronic_invoice_return_message = response_text
                else:
                    if response_text.find('ya fue recibido anteriormente') != -1:
                        if inv.tipo_documento == 'FEC':
                            inv.state_tributacion = 'procesando'
                        else:
                            inv.state_tributacion = 'procesando'
                        inv.message_post(subject=_('Error'),
                                         body=_('Already received previously, it is passed to consult'))
                    elif inv.error_count > 10:
                        inv.message_post(subject=_('Error'), body=response_text)
                        inv.electronic_invoice_return_message = response_text
                        inv.state_tributacion = 'error'
                        _logger.error(_(f'E-INV CR  - Invoice: {inv.number_electronic}' +
                                        'Status: {response_status} Error sending XML: {response_text}'))
                    else:
                        inv.error_count += 1
                        if inv.tipo_documento == 'FEC':
                            inv.state_tributacion = 'procesando'
                        else:
                            inv.state_tributacion = 'procesando'
                        inv.message_post(subject=_('Error'), body=response_text)
                        _logger.error(_('E-INV CR  - Invoice: %s  Status: %s Error '
                                        'sending XML: %s' % (inv.number_electronic, response_status, response_text)))
            except Exception as error:
                inv.state_tributacion = 'error'
                inv.message_post(subject=_('Error'),
                                 body=_('Warning!.\n Error in generate_and_send_invoice: ') + str(error))
                continue

    # Numeracion del diario. Ventas: los documentos electronicos se renombran
    # con su consecutivo de Hacienda (20 digitos) y los disabled continuaban
    # ese formato, chocando con consecutivos reales; los disabled llevan su
    # propia numeracion y los de 20 digitos siguen contando del lado
    # electronico. Compras: cada FEC toma su consecutivo como nombre, y la
    # siguiente factura de proveedor (MR, disabled) salia con un numero que
    # parece un consecutivo FEC sin serlo. Las compras no se numeran a partir
    # de esos nombres.
    # El patron va como parametro: Odoo 14 aplica .format() a la consulta y las
    # llaves de '{20}' la rompian.
    _HACIENDA_NAME_PATTERN = '^[0-9]{20}$'
    _DISABLED_SEQUENCE_CHAIN = "(COALESCE(tipo_documento, '') = 'disabled' AND name !~ %(cr_hacienda_name)s)"
    _PURCHASE_SEQUENCE_CHAIN = "(name !~ %(cr_hacienda_name)s)"

    def _get_cr_sequence_chain(self):
        if self.move_type in ('in_invoice', 'in_refund'):
            return self._PURCHASE_SEQUENCE_CHAIN
        if self.move_type in ('out_invoice', 'out_refund'):
            if self.tipo_documento == 'disabled':
                return self._DISABLED_SEQUENCE_CHAIN
            return 'NOT %s' % self._DISABLED_SEQUENCE_CHAIN
        return False

    def _get_last_sequence_domain(self, relaxed=False):
        chain = self._get_cr_sequence_chain()
        if not chain or not self.date or not self.journal_id:
            return super()._get_last_sequence_domain(relaxed)

        # Mismo filtro que Odoo (diario, rectificativas) pero sin fechas ni
        # formato, que se deducen abajo mirando solo esta cadena: el ultimo
        # documento del diario puede ser de la otra.
        where_string, param = super()._get_last_sequence_domain(relaxed=True)
        where_string += " AND %s " % chain
        param['cr_hacienda_name'] = self._HACIENDA_NAME_PATTERN
        if relaxed:
            return where_string, param

        self.flush(['name', 'date', 'journal_id', 'move_type', 'tipo_documento'])
        reference_move_name = False
        for date_clause, order in (("AND date <= %(ref_date)s", "date DESC, id DESC"), ("", "date ASC, id ASC")):
            self.env.cr.execute(
                f"SELECT name FROM account_move {where_string} AND name IS NOT NULL AND name != '' "
                f"AND id != %(ref_id)s {date_clause} ORDER BY {order} LIMIT 1",
                dict(param, ref_id=self._origin.id or 0, ref_date=self.date),
            )
            row = self.env.cr.fetchone()
            if row:
                reference_move_name = row[0]
                break
        sequence_number_reset = self._deduce_sequence_number_reset(reference_move_name)
        if sequence_number_reset == 'year':
            where_string += " AND date_trunc('year', date::timestamp without time zone) = date_trunc('year', %(date)s) "
            param['date'] = self.date
            param['anti_regex'] = re.sub(r"\?P<\w+>", "?:", self._sequence_monthly_regex.split('(?P<seq>')[0]) + '$'
        elif sequence_number_reset == 'month':
            where_string += " AND date_trunc('month', date::timestamp without time zone) = date_trunc('month', %(date)s) "
            param['date'] = self.date
        else:
            param['anti_regex'] = re.sub(r"\?P<\w+>", "?:", self._sequence_yearly_regex.split('(?P<seq>')[0]) + '$'
        if param.get('anti_regex') and not self.journal_id.sequence_override_regex:
            where_string += " AND sequence_prefix !~ %(anti_regex)s "
        return where_string, param

    # Devuelve la secuencia (ir.sequence), no el numero: el consecutivo se pide
    # en action_post despues de todas las validaciones. Pedirlo aqui lo gastaba
    # aunque la factura no se pudiera confirmar (la secuencia no se deshace).
    def get_invoice_sequence(self):
        tipo_documento = self.tipo_documento
        sequence = False

        if self.move_type == 'out_invoice':
            # tipo de identificación
            if self.partner_id and self.partner_id.vat and not self.partner_id.identification_id:
                raise UserError(_('Select the type of client identification in your profile'))

            if self.tipo_documento == 'FE' and \
                    (not self.partner_id.vat or self.partner_id.identification_id.code == '05'):
                tipo_documento = 'TE'
            if tipo_documento == 'FE':
                sequence = self.journal_id.FE_sequence_id
            elif tipo_documento == 'TE':
                sequence = self.journal_id.TE_sequence_id
            elif tipo_documento == 'FEE':
                sequence = self.journal_id.FEE_sequence_id
            elif tipo_documento == 'ND':
                sequence = self.journal_id.ND_sequence_id

        # Credit Note
        elif self.move_type == 'out_refund':
            tipo_documento = 'NC'
            sequence = self.journal_id.NC_sequence_id

        # Digital Supplier Invoice
        elif self.move_type == 'in_invoice' and self.partner_id.country_id and \
                self.partner_id.country_id.code == 'CR' and self.partner_id.identification_id and \
                self.partner_id.vat and self.xml_supplier_approval is False and self.tipo_documento != 'CONT':
            tipo_documento = 'FEC'
            sequence = self.company_id.FEC_sequence_id

        return (tipo_documento, sequence)

    # ==================================================================
    # Validaciones previas a confirmar (portado de v19, quicknet)
    # ==================================================================
    #
    # Antes de esta noche, un dato incompleto (CABYS faltante, correo del
    # cliente mal formado, direccion incompleta) se descubria hasta que
    # Hacienda rechazaba el envio -o peor, se quedaba en un estado raro a
    # medias-. Esto lo corta ANTES de confirmar, con un mensaje que dice
    # exactamente que falta, en vez de dejar que reviente mas adelante en el
    # armado del XML o en la respuesta de Hacienda.

    def _has_electronic_invoice_configuration(self):
        """La compania tiene todo lo necesario para facturar electronicamente."""
        self.ensure_one()
        company = self.company_id
        return bool(
            company.frm_ws_ambiente != 'disabled'
            and company.identification_id
            and company.frm_ws_identificador
            and company.frm_ws_password
            and company.signature
            and company.frm_pin
        )

    def _validate_electronic_invoice_issuer_before_post(self):
        """El emisor (nuestra compania) tiene los datos que Hacienda exige."""
        email_regex = r'^(\s?[^\s,]+@[^\s,]+\.[^\s,]+\s?,)*(\s?[^\s,]+@[^\s,]+\.[^\s,]+)$'
        for inv in self:
            if inv.tipo_documento == 'disabled' or not inv._has_electronic_invoice_configuration():
                continue
            if inv.move_type not in ('out_invoice', 'out_refund'):
                continue

            errors = []
            company = inv.company_id
            if not company:
                errors.append(_('Seleccione una compania emisora.'))
            else:
                if not company.identification_id:
                    errors.append(_('Emisor "%s": seleccione el tipo de identificacion.') % company.display_name)
                if not company.vat:
                    errors.append(_('Emisor "%s": ingrese la identificacion.') % company.display_name)
                if not company.email:
                    errors.append(_('Emisor "%s": ingrese el correo electronico.') % company.display_name)
                elif not re.match(email_regex, company.email.lower()):
                    errors.append(_('Emisor "%s": el correo electronico no cumple con una '
                                    'estructura valida.') % company.display_name)

                # barrio y otras senas se dejan fuera de lo obligatorio: en
                # api_facturae.py el XML real los trata como opcionales (Barrio
                # solo se agrega si neighborhood_id.code existe; OtrasSenas cae a
                # 'NA' si street esta vacio). Esta empresa lleva meses facturando
                # sin barrio configurado y Hacienda lo acepta -exigirlo aqui
                # bloquearia todas las facturas sin agregar nada real.
                missing_address = []
                address_fields = [
                    ('country_id', _('pais')),
                    ('state_id', _('provincia')),
                    ('county_id', _('canton')),
                    ('district_id', _('distrito')),
                ]
                for field_name, label in address_fields:
                    if field_name in company._fields and not company[field_name]:
                        missing_address.append(label)
                if missing_address:
                    errors.append(_('Emisor "%s": complete la direccion (%s).') %
                                  (company.display_name, ', '.join(missing_address)))

            if errors:
                raise UserError(
                    _('No se puede confirmar el documento electronico. Corrija lo '
                      'siguiente:\n- %s') % '\n- '.join(errors)
                )

    def _validate_out_invoice_cabys_before_post(self):
        """El cliente y cada linea traen lo que una FE domestica necesita.

        Solo se llama para tipo_documento == 'FE' (ver action_post): una FEE
        de exportacion no tiene por que traer provincia/canton/distrito/
        barrio de un cliente en el extranjero -el generador de XML ya omite
        esa seccion (<Ubicacion>) para FEE-, asi que exigirla aqui tumbaria
        cualquier factura de exportacion. No se toca esa condicion.
        """
        for inv in self:
            if inv.move_type != 'out_invoice' or inv.tipo_documento == 'disabled':
                continue

            errors = []
            partner = inv.partner_id
            if not partner:
                errors.append(_('Seleccione un cliente.'))
            else:
                if not partner.identification_id:
                    errors.append(_('Cliente "%s": seleccione el tipo de identificacion.') %
                                  partner.display_name)
                if not partner.vat:
                    errors.append(_('Cliente "%s": ingrese la identificacion.') % partner.display_name)
                if not partner.email:
                    errors.append(_('Cliente "%s": ingrese el correo electronico.') % partner.display_name)
                elif not re.match(r'^(\s?[^\s,]+@[^\s,]+\.[^\s,]+\s?,)*(\s?[^\s,]+@[^\s,]+\.[^\s,]+)$',
                                  partner.email.lower()):
                    errors.append(_('Cliente "%s": el correo electronico no cumple con una '
                                    'estructura valida.') % partner.display_name)

                # No se exige direccion del cliente: en api_facturae.py el bloque
                # <Ubicacion> del receptor es todo-o-nada (solo se agrega si
                # state_id, county_id, district_id y neighborhood_id estan TODOS
                # presentes; si falta cualquiera, se omite el bloque completo sin
                # error). Hay clientes reales, con facturas aceptadas desde hace
                # meses, sin esos datos -exigirlos aqui bloquearia facturas que
                # hoy funcionan bien.

            for line in inv.invoice_line_ids:
                if line.display_type in ('line_section', 'line_note'):
                    continue
                product = line.product_id
                cabys_code = ''
                if product:
                    cabys_code = (product.cabys_code or product.categ_id.cabys_code or '').strip()
                if not cabys_code:
                    line_name = product.display_name if product else (line.name or line.display_name)
                    errors.append(_('Linea "%s": seleccione un producto con codigo CAByS o '
                                    'configure el CAByS en su categoria.') % line_name)
                if not line.tax_ids:
                    line_name = product.display_name if product else (line.name or line.display_name)
                    errors.append(_('Linea "%s": seleccione al menos un impuesto.') % line_name)
                # Hacienda rechaza con -150 si el precio unitario de una linea es cero.
                if line.price_unit <= 0:
                    line_name = product.display_name if product else (line.name or line.display_name)
                    errors.append(_('Linea "%s": el precio unitario debe ser mayor a cero.')
                                  % line_name)

            if errors:
                raise UserError(
                    _('No se puede confirmar la factura electronica. Corrija lo siguiente:\n- %s') %
                    '\n- '.join(errors)
                )

    def _get_fec_blocking_reason(self):
        """Explica por que no se puede generar o enviar una FEC ('' si se puede)."""
        self.ensure_one()
        partner = self.partner_id
        faltan = []
        if not partner.country_id or partner.country_id.code != 'CR':
            faltan.append(_('el pais del proveedor debe ser Costa Rica'))
        if not partner.identification_id:
            faltan.append(_('el proveedor no tiene tipo de identificacion'))
        if not partner.vat:
            faltan.append(_('el proveedor no tiene cedula'))
        if not (partner.state_id and partner.county_id and partner.district_id):
            faltan.append(_('el proveedor no tiene provincia, canton y distrito'))
        # OtrasSenas es obligatorio en el esquema y exige 5 caracteres minimo.
        if len((partner.street or '').strip()) < 5:
            faltan.append(_('las otras senas del proveedor deben tener al menos 5 caracteres'))
        if len((self.company_id.street or '').strip()) < 5:
            faltan.append(_('las otras senas de la compania deben tener al menos 5 caracteres'))
        if not (self.economic_activity_id or partner.activity_id):
            faltan.append(_('el proveedor no tiene actividad economica'))
        if not self.company_id.FEC_sequence_id:
            faltan.append(_('la compania no tiene secuencia de FEC configurada'))
        if self.xml_supplier_approval:
            faltan.append(_('la factura tiene XML de proveedor: corresponde mensaje '
                            'receptor, no factura electronica de compra'))
        # Hacienda rechaza con -150 si el precio unitario de una linea es cero.
        if any(line.price_unit <= 0 for line in self.invoice_line_ids if not line.display_type):
            faltan.append(_('hay lineas con precio unitario en cero'))
        if not faltan:
            return ''
        return _('No se puede generar la Factura Electronica de Compra: %s.') % '; '.join(faltan)

    def _set_electronic_invoice_error(self, message, post_message=True):
        for inv in self:
            inv.state_tributacion = 'error'
            inv.electronic_invoice_return_message = message
            if post_message:
                inv.message_post(subject=_('Error'), body=message)

    def _validate_fec_before_post(self):
        """En una FEC el Emisor del XML es el proveedor: sus datos son obligatorios.

        Portado de v19 (ahi se llama antes de action_post para dar un error
        temprano y claro, en vez de dejar que la factura falle mas adelante
        con un mensaje generico). No se agregan aqui exigencias nuevas que
        v14 no tuviera ya en otro punto (economic_activity_id, FEC_sequence_id):
        esta funcion solo adelanta esas mismas validaciones con mas detalle.
        """
        for inv in self:
            if inv.move_type != 'in_invoice' or inv.tipo_documento != 'FEC':
                continue

            errors = []
            partner = inv.partner_id
            if not partner:
                errors.append(_('Seleccione un proveedor.'))
            else:
                if not partner.identification_id:
                    errors.append(_('Proveedor "%s": seleccione el tipo de identificacion.')
                                  % partner.display_name)
                if not partner.vat:
                    errors.append(_('Proveedor "%s": ingrese la identificacion.') % partner.display_name)
                if not partner.country_id:
                    errors.append(_('Proveedor "%s": seleccione el pais.') % partner.display_name)
                elif partner.country_id.code != 'CR':
                    errors.append(_('Proveedor "%s": la factura electronica de compra solo aplica '
                                    'a proveedores de Costa Rica.') % partner.display_name)

                missing_address = []
                for field_name, label in (('state_id', _('provincia')),
                                          ('county_id', _('canton')),
                                          ('district_id', _('distrito'))):
                    if not partner[field_name]:
                        missing_address.append(label)
                if missing_address:
                    errors.append(_('Proveedor "%s": complete la direccion (%s).')
                                  % (partner.display_name, ', '.join(missing_address)))
                # OtrasSenas es obligatorio y exige 5 caracteres minimo.
                if len((partner.street or '').strip()) < 5:
                    errors.append(_('Proveedor "%s": las otras senas de la direccion deben '
                                    'tener al menos 5 caracteres.') % partner.display_name)

                if not (inv.economic_activity_id or partner.activity_id):
                    errors.append(_('Proveedor "%s": seleccione la actividad economica.')
                                  % partner.display_name)

            if len((inv.company_id.street or '').strip()) < 5:
                errors.append(_('Compania "%s": las otras senas de la direccion deben tener '
                                'al menos 5 caracteres.') % inv.company_id.display_name)
            if not inv.company_id.FEC_sequence_id:
                errors.append(_('Compania "%s": configure la secuencia de factura electronica '
                                'de compra.') % inv.company_id.display_name)

            if inv.xml_supplier_approval:
                errors.append(_('Esta factura tiene XML de proveedor: corresponde un mensaje '
                                'receptor, no una factura electronica de compra.'))

            for line in inv.invoice_line_ids:
                if line.display_type in ('line_section', 'line_note'):
                    continue
                product = line.product_id
                cabys_code = (product.cabys_code or product.categ_id.cabys_code or '').strip() if product else ''
                line_name = product.display_name if product else (line.name or line.display_name)
                if not cabys_code:
                    errors.append(_('Linea "%s": seleccione un producto con codigo CAByS o '
                                    'configure el CAByS en su categoria.') % line_name)
                # Hacienda rechaza con -150 si el precio unitario de una linea es cero.
                if line.price_unit <= 0:
                    errors.append(_('Linea "%s": el precio unitario debe ser mayor a cero.')
                                  % line_name)

            if errors:
                raise UserError(
                    _('No se puede confirmar la factura electronica de compra. Corrija lo '
                      'siguiente:\n- %s\n\nSi esta factura no debe emitirse como FEC, '
                      'cambie el tipo de documento.') % '\n- '.join(errors)
                )

    # Anexos v4.4: con estos tipos de documento de referencia Hacienda verifica
    # que el numero sea una clave valida (50 digitos).
    KEY_REFERENCE_DOCUMENT_CODES = ('01', '02', '03', '04', '09', '11', '12', '15')

    def _get_not_loaded_invoice_key(self):
        self.ensure_one()
        digits = re.sub(r'[^0-9]', '', self.not_loaded_invoice or '')
        return digits if len(digits) == 50 else (self.not_loaded_invoice or '').strip()

    # Una NC o ND lleva obligatoriamente la referencia al comprobante original.
    # Si la factura de referencia no tiene clave en Odoo (la emitio el sistema
    # anterior o esta como disabled), hay que escribir la clave y la fecha de
    # emision originales. Se valida antes de pedir el consecutivo.
    def _validate_reference_before_post(self):
        for inv in self:
            es_nota = (inv.move_type == 'out_refund' and inv.tipo_documento != 'disabled') or \
                (inv.move_type == 'out_invoice' and inv.tipo_documento == 'ND')
            if not es_nota:
                continue
            if inv.invoice_id.number_electronic:
                continue
            origen = inv.invoice_id.display_name or _('el comprobante original')
            if not (inv.not_loaded_invoice and inv.not_loaded_invoice_date):
                raise UserError(_(
                    'No se puede confirmar %(doc)s: %(origen)s no tiene clave de Hacienda en el sistema. '
                    'En la pestana Informacion Electronica escriba la clave y la fecha de emision del '
                    'comprobante original (emitido por el sistema anterior). Si el documento no es '
                    'electronico, cambie el tipo a Electronic Documents Disabled.'
                ) % {'doc': inv.display_name, 'origen': origen})
            if inv.reference_document_id.code in self.KEY_REFERENCE_DOCUMENT_CODES and \
                    len(inv._get_not_loaded_invoice_key()) != 50:
                raise UserError(_(
                    'No se puede confirmar %(doc)s: la clave del comprobante original debe tener 50 digitos '
                    '(tiene "%(clave)s").'
                ) % {'doc': inv.display_name, 'clave': inv.not_loaded_invoice})

    def _requires_tariff_head(self):
        """FEE y las NC/ND que la modifican llevan partida arancelaria."""
        self.ensure_one()
        if self.tipo_documento == 'FEE':
            return True
        return self.tipo_documento in ('NC', 'ND') and self.invoice_id.tipo_documento == 'FEE'

    # Anexos v4.4: la partida es obligatoria, con 12 digitos, en la FEE (y sus
    # NC/ND) cuando el CAByS es de un bien (empieza con 0 a 4). Se valida antes
    # de pedir el consecutivo para no enviar un comprobante que Hacienda rechaza.
    def _validate_tariff_head_before_post(self):
        for inv in self:
            if inv.tipo_documento == 'disabled' or not inv._requires_tariff_head():
                continue
            errors = []
            for line in inv.invoice_line_ids.filtered(lambda l: not l.display_type and l.product_id):
                cabys = line.product_id.cabys_code or line.product_id.categ_id.cabys_code or ''
                if not cabys or cabys[0] not in '01234':
                    continue
                if len(re.sub(r'[^0-9]', '', line.tariff_head or '')) != 12:
                    errors.append(_('Linea "%(line)s": la partida arancelaria debe tener 12 digitos (tiene "%(value)s").') % {
                        'line': line.name or line.product_id.display_name, 'value': line.tariff_head or ''})
            if errors:
                raise UserError(_(
                    'No se puede confirmar %(doc)s: Hacienda exige la partida arancelaria de 12 digitos en las '
                    'mercancias de una factura de exportacion.\n- %(errors)s'
                ) % {'doc': inv.display_name, 'errors': '\n- '.join(errors)})

    # La FEC es un comprobante que emitimos: como la FE, se llama con su
    # consecutivo oficial. Las demas compras (MR, disabled) usan la numeracion
    # del diario. Si otro documento del diario ya tiene ese nombre, la FEC
    # conserva el del diario: renombrarla chocaria y no se podria confirmar.
    def _set_fec_name_from_consecutive(self):
        for move in self:
            if move.move_type != 'in_invoice' or move.tipo_documento != 'FEC' or move.state != 'posted' \
                    or not move.sequence or move.name == move.sequence:
                continue
            taken = self.sudo().search_count([
                ('journal_id', '=', move.journal_id.id), ('name', '=', move.sequence),
                ('state', '=', 'posted'), ('id', '!=', move.id),
            ])
            if taken:
                _logger.warning('E-INV CR - FEC %s: el consecutivo %s ya es el nombre de otro documento',
                                move.id, move.sequence)
                continue
            move.name = move.sequence

    def _set_payment_reference(self, reference):
        """Referencia de pago y nombre de la linea por cobrar, que la copia."""
        for move in self:
            old_reference = move.payment_reference
            move.payment_reference = reference
            move.line_ids.filtered(
                lambda l: l.account_id.internal_type in ('receivable', 'payable') and l.name == old_reference
            ).write({'name': reference})

    def _avoid_hacienda_like_name(self):
        """Evita que un comprobante no-electronico herede un numero con el
        mismo formato de 20 digitos que usa el consecutivo real de Hacienda.

        Un comprobante 'disabled' no llama a get_invoice_sequence(): su
        'name' lo pone el mecanismo generico de numeracion de Odoo (el
        ultimo 'name' existente en el diario, ver _get_last_sequence del
        core). Si los ultimos comprobantes del mismo diario fueron
        electronicos, su 'name' se sobreescribio con el consecutivo real
        de Hacienda (ver generate_and_send_invoice: 'inv.name = inv.sequence'),
        asi que el siguiente comprobante no-electronico hereda ese mismo
        formato de 20 digitos por pura coincidencia de numeracion -nunca
        se envio a Hacienda, pero el numero es indistinguible a simple
        vista de uno que si se envio, y si mas adelante un documento
        electronico real cae en ese mismo numero, terminaria con el mismo
        'name' que este, duplicado dentro del mismo diario. Se le antepone
        un prefijo para que nunca choque ni se confunda con un consecutivo
        real.
        """
        self.ensure_one()
        if self.name and re.match(r'^\d{20}$', self.name):
            self.name = 'NE-%s' % self.name

    def action_post(self):
        # Revisamos si el ambiente para Hacienda está habilitado
        for inv in self:
            narration_save = inv.narration
            if inv.company_id.frm_ws_ambiente == 'disabled' or inv.tipo_documento == 'disabled':
                # Antes de confirmar: el nombre se toma de la numeracion disabled.
                if inv.tipo_documento != 'disabled':
                    inv.tipo_documento = 'disabled'
                super(AccountInvoiceElectronic, inv).action_post()
                inv._avoid_hacienda_like_name()
                continue

            if inv._has_electronic_invoice_configuration():
                inv._validate_electronic_invoice_issuer_before_post()
                if inv.move_type == 'out_invoice' and inv.tipo_documento == 'FE':
                    inv._validate_out_invoice_cabys_before_post()
                # La factura de proveedor llega como FE y se vuelve FEC en
                # get_invoice_sequence: se valida con el tipo que va a quedar.
                if inv.move_type == 'in_invoice':
                    (tipo_compra, secuencia_compra) = inv.get_invoice_sequence()
                    if tipo_compra == 'FEC' and secuencia_compra:
                        inv.tipo_documento = 'FEC'
                        inv._validate_fec_before_post()
                inv._validate_tariff_head_before_post()
                inv._validate_reference_before_post()

            # self._onchange_partner_id(validate_payment=False)

            if inv.partner_id.has_exoneration:
                if inv.partner_id.date_expiration and (inv.partner_id.date_expiration > datetime.date.today()):
                    raise UserError(_('The exoneration of this client has expired'))

            currency = inv.currency_id
            sequence = False
            if (inv.invoice_id) and not (inv.reference_code_id and inv.reference_document_id):
                raise UserError(_('Incomplete reference data for credit note'))
            elif (inv.not_loaded_invoice or inv.not_loaded_invoice_date) and not \
                    (inv.not_loaded_invoice and inv.not_loaded_invoice_date and
                     inv.reference_code_id and inv.reference_document_id):
                raise UserError(_('Incomplete reference data for credit note not uploaded'))

            if inv.move_type == 'in_invoice' and inv.partner_id.country_id and \
                    inv.partner_id.country_id.code == 'CR' and inv.partner_id.identification_id and \
                    inv.partner_id.vat and inv.economic_activity_id is False:
                raise UserError(_('FEC invoices require that the supplier has defined the economic activity'))
            # tipo de identificación
            if not inv.company_id.identification_id:
                raise UserError(_('Select the type of issuer identification in the company profile'))

            if inv.partner_id and inv.partner_id.vat:
                identificacion = re.sub('[^0-9]', '', inv.partner_id.vat)
                id_code = inv.partner_id.identification_id and inv.partner_id.identification_id.code
                if not id_code:
                    if len(identificacion) == 9:
                        id_code = '01'
                    elif len(identificacion) == 10:
                        id_code = '02'
                    elif len(identificacion) in (11, 12):
                        id_code = '03'
                    else:
                        id_code = '05'

                if id_code == '01' and len(identificacion) != 9:
                    raise UserError(_("The recipient's Physical ID must have 9 digits"))
                elif id_code == '02' and len(identificacion) != 10:
                    raise UserError(_('The Legal ID of the recipient must have 10 digits'))
                elif id_code == '03' and len(identificacion) not in (11, 12):
                    raise UserError(_("The recipient's DIMEX identification must have 11 or 12 digits"))
                elif id_code == '04' and len(identificacion) != 10:
                    raise UserError(_('The NITE identification of the receiver must have 10 digits'))

            if inv.invoice_payment_term_id and not inv.invoice_payment_term_id.sale_conditions_id:
                raise UserError(_('The electronic invoice could not be created: \n'
                                  'You must set up payment terms for %s') % (inv.invoice_payment_term_id.name))

            # Validate if invoice currency is the same as the company currency
            if currency.name != inv.company_id.currency_id.name and (not currency.rate_ids or not
            (len(currency.rate_ids) > 0)):
                raise UserError(_(f'There is no registered exchange rate for the currency {currency.name}'))

            # Digital Invoice or ticket
            sequence_record = False
            if inv.move_type in ('out_invoice', 'out_refund') and inv.number_electronic:
                pass
            else:
                (tipo_documento, sequence_record) = inv.get_invoice_sequence()
                if tipo_documento and sequence_record:
                    inv.tipo_documento = tipo_documento
                else:
                    super(AccountInvoiceElectronic, inv).action_post()
                    continue

            # Calcular si aplica IVA Devuelto
            # Sólo aplica para clínicas y para pago por tarjeta
            actividad_iva_devuelto = 'CLINICA, CENTROS MEDICOS, HOSPITALES PRIVADOS Y OTROS'
            if inv.economic_activity_id.name == actividad_iva_devuelto and inv.payment_methods_id.sequence == '02':
                prod_iva_devuelto = self.env.ref('cr_electronic_invoice.product_iva_devuelto')
                iva_devuelto = 0
                for inv_line in inv.invoice_line_ids:
                    if inv_line.product_id:
                        # Remove any existing IVA Devuelto lines
                        if inv_line.product_id.id == prod_iva_devuelto.id:
                            inv_line.unlink
                        elif inv_line.product_id.categ_id.name == 'Servicios de Salud':
                            iva_devuelto += inv_line.price_tax
                if iva_devuelto:
                    self.env['account.move.line'].create({
                        'name': 'IVA Devuelto',
                        'invoice_id': inv.id,
                        'product_id': prod_iva_devuelto.id,
                        'account_id': prod_iva_devuelto.property_account_income_id.id,
                        'price_unit': -iva_devuelto,
                        'quantity': 1,
                    })

            # super() sobre esta factura: sobre self confirmaba todas las del lote
            # sin consecutivo y la siguiente vuelta fallaba por ya confirmada.
            super(AccountInvoiceElectronic, inv).action_post()

            if sequence_record:
                sequence = sequence_record.next_by_id()

            if not inv.number_electronic:
                # if journal doesn't have sucursal use default from company
                sucursal_id = inv.journal_id.sucursal or self.env.user.company_id.sucursal_MR

                # if journal doesn't have terminal use default from company
                terminal_id = inv.journal_id.terminal or self.env.user.company_id.terminal_MR

                response_json = api_facturae.get_clave_hacienda(inv,
                                                                inv.tipo_documento,
                                                                sequence,
                                                                sucursal_id,
                                                                terminal_id)

                inv.number_electronic = response_json.get('clave')
                inv.sequence = response_json.get('consecutivo')

            # Odoo calcula la referencia de pago con el nombre provisional del
            # diario y no la recalcula al renombrar: quedaba otro numero (a
            # veces el de otra factura) y el pago no se conciliaba con esta. Si
            # es la automatica, se recalcula con el consecutivo.
            old_reference = inv._get_invoice_computed_reference() if inv.move_type == 'out_invoice' else False
            if inv.move_type in ('out_invoice', 'out_refund') and inv.sequence and inv.name != inv.sequence:
                inv.name = inv.sequence
            inv._set_fec_name_from_consecutive()
            if old_reference and inv.payment_reference == old_reference:
                inv._set_payment_reference(inv._get_invoice_computed_reference())
            inv.state_tributacion = False

            inv.narration = narration_save

    @api.onchange('amount_total')
    def update_text_amount(self):
        for inv in self:
            inv.invoice_amount_text = extensions.text_converter.number_to_text_es(inv.amount_total)

    def _reverse_move_vals(self, default_values, cancel=True):
        move_vals = super()._reverse_move_vals(default_values, cancel)
        type_override = move_vals.get('type_override')
        if type_override:
            move_vals['move_type'] = type_override

        return move_vals

    def _reverse_moves(self, default_values_list=None, cancel=False):
        """ Reverse a recordset of account.move.
        If cancel parameter is true, the reconcilable or liquidity lines
        of each original move will be reconciled with its reverse's.

        :param default_values_list: A list of default values to consider per move.
                                    ('type' & 'reversed_entry_id' are computed in the method).
        :return:                    An account.move recordset, reverse of the current self.
        """
        if not default_values_list:
            default_values_list = [{} for move in self]

        if cancel:
            lines = self.mapped('line_ids')
            # Avoid maximum recursion depth.
            if lines:
                lines.remove_move_reconcile()

        reverse_type_map = {
            'entry': 'entry',
            'out_invoice': 'out_refund',
            'out_refund': 'entry',
            'in_invoice': 'in_refund',
            'in_refund': 'entry',
            'out_receipt': 'entry',
            'in_receipt': 'entry',
        }

        move_vals_list = []
        for move, default_values in zip(self, default_values_list):
            # Sin el asistente (p. ej. otros modulos) la NC tambien toma su tipo:
            # disabled si la factura lo es.
            if move.move_type == 'out_invoice':
                default_values.setdefault('tipo_documento', 'disabled' if move.tipo_documento == 'disabled' else 'NC')
            default_values.update({
                'move_type': reverse_type_map[move.move_type],
                'reversed_entry_id': move.id,
            })
            move_vals_list.append(move.with_context(move_reverse_cancel=cancel)._reverse_move_vals(default_values,
                                                                                                   cancel=cancel))

        reverse_moves = self.env['account.move'].create(move_vals_list)
        for move, reverse_move in zip(self, reverse_moves.with_context(check_move_validity=False)):
            # Update amount_currency if the date has changed.
            if move.date != reverse_move.date:
                for line in reverse_move.line_ids:
                    if line.currency_id:
                        line._onchange_currency()
            reverse_move._recompute_dynamic_lines(recompute_all_taxes=False)
        reverse_moves._check_balanced()

        # Reconcile moves together to cancel the previous one.
        if cancel:
            # Used for use "Action Post" to get electronic number
            reverse_moves.with_context(move_reverse_cancel=cancel).action_post()
            for move, reverse_move in zip(self, reverse_moves):
                lines = move.line_ids.filtered(
                    lambda x: (x.account_id.reconcile or x.account_id.internal_type == 'liquidity')
                              and not x.reconciled
                )
                for line in lines:
                    counterpart_lines = reverse_move.line_ids.filtered(lambda x: x.account_id == line.account_id
                                                                                 and x.currency_id == line.currency_id
                                                                                 and not x.reconciled)
                    (line + counterpart_lines).with_context(move_reverse_cancel=cancel).reconcile()

        return reverse_moves

    def create_partner_from_xml(self):

        if not self.partner_id and self.xml_supplier_approval:
            info = {}

            invoice_xml = etree.fromstring(base64.b64decode(self.xml_supplier_approval))
            namespaces = invoice_xml.nsmap
            inv_xmlns = namespaces.pop(None)
            namespaces['inv'] = inv_xmlns

            info['vat'] = invoice_xml.xpath("inv:Emisor/inv:Identificacion/inv:Numero", namespaces=namespaces)[0].text

            partner = self.env['res.partner'].search([('vat', '=', info['vat']), ('type', '=', 'contact')], limit=1)
            if len(partner) > 0:
                self.partner_id = partner.id
            else:
                info['name'] = invoice_xml.xpath("inv:Emisor/inv:Nombre", namespaces=namespaces)[0].text
                info['phone'] = invoice_xml.xpath("inv:Emisor/inv:Telefono/inv:NumTelefono",
                                                  namespaces=namespaces)[0].text or False
                info['email'] = invoice_xml.xpath("inv:Emisor/inv:CorreoElectronico",
                                                  namespaces=namespaces)[0].text or False
                info['lang'] = 'es_CR'

                # Se agrega manualmente la información ya que no se puede obtener del XML
                info['property_payment_term_id'] = 1
                info['payment_methods_id'] = 1
                info['property_product_pricelist'] = 1
                info['property_supplier_payment_term_id'] = 1

                # País
                info['country_id'] = self.env['res.country'].search([('code', '=', 'CR')], limit=1).id

                # Provincia
                provincia = invoice_xml.xpath("inv:Emisor/inv:Ubicacion/inv:Provincia", namespaces=namespaces)[0].text
                state_id = self.env['res.country.state'].search([('code', '=', provincia)], limit=1).id
                info['state_id'] = state_id

                # Cantón
                canton = invoice_xml.xpath("inv:Emisor/inv:Ubicacion/inv:Canton", namespaces=namespaces)[0].text
                county_id = self.env['res.country.county'].search([('code', '=', canton),
                                                                   ('state_id', '=', state_id)], limit=1).id
                info['county_id'] = county_id

                # Distrito
                distrito = invoice_xml.xpath("inv:Emisor/inv:Ubicacion/inv:Distrito", namespaces=namespaces)[0].text
                district_id = self.env['res.country.district'].search([('code', '=', distrito),
                                                                       ('county_id', '=', county_id)], limit=1).id
                info['district_id'] = district_id

                actividad_economica = invoice_xml.xpath("inv:CodigoActividad", namespaces=namespaces)[0].text
                info['activity_id'] = self.env['economic.activity'].search([('code', '=', actividad_economica)],
                                                                           limit=1).id

                cliente = self.env['res.partner'].create(info)
                cliente.onchange_vat()
                self.partner_id = cliente.id

    def get_xml_document(self, invoice_id):
        tab_id = []
        invoice = self.env['account.move'].sudo().browse(invoice_id)
        domain = [('res_model', '=', invoice._name),
                  ('res_id', '=', invoice.id),
                  ('res_field', '=', 'xml_comprobante'),
                  ('name', '=', invoice.tipo_documento + '_' + invoice.number_electronic + '.xml')]
        attachment = self.env['ir.attachment'].sudo().search(domain, limit=1)
        if attachment:
            tab_id.append(attachment.id)
            domain_resp = [('res_model', '=', invoice._name),
                           ('res_id', '=', invoice.id),
                           ('res_field', '=', 'xml_respuesta_tributacion'),
                           ('name', '=', 'AHC_' + invoice.number_electronic + '.xml')]
            attachment_resp = self.env['ir.attachment'].sudo().search(domain_resp, limit=1)

            if attachment_resp:
                tab_id.append(attachment_resp.id)

            url = f'/web/binary/download_document?tab_id={tab_id}&invoice_id={invoice_id}'
            return url
        return False
