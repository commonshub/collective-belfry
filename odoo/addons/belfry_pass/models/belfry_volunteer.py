import logging
import secrets
from datetime import timedelta
from html import escape

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_application import mail_button, mail_card
from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

DEFAULT_MIN_SHIFTS = 3
# volunteers may leave a shift until this many hours before it starts
DEFAULT_CHANGE_HOURS = 48


def _param(env, key, default):
    try:
        return int(env['ir.config_parameter'].sudo().get_param(key, default) or default)
    except ValueError:
        return default


class BelfryShift(models.Model):
    """A volunteer shift (decision 2026-10-26): kept in Odoo, not on the relay, because public
    calendar events are readable by every participant and only a server can hold the capacity and
    the count that earns the volunteer pass. Volunteers and stewards see them (shift page, the
    programme app's Shifts tab, the steward pages); visitors never do."""

    _name = 'belfry.shift'
    _description = "Volunteer shift"
    _order = 'start, id'

    name = fields.Char(string="Task", required=True)
    description = fields.Text()
    start = fields.Datetime(required=True, index=True)
    end = fields.Datetime(required=True)
    place = fields.Char()
    capacity = fields.Integer(default=2, required=True)
    active = fields.Boolean(default=True)
    signup_ids = fields.One2many('belfry.shift.signup', 'shift_id')
    taken = fields.Integer(compute='_compute_taken')

    _end_after_start = models.Constraint('CHECK ("end" > start)', "A shift ends after it starts.")
    _capacity_positive = models.Constraint('CHECK (capacity > 0)', "A shift needs at least one place.")

    @api.depends('signup_ids')
    def _compute_taken(self):
        for shift in self:
            shift.taken = len(shift.signup_ids)

    def _row(self, partner_id=None):
        self.ensure_one()
        return {
            'id': self.id,
            'title': self.name,
            'description': self.description or '',
            'start': fields.Datetime.to_string(self.start),
            'end': fields.Datetime.to_string(self.end),
            'place': self.place or '',
            'capacity': self.capacity,
            'taken': self.taken,
            'people': [s.partner_id.name or '' for s in self.signup_ids],
            'mine': bool(partner_id) and partner_id in self.signup_ids.partner_id.ids,
        }

    # ------------------------------------------------------------------
    # the board and signing up (onboarding service, as the service user)
    # ------------------------------------------------------------------

    @api.model
    def _volunteer_pass(self, partner_id):
        return self.env['belfry.pass'].sudo().search(
            [('partner_id', '=', int(partner_id)), ('tier', '=', 'volunteer'),
             ('state', 'in', ('pending', 'active'))], order='id desc', limit=1)

    @api.model
    def _volunteer_info(self, belfry_pass):
        count = self.env['belfry.shift.signup'].sudo().search_count(
            [('partner_id', '=', belfry_pass.partner_id.id), ('shift_id.active', '=', True)])
        return {
            'pass_id': belfry_pass.id,
            'partner_id': belfry_pass.partner_id.id,
            'name': belfry_pass.partner_id.name or '',
            'state': belfry_pass.state,
            'count': count,
            'min_shifts': _param(self.env, 'belfry.volunteer_min_shifts', DEFAULT_MIN_SHIFTS),
            'change_hours': _param(self.env, 'belfry.volunteer_change_hours', DEFAULT_CHANGE_HOURS),
            'date_start': fields.Date.to_string(belfry_pass.date_start) if belfry_pass.date_start else False,
            'date_end': fields.Date.to_string(belfry_pass.date_end) if belfry_pass.date_end else False,
        }

    @api.model
    def belfry_volunteer_by_token(self, token):
        """The volunteer behind a shift-page link, or False."""
        _check_service_caller(self.env)
        if not token or len(token) < 20:
            return False
        belfry_pass = self.env['belfry.pass'].sudo().search(
            [('volunteer_token', '=', token), ('tier', '=', 'volunteer'),
             ('state', 'in', ('pending', 'active'))], limit=1)
        return self._volunteer_info(belfry_pass) if belfry_pass else False

    @api.model
    def belfry_volunteer_of_partner(self, partner_id):
        _check_service_caller(self.env)
        belfry_pass = self._volunteer_pass(partner_id)
        return self._volunteer_info(belfry_pass) if belfry_pass else False

    @api.model
    def belfry_shift_board(self, partner_id=False):
        """Upcoming and running shifts (ended ones are left out), with who signed up."""
        _check_service_caller(self.env)
        since = fields.Datetime.now() - timedelta(hours=12)
        shifts = self.sudo().search([('end', '>=', since)])
        return [shift._row(int(partner_id) if partner_id else None) for shift in shifts]

    @api.model
    def belfry_shift_signup(self, partner_id, shift_id):
        """Sign a volunteer up. Returns the volunteer info plus ``ready`` (the pending pass has
        reached the minimum: the caller activates it)."""
        _check_service_caller(self.env)
        belfry_pass = self._volunteer_pass(partner_id)
        if not belfry_pass:
            raise UserError("Only volunteers can sign up for shifts.")
        shift = self.sudo().browse(int(shift_id)).exists()
        if not shift or not shift.active:
            raise UserError("This shift does not exist any more.")
        self.env.cr.execute("SELECT id FROM belfry_shift WHERE id = %s FOR UPDATE", (shift.id,))
        shift.invalidate_recordset()
        partner = belfry_pass.partner_id
        if partner in shift.signup_ids.partner_id:
            raise UserError("You are signed up for this shift already.")
        if shift.end < fields.Datetime.now():
            raise UserError("This shift is over.")
        if shift.taken >= shift.capacity:
            raise UserError("This shift is full.")
        overlap = self.env['belfry.shift.signup'].sudo().search_count([
            ('partner_id', '=', partner.id), ('shift_id.active', '=', True),
            ('shift_id.start', '<', shift.end), ('shift_id.end', '>', shift.start)])
        if overlap:
            raise UserError("You have another shift at that time.")
        self.env['belfry.shift.signup'].sudo().create({'shift_id': shift.id, 'partner_id': partner.id})
        info = self._volunteer_info(belfry_pass)
        info['ready'] = belfry_pass.state == 'pending' and info['count'] >= info['min_shifts']
        _logger.info("Belfry shift %s: partner %s signed up (%s shifts)", shift.id, partner.id, info['count'])
        return info

    @api.model
    def belfry_shift_leave(self, partner_id, shift_id, steward_emails=None):
        """Leave a shift, until belfry.volunteer_change_hours before it starts. A volunteer whose
        pass is active and who drops below the minimum stays active; the stewards get a mail."""
        _check_service_caller(self.env)
        belfry_pass = self._volunteer_pass(partner_id)
        signup = self.env['belfry.shift.signup'].sudo().search(
            [('shift_id', '=', int(shift_id)), ('partner_id', '=', belfry_pass.partner_id.id)], limit=1
        ) if belfry_pass else None
        if not signup:
            raise UserError("You are not signed up for this shift.")
        hours = _param(self.env, 'belfry.volunteer_change_hours', DEFAULT_CHANGE_HOURS)
        if signup.shift_id.start - timedelta(hours=hours) < fields.Datetime.now():
            raise UserError("This shift starts in less than %d hours. Ask a steward to swap it." % hours)
        shift = signup.shift_id
        signup.unlink()
        info = self._volunteer_info(belfry_pass)
        info['below_minimum'] = belfry_pass.state == 'active' and info['count'] < info['min_shifts']
        if info['below_minimum']:
            self._mail_stewards_below_minimum(belfry_pass, shift, info, steward_emails or [])
        return info

    def _mail_stewards_below_minimum(self, belfry_pass, shift, info, emails):
        emails = sorted({e for e in emails if e and '@' in e})
        if not emails:
            return
        company = self.env.company
        self.env['mail.mail'].sudo().create({
            'subject': "Open Source Village: %s left a shift" % (belfry_pass.partner_id.name or "a volunteer"),
            'email_from': company.email_formatted or company.email,
            'email_to': ', '.join(emails),
            'body_html': "<p>%s left the shift <strong>%s</strong> (%s) and now has %d of %d shifts. "
                         "The volunteer pass stays active.</p>" % (
                             escape(belfry_pass.partner_id.name or ''), escape(shift.name),
                             escape(fields.Datetime.to_string(shift.start)), info['count'], info['min_shifts']),
            'auto_delete': False,
        })

    # ------------------------------------------------------------------
    # stewards (steward pages)
    # ------------------------------------------------------------------

    @api.model
    def belfry_steward_shift_create(self, vals):
        _check_service_caller(self.env)
        allowed = {'name', 'description', 'start', 'end', 'place', 'capacity'}
        data = {k: v for k, v in (vals or {}).items() if k in allowed}
        if not (data.get('name') or '').strip() or not data.get('start') or not data.get('end'):
            raise UserError("A shift needs a task, a start and an end.")
        return self.sudo().create(data).id

    def belfry_steward_shift_cancel(self):
        """Remove a shift; the people signed up are told by mail."""
        _check_service_caller(self.env)
        for shift in self.sudo():
            people = shift.signup_ids.partner_id.filtered('email')
            for partner in people:
                self.env['mail.mail'].sudo().create({
                    'subject': "Shift cancelled: %s" % shift.name,
                    'email_from': self.env.company.email_formatted or self.env.company.email,
                    'email_to': partner.email,
                    'body_html': "<p>Hello %s, the shift <strong>%s</strong> on %s was cancelled by the "
                                 "stewards. Please choose another one on your shift page.</p>"
                                 % (escape(partner.name or ''), escape(shift.name),
                                    escape(fields.Datetime.to_string(shift.start))),
                    'auto_delete': False,
                })
            shift.signup_ids.unlink()
            shift.active = False
        return True


