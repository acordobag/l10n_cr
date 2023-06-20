
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class AccountInvoiceElectronic(models.Model):
    _inherit = "account.edi.format"

    def _create_invoice_from_xml_tree(self, filename, tree, journal=None):
        """ Create a new invoice with the data inside the xml.

        :param filename: The name of the xml.
        :param tree:     The tree of the xml to import.
        :param journal:  The journal on which importing the invoice.
        :returns:        The created invoice.
        """
        _logger.debug('Into load_xml_data new')

        # TO OVERRIDE
        invoice = self.env['account.move']

        invoice.xml_supplier_approval = tree
        invoice.fname_xml_supplier_approval = filename
        invoice.load_xml_data()
        
        return super._create_invoice_from_xml_tree(filename, tree, journal)