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
#  5. waits for the Authentik bootstrap token and the Belfry blueprints (pass bridge, Keycast
#     provider, sign-in link, magic-link login) and writes the SMTP settings into the
#     magic-link e-mail stage
#  6. writes ODOO_API_KEY, KEYCAST_ADMIN_TOKEN, AUTHENTIK_TOKEN into infra/secrets/ (scripts/secrets.sh)
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
ODOO_STD_MODULES="base,sale_management,account_payment,payment_stripe,payment_mollie,loyalty,sale_loyalty,base_automation,portal"
ODOO_LOG="$INFRA_DIR/.bootstrap-odoo.log"   # full output of the odoo CLI runs (gitignored with .env*)

[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE; run scripts/gen-secrets.sh first" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
. "$INFRA_DIR/scripts/secrets.sh"

log() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

wait_for() { # name url [seconds]
  local name=$1 url=$2 timeout=${3:-300} i=0
  printf 'waiting for %s (%s) ' "$name" "$url"
  until curl -fsS -o /dev/null "$url"; do
    (( i += 5 )); [[ $i -ge $timeout ]] && die "$name not healthy after ${timeout}s"
    printf '.'; sleep 5
  done
  echo " ok"
}
wait_for_any() { # name url [seconds]: any HTTP answer counts (behind the proxy the path may be denied)
  local name=$1 url=$2 timeout=${3:-300} i=0 code
  printf 'waiting for %s (%s) ' "$name" "$url"
  until code=$(curl -sS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null) && [[ "$code" =~ ^[1-4] ]]; do
    (( i += 5 )); [[ $i -ge $timeout ]] && die "$name not reachable after ${timeout}s"
    printf '.'; sleep 5
  done
  echo " ok ($code)"
}

# ---------- Odoo JSON-RPC helpers ----------
rpc() { # service method args_json
  # The body goes through stdin, not argv: it carries the admin and SMTP passwords, and argv is
  # visible to every user in ps.
  curl -fsS "$ODOO_URL/jsonrpc" -H 'Content-Type: application/json' --data-binary @- \
    <<<"{\"jsonrpc\":\"2.0\",\"method\":\"call\",\"id\":1,\"params\":{\"service\":\"$1\",\"method\":\"$2\",\"args\":$3}}"
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
wait_for issuer "${ISSUER_PUBLIC_URL:-http://localhost:7005}/.well-known/openid-credential-issuer/openid4vci"
wait_for_any verifier "${VERIFIER_PUBLIC_URL:-http://localhost:7004}/transaction-data-profiles"

# The groups relay's role and database (postgres/init.sh makes them only on a fresh volume). The
# password follows secrets/zooid_db_password on every run; restart groups-relay after a change.
"${COMPOSE[@]}" exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -q <<'SQL'
\set pw `cat /run/secrets/zooid_db_password`
SELECT 'CREATE ROLE zooid LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zooid') \gexec
ALTER ROLE zooid WITH LOGIN PASSWORD :'pw';
SELECT 'CREATE DATABASE zooid OWNER zooid TEMPLATE template0 ENCODING ''UTF8'' LC_COLLATE ''C'' LC_CTYPE ''C'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'zooid') \gexec
SQL
echo "groups relay database: zooid"

