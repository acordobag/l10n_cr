

from odoo import models, fields, _
from odoo.exceptions import UserError
import base64
from lxml import etree
import re
import logging

_logger = logging.getLogger(__name__)


class AccountJournalInherit(models.Model):
    _name = 'account.journal'
    _inherit = 'account.journal'

    sucursal = fields.Integer(default="1")
    terminal = fields.Integer(default="1")
    FE_sequence_id = fields.Many2one("ir.sequence", string="Electronic Invoice Sequence")
    TE_sequence_id = fields.Many2one("ir.sequence", string="Electronic Ticket Sequence")
    FEE_sequence_id = fields.Many2one("ir.sequence", string="Sequence of Electronic Export Invoices")
    NC_sequence_id = fields.Many2one("ir.sequence", string="Electronic Credit Notes Sequence")
    ND_sequence_id = fields.Many2one("ir.sequence", string="Electronic Debit Notes Sequence")
    expense_product_id = fields.Many2one('product.product',
                                         string="Default product for expenses when loading data from XML",
                                         help="The default product used when loading Costa Rican digital invoice")
    expense_account_id = fields.Many2one('account.account',
                                         string="Default Expense Account when loading data from XML",
                                         help="The expense account used when loading Costa Rican digital invoice")
    expense_analytic_account_id = fields.Many2one('account.analytic.account',
                                                  string="Default Analytic Account for expenses "
                                                  "when loading data from XML",
                                                  help="The analytic account used when loading "
                                                  "Costa Rican digital invoice")
    load_lines = fields.Boolean(string="Indicates if invoice lines should be load when loading a "
                                "Costa Rican Digital Invoice", default=True)

    def invoice_from_xml(self, attachment):
        try:
            invoice_xml = etree.fromstring(base64.b64decode(attachment.datas))
            document_names = "FacturaElectronica|NotaCreditoElectronica|NotaDebitoElectronica|TiqueteElectronico"
            document_type = re.search(document_names, invoice_xml.tag).group(0)
            if document_type == 'TiqueteElectronico':
                _logger.exception('This is a TICKET only invoices are valid for taxes')
                # return False
                # raise UserError(_("This is a TICKET only invoices are valid for taxes"))

            namespaces = invoice_xml.nsmap
            inv_xmlns = namespaces.pop(None)
            namespaces['inv'] = inv_xmlns
            number_electronic = invoice_xml.xpath("inv:Clave", namespaces=namespaces)[0].text

            result = self.env['account.move'].search([('number_electronic', '=', number_electronic), '|',
                                                         ('company_id', '=', self.env.user.company_id.id),
                                                         ('company_id', '=', False)], limit=1)

            if result:
                raise UserError("Duplicate invoice")
        except Exception as e:
            _logger.exception('FECR: ERROR Importing invoice %s', e)
            # return False
            raise UserError(_("This XML file is not XML-compliant. Error: %s") % e)
        if self.type != 'purchase':
            raise UserError(_(
                "El XML de proveedor debe importarse desde un diario de compras. "
                "Diario actual: %s"
            ) % self.display_name)

        move_type = 'in_refund' if document_type == 'NotaCreditoElectronica' else 'in_invoice'

        invoice = self.env['account.move'].with_context(default_move_type=move_type).create({
            'move_type': move_type,
            'journal_id': self.id,
            'fname_xml_supplier_approval': attachment.name,
            'xml_supplier_approval': attachment.datas,
        })
        try:
            invoice.load_xml_data()
            invoice.action_post()
        except Exception as e:
            raise e

        return invoice

    def _create_document_from_attachment(self, attachment_ids):
        """Create vendor bills from attachments, keeping Costa Rican XML support."""
        attachments = self.env['ir.attachment'].browse(attachment_ids)
        if not attachments:
            raise UserError(_("No attachment was provided"))
        invoices = self.env['account.move']
        for attachment in attachments:

            if ".xml" in attachment.name or ".XML" in attachment.name:
                try:
                    invoice = self.invoice_from_xml(attachment)
                except Exception as e:
                    _logger.exception('FECR: ERROR Importing invoice %s', e)
                    if len(attachments) == 1:
                        raise UserError(_("Error: %s") % e)
                    invoice = False
                if invoice:
                    invoices += invoice
            else:
                invoices += self.env['account.move'].with_context(
                    default_journal_id=self.id,
                    default_move_type='in_invoice',
                )._create_records_from_attachments(attachment)
        if len(invoices) == 0:
            raise UserError("There was no invoice to process.")

        return invoices
