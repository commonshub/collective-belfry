#!/usr/bin/env bash
# Signs a Nostr event inside the onboarding container (its Schnorr code, no tools on the host):
# reads {"secret": <hex or nsec>, "event": {kind, created_at, tags, content}} on stdin, prints the
# signed event. Used by self-custody-check.sh.
set -euo pipefail
INFRA_DIR=$(cd "$(dirname "$0")/.." && pwd)
exec docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -c '
import json, sys
from belfry_onboarding.nip46check import sign_event

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def nsec_bytes(text):
    data = [CHARSET.index(c) for c in text[text.rindex("1") + 1 : -6]]
    acc = bits = 0
    out = bytearray()
    for v in data:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out[:32])


req = json.load(sys.stdin)
secret = req["secret"]
key = bytes.fromhex(secret) if len(secret) == 64 else nsec_bytes(secret)
print(json.dumps(sign_event(key, req["event"])))
'
