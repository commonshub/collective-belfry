#!/usr/bin/env bash
# Move a belfry from Keycast and Authentik to Belfry's own signer, with nobody signed out and no
# key changed (docs/own-signer.md §6 and "Moving a belfry": read that first).
#
#   migrate-to-own-signer.sh check      read-only: versions, disk, the three sources (Keycast's
#                                       database, Authentik, the register), what would be carried
#   migrate-to-own-signer.sh backup     a fresh encrypted backup (backup.sh): one from the last
#                                       hour, or it makes one; nothing goes on without it
#   migrate-to-own-signer.sh import     the signer starts next to Keycast (no bunker: it answers
#                                       nobody yet) and takes in accounts, keys, usernames,
#                                       states, sessions, admins. The belfry keeps running.
#   migrate-to-own-signer.sh verify     every account, key, username, session and admin compared
#                                       between Keycast, the signer and the register; a report
#                                       file; exit 1 on any difference
#   migrate-to-own-signer.sh switch     the belfry goes down for a few minutes: Keycast and the
#                                       service stop, a last import and a verify that must be
#                                       clean, then SIGNER=belfry and `belfry apply`
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
#   --yes                       no question before `switch` (never for `cleanup`)
#   --before-seven-days         `cleanup` although the switch is less than seven days old
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
BACKUP_STAMP="${BELFRY_BACKUP_STAMP:-$HOME/belfry-backups/last-success}"
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
YES=0 EARLY=0
PY_FLAGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --exclude) [[ "${2:-}" =~ ^[0-9a-f]{64}$ ]] || die "--exclude takes a public key (64 hex)"
               PY_FLAGS+=(--exclude "$2"); shift 2 ;;
    --accept-lost-sessions|--accept-missing-stewards)
               [[ "${2:-}" =~ ^[0-9]+$ ]] || die "$1 takes a number"
               PY_FLAGS+=("$1" "$2"); shift 2 ;;
    --yes) YES=1; shift ;;
    --before-seven-days) EARLY=1; shift ;;
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
  # after the switch Authentik is not running, and people sign in, join and rename: what is
  # compared from then on is the keys (the verify that decided ran with everything stopped)
  if [[ "$(signer_now)" == belfry && "$phase" != plan ]]; then flags+=(--no-stewards --keys-only); fi
  local rc=0
  rows | "${run[@]}" python -m belfry_onboarding signer-migrate "$phase" "${flags[@]}" > "$out.tmp" || rc=$?
  if [[ -s "$out.tmp" ]]; then tail -n1 "$out.tmp" > "$out"; fi
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

backup_age() { # seconds since the last successful backup, or nothing when there is none
  [[ -s "$BACKUP_STAMP" ]] || return 0
  echo $(( $(now) - $(cat "$BACKUP_STAMP") ))
}
fresh_backup() { # a backup from the last hour, or one made now; dies without
  local age; age=$(backup_age)
  if [[ -n "$age" && "$age" -lt 3600 ]]; then
    say "Backup: made $((age / 60)) minutes ago."
  else
    say "Backup: none from the last hour; making one (backup.sh run)."
    "$BACKUP" run || die "the backup failed: nothing goes on without one (set BACKUP_RECIPIENTS and BACKUP_TARGET; infra/DEPLOY.md)"
    age=$(backup_age)
    [[ -n "$age" && "$age" -lt 3600 ]] || die "backup.sh ran but recorded no success in $BACKUP_STAMP"
  fi
  state_set backup_at "$(cat "$BACKUP_STAMP")"
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
  fresh_backup
  say "backup: done. import and switch may go on."
}