class BelfryShiftSignup(models.Model):
    _name = 'belfry.shift.signup'
    _description = "Volunteer signed up for a shift"
    _order = 'id'

    shift_id = fields.Many2one('belfry.shift', required=True, ondelete='cascade', index=True)
    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade', index=True)

    _once = models.Constraint('UNIQUE(shift_id, partner_id)', "Signed up once per shift.")


class BelfryPass(models.Model):
    _inherit = 'belfry.pass'

    volunteer_token = fields.Char(copy=False, readonly=True, index=True, groups='belfry_pass.group_admin',
                                  help="Secret of the volunteer's shift page (pass.<domain>/shifts/<token>).")

    def belfry_shifts_url(self):
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        return "%s/shifts/%s" % (base, self.sudo().volunteer_token)

    @api.model
    def _belfry_create_volunteer(self, name, email):
        """A pending volunteer pass for the person with this e-mail (created when unknown), and the
        mail with the link to the shift page. Returns the pass."""
        email = (email or '').strip()
        if not email or '@' not in email:
            raise UserError("A volunteer needs an e-mail address.")
        Partner = self.env['res.partner'].sudo()
        partner = Partner.search([('email', '=ilike', email), ('is_company', '=', False)], order='id', limit=1)
        if not partner:
            partner = Partner.create({'name': (name or email).strip(), 'email': email, 'type': 'contact'})
        existing = self.sudo().search([('partner_id', '=', partner.id), ('tier', '=', 'volunteer'),
                                       ('state', 'in', ('pending', 'active'))], limit=1)
        if existing:
            return existing
        belfry_pass = self.sudo().create({
            'partner_id': partner.id, 'tier': 'volunteer', 'state': 'pending',
            'volunteer_token': secrets.token_urlsafe(32),
        })
        belfry_pass._belfry_mail_volunteer_link()
        return belfry_pass

    def _belfry_mail_volunteer_link(self):
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        minimum = _param(self.env, 'belfry.volunteer_min_shifts', DEFAULT_MIN_SHIFTS)
        p = '<p style="margin:0 0 12px;">%s</p>'
        company = self.env.company
        self.env['mail.mail'].sudo().create({
            'subject': "Choose your volunteer shifts for Open Source Village",
            'email_from': company.email_formatted or company.email,
            'email_to': self.partner_id.email,
            'body_html': mail_card(base, [
                p % ("Hello %s," % escape(self.partner_id.name or '')),
                p % ("Welcome to the volunteer team! Choose at least %d shifts: with the %s your "
                     "volunteer pass becomes active and you get your sign-in link by mail." %
                     (minimum, {1: 'first', 2: 'second', 3: 'third'}.get(minimum, '%dth' % minimum))),
                mail_button(self.belfry_shifts_url(), "Choose your shifts"),
                p % '<span style="color:#c9d1cb;">Keep this mail: the link is your shift page, also later.</span>',
            ]),
            'auto_delete': False,
        })
        cron = self.env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def belfry_steward_volunteer_add(self, name, email):
        _check_service_caller(self.env)
        return self._belfry_create_volunteer(name, email).id

    @api.model
    def belfry_steward_volunteers(self):
        """Volunteer passes with their number of shifts, for the steward pages."""
        _check_service_caller(self.env)
        Shift = self.env['belfry.shift']
        rows = []
        for belfry_pass in self.sudo().search([('tier', '=', 'volunteer')], order='id desc', limit=200):
            info = Shift._volunteer_info(belfry_pass)
            info['email'] = belfry_pass.partner_id.email or ''
            rows.append(info)
        return rows


class BelfryApplication(models.Model):
    _inherit = 'belfry.application'

    volunteer_pass_id = fields.Many2one('belfry.pass', readonly=True, ondelete='set null')

    def belfry_application_volunteer(self, steward_email):
        """Accept as a volunteer: a pending volunteer pass and the shift-page link by mail."""
        _check_service_caller(self.env)
        self.ensure_one()
        if self.state != 'new':
            raise UserError("This application was answered already (%s)." % self.state)
        belfry_pass = self.env['belfry.pass']._belfry_create_volunteer(self.name, self.email)
        self.sudo().write({'state': 'invited', 'volunteer_pass_id': belfry_pass.id,
                           'handled_by': steward_email, 'handled_at': fields.Datetime.now()})
        return belfry_pass.id
