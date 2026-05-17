
from xml.etree import ElementTree as ET
import requests
import datetime
import json
from . import fe_enums
import io
import re
import os
import base64
import logging
import pytz
import time
import phonenumbers
import random
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from decimal import Decimal, ROUND_HALF_UP
from odoo import _
from odoo.exceptions import UserError
from xml.sax.saxutils import escape
from ..xades.context2 import XAdESContext2, PolicyId2, create_xades_epes_signature

from lxml import etree

try:
    from OpenSSL import crypto
except(ImportError, IOError) as err:
    logging.info(err)

# PARA VALIDAR JSON DE RESPUESTA
# from .. import extensions

_logger = logging.getLogger(__name__)


def sign_xml(cert, password, xml, policy_id='https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/'
             '2016/v4.2/ResolucionComprobantesElectronicosDGT-R-48-2016_4.2.pdf'):
    root = etree.fromstring(xml)
    signature = create_xades_epes_signature()

    policy = PolicyId2()
    policy.id = policy_id

    root.append(signature)
    ctx = XAdESContext2(policy)
    certificate = crypto.load_pkcs12(base64.b64decode(cert), password)
    ctx.load_pkcs12(certificate)
    ctx.sign(signature)

    return etree.tostring(root, encoding='UTF-8', method='xml', xml_declaration=True, with_tail=False)


def get_time_hacienda():
    now_utc = datetime.datetime.now(pytz.timezone('UTC'))
    now_cr = now_utc.astimezone(pytz.timezone('America/Costa_Rica'))
    date_cr = now_cr.strftime("%Y-%m-%dT%H:%M:%S-06:00")
    return date_cr


# Utilizada para establecer un limite de caracteres en la cedula del cliente, no mas de 20
# de lo contrario hacienda lo rechaza
def limit(texto, limit):
    return (texto[:limit - 3] + '...') if len(texto) > limit else texto


def get_mr_sequencevalue(inv):
    # Verificamos si el ID del mensaje receptor es válido
    mr_mensaje_id = int(inv.state_invoice_partner)
    if mr_mensaje_id is None:
        raise UserError(_('No se ha proporcionado un ID válido para el MR.'))
    if mr_mensaje_id < 1 or mr_mensaje_id > 3:
        raise UserError(_('El ID del mensaje receptor es inválido.'))

    if inv.state_invoice_partner == '1':
        detalle_mensaje = 'Aceptado'
        tipo = 1
        tipo_documento = fe_enums.TipoDocumento['CCE']
        sequence = inv.env['ir.sequence'].next_by_code('sequence.electronic.doc.confirmation')
    elif inv.state_invoice_partner == '2':
        detalle_mensaje = 'Aceptado parcial'
        tipo = 2
        tipo_documento = fe_enums.TipoDocumento['CPCE']
        sequence = inv.env['ir.sequence'].next_by_code('sequence.electronic.doc.partial.confirmation')
    else:
        detalle_mensaje = 'Rechazado'
        tipo = 3
        tipo_documento = fe_enums.TipoDocumento['RCE']
        sequence = inv.env['ir.sequence'].next_by_code('sequence.electronic.doc.reject')

    return {'detalle_mensaje': detalle_mensaje, 'tipo': tipo, 'tipo_documento': tipo_documento, 'sequence': sequence}


def get_consecutivo_hacienda(tipo_documento, consecutivo, sucursal_id, terminal_id):
    tipo_doc = fe_enums.TipoDocumento[tipo_documento]
    inv_consecutivo = str(consecutivo).zfill(10)
    inv_sucursal = str(sucursal_id).zfill(3)
    inv_terminal = str(terminal_id).zfill(5)
    consecutivo_mh = inv_sucursal + inv_terminal + tipo_doc + inv_consecutivo
    return consecutivo_mh


def get_clave_hacienda(doc, tipo_documento, consecutivo, sucursal_id, terminal_id, situacion='normal'):
    tipo_doc = fe_enums.TipoDocumento[tipo_documento]

    # Verificamos si el consecutivo indicado corresponde a un numero
    inv_consecutivo = re.sub('[^0-9]', '', consecutivo)
    if len(inv_consecutivo) != 10:
        raise UserError(_('La numeración debe de tener 10 dígitos'))

    # Verificamos la sucursal y terminal
    inv_sucursal = re.sub('[^0-9]', '', str(sucursal_id)).zfill(3)
    inv_terminal = re.sub('[^0-9]', '', str(terminal_id)).zfill(5)

    # Armamos el consecutivo pues ya tenemos los datos necesarios
    consecutivo_mh = inv_sucursal + inv_terminal + tipo_doc + inv_consecutivo

    if not doc.company_id.identification_id:
        raise UserError(_('Seleccione el tipo de identificación del emisor en el pérfil de la compañía'))

    # Obtenemos el número de identificación del Emisor y lo validamos númericamente
    inv_cedula = re.sub('[^0-9]', '', doc.company_id.vat)

    # Validamos el largo de la cadena númerica de la cédula del emisor
    if doc.company_id.identification_id.code == '01' and len(inv_cedula) != 9:
        raise UserError(_('La Cédula Física del emisor debe de tener 9 dígitos'))
    elif doc.company_id.identification_id.code == '02' and len(inv_cedula) != 10:
        raise UserError(_('La Cédula Jurídica del emisor debe de tener 10 dígitos'))
    elif doc.company_id.identification_id.code == '03' and len(inv_cedula) not in (11, 12):
        raise UserError(_('La identificación DIMEX del emisor debe de tener 11 o 12 dígitos'))
    elif doc.company_id.identification_id.code == '04' and len(inv_cedula) != 10:
        raise UserError(_('La identificación NITE del emisor debe de tener 10 dígitos'))

    inv_cedula = str(inv_cedula).zfill(12)

    # Limitamos la cedula del emisor a 20 caracteres o nos dará error
    cedula_emisor = limit(inv_cedula, 20)

    # Validamos la situación del comprobante electrónico
    situacion_comprobante = fe_enums.SituacionComprobante.get(situacion)
    if not situacion_comprobante:
        raise UserError(_(f'La situación indicada para el comprobante electrónico es inválida: {situacion}'))

    # Creamos la fecha para la clave
    dia = str(doc.invoice_date.day).zfill(2)
    mes = str(doc.invoice_date.month).zfill(2)
    anno = str(doc.invoice_date.year)[2:]
    cur_date = dia + mes + anno

    phone = phonenumbers.parse(doc.company_id.phone, doc.company_id.country_id and doc.company_id.country_id.code or 'CR')
    codigo_pais = str(phone and phone.country_code or 506)

    # Creamos un código de seguridad random
    codigo_seguridad = str(random.randint(1, 99999999)).zfill(8)

    clave_hacienda = codigo_pais + cur_date + cedula_emisor + consecutivo_mh + situacion_comprobante + codigo_seguridad
    return {'length': len(clave_hacienda), 'clave': clave_hacienda, 'consecutivo': consecutivo_mh}


# Variables para poder manejar el Refrescar del Token
last_tokens = {}
last_tokens_time = {}
last_tokens_expire = {}
last_tokens_refresh = {}


def get_token_hacienda(inv, tipo_ambiente):
    global last_tokens, last_tokens_time, last_tokens_expire, last_tokens_refresh

    token = last_tokens.get(inv.company_id.id, False)
    token_time = last_tokens_time.get(inv.company_id.id, False)
    token_expire = last_tokens_expire.get(inv.company_id.id, 0)
    current_time = time.time()

    if token and (current_time - token_time < token_expire - 10):
        token_hacienda = token
    else:
        headers = {}
        data = {'client_id': tipo_ambiente,
                'client_secret': '',
                'grant_type': 'password',
                'username': inv.company_id.frm_ws_identificador,
                'password': inv.company_id.frm_ws_password}

        _logger.info('test %s' % (data))

        # establecer el ambiente al cual me voy a conectar
        endpoint = fe_enums.UrlHaciendaToken[tipo_ambiente]

        try:
            response = requests.request("POST", endpoint, data=data, headers=headers)
            response_json = response.json()
            if 200 <= response.status_code <= 299:
                token_hacienda = response_json.get('access_token')
                last_tokens[inv.company_id.id] = token_hacienda  # fix: store actual token
                last_tokens_time[inv.company_id.id] = time.time()
                last_tokens_expire[inv.company_id.id] = response_json.get('expires_in')
                last_tokens_refresh[inv.company_id.id] = response_json.get('refresh_expires_in')
            else:
                _logger.error('FECR - token_hacienda failed.  error: %s' % (response.status_code))
        except requests.exceptions.RequestException as e:
            raise Warning(_('Error Obteniendo el Token desde MH. Excepcion %s'), (e))

    return token_hacienda


