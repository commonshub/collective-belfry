#!/usr/bin/env bash
# One-time move of a deployment from Keycast and Authentik to Belfry's signer and sign-in
# (2026-11-19). Run on the server after updating the code, `scripts/gen-secrets.sh` (it hands
# infra/keycast/master.key over as secrets/signer_master_key and writes the new secrets),
# `make up` and `make bootstrap` (the Odoo module renames keycast_pubkey to nostr_pubkey), and
# with the onboarding service running (`make up-app`). Every npub stays the same.
#
#   1. Keys: every key Keycast holds (its database, sealed with master.key) goes into the signer
#      unchanged, also keys made before the custody seed, which the seed cannot give back. The
#      signer tells which ones the seed reproduces ("derived") and which it keeps sealed only
#      ("imported": they live in the signer's data and its backups).
#   2. Accounts without a held key (members with their own key) and anything missing come from
#      Odoo (`restore-keys`); a key that neither has is reported as "mismatch".
#   3. Stewards: the members of Authentik's group belfry-stewards become stewards in Odoo
#      (res.partner.belfry_steward). Without a running Authentik, give them as STEWARDS="a@x b@y".
#   4. With --remove: stops and removes the old containers (keycast, keycast-migrate,
#      keycast-relay, authentik-server, authentik-worker, redis). With --drop-databases as well:
#      drops the keycast and authentik databases and roles (after a backup: scripts/backup.sh run).
#
# Idempotent: run it again any time; accounts the signer has are left as they are.
#
# Existing bunker connections (apps, phone signers, agents) end with the move: Belfry's apps sign
# in again by themselves; members re-connect outside apps from their account page
# (pass.<domain>/account, "Connect a Nostr app").
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")
[[ -f "$INFRA_DIR/docker-compose.prod.yml" && -n "${BELFRY_PROD:-}" ]] && COMPOSE+=(-f "$INFRA_DIR/docker-compose.prod.yml")
REMOVE=no; DROP=no
for arg in "$@"; do
  case "$arg" in
    --remove) REMOVE=yes ;;
    --drop-databases) REMOVE=yes; DROP=yes ;;
    *) echo "usage: $0 [--remove] [--drop-databases]" >&2; exit 2 ;;
  esac
done

log() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }
psql_q() { "${COMPOSE[@]}" exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -tA "$@"; }
onboarding() { "${COMPOSE[@]}" --profile app exec -T onboarding python -m belfry_onboarding "$@"; }
old_container() { docker ps -aq --filter "label=com.docker.compose.project=belfry" --filter "label=com.docker.compose.service=$1"; }

cmp -s "$INFRA_DIR/keycast/master.key" "$INFRA_DIR/secrets/signer_master_key" 2>/dev/null \
  || [[ ! -f "$INFRA_DIR/keycast/master.key" ]] \
  || die "secrets/signer_master_key is not infra/keycast/master.key: Keycast's keys would not open. Delete secrets/signer_master_key and run scripts/gen-secrets.sh"

# ---------- 1. keys out of Keycast's database ----------
log "1/4 keys from Keycast"
if [[ "$(psql_q -c "select 1 from pg_database where datname = 'keycast'")" == 1 ]]; then
  rows=$(psql_q -d keycast -c "
    SELECT json_build_object('vine_id', u.vine_id, 'pubkey', trim(p.user_pubkey),
                             'sealed', encode(p.encrypted_secret_key, 'hex'))
    FROM personal_keys p
    JOIN users u ON u.pubkey = p.user_pubkey AND u.tenant_id = p.tenant_id
    WHERE u.vine_id LIKE 'odoo-partner-%'
    ORDER BY u.vine_id")
  count=$(grep -c . <<<"$rows" || true)
  echo "Keycast holds $count participant keys"
  if [[ $count -gt 0 ]]; then
    result=$(onboarding import-keycast <<<"$rows")
    jq -r '.imported | group_by(.result) | map("\(.[0].result): \(length)") | join(", ")' <<<"$result"
    if jq -e '.imported | map(select(.result == "error")) | length > 0' <<<"$result" >/dev/null; then
      jq -c '.imported[] | select(.result == "error")' <<<"$result" >&2
      die "some keys did not import (above); nothing else was changed for them"
    fi
  fi
else
  echo "no keycast database: nothing to import (a fresh install, or migrated and dropped)"
fi

# ---------- 2. the rest from Odoo ----------
log "2/4 accounts from Odoo (restore-keys)"
report=$(onboarding restore-keys)
jq -r '.partners | to_entries | group_by(.value) | map("\(.[0].value): \(length)") | join(", ")' <<<"$report"
if jq -e '.partners | to_entries | map(select(.value == "mismatch" or .value == "locked")) | length > 0' <<<"$report" >/dev/null; then
  jq -c '.partners | with_entries(select(.value == "mismatch" or .value == "locked"))' <<<"$report" >&2
  echo "warning: these members have no key at the signer: 'locked' needs the custody seed unlocked" >&2
  echo "(scripts/custody-seed.sh unlock), 'mismatch' a key neither Keycast nor the seed has." >&2
fi

# ---------- 3. stewards ----------
log "3/4 stewards"
stewards=${STEWARDS:-}
ak=$(old_container authentik-server)
if [[ -z "$stewards" && -n "$ak" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$ak")" == true ]]; then
  stewards=$(docker exec "$ak" ak shell -c "
from authentik.core.models import Group
g = Group.objects.filter(name='belfry-stewards').first()
print(' '.join(sorted(u.email for u in g.users.filter(is_active=True) if u.email)) if g else '')
" 2>/dev/null | tail -n1)
fi
if [[ -n "$stewards" ]]; then
  tr ' ' '\n' <<<"$stewards" | onboarding import-stewards | jq -r '.stewards | length | "stewards in Odoo: \(.)"'
else
  echo "no stewards found (no Authentik running, STEWARDS unset); add them with: python -m belfry_onboarding add-steward <e-mail>"
fi

# ---------- 4. the old containers and databases ----------
log "4/4 Keycast and Authentik"
if [[ $REMOVE == yes ]]; then
  for svc in keycast keycast-migrate keycast-relay authentik-server authentik-worker redis; do
    id=$(old_container "$svc")
    [[ -n "$id" ]] && docker rm -f $id >/dev/null && echo "removed $svc"
  done
  for vol in belfry_authentik-media belfry_authentik-certs; do
    docker volume rm "$vol" >/dev/null 2>&1 && echo "removed volume $vol" || true
  done
  if [[ $DROP == yes ]]; then
    psql_q -c "DROP DATABASE IF EXISTS keycast" -c "DROP DATABASE IF EXISTS authentik" \
      -c "DROP ROLE IF EXISTS keycast" -c "DROP ROLE IF EXISTS authentik" >/dev/null
    echo "dropped the keycast and authentik databases"
  fi
else
  echo "kept the old containers and databases; run again with --remove (and --drop-databases) once the smoke test is green"
fi
cat <<EOF

Done. Next: make smoke. Secrets no longer used (delete them when the smoke test is green):
  infra/secrets/{authentik_*,keycast_*,bridge_client_secret,steward_oidc_client_secret,pass_page_oidc_client_secret}
  infra/keycast/master.key stays until the backups no longer need it (it is secrets/signer_master_key now)
EOF
