"""CLI for cron: ``python -m belfry_onboarding reconcile``, ``... expire``, ``... sync-relay``,
``... sync-participants``; ``... login-link <pass id>`` for a new one-time sign-in link and
``... add-steward <e-mail> [name]`` to let someone use the steward pages (/steward)."""

from __future__ import annotations

import argparse
import json
import sys

from . import core, steward
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
    elif args.command == "add-steward":
        result = steward.add_steward(services, args.email, args.name)
    elif args.command == "login-link":
        result = {"result": core.send_login_link(services, args.pass_id), "pass_id": args.pass_id}
    else:
        result = core.expire(services)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
