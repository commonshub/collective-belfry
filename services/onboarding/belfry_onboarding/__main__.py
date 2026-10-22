"""CLI for cron: ``python -m belfry_onboarding reconcile`` and ``... expire``."""

from __future__ import annotations

import argparse
import json
import sys

from . import core
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
    else:
        result = core.expire(services)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
