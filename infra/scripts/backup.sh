#!/usr/bin/env bash
# Nightly encrypted backups, off the VM (security review 2026-10-24, docs/NEXT_STEPS.md §3t).
#
# On a steward's laptop, once:
#   backup.sh keygen <backup-identity.age> <yubikey recipient>…
#       a new backup key; its secret half is written encrypted to the YubiKeys (either one
#       decrypts), the public half (age1…) is printed: put it into the VM's infra/.env as
#       BACKUP_RECIPIENTS. The VM can encrypt backups but never read them.
#
# On the VM (in the repo checkout):
#   backup.sh run           one backup: Postgres (pg_dumpall), the data volumes, the secret files
#                           (.env, master.key, issuer keys, rendered configs) and the code as
#                           deployed; encrypted to BACKUP_RECIPIENTS, sent to BACKUP_TARGET,
#                           pruned to BACKUP_KEEP days there
#   backup.sh install       a systemd timer running `run` every night (BACKUP_AT, default 03:15)
#   backup.sh status        when the last backup succeeded (exit 1 when older than 26 hours)
#   backup.sh list          the backups at BACKUP_TARGET
#
# Anywhere with a YubiKey:
#   backup.sh decrypt <backup.tar.age> <backup-identity.age> <yubikey identity>… [-o <dir>]
#   backup.sh restore <dir> [--yes]
#                           into this checkout: files, volumes, databases (a fresh host: clone the
#                           repo or `tar -xpzf <dir>/code.tar.gz` first; the volumes must not exist yet)
#   backup.sh drill <dir>   restore into a throwaway copy (code from the backup, Compose project
#                           belfry-drill), compare with the backup's manifest, remove it again
#
# Never in a backup: the custody seed in plain (only custody-seed.age, if copied to
# infra/secrets/), Caddy's certificates (Let's Encrypt issues new ones), Redis, the NIP-46 relay.
#
# Settings (infra/.env or the environment):
#   BACKUP_RECIPIENTS  age recipients, space separated (age1…; a second, offline key is a good idea)
#   BACKUP_TARGET      /some/dir, or sftp://user@host[:port]/dir (SFTP only, e.g. a Hetzner
#                      Storage Box on port 23; turn on its snapshots, so a stolen VM key cannot
#                      delete history)
#   BACKUP_SSH_KEY     key for sftp targets (default ~/.ssh/belfry-backup); the target's host key
#                      must be in BACKUP_KNOWN_HOSTS (default ~/.ssh/known_hosts; ssh-keyscan -p 23 …)
#   BACKUP_KEEP        days to keep at the target (default 30); BACKUP_LOCAL_KEEP copies on the VM
#                      in ~/belfry-backups (default 2, encrypted like the rest)
# Needs age (`apt install age`); keygen and decrypt also age-plugin-yubikey (custody-seed.sh).
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA="$ROOT/infra"
AGE=${AGE:-age}
AGE_KEYGEN=${AGE_KEYGEN:-age-keygen}
die() { echo "backup: $*" >&2; exit 1; }
say() { echo "backup: $*"; }
need() { command -v "$1" >/dev/null || die "$1 not found (see the header of this script)"; }

