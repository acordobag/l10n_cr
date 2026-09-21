{
    'name': 'Costa Rica - Reporte de Conciliación IVA (D-150 / TRIBU-CR)',
    'version': '14.0.1.0.0',
    'category': 'Accounting/Localizations/Reporting',
    'summary': 'Prepara un resumen mensual de IVA por tarifa para conciliar contra el borrador de TRIBU-CR (Formulario 150)',
    'description': """
Reporte de conciliación de IVA para Costa Rica (D-150 / TRIBU-CR)
===================================================================

A partir de las facturas de venta, notas de crédito/débito y facturas de compra
registradas en Odoo, agrupa los montos por clasificación de IVA (13%, tarifas
reducidas, no sujeto/exportación, exento, exonerado) y por compras con derecho
a crédito fiscal, para un rango de fechas dado.

IMPORTANTE:
- Este reporte es una herramienta de apoyo para revisar y conciliar el IVA
  ANTES de confirmar el Formulario 150 en TRIBU-CR. TRIBU-CR prellena el
  formulario con base en los comprobantes electrónicos aceptados; este
  reporte le ayuda a verificar que esos montos calcen con su contabilidad
  en Odoo, pero no presenta la declaración ni sustituye la revisión de
  un contador.
- La clasificación de "no sujeto" (código 01) agrupa junto con las
  exportaciones de servicios; verifique con su contador el tratamiento
  fiscal exacto aplicable a su caso antes de confiar en el dato para
  efectos de crédito fiscal.
    """,
    'author': 'Adrian',
    'website': '',
    'license': 'LGPL-3',
    'depends': ['account', 'cr_electronic_invoice'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/iva_report_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
