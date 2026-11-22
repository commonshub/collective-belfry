#!/usr/bin/env bash
# Move a belfry from Keycast and Authentik to Belfry's own signer, with nobody signed out and no
# key changed (docs/own-signer.md §6 and "Moving a belfry": read that first).
#
#   migrate-to-own-signer.sh check      read-only: versions, disk, the three sources (Keycast's
#                                       database, Authentik, the register), what would be carried
#   migrate-to-own-signer.sh backup     a fresh encrypted backup (backup.sh): one from the last
#                                       hour, or it makes one; nothing goes on without it
#   migrate-to-own-signer.sh import     the signer starts next to Keycast (no bunker: it answers
#                                       nobody yet), stops making accounts of its own ("no new
#                                       accounts") and takes in accounts, keys, usernames,
#                                       states, sessions, admins. The belfry keeps running.
#   migrate-to-own-signer.sh verify     every account, key, username, session and admin compared
#                                       between Keycast, the signer and the register; a report
#                                       file; exit 1 on any difference
#   migrate-to-own-signer.sh switch     the belfry goes down for a few minutes: Keycast and the
#                                       service stop, a last import and a verify that must be
#                                       clean, then SIGNER=belfry and `belfry apply`; once the
#                                       verify after it is clean, new accounts are open again
#   migrate-to-own-signer.sh open       new accounts open again by hand (switch does it; for a
#                                       switch that stopped after `belfry apply`)
#   migrate-to-own-signer.sh rollback   back to Keycast and Authentik, which nothing here touched
#   migrate-to-own-signer.sh cleanup    much later, and only when asked: empty Keycast's and
#                                       Authentik's databases and volumes. Typed confirmation, a
#                                       clean verify report, seven days since the switch.
#   migrate-to-own-signer.sh status     where this belfry is in the move
#
# Options (after the phase):
#   --exclude PUBKEY            leave this Keycast account out on purpose (an account without a
#                               vine id stops the migration until it is named here); repeatable
#   --accept-lost-sessions N    exactly N live sessions are signed out by the move because
#                               another account keeps their client key: seen and accepted
#   --accept-missing-stewards N exactly N admins in Authentik have no person in the register
#   --accept-unverified N       exactly N connected sessions Keycast does not verify are left
#                               out (read Keycast's validate_client before accepting)
#   --accept-skipped-rows N     exactly N sessions whose bunker key does not derive are left out
#   --accept-guessed N          exactly N carried sessions have a scope guessed own (an origin
#                               neither the belfry's nor an approved outside app's: read the
#                               report's scope_guessed list before accepting)
#   --yes                       no question before `switch` (never for `cleanup`)
#   --before-seven-days         `cleanup` although the switch is less than seven days old
#   --skip-carry-back           rollback without writing the signer's sign-outs and suspensions
#                               into Keycast (a signer that is down cannot be read): they come back
#   --no-tmux                   switch, rollback and cleanup outside tmux or screen (a dropped
#                               SSH session in the middle of `belfry apply` leaves a half-applied
#                               stack: run them under tmux)
#
# Every phase can be run again. import, verify and switch never change or delete anything of
# Keycast's or Authentik's: they read Keycast's database and Authentik's group, and write only
# to the signer's own volume and the register's admin flag. Nothing is deleted outside
# `cleanup`, and `belfry apply` never runs `cleanup`.
#
# Never printed: keys (public keys only, shortened), sealed keys, session secrets, tokens,
# e-mail addresses (masked). Keycast's rows go from psql to the importer through a pipe and are
# never written to disk; the reports hold counts, vine ids, shortened public keys and app names.
set -euo pipefail
umask 077

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")/../.." && pwd)"
INFRA="$ROOT/infra"
ENV_FILE="$INFRA/.env"
SECRETS="$INFRA/secrets"
BELFRY="$INFRA/scripts/belfry"
BACKUP="$INFRA/scripts/backup.sh"
STATE_DIR="$INFRA/data/setup/migration"
STATE="$STATE_DIR/state.json"
# the backups backup.sh makes (belfry-<host>-<UTC stamp>.tar.age, with <name>.json next to it):
# their time is read from the file's own name, never from the environment
BACKUP_DIR="$HOME/belfry-backups"
CLEANUP_DAYS=7

die() { echo "migrate: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

# as the owner of the checkout, like the belfry command (the secrets and the state are theirs)
OWNER=$(stat -c %U "$ROOT")
if [[ "$(id -un)" != "$OWNER" ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "run as $OWNER or root"
  exec runuser -u "$OWNER" -- "$SELF" "$@"
fi
cd "$ROOT"

PHASE=${1:-}
[[ $# -gt 0 ]] && shift
YES=0 EARLY=0 NO_TMUX=0 SKIP_CARRY_BACK=0
PY_FLAGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --exclude) [[ "${2:-}" =~ ^[0-9a-f]{64}$ ]] || die "--exclude takes a public key (64 hex)"
               PY_FLAGS+=(--exclude "$2"); shift 2 ;;
    --accept-lost-sessions|--accept-missing-stewards|--accept-unverified|--accept-skipped-rows|--accept-guessed)
               [[ "${2:-}" =~ ^[0-9]+$ ]] || die "$1 takes a number"
               PY_FLAGS+=("$1" "$2"); shift 2 ;;
    --yes) YES=1; shift ;;
    --before-seven-days) EARLY=1; shift ;;
    --no-tmux) NO_TMUX=1; shift ;;
    --skip-carry-back) SKIP_CARRY_BACK=1; shift ;;
    *) die "unknown option $1 (see the top of $SELF)" ;;
  esac
