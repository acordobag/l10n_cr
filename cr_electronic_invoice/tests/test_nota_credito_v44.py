# -*- coding: utf-8 -*-
"""Los Anexos y Estructuras v4.4 actualizados, obligatorios desde 1-nov-2026.

QUE CAMBIO
Hacienda agrego codigos de referencia para las notas de credito y debito
(13 a 16) y tipos de documento referenciado (19 y 20), y separo el ERROR
MATERIAL -el comprobante salio mal- del hecho posterior -una devolucion real-.

POR QUE HAY PRUEBAS SOBRE LOS XSD
Desde el 22-sep-2026 el modulo valida el XML contra el esquema local ANTES de
enviarlo, y corta con un UserError si no pasa. Con los esquemas viejos, una
nota con codigo 13 ni siquiera salia de Odoo: el error no venia de Hacienda,
venia de aqui. Por eso las pruebas miran el esquema y no solo el catalogo.

No tocan la red.
"""
import ast
import inspect
import os
import re

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import common, tagged

XSD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'xsd-4.4')
ESQUEMAS_CON_REFERENCIA = (
    'FacturaElectronica_V4.4.xsd', 'TiqueteElectronico_V4.4.xsd',
    'NotaCreditoElectronica_V4.4.xsd', 'NotaDebitoElectronica_V4.4.xsd',
    'FacturaElectronicaCompra_V4.4.xsd', 'FacturaElectronicaExportacion_V4.4.xsd',
)


def _enumeraciones(archivo, tipo):
    ruta = os.path.join(XSD_DIR, archivo)
    with open(ruta, encoding='utf-8', errors='ignore') as f:
        texto = f.read()
    bloque = re.search(r'<xs:simpleType name="%s">(.*?)</xs:simpleType>' % tipo,
                       texto, re.S)
    if not bloque:
        return []
    return re.findall(r'enumeration value="([^"]+)"', bloque.group(1))


