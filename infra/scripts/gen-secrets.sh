#!/usr/bin/env bash
# Writes infra/.env from infra/.env.example with generated secrets, plus infra/keycast/master.key.
# Safe to re-run: an existing .env is kept unless --force is given (a backup is written then);
# variables added to .env.example since are appended to it, "generated" ones with fresh values.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$INFRA_DIR/.env"
EXAMPLE="$INFRA_DIR/.env.example"
MASTER_KEY="$INFRA_DIR/keycast/master.key"

MODE=write
if [[ -f "$ENV_FILE" && "${1:-}" != "--force" ]]; then
  MODE=fill
  echo "$ENV_FILE exists; keeping it (use --force to regenerate, a backup is written)."
elif [[ -f "$ENV_FILE" ]]; then
  cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
fi

rand() { openssl rand -hex "${1:-24}"; }
admin_keys=$(python3 "$INFRA_DIR/scripts/nostr_keys.py")
admin_nsec=$(printf '%s' "$admin_keys" | python3 -c 'import json,sys;print(json.load(sys.stdin)["secret_hex"])')
admin_pub=$(printf '%s' "$admin_keys" | python3 -c 'import json,sys;print(json.load(sys.stdin)["pubkey_hex"])')
server_nsec=$(python3 "$INFRA_DIR/scripts/nostr_keys.py" | python3 -c 'import json,sys;print(json.load(sys.stdin)["secret_hex"])')

# Replace "generated" per variable; keep every comment from the example.
python3 - "$EXAMPLE" "$ENV_FILE" "$MODE" <<PYEOF
import re, sys
values = {
    "POSTGRES_PASSWORD": "$(rand)",
    "ODOO_DB_PASSWORD": "$(rand)",
    "AUTHENTIK_DB_PASSWORD": "$(rand)",
    "KEYCAST_DB_PASSWORD": "$(rand)",
    "ODOO_ADMIN_PASSWORD": "$(rand 16)",
    "ODOO_MASTER_PASSWORD": "$(rand 24)",
    "AUTHENTIK_SECRET_KEY": "$(rand 48)",
    "AUTHENTIK_BOOTSTRAP_PASSWORD": "$(rand 16)",
    "AUTHENTIK_BOOTSTRAP_TOKEN": "$(rand 32)",
    "BRIDGE_CLIENT_SECRET": "$(rand 24)",
    "KEYCAST_OIDC_CLIENT_SECRET": "$(rand 24)",
    "STEWARD_OIDC_CLIENT_SECRET": "$(rand 24)",
    "STEWARD_SESSION_SECRET": "$(rand 32)",
    "PASS_PAGE_OIDC_CLIENT_SECRET": "$(rand 24)",
    "PASS_PAGE_SECRET": "$(rand 32)",
    "ISSUER_OIDC_CLIENT_SECRET": "$(rand 24)",
    "KEYCAST_SERVER_NSEC": "$server_nsec",
    "KEYCAST_ADMIN_NSEC": "$admin_nsec",
    "KEYCAST_ADMIN_PUBKEY": "$admin_pub",
    "KEYCAST_ADMIN_PASSWORD": "$(rand 16)",
    "KEYCAST_SERVICE_TOKEN": "$(rand 32)",
    "HOOK_TOKEN": "$(rand 32)",
    "PASS_INFO_TOKEN": "$(rand 32)",
    "MINT_SEED": "$(rand 32)",
    # Keycast runs as the deploying user, the owner of master.key and the custody seed
    "HOST_UID": "$(id -u)",
    "HOST_GID": "$(id -g)",
}
example, target, mode = sys.argv[1:4]
line_re = re.compile(r"^([A-Z0-9_]+)=(\S*)(\s*#.*)?$")