done

env_get() { sed -nE "s/^$1=\"?([^\"#]*)\"?.*/\1/p" "$ENV_FILE" | tail -n1 | sed 's/[[:space:]]*$//'; }
signer_now() { local s; s=$(env_get SIGNER); echo "${s:-keycast}"; }
domain() { env_get BELFRY_DOMAIN; }
compose() { "$BELFRY" compose "$@"; }
# with the signer next to Keycast (docker-compose.signer-import.yml), while SIGNER is keycast
icompose() { BELFRY_SIGNER_IMPORT=1 "$BELFRY" compose "$@"; }
now() { date +%s; }
stamp() { date -u +%Y%m%dT%H%M%SZ; }

state_get() { # state_get KEY -> value or nothing
  [[ -s "$STATE" ]] || return 0
  python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print("" if v is None else v)' "$STATE" "$1"
}
state_set() { # state_set KEY VALUE…
  mkdir -p "$STATE_DIR"
  python3 - "$STATE" "$@" <<'PY'
import json, os, sys
path, pairs = sys.argv[1], sys.argv[2:]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {}
for k, v in zip(pairs[::2], pairs[1::2]):
    data[k] = int(v) if v.isdigit() else v
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=1)
os.replace(tmp, path)
PY
}
report_field() { # report_field FILE KEY -> the report's value (true/false/number/text)
  python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(str(v).lower() if isinstance(v,bool) else ("" if v is None else v))' "$1" "$2" 2>/dev/null || true
}

# ── Keycast's rows (docs/own-signer.md §6), as one JSON document on stdout ──────────────────
# Read-only. The sealed keys and the session secrets' hashes are in it: it only ever goes
# down a pipe into the importer.
rows() {
  local tenant; tenant=$(env_get KEYCAST_TENANT_DOMAIN); tenant=${tenant:-localhost}
  compose exec -T postgres psql -U postgres -d keycast -At -q -v ON_ERROR_STOP=1 \
    -v tenant="$tenant" -f - <<'SQL'
BEGIN TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ;
SELECT json_build_object(
  'now', extract(epoch FROM now())::bigint,
  'tenant', :'tenant',
  'accounts', coalesce((SELECT json_agg(a) FROM (
      SELECT u.pubkey::text AS pubkey,
             u.vine_id::text AS vine_id,
             u.username::text AS username,
             u.status::text AS status,
             encode(k.encrypted_secret_key, 'hex') AS sealed
      FROM users u
      JOIN tenants t ON t.id = u.tenant_id AND t.domain = :'tenant'
      LEFT JOIN personal_keys k ON k.user_pubkey = u.pubkey AND k.tenant_id = u.tenant_id
      ORDER BY u.pubkey) a), '[]'::json),
  'sessions', coalesce((SELECT json_agg(s) FROM (
      SELECT a.user_pubkey::text AS user_pubkey,
             a.bunker_public_key::text AS bunker_public_key,
             a.secret_hash::text AS secret_hash,
             a.client_id::text AS client_id,
             a.redirect_origin::text AS redirect_origin,
             a.client_pubkey::text AS client_pubkey,
             a.connected_client_pubkey::text AS connected_client_pubkey,
             a.verified_client_pubkey::text AS verified_client_pubkey,
             extract(epoch FROM a.created_at)::bigint AS created_at,
             extract(epoch FROM a.last_activity)::bigint AS last_used_at,
             a.activity_count AS uses,
             extract(epoch FROM a.revoked_at)::bigint AS revoked_at,
             extract(epoch FROM a.expires_at)::bigint AS expires_at
      FROM oauth_authorizations a
      JOIN tenants t ON t.id = a.tenant_id AND t.domain = :'tenant'
      ORDER BY a.user_pubkey, a.created_at) s), '[]'::json)
);
COMMIT;
SQL
}

# the migration's Python side with the rows on stdin: report (JSON) to FILE, the rest to the
# terminal. `plan` runs in the running service; import and verify in a one-off container of
# the same image that reaches the signer (the running one is not touched).
py() { # py PHASE REPORT_FILE [flags…]
  local phase=$1 out=$2; shift 2
  mkdir -p "$STATE_DIR"
  local -a run
  if [[ "$phase" == plan ]] && compose ps --status running -q onboarding 2>/dev/null | grep -q .; then
    run=(compose exec -T onboarding)
  elif [[ "$(signer_now)" == belfry ]]; then
    run=(compose --profile app run --rm -T --no-deps onboarding)
  else
    run=(icompose --profile app run --rm -T --no-deps onboarding)
  fi
  local -a flags=("$@")
  # Keycast's own admin (an account without a vine id on every install with Keycast) is left out
  # by itself; the counts say so
  local kc_admin; kc_admin=$(env_get KEYCAST_ADMIN_PUBKEY)
  if [[ "$kc_admin" =~ ^[0-9a-f]{64}$ ]]; then flags+=(--keycast-admin "$kc_admin"); fi
  # after the switch Authentik is not running, and people sign in, join and rename: what is
  # compared from then on is the keys (the verify that decided ran with everything stopped)
  if [[ "$(signer_now)" == belfry && "$phase" != plan ]]; then flags+=(--no-stewards --keys-only); fi
  local rc=0
  rows | "${run[@]}" python -m belfry_onboarding signer-migrate "$phase" "${flags[@]}" > "$out.tmp" || rc=$?
  if [[ -s "$out.tmp" ]]; then tail -n1 "$out.tmp" > "$out"; stamp_report "$out" || true; fi
  rm -f "$out.tmp"
  return $rc
}