envval() { # value of a variable from the environment, else from infra/.env
  local v=${!1:-}
  if [[ -z "$v" && -f "$INFRA/.env" ]]; then
    v=$(sed -n "s/^$1=//p" "$INFRA/.env" | tail -1)
    v=${v%\"}; v=${v#\"}
  fi
  printf '%s' "$v"
}

compose() {
  local files=(-f "$INFRA/docker-compose.yml")
  [[ -n "$(envval BELFRY_DOMAIN)" ]] && files+=(-f "$INFRA/docker-compose.prod.yml")
  docker compose --project-directory "$INFRA" "${files[@]}" --profile app "$@"
}
project() { compose config --format json | jq -r .name; }

# What a backup holds. Volumes: state that is not derived from Odoo (the relay whitelist config,
# the NIP-46 relay and Redis are). The SQLite and LMDB volumes are paused while they are copied.
VOLUMES=(odoo-data mint-data vote-data blossom-data community-relay-data authentik-media)
PAUSE=(mint vote blossom relay)
FILES=(infra/.env infra/keycast/master.key infra/issuer/keys infra/odoo/odoo.conf
  infra/issuer/config/issuer2-profiles.conf infra/issuer/config/issuer-service.conf
  infra/issuer/config/authentication-service.conf infra/verifier/config/verifier-service.conf
  infra/secrets infra/certs)

# Row counts of every table, per database (the drill compares them after a restore).
COUNTS_SQL="select table_schema||'.'||table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text from information_schema.tables where table_schema not in ('pg_catalog','information_schema') and table_type='BASE TABLE' order by 1"
counts() { # counts <psql command…>: JSON {db: {table: n}}
  local dbs db out='{}'
  dbs=$("$@" -d postgres -tAc "select datname from pg_database where not datistemplate and datname not in ('postgres') and datname not like 'test_%' order by 1")
  for db in $dbs; do
    out=$(jq --arg db "$db" --argjson t "$("$@" -d "$db" -tA -F $'\t' -c "$COUNTS_SQL" | jq -R 'split("\t") | {(.[0]): (.[1]|tonumber)}' | jq -s 'add // {}')" '.[$db] = $t' <<<"$out")
  done
  printf '%s' "$out"
}

cmd_keygen() {
  local out=${1:?usage: keygen <backup-identity.age> <yubikey recipient>…}; shift
  [[ $# -ge 1 ]] || die "name at least one recipient (the YubiKeys' age1yubikey1…)"
  [[ -e "$out" ]] && die "$out exists; a new key cannot read the backups made for the old one"
  need "$AGE"; need "$AGE_KEYGEN"
  local id pub args=()
  for r in "$@"; do args+=(-r "$r"); done
  id=$("$AGE_KEYGEN" 2>/dev/null)
  pub=$("$AGE_KEYGEN" -y <<<"$id")
  printf '%s\n' "$id" | "$AGE" "${args[@]}" -o "$out"
  unset id
  echo "wrote $out (keep it with every steward, like custody-seed.age)."
  echo "On the VM, in infra/.env:"
  echo "BACKUP_RECIPIENTS=$pub"
}

cmd_run() {
  need "$AGE"; need jq
  local recipients target keep local_keep stamp out name args=()
  paused=()  # global: the EXIT trap still sees them after this function returns
  recipients=$(envval BACKUP_RECIPIENTS); target=$(envval BACKUP_TARGET)
  keep=$(envval BACKUP_KEEP); keep=${keep:-30}
  local_keep=$(envval BACKUP_LOCAL_KEEP); local_keep=${local_keep:-2}
  [[ -n "$recipients" ]] || die "BACKUP_RECIPIENTS is empty (backup.sh keygen on a steward's laptop)"
  [[ -n "$target" ]] || die "BACKUP_TARGET is empty"
  for r in $recipients; do args+=(-r "$r"); done
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  name="belfry-$(hostname -s)-$stamp.tar.age"
  work=$(mktemp -d "${TMPDIR:-/var/tmp}/belfry-backup.XXXXXX")
  trap 'rm -rf "${work:-}"; [[ ${#paused[@]} -gt 0 ]] && compose unpause "${paused[@]}" >/dev/null 2>&1 || true' EXIT
  local proj; proj=$(project)
  mkdir -p "$work/b/volumes"

  # Postgres: every database and role, as one consistent dump per database.
  compose exec -T postgres pg_dumpall -U postgres --clean --if-exists | gzip > "$work/b/postgres.sql.gz" \
    || die "pg_dumpall failed"
  local psql=(compose exec -T postgres psql -U postgres)
  counts "${psql[@]}" > "$work/counts.json"

  # Volumes; the SQLite/LMDB ones with their service paused (a crash-consistent copy).
  local running; running=$(compose ps --services --status running)
  for s in "${PAUSE[@]}"; do grep -qx "$s" <<<"$running" && paused+=("$s"); done
  [[ ${#paused[@]} -gt 0 ]] && compose pause "${paused[@]}" >/dev/null 2>&1
  local vols='{}'
  for v in "${VOLUMES[@]}"; do
    docker volume inspect "${proj}_$v" >/dev/null 2>&1 || continue
    docker run --rm -v "${proj}_$v:/v:ro" alpine tar -C /v --exclude=./sessions -czf - . > "$work/b/volumes/$v.tar.gz"
    vols=$(jq --arg v "$v" --argjson n "$(gzip -dc "$work/b/volumes/$v.tar.gz" | tar -t | grep -vc '/$')" '.[$v] = $n' <<<"$vols")
  done
  [[ ${#paused[@]} -gt 0 ]] && compose unpause "${paused[@]}" >/dev/null 2>&1
  paused=()

  # Secret files (never the plain custody seed) and the code as it runs here.
  local present=()
  for f in "${FILES[@]}"; do [[ -e "$ROOT/$f" ]] && present+=("$f"); done
  tar -C "$ROOT" --exclude='infra/secrets/custody' -czf "$work/b/files.tar.gz" "${present[@]}"
  (cd "$ROOT" && git ls-files -co --exclude-standard -z | tar --null -T - -czf "$work/b/code.tar.gz")
  local sums; sums=$(cd "$ROOT" && tar -tzf "$work/b/files.tar.gz" | grep -v '/$' | xargs -d '\n' sha256sum)

  jq -n --arg host "$(hostname -f)" --arg at "$stamp" --arg proj "$proj" \
    --arg rev "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo none)" \
    --arg dirty "$(git -C "$ROOT" status --porcelain 2>/dev/null | wc -l)" \
    --slurpfile counts "$work/counts.json" --argjson volumes "$vols" --arg sums "$sums" \
    '{host:$host, created:$at, project:$proj, git:{rev:$rev, changed_files:($dirty|tonumber)},
      databases:$counts[0], volumes:$volumes,
      files:($sums | split("\n") | map(select(length>0) | capture("^(?<h>[0-9a-f]+)  (?<p>.*)$") | {(.p): .h}) | add // {})}' \
    > "$work/b/manifest.json"

  tar -C "$work/b" -cf - . | "$AGE" "${args[@]}" -o "$work/$name"
  out=$(du -h "$work/$name" | cut -f1)
  send "$target" "$work/$name"
  prune "$target" "$keep"

  mkdir -p "$HOME/belfry-backups"
  mv "$work/$name" "$HOME/belfry-backups/"
  ls -1t "$HOME/belfry-backups"/belfry-*.tar.age | tail -n +"$((local_keep + 1))" | xargs -r rm -f
  date -u +%s > "$HOME/belfry-backups/last-success"
  say "$name ($out) sent to $target"
}

# Targets: a directory, or sftp://user@host[:port]/dir (only the SFTP subsystem is used: put, ls,
# rm, which is all a Hetzner Storage Box or an rssh/chroot account offers).
sftp_batch() { # sftp_batch <target> <commands on stdin>
  local rest=${1#sftp://} dir port=22 key hosts
  dir=/${rest#*/}; rest=${rest%%/*}
  [[ "$rest" == *:* ]] && { port=${rest##*:}; rest=${rest%:*}; }
  key=$(envval BACKUP_SSH_KEY); key=${key:-$HOME/.ssh/belfry-backup}
  hosts=$(envval BACKUP_KNOWN_HOSTS); hosts=${hosts:-$HOME/.ssh/known_hosts}
  { echo "cd $dir"; cat; } | sftp -q -b - -P "$port" -i "$key" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o UserKnownHostsFile="$hosts" "$rest"
}
send() {
  if [[ "$1" == sftp://* ]]; then
    sftp_batch "$1" <<<"put $2" >/dev/null || die "upload to $1 failed"
  else
    mkdir -p "${1#file://}" && cp "$2" "${1#file://}/" || die "copy to $1 failed"
  fi
}
listing() {
  if [[ "$1" == sftp://* ]]; then
    sftp_batch "$1" <<<"ls -1" | grep -o 'belfry-[^/ ]*\.tar\.age$' || true
  else
    ls -1 "${1#file://}" 2>/dev/null | grep '^belfry-.*\.tar\.age$' || true
  fi
}
prune() { # prune <target> <days>: by the date in the name, never the newest one
  local cutoff f all; cutoff=$(date -u -d "-$2 days" +%Y%m%d)
  all=$(listing "$1" | sort)
  for f in $(head -n -1 <<<"$all"); do
    [[ "$f" =~ -([0-9]{8})T[0-9]{6}Z\.tar\.age$ && "${BASH_REMATCH[1]}" < "$cutoff" ]] || continue
    if [[ "$1" == sftp://* ]]; then
      sftp_batch "$1" <<<"rm $f" >/dev/null
    else
      rm -f "${1#file://}/$f"
    fi
  done
}

cmd_install() {
  local at user; at=$(envval BACKUP_AT); at=${at:-03:15}; user=$(id -un)
  need "$AGE"
  sudo tee /etc/systemd/system/belfry-backup.service >/dev/null <<EOF
[Unit]
Description=Belfry nightly encrypted backup
After=docker.service
[Service]
Type=oneshot
User=$user
WorkingDirectory=$ROOT
ExecStart=$ROOT/infra/scripts/backup.sh run
EOF
  sudo tee /etc/systemd/system/belfry-backup.timer >/dev/null <<EOF
[Unit]
Description=Belfry nightly encrypted backup
[Timer]
OnCalendar=*-*-* $at:00 UTC
RandomizedDelaySec=10m
Persistent=true
[Install]
WantedBy=timers.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now belfry-backup.timer
  systemctl list-timers belfry-backup.timer --no-pager
}

cmd_status() {
  local f="$HOME/belfry-backups/last-success" t
  [[ -f "$f" ]] || { echo "no backup yet"; exit 1; }
  t=$(cat "$f")
  echo "last backup $(date -u -d "@$t" '+%F %T UTC') ($(( ($(date +%s) - t) / 3600 )) h ago)"
  (( $(date +%s) - t < 26 * 3600 )) || exit 1
}

cmd_list() { listing "$(envval BACKUP_TARGET)"; }

cmd_decrypt() {
  local file=${1:?usage: decrypt <backup.tar.age> <backup-identity.age> <yubikey identity>… [-o dir]}
  local idfile=${2:?backup identity file}; shift 2
  local out="" ids=()
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == -o ]]; then out=$2; shift 2; else ids+=(-i "$1"); shift; fi
  done
  [[ ${#ids[@]} -gt 0 ]] || die "name the YubiKey identity file (age-plugin-yubikey --identity > id.txt)"
  need "$AGE"
  out=${out:-$(basename "$file" .tar.age)}
  [[ -e "$out" ]] && die "$out exists"
  mkdir -p "$out"
  # The backup key is decrypted with the YubiKey (touch/PIN) and only ever held in a pipe.
  "$AGE" -d -i <("$AGE" -d "${ids[@]}" "$idfile") "$file" | tar -C "$out" -xf - || { rm -rf "$out"; die "could not decrypt $file"; }
  say "unpacked into $out ($(jq -r '.host + ", " + .created' "$out/manifest.json"))"
}

cmd_restore() {
  local dir=${1:?usage: restore <dir> [--yes]}; dir=$(cd "$dir" && pwd)
  [[ -f "$dir/manifest.json" ]] || die "$dir has no manifest.json (backup.sh decrypt first)"
  if [[ "${2:-}" != --yes ]]; then
    read -r -p "Overwrite infra/.env and the secret files in $ROOT and fill the volumes of project $(project)? [y/N] " a
    [[ "$a" == y* ]] || exit 1
  fi
  local proj v
  tar -C "$ROOT" -xpzf "$dir/files.tar.gz"
  proj=$(project)
  docker volume inspect "${proj}_postgres" >/dev/null 2>&1 && die "volume ${proj}_postgres exists: restore needs a fresh host (or another project)"
  for f in "$dir"/volumes/*.tar.gz; do
    v=$(basename "$f" .tar.gz)
    docker volume inspect "${proj}_$v" >/dev/null 2>&1 && die "volume ${proj}_$v exists"
    docker volume create --label com.docker.compose.project="$proj" --label com.docker.compose.volume="$v" "${proj}_$v" >/dev/null
    docker run --rm -i -v "${proj}_$v:/v" alpine tar -C /v -xzf - < "$f"
    say "volume $v restored"
  done
  compose up -d --wait postgres >/dev/null
  # pg_dumpall cannot drop or create the role it connects as: those two errors are expected.
  local errs
  errs=$(gzip -dc "$dir/postgres.sql.gz" | compose exec -T postgres psql -U postgres -q -o /dev/null 2>&1 \
    | grep -E '^(psql:.*)?ERROR' | grep -v -e 'role "postgres" already exists' -e 'current user cannot be dropped' || true)
  [[ -z "$errs" ]] || { echo "$errs" | head -20 >&2; die "the database restore reported errors"; }
  say "databases restored"
  say "next: build the images, start the stack, unlock the custody seed (DEPLOY.md), run the smoke test"
}

cmd_drill() {
  local dir=${1:?usage: drill <dir>}; dir=$(cd "$dir" && pwd)
  [[ -f "$dir/manifest.json" ]] || die "$dir has no manifest.json"
  # globals: the EXIT trap runs after this function has returned
  tmp=$(mktemp -d "${TMPDIR:-/var/tmp}/belfry-drill.XXXXXX")
  P=belfry-drill
  cleanup() {
    COMPOSE_PROJECT_NAME=$P ROOT=$tmp INFRA=$tmp/infra compose down -v >/dev/null 2>&1 || true
    docker volume ls -q --filter label=com.docker.compose.project=$P | xargs -r docker volume rm >/dev/null
    rm -rf "$tmp"
  }
  trap cleanup EXIT
  tar -C "$tmp" -xpzf "$dir/code.tar.gz"
  # The drill copy must not touch the live stack: its own project, no seed, no host ports needed.
  ( export COMPOSE_PROJECT_NAME=$P; ROOT=$tmp INFRA=$tmp/infra cmd_restore "$dir" --yes )
  local fail=0

  # Files: the restored copies hash as recorded.
  local bad
  bad=$(jq -r '.files | to_entries[] | "\(.value)  \(.key)"' "$dir/manifest.json" | (cd "$tmp" && sha256sum -c --quiet 2>&1) || true)
  if [[ -z "$bad" ]]; then say "files: $(jq '.files|length' "$dir/manifest.json") restored, hashes match"; else echo "$bad" >&2; fail=1; fi

  # Volumes: as many files as were backed up.
  for v in $(jq -r '.volumes | keys[]' "$dir/manifest.json"); do
    local want got
    want=$(jq -r --arg v "$v" '.volumes[$v]' "$dir/manifest.json")
    got=$(docker run --rm -v "${P}_$v:/v:ro" alpine sh -c 'find /v -type f -o -type l | wc -l')
    if [[ "$want" == "$got" ]]; then say "volume $v: $got files"; else echo "volume $v: $got files, backup had $want" >&2; fail=1; fi
  done

  # Databases: every table's row count as at backup time (counted next to the dump, so busy
  # tables such as sessions may differ by a few rows: reported, not failed).
  local now diff
  now=$(cd "$tmp/infra" && COMPOSE_PROJECT_NAME=$P ROOT=$tmp INFRA=$tmp/infra counts compose exec -T postgres psql -U postgres)
  diff=$(jq -n --argjson a "$(jq .databases "$dir/manifest.json")" --argjson b "$now" \
    '[$a | to_entries[] | .key as $db | .value | to_entries[] | select($b[$db][.key] != .value)
      | "\($db).\(.key): \(.value) at backup, \($b[$db][.key] // "missing") restored"]')
  local key_tables='^(odoo\.public\.(belfry_pass|res_partner|account_move)|keycast\.public\.(users|personal_keys|oidc_identities)|authentik\.public\.authentik_core_user):'
  say "databases: $(jq -n --argjson b "$now" '[$b[]|length]|add') tables in $(jq -n --argjson b "$now" '$b|keys|join(", ")')"
  for k in odoo.public.belfry_pass odoo.public.res_partner keycast.public.users keycast.public.personal_keys authentik.public.authentik_core_user; do
    say "  ${k#*.public.}: $(jq -n --argjson b "$now" --arg db "${k%%.*}" --arg t "public.${k##*.public.}" '$b[$db][$t] // "missing"')"
  done
  if [[ "$(jq length <<<"$diff")" != 0 ]]; then
    jq -r '.[]' <<<"$diff" | sed 's/^/  differs: /'
    jq -r '.[]' <<<"$diff" | grep -Eq "$key_tables" && fail=1
  fi
  [[ $fail == 0 ]] && say "drill passed: $(jq -r '.host + " " + .created' "$dir/manifest.json") restores" || die "drill failed"
}

cmd=${1:-}; shift || true
case "$cmd" in
  keygen|run|install|status|list|decrypt|restore|drill) "cmd_$cmd" "$@" ;;
  *) sed -n '2,40p' "$0"; exit 2 ;;
esac
