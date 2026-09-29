from odoo import api, models
from odoo.tools.misc import get_lang


class AccountInvoiceSend(models.TransientModel):
    _inherit = 'account.invoice.send'

    # Los XML de la factura van en el correo, no en la plantilla: antes se
    # escribian en email_template.attachment_ids, un registro compartido, y el
    # siguiente envio salia con el XML de otra factura.
    def _add_electronic_xml_attachments(self):
        for wizard in self:
            if wizard.composer_id and wizard.composition_mode == 'comment' and len(wizard.invoice_ids) == 1:
                attachments = wizard.invoice_ids._get_electronic_mail_attachments()
                if attachments:
                    wizard.composer_id.attachment_ids = [(4, attachment.id) for attachment in attachments]

    @api.onchange('template_id')
    def onchange_template_id(self):
        super().onchange_template_id()
        self._add_electronic_xml_attachments()

    @api.onchange('is_email')
    def onchange_is_email(self):
        super().onchange_is_email()
        self._add_electronic_xml_attachments()

    def _send_email(self):
        self._add_electronic_xml_attachments()
        return super()._send_email()

    def send_and_print_action(self):
        self.ensure_one()
        if self.composition_mode == 'mass_mail' and self.template_id:
            for move in self.invoice_ids:
                if move.company_id.frm_ws_ambiente == 'disabled':
                    active_records = self.env[self.model].browse(move.ids)
                    langs = active_records.mapped('partner_id.lang')
                    default_lang = get_lang(self.env)
                    for lang in (set(langs) or [default_lang]):
                        active_ids_lang = active_records.filtered(lambda r: r.partner_id.lang == lang).ids
                        self_lang = self.with_context(active_ids=active_ids_lang, lang=lang)
                        self_lang.onchange_template_id()
                        self_lang._send_email()
                else:
                    if move.state_tributacion == 'aceptado':
                        move.action_invoice_sent_mass()
        else:
            self._send_email()
        if self.is_print:
            return self._print_document()
        return {'type': 'ir.actions.act_window_close'}
