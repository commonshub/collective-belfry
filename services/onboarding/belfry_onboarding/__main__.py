"""CLI for cron: ``python -m belfry_onboarding reconcile``, ``... expire``, ``... sync-relay``,
``... sync-participants``; ``... login-link <pass id>`` for a new one-time sign-in link and
``... add-steward <e-mail> [name]`` to let someone use the steward pages (/steward) and give them
their free pass, ``... steward-passes`` for the passes of all stewards;
``... publish-starter-events`` to backfill profiles, follow and relay lists on the relay;
``... sync-programme`` for the programme and room bookings as calendar events (programme.py);
``... accounting-check`` for the ticket seller's Odoo and its ids, ``... accounting-book [pass id]``
to book paid tickets there (accounting.py);
``... admins [list|add|remove] [npub]`` and ``... grant <npub> [--role R] [--days N]``,
``... revoke <npub>``: access events signed with the service key, the way to change admins with
shell access (access.py)."""

from __future__ import annotations

import argparse
import json
import sys

from . import access, accounting, core, programme, steward
from .app import build_services, configure_logging
from .nostr import hex_to_npub
from .settings import Settings


def access_command(services, settings: Settings, args) -> dict | None:
    """The operator's access events, signed with the service key: they count whoever the admins
    are, so shell access to the Belfry can always change them."""
    svc = access.from_settings(
        settings, stewards=lambda: core.steward_pubkeys(services), on_members=lambda _m: None
    )
    if svc is None:
        print("GROUPS_RELAY_URL, GROUPS_API_NSEC or ACCESS_ENABLED is not set", file=sys.stderr)
        return None
    svc.load_once()
    pubkey = access.parse_pubkey(args.pubkey) if getattr(args, "pubkey", None) else None
    action = getattr(args, "action", None)
    if args.command == "admins" and action in ("add", "remove") and pubkey is None:
        print("admins add|remove needs an npub", file=sys.stderr)
        return None
    if args.command == "admins" and action == "add":
        svc.set_role(pubkey, access.ADMIN)
    elif args.command == "admins" and action == "remove":
        svc.revoke(pubkey, "admin removed by the operator")
    elif args.command == "grant":
        svc.set_role(pubkey, args.role.lower(), args.days)
    elif args.command == "revoke":
        svc.revoke(pubkey)
    state = svc.public_state()
    return {
        "admins": [hex_to_npub(p) for p in state["admins"]],
        "members": len(svc.members()),
        "pending_requests": len(svc.state.pending(svc.now())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="belfry_onboarding")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "reconcile", help="activate paid invoices without an active pass, revoke refunded ones"
    )
    sub.add_parser("expire", help="expire active passes whose date_end has passed")
    sub.add_parser("sync-relay", help="rewrite the relay whitelist from the active passes")
    sub.add_parser(
        "sync-participants",
        help="make the Authentik participants group equal to the holders of active passes",
    )
    link = sub.add_parser(
        "login-link", help="mint and store a new one-time sign-in link for an active pass"
    )
    link.add_argument("pass_id", type=int)
    add = sub.add_parser(
        "add-steward", help="create or find the Authentik user and add it to the stewards group"
    )
    add.add_argument("email")
    add.add_argument("name", nargs="?", default="")
    sub.add_parser("steward-passes", help="give every member of the stewards group their free pass")
    admins = sub.add_parser("admins", help="list, add or remove admins (access events)")
    admins.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    admins.add_argument("pubkey", nargs="?", help="npub or hex")
    grant = sub.add_parser("grant", help="grant access to a key (an access event)")
    grant.add_argument("pubkey", help="npub or hex")
    grant.add_argument("--role", default="member")
    grant.add_argument("--days", type=int, default=None, help="valid for this many days")
    rev = sub.add_parser("revoke", help="end a key's granted access (an access event)")
    rev.add_argument("pubkey", help="npub or hex")
    sub.add_parser(
        "publish-starter-events",
        help="publish the missing profile, follow and relay lists of every active pass",
    )
    sub.add_parser(
        "sync-programme",
        help="publish the programme and the rooms' bookings as public calendar events",
    )
    sub.add_parser(
        "restore-keys",
        help="recreate every partner's Keycast account from Odoo (custody seed unlocked)",
    )
    sub.add_parser(
        "accounting-check",
        help="connect to the ticket seller's Odoo: the configured ids and the candidates",
    )
    book = sub.add_parser(
        "accounting-book",
        help="book a paid ticket at the ticket seller (all that are missing without a pass id)",
    )
    book.add_argument("pass_id", type=int, nargs="?")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    services = build_services(settings)
    if args.command == "reconcile":
        result = core.reconcile(services)
    elif args.command == "sync-relay":
        if services.whitelist is None:
            print("RELAY_CONFIG_PATH is not set", file=sys.stderr)
            return 2
        result = core.sync_relay_whitelist(services, strict=True)
    elif args.command == "sync-participants":
        result = core.sync_participants(services, strict=True)
    elif args.command == "publish-starter-events":
        if services.relay is None:
            print("RELAY_URL is not set", file=sys.stderr)
            return 2
        result = core.publish_starter_events(services)
    elif args.command == "add-steward":
        result = steward.add_steward(
            services, args.email, args.name, steward.activation_via_hook(services)
        )
    elif args.command == "steward-passes":
        result = steward.steward_passes(services, steward.activation_via_hook(services))
    elif args.command == "sync-programme":
        result = programme.sync(services)
    elif args.command == "restore-keys":
        result = core.restore_keys(services)
    elif args.command in ("accounting-check", "accounting-book"):
        if services.accounting is None:
            print(
                "ACCOUNTING_ODOO_URL or the accounting_odoo_api_key secret is not set",
                file=sys.stderr,
            )
            return 2
        if args.command == "accounting-check":
            result = accounting.check(services.accounting)
        elif args.pass_id:
            result = {"result": accounting.book_pass(services, args.pass_id)}
        else:
            result = accounting.reconcile(services)
    elif args.command in ("admins", "grant", "revoke"):
        result = access_command(services, settings, args)
        if result is None:
            return 2
    elif args.command == "login-link":
        result = {"result": core.send_login_link(services, args.pass_id), "pass_id": args.pass_id}
    else:
        result = core.expire(services)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
