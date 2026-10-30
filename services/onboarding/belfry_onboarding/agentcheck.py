"""An agent acting for a pass holder, with nothing but the holder's ``bunker://`` URL.

This is the smoke test's agent and a working reference for the agent section of the skill
(``skills/open-source-village/SKILL.md``). Every step is signed by the holder's key through the
bunker (NIP-46): an invite by e-mail (``/api/invites``, NIP-98), a photo on Blossom (kind 24242),
a note with the photo, a reply, a reaction and a repost on the community relay, and on the groups
relay (NIP-29) a private group with a member added, a message, a reaction and a repost in it, and
a public group. There is no API key anywhere.

    python -m belfry_onboarding.agentcheck "<bunker url>" --pass-url https://pass.<domain> \\
        --relay wss://relay.<domain> --groups-relay wss://groups-relay.<domain> \\
        --blossom https://blossom.<domain> --invite someone@example.org [--map src=dst,...]

``--map`` rewrites hosts for the transport only (inside the Compose network the public names do
not resolve); the signed URLs, the AUTH relay tags and the Host header stay the public ones.
Exit code 0 when every step passed; the steps are printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import socket
import struct
import sys
import time
import urllib.parse
import zlib
from typing import Any

import httpx
from coincurve import PrivateKey
from websockets.sync.client import connect

from .blossom import event_id, verify_signature
from .nip46check import BunkerClient, HostMap, Nip46Error


class Signer:
    """Signs unsigned events as the holder through the bunker."""

    def __init__(self, bunker_url: str, hostmap: HostMap, timeout: float):
        self.client = BunkerClient(bunker_url, hostmap, timeout)
        for attempt in range(1, 4):  # like real clients (see nip46check.run_check)
            try:
                reply = self.client.call(
                    "connect", [self.client.bunker_pubkey, self.client.secret or "", ""]
                )
                break
            except Nip46Error:
                if attempt == 3:
                    raise
        if reply.get("result") != "ack":
            raise Nip46Error(f"connect refused: {reply.get('error') or reply}")
        self.pubkey = self.client.call("get_public_key", [])["result"]

    def sign(self, kind: int, content: str = "", tags: list[list[str]] | None = None) -> dict:
        unsigned = {
            "pubkey": self.pubkey,
            "created_at": int(time.time()),
            "kind": kind,
            "tags": tags or [],
            "content": content,
        }
        reply = self.client.call("sign_event", [json.dumps(unsigned)])
        if not isinstance(reply.get("result"), str):
            raise Nip46Error(f"sign_event kind {kind} refused: {reply.get('error')}")
        event = json.loads(reply["result"])
        if event.get("id") != event_id(event) or not verify_signature(event):
            raise Nip46Error(f"sign_event kind {kind}: bad signature")
        return event

    def close(self) -> None:
        self.client.close()


class Relay:
    """One relay session: AUTH (NIP-42) through the signer, then publish and query."""

    def __init__(self, url: str, hostmap: HostMap, signer: Signer, timeout: float):
        self.url, self.signer, self.timeout = url, signer, timeout
        target = urllib.parse.urlsplit(hostmap(url))
        public = urllib.parse.urlsplit(url)
        if target.netloc != public.netloc:
            # the public name as URI (Host header) over a socket to the internal address; zooid
            # serves a relay only on its public host
            host, _, port = target.netloc.partition(":")
            sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
            uri = urllib.parse.urlunsplit(public._replace(scheme="ws"))
            self._conn = connect(uri, sock=sock, open_timeout=timeout)
        else:
            self._conn = connect(url, open_timeout=timeout)
        self.ws = self._conn.__enter__()
        self.auth_id: str | None = None
        self.auth_ok = False

    def close(self) -> None:
        self._conn.__exit__(None, None, None)

    def _recv(self, deadline: float) -> list[Any]:
        """The next message; answers the relay's AUTH challenge once and notes its OK."""
        msg = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        if msg[0] == "AUTH" and self.auth_id is None:
            auth = self.signer.sign(22242, "", [["relay", self.url], ["challenge", msg[1]]])
            self.auth_id = auth["id"]
            self.ws.send(json.dumps(["AUTH", auth]))
        elif msg[0] == "OK" and msg[1] == self.auth_id:
            if not msg[2]:
                raise RuntimeError(f"AUTH refused by {self.url}: {msg[3]}")
            self.auth_ok = True
        return msg

    def _await_auth(self, deadline: float) -> None:
        while not self.auth_ok and time.monotonic() < deadline:
            self._recv(deadline)

    def publish(self, event: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.timeout
        self.ws.send(json.dumps(["EVENT", event]))
        while time.monotonic() < deadline:
            msg = self._recv(deadline)
            if msg[0] == "OK" and msg[1] == event["id"]:
                if msg[2]:
                    return
                if str(msg[3]).startswith("auth-required") and not self.auth_ok:
                    self._await_auth(deadline)
                    self.ws.send(json.dumps(["EVENT", event]))
                    continue
                raise RuntimeError(f"kind {event['kind']} refused: {msg[3]}")
        raise RuntimeError(f"no OK for kind {event['kind']} from {self.url}")

    def query(self, flt: dict[str, Any]) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.timeout
        sub = secrets.token_hex(4)
        self.ws.send(json.dumps(["REQ", sub, flt]))
        events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            msg = self._recv(deadline)
            if msg[0] == "EVENT" and msg[1] == sub:
                events.append(msg[2])
            elif msg[0] == "EOSE" and msg[1] == sub:
                return events
            elif msg[0] == "CLOSED" and msg[1] == sub:
                if not str(msg[2]).startswith("auth-required") or self.auth_ok:
                    raise RuntimeError(f"query refused by {self.url}: {msg[2]}")
                self._await_auth(deadline)
                self.ws.send(json.dumps(["REQ", sub, flt]))
        raise RuntimeError(f"no EOSE from {self.url} for {flt}")


def nip98(signer: Signer, url: str, method: str, body: bytes = b"") -> str:
    tags = [["u", url], ["method", method]]
    if body:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = signer.sign(27235, "", tags)
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


def tiny_png() -> bytes:
    """A 1x1 PNG with a random colour, so every run uploads a new blob."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    pixel = b"\x00" + secrets.token_bytes(3)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixel))
        + chunk(b"IEND", b"")
    )


def imeta(blob: dict[str, Any]) -> list[str]:
    return [
        "imeta",
        f"url {blob['url']}",
        f"m {blob.get('type') or 'image/png'}",
        f"x {blob['sha256']}",
        f"size {blob['size']}",
    ]


def run(args: argparse.Namespace) -> int:
    hostmap = HostMap(args.map)
    failures = 0

    def check(cond: bool, text: str) -> None:
        nonlocal failures
        print(("  ok   " if cond else "  FAIL ") + text)
        failures += 0 if cond else 1

    signer = Signer(args.bunker_url, hostmap, args.timeout)
    print(f"signing as {signer.pubkey[:12]}... through the bunker")
    http = httpx.Client(timeout=args.timeout)
    relays: list[Relay] = []
    try:
        # -- invites by e-mail (NIP-98) -----------------------------------------------------
        api = args.pass_url.rstrip("/") + "/api/invites"

        def call(method: str, url: str, data: Any = None) -> httpx.Response:
            body = json.dumps(data).encode() if data is not None else b""
            headers = {"Authorization": nip98(signer, url, method, body)}
            if body:
                headers["Content-Type"] = "application/json"
            return http.request(method, hostmap(url), content=body or None, headers=headers)

        r = call("GET", api)
        check(r.status_code == 200, f"GET /api/invites -> {r.status_code} {r.text[:120]}")
        steward = r.status_code == 200 and r.json().get("steward")
        if args.invite:
            r = call("POST", api, {"email": args.invite, "name": "Agent Invitee"})
            invite = r.json()
            check(
                r.status_code in (200, 201) and invite.get("state") in ("open", "member"),
                f"invite {args.invite} by e-mail -> {r.status_code} {invite.get('state')}"
                f" (steward: {steward})",
            )
            if invite.get("id"):
                r = call("GET", f"{api}/{invite['id']}")
                check(
                    r.status_code == 200 and r.json().get("email") == args.invite,
                    f"the invite reads back: state {r.json().get('state')},"
                    f" npub {r.json().get('npub')}",
                )
            print(f"INVITE_ID={invite.get('id', '')}")
        r = http.get(hostmap(api))
        check(r.status_code == 401, f"unsigned request -> {r.status_code}")

        # -- a photo on Blossom (kind 24242) --------------------------------------------------
        png = tiny_png()
        sha = hashlib.sha256(png).hexdigest()
        auth = signer.sign(
            24242,
            "Upload photo",
            [["t", "upload"], ["x", sha], ["expiration", str(int(time.time()) + 300)]],
        )
        r = http.put(
            hostmap(args.blossom.rstrip("/") + "/upload"),
            content=png,
            headers={
                "Authorization": "Nostr " + base64.b64encode(json.dumps(auth).encode()).decode(),
                "Content-Type": "image/png",
            },
        )
        blob = r.json() if r.is_success else {}
        check(
            blob.get("sha256") == sha,
            f"photo uploaded to Blossom -> {blob.get('url') or r.text[:120]}",
        )

        # -- the community relay: a note with the photo, reply, reaction, repost ----------------
        community = Relay(args.relay, hostmap, signer, args.timeout)
        relays.append(community)
        note = signer.sign(
            1, f"Hello from an agent\n{blob.get('url', '')}", [imeta(blob)] if blob else []
        )
        community.publish(note)
        reply = signer.sign(
            1,
            "A reply",
            [["e", note["id"], args.relay, "root"], ["p", note["pubkey"]]],
        )
        community.publish(reply)
        reaction = signer.sign(7, "+", [["e", note["id"]], ["p", note["pubkey"]], ["k", "1"]])
        community.publish(reaction)
        repost = signer.sign(
            6, json.dumps(note), [["e", note["id"], args.relay], ["p", note["pubkey"]]]
        )
        community.publish(repost)
        ids = [note["id"], reply["id"], reaction["id"], repost["id"]]
        found = {e["id"] for e in community.query({"ids": ids})}
        check(
            found == set(ids), f"note, reply, reaction and repost on {args.relay} ({len(found)}/4)"
        )

        # -- the groups relay: a private group with a member, and a public group ----------------
        groups = Relay(args.groups_relay, hostmap, signer, args.timeout)
        relays.append(groups)
        member = PrivateKey().public_key_xonly.format().hex()
        private = "agent-" + secrets.token_hex(4)
        groups.publish(signer.sign(9007, "", [["h", private]]))
        groups.publish(
            signer.sign(
                9002,
                "",
                [
                    ["h", private],
                    ["name", "Agent's private room"],
                    ["about", "made by an agent"],
                    ["private"],
                    ["closed"],
                ],
            )
        )
        groups.publish(signer.sign(9000, "", [["h", private], ["p", member]]))
        message = signer.sign(
            9,
            f"Hello room\n{blob.get('url', '')}",
            [["h", private], *([imeta(blob)] if blob else [])],
        )
        groups.publish(message)
        answer = signer.sign(
            9,
            "A reply in the room",
            [
                ["h", private],
                ["q", message["id"], args.groups_relay, message["pubkey"]],
                ["p", message["pubkey"]],
            ],
        )
        groups.publish(answer)
        groups.publish(
            signer.sign(
                7, "+", [["h", private], ["e", message["id"]], ["p", message["pubkey"]], ["k", "9"]]
            )
        )
        groups.publish(
            signer.sign(
                16,
                json.dumps(message),
                [
                    ["h", private],
                    ["e", message["id"], args.groups_relay],
                    ["p", message["pubkey"]],
                    ["k", "9"],
                ],
            )
        )
        meta = groups.query({"kinds": [39000], "#d": [private]})
        tags = {t[0] for t in (meta[0]["tags"] if meta else [])}
        check({"private", "closed"} <= tags, f"private group {private}: metadata {sorted(tags)}")
        members = groups.query({"kinds": [39002], "#d": [private]})
        listed = {t[1] for t in (members[0]["tags"] if members else []) if t[0] == "p"}
        check(
            {member, signer.pubkey} <= listed,
            f"members: the holder (creator) and the added member ({len(listed)} listed)",
        )
        kinds = sorted(e["kind"] for e in groups.query({"#h": [private], "kinds": [7, 9, 16]}))
        check(
            kinds == [7, 9, 9, 16],
            f"message, reply, reaction and repost in the room -> kinds {kinds}",
        )

        public = "agent-" + secrets.token_hex(4)
        groups.publish(signer.sign(9007, "", [["h", public]]))
        groups.publish(signer.sign(9002, "", [["h", public], ["name", "Agent's public room"]]))
        groups.publish(signer.sign(9, "Hello everyone", [["h", public]]))
        meta = groups.query({"kinds": [39000], "#d": [public]})
        tags = {t[0] for t in (meta[0]["tags"] if meta else [])}
        check(
            bool(meta) and not tags & {"private", "closed", "hidden"},
            f"public group {public}: visible to every member, metadata {sorted(tags)}",
        )
        print(f"GROUPS={private},{public}")
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
    ap.add_argument("--pass-url", required=True, help="https://pass.<domain> (signed in NIP-98)")
    ap.add_argument("--relay", required=True, help="the community relay, wss://relay.<domain>")
    ap.add_argument("--groups-relay", required=True, help="wss://groups-relay.<domain>")
    ap.add_argument("--blossom", required=True, help="https://blossom.<domain>")
    ap.add_argument("--invite", default="", help="an e-mail address to invite")
    ap.add_argument("--map", default=None, help="transport host rewrites, src=dst[,src=dst]")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args(argv)
    try:
        return run(args)
    except (Nip46Error, RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        print(f"  FAIL {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