def refresh_token_hacienda(tipo_ambiente, token):
    headers = {}
    data = {'client_id': tipo_ambiente,
            'client_secret': '',
            'grant_type': 'refresh_token',
            'refresh_token': token}

    endpoint = fe_enums.UrlHaciendaToken[tipo_ambiente]

    try:
        response = requests.request("POST", endpoint, data=data, headers=headers)
        response_json = response.json()
        token_hacienda = response_json.get('access_token')
        return token_hacienda
    except ImportError:
        raise Warning(_('Error Refrescando el Token desde MH'))


def gen_xml_mr_43(clave, cedula_emisor, fecha_emision, id_mensaje,
                  detalle_mensaje, cedula_receptor,
                  consecutivo_receptor,
                  monto_impuesto=0, total_factura=0,
                  codigo_actividad=False,
                  condicion_impuesto=False,
                  monto_total_impuesto_acreditar=False,
                  monto_total_gasto_aplicable=False):
    # Validaciones de datos del MR
    if clave:
        mr_clave = re.sub('[^0-9]', '', clave)
    else:
        mr_clave = False
    if len(mr_clave) != 50:
        raise UserError(_('La clave a utilizar es inválida. Debe contener al menos 50 digitos'))

    mr_cedula_emisor = re.sub('[^0-9]', '', cedula_emisor)
    if len(mr_cedula_emisor) != 12:
        mr_cedula_emisor = str(mr_cedula_emisor).zfill(12)
    elif mr_cedula_emisor is None:
        raise UserError(_('La cédula del Emisor en el MR es inválida.'))

    if fecha_emision is None:
        raise UserError(_('La fecha de emisión en el MR es inválida.'))

    mr_mensaje_id = int(id_mensaje)
    if mr_mensaje_id < 1 and mr_mensaje_id > 3:
        raise UserError(_('El ID del mensaje receptor es inválido.'))
    elif mr_mensaje_id is None:
        raise UserError(_('No se ha proporcionado un ID válido para el MR.'))

    mr_cedula_receptor = re.sub('[^0-9]', '', cedula_receptor)
    if len(mr_cedula_receptor) != 12:
        mr_cedula_receptor = str(mr_cedula_receptor).zfill(12)
    elif mr_cedula_receptor is None:
        raise UserError(_('No se ha proporcionado una cédula de receptor válida para el MR.'))

    mr_consecutivo_receptor = re.sub('[^0-9]', '', consecutivo_receptor)
    if len(mr_consecutivo_receptor) != 20:
        raise UserError(_('La clave del consecutivo para el mensaje receptor es inválida. Debe contener 20 dígitos'))

    mr_monto_impuesto = monto_impuesto
    mr_detalle_mensaje = detalle_mensaje
    mr_total_factura = total_factura

    # Creación del MR
    sb = StringBuilder()
    sb.append('<MensajeReceptor xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ')
    sb.append('xmlns="https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/mensajeReceptor" ')
    sb.append('xsi:schemaLocation="https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/mensajeReceptor ')
    sb.append('https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.3/MensajeReceptor_V4.3.xsd">')
    sb.append('<Clave>' + mr_clave + '</Clave>')
    sb.append('<NumeroCedulaEmisor>' + mr_cedula_emisor + '</NumeroCedulaEmisor>')
    sb.append('<FechaEmisionDoc>' + fecha_emision + '</FechaEmisionDoc>')
    sb.append('<Mensaje>' + str(mr_mensaje_id) + '</Mensaje>')

    if mr_detalle_mensaje is not None:
        sb.append('<DetalleMensaje>' + escape(mr_detalle_mensaje) + '</DetalleMensaje>')

    if mr_monto_impuesto is not None and mr_monto_impuesto > 0:
        sb.append('<MontoTotalImpuesto>' + str(mr_monto_impuesto) + '</MontoTotalImpuesto>')

    if codigo_actividad:
        sb.append('<CodigoActividad>' + str(codigo_actividad) + '</CodigoActividad>')

    sb.append('<CondicionImpuesto>' + str(condicion_impuesto) + '</CondicionImpuesto>')

    if monto_total_impuesto_acreditar:
        sb.append('<MontoTotalImpuestoAcreditar>' + str(monto_total_impuesto_acreditar) + '</MontoTotalImpuestoAcreditar>')

    if monto_total_gasto_aplicable:
        sb.append('<MontoTotalDeGastoAplicable>' + str(monto_total_gasto_aplicable) + '</MontoTotalDeGastoAplicable>')

    if mr_total_factura is not None and mr_total_factura > 0:
        sb.append('<TotalFactura>' + str(mr_total_factura) + '</TotalFactura>')
    else:
        raise UserError(_('El monto Total de la Factura para el Mensaje Receptor es inválido'))

    sb.append('<NumeroCedulaReceptor>' + mr_cedula_receptor + '</NumeroCedulaReceptor>')
    sb.append('<NumeroConsecutivoReceptor>' + mr_consecutivo_receptor + '</NumeroConsecutivoReceptor>')
    sb.append('</MensajeReceptor>')
    return str(sb)


def _q5(value) -> str:
    """Redondeo a 5 decimales con HALF_UP, conforme al XSD (fractionDigits=5)."""
    return str(Decimal(str(value)).quantize(Decimal('0.00000'), rounding=ROUND_HALF_UP))

def _clave_extract_cedula_12(clave: str) -> str:
    """
    Extrae los 12 dígitos de cédula embebidos en la Clave (posiciones 10..21, 1-based).
    Clave = 3 (pais) + 6 (fecha) + 12 (cedula) + 20 (consecutivo) + 8 (situación) + 1 (seguridad) = 50.
    """
    m = re.match(r'^(\d{3})(\d{6})(\d{12})', clave or '')
    return m.group(3) if m else None

def _ensure_datetime_iso8601(dt_str: str) -> str:
    """
    Valida mínimamente que venga tipo 'YYYY-MM-DDThh:mm:ss±hh:mm'.
    Si usas objetos datetime, formatear con .isoformat().
    """
    if not isinstance(dt_str, str) or 'T' not in dt_str:
        raise UserError(_('FechaEmisionDoc debe ser xs:dateTime ISO-8601, ej: 2025-11-06T21:13:00-06:00'))
    return dt_str

