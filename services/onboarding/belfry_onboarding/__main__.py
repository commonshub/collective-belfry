"""CLI for cron: ``python -m belfry_onboarding reconcile``, ``... expire``, ``... sync-relay``,
``... sync-participants``; ``... login-link <pass id>`` for a new one-time sign-in link and
``... add-steward <e-mail> [name]`` to let someone use the steward pages (/steward) and give them
their free pass, ``... steward-passes`` for the passes of all stewards;
``... publish-starter-events`` to backfill profiles, follow and relay lists on the relay;
``... sync-programme`` for the programme and room bookings as calendar events (programme.py);
``... accounting-check`` for the ticket seller's Odoo and its ids, ``... accounting-book [pass id]``
to book paid tickets there (accounting.py)."""

from __future__ import annotations

import argparse
import json
import sys

from . import accounting, core, programme, steward
from .app import build_services, configure_logging
from .settings import Settings


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
    pub = sub.add_parser(
        "homepage-publish",
        help="publish a static site (a tar archive on stdin) as the community's homepage nsite",
    )
    pub.add_argument("--title", default="")
    pub.add_argument("--by", default="", help="who publishes, for the changelog")
    sub.add_parser("homepage-status", help="what the apex serves and the published nsite")
    args = parser.parse_args(argv)
    if args.command in ("homepage-publish", "homepage-status"):
        return homepage_cli(args)

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
    elif args.command == "login-link":
        result = {"result": core.send_login_link(services, args.pass_id), "pass_id": args.pass_id}
    else:
        result = core.expire(services)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


def homepage_cli(args: argparse.Namespace) -> int:
    from . import homepage

    settings = Settings()
    configure_logging(settings.log_level)
    try:
        if args.command == "homepage-publish":
            files = homepage.files_from_tar(sys.stdin.buffer.read())
            result = homepage.publish(settings, files, args.title, args.by)
            result["serving"] = settings.homepage == "nsite"
        else:
            config = homepage.parse_config(settings)
            result = {
                "mode": config.mode,
                "proxy": config.proxy_url,
                "pubkey": config.pubkey,
                "name": config.name,
                "belfry_key": homepage.belfry_pubkey(settings),
            }
            if config.mode == "nsite":
                site = homepage.Homepage(settings, config)._resolve()
                result |= {
                    "files": len(site.paths),
                    "manifest": site.event_id,
                    "servers": site.servers,
                }
    except homepage.HomepageError as exc:
        print(f"homepage: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