if mode == "fill":
    have = {m.group(1) for m in (re.match(r"^([A-Z0-9_]+)=", l) for l in open(target)) if m}
    added = []
    for line in open(example):
        m = line_re.match(line.rstrip("\n"))
        if m and m.group(1) not in have:
            key, value = m.group(1), m.group(2)
            if value == "generated":
                value = values[key]
            added.append(f"{key}={value}{m.group(3) or ''}\n")
    if added:
        with open(target, "a") as f:
            f.write("\n# --- appended by gen-secrets.sh (new in .env.example)\n" + "".join(added))
        print("appended to .env: " + ", ".join(a.split("=", 1)[0] for a in added))
    sys.exit(0)

out = []
for line in open(example):
    m = line_re.match(line.rstrip("\n"))
    if m and m.group(2) == "generated":
        line = f"{m.group(1)}={values[m.group(1)]}{m.group(3) or ''}\n"
    out.append(line)
open(target, "w").write("".join(out))
print(f"wrote {target}")
PYEOF
chmod 600 "$ENV_FILE"

# Local development only: the custody seed in a file (Keycast patch 0014). A deployment keeps it
# off the disk (CUSTODY_SEED_DIR=/run/belfry, scripts/custody-seed.sh).
CUSTODY_SEED="$INFRA_DIR/secrets/custody/custody-seed"
if [[ ! -f "$CUSTODY_SEED" && "$(grep -s '^CUSTODY_SEED_DIR=' "$ENV_FILE" | cut -d= -f2)" == "" ]]; then
  mkdir -p "$(dirname "$CUSTODY_SEED")" && chmod 700 "$(dirname "$CUSTODY_SEED")"
  openssl rand -hex 32 > "$CUSTODY_SEED" && chmod 600 "$CUSTODY_SEED"
  echo "wrote $CUSTODY_SEED (local development; deployments unlock it from YubiKeys)"
fi

if [[ -f "$MASTER_KEY" ]]; then
  echo "$MASTER_KEY exists; keeping it (deleting it makes every stored Keycast key unreadable)."
else
  mkdir -p "$(dirname "$MASTER_KEY")"
  openssl rand 32 | base64 | tr -d '\n' > "$MASTER_KEY"
  chmod 600 "$MASTER_KEY"
  echo "wrote $MASTER_KEY"
fi

# Odoo config (odoo.conf is gitignored). It carries only a PBKDF2 hash of the master password,
# in passlib's format, which Odoo verifies (tools/config.py), so the file can stay readable for
# the Odoo container's user; the password itself stays in .env. A hash that still matches is kept.
python3 - "$INFRA_DIR" <<'PYRENDER'
import base64, hashlib, hmac, os, re, sys
infra = sys.argv[1]
env = dict(m.groups() for m in (re.match(r"^([A-Z0-9_]+)=([^#\s]*)", l) for l in open(f"{infra}/.env")) if m)
password = env.get("ODOO_MASTER_PASSWORD", "").encode()
conf = f"{infra}/odoo/odoo.conf"
ab64 = lambda b: base64.b64encode(b).decode().rstrip("=").replace("+", ".")
unab64 = lambda s: base64.b64decode(s.replace(".", "+") + "=" * (-len(s) % 4))

def pbkdf2(salt: bytes, rounds: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", password, salt, rounds)

stored = ""
if os.path.exists(conf):
    m = re.search(r"^admin_passwd = (\$pbkdf2-sha512\$(\d+)\$([^$]+)\$(\S+))$", open(conf).read(), re.M)
    if m and hmac.compare_digest(pbkdf2(unab64(m.group(3)), int(m.group(2))), unab64(m.group(4))):
        stored = m.group(1)
if password and not stored:
    salt, rounds = os.urandom(16), 600_000
    stored = f"$pbkdf2-sha512${rounds}${ab64(salt)}${ab64(pbkdf2(salt, rounds))}"
tmpl = open(f"{infra}/odoo/odoo.conf.tmpl").read()
open(conf, "w").write(tmpl.replace("@@ODOO_MASTER_PASSWORD@@", stored))
os.chmod(conf, 0o644)
print(f"wrote {conf}")
PYRENDER

# Phase 3: the credential issuer's CA, signer key and the walt.id Issuer2 key files.
"$INFRA_DIR/scripts/gen-issuer-pki.sh"