need_keycast_mode() {
  [[ "$(signer_now)" == keycast ]] || die "this belfry already runs on its own signer (SIGNER=belfry)"
}
need_own_signin() {
  local s; s=$(env_get PASS_SIGNIN)
  [[ "$s" == belfry ]] || die "PASS_SIGNIN is ${s:-authentik}: a belfry that signs people in through Authentik cannot move yet (docs/own-signer.md §5, entry 6)"
}

latest_backup() { # the newest backup file, or nothing
  ls -1t "$BACKUP_DIR"/belfry-*.tar.age 2>/dev/null | head -n1
}
backup_time() { # backup_time FILE -> its time (seconds) from its own name, or nothing
  local stamp
  stamp=$(basename "$1" | sed -nE 's/.*-([0-9]{8}T[0-9]{6}Z)\.tar\.age$/\1/p')
  [[ -n "$stamp" ]] || return 0
  date -u -d "$(sed -E 's/^(....)(..)(..)T(..)(..)(..)Z$/\1-\2-\3 \4:\5:\6/' <<<"$stamp")" +%s 2>/dev/null || true
}
backup_age() { # seconds since the newest backup was made, or nothing (none, or one from the future)
  local f t; f=$(latest_backup); [[ -n "$f" ]] || return 0
  t=$(backup_time "$f"); [[ -n "$t" ]] || return 0
  # a clock or a name from the future is not a fresh backup
  [[ "$t" -le $(( $(now) + 300 )) ]] || return 0
  echo $(( $(now) - t ))
}
fresh_backup() { # a backup from the last hour, or one made now; dies without
  local age; age=$(backup_age)
  if [[ -n "$age" && "$age" -lt 3600 ]]; then
    say "Backup: made $((age / 60)) minutes ago."
  else
    say "Backup: none from the last hour; making one (backup.sh run)."
    "$BACKUP" run || die "the backup failed: nothing goes on without one (set BACKUP_RECIPIENTS and BACKUP_TARGET; infra/DEPLOY.md)"
    age=$(backup_age)
    [[ -n "$age" && "$age" -lt 3600 ]] || die "backup.sh ran but no backup from the last hour is in $BACKUP_DIR"
  fi
  local f; f=$(latest_backup)
  box_id >/dev/null
  state_set backup_at "$(backup_time "$f")" backup_path "$f" backup_sha "$(sha256sum "$f" | cut -d' ' -f1)" box "$BOX"
}
# backup_holds_signer FILE: the backup's own record (FILE.json, written by backup.sh) says it
# carries the signer's volume, and its checksum is the file's
backup_holds_signer() {
  local f=$1 side="$1.json"
  [[ -s "$f" && -s "$side" ]] || return 1
  python3 -c '
import hashlib, json, sys
f, side = sys.argv[1], sys.argv[2]
meta = json.load(open(side))
h = hashlib.sha256()
with open(f, "rb") as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
ok = meta.get("sha256") == h.hexdigest() and int((meta.get("volumes") or {}).get("signer-data") or 0) > 0
sys.exit(0 if ok else 1)' "$f" "$side"
}

# ── the box a report belongs to ─────────────────────────────────────────────────────────────
# Reports and state.json travel in backups: a copy restored on another box must not pass this
# box's guards. A report is bound to the domain, a fingerprint of the signer's master key (the
# file's hash, never the key) and the identifier of the Postgres cluster Keycast's database
# lives in.
# box_id must first run in the script's own shell (need_this_box, stamp_report do), so that a
# Postgres that does not answer stops the script instead of an empty "$(box_id)".
BOX=""
box_id() {
  if [[ -z "$BOX" ]]; then
    local master="$SECRETS/signer_master_key" fp db
    [[ -s "$master" ]] || master="$INFRA/keycast/master.key"
    if [[ -s "$master" ]]; then fp=$(sha256sum "$master" | cut -c1-16); else fp=none; fi
    db=$(compose exec -T postgres psql -U postgres -d keycast -tAc "select system_identifier from pg_control_system()" 2>/dev/null | tr -d '[:space:]')
    # never a box without its database: every guard would then say "made on another box"
    [[ -n "$db" ]] || die "Postgres didn't answer (the system identifier of Keycast's database cluster could not be read): start it ('$BELFRY compose up -d postgres') and run this again"
    BOX=$(python3 -c 'import json,sys; print(json.dumps({"domain": sys.argv[1], "master": sys.argv[2], "keycast_db": sys.argv[3]}, sort_keys=True))' "$(domain)" "$fp" "$db")
  fi
  echo "$BOX"
}
stamp_report() { # stamp_report FILE: the box goes into the report
  [[ -s "$1" ]] || return 0
  box_id >/dev/null
  python3 -c '
import json, os, sys
path, box = sys.argv[1], json.loads(sys.argv[2])
data = json.load(open(path))
data["box"] = box
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f)
os.replace(tmp, path)' "$1" "$(box_id)"
}
report_of_this_box() { # report_of_this_box FILE: true when FILE was made on this box
  [[ -s "$1" ]] || return 1
  python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("box") == json.loads(sys.argv[2]) else 1)' "$1" "$(box_id)"
}

