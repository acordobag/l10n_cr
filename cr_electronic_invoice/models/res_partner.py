import re
import json
import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import phonenumbers
import logging
from datetime import datetime, timedelta, date
from . import api_facturae

_logger = logging.getLogger(__name__)


class PartnerElectronic(models.Model):
    _inherit = "res.partner"

    commercial_name = fields.Char()
    identification_id = fields.Many2one("identification.type")
    payment_methods_id = fields.Many2one("payment.methods", string="Payment Method")
    has_exoneration = fields.Boolean(string="Has Exoneration?", required=False)
    type_exoneration = fields.Many2one("aut.ex", string="Authorization Type")
    exoneration_number = fields.Char()
    percentage_exoneration = fields.Float(string="Percentage of VAT Exoneration", required=False)
    institution_name = fields.Char(string="Exoneration Issuer")
    date_issue = fields.Date(string="Issue Date")
    date_expiration = fields.Date(string="Expiration Date")
    date_notification = fields.Date(string="Last notification date")
    activity_id = fields.Many2one("economic.activity",
                                  string="Default Economic Activity",
                                  context={'active_test': False})
    economic_activities_ids = fields.Many2many('economic.activity',
                                               string='Economic Activities',
                                               context={'active_test': False})
    export = fields.Boolean(string="It's export", default=False)
    oc_xml = fields.Char(string="OC XML", default="OC")
    
    @api.onchange('phone')
    def _onchange_phone(self):
        if self.phone:
            phone = phonenumbers.parse(self.phone, self.country_id and self.country_id.code or 'CR')
            valid = phonenumbers.is_valid_number(phone)
            if not valid:
                alert = {
                    'title': 'Atención',
                    'message': _('Número de teléfono inválido')
                }
                return {'value': {'phone': ''}, 'warning': alert}

    @api.onchange('mobile')
    def _onchange_mobile(self):
        if self.mobile:
            mobile = phonenumbers.parse(self.mobile, self.country_id and self.country_id.code or 'CR')
            valid = phonenumbers.is_valid_number(mobile)
            if not valid:
                alert = {
                    'title': 'Atención',
                    'message': 'Número de teléfono inválido'
                }
                return {'value': {'mobile': ''}, 'warning': alert}

    @api.onchange('email')
    def _onchange_email(self):
        if self.email:
            if not re.match(r'^(\s?[^\s,]+@[^\s,]+\.[^\s,]+\s?,)*(\s?[^\s,]+@[^\s,]+\.[^\s,]+)$', self.email.lower()):
                vals = {'email': False}
                alerta = {
                    'title': 'Atención',
                    'message': 'El correo electrónico no cumple con una estructura válida. ' + str(self.email)
                }
                return {'value': vals, 'warning': alerta}

    @api.onchange('vat')
    def _onchange_vat(self):
        if self.identification_id and self.vat:
            if self.identification_id.code == '05':
                if len(self.vat) == 0 or len(self.vat) > 20:
                    raise UserError(_('La identificación debe tener menos de 20 carateres.'))
            else:
                # Remove leters, dashes, dots or any other special character.
                self.vat = re.sub(r"[^0-9]+", "", self.vat)
                if self.identification_id.code == '01':
                    if self.vat.isdigit() and len(self.vat) != 9:
                        raise UserError(_('La identificación tipo Cédula física debe ' +
                                        'de contener 9 dígitos, sin cero al inicio y sin guiones.'))
                elif self.identification_id.code == '02':
                    if self.vat.isdigit() and len(self.vat) != 10:
                        raise UserError(_('La identificación tipo Cédula jurídica debe contener 10 ' +
                                          'dígitos, sin cero al inicio y sin guiones.'))
                elif self.identification_id.code == '03' and self.vat.isdigit():
                    if self.vat.isdigit() and len(self.vat) < 11 or len(self.vat) > 12:
                        raise UserError(_('La identificación tipo DIMEX debe contener 11 o 12 ' +
                                          'dígitos, sin ceros al inicio y sin guiones.'))
                elif self.identification_id.code == '04' and self.vat.isdigit():
                    if self.vat.isdigit() and len(self.vat) != 9:
                        raise UserError(_('La identificación tipo NITE debe contener 10 dígitos, ' +
                                          'sin ceros al inicio y sin guiones.'))

        self.action_get_economic_activities()

    def action_get_economic_activities(self):
        if not self.vat:
            return {
                'value': {'vat': ''},
                'warning': {'title': 'Atención', 'message': _('Company VAT is invalid')}
            }

        json_response = api_facturae.get_economic_activities(self)
        _logger.debug('E-INV CR - Economic Activities: %s', json_response)

        if not json_response or json_response.get("status") != 200:
            alert = {'title': json_response.get("status"), 'message': json_response.get("name")}
            return {'value': {'vat': ''}, 'warning': alert}

        activities = json_response.get("activities", []) or []

        EconomicActivity = self.env['economic.activity'].with_context(active_test=False)

        def _norm_code(v):
            # preserva decimales (e.g. "6202.0")
            return (str(v) if v is not None else '').strip()

        def _norm_ciiu3(v):
            s = (str(v) if v is not None else '').strip()
            return s.zfill(6) if s.isdigit() and len(s) < 6 else s

        # acumulamos en un recordset (union con |=)
        economic_activities = EconomicActivity.browse()
        pair_count = 0

        for item in activities:
            if item.get("estado") != "A":
                continue

            code = _norm_code(item.get("codigo"))  # CIIU4 con decimal
            c3_list = item.get("ciiu3") or []
            if not isinstance(c3_list, list):
                c3_list = [c3_list]

            found_any = False
            for c3_entry in c3_list:
                c3 = c3_entry.get("codigo") if isinstance(c3_entry, dict) else c3_entry
                c3 = _norm_ciiu3(c3)
                if not code or not c3:
                    continue
                rec = EconomicActivity.search([('code', '=', code), ('ciiu3', '=', c3)], limit=1)
                if rec:
                    economic_activities |= rec
                    pair_count += 1
                    found_any = True

            # si Hacienda no trae ciiu3, hacemos fallback solo por code
            if not found_any and code:
                rec = EconomicActivity.search([('code', '=', code)], limit=1)
                if rec:
                    economic_activities |= rec

        # Escribimos el M2M correctamente
        self.economic_activities_ids = [(6, 0, economic_activities.ids)]

        # Nombre del contribuyente devuelto por Hacienda
        if json_response.get("name"):
            self.name = json_response["name"]

        # Settear activity_id con el primero, si existe
        if economic_activities:
            self.activity_id = economic_activities[:1].id  # recordset slice ok
        else:
            # sin resultados: warning opcional
            _logger.info("E-INV CR - No se encontraron actividades económicas activas en la base local para %s",
                         self.vat)

    @api.onchange('exoneration_number')
    def _onchange_exoneration_number(self):
        if self.exoneration_number:
            self.definir_informacion_exo(self.exoneration_number)

    def definir_informacion_exo(self, cedula):
        url_base = self.sudo().env.company.url_base_exo
        if url_base:
            url_base = url_base.strip()

            if url_base[-1:] == '/':
                url_base = url_base[:-1]

            end_point = url_base + 'autorizacion=' + cedula

            headers = {
                'content-type': 'application/json',
            }

            peticion = requests.get(end_point, headers=headers, timeout=10)

            ultimo_mensaje = 'Fecha/Hora: ' + str(datetime.now()) + ', Codigo: ' + str(
                peticion.status_code) + ', Mensaje: ' + str(peticion._content.decode())

            if peticion.status_code == 404:
                self.date_issue = False
                self.date_expiration = False
                self.percentage_exoneration = 0
                self.institution_name = False
                self.type_exoneration = False
                # raise UserError(_('El documento de exoneración no existe.'))

            if peticion.status_code in (200, 202) and len(peticion._content) > 0:
                contenido = json.loads(str(peticion._content, 'utf-8'))

                self.sudo().env.company.ultima_respuesta_exo = ultimo_mensaje

                if 'identificacion' in contenido:
                    if self.vat != contenido.get('identificacion'):
                        raise UserError(_('El código de exoneración no concuerda con la cédula del socio de negocio.'))
                    fecha_emision = datetime.strptime(str(contenido.get('fechaEmision'))[:10], '%Y-%m-%d')
                    self.date_issue = fecha_emision
                    fecha_vencimiento = datetime.strptime(str(contenido.get('fechaVencimiento'))[:10], '%Y-%m-%d')
                    self.date_expiration = fecha_vencimiento
                    self.percentage_exoneration = float(contenido.get('porcentajeExoneracion'))
                    self.institution_name = contenido.get('nombreInstitucion')

                    tipo_documento = contenido.get('tipoDocumento')

                    autorizacion = self.env['aut.ex'].sudo().search([('code',
                                                                      '=',
                                                                      tipo_documento.get('codigo')),
                                                                     ('active', '=', True)], limit=1)

                    if len(autorizacion) > 0:
                        self.type_exoneration = autorizacion.id

    def check_exonerations(self):
        clients = self.env["res.partner"].search([("has_exoneration", "=", True),
                                                  ("date_expiration", "<", datetime.today())])
        for client in clients:
            if client.date_notification == False or (client.date_notification + timedelta(days=8)) < date.today():
                email_template = client.env.ref("cr_electronic_invoice.email_template_client_exoneration_expired")
                if email_template:
                    email_template.send_mail(client.id)
                    client.date_notification = date.today()