def gen_xml_mr_44(clave, cedula_emisor, fecha_emision_iso8601, id_mensaje,
                  detalle_mensaje, cedula_receptor,
                  consecutivo_receptor,
                  monto_impuesto=None, total_factura=None,
                  codigo_actividad=None,
                  condicion_impuesto=None,
                  monto_total_impuesto_acreditar=None,
                  monto_total_gasto_aplicable=None):
    """
    Genera XML de Mensaje Receptor v4.4 (SIN firma).
    NOTA: El XSD exige <ds:Signature>, pero aquí NO se incluye ni valida.
          Agrega la firma en tu función externa y luego inserta <ds:Signature> antes de enviar/validar.
    """

    # --- Validaciones base ---
    mr_clave = re.sub(r'[^0-9]', '', str(clave or ''))
    if len(mr_clave) != 50:
        raise UserError(_('La clave a utilizar es inválida. Debe contener 50 dígitos.'))

    # Cedulas: XSD permite 9..12 dígitos; para comparar con la clave, zfill(12)
    _ced_emisor_raw = re.sub(r'[^0-9]', '', str(cedula_emisor or ''))
    if not (9 <= len(_ced_emisor_raw) <= 12):
        raise UserError(_('NumeroCedulaEmisor inválido: Debe contener entre 9 y 12 dígitos.'))
    mr_cedula_emisor = _ced_emisor_raw
    mr_cedula_emisor_12 = _ced_emisor_raw.zfill(12)

    _ced_receptor_raw = re.sub(r'[^0-9]', '', str(cedula_receptor or ''))
    if not (9 <= len(_ced_receptor_raw) <= 12):
        raise UserError(_('NumeroCedulaReceptor inválido: Debe contener entre 9 y 12 dígitos.'))
    mr_cedula_receptor = _ced_receptor_raw

    mr_consecutivo_receptor = re.sub(r'[^0-9]', '', str(consecutivo_receptor or ''))
    if len(mr_consecutivo_receptor) != 20:
        raise UserError(_('NumeroConsecutivoReceptor inválido: Debe contener exactamente 20 dígitos.'))

    fecha_emision_iso8601 = _ensure_datetime_iso8601(fecha_emision_iso8601)

    try:
        mr_mensaje_id = int(id_mensaje)
    except Exception:
        raise UserError(_('El ID del mensaje receptor es inválido.'))
    if mr_mensaje_id < 1 or mr_mensaje_id > 3:
        raise UserError(_('El ID del mensaje receptor debe ser 1, 2 o 3.'))

    # Comparar cédula embebida en Clave (evita error -8)
    cedula_en_clave = _clave_extract_cedula_12(mr_clave)
    if not cedula_en_clave or cedula_en_clave != mr_cedula_emisor_12:
        raise UserError(_('La cédula del emisor en el MR no coincide con la cédula embebida en la Clave.'))

    # Montos (XSD permite 5 decimales). TotalFactura es obligatorio.
    if total_factura is None:
        raise UserError(_('TotalFactura es obligatorio.'))
    total_factura_q = _q5(total_factura)

    monto_impuesto_q = None
    if monto_impuesto is not None and Decimal(str(monto_impuesto)) > 0:
        monto_impuesto_q = _q5(monto_impuesto)

    mti_acred_q = _q5(monto_total_impuesto_acreditar) if monto_total_impuesto_acreditar not in (None, '') else None
    mt_gasto_q  = _q5(monto_total_gasto_aplicable)    if monto_total_gasto_aplicable    not in (None, '') else None

    # DetalleMensaje (opcional, máx 160 chars)
    detalle_xml = None
    if detalle_mensaje:
        dm = str(detalle_mensaje).replace('\r\n', '\n').replace('\r', '\n')
        dm = escape(dm).replace('\n', '&#13;')  # Hacienda suele representar saltos como &#13;
        if len(dm) > 160:
            dm = dm[:160]
        detalle_xml = dm

    # CondicionImpuesto: si se envía, debe ser 01..05
    if condicion_impuesto is not None and condicion_impuesto not in ('01', '02', '03', '04', '05'):
        raise UserError(_('CondicionImpuesto inválida. Valores permitidos: 01, 02, 03, 04, 05.'))

    # --- Construcción XML (SIN firma) ---
    parts = []
    parts.append('<MensajeReceptor xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ')
    parts.append('xmlns="https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.4/mensajeReceptor" ')
    # Nota: no declaramos xmlns:ds aquí porque no incluimos la firma todavía.
    parts.append('xsi:schemaLocation="https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.4/mensajeReceptor ')
    parts.append('https://www.hacienda.go.cr/ATV/ComprobanteElectronico/docs/esquemas/2016/v4.4/MensajeReceptor_V4.4.xsd">')

    parts.append(f'<Clave>{mr_clave}</Clave>')
    parts.append(f'<NumeroCedulaEmisor>{mr_cedula_emisor}</NumeroCedulaEmisor>')
    parts.append(f'<FechaEmisionDoc>{fecha_emision_iso8601}</FechaEmisionDoc>')
    parts.append(f'<Mensaje>{mr_mensaje_id}</Mensaje>')

    if detalle_xml is not None:
        parts.append(f'<DetalleMensaje>{detalle_xml}</DetalleMensaje>')

    if monto_impuesto_q is not None:
        parts.append(f'<MontoTotalImpuesto>{monto_impuesto_q}</MontoTotalImpuesto>')

    if codigo_actividad:
        codigo_str = str(codigo_actividad).strip()
        if len(codigo_str) > 6:
            raise UserError(_('CodigoActividad supera 6 caracteres.'))
        parts.append(f'<CodigoActividad>{codigo_str}</CodigoActividad>')

    if condicion_impuesto is not None:
        parts.append(f'<CondicionImpuesto>{condicion_impuesto}</CondicionImpuesto>')

    if mti_acred_q is not None:
        parts.append(f'<MontoTotalImpuestoAcreditar>{mti_acred_q}</MontoTotalImpuestoAcreditar>')

    if mt_gasto_q is not None:
        parts.append(f'<MontoTotalDeGastoAplicable>{mt_gasto_q}</MontoTotalDeGastoAplicable>')

    parts.append(f'<TotalFactura>{total_factura_q}</TotalFactura>')
    parts.append(f'<NumeroCedulaReceptor>{mr_cedula_receptor}</NumeroCedulaReceptor>')
    parts.append(f'<NumeroConsecutivoReceptor>{mr_consecutivo_receptor}</NumeroConsecutivoReceptor>')
    parts.append('</MensajeReceptor>')

    return ''.join(parts)


