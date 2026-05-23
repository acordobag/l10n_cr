# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResConfigSettings(models.TransientModel):

    _inherit = 'res.config.settings'

    url_base_yo_contribuyo = fields.Char(
        string="URL Base Yo Contribuyo",
        help="URL Base Yo Contribuyo",
        config_parameter='l10n_cr_hacienda_info_query.url_base_yo_contribuyo',
        default="https://api.hacienda.go.cr/fe/mifacturacorreo?",
    )

    usuario_yo_contribuyo = fields.Char(
        string="Yo Contribuyo User",
        help="Yo Contribuyo Developer Identification",
        config_parameter='l10n_cr_hacienda_info_query.usuario_yo_contribuyo',
    )

    token_yo_contribuyo = fields.Char(
        string="Yo Contribuyo Token",
        help="Yo Contribuyo Token provided by Ministerio de Hacienda",
        config_parameter='l10n_cr_hacienda_info_query.token_yo_contribuyo',
    )

    ultima_respuesta = fields.Char(
        string="Latest API response",
        help="Last API Response, this allows debugging errors if they exist",
        config_parameter='l10n_cr_hacienda_info_query.ultima_respuesta',
    )

    url_base = fields.Char(
        string="URL Base",
        help="URL Base of the END POINT",
        config_parameter='l10n_cr_hacienda_info_query.url_base',
        default="https://api.hacienda.go.cr/fe/ae?",
    )
