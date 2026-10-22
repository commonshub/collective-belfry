#!/usr/bin/env bash
# Writes infra/.env from infra/.env.example with generated secrets, plus infra/keycast/master.key.
# Safe to re-run: an existing .env is kept unless --force is given (a backup is written then).
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$INFRA_DIR/.env"
EXAMPLE="$INFRA_DIR/.env.example"
MASTER_KEY="$INFRA_DIR/keycast/master.key"

if [[ -f "$ENV_FILE" && "${1:-}" != "--force" ]]; then
  echo "$ENV_FILE exists; keeping it (use --force to regenerate, a backup is written)."
else
  if [[ -f "$ENV_FILE" ]]; then
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
  fi
  rand() { openssl rand -hex "${1:-24}"; }
  admin_keys=$(python3 "$INFRA_DIR/scripts/nostr_keys.py")
  admin_nsec=$(printf '%s' "$admin_keys" | python3 -c 'import json,sys;print(json.load(sys.stdin)["secret_hex"])')
  admin_pub=$(printf '%s' "$admin_keys" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pubkey_hex"])')
  server_nsec=$(python3 "$INFRA_DIR/scripts/nostr_keys.py" | python3 -c 'import json,sys;print(json.load(sys.stdin)["secret_hex"])')

  # Replace "generated" per variable; keep every comment from the example.
  python3 - "$EXAMPLE" "$ENV_FILE" <<EOF
import re, sys
values = {
    "POSTGRES_PASSWORD": "$(rand)",
    "ODOO_DB_PASSWORD": "$(rand)",
    "AUTHENTIK_DB_PASSWORD": "$(rand)",
    "KEYCAST_DB_PASSWORD": "$(rand)",
    "ODOO_ADMIN_PASSWORD": "$(rand 16)",
    "AUTHENTIK_SECRET_KEY": "$(rand 48)",
    "AUTHENTIK_BOOTSTRAP_PASSWORD": "$(rand 16)",
    "AUTHENTIK_BOOTSTRAP_TOKEN": "$(rand 32)",
    "BRIDGE_CLIENT_SECRET": "$(rand 24)",
    "KEYCAST_SERVER_NSEC": "$server_nsec",
    "KEYCAST_ADMIN_NSEC": "$admin_nsec",
    "KEYCAST_ADMIN_PUBKEY": "$admin_pub",
    "KEYCAST_ADMIN_PASSWORD": "$(rand 16)",
    "KEYCAST_SERVICE_TOKEN": "$(rand 32)",
    "HOOK_TOKEN": "$(rand 32)",
}
out = []
for line in open(sys.argv[1]):
    m = re.match(r"^([A-Z0-9_]+)=generated(\s*#.*)?$", line.rstrip("\n"))
    if m:
        key = m.group(1)
        line = f"{key}={values[key]}{m.group(2) or ''}\n"
    out.append(line)
open(sys.argv[2], "w").write("".join(out))
EOF
  chmod 600 "$ENV_FILE"
  echo "wrote $ENV_FILE"
fi

if [[ -f "$MASTER_KEY" ]]; then
  echo "$MASTER_KEY exists; keeping it (deleting it makes every stored Keycast key unreadable)."
else
  mkdir -p "$(dirname "$MASTER_KEY")"
  openssl rand 32 | base64 | tr -d '\n' > "$MASTER_KEY"
  chmod 600 "$MASTER_KEY"
  echo "wrote $MASTER_KEY"
fi