@tagged('post_install', '-at_install', 'cr_electronic_invoice')
class TestCatalogosV44(common.TransactionCase):
    """El catalogo que ve el usuario y el esquema que valida tienen que decir
    lo mismo: de nada sirve poder escoger un codigo que el XSD rechaza."""

    CODIGOS_NUEVOS = {
        '13': 'Anula documento de referencia por error material',
        '14': 'Corrige monto por error material',
        '15': 'Sustituye comprobante electrónico por error material',
        '16': 'Sustituye comprobante electrónico rechazado',
    }
    TIPOS_NUEVOS = ('16', '17', '18', '19', '20')

    # ------------------------------------------------------------------ 1
    def test_los_codigos_nuevos_estan_en_el_catalogo(self):
        for codigo, nombre in self.CODIGOS_NUEVOS.items():
            registro = self.env['reference.code'].search([('code', '=', codigo)])
            self.assertEqual(len(registro), 1,
                             'falta el codigo de referencia %s' % codigo)
            self.assertTrue(registro.active, 'el codigo %s esta inactivo' % codigo)
            self.assertEqual(registro.name, nombre)

    # ------------------------------------------------------------------ 2
    def test_el_codigo_17_no_se_ofrece(self):
        """17 es "Pago a comprobante electronico".

        El esquema solo lo acepta en el Recibo Electronico de Pago, que este
        modulo todavia no emite. Ofrecerlo seria ofrecer un codigo que la
        validacion local rechaza: el usuario escoge, confirma y choca.
        """
        self.assertFalse(self.env['reference.code'].search([('code', '=', '17')]))
        for archivo in ESQUEMAS_CON_REFERENCIA:
            self.assertNotIn(
                '17', _enumeraciones(archivo, 'CodigoReferenciaType'),
                '%s acepta el 17: si Hacienda lo habilito aqui, hay que '
                'agregarlo al catalogo' % archivo)

    # ------------------------------------------------------------------ 3
    def test_los_tipos_de_documento_nuevos_estan_en_el_catalogo(self):
        for codigo in self.TIPOS_NUEVOS:
            registro = self.env['reference.document'].search([('code', '=', codigo)])
            self.assertEqual(len(registro), 1,
                             'falta el tipo de documento de referencia %s' % codigo)
            self.assertTrue(registro.active)

    # ------------------------------------------------------------------ 4
    def test_los_nombres_que_hacienda_corrigio(self):
        tipo = self.env['reference.document'].search([('code', '=', '10')])
        self.assertEqual(
            tipo.name, 'Comprobante electrónico rechazado por el Ministerio de Hacienda')
        tipo14 = self.env['reference.document'].search([('code', '=', '14')])
        self.assertIn('Régimen Especial', tipo14.name)

    # ------------------------------------------------------------------ 5
    def test_el_esquema_acepta_los_codigos_nuevos(self):
        """La prueba que habria atrapado el bloqueo."""
        for archivo in ESQUEMAS_CON_REFERENCIA:
            codigos = _enumeraciones(archivo, 'CodigoReferenciaType')
            self.assertTrue(codigos, 'no se leyo CodigoReferenciaType en %s' % archivo)
            for codigo in self.CODIGOS_NUEVOS:
                self.assertIn(codigo, codigos,
                              '%s no acepta el codigo %s: el esquema esta '
                              'desactualizado y la nota no va a salir de Odoo'
                              % (archivo, codigo))

    # ------------------------------------------------------------------ 6
    def test_el_esquema_sigue_aceptando_los_codigos_de_siempre(self):
        """Actualizar el esquema no puede quitar nada: hay comprobantes vivos
        con esos codigos."""
        de_siempre = ['01', '02', '04', '05', '06', '07', '08', '09', '10',
                      '11', '12', '99']
        for archivo in ESQUEMAS_CON_REFERENCIA:
            codigos = _enumeraciones(archivo, 'CodigoReferenciaType')
            for codigo in de_siempre:
                self.assertIn(codigo, codigos, '%s dejo de aceptar el %s'
                              % (archivo, codigo))

    # ------------------------------------------------------------------ 7
    def test_el_esquema_acepta_los_tipos_19_y_20(self):
        for archivo in ESQUEMAS_CON_REFERENCIA:
            tipos = _enumeraciones(archivo, 'TipoDocReferenciaType')
            self.assertIn('19', tipos, '%s no acepta el tipo 19' % archivo)
            self.assertIn('20', tipos, '%s no acepta el tipo 20' % archivo)

    # ------------------------------------------------------------------ 8
    def test_el_reintento_de_una_rechazada_usa_el_codigo_16(self):
        """Antes usaba el 04, que no decia que el original fue rechazado."""
        move = self.env['account.move']
        self.assertEqual(move.FE_RETRY_REFERENCE_CODE, '16')
        self.assertEqual(move.FE_RETRY_REFERENCE_DOCUMENT_CODE, '10')
        codigo = self.env['reference.code'].search(
            [('code', '=', move.FE_RETRY_REFERENCE_CODE)])
        self.assertTrue(codigo and codigo.active,
                        'el reintento apunta a un codigo que no existe')


