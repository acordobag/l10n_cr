
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
        return journal.type == 'purchase'

    def _create_invoice_from_xml_tree(self, filename, tree, journal=None):
        """Create a new invoice with the data inside the xml."""
        self.ensure_one()
        if self.code != 'facturx_cr_1_0':
            return super()._create_invoice_from_xml_tree(filename, tree, journal)

        invoice = self.env['account.move'].create({})
        invoice.xml_supplier_approval = tree
        invoice.fname_xml_supplier_approval = filename
        invoice.load_xml_data()
        _logger.info('CR supplier XML processed from attachment: %s', filename)
        return invoice

    def _is_facturx(self, filename, tree):
        return self.code == 'facturx_cr_1_0'
    