def gen_xml_v44(inv, sale_conditions, total_servicio_gravado,
                total_servicio_exento, totalServExonerado,
                total_mercaderia_gravado, total_mercaderia_exento,
                totalMercExonerada, totalOtrosCargos, total_iva_devuelto, base_total,
                total_impuestos, total_descuento, lines,
                otrosCargos, currency_rate, invoice_comments,
                tipo_documento_referencia, numero_documento_referencia,
                fecha_emision_referencia, codigo_referencia, razon_referencia,
                total_mercaderia_no_sujeta = 0,total_servicio_no_sujeto = 0, total_impuestos_asumidos = 0):

    numero_linea = 0

    # === Medios de pago (nuevo v4.4) ===
    medios_pago = []
    desglose_impuesto = {}  # key: (codigo, codigo_tarifa_iva_o_otro) -> monto acumulado
    total_comprobante_tmp = round(base_total + total_impuestos + totalOtrosCargos - (total_iva_devuelto or 0.0), 5)

    if inv._name == 'pos.order':
        plazo_credito = '0'
        cod_moneda = str(inv.company_id.currency_id.name)
        for payment in getattr(inv, 'payment_ids', []):
            code = str(getattr(payment.payment_method_id, 'sequence', '') or '01')
            amount = float(getattr(payment, 'amount', 0.0) or 0.0)
            descOtros = str(getattr(payment, 'notes', '') or 'Otros no especificado.')
            if amount:
                medios_pago.append({'tipo': code, 'monto': amount, 'otros': descOtros})
        if not medios_pago:
            medios_pago.append({'tipo': '01', 'monto': total_comprobante_tmp})
    else:
        # account.move
        cod_moneda = str(inv.currency_id.name)
        plazo_credito = str(inv.invoice_payment_term_id and inv.invoice_payment_term_id.line_ids[:1].days or 0)
        code = str(inv.payment_methods_id.sequence or '01')
        otros = (inv.payment_method_otros or '').strip()
        mp = {'tipo': code, 'monto': total_comprobante_tmp}
        if code == '99':
            mp['otros'] = otros
        medios_pago.append(mp)

    if inv.tipo_documento == 'FEC':
        issuing_company = inv.partner_id
        receiver_company = inv.company_id
    else:
        issuing_company = inv.company_id
        receiver_company = inv.partner_id

    sb = StringBuilder()
    sb.append('<' + fe_enums.tagName[inv.tipo_documento] + ' xmlns="' + fe_enums.XmlnsHacienda[inv.tipo_documento] + '" ')
    sb.append('xmlns:ds="http://www.w3.org/2000/09/xmldsig#" xmlns:xsd="http://www.w3.org/2001/XMLSchema" ')
    sb.append('xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ')
    sb.append('xsi:schemaLocation="' + fe_enums.schemaLocation[inv.tipo_documento] + '">')

    # Encabezado v4.4
    sb.append('<Clave>' + inv.number_electronic + '</Clave>')
    prov = re.sub('[^0-9]', '', (getattr(inv.company_id, 'x_proveedor_sistemas', '') or inv.company_id.vat or ''))
    if prov:
        sb.append('<ProveedorSistemas>' + str(prov) + '</ProveedorSistemas>')
    if getattr(inv, 'economic_activity_id', False):
        sb.append('<CodigoActividadEmisor>' + inv.economic_activity_id.code + '</CodigoActividadEmisor>')
    # Codigo Actividad del receptor (si existe)
    rec_act = ''
    try:
        rec_act = getattr(getattr(receiver_company, 'activity_id', None), 'code', '') or ''
    except Exception:
        rec_act = ''
    if rec_act:
        sb.append('<CodigoActividadReceptor>' + rec_act + '</CodigoActividadReceptor>')

    sb.append('<NumeroConsecutivo>' + inv.number_electronic[21:41] + '</NumeroConsecutivo>')
    sb.append('<FechaEmision>' + inv.date_issuance + '</FechaEmision>')

    # Emisor
    sb.append('<Emisor>')
    sb.append('<Nombre>' + escape(issuing_company.legal_name or issuing_company.name) + '</Nombre>')
    sb.append('<Identificacion>')
    sb.append('<Tipo>' + issuing_company.identification_id.code + '</Tipo>')
    sb.append('<Numero>' + issuing_company.vat + '</Numero>')
    sb.append('</Identificacion>')
    if issuing_company.commercial_name:
        sb.append('<NombreComercial>' + escape(str(issuing_company.commercial_name or 'NA')) + '</NombreComercial>')
    sb.append('<Ubicacion>')
    sb.append('<Provincia>' + issuing_company.state_id.code + '</Provincia>')
    sb.append('<Canton>' + issuing_company.county_id.code + '</Canton>')
    sb.append('<Distrito>' + issuing_company.district_id.code + '</Distrito>')
    if issuing_company.neighborhood_id and issuing_company.neighborhood_id.code:
        sb.append('<Barrio>' + str(issuing_company.neighborhood_id.name or 'Otro barrio') + '</Barrio>')
    sb.append('<OtrasSenas>' + escape(str(issuing_company.street or 'NA')) + '</OtrasSenas>')
    sb.append('</Ubicacion>')
    if issuing_company.phone:
        phone = phonenumbers.parse(issuing_company.phone, (issuing_company.country_id.code or 'CR'))
        sb.append('<Telefono>')
        sb.append('<CodigoPais>' + str(phone.country_code) + '</CodigoPais>')
        sb.append('<NumTelefono>' + str(phone.national_number) + '</NumTelefono>')
        sb.append('</Telefono>')
    sb.append('<CorreoElectronico>' + str(issuing_company.email) + '</CorreoElectronico>')
    sb.append('</Emisor>')

    # Receptor
    if inv.tipo_documento == 'TE' or (inv.tipo_documento == 'NC' and not receiver_company.vat):
        pass
    else:
        vat = re.sub('[^0-9]', '', receiver_company.vat)
        if not receiver_company.identification_id:
            if len(vat) == 9:
                id_code = '01'
            elif len(vat) == 10:
                id_code = '02'
            elif len(vat) in (11, 12):
                id_code = '03'
            else:
                id_code = '05'
        else:
            id_code = receiver_company.identification_id.code

        if receiver_company.name:
            sb.append('<Receptor>')
            sb.append('<Nombre>' + escape(str(receiver_company.name[:99])) + '</Nombre>')

            if inv.tipo_documento == 'FEE' or id_code == '05':
                if receiver_company.vat:
                    sb.append('<IdentificacionExtranjero>' + receiver_company.vat + '</IdentificacionExtranjero>')
            else:
                sb.append('<Identificacion>')
                sb.append('<Tipo>' + id_code + '</Tipo>')
                sb.append('<Numero>' + vat + '</Numero>')
                sb.append('</Identificacion>')

            if inv.tipo_documento != 'FEE':
                if receiver_company.state_id and receiver_company.county_id and receiver_company.district_id and receiver_company.neighborhood_id:
                    sb.append('<Ubicacion>')
                    sb.append('<Provincia>' + str(receiver_company.state_id.code or '') + '</Provincia>')
                    sb.append('<Canton>' + str(receiver_company.county_id.code or '') + '</Canton>')
                    sb.append('<Distrito>' + str(receiver_company.district_id.code or '') + '</Distrito>')
                    if receiver_company.neighborhood_id and receiver_company.neighborhood_id.code:
                        sb.append('<Barrio>' + str(receiver_company.neighborhood_id.name or 'Otro barrio') + '</Barrio>')
                    sb.append('<OtrasSenas>' + escape(str(receiver_company.street or 'NA')) + '</OtrasSenas>')
                    sb.append('</Ubicacion>')

                if receiver_company.phone:
                    phone = phonenumbers.parse(receiver_company.phone, (receiver_company.country_id.code or 'CR'))
                    sb.append('<Telefono>')
                    sb.append('<CodigoPais>' + str(phone.country_code) + '</CodigoPais>')
                    sb.append('<NumTelefono>' + str(phone.national_number) + '</NumTelefono>')
                    sb.append('</Telefono>')

                re_match = r'^(\s?[^\s,]+@[^\s,]+\.[^\s,]+\s?,)*(\s?[^\s,]+@[^\s,]+\.[^\s,]+)$'
                match = receiver_company.email and re.match(re_match, receiver_company.email.lower())
                email_receptor = receiver_company.email if match else 'indefinido@indefinido.com'
                sb.append('<CorreoElectronico>' + email_receptor + '</CorreoElectronico>')

            sb.append('</Receptor>')

    # Condiciones de venta
    sb.append('<CondicionVenta>' + sale_conditions + '</CondicionVenta>')
    sb.append('<PlazoCredito>' + plazo_credito + '</PlazoCredito>')

    # ===== Detalle =====
    if lines:
        sb.append('<DetalleServicio>')
        for (k, v) in lines.items():
            numero_linea += 1
            sb.append('<LineaDetalle>')
            sb.append('<NumeroLinea>' + str(numero_linea) + '</NumeroLinea>')

            # CABYS
            if v.get('codigoCabys'):
                # En 4.4 el CABYS sigue en <Codigo> (no <CodigoCABYS>)
                sb.append('<CodigoCABYS>' + (v['codigoCabys']) + '</CodigoCABYS>')

            # Partida Arancelaria (solo FEE)
            if inv.tipo_documento == 'FEE' and v.get('partidaArancelaria'):
                sb.append('<PartidaArancelaria>' + str(v['partidaArancelaria']) + '</PartidaArancelaria>')

            # Código comercial opcional
            if v.get('codigo'):
                sb.append('<CodigoComercial>')
                sb.append('<Tipo>04</Tipo>')
                sb.append('<Codigo>' + (v['codigo']) + '</Codigo>')
                sb.append('</CodigoComercial>')

            sb.append('<Cantidad>' + str(v['cantidad']) + '</Cantidad>')
            sb.append('<UnidadMedida>' + str(v['unidadMedida']) + '</UnidadMedida>')
            sb.append('<Detalle>' + str(v['detalle']) + '</Detalle>')
            sb.append('<PrecioUnitario>' + str(v['precioUnitario']) + '</PrecioUnitario>')
            sb.append('<MontoTotal>' + str(v['montoTotal']) + '</MontoTotal>')

            # Descuento (si aplica)
            if v.get('montoDescuento'):
                sb.append('<Descuento>')
                sb.append('<MontoDescuento>' + str(v['montoDescuento']) + '</MontoDescuento>')         
                # (Opcional en 4.4) CódigoDescuento si lo manejas:
                if v.get('codigoDescuento'):
                    sb.append('<CodigoDescuento>' + str(v['codigoDescuento']) + '</CodigoDescuento>')
                            
                if v.get('naturalezaDescuento'):
                    sb.append('<NaturalezaDescuento>' + str(v['naturalezaDescuento']) + '</NaturalezaDescuento>')
                sb.append('</Descuento>')
            sb.append('<SubTotal>' + str(v['subtotal']) + '</SubTotal>')

            # === CAMBIO v4.4: estos dos van ANTES de <Impuesto> y a NIVEL DE LÍNEA ===
            # IVACobradoFabrica (opcional)
            if v.get('iva_cobrado_fabrica') is not None:
                sb.append('<IVACobradoFabrica>' + _fmt(v['iva_cobrado_fabrica']) + '</IVACobradoFabrica>')

            # BaseImponible de la línea (OBLIGATORIO en 4.4) – antes de cualquier <Impuesto>
            base_imponible_linea = v.get('base_imponible')
            if base_imponible_linea is None:
                base_imponible_linea = float(v['cantidad']) * float(v['precioUnitario']) - float(
                    v.get('montoDescuento', 0.0))
            sb.append('<BaseImponible>' + _fmt(base_imponible_linea) + '</BaseImponible>')
            # === FIN CAMBIO ===
            valor_asumido = v.get('impuesto_asumido_fab')
            # Impuesto por línea (0..n)
            if v.get('impuesto'):
                for (a, b) in v['impuesto'].items():
                    sb.append('<Impuesto>')
                    sb.append('<Codigo>' + str(b['codigo']) + '</Codigo>')

                    # IVA (Codigo=01): en 4.4 se usa CodigoTarifaIVA
                    if str(b.get('codigo')) == '01':
                        tax_code = str(b.get('iva_tax_code', '')).strip()
                        if tax_code:
                            sb.append('<CodigoTarifaIVA>' + tax_code + '</CodigoTarifaIVA>')
                    # Otros impuestos específicos (si aplica)
                    elif b.get('codigo_impuesto_otro'):
                        sb.append('<CodigoImpuestoOTRO>' + str(b['codigo_impuesto_otro']) + '</CodigoImpuestoOTRO>')

                    # Tarifa
                    sb.append('<Tarifa>' + _fmt(b['tarifa']) + '</Tarifa>')

                    # FactorCalculoIVA (opcional)
                    if b.get('factor_calculo_iva') is not None:
                        sb.append('<FactorCalculoIVA>' + _fmt(b['factor_calculo_iva']) + '</FactorCalculoIVA>')

                    # OJO: En 4.4, BaseImponible YA NO va dentro de <Impuesto>
                    # Monto del impuesto
                    sb.append('<Monto>' + _fmt(b['monto']) + '</Monto>')

                    # Exoneración (si corresponde)
                    if inv.tipo_documento != 'FEE' and b.get('exoneracion'):
                        sb.append('<Exoneracion>')
                        sb.append('<TipoDocumento>' + receiver_company.type_exoneration.code + '</TipoDocumento>')
                        sb.append('<NumeroDocumento>' + receiver_company.exoneration_number + '</NumeroDocumento>')
                        sb.append('<NombreInstitucion>' + receiver_company.institution_name + '</NombreInstitucion>')
                        sb.append(
                            '<FechaEmision>' + str(receiver_company.date_issue) + 'T00:00:00-06:00</FechaEmision>')
                        sb.append('<PorcentajeExoneracion>' + _fmt(
                            b['exoneracion']['porcentajeCompra']) + '</PorcentajeExoneracion>')
                        sb.append(
                            '<MontoExoneracion>' + _fmt(b['exoneracion']['montoImpuesto']) + '</MontoExoneracion>')
                        sb.append('</Exoneracion>')

                    sb.append('</Impuesto>')
                    # === acumular para <TotalDesgloseImpuesto> ===
                    codigo = str(b.get('codigo') or '')
                    if codigo == '01':
                        code_key = str(b.get('iva_tax_code') or '')  # p.ej. '08'
                    else:
                        code_key = str(b.get('codigo_impuesto_otro') or '')
                    k = (codigo, code_key)
                    if valor_asumido is None:
                        desglose_impuesto[k] = float(desglose_impuesto.get(k, 0.0)) + float(b.get('monto') or 0.0)

            if valor_asumido is not None:
                sb.append('<ImpuestoAsumidoEmisorFabrica>' + _fmt(valor_asumido) + '</ImpuestoAsumidoEmisorFabrica>')
            else:
                sb.append('<ImpuestoAsumidoEmisorFabrica>0.0</ImpuestoAsumidoEmisorFabrica>')
            # ImpuestoNeto al final de los impuestos de la línea
            sb.append('<ImpuestoNeto>' + _fmt(v['impuestoNeto']) + '</ImpuestoNeto>')
            sb.append('<MontoTotalLinea>' + _fmt(v['montoTotalLinea']) + '</MontoTotalLinea>')
            sb.append('</LineaDetalle>')
        sb.append('</DetalleServicio>')

    # ===== Otros Cargos =====
    if otrosCargos:
        sb.append('<OtrosCargos>')
        for otro_cargo in otrosCargos:
            sb.append('<TipoDocumento>' + str(otrosCargos[otro_cargo]['TipoDocumento']) + '</TipoDocumento>')
            if otrosCargos[otro_cargo].get('NumeroIdentidadTercero'):
                sb.append('<NumeroIdentidadTercero>' + str(otrosCargos[otro_cargo]['NumeroIdentidadTercero']) + '</NumeroIdentidadTercero>')
            if otrosCargos[otro_cargo].get('NombreTercero'):
                sb.append('<NombreTercero>' + str(otrosCargos[otro_cargo]['NombreTercero']) + '</NombreTercero>')
            sb.append('<Detalle>' + str(otrosCargos[otro_cargo]['Detalle']) + '</Detalle>')
            if otrosCargos[otro_cargo].get('Porcentaje'):
                sb.append('<Porcentaje>' + str(otrosCargos[otro_cargo]['Porcentaje']) + '</Porcentaje>')
            sb.append('<MontoCargo>' + str(otrosCargos[otro_cargo]['MontoCargo']) + '</MontoCargo>')
        sb.append('</OtrosCargos>')

    # ===== Resumen =====
    sb.append('<ResumenFactura>')
    # Moneda
    sb.append('<CodigoTipoMoneda>')
    sb.append('<CodigoMoneda>' + cod_moneda + '</CodigoMoneda>')
    sb.append('<TipoCambio>' + str(currency_rate) + '</TipoCambio>')
    sb.append('</CodigoTipoMoneda>')

    sb.append('<TotalServGravados>' + str(total_servicio_gravado) + '</TotalServGravados>')
    sb.append('<TotalServExentos>' + str(total_servicio_exento) + '</TotalServExentos>')
    if inv.tipo_documento != 'FEE':
        sb.append('<TotalServExonerado>' + str(totalServExonerado) + '</TotalServExonerado>')
    if inv.tipo_documento != 'FEE' and total_servicio_no_sujeto > 0:
        sb.append('<TotalServNoSujeto>' + str(total_servicio_no_sujeto) + '</TotalServNoSujeto>')
    sb.append('<TotalMercanciasGravadas>' + str(total_mercaderia_gravado) + '</TotalMercanciasGravadas>')
    sb.append('<TotalMercanciasExentas>' + str(total_mercaderia_exento) + '</TotalMercanciasExentas>')
    if inv.tipo_documento != 'FEE':
        sb.append('<TotalMercExonerada>' + str(totalMercExonerada) + '</TotalMercExonerada>')
    if inv.tipo_documento != 'FEE' and total_mercaderia_no_sujeta > 0:
        sb.append('<TotalMercNoSujeta>' + str(total_mercaderia_no_sujeta) + '</TotalMercNoSujeta>')
    sb.append('<TotalGravado>' + str(round(total_servicio_gravado + total_mercaderia_gravado, 5)) + '</TotalGravado>')
    sb.append('<TotalExento>' + str(round(total_servicio_exento + total_mercaderia_exento, 5)) + '</TotalExento>')
    if inv.tipo_documento != 'FEE':
        sb.append('<TotalExonerado>' + str(round(totalServExonerado + totalMercExonerada, 5)) + '</TotalExonerado>')
    if inv.tipo_documento != 'FEE':
        sb.append('<TotalNoSujeto>' + str(round(total_mercaderia_no_sujeta+total_servicio_no_sujeto, 5)) + '</TotalNoSujeto>')
    sb.append('<TotalVenta>' + str(round(total_servicio_gravado + total_mercaderia_gravado + total_servicio_exento + total_mercaderia_exento + totalServExonerado + totalMercExonerada + total_mercaderia_no_sujeta, 5)) + '</TotalVenta>')
    sb.append('<TotalDescuentos>' + str(round(total_descuento, 5)) + '</TotalDescuentos>')
    sb.append('<TotalVentaNeta>' + str(round(base_total, 5)) + '</TotalVentaNeta>')
    if desglose_impuesto:
        # v4.4: entradas repetidas TotalDesgloseImpuesto
        for (codigo, key_iva), monto in desglose_impuesto.items():
            sb.append('<TotalDesgloseImpuesto>')
            sb.append('<Codigo>' + codigo + '</Codigo>')
            if codigo == '01' and key_iva:
                sb.append('<CodigoTarifaIVA>' + key_iva + '</CodigoTarifaIVA>')
            sb.append('<TotalMontoImpuesto>' + _fmt(monto) + '</TotalMontoImpuesto>')
            sb.append('</TotalDesgloseImpuesto>')
    sb.append('<TotalImpuesto>' + str(round(total_impuestos, 5)) + '</TotalImpuesto>')
    sb.append('<TotalImpAsumEmisorFabrica>' + str(round(total_impuestos_asumidos, 5)) + '</TotalImpAsumEmisorFabrica>')
    if total_iva_devuelto:
        sb.append('<TotalIVADevuelto>' + str(round(total_iva_devuelto, 5)) + '</TotalIVADevuelto>')
    sb.append('<TotalOtrosCargos>' + str(totalOtrosCargos) + '</TotalOtrosCargos>')

    # Medios de Pago (v4.4) – estructura compleja
    for mp in medios_pago[:4]:
        sb.append('<MedioPago>')
        sb.append('<TipoMedioPago>' + str(mp.get('tipo') or '01') + '</TipoMedioPago>')
        if (mp.get('tipo') == '99'):
            sb.append('<MedioPagoOtros>' + escape(str(mp['otros'])[:100]) + '</MedioPagoOtros>')
        sb.append('<TotalMedioPago>' + ('%0.5f' % float(mp.get('monto', 0.0))).rstrip('0').rstrip('.') + '</TotalMedioPago>')
        sb.append('</MedioPago>')

    sb.append('<TotalComprobante>' + str(total_comprobante_tmp) + '</TotalComprobante>')

    sb.append('</ResumenFactura>')

    # Información de referencia
    if tipo_documento_referencia and numero_documento_referencia and fecha_emision_referencia:
        sb.append('<InformacionReferencia>')
        sb.append('<TipoDocIR>' + str(tipo_documento_referencia) + '</TipoDocIR>')
        sb.append('<Numero>' + str(numero_documento_referencia) + '</Numero>')
        sb.append('<FechaEmisionIR>' + fecha_emision_referencia + '</FechaEmisionIR>')
        sb.append('<Codigo>' + str(codigo_referencia) + '</Codigo>')
        sb.append('<Razon>' + str(razon_referencia) + '</Razon>')
        sb.append('</InformacionReferencia>')

    # Otros
    if invoice_comments or (hasattr(inv, 'ref') and inv.ref):
        sb.append('<Otros>')
        if invoice_comments:
            sb.append('<OtroTexto>' + str(invoice_comments) + '</OtroTexto>')
        if inv.ref and inv.tipo_documento == 'FE':
            sb.append('<OtroTexto codigo="'+ inv.partner_id.oc_xml +'">' + str(inv.ref) + '</OtroTexto>')

        sb.append('</Otros>')

    sb.append('</' + fe_enums.tagName[inv.tipo_documento] + '>')
    return sb