# ---------- 2. Odoo database ----------
log "2/6 Odoo database"
mkdir -p "$REPO_DIR/odoo/addons"
psql_odoo() { "${COMPOSE[@]}" exec -T postgres psql -U postgres -d "$ODOO_DB" -tAc "$1" 2>/dev/null || true; }
# odoo_cli ARGS: run the odoo CLI in the running container; full log to $ODOO_LOG, errors to the terminal.
odoo_cli() {
  if ! "${COMPOSE[@]}" exec -T odoo /belfry/with-secrets odoo "$@" --no-http >"$ODOO_LOG" 2>&1; then
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
  # base first, then the company's country and currency, then the rest: accounting installs the
  # chart of accounts and taxes of the company's country (l10n_be for BE) and fixes the currency
  # once journal items exist.
  COMPANY_COUNTRY=${COMPANY_COUNTRY:-BE}
  echo "creating database $ODOO_DB (base), company in $COMPANY_COUNTRY with ${COMPANY_CURRENCY:-EUR}"
  odoo_cli -d "$ODOO_DB" -i base --stop-after-init --without-demo
  [[ "$(psql_odoo "select count(*) from res_country where code='$COMPANY_COUNTRY'")" == 1 ]] || die "unknown COMPANY_COUNTRY $COMPANY_COUNTRY"
  psql_odoo "update res_partner set country_id=(select id from res_country where code='$COMPANY_COUNTRY') where id=(select partner_id from res_company where id=1)" >/dev/null
  psql_odoo "update res_currency set active=true where name='${COMPANY_CURRENCY:-EUR}'" >/dev/null
  psql_odoo "update res_company set currency_id=(select id from res_currency where name='${COMPANY_CURRENCY:-EUR}') where id=1" >/dev/null
  echo "initialising $ODOO_DB with $WANT_MODULES (takes a minute or two)"
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
# Least privilege for the service: an internal user with the Belfry admin group (belfry.*
# plus read on invoices and payment transactions, see the module's ACL) and contact
# management (member numbers, Authentik id and npub are written on res.partner). Everything
# the smoke test does beyond that runs as admin.
GROUP_IDS=()
for xid in base.group_user base.group_partner_manager belfry_pass.group_admin; do
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
  echo "ODOO_API_KEY still works"
else
  # res.users.apikeys.generate() over RPC needs an existing key of the same user, so the
  # first key comes from odoo shell (_generate in a sudo env allows a persistent key).
  echo "generating an API key for $ODOO_ONBOARDING_LOGIN"
  shell_out=$("${COMPOSE[@]}" exec -T odoo /belfry/with-secrets odoo shell -d "$ODOO_DB" --no-http 2>/dev/null <<PY
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
  set_secret ODOO_API_KEY "$NEW_KEY"
  echo "ODOO_API_KEY written to infra/secrets/odoo_api_key"
fi

odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.onboarding_url\",\"${ONBOARDING_URL:-http://onboarding:8100}\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.hook_token\",\"$HOOK_TOKEN\"]" >/dev/null
# set_param leaves unchanged values alone, so webhooks new in a module upgrade get their URLs here
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter belfry_sync_webhook_urls "[]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.apps_url\",\"${KEYCAST_APP_URL:-http://localhost:3000}\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.login_url\",\"${AUTHENTIK_PUBLIC_URL:-http://localhost:9000}\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.public_url\",\"${ONBOARDING_PUBLIC_URL:-http://localhost:8100}\"]" >/dev/null
# Odoo's own absolute links (invite URLs, payment links in mails) use web.base.url; freeze it so a
# login from another host name does not rewrite it.
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"web.base.url\",\"${ODOO_PUBLIC_URL:-http://localhost:8069}\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"web.base.url.freeze\",\"True\"]" >/dev/null
echo "config parameters belfry.onboarding_url, belfry.hook_token, belfry.apps_url, belfry.login_url, belfry.public_url and web.base.url (${ODOO_PUBLIC_URL:-http://localhost:8069}) set"

# Outgoing mail: the company (sender) and one ir.mail_server from SMTP_* (Mailpit locally).
COMPANY_ID=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.company search '[[]]' '{"limit":1}' | jq -r '.[0]')
# Company logo (invoices, portal navbar): the Open Source Village wordmark on the dark background.
COMPANY_LOGO=$(base64 -w0 "$INFRA_DIR/../brand/osv-logo-horizontal-dark.png")
odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.company write "[[$COMPANY_ID],{\"name\":\"${COMPANY_NAME:-Belfry}\",\"email\":\"${MAIL_FROM:-belfry@belfry.local}\",\"logo\":\"$COMPANY_LOGO\"}]" >/dev/null
# Company currency (the passes are priced in euro; a database created without a country starts in
# USD). Odoo refuses to change it once journal items exist, so only a fresh database is switched.
COMPANY_CURRENCY=${COMPANY_CURRENCY:-EUR}
CUR_NOW=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.company read "[[$COMPANY_ID]]" '{"fields":["currency_id"]}' | jq -r '.[0].currency_id[1]')
if [[ "$CUR_NOW" != "$COMPANY_CURRENCY" ]]; then
  if [[ "$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" account.move.line search_count '[[]]')" == "0" ]]; then
    CUR_ID=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.currency search "[[[\"name\",\"=\",\"$COMPANY_CURRENCY\"]]]" '{"limit":1,"context":{"active_test":false}}' | jq -r '.[0] // empty')
    [[ -n "$CUR_ID" ]] || die "currency $COMPANY_CURRENCY not found in Odoo"
    odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.currency write "[[$CUR_ID],{\"active\":true}]" >/dev/null
    odoo_exec "$ADMIN_UID" "$ADMIN_PW" res.company write "[[$COMPANY_ID],{\"currency_id\":$CUR_ID}]" >/dev/null
    echo "company currency set to $COMPANY_CURRENCY"
  else
    echo "WARNING: company currency is $CUR_NOW, not $COMPANY_CURRENCY, and journal items exist; Odoo cannot change it any more (fresh database needed)" >&2
  fi
fi
MS_ID=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.mail_server search '[[["name","=","belfry"]]]' '{"limit":1}' | jq -r '.[0] // empty')
# The password comes from infra/secrets/smtp_password (scripts/put-secret.sh) unless SMTP_PASSWORD is set.
SMTP_PASSWORD=${SMTP_PASSWORD:-$(cat "$INFRA_DIR/secrets/smtp_password" 2>/dev/null || true)}
export SMTP_PASSWORD   # jq reads it from the environment ($ENV), never from its argv
MS_VALS=$(jq -cn --arg host "${SMTP_HOST:-mailpit}" --argjson port "${SMTP_PORT:-1025}" \
  --arg enc "${SMTP_ENCRYPTION:-none}" --arg user "${SMTP_USER:-}" \
  --arg from "${MAIL_FROM:-belfry@belfry.local}" \
  '{name: "belfry", smtp_host: $host, smtp_port: $port, smtp_encryption: $enc, smtp_authentication: "login",
    smtp_user: $user, smtp_pass: $ENV.SMTP_PASSWORD, from_filter: $from, sequence: 1}')
