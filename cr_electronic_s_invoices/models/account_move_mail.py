import base64
import logging

from lxml import etree

from odoo import api, models

_logger = logging.getLogger(__name__)


class AccountMoveMail(models.Model):
    _inherit = "account.move"

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        record = super().message_new(msg_dict, custom_values=custom_values)

        try:
            record._process_cr_supplier_xml_from_incoming_email(msg_dict)
        except Exception as e:
            _logger.exception(
                "CR FE: error processing incoming email XML on message_new: %s",
                e
            )

        return record

    def message_update(self, msg_dict, update_vals=None):
        res = super().message_update(msg_dict, update_vals=update_vals)

        try:
            self._process_cr_supplier_xml_from_incoming_email(msg_dict)
        except Exception as e:
            _logger.exception(
                "CR FE: error processing incoming email XML on message_update: %s",
                e
            )

        return res

    def _content_to_base64(self, content):
        """
        Convert incoming email attachment content to base64 for ir.attachment.datas.

        Odoo ir.attachment.datas expects base64-encoded content.
        Incoming mail attachments may arrive as:
        - bytes
        - str / unicode text
        """
        if not content:
            return False

        if isinstance(content, str):
            content = content.encode('utf-8')

        return base64.b64encode(content).decode('ascii')

    def _get_xml_root_name_from_content(self, content):
        """
        Return XML root local name from incoming email attachment content.

        Examples:
        - FacturaElectronica
        - NotaCreditoElectronica
        - MensajeHacienda
        """
        if not content:
            return False

        if isinstance(content, str):
            content = content.encode('utf-8')

        try:
            root = etree.fromstring(content)
            return etree.QName(root).localname
        except Exception as e:
            _logger.warning(
                "CR FE: could not parse XML attachment content, skipping. Error: %s",
                e
            )
            return False

    def _is_supplier_invoice_xml(self, content):
        """
        Only process real supplier invoice XMLs.

        This intentionally processes only:
        - FacturaElectronica

        This skips:
        - MensajeHacienda / respuesta XML
        - NotaCreditoElectronica
        - NotaDebitoElectronica
        - TiqueteElectronico
        - any other XML file
        """
        root_name = self._get_xml_root_name_from_content(content)

        if root_name != 'FacturaElectronica':
            _logger.info(
                "CR FE: skipping XML attachment because root is '%s', not FacturaElectronica.",
                root_name
            )
            return False

        return True

    def _process_cr_supplier_xml_from_incoming_email(self, msg_dict):
        self.ensure_one()

        attachments = msg_dict.get('attachments') or []

        if not attachments:
            _logger.info(
                "CR FE: incoming email without attachments, skipping XML import."
            )
            return

        company = self.company_id or self.env.company

        default_journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', company.id),
        ], limit=1)

        if not default_journal:
            default_journal = self.env['account.journal'].search([
                ('type', '=', 'purchase'),
                ('company_id', '=', False),
            ], limit=1)

        if not default_journal:
            _logger.warning(
                "CR FE: no purchase journal found, skipping XML email processing."
            )
            return

        ir_attachment = self.env['ir.attachment']
        created_invoices = self.env['account.move']

        for attachment in attachments:
            filename = attachment[0] if len(attachment) > 0 else False
            content = attachment[1] if len(attachment) > 1 else False

            if not filename or not content:
                continue

            if not filename.lower().endswith('.xml'):
                continue

            if not self._is_supplier_invoice_xml(content):
                _logger.info(
                    "CR FE: skipped non-invoice XML attachment from email: %s",
                    filename
                )
                continue

            _logger.info(
                "CR FE: processing incoming FacturaElectronica XML attachment from email: %s",
                filename
            )

            try:
                new_attachment = ir_attachment.create({
                    'name': filename,
                    'datas': self._content_to_base64(content),
                    'type': 'binary',
                    'res_model': 'account.move',
                    'res_id': self.id,
                    'mimetype': 'application/xml',
                })
            except Exception as e:
                _logger.exception(
                    "CR FE: failed to create ir.attachment for XML '%s': %s",
                    filename,
                    e
                )
                continue

            try:
                journal_company = default_journal.company_id or self.env.company

                allowed_company_ids = list(self.env.context.get('allowed_company_ids') or [])
                if journal_company.id not in allowed_company_ids:
                    allowed_company_ids.append(journal_company.id)

                action = default_journal.with_company(journal_company).with_context(
                    allowed_company_ids=allowed_company_ids,
                ).create_invoice_from_attachment([
                    new_attachment.id
                ])

                if action and action.get('res_id'):
                    created_invoices |= self.env['account.move'].browse(
                        action['res_id']
                    )

            except Exception as e:
                _logger.exception(
                    "CR FE: failed to create invoice from XML attachment '%s': %s",
                    filename,
                    e
                )

        if created_invoices:
            _logger.info(
                "CR FE: created %s invoice(s) from incoming email XML(s): %s",
                len(created_invoices),
                created_invoices.ids
            )
        else:
            _logger.info(
                "CR FE: no invoices were created from incoming email XML attachments."
            )