def _fmt(x):
    # v4.4 admite decimales; formateo consistente a 5 decimales sin ceros de cola
    return ('%.5f' % float(x)).rstrip('0').rstrip('.')


# Funcion para enviar el XML al Ministerio de Hacienda
def send_xml_fe(inv, token, date, xml, tipo_ambiente):
    headers = {'Authorization': 'Bearer ' + token, 'Content-type': 'application/json'}
    endpoint = fe_enums.UrlHaciendaRecepcion[tipo_ambiente]

    xml_base64 = string_to_base64(xml)

    data = {'clave': inv.number_electronic,
            'fecha': date,
            'emisor': {
                'tipoIdentificacion': inv.company_id.identification_id.code,
                'numeroIdentificacion': inv.company_id.vat
            },
            'comprobanteXml': xml_base64
            }
    if inv.partner_id and inv.partner_id.vat:
        if not inv.partner_id.identification_id:
            if len(inv.partner_id.vat) == 9:
                id_code = '01'
            elif len(inv.partner_id.vat) == 10:
                id_code = '02'
            elif len(inv.partner_id.vat) in (11, 12):
                id_code = '03'
            else:
                id_code = '05'
        else:
            id_code = inv.partner_id.identification_id.code

        data['receptor'] = {'tipoIdentificacion': id_code,
                            'numeroIdentificacion': inv.partner_id.vat}

    json_hacienda = json.dumps(data)

    try:
        response = requests.request("POST", endpoint, data=json_hacienda, headers=headers)
        if response.status_code != 202:
            error_caused_by = response.headers.get('X-Error-Cause') if 'X-Error-Cause' in response.headers else ''
            error_caused_by += response.headers.get('validation-exception', '')
            _logger.error('Status: {}, Text {}'.format(response.status_code, error_caused_by))
            return {'status': response.status_code, 'text': error_caused_by}
        else:
            return {'status': response.status_code, 'text': response.reason}
    except ImportError:
        raise Warning(_('Error enviando el XML al Ministerior de Hacienda'))