@tagged('post_install', '-at_install', 'cr_electronic_invoice')
class TestPeriodoErrorMaterial(common.TransactionCase):
    """Con los codigos 13 y 14 el ajuste cae en el periodo del comprobante que
    corrige, no en el mes en que se emite la nota."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'CLIENTE NC V44'})
        self.codigo_14 = self.env['reference.code'].search([('code', '=', '14')])
        self.codigo_01 = self.env['reference.code'].search([('code', '=', '01')])
        self.tipo_01 = self.env['reference.document'].search([('code', '=', '01')])

    def _factura(self, fecha, monto=1000.0):
        vals = {
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': fields.Date.to_date(fecha),
            'date': fields.Date.to_date(fecha),
            'tipo_documento': 'disabled',
            'invoice_line_ids': [(0, 0, {
                'name': 'Articulo', 'quantity': 1, 'price_unit': monto})],
        }
        factura = self.env['account.move'].create(vals)
        factura.action_post()
        return factura

    def _nota(self, original, codigo, fecha_emision='2026-11-02'):
        return self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner.id,
            'invoice_date': fields.Date.to_date(fecha_emision),
            'date': fields.Date.to_date(fecha_emision),
            'tipo_documento': 'disabled',
            'invoice_id': original.id,
            'reference_code_id': codigo.id,
            'reference_document_id': self.tipo_01.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Ajuste', 'quantity': 1, 'price_unit': 1000.0})],
        })

    # ------------------------------------------------------------------ 1
    def test_codigo_14_lleva_la_nota_al_periodo_de_la_factura(self):
        """Noviembre corrige agosto: el IVA que se ajusta es el de agosto."""
        factura = self._factura('2026-08-20')
        nota = self._nota(factura, self.codigo_14)

        nota._align_accounting_date_with_reference()

        self.assertEqual(nota.date, fields.Date.to_date('2026-08-20'),
                         'la fecha contable debe caer en el periodo de la factura')
        self.assertEqual(nota.invoice_date, fields.Date.to_date('2026-11-02'),
                         'la fecha de emision NO se toca: es la que va a Hacienda')

    # ------------------------------------------------------------------ 2
    def test_codigo_01_se_queda_en_el_mes_de_emision(self):
        """Una devolucion o una anulacion por un hecho posterior ocurrio
        cuando ocurrio."""
        factura = self._factura('2026-08-20')
        nota = self._nota(factura, self.codigo_01)

        nota._align_accounting_date_with_reference()

        self.assertEqual(nota.date, fields.Date.to_date('2026-11-02'))

    # ------------------------------------------------------------------ 3
    def test_sin_documento_de_referencia_no_hace_nada(self):
        nota = self._nota(self._factura('2026-08-20'), self.codigo_14)
        nota.invoice_id = False

        nota._align_accounting_date_with_reference()

        self.assertEqual(nota.date, fields.Date.to_date('2026-11-02'))

    # ------------------------------------------------------------------ 4
    def test_con_el_periodo_cerrado_avisa_en_vez_de_mentir(self):
        """Ni mueve la fecha a un periodo cerrado ni la deja en el mes
        equivocado callando: la salida la decide el contador."""
        factura = self._factura('2026-08-20')
        nota = self._nota(factura, self.codigo_14)
        self.env.company.fiscalyear_lock_date = fields.Date.to_date('2026-09-30')

        with self.assertRaises(UserError):
            nota._align_accounting_date_with_reference()

        self.assertEqual(nota.date, fields.Date.to_date('2026-11-02'),
                         'si no se pudo mover, no se movio')

    # ------------------------------------------------------------------ 5
    def test_la_misma_fecha_no_genera_ruido(self):
        """Una nota del mismo mes no deja un mensaje en el historial."""
        factura = self._factura('2026-11-02')
        nota = self._nota(factura, self.codigo_14)
        antes = len(nota.message_ids)

        nota._align_accounting_date_with_reference()

        self.assertEqual(len(nota.message_ids), antes)

    # ------------------------------------------------------------------ 6
    def test_action_post_llama_al_ajuste_de_periodo(self):
        """Estatica, igual que las de timeout: que nadie quite la llamada.

        La ruta real de action_post exige una compania con firma, PIN y
        credenciales de Hacienda, que esta base de pruebas no tiene. Lo que se
        puede fijar aqui es que la llamada siga en el codigo.
        """
        from odoo.addons.cr_electronic_invoice.models import account_move
        fuente = inspect.getsource(account_move.AccountInvoiceElectronic.action_post)
        arbol = ast.parse(fuente.lstrip())
        llamadas = {n.func.attr for n in ast.walk(arbol)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn('_align_accounting_date_with_reference', llamadas,
                      'action_post ya no ajusta el periodo de las notas con '
                      'codigo 13 o 14')
