#!/usr/bin/env bash
# Bootstraps a running Belfry stack. Idempotent: every step checks before it changes.
#
#  1. waits for Odoo, Authentik and Keycast health
#  2. initialises the Odoo database "odoo" (belfry_pass when the module exists, else the
#     standard modules it depends on), sets the admin password
#  3. creates the Odoo technical user for the onboarding service and an API key for it
#     (odoo shell, because res.users.apikeys.generate needs an existing key), stores the
#     belfry.onboarding_url / belfry.hook_token config parameters
#  4. bootstraps the Keycast admin (register with the ALLOWED_PUBKEYS key, verify through the
#     dev inbox, login, fetch the admin token) and inserts the belfry-event policy
#  5. waits for the Authentik bootstrap token and the pass-bridge blueprint
#  6. writes ODOO_API_KEY, KEYCAST_ADMIN_TOKEN, AUTHENTIK_TOKEN into infra/.env
#
# Usage: infra/scripts/bootstrap.sh   (needs curl, jq, python3, docker compose)
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$INFRA_DIR/.." && pwd)"
ENV_FILE="$INFRA_DIR/.env"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")
ODOO_URL=${ODOO_URL:-http://localhost:8069}
AK_URL=${AK_URL:-http://localhost:9000}
KC_URL=${KC_URL:-http://localhost:3000}
ODOO_DB=odoo
ODOO_STD_MODULES="base,sale_management,account_payment,payment_stripe,loyalty,sale_loyalty,base_automation,portal"
ODOO_LOG="$INFRA_DIR/.bootstrap-odoo.log"   # full output of the odoo CLI runs (gitignored with .env*)

[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE; run scripts/gen-secrets.sh first" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

log() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

# set_env KEY VALUE: replace KEY's value in .env in place, keeping its trailing comment.
set_env() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import re, sys
path, key, value = sys.argv[1:]
lines = open(path).read().splitlines(keepends=True)
done = False
for i, line in enumerate(lines):
    m = re.match(rf"^{re.escape(key)}=([^#\n]*?)(\s*#.*)?\n?$", line)
    if m:
        lines[i] = f"{key}={value}{m.group(2) or ''}\n"
        done = True
if not done:
    lines.append(f"{key}={value}\n")
open(path, "w").write("".join(lines))
PY
  export "$1=$2"
}

wait_for() { # name url [seconds]
  local name=$1 url=$2 timeout=${3:-300} i=0
  printf 'waiting for %s (%s) ' "$name" "$url"
  until curl -fsS -o /dev/null "$url"; do
    (( i += 5 )); [[ $i -ge $timeout ]] && die "$name not healthy after ${timeout}s"
    printf '.'; sleep 5
  done
  echo " ok"
}

# ---------- Odoo JSON-RPC helpers ----------
rpc() { # service method args_json
  curl -fsS "$ODOO_URL/jsonrpc" -H 'Content-Type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"call\",\"id\":1,\"params\":{\"service\":\"$1\",\"method\":\"$2\",\"args\":$3}}"
}
rpc_result() { # same as rpc, prints .result or fails with the error message
  local out; out=$(rpc "$@")
  if [[ $(jq -r 'has("error")' <<<"$out") == true ]]; then
    jq -r '.error.data.message // .error.message' <<<"$out" >&2; return 1
  fi
  jq -c '.result' <<<"$out"
}
odoo_auth() { # login password -> uid or empty
  local r; r=$(rpc common authenticate "[\"$ODOO_DB\",\"$1\",\"$2\",{}]" 2>/dev/null || true)
  jq -r '.result // empty | select(. != false)' <<<"$r" 2>/dev/null || true
}
# odoo_exec uid password model method args_json [kwargs_json]
odoo_exec() {
  rpc_result object execute_kw "[\"$ODOO_DB\",$1,\"$2\",\"$3\",\"$4\",$5,${6:-{\}}]"
}

# ---------- 1. health ----------
log "1/6 waiting for services"
wait_for odoo "$ODOO_URL/web/health"
wait_for authentik "$AK_URL/-/health/ready/"
wait_for keycast "$KC_URL/health"

# ---------- 2. Odoo database ----------
log "2/6 Odoo database"
mkdir -p "$REPO_DIR/odoo/addons"
psql_odoo() { "${COMPOSE[@]}" exec -T postgres psql -U postgres -d "$ODOO_DB" -tAc "$1" 2>/dev/null || true; }
# odoo_cli ARGS: run the odoo CLI in the running container; full log to $ODOO_LOG, errors to the terminal.
odoo_cli() {
  if ! "${COMPOSE[@]}" exec -T odoo odoo "$@" --no-http >"$ODOO_LOG" 2>&1; then
    grep -E 'ERROR|CRITICAL|Traceback' "$ODOO_LOG" | tail -n 20 >&2
    die "odoo $* failed, see $ODOO_LOG"
  fi
  grep -E ' (ERROR|CRITICAL) ' "$ODOO_LOG" | tail -n 10 >&2 || true
}
module_state() { psql_odoo "select state from ir_module_module where name='$1'" 2>/dev/null || true; }

if [[ -d "$REPO_DIR/odoo/addons/belfry_pass" ]]; then
  WANT_MODULES="base,belfry_pass"
else
  WANT_MODULES="$ODOO_STD_MODULES"
  echo "notice: odoo/addons/belfry_pass not found; installing the standard modules it depends on instead ($ODOO_STD_MODULES)"
fi

if [[ "$(psql_odoo "select 1 from information_schema.tables where table_name='ir_module_module'")" != "1" ]]; then
  echo "creating and initialising database $ODOO_DB with $WANT_MODULES (takes a minute or two)"
  odoo_cli -d "$ODOO_DB" -i "$WANT_MODULES" --stop-after-init --without-demo
elif [[ "$WANT_MODULES" == *belfry_pass* && "$(module_state belfry_pass)" != "installed" ]]; then
  echo "installing belfry_pass into the existing database"
  odoo_cli -d "$ODOO_DB" -i belfry_pass --stop-after-init --without-demo
elif [[ "$WANT_MODULES" == *belfry_pass* ]] && \
     [[ "$(psql_odoo "select latest_version from ir_module_module where name='belfry_pass'")" != \
        "$(sed -n "s/^ *'version': *'\([^']*\)'.*/\1/p" "$REPO_DIR/odoo/addons/belfry_pass/__manifest__.py")" ]]; then
  echo "upgrading belfry_pass (manifest version changed)"
  odoo_cli -d "$ODOO_DB" -u belfry_pass --stop-after-init --without-demo
  # The running server does not pick up new fields from a CLI upgrade (threaded mode, no
  # registry reload observed), so restart it.
  "${COMPOSE[@]}" restart odoo >/dev/null
  wait_for odoo "$ODOO_URL/web/health"
else
  echo "database $ODOO_DB already initialised"
fi
# The running server reloads its registry on the next request (registry signaling).

# admin password: DB is created with admin/admin
ADMIN_UID=$(odoo_auth admin "$ODOO_ADMIN_PASSWORD")
if [[ -z "$ADMIN_UID" ]]; then
  ADMIN_UID=$(odoo_auth admin admin)
  [[ -n "$ADMIN_UID" ]] || die "cannot log in to Odoo as admin with the configured or the default password"
  odoo_exec "$ADMIN_UID" admin res.users write "[[$ADMIN_UID],{\"password\":\"$ODOO_ADMIN_PASSWORD\"}]" >/dev/null
  echo "admin password set"
else
  echo "admin password already set"
fi
ADMIN_PW="$ODOO_ADMIN_PASSWORD"

# ---------- 3. onboarding user, API key, config parameters ----------
log "3/6 Odoo onboarding user and API key"
xmlid_res_id() { # module name -> res_id or empty
  odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.model.data search_read \
    "[[[\"module\",\"=\",\"$1\"],[\"name\",\"=\",\"$2\"]]]" '{"fields":["res_id"],"limit":1}' | jq -r '.[0].res_id // empty'
}
GROUP_IDS=()
for xid in base.group_user base.group_system sales_team.group_sale_manager account.group_account_manager belfry_pass.group_admin; do
  gid=$(xmlid_res_id "${xid%%.*}" "${xid#*.}")
  [[ -n "$gid" ]] && GROUP_IDS+=("$gid")
done
GROUPS_JSON=$(printf '%s\n' "${GROUP_IDS[@]}" | jq -sc '.')

ONB_UID=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.users search_read \
  "[[[\"login\",\"=\",\"$ODOO_ONBOARDING_LOGIN\"]]]" '{"fields":["id"],"limit":1,"context":{"active_test":false}}' | jq -r '.[0].id // empty')
if [[ -z "$ONB_UID" ]]; then
  ONB_UID=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.users create \
    "[{\"name\":\"Belfry onboarding service\",\"login\":\"$ODOO_ONBOARDING_LOGIN\",\"email\":\"$ODOO_ONBOARDING_LOGIN\",\"group_ids\":[[6,0,$GROUPS_JSON]]}]")
  echo "created user $ODOO_ONBOARDING_LOGIN (id $ONB_UID)"
else
  odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.users write "[[$ONB_UID],{\"active\":true,\"group_ids\":[[6,0,$GROUPS_JSON]]}]" >/dev/null
  echo "user $ODOO_ONBOARDING_LOGIN exists (id $ONB_UID), groups refreshed"
fi

if [[ -n "${ODOO_API_KEY:-}" && -n "$(odoo_auth "$ODOO_ONBOARDING_LOGIN" "$ODOO_API_KEY")" ]]; then
  echo "ODOO_API_KEY in .env still works"
else
  # res.users.apikeys.generate() over RPC needs an existing key of the same user, so the
  # first key comes from odoo shell (_generate in a sudo env allows a persistent key).
  echo "generating an API key for $ODOO_ONBOARDING_LOGIN"
  shell_out=$("${COMPOSE[@]}" exec -T odoo odoo shell -d "$ODOO_DB" --no-http 2>/dev/null <<PY
user = env['res.users'].search([('login', '=', '$ODOO_ONBOARDING_LOGIN')], limit=1)
Keys = env['res.users.apikeys'].sudo()
Keys.search([('user_id', '=', user.id), ('name', '=', 'onboarding')])._remove()
key = Keys.with_user(user.id).sudo()._generate(None, 'onboarding', None)
env.cr.commit()
print('APIKEY=' + key)
PY
)
  NEW_KEY=$(sed -n 's/^APIKEY=//p' <<<"$shell_out" | tail -n1)
  [[ -n "$NEW_KEY" ]] || { echo "$shell_out" >&2; die "odoo shell did not return an API key"; }
  [[ -n "$(odoo_auth "$ODOO_ONBOARDING_LOGIN" "$NEW_KEY")" ]] || die "the new API key does not authenticate"
  set_env ODOO_API_KEY "$NEW_KEY"
  echo "ODOO_API_KEY written to .env"
fi

odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.onboarding_url\",\"${ONBOARDING_URL:-http://onboarding:8100}\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.hook_token\",\"$HOOK_TOKEN\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.apps_url\",\"${KEYCAST_APP_URL:-http://localhost:3000}\"]" >/dev/null
echo "config parameters belfry.onboarding_url, belfry.hook_token and belfry.apps_url set"

# ---------- 4. Keycast ----------
log "4/6 Keycast admin and policy"
kc() { curl -sS -H 'Content-Type: application/json' -H "Origin: $KEYCAST_APP_URL" "$@"; }
kc_is_admin() { [[ "$(kc "$KC_URL/api/admin/status" -H "Authorization: Bearer $1" | jq -r '.is_admin // false' 2>/dev/null)" == true ]]; }

# The admin UCAN is signed by SERVER_NSEC, so it stays valid even after the database was wiped;
# also require the admin's user row, otherwise re-register (web login needs the row).
kc_admin_row() { "${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -tAc "select 1 from users where pubkey='$KEYCAST_ADMIN_PUBKEY'" | grep -q 1; }
if [[ -n "${KEYCAST_ADMIN_TOKEN:-}" ]] && kc_is_admin "$KEYCAST_ADMIN_TOKEN" && kc_admin_row; then
  echo "KEYCAST_ADMIN_TOKEN in .env still works and the admin user exists"
else
  reg=$(kc -X POST "$KC_URL/api/auth/register" \
    -d "{\"email\":\"$KEYCAST_ADMIN_EMAIL\",\"password\":\"$KEYCAST_ADMIN_PASSWORD\",\"nsec\":\"$KEYCAST_ADMIN_NSEC\"}")
  echo "register: $(jq -c 'del(.token)' <<<"$reg" 2>/dev/null || echo "$reg")"
  COOKIES=$(mktemp)
  login_out=$(kc -c "$COOKIES" -X POST "$KC_URL/api/auth/login" \
    -d "{\"email\":\"$KEYCAST_ADMIN_EMAIL\",\"password\":\"$KEYCAST_ADMIN_PASSWORD\"}")
  if grep -q EMAIL_NOT_VERIFIED <<<"$login_out"; then
    vtoken=$(kc "$KC_URL/api/dev/emails" | jq -r --arg to "$KEYCAST_ADMIN_EMAIL" \
      '[.[] | select((.to // .recipient // "") | test($to)) | .verification_url // empty] | last // empty' | sed 's/.*token=//')
    [[ -n "$vtoken" ]] || die "no verification mail for $KEYCAST_ADMIN_EMAIL in /api/dev/emails"
    kc -X POST "$KC_URL/api/auth/verify-email" -d "{\"token\":\"$vtoken\"}" >/dev/null
    echo "admin e-mail verified through the dev inbox"
    login_out=$(kc -c "$COOKIES" -X POST "$KC_URL/api/auth/login" \
      -d "{\"email\":\"$KEYCAST_ADMIN_EMAIL\",\"password\":\"$KEYCAST_ADMIN_PASSWORD\"}")
  fi
  SESSION=$(awk '$6 == "keycast_session" {print $7}' "$COOKIES")
  rm -f "$COOKIES"
  [[ -n "$SESSION" ]] || { echo "$login_out" >&2; die "Keycast login failed"; }
  kc_is_admin "$SESSION" || die "the Keycast admin session is not an admin; is KEYCAST_ADMIN_PUBKEY in ALLOWED_PUBKEYS?"
  tok=$(kc "$KC_URL/api/admin/token" -H "Authorization: Bearer $SESSION")
  ADMIN_TOKEN=$(jq -r '.token // .admin_token // empty' <<<"$tok")
  [[ -n "$ADMIN_TOKEN" ]] || { echo "$tok" >&2; die "no admin token returned"; }
  kc_is_admin "$ADMIN_TOKEN" || die "the fetched admin token does not pass /api/admin/status"
  set_env KEYCAST_ADMIN_TOKEN "$ADMIN_TOKEN"
  echo "KEYCAST_ADMIN_TOKEN written to .env"
fi

# policy.sql is idempotent and updates the allowed kinds of an existing permission, so apply it
# on every run.
"${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -v ON_ERROR_STOP=1 -q < "$INFRA_DIR/keycast/policy.sql"
echo "policy belfry-event applied: $("${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -tAc "select config from permissions where identifier='allowed_kinds_belfry_event'")"
kc "$KC_URL/api/policies" | jq -e '.policies | map(select(.slug == "belfry-event")) | length == 1' >/dev/null \
  || die "GET /api/policies does not list belfry-event"

# ---------- 5. Authentik ----------
log "5/6 Authentik"
ak() { curl -sS -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" -H 'Content-Type: application/json' "$@"; }
i=0
until ak -f -o /dev/null "$AK_URL/api/v3/admin/version/" 2>/dev/null; do
  (( i += 5 )); [[ $i -ge 180 ]] && die "the Authentik bootstrap token does not work after 180s (worker not done, or the token in .env changed after first start)"
  printf '.'; sleep 5
done
echo "bootstrap token works"
i=0
while :; do
  bp=$(ak "$AK_URL/api/v3/managed/blueprints/?search=belfry-pass-bridge-source" | jq -c '.results[0] // empty')
  status=$(jq -r '.status // empty' <<<"$bp")
  [[ "$status" == successful ]] && break
  (( i += 5 )); [[ $i -ge 120 ]] && { echo "$bp" | jq '.' >&2; die "blueprint belfry-pass-bridge-source status '$status'"; }
  printf '.'; sleep 5
done
echo "blueprint belfry-pass-bridge-source: successful"
ak "$AK_URL/api/v3/sources/oauth/?slug=pass-bridge" | jq -e '.pagination.count == 1' >/dev/null || die "source pass-bridge missing"
ak "$AK_URL/api/v3/core/groups/?name=belfry-participants" | jq -e '.pagination.count == 1' >/dev/null || die "group belfry-participants missing"
echo "source pass-bridge and group belfry-participants present"
# The Keycast provider blueprint binds the application to belfry-participants, so it fails when
# the worker applies it before the pass-bridge blueprint; apply it again once that one is done.
i=0
while :; do
  bp=$(ak "$AK_URL/api/v3/managed/blueprints/?search=belfry-keycast-provider" | jq -c '.results[0] // empty')
  status=$(jq -r '.status // empty' <<<"$bp")
  [[ "$status" == successful ]] && break
  if [[ -n "$bp" && "$status" != unknown ]]; then
    ak -f -o /dev/null -X POST "$AK_URL/api/v3/managed/blueprints/$(jq -r .pk <<<"$bp")/apply/" || true
  fi
  (( i += 5 )); [[ $i -ge 120 ]] && { echo "$bp" | jq '.' >&2; die "blueprint belfry-keycast-provider status '$status'"; }
  printf '.'; sleep 5
done
echo "blueprint belfry-keycast-provider: successful"
ak "$AK_URL/application/o/keycast/.well-known/openid-configuration" | jq -e '.issuer | endswith("/application/o/keycast/")' >/dev/null \
  || die "Keycast OIDC application not served at /application/o/keycast/"
echo "Keycast OIDC issuer: ${AUTHENTIK_PUBLIC_URL:-http://localhost:9000}/application/o/keycast/"
# The account-setup flow (participants choose a password through the link mailed at activation)
# must be the default brand's recovery flow, otherwise the onboarding service cannot mint links.
i=0
while :; do
  bp=$(ak "$AK_URL/api/v3/managed/blueprints/?search=belfry-account-setup-flow" | jq -c '.results[0] // empty')
  status=$(jq -r '.status // empty' <<<"$bp")
  [[ "$status" == successful ]] && break
  (( i += 5 )); [[ $i -ge 120 ]] && { echo "$bp" | jq '.' >&2; die "blueprint belfry-account-setup-flow status '$status'"; }
  printf '.'; sleep 5
done
SETUP_FLOW=$(ak "$AK_URL/api/v3/flows/instances/belfry-account-setup/" | jq -r '.pk // empty')
[[ -n "$SETUP_FLOW" ]] || die "flow belfry-account-setup missing"
ak "$AK_URL/api/v3/core/brands/" | jq -e --arg f "$SETUP_FLOW" '.results[] | select(.default) | .flow_recovery == $f' >/dev/null \
  || die "the default brand's recovery flow is not belfry-account-setup"
echo "blueprint belfry-account-setup-flow: successful, default brand recovery flow set"
set_env AUTHENTIK_TOKEN "$AUTHENTIK_BOOTSTRAP_TOKEN"

# ---------- 6. summary ----------
log "6/6 done"
cat <<EOF
Odoo        $ODOO_URL  admin / ODOO_ADMIN_PASSWORD; RPC user $ODOO_ONBOARDING_LOGIN with ODOO_API_KEY
Authentik   $AK_URL  akadmin / AUTHENTIK_BOOTSTRAP_PASSWORD; API token AUTHENTIK_TOKEN
Keycast     $KC_URL  $KEYCAST_ADMIN_EMAIL / KEYCAST_ADMIN_PASSWORD; admin UCAN KEYCAST_ADMIN_TOKEN (30 days)
Tokens are in $ENV_FILE. Restart the onboarding service to pick them up:
  docker compose -f infra/docker-compose.yml --profile app up -d
EOF