def schema_validator(xml_file, xsd_file) -> bool:
    """ verifies a xml """
    xmlschema = etree.XMLSchema(etree.parse(os.path.join(os.path.dirname(__file__), "xsd/" + xsd_file)))
    xml_doc = base64decode(xml_file)
    root = etree.fromstring(xml_doc, etree.XMLParser(remove_blank_text=True))
    result = xmlschema.validate(root)
    return result


# Obtener Attachments para las Facturas Electrónicas
def get_invoice_attachments(invoice, record_id):
    attachments = []

    domain = [('res_model', '=', invoice._name),
              ('res_id', '=', invoice.id),
              ('res_field', '=', 'xml_comprobante'),
              ('name', '=', invoice.tipo_documento + '_' + invoice.number_electronic + '.xml')]
    attachment = invoice.env['ir.attachment'].sudo().search(domain, limit=1)

    if attachment.id:
        attach_copy = invoice.env['ir.attachment'].create({'name': invoice.fname_xml_comprobante,
                                                           'type': 'binary',
                                                           'datas': invoice.xml_comprobante,
                                                           'res_name': invoice.fname_xml_comprobante,
                                                           'mimetype': 'text/xml'})
        attachments.append(attach_copy.id)

    domain_resp = [('res_model', '=', invoice._name),
                   ('res_id', '=', invoice.id),
                   ('res_field', '=', 'xml_respuesta_tributacion'),
                   ('name', '=', 'AHC_' + invoice.number_electronic + '.xml')]
    attachment_resp = invoice.env['ir.attachment'].sudo().search(domain_resp, limit=1)

    if attachment_resp.id:
        attach_resp_copy = invoice.env['ir.attachment'].create({'name': invoice.fname_xml_respuesta_tributacion,
                                                                'type': 'binary',
                                                                'datas': invoice.xml_respuesta_tributacion,
                                                                'res_name': invoice.fname_xml_respuesta_tributacion,
                                                                'mimetype': 'text/xml'})
        attachments.append(attach_resp_copy.id)

    return attachments


def parse_xml(name):
    return etree.parse(name).getroot()


# CONVIERTE UN STRING A BASE 64
def string_to_base64(s):
    return base64.b64encode(s).decode()


# TOMA UNA CADENA Y ELIMINA LOS CARACTERES AL INICIO Y AL FINAL
def string_strip(s, start, end):
    return s[start:-end]


# Tomamos el XML y le hacemos el decode de base 64, esto por ahora es solo para probar
# la posible implementacion de la firma en python
def base64decode(string_decode):
    return base64.b64decode(string_decode)


# TOMA UNA CADENA EN BASE64 Y LA DECODIFICA PARA ELIMINAR EL b' Y DEJAR EL STRING CODIFICADO
# DE OTRA MANERA HACIENDA LO RECHAZA
def base64_utf8_decoder(s):
    return s.decode("utf-8")


# CLASE PERSONALIZADA (NO EXISTE EN PYTHON) QUE CONSTRUYE UNA CADENA MEDIANTE APPEND SEMEJANTE
# AL STRINGBUILDER DEL C#
class StringBuilder:
    _file_str = None

    def __init__(self):
        self._file_str = io.StringIO()

    def append(self, s):
        self._file_str.write(s)

    def __str__(self):
        return self._file_str.getvalue()


def consulta_clave(clave, token, tipo_ambiente):
    endpoint = fe_enums.UrlHaciendaRecepcion[tipo_ambiente] + clave

    headers = {'Authorization': 'Bearer {}'.format(token),
               'Cache-Control': 'no-cache',
               'Content-Type': 'application/x-www-form-urlencoded'}

    _logger.debug('FECR - consulta_clave - url: %s', endpoint)

    try:
        response = requests.get(endpoint, headers=headers)
    except requests.exceptions.RequestException as e:
        _logger.error('Exception %s', e)
        return {'status': -1, 'text': 'Excepcion %s' % e}

    if 200 <= response.status_code <= 299:
        response_json = {'status': 200,
                         'ind-estado': response.json().get('ind-estado'),
                         'respuesta-xml': response.json().get('respuesta-xml')}
    elif 400 <= response.status_code <= 499:
        _logger.error('FECR - 400 - consulta_clave failed.  error: %s reason: %s', response.status_code, response.reason)
        response_json = {'status': 400, 'ind-estado': 'error'}
    else:
        _logger.error('FECR - consulta_clave failed.  error: %s', response.status_code)
        response_json = {'status': response.status_code, 'text': 'token_hacienda failed: %s' % response.reason}
    return response_json


def get_economic_activities(company):
    endpoint = "https://api.hacienda.go.cr/fe/ae?identificacion=" + company.vat

    headers = {'Cache-Control': 'no-cache',
               'Content-Type': 'application/x-www-form-urlencoded'}

    try:
        response = requests.get(endpoint, headers=headers, verify=False)
    except requests.exceptions.RequestException as e:
        _logger.error('Exception %s', e)
        return {'status': -1, 'text': 'Excepcion %s' % e}

    if 200 <= response.status_code <= 299:
        _logger.debug('FECR - get_economic_activities response: %s', (response.json()))
        response_json = {'status': 200, 'activities': response.json().get('actividades'), 'name': response.json().get('nombre')}
    else:
        _logger.error('FECR - get_economic_activities failed.  error: %s', response.status_code)
        response_json = {'status': response.status_code, 'text': 'get_economic_activities failed: %s' % response.reason}
    return response_json


