#!/usr/bin/env bash
# Writes infra/.env (settings) from infra/.env.example, the secrets as one file each in
# infra/secrets/ (SECRETS below), and infra/keycast/master.key. Containers get their secrets as
# Compose secrets (/run/secrets/<name>), never as environment variables; host scripts read them
# with scripts/secrets.sh. infra/README.md, "Secrets".
# Safe to re-run: existing files are kept (--force rewrites .env from the example, a backup is
# written; delete a file in infra/secrets/ to generate that secret anew). Variables added to
# .env.example since are appended to .env. A secret still found in .env (setups from before
# 2026-10-25) is moved into its file and removed from .env.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$INFRA_DIR/.env"
EXAMPLE="$INFRA_DIR/.env.example"
SECRETS_DIR="$INFRA_DIR/secrets"
MASTER_KEY="$INFRA_DIR/keycast/master.key"

MODE=write
if [[ -f "$ENV_FILE" && "${1:-}" != "--force" ]]; then
  MODE=fill
  echo "$ENV_FILE exists; keeping it (use --force to regenerate, a backup is written)."
elif [[ -f "$ENV_FILE" ]]; then
  cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
  chmod 600 "$ENV_FILE".bak.*
fi

admin_keys=$(python3 "$INFRA_DIR/scripts/nostr_keys.py")
server_nsec=$(python3 "$INFRA_DIR/scripts/nostr_keys.py" | python3 -c 'import json,sys;print(json.load(sys.stdin)["secret_hex"])')

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"
python3 - "$EXAMPLE" "$ENV_FILE" "$MODE" "$SECRETS_DIR" "$admin_keys" "$server_nsec" "$(id -u)" "$(id -g)" <<'PYEOF'
import json, os, re, secrets, subprocess, sys

example, target, mode, secrets_dir, admin_keys, server_nsec, uid, gid = sys.argv[1:9]
scripts_dir = os.path.join(os.path.dirname(secrets_dir), "scripts")
admin = json.loads(admin_keys)
rand = lambda n=24: secrets.token_hex(n)

# Every secret, by its variable name; the file is infra/secrets/<name in lower case>. None: no
# generated value (bootstrap.sh or scripts/put-secret.sh writes it; an empty file until then, so
# Compose can mount it).
SECRETS = {
    "POSTGRES_PASSWORD": rand(),               # superuser "postgres"
    "ODOO_DB_PASSWORD": rand(),                # role "odoo" (CREATEDB for test databases)
    "AUTHENTIK_DB_PASSWORD": rand(),
    "KEYCAST_DB_PASSWORD": rand(),
    "ODOO_ADMIN_PASSWORD": rand(16),           # Odoo user "admin", set by bootstrap
    "ODOO_MASTER_PASSWORD": rand(24),          # database management; odoo.conf holds its hash
    "ODOO_API_KEY": None,                      # bootstrap: the onboarding service's Odoo user
    "AUTHENTIK_SECRET_KEY": rand(48),          # Django secret key; changing it ends sessions
    "AUTHENTIK_BOOTSTRAP_PASSWORD": rand(16),  # akadmin, applied on the first start only
    "AUTHENTIK_BOOTSTRAP_TOKEN": rand(32),     # akadmin's API token, first start only
    "AUTHENTIK_TOKEN": None,                   # bootstrap: the API token the services use
    "BRIDGE_CLIENT_SECRET": rand(24),          # Authentik's pass-bridge source at the bridge
    "KEYCAST_OIDC_CLIENT_SECRET": rand(24),
    "STEWARD_OIDC_CLIENT_SECRET": rand(24),
    "STEWARD_SESSION_SECRET": rand(32),
    "PASS_PAGE_OIDC_CLIENT_SECRET": rand(24),
    "PASS_PAGE_SECRET": rand(32),
    "ISSUER_OIDC_CLIENT_SECRET": rand(24),
    "KEYCAST_SERVER_NSEC": server_nsec,        # signs server-issued UCANs
    "KEYCAST_ADMIN_NSEC": admin["secret_hex"], # registers the Keycast admin (bootstrap)
    "KEYCAST_ADMIN_PASSWORD": rand(16),
    "KEYCAST_SERVICE_TOKEN": rand(32),
    "KEYCAST_ADMIN_TOKEN": None,               # bootstrap
    "HOOK_TOKEN": rand(32),                    # Odoo's and Keycast's webhooks
    "PASS_INFO_TOKEN": rand(32),               # Keycast and the apps at /admin/*
    "MINT_SEED": rand(32),                     # the token mint's keys; back it up
    "STRIPE_WEBHOOK_SECRET": None,             # bootstrap, once Stripe is configured
    "SMTP_PASSWORD": None,                     # put-secret.sh smtp_password
}
generated = {"KEYCAST_ADMIN_PUBKEY": admin["pubkey_hex"], "HOST_UID": uid, "HOST_GID": gid}
line_re = re.compile(r"^([A-Z0-9_]+)=(\S*)(\s*#.*)?$")
path = lambda name: os.path.join(secrets_dir, name.lower())

def write_secret(name, value):
    fd = os.open(path(name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(value)

# Secrets from an existing .env move into their files (a file that exists wins).
env_lines = open(target).read().splitlines(keepends=True) if os.path.exists(target) else []
moved = []
for line in env_lines:
    m = line_re.match(line.rstrip("\n"))
    if m and m.group(1) in SECRETS and m.group(2) not in ("", "generated"):
        if not os.path.exists(path(m.group(1))):
            write_secret(m.group(1), m.group(2))
            moved.append(m.group(1))
if moved:
    print("moved from .env into infra/secrets/: " + ", ".join(moved))
new = []
for name, value in SECRETS.items():
    if not os.path.exists(path(name)):
        write_secret(name, value or "")
        if value:
            new.append(name)
    os.chmod(path(name), 0o640)
if new:
    print("generated in infra/secrets/: " + ", ".join(new))
# The admin's public key (a setting: Keycast's ALLOWED_PUBKEYS) always follows its secret key.
nsec = open(path("KEYCAST_ADMIN_NSEC")).read().strip()
out = subprocess.run([sys.executable, os.path.join(scripts_dir, "nostr_keys.py"), nsec],
                     check=True, capture_output=True, text=True).stdout
generated["KEYCAST_ADMIN_PUBKEY"] = json.loads(out)["pubkey_hex"]

def env_out(lines):
    out = []
    for line in lines:
        m = line_re.match(line.rstrip("\n"))
        if m and m.group(1) in SECRETS:
            continue
        if m and m.group(1) == "KEYCAST_ADMIN_PUBKEY":
            line = f"KEYCAST_ADMIN_PUBKEY={generated['KEYCAST_ADMIN_PUBKEY']}{m.group(3) or ''}\n"
        elif m and m.group(2) == "generated":
            line = f"{m.group(1)}={generated[m.group(1)]}{m.group(3) or ''}\n"
        out.append(line)
    return out

if mode == "fill":
    out = env_out(env_lines)
    have = {m.group(1) for m in (line_re.match(l.rstrip("\n")) for l in out) if m}
    added = [l for l in env_out(open(example).readlines())
             if (m := line_re.match(l.rstrip("\n"))) and m.group(1) not in have]
    if added:
        out += ["\n# --- appended by gen-secrets.sh (new in .env.example)\n"] + added
        print("appended to .env: " + ", ".join(a.split("=", 1)[0] for a in added))
    if out != env_lines:
        open(target, "w").write("".join(out))
else:
    open(target, "w").write("".join(env_out(open(example).readlines())))
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
password = open(f"{infra}/secrets/odoo_master_password").read().strip().encode()
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