if [[ -z "$MS_ID" ]]; then
  odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.mail_server create "[$MS_VALS]" >/dev/null
  echo "outgoing mail server 'belfry' created (${SMTP_HOST:-mailpit}:${SMTP_PORT:-1025})"
else
  odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.mail_server write "[[$MS_ID],$MS_VALS]" >/dev/null
  echo "outgoing mail server 'belfry' updated (${SMTP_HOST:-mailpit}:${SMTP_PORT:-1025})"
fi

# Stripe: Odoo's own Stripe provider takes the card payments (payment links), Belfry's endpoint
# /hooks/stripe the disputes. Keys: STRIPE_PUBLISHABLE_KEY in .env, the secret key in
# infra/secrets/stripe_secret_key (scripts/put-secret.sh). Test keys (sk_test_) put the
# provider in test mode, live keys enable it. Both webhooks are created through Stripe's API once
# the public URLs are https: Odoo's (action_stripe_create_webhook) and Belfry's dispute endpoint,
# whose signing secret goes into infra/secrets/stripe_webhook_secret (restart onboarding to read it).
STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY:-$(cat "$INFRA_DIR/secrets/stripe_secret_key" 2>/dev/null || true)}
export STRIPE_SECRET_KEY
if [[ -n "${STRIPE_PUBLISHABLE_KEY:-}" && -n "$STRIPE_SECRET_KEY" ]]; then
  case "$STRIPE_SECRET_KEY" in
    sk_test_*) STRIPE_STATE=test; [[ "$STRIPE_PUBLISHABLE_KEY" == pk_test_* ]] || die "STRIPE_PUBLISHABLE_KEY is not a test key while the secret key is" ;;
    sk_live_*) STRIPE_STATE=enabled; [[ "$STRIPE_PUBLISHABLE_KEY" == pk_live_* ]] || die "STRIPE_PUBLISHABLE_KEY is not a live key while the secret key is" ;;
    *) die "infra/secrets/stripe_secret_key is not a Stripe secret key (sk_test_… or sk_live_…)" ;;
  esac
  SP_ID=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider search '[[["code","=","stripe"]]]' '{"limit":1}' | jq -r '.[0] // empty')
  [[ -n "$SP_ID" ]] || die "Odoo has no Stripe payment provider (module payment_stripe)"
  SP_VALS=$(jq -cn --arg pk "$STRIPE_PUBLISHABLE_KEY" --arg state "$STRIPE_STATE" \
    '{stripe_publishable_key: $pk, stripe_secret_key: $ENV.STRIPE_SECRET_KEY, state: $state, is_published: true}')
  odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider write "[[$SP_ID],$SP_VALS]" >/dev/null
  echo "Stripe provider: $STRIPE_STATE mode"
  if [[ "${ODOO_PUBLIC_URL:-}" == https://* ]]; then
    HAS_SECRET=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider read "[[$SP_ID],[\"stripe_webhook_secret\"]]" | jq -r '.[0].stripe_webhook_secret // empty')
    if [[ -z "$HAS_SECRET" ]]; then
      odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider action_stripe_create_webhook "[[$SP_ID]]" >/dev/null \
        && echo "Stripe: Odoo's webhook created" || echo "WARNING: Odoo could not create its Stripe webhook (create it under Payment Providers › Stripe)" >&2
    fi
  fi
  if [[ "${ONBOARDING_PUBLIC_URL:-}" == https://* && -z "${STRIPE_WEBHOOK_SECRET:-}" ]]; then
    DISPUTE_URL="${ONBOARDING_PUBLIC_URL%/}/hooks/stripe"
    WH=$(curl -sS -H @<(printf 'Authorization: Bearer %s\n' "$STRIPE_SECRET_KEY") https://api.stripe.com/v1/webhook_endpoints \
      -d url="$DISPUTE_URL" -d "enabled_events[]=charge.dispute.created" -d "enabled_events[]=charge.dispute.closed" \
      -d description="Belfry disputes (onboarding service)")
    WH_SECRET=$(jq -r '.secret // empty' <<<"$WH")
    if [[ -n "$WH_SECRET" ]]; then
      set_secret STRIPE_WEBHOOK_SECRET "$WH_SECRET"
      echo "Stripe: dispute webhook for $DISPUTE_URL created; written to infra/secrets/stripe_webhook_secret (restart onboarding)"
    else
      echo "WARNING: Stripe refused the dispute webhook: $(jq -r '.error.message // .' <<<"$WH" | head -c 200)" >&2
    fi
  fi
else
  echo "Stripe: no keys (STRIPE_PUBLISHABLE_KEY, infra/secrets/stripe_secret_key)"
fi

# Mollie (Netherlands), next to Stripe or instead of it: Odoo's Mollie provider with Belfry's
# additions (belfry_pass models/payment_mollie.py: refunds from Odoo; refunds and chargebacks from
# Mollie's webhook revoke the pass). The API key in infra/secrets/mollie_api_key
# (scripts/put-secret.sh): test_… puts the provider in test mode, live_… enables it. No webhook to
# register: Odoo names its own /payment/mollie/webhook in every payment (needs a public https
# ODOO_PUBLIC_URL). MOLLIE_METHODS: the Odoo payment method codes to offer (the Mollie account must
# have them enabled too).
MOLLIE_API_KEY=${MOLLIE_API_KEY:-$(cat "$INFRA_DIR/secrets/mollie_api_key" 2>/dev/null || true)}
export MOLLIE_API_KEY
if [[ -n "$MOLLIE_API_KEY" ]]; then
  case "$MOLLIE_API_KEY" in
    test_*) MOLLIE_STATE=test ;;
    live_*) MOLLIE_STATE=enabled ;;
    *) die "infra/secrets/mollie_api_key is not a Mollie API key (test_… or live_…)" ;;
  esac
  MP_ID=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider search '[[["code","=","mollie"]]]' '{"limit":1}' | jq -r '.[0] // empty')
  [[ -n "$MP_ID" ]] || die "Odoo has no Mollie payment provider (module payment_mollie)"
  MP_VALS=$(jq -cn --arg state "$MOLLIE_STATE" '{mollie_api_key: $ENV.MOLLIE_API_KEY, state: $state, is_published: true}')
  odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider write "[[$MP_ID],$MP_VALS]" >/dev/null
  METHODS=$(jq -cn --arg m "${MOLLIE_METHODS:-card,ideal,bancontact}" '$m | split(",") | map(select(length > 0))')
  # Offer only what the Mollie account has enabled (Mollie refuses the others at checkout with
  # "The payment method is not enabled in your website profile"); Odoo codes differ for a few.
  ENABLED=$(curl -sS -H @<(printf 'Authorization: Bearer %s\n' "$MOLLIE_API_KEY") \
    "https://api.mollie.com/v2/methods?amount%5Bvalue%5D=10.00&amount%5Bcurrency%5D=${COMPANY_CURRENCY:-EUR}" | jq -c '[._embedded.methods[]?.id]' 2>/dev/null || echo '[]')
  MOLLIE_CODE='{"card":"creditcard","apple_pay":"applepay","bank_transfer":"banktransfer","kbc_cbc":"kbc","p24":"przelewy24","sepa_direct_debit":"directdebit"}'
  SKIPPED=$(jq -cn --argjson m "$METHODS" --argjson e "$ENABLED" --argjson c "$MOLLIE_CODE" '$m | map(select(($c[.] // .) as $x | $e | index($x) | not))')
  if [[ "$ENABLED" != "[]" && "$SKIPPED" != "[]" ]]; then
    echo "WARNING: not enabled in the Mollie account, not offered: $(jq -r 'join(", ")' <<<"$SKIPPED") (enable them in Mollie's dashboard, Settings › Payment methods, then run bootstrap again)" >&2
    SKIP_IDS=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.method search "[[[\"code\",\"in\",$SKIPPED],[\"active\",\"in\",[true,false]]]]" | jq -c '.')
    [[ "$SKIP_IDS" == "[]" ]] || odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider write "[[$MP_ID],{\"payment_method_ids\":$(jq -c 'map([3, .])' <<<"$SKIP_IDS")}]" >/dev/null
    METHODS=$(jq -cn --argjson m "$METHODS" --argjson s "$SKIPPED" '$m - $s')
  fi
  PM_IDS=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.method search "[[[\"code\",\"in\",$METHODS],[\"active\",\"in\",[true,false]]]]" | jq -c '.')
  if [[ "$PM_IDS" != "[]" ]]; then
    odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.method write "[$PM_IDS,{\"active\":true}]" >/dev/null
    odoo_exec "$ADMIN_UID" "$ADMIN_PW" payment.provider write "[[$MP_ID],{\"payment_method_ids\":$(jq -c 'map([4, .])' <<<"$PM_IDS")}]" >/dev/null
  fi
  echo "Mollie provider: $MOLLIE_STATE mode, methods $(jq -r 'join(", ")' <<<"$METHODS")"
  [[ "${ODOO_PUBLIC_URL:-}" == https://* ]] || echo "WARNING: Mollie calls Odoo's webhook at ODOO_PUBLIC_URL, which is not public https; payments stay pending until the return page" >&2
else
  echo "Mollie: no key (infra/secrets/mollie_api_key)"
fi
[[ -n "$STRIPE_SECRET_KEY$MOLLIE_API_KEY" ]] || echo "No payment provider configured: payments are marked paid by stewards"

# Invites from participants (belfry_friends.py) and the test-run notice on Odoo's pages (invite,
# payment, portal); while Mollie is in test mode the notice names Mollie's test card.
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.invites_per_participant\",\"${INVITES_PER_PARTICIPANT:-3}\"]" >/dev/null
NOTICE=${TEST_RUN_NOTICE:-}
if [[ -n "$NOTICE" && "${MOLLIE_STATE:-}" == test ]]; then
  NOTICE="$NOTICE Pay with Mollie's test card 4543 4740 0224 9996, any future expiry date, any CVC."
fi
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "$(jq -cn --arg v "$NOTICE" '["belfry.test_notice", $v]')" >/dev/null
echo "invites per participant: ${INVITES_PER_PARTICIPANT:-3}; test notice: ${NOTICE:-none}"

# VAT (decision 2026-10-26): the chart's 21% sale tax, included in the pass prices for
# individuals; organisations get the same rate on top of the company price (controllers/invite.py).
VAT_IDS=$(odoo_exec "$ADMIN_UID" "$ADMIN_PW" product.template belfry_setup_vat "[${PASS_VAT_RATE:-21}]") \
  || die "could not set up VAT on the pass products"
echo "VAT ${PASS_VAT_RATE:-21}% on the passes (included, excluded tax ids: $VAT_IDS)"

# ---------- 4. Keycast ----------
log "4/6 Keycast admin and policy"
# Keycast is called inside its container: behind the public proxy /api/admin/* and /api/dev/*
# are denied, and the tenant is the Host header (ALLOWED_TENANT_DOMAINS). Paths are relative.
kc() { "${COMPOSE[@]}" exec -T keycast curl -sS -H 'Content-Type: application/json' -H "Origin: $KEYCAST_APP_URL" -H "Host: ${KEYCAST_TENANT_DOMAIN:-localhost}" "${@/#\//http://localhost:3000/}"; }
kc_is_admin() { [[ "$(kc /api/admin/status -H "Authorization: Bearer $1" | jq -r '.is_admin // false' 2>/dev/null)" == true ]]; }

# The admin UCAN is signed by SERVER_NSEC, so it stays valid even after the database was wiped;
# also require the admin's user row, otherwise re-register (web login needs the row).
kc_admin_row() { "${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -tAc "select 1 from users where pubkey='$KEYCAST_ADMIN_PUBKEY'" | grep -q 1; }
if [[ -n "${KEYCAST_ADMIN_TOKEN:-}" ]] && kc_is_admin "$KEYCAST_ADMIN_TOKEN" && kc_admin_row; then
  echo "KEYCAST_ADMIN_TOKEN still works and the admin user exists"
else
  reg=$(kc -X POST /api/auth/register \
    -d "{\"email\":\"$KEYCAST_ADMIN_EMAIL\",\"password\":\"$KEYCAST_ADMIN_PASSWORD\",\"nsec\":\"$KEYCAST_ADMIN_NSEC\"}")
  echo "register: $(jq -c 'del(.token)' <<<"$reg" 2>/dev/null || echo "$reg")"
  kc_login() { # prints the response headers and body; the session cookie is read from Set-Cookie
    kc -D - -X POST /api/auth/login -d "{\"email\":\"$KEYCAST_ADMIN_EMAIL\",\"password\":\"$KEYCAST_ADMIN_PASSWORD\"}"
  }
  login_out=$(kc_login)
  if grep -q EMAIL_NOT_VERIFIED <<<"$login_out"; then
    vtoken=$(kc /api/dev/emails | jq -r --arg to "$KEYCAST_ADMIN_EMAIL" \
      '[.[] | select((.to // .recipient // "") | test($to)) | .verification_url // empty] | last // empty' | sed 's/.*token=//')
    [[ -n "$vtoken" ]] || die "no verification mail for $KEYCAST_ADMIN_EMAIL in /api/dev/emails"
    kc -X POST /api/auth/verify-email -d "{\"token\":\"$vtoken\"}" >/dev/null
    echo "admin e-mail verified through the dev inbox"
    login_out=$(kc_login)
  fi
  SESSION=$(sed -n 's/^[Ss]et-[Cc]ookie: keycast_session=\([^;]*\).*/\1/p' <<<"$login_out" | tr -d '\r' | tail -n1)
  [[ -n "$SESSION" ]] || { echo "$login_out" | tail -n 3 >&2; die "Keycast login failed"; }
  kc_is_admin "$SESSION" || die "the Keycast admin session is not an admin; is KEYCAST_ADMIN_PUBKEY in ALLOWED_PUBKEYS?"
  tok=$(kc /api/admin/token -H "Authorization: Bearer $SESSION")
  ADMIN_TOKEN=$(jq -r '.token // .admin_token // empty' <<<"$tok")
  [[ -n "$ADMIN_TOKEN" ]] || { echo "$tok" >&2; die "no admin token returned"; }
  kc_is_admin "$ADMIN_TOKEN" || die "the fetched admin token does not pass /api/admin/status"
  set_secret KEYCAST_ADMIN_TOKEN "$ADMIN_TOKEN"
  echo "KEYCAST_ADMIN_TOKEN written to infra/secrets/keycast_admin_token"
fi

# policy.sql is idempotent and updates the allowed kinds of an existing permission, so apply it
# on every run.
"${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -v ON_ERROR_STOP=1 -q \
  -v tenant="${KEYCAST_TENANT_DOMAIN:-localhost}" < "$INFRA_DIR/keycast/policy.sql"
echo "policy belfry-event applied: $("${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -tAc "select config from permissions where identifier='allowed_kinds_belfry_event'")"
kc /api/policies | jq -e '.policies | map(select(.slug == "belfry-event")) | length == 1' >/dev/null \
  || die "GET /api/policies does not list belfry-event"

# Belfry's own apps as registered OAuth clients: Keycast then accepts only their own redirect URI,
# and KEYCAST_TRUSTED_CLIENTS (compose) lets them skip the consent screen and sign in silently
# (prompt=none, Keycast patch 0008). The URIs follow the public URLs, so re-run after changing them.
"${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -v ON_ERROR_STOP=1 -q \
  -v chat_cb="${CHAT_PUBLIC_URL:-http://localhost:3300}/oauth/callback" \
  -v cal_cb="${CALENDAR_PUBLIC_URL:-http://localhost:3200}/oauth/callback" \
  -v vote_cb="${VOTE_PUBLIC_URL:-http://localhost:3500}/oauth/callback" \
  -v wallet_cb="${WALLET_PUBLIC_URL:-http://localhost:3400}/oauth/callback" \
  -v people_cb="${PEOPLE_PUBLIC_URL:-http://localhost:3600}/oauth/callback" \
  -v market_cb="${MARKET_PUBLIC_URL:-http://localhost:3700}/oauth/callback" \
  -v photos_cb="${PHOTOS_PUBLIC_URL:-http://localhost:3800}/oauth/callback" \
  -v programme_cb="${PROGRAMME_PUBLIC_URL:-http://localhost:3900}/oauth/callback" \
  -v groups_cb="${GROUPS_PUBLIC_URL:-http://localhost:3150}/oauth/callback" \
  -v tenant="${KEYCAST_TENANT_DOMAIN:-localhost}" <<'SQL'
-- Keycast resolves the tenant from the Host header (KEYCAST_TENANT_DOMAIN), not the seed tenant 1.
INSERT INTO registered_clients (tenant_id, client_id, name, allowed_redirect_uris)
SELECT t.id, c.client_id, c.name, ARRAY[c.cb]::TEXT[]
FROM tenants t,
     (VALUES ('belfry-chat', 'Open Source Village chat', :'chat_cb'),
             ('belfry-calendar', 'Open Source Village calendar', :'cal_cb'),
             ('belfry-vote', 'Open Source Village polls', :'vote_cb'),
             ('belfry-wallet', 'Open Source Village wallet', :'wallet_cb'),
             ('belfry-people', 'Open Source Village directory', :'people_cb'),
             ('belfry-market', 'Open Source Village marketplace', :'market_cb'),
             ('belfry-photos', 'Open Source Village photos', :'photos_cb'),
             ('belfry-programme', 'Open Source Village programme', :'programme_cb'),
             ('belfry-groups', 'Open Source Village groups', :'groups_cb')) AS c(client_id, name, cb)
WHERE t.domain = :'tenant'
ON CONFLICT (tenant_id, client_id) DO UPDATE
SET name = EXCLUDED.name, allowed_redirect_uris = EXCLUDED.allowed_redirect_uris, updated_at = NOW()
WHERE registered_clients.allowed_redirect_uris IS DISTINCT FROM EXCLUDED.allowed_redirect_uris
   OR registered_clients.name IS DISTINCT FROM EXCLUDED.name;
SQL
echo "registered clients: $("${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -tAc "select string_agg(client_id || ' ' || array_to_string(allowed_redirect_uris, ','), '; ' order by client_id) from registered_clients where client_id like 'belfry-%' and tenant_id = (select id from tenants where domain = '${KEYCAST_TENANT_DOMAIN:-localhost}')")"
[[ -n "$("${COMPOSE[@]}" exec -T postgres psql -U keycast -d keycast -tAc "select 1 from registered_clients r join tenants t on t.id = r.tenant_id where t.domain = '${KEYCAST_TENANT_DOMAIN:-localhost}' and r.client_id = 'belfry-chat'")" ]] \
  || die "belfry-chat is not registered for tenant ${KEYCAST_TENANT_DOMAIN:-localhost} (no tenant row yet?)"

# ---------- 5. Authentik ----------
log "5/6 Authentik"
# the token goes in as a header file, not as an argument (arguments are visible to everyone in ps)
ak() { curl -sS -H @<(printf 'Authorization: Bearer %s\n' "$AUTHENTIK_BOOTSTRAP_TOKEN") -H 'Content-Type: application/json' "$@"; }
i=0
until ak -f -o /dev/null "$AK_URL/api/v3/admin/version/" 2>/dev/null; do
  (( i += 5 )); [[ $i -ge 180 ]] && die "the Authentik bootstrap token does not work after 180s (worker not done, or infra/secrets/authentik_bootstrap_token changed after first start)"
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

# Participants and stewards are external users (2026-10-26): no authentik portal, and a sign-in
# with no app waiting ends on the apps page (brand default application). Users created before
# were internal; move them over (akadmin and other users outside belfry/ stay as they are).
for ak_path in "${AUTHENTIK_USER_PATH:-belfry/participants}" belfry/stewards; do
  ak "$AK_URL/api/v3/core/users/?path=$ak_path&type=internal&page_size=500" | jq -r '.results[].pk' |
    while read -r pk; do
      ak -f -o /dev/null -X PATCH "$AK_URL/api/v3/core/users/$pk/" -d '{"type":"external"}' \
        || die "could not make authentik user $pk external"
      echo "authentik user $pk ($ak_path): now external"
    done
done
# Blueprints read client secrets with !File and store a placeholder when the file was missing at
# that moment (the worker applies a changed blueprint as soon as git updates it, possibly before
# gen-secrets wrote the secret): re-apply every Belfry blueprint until no provider keeps one.
i=0
while ak "$AK_URL/api/v3/providers/oauth2/?page_size=100" | jq -e '[.results[] | select((.client_secret // "") | startswith("placeholder"))] | length > 0' >/dev/null; do
  (( i += 5 )); [[ $i -ge 60 ]] && die "Authentik providers still have placeholder client secrets: $(ak "$AK_URL/api/v3/providers/oauth2/?page_size=100" | jq -r '[.results[] | select((.client_secret // "") | startswith("placeholder")) | .name] | join(", ")') (check infra/secrets/ and the worker's /run/secrets)"
  for pk in $(ak "$AK_URL/api/v3/managed/blueprints/?search=belfry-&page_size=100" | jq -r '.results[].pk'); do
    ak -f -o /dev/null -X POST "$AK_URL/api/v3/managed/blueprints/$pk/apply/" || true
  done
  printf '.'; sleep 5
done
echo "Authentik providers: client secrets from infra/secrets/"
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
ak "$AK_URL/api/v3/core/brands/" | jq -e --arg f "$SETUP_FLOW" '[.results[] | select(.flow_recovery == $f and .default)] | length >= 1' >/dev/null \
  || die "no default brand carries the recovery flow belfry-account-setup (blueprint belfry-account-setup-flow)"
echo "blueprint belfry-account-setup-flow: successful, default brand recovery flow set"
# Branding (blueprint belfry-brand): the worker may have applied the file before the container
# had BELFRY_BRAND_URL (then the !Env fallback, localhost, is stored), so re-apply when the
# brand's logo does not point at the configured brand URL.
BRAND_URL="${ONBOARDING_PUBLIC_URL:-http://localhost:8100}/brand"
BRAND_BP=$(ak "$AK_URL/api/v3/managed/blueprints/?search=belfry-brand" | jq -r '.results[] | select(.name|test("belfry-brand")) | .pk' | head -n1)
[[ -n "$BRAND_BP" ]] || die "blueprint belfry-brand not found (infra/authentik/blueprints/belfry-brand.yaml)"
brand_logo() { ak "$AK_URL/api/v3/core/brands/" | jq -r '.results[] | select(.domain=="authentik-default") | .branding_logo'; }
if [[ "$(brand_logo)" != "$BRAND_URL/"* ]]; then
  ak -f -o /dev/null -X POST "$AK_URL/api/v3/managed/blueprints/$BRAND_BP/apply/" || die "could not re-apply blueprint belfry-brand"
  i=0; while [[ "$(brand_logo)" != "$BRAND_URL/"* ]]; do (( i += 3 )); [[ $i -ge 90 ]] && die "brand logo still not under $BRAND_URL after re-applying belfry-brand"; printf '.'; sleep 3; done
fi
echo "blueprint belfry-brand: Open Source Village branding from $BRAND_URL"
# No avatars on Authentik's pages: after the e-mail is typed, every step shows the pending user's
# avatar, also to someone who typed another participant's address; a picture from the Nostr key
# would link the address to that identity, and the default (gravatar,initials) sends a hash of
# the address to gravatar.com. "none" shows no picture at all.
ak -f -o /dev/null -X PATCH "$AK_URL/api/v3/admin/settings/" -d '{"avatars":"none"}' || die "could not set Authentik's avatars to none"
echo "authentik: no avatars"
# Participants sign in by magic link (blueprint belfry-magic-link-flow): the flow must be the
# default brand's authentication flow, and its e-mail stage gets the SMTP settings here (the
# blueprint leaves them out so a re-apply never resets them; same values as Odoo's mail server).
i=0
while :; do
  bp=$(ak "$AK_URL/api/v3/managed/blueprints/?search=belfry-magic-link-flow" | jq -c '.results[0] // empty')
  status=$(jq -r '.status // empty' <<<"$bp")
  [[ "$status" == successful ]] && break
  (( i += 5 )); [[ $i -ge 120 ]] && { echo "$bp" | jq '.' >&2; die "blueprint belfry-magic-link-flow status '$status'"; }
  printf '.'; sleep 5
done
LOGIN_FLOW=$(ak "$AK_URL/api/v3/flows/instances/belfry-magic-link/" | jq -r '.pk // empty')
[[ -n "$LOGIN_FLOW" ]] || die "flow belfry-magic-link missing"
ak "$AK_URL/api/v3/core/brands/" | jq -e --arg f "$LOGIN_FLOW" '[.results[] | select(.flow_authentication == $f and .default)] | length >= 1' >/dev/null \
  || die "the default brand's authentication flow is not belfry-magic-link (blueprint belfry-magic-link-flow)"
EMAIL_STAGE=$(ak "$AK_URL/api/v3/stages/email/?name=belfry-magic-link-email" | jq -r '.results[0].pk // empty')
[[ -n "$EMAIL_STAGE" ]] || die "e-mail stage belfry-magic-link-email missing"
STAGE_SMTP=$(jq -cn --arg host "${SMTP_HOST:-mailpit}" --argjson port "${SMTP_PORT:-1025}" \
  --arg user "${SMTP_USER:-}" --arg enc "${SMTP_ENCRYPTION:-none}" \
  --arg from "${MAIL_FROM:-belfry@belfry.local}" \
  '{use_global_settings: false, host: $host, port: $port, username: $user, password: $ENV.SMTP_PASSWORD,
    use_tls: ($enc == "starttls"), use_ssl: ($enc == "ssl"), timeout: 30, from_address: $from}')
ak -f -o /dev/null -X PATCH "$AK_URL/api/v3/stages/email/$EMAIL_STAGE/" --data-binary @- <<<"$STAGE_SMTP" \
  || die "could not write the SMTP settings into the e-mail stage"
echo "blueprint belfry-magic-link-flow: successful, brand login flow set, magic links sent via ${SMTP_HOST:-mailpit}:${SMTP_PORT:-1025}"
# Blueprints that read a client secret (!Env) are applied once more on every run: the worker
# applies a new blueprint file as soon as it appears, possibly before the container has the
# secret, and then keeps the placeholder. The apply runs asynchronously: wait for a newer
# last_applied, not just for "successful".
reapply_blueprint() {
  local name=$1 bp before status i=0
  bp=$(ak "$AK_URL/api/v3/managed/blueprints/?search=$name" | jq -c '.results[0] // empty')
  before=$(jq -r '.last_applied // empty' <<<"$bp")
  [[ -n "$bp" ]] && ak -f -o /dev/null -X POST "$AK_URL/api/v3/managed/blueprints/$(jq -r .pk <<<"$bp")/apply/" || true
  while :; do
    bp=$(ak "$AK_URL/api/v3/managed/blueprints/?search=$name" | jq -c '.results[0] // empty')
    status=$(jq -r '.status // empty' <<<"$bp")
    [[ "$status" == successful && "$(jq -r '.last_applied // empty' <<<"$bp")" != "$before" ]] && break
    (( i += 5 )); [[ $i -ge 120 ]] && { echo "$bp" | jq '.' >&2; die "blueprint $name status '$status'"; }
    printf '.'; sleep 5
  done
}
# Link and code (2026-10-26): the flow's code stage mails both; the e-mail stage only holds the
# SMTP settings. Apply the flow again (the worker does not always notice an edited file) and
# remove the e-mail stage's binding that installs from before still have.
reapply_blueprint belfry-magic-link-flow
for b in $(ak "$AK_URL/api/v3/flows/bindings/?target=$LOGIN_FLOW&stage=$EMAIL_STAGE" | jq -r '.results[].pk'); do
  ak -f -o /dev/null -X DELETE "$AK_URL/api/v3/flows/bindings/$b/" || die "could not unbind the e-mail stage"
  echo "magic-link flow: e-mail stage unbound (link and code are mailed by the code stage)"
done
ak "$AK_URL/api/v3/flows/bindings/?target=$LOGIN_FLOW" | jq -e '[.results[].stage_obj.name] | index("belfry-magic-link-code")' >/dev/null \
  || die "flow belfry-magic-link has no code stage (blueprint belfry-magic-link-flow)"
# Steward pages (/steward in the onboarding service): their OIDC application and the group.
reapply_blueprint belfry-steward-app
# The brand's custom CSS changes with the repo (mobile fixes 2026-10-24); the worker does not
# always notice an edited file, so apply it on every run.
reapply_blueprint belfry-brand
ak "$AK_URL/api/v3/core/groups/?name=belfry-stewards" | jq -e '.results | length == 1' >/dev/null || die "group belfry-stewards missing"
echo "blueprint belfry-steward-app: successful (add stewards: docker compose exec onboarding python -m belfry_onboarding add-steward <e-mail> [name])"
# The pass on the phone (/me): participants sign in to it like to the apps.
reapply_blueprint belfry-pass-app
ak "$AK_URL/api/v3/providers/oauth2/?client_id=belfry-pass" \
  | jq -e --arg s "${PASS_PAGE_OIDC_CLIENT_SECRET:-}" '.results[0].client_secret == $s and $s != ""' >/dev/null \
  || die "provider belfry-pass missing or without PASS_PAGE_OIDC_CLIENT_SECRET (run gen-secrets.sh, recreate authentik)"
echo "blueprint belfry-pass-app: successful (the pass on the phone at /me)"
# Issuer2's login for authorization-code pass offers (DIDroom and other wallets that sign in).
reapply_blueprint belfry-issuer-app
ak "$AK_URL/api/v3/providers/oauth2/?client_id=belfry-issuer" \
  | jq -e --arg s "${ISSUER_OIDC_CLIENT_SECRET:-}" '.results[0].client_secret == $s and $s != ""' >/dev/null \
  || die "provider belfry-issuer missing or without ISSUER_OIDC_CLIENT_SECRET (run gen-secrets.sh, recreate authentik)"
echo "blueprint belfry-issuer-app: successful (issuer sign-in for authorization-code offers)"
set_secret AUTHENTIK_TOKEN "$AUTHENTIK_BOOTSTRAP_TOKEN"

# ---------- 6. summary ----------
log "6/6 done"
cat <<EOF
Odoo        $ODOO_URL  admin / ODOO_ADMIN_PASSWORD; RPC user $ODOO_ONBOARDING_LOGIN with ODOO_API_KEY
Authentik   $AK_URL  akadmin / AUTHENTIK_BOOTSTRAP_PASSWORD; API token AUTHENTIK_TOKEN
Keycast     $KC_URL  $KEYCAST_ADMIN_EMAIL / KEYCAST_ADMIN_PASSWORD; admin UCAN KEYCAST_ADMIN_TOKEN (30 days)
Tokens are in infra/secrets/. Restart the onboarding service to pick up new ones:
  docker compose -f infra/docker-compose.yml --profile app up -d
EOF