def consulta_documentos(self, inv, env, token_m_h, date_cr, xml_firmado):
    if (inv.move_type in ['in_invoice', 'in_refund']) and (inv.tipo_documento != 'FEC'):
        clave = inv.number_electronic + "-" + inv.consecutive_number_receiver
    else:
        clave = inv.number_electronic

    response_json = consulta_clave(clave, token_m_h, env)
    _logger.debug(response_json)
    estado_m_h = response_json.get('ind-estado')

    last_state = inv.state_tributacion
    inv.state_tributacion = estado_m_h
    if inv.move_type in ['out_invoice', 'out_refund']:
        last_state = inv.state_tributacion
        inv.state_tributacion = estado_m_h
        if date_cr:
            inv.date_issuance = date_cr
        if xml_firmado:
            inv.fname_xml_comprobante = inv.tipo_documento + inv.number_electronic + '.xml'
            self.env['ir.attachment'].sudo().create({'name': inv.fname_xml_comprobante,
                                                     'type': 'binary',
                                                     'datas': xml_firmado,
                                                     'res_model': self._name,
                                                     'res_id': inv.id,
                                                     'res_field': 'xml_comprobante',
                                                     'res_name': inv.fname_xml_comprobante,
                                                     'mimetype': 'text/xml'})
    elif inv.move_type in ['in_invoice', 'in_refund']:
        if xml_firmado:
            inv.fname_xml_comprobante = 'AHC_' + inv.number_electronic + '.xml'
            self.env['ir.attachment'].sudo().create({'name': inv.fname_xml_comprobante,
                                                     'type': 'binary',
                                                     'datas': xml_firmado,
                                                     'res_model': self._name,
                                                     'res_id': inv.id,
                                                     'res_field': 'xml_comprobante',
                                                     'res_name': inv.fname_xml_comprobante,
                                                     'mimetype': 'text/xml'})

    if (estado_m_h in ['aceptado', 'rechazado']) or (inv.move_type in ['out_invoice', 'out_refund']):
        inv.fname_xml_respuesta_tributacion = 'AHC_' + inv.number_electronic + '.xml'
        self.env['ir.attachment'].sudo().create({'name': inv.fname_xml_respuesta_tributacion,
                                          'type': 'binary',
                                          'datas': response_json.get('respuesta-xml'),
                                          'res_model': inv._name,
                                          'res_id': inv.id,
                                          'res_field': 'xml_respuesta_tributacion',
                                          'res_name': inv.fname_xml_respuesta_tributacion,
                                          'mimetype': 'text/xml'})

    if inv.tipo_documento != 'FEC' and estado_m_h == 'aceptado' and (not last_state or last_state == 'procesando'):
        if inv.move_type in ['in_invoice', 'in_refund']:
            email_template = self.env.ref('cr_electronic_invoice.email_template_invoice_vendor', False)
        else:
            email_template = self.env.ref('account.email_template_edi_invoice', False)

        attachments = get_invoice_attachments(inv, inv.id)
        if len(attachments) == 2:
            email_template.attachment_ids = [(6, 0, attachments)]
            try:
                email_template.with_context(type='binary', default_type='binary').send_mail(inv.id, raise_exception=False, force_send=True)
            except Exception:
                _logger.error('FECR - consulta documento error al enviar correo: %s', inv.number_electronic)
            email_template.attachment_ids = [(5, 0, 0)]


def send_message(inv, date_cr, xml, token, env):
    endpoint = fe_enums.UrlHaciendaRecepcion[env]

    vat = re.sub('[^0-9]', '', inv.partner_id.vat)
    xml_base64 = string_to_base64(xml)

    comprobante = {'clave': inv.number_electronic,
                   'consecutivoReceptor': inv.consecutive_number_receiver,
                   "fecha": date_cr,
                   'emisor': {'tipoIdentificacion': str(inv.partner_id.identification_id.code),
                              'numeroIdentificacion': vat
                              },
                   'receptor': {'tipoIdentificacion': str(inv.company_id.identification_id.code),
                                'numeroIdentificacion': inv.company_id.vat},
                   'comprobanteXml': xml_base64}

    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer {}'.format(token)}
    try:
        response = requests.post(endpoint, data=json.dumps(comprobante), headers=headers)
    except requests.exceptions.RequestException as e:
        _logger.info('Exception %s', e)
        return {'status': 400, 'text': 'Excepción de envio XML'}

    if (200 <= response.status_code <= 299):
        return {'status': response.status_code, 'text': response.text}

    _logger.error('E-INV CR - ERROR SEND MESSAGE - RESPONSE:%s', response.headers.get('X-Error-Cause', 'Unknown'))
    return {'status': response.status_code, 'text': response.headers.get('X-Error-Cause', 'Unknown')}


