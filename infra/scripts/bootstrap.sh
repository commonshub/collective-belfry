#!/usr/bin/env bash
# Bootstraps a running Belfry stack. Idempotent: every step checks before it changes.
#
#  1. waits for Odoo, the signer, the issuer and the verifier
#  2. initialises the Odoo database "odoo" (belfry_pass when the module exists, else the
#     standard modules it depends on), sets the admin password
#  3. creates the Odoo technical user for the onboarding service and an API key for it
#     (odoo shell, because res.users.apikeys.generate needs an existing key), stores the
#     belfry.onboarding_url / belfry.hook_token config parameters
#  4. checks the signer (its API answers with the token, the custody seed's state)
#  5. writes ODOO_API_KEY into infra/secrets/ (scripts/secrets.sh)
# Sign-in, the apps' OAuth clients and the stewards need no setup here: the onboarding service
# serves the sign-in, its clients come from docker-compose.yml (OAUTH_CLIENTS) and the stewards
# are Odoo contacts (python -m belfry_onboarding add-steward).
#
# Usage: infra/scripts/bootstrap.sh   (needs curl, jq, python3, docker compose)
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$INFRA_DIR/.." && pwd)"
ENV_FILE="$INFRA_DIR/.env"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")
ODOO_URL=${ODOO_URL:-http://localhost:8069}
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
log "1/5 waiting for services"
wait_for odoo "$ODOO_URL/web/health"
# Issuer2's own metadata: behind the proxy the credential issuer metadata comes through the
# onboarding service, which starts only after bootstrap.
wait_for issuer "${ISSUER_PUBLIC_URL:-http://localhost:7005}/.well-known/oauth-authorization-server/openid4vci"
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
log "2/5 Odoo database"
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
log "3/5 Odoo onboarding user and API key"
xmlid_res_id() { # module name -> res_id or empty
  odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.model.data search_read \
    "[[[\"module\",\"=\",\"$1\"],[\"name\",\"=\",\"$2\"]]]" '{"fields":["res_id"],"limit":1}' | jq -r '.[0].res_id // empty'
}
# Least privilege for the service: an internal user with the Belfry admin group (belfry.*
# plus read on invoices and payment transactions, see the module's ACL) and contact
# management (member numbers and npub are written on res.partner). Everything
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
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.apps_url\",\"${ONBOARDING_PUBLIC_URL:-http://localhost:8100}/account\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.login_url\",\"${ONBOARDING_PUBLIC_URL:-http://localhost:8100}/login\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"belfry.public_url\",\"${ONBOARDING_PUBLIC_URL:-http://localhost:8100}\"]" >/dev/null
# the organisation that invoices the tickets (onboarding's accounting.py); empty: Belfry's own invoice
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "$(jq -cn --arg v "${ACCOUNTING_ISSUER:-}" '["belfry.invoice_issuer", (if $v == "" then false else $v end)]')" >/dev/null
# pages on these origins may call the pass API from the browser (controllers/api.py); empty: none
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "$(jq -cn --arg v "${API_CORS_ORIGINS:-}" '["belfry.api_cors_origins", (if $v == "" then false else $v end)]')" >/dev/null
# Odoo's own absolute links (invite URLs, payment links in mails) use web.base.url; freeze it so a
# login from another host name does not rewrite it.
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"web.base.url\",\"${ODOO_PUBLIC_URL:-http://localhost:8069}\"]" >/dev/null
odoo_exec "$ADMIN_UID" "$ADMIN_PW" ir.config_parameter set_param "[\"web.base.url.freeze\",\"True\"]" >/dev/null
echo "config parameters belfry.onboarding_url, belfry.hook_token, belfry.apps_url, belfry.login_url, belfry.public_url, belfry.api_cors_origins, belfry.invoice_issuer and web.base.url (${ODOO_PUBLIC_URL:-http://localhost:8069}) set"

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
# /hooks/stripe the disputes. Keys: STRIPE_PUBLISHABLE_KEY in .env, the secret key (or a restricted
# key, rk_…, with the permissions in DEPLOY.md) in
# infra/secrets/stripe_secret_key (scripts/put-secret.sh). Test keys (sk_test_) put the
# provider in test mode, live keys enable it. Both webhooks are created through Stripe's API once
# the public URLs are https: Odoo's (action_stripe_create_webhook) and Belfry's dispute endpoint,
# whose signing secret goes into infra/secrets/stripe_webhook_secret (restart onboarding to read it).
STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY:-$(cat "$INFRA_DIR/secrets/stripe_secret_key" 2>/dev/null || true)}
export STRIPE_SECRET_KEY
if [[ -n "${STRIPE_PUBLISHABLE_KEY:-}" && -n "$STRIPE_SECRET_KEY" ]]; then
  case "$STRIPE_SECRET_KEY" in
    sk_test_*|rk_test_*) STRIPE_STATE=test; [[ "$STRIPE_PUBLISHABLE_KEY" == pk_test_* ]] || die "STRIPE_PUBLISHABLE_KEY is not a test key while the secret key is" ;;
    sk_live_*|rk_live_*) STRIPE_STATE=enabled; [[ "$STRIPE_PUBLISHABLE_KEY" == pk_live_* ]] || die "STRIPE_PUBLISHABLE_KEY is not a live key while the secret key is" ;;
    *) die "infra/secrets/stripe_secret_key is not a Stripe secret or restricted key (sk_test_…, sk_live_…, rk_test_…, rk_live_…)" ;;
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

# ---------- 4. the signer ----------
log "4/5 signer"
# The signer is on an internal network of its own (no published port); its binary checks itself
# inside the container, and the seed's state is the file the container sees.
for i in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T signer belfry-signer healthcheck && break
  [[ $i -eq 60 ]] && die "the signer is not up (docker compose logs signer)"
  sleep 2
done
if [[ -s "$INFRA_DIR/secrets/custody/custody-seed" || -n "${CUSTODY_SEED_DIR:-}" ]]; then
  echo "signer up; custody seed: $("${COMPOSE[@]}" exec -T signer sh -c 'test -s /run/belfry/custody-seed && echo unlocked || echo "LOCKED (new accounts wait: scripts/custody-seed.sh unlock)"' 2>/dev/null || echo unknown)"
fi
[[ -s "$INFRA_DIR/secrets/signer_token" && -s "$INFRA_DIR/secrets/signer_master_key" ]] \
  || die "infra/secrets/signer_token or signer_master_key missing: run scripts/gen-secrets.sh"

# ---------- 6. summary ----------
log "5/5 done"
cat <<EOF
Odoo        $ODOO_URL  admin / ODOO_ADMIN_PASSWORD; RPC user $ODOO_ONBOARDING_LOGIN with ODOO_API_KEY
Sign-in     ${ONBOARDING_PUBLIC_URL:-http://localhost:8100}/login (the onboarding service; stewards: add-steward)
Signer      internal only (services/signer); API token SIGNER_TOKEN
Tokens are in infra/secrets/. Restart the onboarding service to pick up new ones:
  docker compose -f infra/docker-compose.yml --profile app up -d
EOF