# ── what must hold before the belfry is touched ─────────────────────────────────────────────
need_tmux() { # switch, rollback and cleanup outlive a dropped SSH session only under tmux/screen
  [[ $NO_TMUX -eq 1 || -n "${TMUX:-}" || -n "${STY:-}" ]] \
    || die "run '$PHASE' under tmux or screen (a dropped connection during 'belfry apply' leaves a half-applied stack), or add --no-tmux"
}
LOCK_WAIT=${BELFRY_MIGRATE_LOCK_WAIT:-120}
take_apply_lock() { # the lock 'belfry apply' takes, held to the end of this run
  command -v flock >/dev/null || die "flock is missing: the switch holds the apply lock"
  local lock=/run/lock/belfry-apply.lock
  [[ -w ${lock%/*} ]] || lock=/tmp/belfry-apply.lock
  exec 8>>"$lock"
  flock -w "$LOCK_WAIT" 8 || die "another 'belfry apply' (or update) holds $lock: try again when it is done (waited ${LOCK_WAIT}s)"
  # the 'belfry apply' run from here knows the lock is held (it would wait for itself)
  export BELFRY_APPLY_LOCKED=1
}

signer_container() { icompose ps -q signer 2>/dev/null | head -n1; }
signer_healthy() {
  local id; id=$(if [[ "$(signer_now)" == belfry ]]; then compose ps -q signer; else signer_container; fi | head -n1)
  [[ -n "$id" ]] && [[ "$(docker inspect --format '{{.State.Health.Status}}' "$id" 2>/dev/null)" == healthy ]]
}
wait_signer() {
  local i
  for i in $(seq 1 60); do signer_healthy && return 0; sleep 2; done
  die "the signer did not become healthy in two minutes (belfry logs signer)"
}

# ── check ───────────────────────────────────────────────────────────────────────────────────
cmd_check() {
  need_keycast_mode
  step "Versions"
  say "code: $(git -c safe.directory="$ROOT" -C "$ROOT" rev-parse --short HEAD) on $(git -c safe.directory="$ROOT" -C "$ROOT" rev-parse --abbrev-ref HEAD)"
  say "domain: $(domain); register: $(env_get REGISTER); sign-in: $(env_get PASS_SIGNIN); signer: $(signer_now)"
  need_own_signin
  # after the switch the apps' sign-in sends people to CONSOLE_URL/connect/authorize: empty, it
  # answers 404 and nobody new signs in to the chat (sessions that exist keep working)
  [[ -n "$(env_get CONSOLE_URL)" ]] \
    || die "CONSOLE_URL is empty in $ENV_FILE: after the switch the apps' sign-in would answer 404. Add CONSOLE_URL=https://belfry.$(domain) to $ENV_FILE (the switch's 'belfry apply' would also fill it in) and run check again"
  [[ -f "$INFRA/docker-compose.signer.yml" && -d "$ROOT/services/signer" ]] || die "this checkout has no signer (services/signer): update the code first"
  docker --version | head -n1
  step "Disk"
  local free; free=$(df -Pk "$ROOT" | awk 'NR==2 {printf "%d", $4/1024}')
  say "free on $(df -Pk "$ROOT" | awk 'NR==2 {print $6}'): ${free} MB"
  [[ "$free" -ge 1024 ]] || die "under 1 GB free: the signer's image and a backup do not fit"
  [[ "$free" -ge 4096 ]] || say "note: under 4 GB free. A signer image built here needs about 3 GB while it builds; a prebuilt one about 100 MB."
  step "Secrets"
  [[ -s "$INFRA/keycast/master.key" ]] || die "infra/keycast/master.key is missing: Keycast's keys cannot be opened without it"
  if [[ -s "$SECRETS/signer_master_key" ]]; then
    cmp -s "$INFRA/keycast/master.key" "$SECRETS/signer_master_key" \
      || die "infra/secrets/signer_master_key is not Keycast's master.key: the signer could not open Keycast's keys (it must be a copy; never replace it once the signer has data)"
    say "master key: the signer's is Keycast's."
  else
    say "master key: the signer's will be made from Keycast's by import (gen-secrets.sh)."
  fi
  local seed_dir; seed_dir=$(env_get CUSTODY_SEED_DIR); seed_dir=${seed_dir:-$SECRETS/custody}
  [[ "$seed_dir" == /* ]] || seed_dir="$INFRA/${seed_dir#./}"
  if [[ -s "$seed_dir/custody-seed" ]]; then say "custody seed: unlocked."; else say "custody seed: LOCKED or missing at $seed_dir/custody-seed. Unlock it before import (custody-seed.sh unlock)."; fi
  step "Sources"
  local users
  users=$(compose exec -T postgres psql -U postgres -d keycast -tAc "select count(*) from users" 2>/dev/null) \
    || die "Keycast's database does not answer (the postgres container, database keycast)"
  say "Keycast's database: $users users (all tenants)."
  local admins
  admins=$(compose exec -T onboarding python -c '
from belfry_onboarding.settings import Settings
from belfry_onboarding.clients.authentik import AuthentikClient
s = Settings()
print(len(AuthentikClient(s.authentik_url, s.authentik_token).group_member_emails(s.steward_group)))' 2>/dev/null | tail -n1) \
    || die "Authentik does not answer (its stewards group could not be read)"
  say "Authentik: $admins admins in the stewards group."
  local people
  people=$(compose exec -T onboarding python -m belfry_onboarding has-people 2>/dev/null | tail -n1) \
    || die "the register does not answer (is the stack running? belfry status)"
  say "The register: has people: $people."
  local age; age=$(backup_age)
  say "Last backup: $([[ -n "$age" ]] && echo "$((age / 3600)) h ago" || echo "none recorded")."
  step "What would be carried"
  local out="$STATE_DIR/plan-$(stamp).json"
  if py plan "$out" "${PY_FLAGS[@]}"; then
    [[ "$users" == 0 || "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["counts"]["accounts"])' "$out")" != 0 ]] \
      || die "Keycast has users and none in tenant $(env_get KEYCAST_TENANT_DOMAIN): KEYCAST_TENANT_DOMAIN in infra/.env is not the tenant they are in"
    say ""
    say "check: nothing stands in the way. Report: $out"
  else
    say ""
    die "check: problems above must be settled first (report: $out)"
  fi
}

# ── backup ──────────────────────────────────────────────────────────────────────────────────
cmd_backup() {
  step "Backup"
  need_this_box
  fresh_backup
  say "backup: done. import and switch may go on."
}

# ── import ──────────────────────────────────────────────────────────────────────────────────
start_import_signer() {
  if [[ ! -s "$SECRETS/signer_token" || ! -s "$SECRETS/signer_master_key" ]]; then
    say "Making the signer's token and its master key (a copy of Keycast's)."
    BELFRY_SIGNER_SECRETS=1 "$INFRA/scripts/gen-secrets.sh" >/dev/null
  fi
  cmp -s "$INFRA/keycast/master.key" "$SECRETS/signer_master_key" \
    || die "infra/secrets/signer_master_key is not Keycast's master.key: stop here"
  say "Starting the signer next to Keycast, without its bunker (a build here takes 15 to 30 minutes; a prebuilt image a minute)."
  icompose --profile app up -d --no-deps signer-init >/dev/null
  icompose --profile app up -d --no-deps signer >/dev/null
  wait_signer
  # it must not answer NIP-46 while Keycast does: two answers to one request
  local id; id=$(signer_container)
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$id" | grep -qx 'BUNKER_RELAYS=' \
    || die "the signer was started with a bunker relay: stopping (it must not answer while Keycast does)"
}

need_this_box() { # the state on record was made here, not restored from another box's backup
  box_id >/dev/null   # here, not in a subshell: a Postgres that does not answer stops us
  [[ -s "$STATE" ]] || return 0   # nothing on record yet: the first backup writes the box
  local box; box=$(state_get box)
  [[ -n "$box" ]] \
    || die "$STATE names no box: it cannot be told apart from another box's record. Move it aside to start over."
  [[ "$box" == "$BOX" ]] \
    || die "$STATE was made on another box (domain, master key or Keycast's database differ): it is not this belfry's record. Move it aside to start over."
}

cmd_import() {
  need_keycast_mode; need_own_signin; need_this_box
  local at; at=$(state_get backup_at)
  [[ -n "$at" && $(( $(now) - at )) -lt 86400 && "$at" -le $(( $(now) + 300 )) ]] \
    || die "no backup from the last 24 hours on record: run '$0 backup' first"
  step "The signer"
  start_import_signer
  step "Import"
  local out="$STATE_DIR/import-$(stamp).json"
  py import "$out" "${PY_FLAGS[@]}" || die "import: errors above (report: $out). Nothing of Keycast's was changed; settle them and run import again."
  state_set imported_at "$(now)" box "$(box_id)"
  say ""
  say "import: done (report: $out). The belfry still runs on Keycast. Run it again whenever"
  say "Keycast moved on (it brings the signer to Keycast's state). Next: $0 verify"
}

# ── verify ──────────────────────────────────────────────────────────────────────────────────
cmd_verify() {
  step "Verify"
  need_this_box
  if [[ "$(signer_now)" == keycast ]]; then signer_healthy || die "the signer is not running: run '$0 import' first"; fi
  local out="$STATE_DIR/verify-$(stamp).json"
  if py verify "$out" "${PY_FLAGS[@]}"; then
    if [[ "$(signer_now)" == belfry ]]; then state_set verified_after "$(now)" after_report "$out"
    else state_set verified_at "$(now)" verify_report "$out" verify_digest "$(report_field "$out" digest)"; fi
    say ""
    say "verify: CLEAN. Report: $out"
  else
    say ""
    die "verify: NOT clean (report: $out). If Keycast moved on since the import (a new member, a sign-in, a sign-out), run import again; anything else must be understood before a switch."
  fi
}

# ── switch ──────────────────────────────────────────────────────────────────────────────────
STOPPED=0
restart_old() {
  if [[ $STOPPED -eq 1 ]]; then
    say "Starting Keycast and the service again, as they were."
    compose --profile app start keycast onboarding >/dev/null 2>&1 || true
    STOPPED=0
  fi
}

cmd_switch() {
  need_keycast_mode; need_own_signin; need_tmux; need_this_box
  local d; d=$(domain)
  [[ -n "$(state_get verified_at)" ]] || die "no clean verify on record: run import and verify first (a rehearsal while the belfry runs)"
  # one apply at a time: held from here to the end (the apply below runs inside it)
  take_apply_lock
  step "Before anything stops"
  fresh_backup
  start_import_signer
  say ""
  say "The belfry at $d will be down for a few minutes: sign-ins, the chat's signing and"
  say "every page of the service stop until 'belfry apply' has finished."
  if [[ $YES -ne 1 ]]; then
    [[ -t 0 ]] || die "add --yes to switch without a terminal"
    local answer
    read -r -p "Type 'switch $d' to go on: " answer
    [[ "$answer" == "switch $d" ]] || die "nothing changed"
  fi
  local t0; t0=$(now)
  step "Stopping what writes to Keycast"
  # with Keycast (its API and its signer) and the service stopped, nothing writes to Keycast's
  # database: what the last import reads is what the signer will hold
  trap restart_old EXIT
  STOPPED=1
  compose --profile app stop onboarding keycast >/dev/null
  step "The last import (a sync: sign-outs, key moves, shared keys since the rehearsal)"
  local out="$STATE_DIR/import-switch-$(stamp).json"
  py import "$out" "${PY_FLAGS[@]}" || die "the last import had errors (report: $out): nothing was switched"
  step "The verify that decides"
  out="$STATE_DIR/verify-switch-$(stamp).json"
  py verify "$out" "${PY_FLAGS[@]}" || die "the verify is NOT clean (report: $out): nothing was switched"
  [[ "$(report_field "$out" ok)" == true ]] && report_of_this_box "$out" \
    || die "the verify report does not say ok for this box: nothing was switched"
  state_set verified_at "$(now)" verify_report "$out" switch_report "$out" verify_digest "$(report_field "$out" digest)" box "$(box_id)"
  # the migration's window ends with the verify that decides: from here no import syncs, and
  # a move to an own key drops the held key as everywhere else
  py close-window "$STATE_DIR/close-window-$(stamp).json" \
    || die "the signer's migration window could not be closed: nothing was switched"
  step "Switching"
  trap - EXIT   # from here on the way back is '$0 rollback', not a restart of the old services
  STOPPED=0
  BELFRY_NO_APPLY=1 BELFRY_SIGNER=belfry BELFRY_SIGNER_MIGRATED=1 "$BELFRY" config \
    || die "belfry config refused the switch. Keycast and the service are stopped: '$BELFRY compose --profile app start keycast onboarding' brings them back."
  state_set switched_at "$(now)"
  "$BELFRY" apply || die "belfry apply failed after the switch. Look at 'belfry status' and 'belfry logs onboarding'; '$0 rollback' goes back to Keycast."
  step "After the switch"
  wait_signer
  # the service may still be starting (its start check reads the signer): asked again for a
  # minute before giving up
  local tries=0
  until compose exec -T onboarding python -m belfry_onboarding has-people >/dev/null 2>&1; do
    tries=$((tries + 1))
    [[ $tries -lt 30 ]] || break
    sleep 2
  done
  [[ $tries -lt 30 ]] \
    || die "the service does not answer after the switch (belfry logs onboarding; it refuses to start when the signer does not hold its members). '$0 rollback' goes back."
  out="$STATE_DIR/verify-after-$(stamp).json"
  py verify "$out" "${PY_FLAGS[@]}" || die "the verify after the switch is NOT clean (report: $out). New accounts stay closed. '$0 rollback' goes back to Keycast."
  state_set verified_after "$(now)" after_report "$out"
  # the signer made no account of its own since the import: people may join again
  py open "$STATE_DIR/open-$(stamp).json" || die "new accounts could not be opened again: run '$0 open' (until then nobody new can join)"
  state_set opened_at "$(now)"
  say ""
  say "switch: done in $(( ($(now) - t0) / 60 )) min $(( ($(now) - t0) % 60 )) s. This belfry runs on its own signer."
  say "Check by hand now: a web chat session, an iPhone and an Android still sign (no new sign-in);"
  say "https://$d/.well-known/nostr.json?name=<a username>; the console's Status page."
  say "Keycast's and Authentik's data are untouched: '$0 rollback' goes back. '$0 cleanup' removes them, not before $CLEANUP_DAYS days."
}

cmd_open() {
  [[ "$(signer_now)" == belfry ]] || die "this belfry still runs on Keycast: new accounts open with the switch"
  need_this_box
  [[ -n "$(state_get verified_after)" ]] || die "no clean verify after the switch on record: run '$0 verify' first"
  py open "$STATE_DIR/open-$(stamp).json" || die "the signer did not take it"
  state_set opened_at "$(now)"
  say "open: the signer makes new accounts again."
}

# ── rollback ────────────────────────────────────────────────────────────────────────────────
# What happened on the signer since the switch, written back into Keycast before it starts
# again, so that a rollback does not bring back what was signed out or suspended: sessions
# Keycast still has live that the signer revoked, accounts Keycast has active that the signer
# suspended. Sessions opened on the signer since cannot go back (Keycast has no row for them).
# Fails (returns 1) instead of dying: the caller starts the service again first.
keycast_carry_back() {
  local tenant back n
  tenant=$(env_get KEYCAST_TENANT_DOMAIN); tenant=${tenant:-localhost}
  # read only: works with the custody seed locked (after a reboot); not with a signer that is
  # down (crash loop): then --skip-carry-back
  back=$(rows | compose --profile app run --rm -T --no-deps onboarding python -m belfry_onboarding signer-migrate carry-back --no-stewards) || {
    say "the signer's sign-outs and suspensions could not be read (is the signer up? belfry logs signer). Run rollback again, or with --skip-carry-back to go back without them."
    return 1
  }
  # on stdin, never as one argument: a long list of sign-outs is over the kernel's 128 KiB cap
  n=$(printf '%s' "$back" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["revoke"]) + len(d["suspend"]))') || return 1
  say "Carrying back to Keycast: $n sign-outs and suspensions made on the signer since the switch."
  [[ "$n" -gt 0 ]] || return 0
  # values checked here once more (64 hex, whole numbers): nothing else reaches the SQL
  printf '%s' "$back" | python3 -c '
import json, re, sys
d = json.load(sys.stdin)
hexkey = re.compile(r"^[0-9a-f]{64}$")
print("BEGIN;")
for bunker, at in d["revoke"]:
    assert hexkey.fullmatch(bunker) and int(at) > 0
    print(f"UPDATE oauth_authorizations SET revoked_at = to_timestamp({int(at)}) "
          f"WHERE bunker_public_key = \x27{bunker}\x27 AND revoked_at IS NULL "
          "AND tenant_id = (SELECT id FROM tenants WHERE domain = :\x27tenant\x27);")
for pubkey in d["suspend"]:
    assert hexkey.fullmatch(pubkey)
    print(f"UPDATE users SET status = \x27suspended\x27 WHERE pubkey = \x27{pubkey}\x27 "
          "AND status = \x27active\x27 "
          "AND tenant_id = (SELECT id FROM tenants WHERE domain = :\x27tenant\x27);")
print("COMMIT;")' \
    | compose exec -T postgres psql -U postgres -d keycast -q -v ON_ERROR_STOP=1 -v tenant="$tenant" -f - >/dev/null || {
    say "writing the sign-outs and suspensions into Keycast failed (one transaction: nothing of it was written)."
    return 1
  }
}

cmd_rollback() {
  [[ "$(signer_now)" == belfry ]] || die "this belfry runs on Keycast: nothing to roll back (a signer started by import is stopped with: BELFRY_SIGNER_IMPORT=1 $BELFRY compose stop signer)"
  [[ -z "$(state_get cleaned_at)" ]] || die "Keycast's and Authentik's data were removed by cleanup: there is nothing to go back to (restore the backup made before it)"
  need_tmux; need_this_box
  local d; d=$(domain)
  say "Going back to Keycast and Authentik on $d."
  say "- Sign-outs and suspensions made on the signer since the switch are written into Keycast"
  say "  first (sessions it still had live are revoked there, accounts suspended)."
  say "- NOT carried back: sessions opened since the switch (those devices sign in again),"
  say "  reactivated accounts (they stay suspended in Keycast), usernames, admins, people who"
  say "  joined since (they get the same key from Keycast when it is a derived one), and moves"
  say "  to an own key made on the signer: Keycast still has the old held key and account (the"
  say "  sessions such a move signed out are revoked in Keycast by the carry-back)."
  if [[ $YES -ne 1 ]]; then
    [[ -t 0 ]] || die "add --yes to roll back without a terminal"
    local answer
    read -r -p "Type 'rollback $d' to go on: " answer
    [[ "$answer" == "rollback $d" ]] || die "nothing changed"
  fi
  take_apply_lock
  step "Stopping the service"
  compose --profile app stop onboarding >/dev/null || true
  if [[ $SKIP_CARRY_BACK -eq 1 ]]; then
    say "--skip-carry-back: sign-outs and suspensions made on the signer since the switch are NOT"
    say "written into Keycast: sessions signed out there and accounts suspended there come back."
  else
    step "Carrying sign-outs and suspensions back to Keycast"
    if ! keycast_carry_back; then
      say "Starting the service again: nothing was rolled back, this belfry stays on its own signer."
      compose --profile app start onboarding >/dev/null 2>&1 || say "the service did not start: belfry apply"
      die "rollback stopped before anything changed"
    fi
  fi
  step "Stopping the signer"
  # it must not go on answering the sessions Keycast answers again
  compose --profile app stop signer >/dev/null || true
  compose --profile app rm -f signer signer-init >/dev/null 2>&1 || true   # the container, not its volume
  step "Back to Keycast"
  BELFRY_NO_APPLY=1 BELFRY_SIGNER=keycast BELFRY_SIGNER_MIGRATED=1 "$BELFRY" config \
    || die "belfry config refused"
  state_set rolled_back_at "$(now)"
  "$BELFRY" apply || die "belfry apply failed: see belfry status"
  say ""
  say "rollback: done. This belfry runs on Keycast and Authentik again; the signer's volume (signer-data) is kept."
}

# ── cleanup ─────────────────────────────────────────────────────────────────────────────────
cmd_cleanup() {
  [[ "$(signer_now)" == belfry ]] || die "this belfry does not run on its own signer"
  need_tmux; need_this_box
  local d switched report after; d=$(domain); switched=$(state_get switched_at)
  report=$(state_get switch_report); after=$(state_get after_report)
  [[ -n "$switched" ]] || die "no switch on record ($STATE): this belfry was not moved by this script"
  # a switch that stopped before 'open' leaves new accounts closed: cleanup removes the way back,
  # so the belfry must first be whole
  [[ -n "$(state_get opened_at)" ]] \
    || die "new accounts were never opened again after the switch (no 'open' on record): run '$0 open' first"
  local started; started=$(state_get cleanup_started)
  [[ -s "$report" && "$(report_field "$report" ok)" == true ]] \
    || die "the clean verify report the switch was decided on is missing ($STATE_DIR): nothing is deleted without it"
  report_of_this_box "$report" || die "the switch's report was not made on this box: nothing is deleted"
  [[ -s "$after" && "$(report_field "$after" ok)" == true ]] \
    || die "no clean verify report from after the switch: run '$0 verify' (Keycast's database must still be there for it)"
  report_of_this_box "$after" || die "the report from after the switch was not made on this box: nothing is deleted"
  local days=$(( ($(now) - switched) / 86400 ))
  if [[ $days -lt $CLEANUP_DAYS && $EARLY -ne 1 ]]; then
    die "the switch was $days days ago: cleanup waits $CLEANUP_DAYS days (a rollback needs what it deletes). --before-seven-days overrides."
  fi
  [[ -t 0 ]] || die "cleanup asks its question at a terminal: it never runs unattended"
  take_apply_lock
  step "A last look, and a backup that holds what is deleted"
  local last
  if [[ -z "$started" ]]; then
    last="$STATE_DIR/verify-cleanup-$(stamp).json"
    py verify "$last" "${PY_FLAGS[@]}" || die "the verify is not clean any more: nothing was deleted"
    state_set cleanup_verify "$last"
  else
    # a cleanup that stopped half-way: Keycast's tables may be gone, so no verify can run any
    # more; the one made before the first deletion stands
    last=$(state_get cleanup_verify)
    [[ -s "$last" && "$(report_field "$last" ok)" == true ]] && report_of_this_box "$last" \
      || die "a cleanup was started on $(date -u -d "@$started" '+%F %T UTC') but its clean verify report is missing ($last): nothing more is deleted"
    say "Resuming the cleanup started on $(date -u -d "@$started" '+%F %T UTC') (its verify: $last)."
  fi
  fresh_backup
  # keys from before the custody seed ("imported") exist only in signer-data and its backups
  # once Keycast's database is emptied: a backup made after the switch must hold them
  local imported; imported=$(python3 -c 'import json,sys; print(int((json.load(open(sys.argv[1])).get("signer_custody") or {}).get("imported") or 0))' "$last")
  if [[ "$imported" -gt 0 ]]; then
    local bf bt; bf=$(state_get backup_path); bt=$(state_get backup_at)
    [[ -n "$bf" && -n "$bt" && "$bt" -gt "$switched" ]] && backup_holds_signer "$bf" \
      && [[ "$(sha256sum "$bf" | cut -d' ' -f1)" == "$(state_get backup_sha)" ]] \
      || die "$imported accounts hold keys that only signer-data has (made before the custody seed), and no backup from after the switch that carries signer-data (checked against its own record, $bf.json) is here: nothing was deleted"
    state_set cleanup_backup "$bf" cleanup_backup_sha "$(state_get backup_sha)"
    say "Backup with the signer's keys: $bf (sha256 $(state_get backup_sha | cut -c1-16)…)."
  fi
  say ""
  say "This deletes, for good: the held keys the signer archived during the migration (members who"
  say "took their own key between import and switch), every table of Keycast's database (accounts, sealed keys, sessions)"
  say "and of Authentik's (users, groups, flows), Authentik's volumes (media, certificates), and"
  say "their containers. After it there is no rollback to Keycast. It keeps infra/keycast/master.key"
  say "(the signer's key is a copy of it and old backups need it), the custody seed and the backups."
  local answer
  read -r -p "Type 'delete keycast and authentik of $d' to go on: " answer
  [[ "$answer" == "delete keycast and authentik of $d" ]] || die "nothing was deleted"
  step "Deleting"
  # from the first deletion on, a rerun resumes (no verify: Keycast's tables may be gone)
  [[ -n "$started" ]] || state_set cleanup_started "$(now)"
  # held keys the signer archived during the migration's window (moves to an own key made in
  # Keycast between import and switch): kept until now as the window's safety net
  py drop-archive "$STATE_DIR/drop-archive-$(stamp).json" \
    || die "the signer's archived keys could not be deleted: cleanup stopped here (run it again)"
  say "deleted the signer's archived keys"
  compose --profile keycast rm -sf keycast keycast-migrate authentik-server authentik-worker redis >/dev/null \
    || die "removing Keycast's and Authentik's containers failed: cleanup stopped here (run it again)"
  say "removed Keycast's and Authentik's containers"
  # the databases stay, empty, as on a belfry installed with its own signer (postgres' health
  # check looks for the database keycast). The first failure stops here: cleaned_at is only
  # recorded when everything went.
  local db
  for db in keycast authentik; do
    compose exec -T postgres psql -U postgres -d "$db" -q -v ON_ERROR_STOP=1 \
      -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' >/dev/null \
      || die "emptying the database $db failed: cleanup stopped here (run it again)"
    say "emptied the database $db"
  done
  local project=${COMPOSE_PROJECT_NAME:-belfry} v
  for v in authentik-media authentik-certs; do
    if docker volume inspect "${project}_$v" >/dev/null 2>&1; then
      docker volume rm "${project}_$v" >/dev/null || die "removing the volume ${project}_$v failed: cleanup stopped here (run it again)"
      say "removed the volume ${project}_$v"
    fi
  done
  state_set cleaned_at "$(now)"
  say ""
  say "cleanup: done."
}

cmd_status() {
  say "signer: $(signer_now); sign-in: $(env_get PASS_SIGNIN)"
  local k v
  for k in backup_at imported_at verified_at switched_at verified_after opened_at rolled_back_at cleanup_started cleaned_at; do
    v=$(state_get "$k")
    say "$k: $([[ -n "$v" ]] && date -u -d "@$v" '+%F %T UTC' || echo -)"
  done
  for k in verify_report switch_report after_report backup_path cleanup_backup; do v=$(state_get "$k"); say "$k: ${v:--}"; done
}

case "$PHASE" in
  check) cmd_check ;;
  backup) cmd_backup ;;
  import) cmd_import ;;
  verify) cmd_verify ;;
  switch) cmd_switch ;;
  open) cmd_open ;;
  rollback) cmd_rollback ;;
  cleanup) cmd_cleanup ;;
  status) cmd_status ;;
  *) sed -n '2,/^set -euo/p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
