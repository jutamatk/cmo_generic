# -*- coding: utf-8 -*-
# Copyright 2009-2017 Noviat
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import datetime
import logging

from openerp import api, fields, models, _
from openerp.addons.decimal_precision import decimal_precision as dp
from openerp.exceptions import Warning as UserError

_logger = logging.getLogger(__name__)


class AccountAssetLine(models.Model):
    _name = 'account.asset.line'
    _description = 'Asset depreciation table line'
    _order = 'type, line_date'

    name = fields.Char(string='Depreciation Name', size=64, readonly=True)
    asset_id = fields.Many2one(
        comodel_name='account.asset', string='Asset',
        required=True, ondelete='cascade')
    previous_id = fields.Many2one(
        comodel_name='account.asset.line',
        string='Previous Depreciation Line',
        readonly=True)
    parent_state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('open', 'Running'),
            ('close', 'Close'),
            ('removed', 'Removed')],
        related='asset_id.state',
        string='State of Asset')
    depreciation_base = fields.Float(
        related='asset_id.depreciation_base',
        string='Depreciation Base')
    amount = fields.Float(
        string='Amount', digits=dp.get_precision('Account'),
        required=True)
    remaining_value = fields.Float(
        compute='_compute_values',
        digits=dp.get_precision('Account'),
        string='Next Period Depreciation',
        store=True)
    depreciated_value = fields.Float(
        compute='_compute_values',
        digits=dp.get_precision('Account'),
        string='Amount Already Depreciated',
        store=True)
    line_date = fields.Date(string='Date', required=True)
    line_days = fields.Integer(
        string='Days',
        readonly=False,
    )
    move_id = fields.Many2one(
        comodel_name='account.move',
        string='Depreciation Entry', readonly=True)
    move_check = fields.Boolean(
        compute='_compute_move_check',
        string='Posted',
        store=True)
    type = fields.Selection(
        selection=[
            ('create', 'Depreciation Base'),
            ('depreciate', 'Depreciation'),
            ('remove', 'Asset Removal')],
        readonly=True, default='depreciate')
    init_entry = fields.Boolean(
        string='Initial Balance Entry',
        help="Set this flag for entries of previous fiscal years "
             "for which OpenERP has not generated accounting entries.")

    @api.depends('amount', 'previous_id')
    @api.multi
    def _compute_values(self):
        dlines = self
        if self._context.get('no_compute_asset_line_ids'):
            # skip compute for lines in unlink
            exclude_ids = self._context['no_compute_asset_line_ids']
            dlines = dlines.filtered(lambda l: l.id not in exclude_ids)
        dlines = self.filtered(lambda l: l.type == 'depreciate')
        dlines = dlines.sorted(key=lambda l: l.line_date)

        for i, dl in enumerate(dlines):
            if i == 0:
                depreciation_base = dl.depreciation_base
                depreciated_value = dl.previous_id \
                    and (depreciation_base - dl.previous_id.remaining_value) \
                    or 0.0
                remaining_value = \
                    depreciation_base - depreciated_value - dl.amount
            else:
                depreciated_value += dl.previous_id.amount
                remaining_value -= dl.amount
            dl.depreciated_value = depreciated_value
            dl.remaining_value = remaining_value

    @api.depends('move_id')
    @api.multi
    def _compute_move_check(self):
        for line in self:
            line.move_check = bool(line.move_id)

    @api.onchange('amount')
    def _onchange_amount(self):
        if self.type == 'depreciate':
            self.remaining_value = self.depreciation_base - \
                self.depreciated_value - self.amount

    @api.multi
    def write(self, vals):
        for dl in self:
            if vals.get('line_date'):
                if isinstance(vals['line_date'], datetime.date):
                    vals['line_date'] = vals['line_date'].strftime('%Y-%m-%d')
            line_date = vals.get('line_date') or dl.line_date
            asset_lines = dl.asset_id.depreciation_line_ids
            if vals.keys() == ['move_id'] and not vals['move_id']:
                # allow to remove an accounting entry via the
                # 'Delete Move' button on the depreciation lines.
                if not self._context.get('unlink_from_asset'):
                    raise UserError(_(
                        "You are not allowed to remove an accounting entry "
                        "linked to an asset."
                        "\nYou should remove such entries from the asset."))
            elif vals.keys() == ['asset_id']:
                continue
            elif dl.move_id and not self._context.get(
                    'allow_asset_line_update'):
                raise UserError(_(
                    "You cannot change a depreciation line "
                    "with an associated accounting entry."))
            elif vals.get('init_entry'):
                check = asset_lines.filtered(
                    lambda l: l.move_check and l.type == 'depreciate' and
                    l.line_date <= line_date)
                if check:
                    raise UserError(_(
                        "You cannot set the 'Initial Balance Entry' flag "
                        "on a depreciation line "
                        "with prior posted entries."))
            elif vals.get('line_date'):
                if dl.type == 'create':
                    check = asset_lines.filtered(
                        lambda l: l.type != 'create' and
                        (l.init_entry or l.move_check) and
                        l.line_date < vals['line_date'])
                    if check:
                        raise UserError(
                            _("You cannot set the Asset Start Date "
                              "after already posted entries."))
                else:
                    check = asset_lines.filtered(
                        lambda l: (l.init_entry or l.move_check) and
                        l.line_date > vals['line_date'] and l != dl)
                    if check:
                        raise UserError(_(
                            "You cannot set the date on a depreciation line "
                            "prior to already posted entries."))
        return super(AccountAssetLine, self).write(vals)

    @api.multi
    def unlink(self):
        for dl in self:
            if dl.type == 'create':
                raise UserError(_(
                    "You cannot remove an asset line "
                    "of type 'Depreciation Base'."))
            elif dl.move_id:
                raise UserError(_(
                    "You cannot delete a depreciation line with "
                    "an associated accounting entry."))
            previous = dl.previous_id
            next = dl.asset_id.depreciation_line_ids.filtered(
                lambda l: l.previous_id == dl and l not in self)
            if next:
                next.previous_id = previous
            ctx = dict(self._context, no_compute_asset_line_ids=self.ids)
        return super(
            AccountAssetLine, self.with_context(ctx)).unlink()

    def _setup_move_data(self, depreciation_date, period):
        asset = self.asset_id
        move_data = {
            'name': asset.name,
            'date': depreciation_date,
            'ref': self.name,
            'period_id': period.id,
            'journal_id': asset.profile_id.journal_id.id,
        }
        return move_data

    def _setup_move_line_data(self, depreciation_date, period,
                              account, type, move):
        asset = self.asset_id
        amount = self.amount
        analytic_id = False
        if type == 'depreciation':
            debit = amount < 0 and -amount or 0.0
            credit = amount > 0 and amount or 0.0
        elif type == 'expense':
            debit = amount > 0 and amount or 0.0
            credit = amount < 0 and -amount or 0.0
            analytic_id = asset.account_analytic_id.id
        move_line_data = {
            'name': asset.name,
            'ref': self.name,
            'move_id': move.id,
            'account_id': account.id,
            'credit': credit,
            'debit': debit,
            'period_id': period.id,
            'journal_id': asset.profile_id.journal_id.id,
            'partner_id': asset.partner_id.id,
            'analytic_account_id': analytic_id,
            'date': depreciation_date,
            'asset_id': asset.id,
            'state': 'valid',
        }
        return move_line_data

    @api.multi
    def create_move(self):
        created_move_ids = []
        asset_ids = []
        for line in self:
            asset = line.asset_id
            depreciation_date = line.line_date
            ctx = dict(self._context,
                       account_period_prefer_normal=True,
                       company_id=asset.company_id.id,
                       allow_asset=True, novalidate=True)
            period = self.env['account.period'].with_context(ctx).find(
                depreciation_date)
            am_vals = line._setup_move_data(depreciation_date, period)
            move = self.env['account.move'].with_context(ctx).create(am_vals)
            depr_acc = asset.profile_id.account_depreciation_id
            exp_acc = asset.profile_id.account_expense_depreciation_id
            aml_d_vals = line._setup_move_line_data(
                depreciation_date, period, depr_acc, 'depreciation', move)
            self.env['account.move.line'].with_context(ctx).create(aml_d_vals)
            aml_e_vals = line._setup_move_line_data(
                depreciation_date, period, exp_acc, 'expense', move)
            self.env['account.move.line'].with_context(ctx).create(aml_e_vals)
            if move.journal_id.entry_posted:
                del ctx['novalidate']
                move.with_context(ctx).post()
            write_ctx = dict(self._context, allow_asset_line_update=True)
            line.with_context(write_ctx).write({'move_id': move.id})
            created_move_ids.append(move.id)
            asset_ids.append(asset.id)
        # we re-evaluate the assets to determine if we can close them
        for asset in self.env['account.asset'].browse(
                list(set(asset_ids))):
            if asset.company_id.currency_id.is_zero(asset.value_residual):
                asset.state = 'close'
        return created_move_ids

    @api.multi
    def open_move(self):
        self.ensure_one()
        return {
            'name': _("Journal Entry"),
            'view_type': 'form',
            'view_mode': 'tree,form',
            'res_model': 'account.move',
            'view_id': False,
            'type': 'ir.actions.act_window',
            'context': self._context,
            'nodestroy': True,
            'domain': [('id', '=', self.move_id.id)],
        }

    @api.multi
    def unlink_move(self):
        for line in self:
            move = line.move_id
            if move.state == 'posted':
                move.button_cancel()
            move.with_context(unlink_from_asset=True).unlink()
            # trigger store function
            line.with_context(unlink_from_asset=True).write(
                {'move_id': False})
            if line.parent_state == 'close':
                line.asset_id.write({'state': 'open'})
            elif line.parent_state == 'removed' and line.type == 'remove':
                line.asset_id.write({'state': 'close'})
                line.unlink()
        return True
