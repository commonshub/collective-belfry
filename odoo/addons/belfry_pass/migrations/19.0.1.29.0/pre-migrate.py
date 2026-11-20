# Keycast and Authentik are replaced by Belfry's own signer and sign-in (2026-11-19):
# belfry.pass.keycast_pubkey becomes nostr_pubkey (the participant's key, wherever it is held).
# Renamed in place, with its field metadata, so no value is lost and no column is rebuilt.
# The Authentik user ids (belfry.pass.authentik_user_id, res.partner.belfry_authentik_user_id)
# are no longer fields; Odoo leaves their columns, which nothing reads any more.


def migrate(cr, version):
    cr.execute(
        "SELECT 1 FROM information_schema.columns"
        " WHERE table_name = 'belfry_pass' AND column_name = 'keycast_pubkey'"
    )
    if not cr.fetchone():
        return
    cr.execute("ALTER TABLE belfry_pass RENAME COLUMN keycast_pubkey TO nostr_pubkey")
    cr.execute(
        "UPDATE ir_model_fields SET name = 'nostr_pubkey'"
        " WHERE model = 'belfry.pass' AND name = 'keycast_pubkey'"
    )
    cr.execute(
        "UPDATE ir_model_data SET name = 'field_belfry_pass__nostr_pubkey'"
        " WHERE module = 'belfry_pass' AND name = 'field_belfry_pass__keycast_pubkey'"
    )
    cr.execute("ALTER INDEX IF EXISTS belfry_pass__keycast_pubkey_index RENAME TO belfry_pass__nostr_pubkey_index")
