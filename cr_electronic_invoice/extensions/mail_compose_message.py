

from odoo import models


class MailComposeMessage(models.TransientModel):
    _inherit = 'mail.compose.message'

    def _mark_account_moves_as_sent(self):
        context = self.env.context
        if not context.get('mass_mark_invoice_as_sent') or context.get('default_model') != 'account.move':
            return

        invoice_ids = context.get('active_ids') or []
        invoices = self.env['account.move'].browse(invoice_ids)
        invoices.write({'is_move_sent': True})

    def action_send_mail(self):
        self._mark_account_moves_as_sent()
        return super().action_send_mail()

    def _action_send_mail(self, auto_commit=False):
        self._mark_account_moves_as_sent()
        return super()._action_send_mail(auto_commit=auto_commit)
