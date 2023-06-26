
import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class AccountInvoiceElectronic(models.Model):
    _inherit = "account.edi.format"

    def _is_compatible_with_journal(self, journal):
        self.ensure_one()
        res = super()._is_compatible_with_journal(journal)
        if self.code != 'facturx_cr_1_0':
            return res
        return journal.type == 'sale'

    def _create_invoice_from_xml_tree(self, filename, tree, journal=None):
        """ Create a new invoice with the data inside the xml.

        :param filename: The name of the xml.
        :param tree:     The tree of the xml to import.
        :param journal:  The journal on which importing the invoice.
        :returns:        The created invoice.
        """
        _logger.debug('Into load_xml_data new')
        self.ensure_one()

        # TO OVERRIDE
        invoice = self.env['account.move']

        invoice.xml_supplier_approval = tree
        invoice.fname_xml_supplier_approval = filename
        invoice.load_xml_data()
        
        return super()._create_invoice_from_xml_tree(filename, tree, journal)
    
    def _is_facturx(self, filename, tree):
        return self.code == 'facturx_1_0_05'
    