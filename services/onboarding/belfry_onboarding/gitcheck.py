"""An agent works on code at the village's git server, with nothing but the holder's bunker URL.

The smoke test's check of ``git.<domain>`` (ngit-grasp, private GRASP-08 service; see
``infra/git/README.md``) and a reference for the git part of the skill. Everything is signed by
the holder's key through the bunker: NIP-42 AUTH, a repository announcement (kind 30617) and its
state (30618), the NIP-98 credential of the ``git push`` and ``git clone``, an issue (1621) and a
comment on it (1111). A key without a pass is refused by the relay, and git without a credential
gets 401. ngit does all of this for agents; this module does it by hand to show what is checked.

    python -m belfry_onboarding.gitcheck "<bunker url>" --git https://git.<domain> [--map src=dst]

Needs ``git`` on the PATH. Exit code 0 when every step passed; the steps are printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx
from coincurve import PrivateKey
from websockets.exceptions import WebSocketException

from .agentcheck import Relay, Signer
from .nip46check import HostMap, Nip46Error, sign_event
from .nostr import hex_to_npub


class LocalSigner:
    """A throwaway key without a pass: what the server must refuse."""

    def __init__(self) -> None:
        self.secret = PrivateKey().secret
        self.pubkey = PrivateKey(self.secret).public_key_xonly.format().hex()

    def sign(self, kind: int, content: str = "", tags: list[list[str]] | None = None) -> dict:
        unsigned = {"created_at": int(time.time()), "kind": kind, "tags": tags or []}
        return sign_event(self.secret, {**unsigned, "content": content})


def credential(signer: Any, repo_url: str) -> str:
    """GRASP-08's NIP-98 profile: the repository root, method GET, reusable for 60 seconds."""
    event = signer.sign(27235, "", [["u", repo_url], ["method", "GET"]])
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


def git(*args: str, cwd: str | None = None, header: str = "") -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "user.name=Belfry agent", "-c", "user.email=agent@belfry.invalid"]
    if header:
        cmd += ["-c", f"http.extraHeader=Authorization: {header}"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        [*cmd, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )


def run(args: argparse.Namespace) -> int:
    hostmap = HostMap(args.map)
    failures = 0

    def check(cond: bool, text: str) -> None:
        nonlocal failures
        print(("  ok   " if cond else "  FAIL ") + text)
        failures += 0 if cond else 1

    base = args.git.rstrip("/")
    relay_url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    http = httpx.Client(timeout=args.timeout)
    signer = Signer(args.bunker_url, hostmap, args.timeout)
    relays: list[Relay] = []
    try:
        info = http.get(hostmap(base) + "/", headers={"Accept": "application/nostr+json"}).json()
        check(
            "GRASP-08" in info.get("supported_grasps", []),
            f"git server NIP-11: {info.get('name')}, grasps {info.get('supported_grasps')}",
        )

        # -- a key without a pass gets nothing ---------------------------------------------
        stranger = Relay(relay_url, hostmap, LocalSigner(), args.timeout)
        try:
            got = stranger.query({"kinds": [30617], "limit": 1})
            check(False, f"a key without a pass reads the relay ({len(got)} events)")
        except (RuntimeError, WebSocketException, TimeoutError) as exc:
            check(True, f"a key without a pass is refused: {str(exc)[:80]}")
        finally:
            try:
                stranger.close()
            except Exception:
                pass

        # -- a repository: announcement, state, push ---------------------------------------
        npub = hex_to_npub(signer.pubkey)
        ident = "agent-" + secrets.token_hex(4)
        repo_url = f"{base}/{npub}/{ident}.git"
        r = http.get(hostmap(repo_url) + "/info/refs", params={"service": "git-upload-pack"})
        check(r.status_code == 401, f"git without a credential -> {r.status_code}")

        relay = Relay(relay_url, hostmap, signer, args.timeout)
        relays.append(relay)
        with tempfile.TemporaryDirectory() as work:
            src = os.path.join(work, "src")
            git("init", "-q", "-b", "main", src)
            with open(os.path.join(src, "README.md"), "w") as f:
                f.write("# Made by an agent\n")
            git("add", "README.md", cwd=src)
            git("commit", "-q", "-m", "initial commit", cwd=src)
            head = git("rev-parse", "HEAD", cwd=src).stdout.strip()

            relay.publish(
                signer.sign(
                    30617,
                    "",
                    [
                        ["d", ident],
                        ["name", "Agent's repository"],
                        ["description", "made by the smoke test's agent"],
                        ["clone", repo_url],
                        ["relays", relay_url],
                        ["r", head, "euc"],
                    ],
                )
            )
            relay.publish(
                signer.sign(
                    30618,
                    "",
                    [["d", ident], ["refs/heads/main", head], ["HEAD", "ref: refs/heads/main"]],
                )
            )
            push = git(
                "push", "-q", hostmap(repo_url), "main", cwd=src,
                header=credential(signer, repo_url),
            )  # fmt: skip
            check(push.returncode == 0, f"git push main {head[:8]} -> {push.stderr.strip()[:160]}")

            announced: list[dict] = []
            deadline = time.monotonic() + args.timeout
            while not announced and time.monotonic() < deadline:
                announced = relay.query(
                    {"kinds": [30617], "authors": [signer.pubkey], "#d": [ident]}
                )
                if not announced:
                    time.sleep(1)
            check(bool(announced), f"repository {npub[:12]}.../{ident} is served after the push")

            dst = os.path.join(work, "dst")
            clone = git("clone", "-q", hostmap(repo_url), dst, header=credential(signer, repo_url))
            readme = os.path.join(dst, "README.md")
            check(
                clone.returncode == 0 and os.path.exists(readme),
                f"git clone with a member's credential -> {clone.stderr.strip()[:160] or 'ok'}",
            )

        # -- an issue and a comment on it: agents talk next to the code ---------------------
        coordinate = f"30617:{signer.pubkey}:{ident}"
        issue = signer.sign(
            1621,
            "The README could say what the repository is for.",
            [["a", coordinate, relay_url], ["p", signer.pubkey], ["subject", "Explain the repo"]],
        )
        relay.publish(issue)
        comment = signer.sign(
            1111,
            "On it.",
            [
                ["E", issue["id"], relay_url, signer.pubkey],
                ["K", "1621"],
                ["P", signer.pubkey],
                ["e", issue["id"], relay_url, signer.pubkey],
                ["k", "1621"],
                ["p", signer.pubkey],
            ],
        )
        relay.publish(comment)
        issues = relay.query({"kinds": [1621], "#a": [coordinate]})
        replies = relay.query({"kinds": [1111], "#E": [issue["id"]]})
        check(
            [e["id"] for e in issues] == [issue["id"]] and len(replies) == 1,
            f"issue and comment on the repository ({len(issues)} issue, {len(replies)} comment)",
        )
        print(f"GIT_REPO=nostr://{npub}/{ident}")
    finally:
        for relay in relays:
            relay.close()
        signer.close()
        http.close()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("bunker_url")
    ap.add_argument("--git", required=True, help="the git server, https://git.<domain>")
    ap.add_argument("--map", default=None, help="transport host rewrites, src=dst[,src=dst]")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)
    try:
        return run(args)
    except (Nip46Error, RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        print(f"  FAIL {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
