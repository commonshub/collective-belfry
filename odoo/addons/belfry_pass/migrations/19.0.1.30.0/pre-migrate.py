"""Followers' frequencies are now "as soon as possible" or monthly: daily and weekly become asap."""


def migrate(cr, version):
    cr.execute("SELECT to_regclass('belfry_follower')")
    if cr.fetchone()[0]:
        cr.execute("UPDATE belfry_follower SET frequency = 'asap' WHERE frequency IN ('daily', 'weekly')")
