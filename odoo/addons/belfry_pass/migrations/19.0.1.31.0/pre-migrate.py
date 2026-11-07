"""Followers' frequencies are now "as soon as possible" or monthly (daily and weekly become asap),
and one contact may follow several channels: the one-follow-per-contact constraint goes."""


def migrate(cr, version):
    cr.execute("SELECT to_regclass('belfry_follower')")
    if cr.fetchone()[0]:
        cr.execute("UPDATE belfry_follower SET frequency = 'asap' WHERE frequency IN ('daily', 'weekly')")
        cr.execute("ALTER TABLE belfry_follower DROP CONSTRAINT IF EXISTS belfry_follower_partner_unique")
