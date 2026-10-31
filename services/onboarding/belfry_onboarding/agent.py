"""One action for a pass holder, signed through the holder's ``bunker://`` URL (NIP-46, NIP-98).

The command-line companion to the agent section of the skill (``skills/open-source-village``):
where ``agentcheck`` runs every step as a test, this does one thing and prints the API's JSON.

    export BELFRY_BUNKER='bunker://…'           # from the holder; not on the command line
    python -m belfry_onboarding.agent --domain <domain> me
    python -m belfry_onboarding.agent --domain <domain> invite sam@example.org [--name Sam]
    python -m belfry_onboarding.agent --domain <domain> invite [--note …]   # a code, nothing mailed
    python -m belfry_onboarding.agent --domain <domain> invites
    python -m belfry_onboarding.agent --domain <domain> status <invite id>
    python -m belfry_onboarding.agent --domain <domain> revoke <invite id>

A bunker secret binds to the first client key that connects, so the client key is kept in a
file (``--key-file``, default ``~/.config/belfry-agent/<bunker pubkey>.key``, mode 600) and
reused on every run. Exit code 0 for a 2xx answer, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from coincurve import PrivateKey

from .agentcheck import Signer, nip98
from .nip46check import HostMap, Nip46Error, parse_bunker_url


def client_key(path: Path) -> bytes:
    """The kept client key, created on the first run."""
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    key = PrivateKey().secret
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.hex())
    return key


def request(args: argparse.Namespace) -> tuple[str, str, Any]:
    """The method, the path under pass.<domain> and the JSON body of the chosen action."""
    if args.action == "me":
        return "GET", "/api/me", None
    if args.action == "invites":
        return "GET", "/api/invites", None
    if args.action == "status":
        return "GET", f"/api/invites/{args.id}", None
    if args.action == "revoke":
        return "POST", f"/api/invites/{args.id}/revoke", None
    body = {"email": args.email} if args.email else {}
    if args.name:
        body["name"] = args.name
    if args.note:
        body["note"] = args.note
    return "POST", "/api/invites", body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--domain", required=True, help="the Belfry's domain, e.g. belfry.example")
    ap.add_argument(
        "--bunker",
        default=os.environ.get("BELFRY_BUNKER", ""),
        help="the holder's bunker:// URL (default: $BELFRY_BUNKER)",
    )
    ap.add_argument("--pass-url", default="", help="default https://pass.<domain>")
    ap.add_argument("--key-file", default="", help="where the client key is kept")
    ap.add_argument("--map", default=None, help="transport host rewrites, src=dst[,src=dst]")
    ap.add_argument("--timeout", type=float, default=20.0)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("me", help="where the holder's key stands: member, steward, passes")
    inv = sub.add_parser(
        "invite", help="invite someone by e-mail (Belfry mails the link), or without one: a code"
    )
    inv.add_argument("email", nargs="?", default="")
    inv.add_argument("--name", default="")
    inv.add_argument("--note", default="")
    sub.add_parser("invites", help="the holder's invites")
    sub.add_parser("status", help="one invite: state, pass, npub once active").add_argument("id")
    sub.add_parser("revoke", help="revoke an open invite").add_argument("id")
    args = ap.parse_args(argv)
    if not args.bunker.startswith("bunker://"):
        ap.error("no bunker URL: set BELFRY_BUNKER or pass --bunker")

    bunker_pubkey = parse_bunker_url(args.bunker)[0]
    key_file = Path(args.key_file or Path.home() / ".config/belfry-agent" / f"{bunker_pubkey}.key")
    method, path, data = request(args)
    # The signed URL is the public one, exactly as the API sees it (no trailing slash).
    url = (args.pass_url or f"https://pass.{args.domain}").rstrip("/") + path
    hostmap = HostMap(args.map)
    try:
        signer = Signer(args.bunker, hostmap, args.timeout, client_key(key_file))
        try:
            body = json.dumps(data).encode() if data is not None else b""
            headers = {"Authorization": nip98(signer, url, method, body)}
            if body:
                headers["Content-Type"] = "application/json"
            with httpx.Client(timeout=args.timeout) as http:
                r = http.request(method, hostmap(url), content=body or None, headers=headers)
        finally:
            signer.close()
    except (Nip46Error, OSError, httpx.HTTPError) as exc:
        print(json.dumps({"error": "signer", "message": str(exc)}))
        return 1
    try:
        print(json.dumps(r.json(), indent=2))
    except ValueError:
        print(json.dumps({"error": f"http_{r.status_code}", "message": r.text[:300]}))
    return 0 if r.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