# ── import ──────────────────────────────────────────────────────────────────────────────────
start_import_signer() {
  if [[ ! -s "$SECRETS/signer_token" || ! -s "$SECRETS/signer_master_key" ]]; then
    say "Making the signer's token and its master key (a copy of Keycast's)."
    "$INFRA/scripts/gen-secrets.sh" >/dev/null
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

cmd_import() {
  need_keycast_mode; need_own_signin
  local at; at=$(state_get backup_at)
  [[ -n "$at" && $(( $(now) - at )) -lt 86400 ]] \
    || die "no backup from the last 24 hours on record: run '$0 backup' first"
  step "The signer"
  start_import_signer
  step "Import"
  local out="$STATE_DIR/import-$(stamp).json"
  py import "$out" "${PY_FLAGS[@]}" || die "import: errors above (report: $out). Nothing of Keycast's was changed; settle them and run import again."
  state_set imported_at "$(now)"
  say ""
  say "import: done (report: $out). The belfry still runs on Keycast. Next: $0 verify"
}

# ── verify ──────────────────────────────────────────────────────────────────────────────────
cmd_verify() {
  step "Verify"
  if [[ "$(signer_now)" == keycast ]]; then signer_healthy || die "the signer is not running: run '$0 import' first"; fi
  local out="$STATE_DIR/verify-$(stamp).json"
  if py verify "$out" "${PY_FLAGS[@]}"; then
    if [[ "$(signer_now)" == belfry ]]; then state_set verified_after "$(now)" after_report "$out"
    else state_set verified_at "$(now)" verify_report "$out" verify_digest "$(report_field "$out" digest)"; fi
    say ""
    say "verify: CLEAN. Report: $out"
  else
    say ""
    die "verify: NOT clean (report: $out). If Keycast moved on since the import (a new member, a new sign-in), run import again; anything else must be understood before a switch."
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
  need_keycast_mode; need_own_signin
  local d; d=$(domain)
  [[ -n "$(state_get verified_at)" ]] || die "no clean verify on record: run import and verify first (a rehearsal while the belfry runs)"
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
  step "The last import"
  local out="$STATE_DIR/import-switch-$(stamp).json"
  py import "$out" "${PY_FLAGS[@]}" || die "the last import had errors (report: $out): nothing was switched"
  step "The verify that decides"
  out="$STATE_DIR/verify-switch-$(stamp).json"
  py verify "$out" "${PY_FLAGS[@]}" || die "the verify is NOT clean (report: $out): nothing was switched"
  [[ "$(report_field "$out" ok)" == true ]] || die "the verify report does not say ok: nothing was switched"
  state_set verified_at "$(now)" verify_report "$out" switch_report "$out" verify_digest "$(report_field "$out" digest)"
  step "Switching"
  trap - EXIT   # from here on the way back is '$0 rollback', not a restart of the old services
  STOPPED=0
  BELFRY_NO_APPLY=1 BELFRY_SIGNER=belfry BELFRY_SIGNER_MIGRATED=1 "$BELFRY" config \
    || die "belfry config refused the switch. Keycast and the service are stopped: '$BELFRY compose --profile app start keycast onboarding' brings them back."
  state_set switched_at "$(now)"
  "$BELFRY" apply || die "belfry apply failed after the switch. Look at 'belfry status' and 'belfry logs onboarding'; '$0 rollback' goes back to Keycast."
  step "After the switch"
  wait_signer
  compose exec -T onboarding python -m belfry_onboarding has-people >/dev/null \
    || die "the service does not answer after the switch (belfry logs onboarding; it refuses to start when the signer does not hold its members). '$0 rollback' goes back."
  out="$STATE_DIR/verify-after-$(stamp).json"
  py verify "$out" "${PY_FLAGS[@]}" || die "the verify after the switch is NOT clean (report: $out). '$0 rollback' goes back to Keycast."
  state_set verified_after "$(now)" after_report "$out"
  say ""
  say "switch: done in $(( ($(now) - t0) / 60 )) min $(( ($(now) - t0) % 60 )) s. This belfry runs on its own signer."
  say "Check by hand now: a web chat session, an iPhone and an Android still sign (no new sign-in);"
  say "https://$d/.well-known/nostr.json?name=<a username>; the console's Status page."
  say "Keycast's and Authentik's data are untouched: '$0 rollback' goes back. '$0 cleanup' removes them, not before $CLEANUP_DAYS days."
}

# ── rollback ────────────────────────────────────────────────────────────────────────────────
cmd_rollback() {
  [[ "$(signer_now)" == belfry ]] || die "this belfry runs on Keycast: nothing to roll back (a signer started by import is stopped with: BELFRY_SIGNER_IMPORT=1 $BELFRY compose stop signer)"
  [[ -z "$(state_get cleaned_at)" ]] || die "Keycast's and Authentik's data were removed by cleanup: there is nothing to go back to (restore the backup made before it)"
  local d; d=$(domain)
  say "Going back to Keycast and Authentik on $d. Sessions opened since the switch end (those"
  say "devices sign in again); usernames and admins changed since the switch are not carried back."
  if [[ $YES -ne 1 ]]; then
    [[ -t 0 ]] || die "add --yes to roll back without a terminal"
    local answer
    read -r -p "Type 'rollback $d' to go on: " answer
    [[ "$answer" == "rollback $d" ]] || die "nothing changed"
  fi
  step "Stopping the signer"
  # it must not go on answering the sessions Keycast answers again
  compose --profile app stop onboarding signer >/dev/null || true
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
  local d switched report after; d=$(domain); switched=$(state_get switched_at)
  report=$(state_get switch_report); after=$(state_get after_report)
  [[ -n "$switched" ]] || die "no switch on record ($STATE): this belfry was not moved by this script"
  [[ -s "$report" && "$(report_field "$report" ok)" == true ]] \
    || die "the clean verify report the switch was decided on is missing ($STATE_DIR): nothing is deleted without it"
  [[ -s "$after" && "$(report_field "$after" ok)" == true ]] \
    || die "no clean verify report from after the switch: run '$0 verify' (Keycast's database must still be there for it)"
  local days=$(( ($(now) - switched) / 86400 ))
  if [[ $days -lt $CLEANUP_DAYS && $EARLY -ne 1 ]]; then
    die "the switch was $days days ago: cleanup waits $CLEANUP_DAYS days (a rollback needs what it deletes). --before-seven-days overrides."
  fi
  [[ -t 0 ]] || die "cleanup asks its question at a terminal: it never runs unattended"
  step "A last look, and a backup that holds what is deleted"
  py verify "$STATE_DIR/verify-cleanup-$(stamp).json" "${PY_FLAGS[@]}" \
    || die "the verify is not clean any more: nothing was deleted"
  fresh_backup
  say ""
  say "This deletes, for good: every table of Keycast's database (accounts, sealed keys, sessions)"
  say "and of Authentik's (users, groups, flows), Authentik's volumes (media, certificates), and"
  say "their containers. After it there is no rollback to Keycast. It keeps infra/keycast/master.key"
  say "(the signer's key is a copy of it and old backups need it), the custody seed and the backups."
  local answer
  read -r -p "Type 'delete keycast and authentik of $d' to go on: " answer
  [[ "$answer" == "delete keycast and authentik of $d" ]] || die "nothing was deleted"
  step "Deleting"
  compose --profile keycast rm -sf keycast keycast-migrate authentik-server authentik-worker redis >/dev/null 2>&1 || true
  # the databases stay, empty, as on a belfry installed with its own signer (postgres' health
  # check looks for the database keycast)
  local db
  for db in keycast authentik; do
    compose exec -T postgres psql -U postgres -d "$db" -q -v ON_ERROR_STOP=1 \
      -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' >/dev/null \
      && say "emptied the database $db"
  done
  local project=${COMPOSE_PROJECT_NAME:-belfry} v
  for v in authentik-media authentik-certs; do
    if docker volume inspect "${project}_$v" >/dev/null 2>&1; then
      docker volume rm "${project}_$v" >/dev/null && say "removed the volume ${project}_$v"
    fi
  done
  state_set cleaned_at "$(now)"
  say ""
  say "cleanup: done."
}

cmd_status() {
  say "signer: $(signer_now); sign-in: $(env_get PASS_SIGNIN)"
  local k v
  for k in backup_at imported_at verified_at switched_at verified_after rolled_back_at cleaned_at; do
    v=$(state_get "$k")
    say "$k: $([[ -n "$v" ]] && date -u -d "@$v" '+%F %T UTC' || echo -)"
  done
  for k in verify_report switch_report after_report; do v=$(state_get "$k"); say "$k: ${v:--}"; done
}

case "$PHASE" in
  check) cmd_check ;;
  backup) cmd_backup ;;
  import) cmd_import ;;
  verify) cmd_verify ;;
  switch) cmd_switch ;;
  rollback) cmd_rollback ;;
  cleanup) cmd_cleanup ;;
  status) cmd_status ;;
  *) sed -n '2,40p' "$SELF" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
