# -*- coding: utf-8 -*-
##############################################################################
#
#  licence AGPL version 3 or later
#  see licence in __openerp__.py or http://www.gnu.org/licenses/agpl-3.0.txt
#  Copyright (C) 2018 Akretion (http://www.akretion.com).
#
##############################################################################
from odoo import models, fields, api, exceptions, _
import logging

_logger = logging.getLogger(__name__)


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    def update_locations(self, subcontract_loc):
        self.ensure_one()
        if not subcontract_loc:
            raise exceptions.ValidationError(
                _('No location configured on the subcontractor'))

        self.location_src_id = subcontract_loc
        self.location_dest_id = subcontract_loc
        self.picking_type_id = subcontract_loc.get_warehouse().manu_type_id

    @api.multi
    def add_moves_before_production(self, supplier_location_id, picking=False):
        """Change products locations according to mo's location

        Workflow
        1) a subcontracted MO in WH/Stock is created due to some procurments
        2) when a subcontracted service is bought to a supplier it changes
        the mo's locations (where raw materials are taken and produced product is put):
        (it changes from WH/Stock to A_SUPPLIER/Stock)
        3) related moves should be then changed: instead of taken goods from WH/STOCK 
        it shoud take them from A_SUPPLIER/Stock.
        WH/Stock > Virt/Production becomes
        WH/Stock > A_SUPPLIER/Stock + A_SUPPLIER/Stock > Virt/Production
        4) pickings should then be added :
        WH/Stock > A_SUPPLIER/Stock is changed to
        Virt/Inter-warehouse > A_SUPPLIER/Stock and it's added to a
        new picking A_SUPPLIER:Receipts
        Add moves between Stock and Production."""
        self.ensure_one()

        # Modify incoming move and create a second one hereafter
        # It's easier to add new moves after existing ones.
        moves_in = self.env['stock.move']
        moves_prod = self.env['stock.move']
        for move_in in self.move_raw_ids:
            if move_in.location_id == supplier_location_id:
                # don't run twice
                continue
            if move_in.procure_method == 'make_to_stock':
                # prevent picking creation
                move_in.location_id = supplier_location_id
            elif move_in.procure_method == 'make_to_order':
                sup_2_prod = self.new_move_just_before_prod(
                    move_source=move_in,
                    location=supplier_location_id)
                move_in.raw_material_production_id = False
                move_in.move_dest_id = sup_2_prod
                move_in.location_id = picking.location_id
                move_in.location_dest_id = picking.location_dest_id
                move_in.picking_id = picking

                # update the procurement
                for procurement in self.procurement_group_id.procurement_ids:
                    if procurement.move_dest_id == move_in:
                        procurement.move_dest_id = sup_2_prod
                        procurement.location_id = supplier_location_id
                sup_2_prod.action_confirm()  # don't let it in draft
                moves_prod |= sup_2_prod
            else:
                raise exceptions.UserError(_('Unkown procure method'))
            moves_in |= move_in
        return moves_in, moves_prod

    @api.multi
    def add_moves_after_production(self, location, picking=False):
        # Modify outgoing move and create a second one herebefore
        # It's easier to add new move before the existing one
        moves_out = self.env['stock.move']
        moves_prod = self.env['stock.move']

        for move_out in self.move_finished_ids:
            if move_out.location_dest_id == self.location_dest_id:
                continue
            # Production has not procure_method
            # (see mrp_production.py:_generate_finished_moves)
            if not move_out.move_dest_id:
                _logger.info(
                    'Prod fini en make to stock. On change juste la loc')
                move_out.location_dest_id = location
            else:
                _logger.info('Prod fini en make to order')
                prod_2_sup = self.new_move_just_after_prod(
                    move_source=move_out,  # virtual/prod
                    location=location)
                move_out.production_id = False
                move_out.location_id = picking.location_id
                move_out.location_dest_id = picking.location_dest_id
                move_out.picking_id = picking
                prod_2_sup.action_confirm()  # don't let it in draft
                moves_prod |= prod_2_sup
            moves_out |= move_out
        return moves_out, moves_prod


    def _prepare_copy_move(self, move_source):
        # Existing -> New + Existing 
        return {
            'name': move_source.name,
            'date': move_source.date,
            'date_expected': move_source.date_expected,
            'product_id': move_source.product_id.id,
            'product_uom': move_source.product_uom.id,
            'product_uom_qty': move_source.product_uom_qty,
            'procurement_id': move_source.procurement_id.id,
            'company_id': move_source.company_id.id,
            'origin': move_source.name,
            'group_id': move_source.group_id.id,
            'propagate': move_source.propagate,
            'location_id': False,
            'location_dest_id': False,
            'move_dest_id': False,
            'production_id': False,
        }

    def new_move_just_before_prod(self, move_source, location):
        vals = self._prepare_copy_move(move_source)
        vals['location_id'] = location.id
        vals['location_dest_id'] = move_source.location_dest_id.id
        vals['move_dest_id'] = move_source.move_dest_id.id
        vals['raw_material_production_id'] = (
            move_source.raw_material_production_id.id)
        vals['name'] = 'before prod %s' % vals['name']
        new_move = self.env['stock.move'].create(vals)
        print "move cree"
        print new_move
        return new_move

    def new_move_just_after_prod(self, move_source, location):
        vals = self._prepare_copy_move(move_source)
        vals['location_id'] = move_source.location_id.id
        vals['location_dest_id'] = location.id
        vals['location_dest_id'] = location.id
        vals['production_id'] = move_source.production_id.id
        vals['move_dest_id'] = move_source.id
        vals['name'] = 'after prod %s' % vals['name']
        new_move = self.env['stock.move'].create(vals)
        return new_move

    @api.multi
    def write(self, vals):
        """Propagate date_planned start to previous move."""

        # lorsqu'on change la date de la mo
        #  on change la date de reception associée
        # est-ce vraiment utile ??
        res = super(MrpProduction, self).write(vals)
        if 'date_planned_start' in vals:
            inter_wh = self.env.ref('stock.stock_location_inter_wh')

            for move_in in self.move_raw_ids.filtered(
                lambda r: r.state not in ['done', 'cancel']
            ):
                for previous_move in move_in.move_orig_ids:
                    if previous_move.location_id != inter_wh:
                        _logger.warning('precedent non concernee')
                        continue
                    if len(move_in.move_orig_ids) > 1:
                        _logger.warning('devrait pas arriver !')
                    previous_move.date_expected = move_in.date_expected
                    _logger.info('date du precedent pickng changee')
            _logger.info('pour le moment on fait pas les move out')
        return res
