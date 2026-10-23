# The activation mail template was loaded with noupdate on, so an upgrade would keep the old
# set-a-password text. Release it once; the data file (now noupdate off) then rewrites it.


def migrate(cr, version):
    cr.execute(
        "UPDATE ir_model_data SET noupdate = false"
        " WHERE module = 'belfry_pass' AND name = 'mail_template_pass_activated'"
    )
