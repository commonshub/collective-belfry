from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    belfry_member_number = fields.Char(
        string="Belfry member number",
        copy=False,
        readonly=True,
        index=True,
        help="Immutable member identifier (OSV-0001). Assigned when the partner first gets a pass. "
             "Used as the subject of the pass bridge and the Authentik source link.",
    )
    belfry_authentik_user_id = fields.Integer(string="Authentik user id", copy=False, index=True)
    belfry_npub = fields.Char(string="Nostr npub", copy=False, index=True)
    # Keycast patch 0014: Belfry derives the partner's key from its custody seed with this
    # version (raise it to replace a leaked key); after a self-custody move the partner holds
    # their own key and recovery recreates a keyless account for it.
    belfry_key_version = fields.Integer(string="Key version", default=0, copy=False)
    belfry_key_custody = fields.Selection(
        [('belfry', "Belfry (recoverable)"), ('self', "Own key")],
        string="Key custody",
        default='belfry',
        copy=False,
    )
    belfry_pass_ids = fields.One2many('belfry.pass', 'partner_id', string="Belfry passes")
    belfry_pass_count = fields.Integer(compute='_compute_belfry_pass_count')

    _belfry_member_number_unique = models.Constraint(
        'UNIQUE(belfry_member_number)',
        'The Belfry member number must be unique.',
    )

    def _compute_belfry_pass_count(self):
        for partner in self:
            partner.belfry_pass_count = len(partner.belfry_pass_ids)

    def belfry_ensure_member_number(self):
        """Assign a member number from the ``belfry.member`` sequence to every partner in
        ``self`` that has none yet. Public so the onboarding service can call it over RPC
        (``execute_kw res.partner belfry_ensure_member_number [[id]]``).

        :return: the member numbers, in the order of ``self``
        :rtype: list[str]
        """
        sequence = self.env['ir.sequence'].sudo()
        for partner in self:
            if not partner.belfry_member_number:
                partner.write({'belfry_member_number': sequence.next_by_code('belfry.member')})
        return self.mapped('belfry_member_number')

    def action_view_belfry_passes(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('belfry_pass.action_belfry_pass')
        action['domain'] = [('partner_id', '=', self.id)]
        action['context'] = {'default_partner_id': self.id}
        return action
