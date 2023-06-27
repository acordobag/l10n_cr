

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
        # attachment.write({'res_model': 'mail.compose.message'})

        # decoders = self.env['account.move']._get_create_invoice_from_attachment_decoders()
        # invoice = False
        #         # for decoder in sorted(decoders, key=lambda d: d[0]):
        #         #     invoice = decoder[1](attachment)
        #         #     if invoice:
        #         #         break
        #         # if not invoice:
        invoice = self.env['account.move'].create({})
        invoice.fname_xml_supplier_approval = attachment.name
        invoice.xml_supplier_approval = attachment.datas
        try:
            invoice.load_xml_data()
            # invoice.action_post()
        except Exception as e:
            raise e

        return invoice

    def create_invoice_from_attachment(self, attachment_ids=[]):
        # [W0102(dangerous-default-value), AccountJournalInherit.create_invoice_from_attachment]
        # Dangerous default value [] as argument
        # Method defined by Odoo

        """Create the invoices from files.
        :return: A action redirecting to account.move tree/form view.
        """
        attachments = self.env['ir.attachment'].browse(attachment_ids)
        if not attachments:
            raise UserError(_("No attachment was provided"))
        invoices = self.env['account.move']
        index = 0
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
                attachment.write({'res_model': 'mail.compose.message'})
                decoders = self.env['account.move']._get_create_invoice_from_attachment_decoders()
                invoice = False
                for decoder in sorted(decoders, key=lambda d: d[0]):
                    invoice = decoder[1](attachment)
                    if invoice:
                        break
                if not invoice:
                    invoice = self.env['account.move'].create({})
                invoice.with_context(no_new_invoice=True).message_post(attachment_ids=[attachment.id])
                invoices += invoice
            index += 1
        action_vals = {
            'name': _('Generated Documents'),
            'domain': [('id', 'in', invoices.ids)],
            'res_model': 'account.move',
            'views': [[False, "tree"], [False, "form"]],
            'type': 'ir.actions.act_window',
            'context': self._context
        }

        if len(invoices) == 0:
            raise UserError("There was no invoice to process.")

        if len(invoices) == 1:
            action_vals.update({'res_id': invoices[0].id, 'view_mode': 'form'})
        else:
            action_vals['view_mode'] = 'tree,form'
        return action_vals