def load_xml_data(invoice, load_lines, account_id, product_id=False, analytic_account_id=False):
    _logger.debug('Into load_xml_data')

    try:
        invoice_xml = etree.fromstring(base64.b64decode(invoice.xml_supplier_approval))
        doc_types = 'FacturaElectronica|NotaCreditoElectronica|NotaDebitoElectronica|TiqueteElectronico'
        regex_result = re.search(doc_types, invoice_xml.tag)
        document_type = regex_result.group(0)

        if document_type == 'TiqueteElectronico':
            raise UserError("This is a TICKET only invoices are valid for taxes")

        _logger.debug('Invoice type done.')
    except Exception as e:
        raise UserError("This XML file is not XML-compliant. Error: %s" % e)

    namespaces = invoice_xml.nsmap
    inv_xmlns = namespaces.pop(None)
    namespaces['inv'] = inv_xmlns

    invoice.ref = invoice_xml.xpath("inv:NumeroConsecutivo", namespaces=namespaces)[0].text

    invoice.number_electronic = invoice_xml.xpath("inv:Clave", namespaces=namespaces)[0].text
    # v4.3 y v4.4
    activity_node = invoice_xml.xpath("inv:CodigoActividad", namespaces=namespaces) or invoice_xml.xpath("inv:CodigoActividadEmisor", namespaces=namespaces)
    activity_id = False
    activity = False
    if activity_node:
        activity = invoice.env['economic.activity'].with_context(active_test=False).search([('code', '=', activity_node[0].text)], limit=1)
        activity_id = activity.id
        _logger.debug('Activity node found done. %s', activity_id)

    invoice.economic_activity_id = activity
    invoice.date_issuance = invoice_xml.xpath("inv:FechaEmision", namespaces=namespaces)[0].text
    invoice.invoice_date = invoice.date_issuance
    invoice.tipo_documento = 'CCE'
    invoice.state_invoice_partner = '1'
    if document_type == 'NotaCreditoElectronica':
        move_type = 'in_refund'  # Nota de crédito
    else:
        move_type = 'in_invoice'
    invoice.move_type = move_type

    emisor = invoice_xml.xpath("inv:Emisor/inv:Identificacion/inv:Numero", namespaces=namespaces)[0].text
    tipo_emisor = invoice_xml.xpath("inv:Emisor/inv:Identificacion/inv:Tipo", namespaces=namespaces)[0].text
    nombre_emisor = invoice_xml.xpath("inv:Emisor/inv:Nombre", namespaces=namespaces)[0].text
    pais_emisor = invoice.env['res.country'].search([('name', '=', 'Costa Rica')], limit=1).id

    try:
        telefono_emisor = invoice_xml.xpath("inv:Emisor/inv:Telefono/inv:NumTelefono", namespaces=namespaces)[0].text
    except IndexError:
        telefono_emisor = ''
    try:
        otrassenas_emisor = invoice_xml.xpath("inv:Emisor/inv:Telefono/inv:NumTelefono", namespaces=namespaces)[0].text
    except IndexError:
        otrassenas_emisor = ''

    correo_emisor = invoice_xml.xpath("inv:Emisor/inv:CorreoElectronico", namespaces=namespaces)[0].text

    receptor_node = invoice_xml.xpath("inv:Receptor/inv:Identificacion/inv:Numero", namespaces=namespaces)
    if receptor_node:
        receptor = receptor_node[0].text
    else:
        raise UserError(_('El receptor no está definido en el xml'))

    if receptor != invoice.company_id.vat:
        raise UserError(_('El receptor no corresponde con la compañía actual con identificación ' + receptor + '. Por favor active la compañía correcta.'))

    currency_node = invoice_xml.xpath("inv:ResumenFactura/inv:CodigoTipoMoneda/inv:CodigoMoneda", namespaces=namespaces)
    if currency_node:
        invoice.currency_id = invoice.env['res.currency'].search([('name', '=', currency_node[0].text)], limit=1).id
    else:
        invoice.currency_id = invoice.env['res.currency'].search([('name', '=', 'CRC')], limit=1).id

    partner = invoice.env['res.partner'].search([('vat', '=', emisor), '|', ('company_id', '=', invoice.company_id.id), ('company_id', '=', False)], limit=1)

    if partner:
        invoice.partner_id = partner
        _logger.debug('Supplier found: %s', (partner.vat))
    else:
        new_partner = invoice.env['res.partner'].create({'name': nombre_emisor,
                                                         'vat': emisor,
                                                         'identification_id': int(tipo_emisor),
                                                         'type': 'contact',
                                                         'country_id': pais_emisor,
                                                         'phone': telefono_emisor,
                                                         'email': correo_emisor,
                                                         'street': otrassenas_emisor,
                                                         'supplier_rank': '1'})
        if new_partner:
            invoice.partner_id = new_partner
            _logger.debug('Supplier created: %s', (new_partner.vat))
        else:
            raise UserError(_('The provider in the invoice does not exists. I tried to created without success. Please review it.'))

    invoice.invoice_payment_term_id = partner.property_supplier_payment_term_id

    # v4.4: ResumenFactura/MedioPago/TipoMedioPago; v4.3: MedioPago simple
    payment_method_node = invoice_xml.xpath("inv:ResumenFactura/inv:MedioPago/inv:TipoMedioPago", namespaces=namespaces) or invoice_xml.xpath("inv:MedioPago", namespaces=namespaces)
    if payment_method_node:
        invoice.payment_methods_id = invoice.env['payment.methods'].search([('sequence', '=', payment_method_node[0].text)], limit=1)
    else:
        invoice.payment_methods_id = partner.payment_methods_id

    _logger.debug('FECR - load_lines: %s - account: %s', (load_lines, account_id))

    product = product_id or False
    analytic_account = analytic_account_id.id if analytic_account_id else False

    if load_lines:
        lines = invoice_xml.xpath("inv:DetalleServicio/inv:LineaDetalle", namespaces=namespaces)
        new_lines = []
        for line in lines:
            product_uom = invoice.env['uom.uom'].search([('code', '=', line.xpath("inv:UnidadMedida", namespaces=namespaces)[0].text)], limit=1).id
            total_amount = float(line.xpath("inv:MontoTotal", namespaces=namespaces)[0].text)

            discount_percentage = 0.0
            discount_note = None

            if total_amount > 0:
                discount_node = line.xpath("inv:Descuento", namespaces=namespaces)
                if discount_node:
                    discount_amount_node = discount_node[0].xpath("inv:MontoDescuento", namespaces=namespaces)[0]
                    discount_amount = float(discount_amount_node.text or '0.0')
                    discount_percentage = discount_amount / total_amount * 100
                    discount_note = (discount_node and discount_node[0].xpath('string(inv:NaturalezaDescuento)', namespaces=namespaces).strip()) or None
                else:
                    discount_amount_node = line.xpath("inv:MontoDescuento", namespaces=namespaces)
                    if discount_amount_node:
                        discount_amount = float(discount_amount_node[0].text or '0.0')
                        discount_percentage = discount_amount / total_amount * 100
                        discount_note = line.xpath("inv:NaturalezaDescuento", namespaces=namespaces)[0].text

            total_tax = 0.0
            taxes = []
            tax_nodes = line.xpath("inv:Impuesto", namespaces=namespaces)
            for tax_node in tax_nodes:
                tax_code = re.sub(r"[^0-9]+", "", tax_node.xpath("inv:Codigo", namespaces=namespaces)[0].text)
                tax_amount = float(tax_node.xpath("inv:Tarifa", namespaces=namespaces)[0].text)
                _logger.debug('FECR - tax_code: %s', tax_code)
                _logger.debug('FECR - tax_amount: %s', tax_amount)

                if product_id and product_id.non_tax_deductible:
                    tax = invoice.env['account.tax'].search([
                        ('tax_code', '=', tax_code),
                        ('amount', '=', tax_amount),
                        ('type_tax_use', '=', 'purchase'),
                        ('non_tax_deductible', '=', True),
                        ('active', '=', True),
                        '|',
                        ('company_id', '=', invoice.company_id.id),
                        ('company_id', '=', False),
                    ], limit=1)
                else:
                    tax = invoice.env['account.tax'].search([
                        ('tax_code', '=', tax_code),
                        ('amount', '=', tax_amount),
                        ('type_tax_use', '=', 'purchase'),
                        ('non_tax_deductible', '=', False),
                        ('active', '=', True),
                        '|',
                        ('company_id', '=', invoice.company_id.id),
                        ('company_id', '=', False),
                    ], limit=1)

                if tax:
                    total_tax += float(tax_node.xpath("inv:Monto", namespaces=namespaces)[0].text)

                    exonerations = tax_node.xpath("inv:Exoneracion", namespaces=namespaces)
                    if exonerations:
                        for exoneration_node in exonerations:
                            exoneration_percentage = float(exoneration_node.xpath("inv:PorcentajeExoneracion", namespaces=namespaces)[0].text)
                            tax = invoice.env['account.tax'].search([
                                ('percentage_exoneration', '=', exoneration_percentage),
                                ('type_tax_use', '=', 'purchase'),
                                ('non_tax_deductible', '=', False),
                                ('has_exoneration', '=', True),
                                ('active', '=', True),
                                '|',
                                ('company_id', '=', invoice.company_id.id),
                                ('company_id', '=', False),
                            ], limit=1)
                            taxes.append((4, tax.id))
                    else:
                        taxes.append((4, tax.id))
                else:
                    if product_id and product_id.non_tax_deductible:
                        raise UserError(_(str('Tax code %s and percentage %s as non-tax ', (tax_code, tax_amount)) + 'deductible is not registered in the system'))
                    raise UserError(_(str('Tax code %s and percentage %s is not ' % (tax_code, tax_amount)) + 'registered in the system'))

            _logger.debug('E-INV CR - impuestos de linea: %s', (taxes))
            columns = {'name': line.xpath("inv:Detalle", namespaces=namespaces)[0].text,
                       'move_id': invoice.id,
                       'price_unit': line.xpath("inv:PrecioUnitario", namespaces=namespaces)[0].text,
                       'quantity': line.xpath("inv:Cantidad", namespaces=namespaces)[0].text,
                       'product_uom_id': product_uom,
                       'sequence': line.xpath("inv:NumeroLinea", namespaces=namespaces)[0].text,
                       'discount': discount_percentage,
                       'discount_note': discount_note,
                       'product_id': product,
                       'account_id': account_id.id,
                       'analytic_account_id': analytic_account,
                       'economic_activity_id': activity_id,
                       'tax_ids': taxes}
            new_lines.append((0, 0, columns))

            # v4.4: Importar OtrosCargos como líneas adicionales sin impuestos.
            # Estos cargos no vienen dentro de DetalleServicio/LineaDetalle,
            # pero sí forman parte de ResumenFactura/TotalComprobante.
            other_charge_nodes = invoice_xml.xpath("inv:OtrosCargos", namespaces=namespaces)

            for other_charge_node in other_charge_nodes:
                amount_node = other_charge_node.xpath("inv:MontoCargo", namespaces=namespaces)
                if not amount_node:
                    continue

                amount = float(amount_node[0].text or '0.0')
                if not amount:
                    continue

                detail_node = other_charge_node.xpath("inv:Detalle", namespaces=namespaces)
                detail = detail_node[0].text if detail_node and detail_node[0].text else _('Otros cargos')

                columns = {
                    'name': detail,
                    'move_id': invoice.id,
                    'price_unit': amount,
                    'quantity': 1.0,
                    'sequence': len(new_lines) + 1,
                    'product_id': False,
                    'account_id': account_id.id,
                    'analytic_account_id': analytic_account,
                    'economic_activity_id': activity_id,
                    'tax_ids': [(6, 0, [])],
                }

                new_lines.append((0, 0, columns))

        invoice.invoice_line_ids = new_lines

    invoice.amount_total_electronic_invoice = invoice_xml.xpath("inv:ResumenFactura/inv:TotalComprobante", namespaces=namespaces)[0].text
    tax_node = invoice_xml.xpath("inv:ResumenFactura/inv:TotalImpuesto", namespaces=namespaces)
    if tax_node:
        invoice.amount_tax_electronic_invoice = tax_node[0].text
    invoice._compute_amount()


def p12_expiration_date(p12file, password):
    try:
        pkcs12 = crypto.load_pkcs12(base64.b64decode(p12file), password)
        data = crypto.dump_certificate(crypto.FILETYPE_PEM, pkcs12.get_certificate())
        cert = x509.load_pem_x509_certificate(data, default_backend())
        return cert.not_valid_after
    except crypto.Error as crypte:
        exc_str = str(crypte)
        if exc_str.find('mac verify failure'):
            raise
        raise
