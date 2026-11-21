"""CLI for cron: ``python -m belfry_onboarding reconcile``, ``... expire``, ``... sync-relay``,
``... sync-participants``; ``... login-link <pass id>`` for a new one-time sign-in link and
``... add-steward <e-mail> [name]`` to let someone use the steward pages (/steward) and give them
their free pass, ``... steward-passes`` for the passes of all stewards;
``... publish-starter-events`` to backfill profiles, follow and relay lists on the relay;
``... sync-programme`` for the programme and room bookings as calendar events (programme.py);
``... accounting-check`` for the ticket seller's Odoo and its ids, ``... accounting-book [pass id]``
to book paid tickets there (accounting.py); ``... setup-reset [--all-admins] [--dry-run]`` to
open /setup again after a stranger claimed a new belfry (setup.reset; prints JSON, run by
``belfry setup-reset``)."""

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
        "import-stewards",
        help="copy Authentik's stewards group into the register (for a belfry leaving Authentik; "
        "needs AUTHENTIK_URL and AUTHENTIK_TOKEN; safe to run again)",
    )
    sub.add_parser(
        "usernames", help="members whose username is still a number get their name as a slug"
    )
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
    rst = sub.add_parser(
        "setup-reset",
        help="remove the first admin so /setup is open again (JSON out; belfry setup-reset)",
    )
    rst.add_argument("--all-admins", action="store_true", help="remove every admin")
    rst.add_argument("--dry-run", action="store_true", help="only say what would be done")
    sub.add_parser("sales-status", help="whether this belfry can sell passes (no values)")
    mig = sub.add_parser(
        "signer-migrate",
        help="move from Keycast to Belfry's own signer: plan (read-only), import, verify; the "
        "rows of Keycast's database as JSON on stdin (infra/scripts/migrate-to-own-signer.sh)",
    )
    mig.add_argument("phase", choices=["plan", "import", "verify"])
    mig.add_argument(
        "--exclude",
        action="append",
        metavar="PUBKEY",
        help="leave this Keycast account out on purpose (hex; may be repeated)",
    )
    mig.add_argument(
        "--accept-lost-sessions",
        type=int,
        default=0,
        metavar="N",
        help="exactly N live sessions are signed out because another account keeps their "
        "client key: seen and accepted",
    )
    mig.add_argument(
        "--accept-missing-stewards",
        type=int,
        default=0,
        metavar="N",
        help="exactly N admins in Authentik have no person in the register: seen and accepted",
    )
    mig.add_argument(
        "--no-stewards", action="store_true", help="Authentik is gone: leave the admins out"
    )
    mig.add_argument(
        "--keys-only",
        action="store_true",
        help="verify after the switch: every key still equal; what moves on afterwards "
        "(names, states, sessions, admins) is not compared",
    )
    sub.add_parser(
        "has-people",
        help="yes or no: the register has, or ever had, a member (belfry config asks before "
        "it lets the signer be switched)",
    )
    pl = sub.add_parser(
        "pay-link",
        help="a signed belfry.<domain>/pay link (its description shows; belfry pay-link)",
    )
    pl.add_argument("--amount", default="", help="in euros, e.g. 12.50")
    pl.add_argument("--communication", default="")
    pl.add_argument("--description", default="")
    imp = sub.add_parser(
        "register-import",
        help="copy people, passes and invites from Odoo into the belfry's own register",
    )
    imp.add_argument("--force", action="store_true", help="replace what the register holds")
    args = parser.parse_args(argv)
    if args.command in ("homepage-publish", "homepage-status"):
        return homepage_cli(args)

    settings = Settings()
    configure_logging(settings.log_level)
    if args.command == "register-import":
        from .clients.odoo import OdooClient
        from .register import Register
        from .register_import import run as register_import

        odoo = OdooClient(
            settings.odoo_url, settings.odoo_db, settings.odoo_user, settings.odoo_api_key
        )
        print(register_import(odoo, Register(settings.register_path, settings), args.force))
        return 0
    services = build_services(settings)
    if args.command == "sales-status":
        from . import sales

        print(
            sales.status_line(services)
            if sales.own(services)
            else "Selling passes: off (Odoo register)."
        )
        return 0
    if args.command == "pay-link":
        from . import sales, salespay

        if not sales.bank_ready(services):
            print("Set the bank details first (belfry.<domain>/sales)", file=sys.stderr)
            return 2
        try:
            print(salespay.make_link(services, args.amount, args.communication, args.description))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
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
    elif args.command == "setup-reset":
        from . import setup

        print(json.dumps(setup.reset(services, args.all_admins, args.dry_run)))
        return 0
    elif args.command == "signer-migrate":
        from . import migration

        return migration.cli(args, services, settings, sys.stdin, sys.stdout, sys.stderr)
    elif args.command == "has-people":
        from . import signerguard

        print("yes" if signerguard.has_people(services) else "no")
        return 0
    elif args.command == "usernames":
        from . import usernames

        result = usernames.backfill(services)
    elif args.command == "import-stewards":
        from . import stewards
        from .clients.authentik import AuthentikClient

        if not settings.authentik_token:
            print("AUTHENTIK_TOKEN is not set: nothing to read the stewards from", file=sys.stderr)
            return 2
        result = stewards.import_from_authentik(
            services, AuthentikClient(settings.authentik_url, settings.authentik_token)
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
            try:  # a line in #website: who updated the website (website.py)
                from .app import build_services
                from .website import changelog

                svc = build_services(settings)
                changelog(
                    svc,
                    f"**Website updated** by {args.by or 'an admin'} from the server "
                    f"({len(files)} files): https://{settings.nip05_domain}",
                )
            except Exception as exc:  # noqa: BLE001 — the site is published either way
                print(f"(changelog not posted: {exc})", file=sys.stderr)
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
