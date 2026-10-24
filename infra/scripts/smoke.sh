#!/usr/bin/env bash
# Smoke test for slice 1 (docs/slice-1-onboarding.md, "Infra"): invite -> pay -> accounts
# created exactly once, replay-safe.
#
# STATUS (2026-10-23): ran green end to end against the full stack (slice 1, relay, Keycast
# login, login setup links, pass certificates: claim, door check, pass sign-in, revocation). The Authentik
# source link (POST /api/v3/sources/user_connections/oauth/) cannot be created over the API on
# authentik 2026.8.5 ("user" is read-only), so the service logs a warning and the participant
# links the pass once from user settings; this script reports the missing link as a note, and
# only fails if a link count changes between replays.
# Assumptions checked against the module (override with the variables below if they change):
#   - the invite form posts fields name, email, company_name, vat, product_id, promo_code
#   - the belfry_pass products carry xmlids belfry_pass.product_day/week/full
#   - a Stripe payment is emulated by a payment.transaction in state done with a
#     provider_reference (what activate_pass requires) plus a manual payment on the invoice
#     (what marks it paid and fires the automation rule). A manual payment alone creates no
#     payment.transaction, so activate_pass would answer not_paid.
#
# Needs: the stack up WITH the app profile (docker compose --profile app up -d), bootstrap done.
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; . "$INFRA_DIR/.env"; set +a
# Public deployment: set the public URLs (ODOO_URL, AK_URL, KC_URL, ONB_URL, BLOSSOM_URL),
# WALLET_MAP="" (the test wallet then uses the public hostnames) and ONB_ADMIN_EXEC=1; MAILPIT_URL
# is optional there (the activation mail to the fake smoke address is refused by a real relay).
ODOO_URL=${ODOO_URL:-http://localhost:8069}
AK_URL=${AK_URL:-http://localhost:9000}
KC_URL=${KC_URL:-http://localhost:3000}
ONB_URL=${ONB_URL:-http://localhost:8100}
WALLET_MAP=${WALLET_MAP-localhost:7005=issuer:7005,localhost:7004=verifier:7004,localhost:8100=onboarding:8100}
# ONB_ADMIN_EXEC=1: the public proxy denies /admin/* and /hooks/odoo/*, so those calls run inside
# the onboarding container (python's urllib; the image has no curl).
ONB_ADMIN_EXEC=${ONB_ADMIN_EXEC:-}
onb_post() { # path -> body (admin and hook endpoints of the onboarding service)
  if [[ -n "$ONB_ADMIN_EXEC" ]]; then
    docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -c '
import sys, urllib.request, urllib.error
req = urllib.request.Request("http://localhost:8100" + sys.argv[1], method="POST", data=(sys.argv[2].encode() if len(sys.argv) > 2 else None), headers={"Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req); print(r.read().decode())
except urllib.error.HTTPError as e:
    print(e.read().decode()); sys.exit(0)' "$@"
  else
    local path=$1; shift
    curl -sS -X POST "$ONB_URL$path" ${1:+-H 'Content-Type: application/json' -d "$1"}
  fi
}
onb_post_code() { # path -> HTTP status
  if [[ -n "$ONB_ADMIN_EXEC" ]]; then
    docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -c '
import sys, urllib.request, urllib.error
try:
    print(urllib.request.urlopen(urllib.request.Request("http://localhost:8100" + sys.argv[1], method="POST")).status)
except urllib.error.HTTPError as e:
    print(e.code)' "$1"
  else
    curl -sS -o /dev/null -w '%{http_code}' -X POST "$ONB_URL$1"
  fi
}
ODOO_DB=odoo
FORM_FIELD_NAME=${FORM_FIELD_NAME:-name}
FORM_FIELD_EMAIL=${FORM_FIELD_EMAIL:-email}
FORM_FIELD_PRODUCT=${FORM_FIELD_PRODUCT:-product_id}
FORM_FIELD_PROMO=${FORM_FIELD_PROMO:-promo_code}
TIER_XMLID=${TIER_XMLID:-product_week}

for v in ODOO_API_KEY AUTHENTIK_TOKEN KEYCAST_ADMIN_TOKEN HOOK_TOKEN; do
  [[ -n "${!v:-}" ]] || { echo "$v is empty in infra/.env; run bootstrap first" >&2; exit 1; }
done

pass=0; fail=0
ok()   { echo "  ok   $*"; (( pass += 1 )) || true; }
bad()  { echo "  FAIL $*" >&2; (( fail += 1 )) || true; }
step() { printf '\n== %s\n' "$*"; }
die()  { echo "abort: $*" >&2; exit 1; }

rpc() {
  curl -fsS "$ODOO_URL/jsonrpc" -H 'Content-Type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"call\",\"id\":1,\"params\":{\"service\":\"$1\",\"method\":\"$2\",\"args\":$3}}"
}
# The test's own Odoo calls (invites, emulated payments, inspection) run as admin; the service
# user is restricted to what the onboarding service needs (bootstrap).
UID_=$(rpc common authenticate "[\"$ODOO_DB\",\"admin\",\"$ODOO_ADMIN_PASSWORD\",{}]" | jq -r '.result')
[[ "$UID_" =~ ^[0-9]+$ ]] || die "cannot authenticate to Odoo as admin with ODOO_ADMIN_PASSWORD"
[[ "$(rpc common authenticate "[\"$ODOO_DB\",\"$ODOO_ONBOARDING_LOGIN\",\"$ODOO_API_KEY\",{}]" | jq -r '.result')" =~ ^[0-9]+$ ]] || die "the service user's ODOO_API_KEY does not authenticate"
ox() { # model method args_json [kwargs_json]
  local out; out=$(rpc object execute_kw "[\"$ODOO_DB\",$UID_,\"$ODOO_ADMIN_PASSWORD\",\"$1\",\"$2\",$3,${4:-{\}}]")
  if [[ $(jq -r 'has("error")' <<<"$out") == true ]]; then jq -r '.error.data.message // .error.message' <<<"$out" >&2; return 1; fi
  jq -c '.result' <<<"$out"
}
xmlid() { ox ir.model.data search_read "[[[\"module\",\"=\",\"$1\"],[\"name\",\"=\",\"$2\"]]]" '{"fields":["res_id"],"limit":1}' | jq -r '.[0].res_id // empty'; }
ak() { curl -sS -H "Authorization: Bearer $AUTHENTIK_TOKEN" "$@"; }

STAMP=$(date +%s)
EMAIL="smoke-$STAMP@belfry.local"
NAME="Smoke Test $STAMP"

step "1. create an invite over JSON-RPC"
INVITE_ID=$(ox belfry.invite create '[{"note":"smoke"}]') || die "belfry.invite create failed (module not installed?)"
INVITE=$(ox belfry.invite read "[[$INVITE_ID]]" '{"fields":["token","url","state","expires_at"]}' | jq -c '.[0]')
TOKEN=$(jq -r '.token' <<<"$INVITE")
[[ -n "$TOKEN" && "$TOKEN" != null ]] && ok "invite $INVITE_ID token=$TOKEN state=$(jq -r .state <<<"$INVITE")" || die "invite has no token"

step "2. open the invite page"
PAGE=$(curl -sS -o /dev/null -w '%{http_code}' "$ODOO_URL/belfry/invite/$TOKEN")
[[ "$PAGE" == 200 ]] && ok "GET /belfry/invite/<token> -> 200" || bad "GET /belfry/invite/<token> -> $PAGE"

step "3. submit the billing form"
TMPL_ID=$(xmlid belfry_pass "$TIER_XMLID"); [[ -n "$TMPL_ID" ]] || die "xmlid belfry_pass.$TIER_XMLID not found"
PRODUCT_ID=$(ox product.product search "[[[\"product_tmpl_id\",\"=\",$TMPL_ID]]]" '{"limit":1}' | jq -r '.[0]')
JAR=$(mktemp)
curl -sS -c "$JAR" -o /dev/null "$ODOO_URL/belfry/invite/$TOKEN"
CSRF=$(curl -sS -b "$JAR" "$ODOO_URL/belfry/invite/$TOKEN" | sed -n 's/.*name="csrf_token"[^>]*value="\([^"]*\)".*/\1/p' | head -n1)
POST_CODE=$(curl -sS -b "$JAR" -o /dev/null -w '%{http_code}' -X POST "$ODOO_URL/belfry/invite/$TOKEN" \
  --data-urlencode "csrf_token=$CSRF" \
  --data-urlencode "$FORM_FIELD_NAME=$NAME" --data-urlencode "$FORM_FIELD_EMAIL=$EMAIL" \
  --data-urlencode "$FORM_FIELD_PRODUCT=$PRODUCT_ID" --data-urlencode "$FORM_FIELD_PROMO=")
rm -f "$JAR"
[[ "$POST_CODE" =~ ^30[1-8]$ ]] && ok "POST -> $POST_CODE (redirect to the payment link)" || bad "POST -> $POST_CODE"

PASS=$(ox belfry.pass search_read "[[[\"invite_id\",\"=\",$INVITE_ID]]]" '{"fields":["id","state","invoice_id","partner_id","sale_order_id"],"limit":1}' | jq -c '.[0] // empty')
[[ -n "$PASS" ]] || die "no belfry.pass linked to invite $INVITE_ID"
PASS_ID=$(jq -r .id <<<"$PASS"); INV_ID=$(jq -r '.invoice_id[0]' <<<"$PASS"); PARTNER_ID=$(jq -r '.partner_id[0]' <<<"$PASS")
[[ "$(jq -r .state <<<"$PASS")" == pending ]] && ok "pass $PASS_ID pending, invoice $INV_ID, partner $PARTNER_ID" || bad "pass state $(jq -r .state <<<"$PASS")"
[[ "$(ox belfry.invite read "[[$INVITE_ID]]" '{"fields":["state"]}' | jq -r '.[0].state')" == used ]] && ok "invite marked used" || bad "invite not marked used"
INV=$(ox account.move read "[[$INV_ID]]" '{"fields":["state","amount_total","currency_id","partner_id"]}' | jq -c '.[0]')
[[ "$(jq -r .state <<<"$INV")" == posted ]] && ok "invoice posted, total $(jq -r .amount_total <<<"$INV")" || bad "invoice state $(jq -r .state <<<"$INV")"

step "4. emulate a Stripe payment: done transaction + manual payment on the invoice"
PROVIDER_ID=$(ox payment.provider search "[[[\"code\",\"=\",\"stripe\"]]]" '{"limit":1,"context":{"active_test":false}}' | jq -r '.[0] // empty')
[[ -n "$PROVIDER_ID" ]] || die "no stripe payment.provider"
PM_ID=$(ox payment.method search '[[["code","=","card"]]]' '{"limit":1,"context":{"active_test":false}}' | jq -r '.[0] // empty')
TX_ID=$(ox payment.transaction create "[{\"provider_id\":$PROVIDER_ID,${PM_ID:+\"payment_method_id\":$PM_ID,}\"reference\":\"SMOKE-$STAMP\",\"amount\":$(jq -r .amount_total <<<"$INV"),\"currency_id\":$(jq -r '.currency_id[0]' <<<"$INV"),\"partner_id\":$(jq -r '.partner_id[0]' <<<"$INV"),\"operation\":\"online_direct\",\"invoice_ids\":[[6,0,[$INV_ID]]],\"provider_reference\":\"pi_smoke_$STAMP\",\"state\":\"done\"}]")
ok "payment.transaction $TX_ID done, provider_reference pi_smoke_$STAMP"
WIZ=$(ox account.payment.register create '[{}]' "{\"context\":{\"active_model\":\"account.move\",\"active_ids\":[$INV_ID]}}")
ox account.payment.register action_create_payments "[[$WIZ]]" >/dev/null
PSTATE=$(ox account.move read "[[$INV_ID]]" '{"fields":["payment_state"]}' | jq -r '.[0].payment_state')
[[ "$PSTATE" == paid || "$PSTATE" == in_payment ]] && ok "invoice payment_state $PSTATE" || bad "invoice payment_state $PSTATE"

step "5. wait for the onboarding webhook to activate the pass"
for i in $(seq 1 30); do
  PASS=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","npub","keycast_pubkey","authentik_user_id","date_start","date_end","stripe_payment_intent"]}' | jq -c '.[0]')
  [[ "$(jq -r .state <<<"$PASS")" == active ]] && break
  sleep 2
done
if [[ "$(jq -r .state <<<"$PASS")" != active ]]; then
  echo "  pass still $(jq -r .state <<<"$PASS") after 60s; calling the webhook directly"
  onb_post "/hooks/odoo/invoice-paid?token=$HOOK_TOKEN" "{\"_model\":\"account.move\",\"_id\":$INV_ID,\"_action\":\"smoke\"}" >/dev/null || true
  sleep 3
  PASS=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","npub","keycast_pubkey","authentik_user_id","date_start","date_end","stripe_payment_intent"]}' | jq -c '.[0]')
fi
[[ "$(jq -r .state <<<"$PASS")" == active ]] && ok "pass active: $PASS" || die "pass not active: $PASS"
NPUB=$(jq -r .npub <<<"$PASS"); KC_PUB=$(jq -r .keycast_pubkey <<<"$PASS"); AK_UID=$(jq -r .authentik_user_id <<<"$PASS")
MEMBER=$(ox res.partner read "[[$PARTNER_ID]]" '{"fields":["belfry_member_number","belfry_npub","belfry_authentik_user_id"]}' | jq -r '.[0].belfry_member_number')
[[ -n "$MEMBER" && "$MEMBER" != null ]] && ok "member number $MEMBER" || bad "no member number on the partner"
USERNAME=$(tr '[:upper:]' '[:lower:]' <<<"$MEMBER")

step "6. check Authentik, source link and Keycast"
AKU=$(ak "$AK_URL/api/v3/core/users/?username=$USERNAME" | jq -c '.results')
[[ "$(jq length <<<"$AKU")" == 1 && "$(jq -r '.[0].pk' <<<"$AKU")" == "$AK_UID" ]] && ok "one Authentik user $USERNAME (pk $AK_UID)" || bad "Authentik users for $USERNAME: $AKU"
AKC=$(ak "$AK_URL/api/v3/sources/user_connections/oauth/?user=$AK_UID&source__slug=pass-bridge" | jq -c '.results')
if [[ "$(jq length <<<"$AKC")" == 1 && "$(jq -r '.[0].identifier' <<<"$AKC")" == "$MEMBER" ]]; then ok "one pass-bridge link, identifier $MEMBER"; else echo "  note source link not pre-created (expected on authentik 2026.8.5; participant links once): $AKC"; fi
LINKS_BEFORE=$(jq length <<<"$AKC")
KCP=$(curl -sS "$KC_URL/.well-known/nostr.json?name=$USERNAME" | jq -r --arg n "$USERNAME" '.names[$n] // empty')
[[ "$KCP" == "$KC_PUB" ]] && ok "Keycast NIP-05 $USERNAME -> $KC_PUB" || bad "Keycast pubkey for $USERNAME: '$KCP' (pass has $KC_PUB)"

step "6b. community relay: whitelist rendered and the kind-0 profile stored"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app)
RELAY_CONF=$("${COMPOSE[@]}" exec -T relay cat /rnostr/config/rnostr.toml 2>/dev/null || true)
WL_LINES=$(grep -c "^pubkey_whitelist = \[.*belfry:whitelist" <<<"$RELAY_CONF" || true)
[[ "$WL_LINES" == 2 ]] && ok "relay config has the two whitelist lines (req and event)" || bad "relay config whitelist lines: $WL_LINES"
[[ "$(grep -c "\"$KC_PUB\"" <<<"$RELAY_CONF")" == 2 ]] && ok "pubkey $KC_PUB whitelisted for req and event" || bad "pubkey not in both whitelists"
# Count instead of grep -q: an early grep exit makes docker logs die of SIGPIPE, and pipefail
# then reports a failure once the log is long.
RELOADS=$("${COMPOSE[@]}" logs --no-log-prefix relay 2>/dev/null | grep -ci "reload config success" || true)
[[ "${RELOADS:-0}" -gt 0 ]] && ok "rnostr reloaded its config ($RELOADS reloads logged)" || bad "no reload in the relay log"
PROFILE=$("${COMPOSE[@]}" exec -T relay rnostr export /rnostr/data/events -f "{\"authors\":[\"$KC_PUB\"],\"kinds\":[0]}" 2>/dev/null | head -n1 || true)
if [[ -n "$PROFILE" ]]; then
  CONTENT=$(jq -r '.content' <<<"$PROFILE")
  [[ "$(jq -r .nip05 <<<"$CONTENT")" == "$USERNAME@"* && "$(jq -r .name <<<"$CONTENT")" == "$NAME" ]] && ok "kind-0 profile on the relay: $CONTENT" || bad "unexpected profile content: $CONTENT"
  [[ "$(jq -r 'keys | join(",")' <<<"$CONTENT")" == "display_name,name,nip05" ]] && ok "profile carries only name, display_name, nip05" || bad "profile has extra fields: $(jq -c keys <<<"$CONTENT")"
else
  bad "no kind-0 event for $KC_PUB on the relay (check the onboarding log for 'not published')"
fi
# Coracle waits for profile, follows and relays after sign-in; NIP-17 senders need the DM relays.
LISTS=$("${COMPOSE[@]}" exec -T relay rnostr export /rnostr/data/events -f "{\"authors\":[\"$KC_PUB\"],\"kinds\":[3,10002,10050]}" 2>/dev/null | jq -sc 'map({kind, tags}) | sort_by(.kind)' || true)
[[ "$(jq -c 'map(.kind)' <<<"${LISTS:-[]}")" == "[3,10002,10050]" ]] && ok "follow, relay and DM relay lists on the relay: $LISTS" || bad "starter lists missing on the relay: ${LISTS:-none}"
SYNC=$("${COMPOSE[@]}" exec -T onboarding python -m belfry_onboarding sync-relay 2>/dev/null || true)
[[ "$(jq -r '.changed' <<<"$SYNC" 2>/dev/null)" == false ]] && ok "sync-relay CLI: whitelist already up to date ($(jq -r .pubkeys <<<"$SYNC") pubkeys)" || bad "sync-relay CLI: $SYNC"

step "6c. sign-in link: mailed by Odoo, signs the browser in through Authentik (no password)"
# Participants never get an Authentik password: activation stores a one-time sign-in link
# (authentik recovery flow belfry-account-setup) on the pass and Odoo mails it.
PASS_LOGIN=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["login_setup_url","login_setup_expires_at","login_mail_sent_at"]}' | jq -c '.[0]')
SETUP_URL=$(jq -r '.login_setup_url // empty' <<<"$PASS_LOGIN")
[[ "$SETUP_URL" == "${AUTHENTIK_PUBLIC_URL:-http://localhost:9000}/if/flow/belfry-account-setup/?flow_token="* ]] \
  && ok "pass carries a sign-in link on the public Authentik URL (valid until $(jq -r .login_setup_expires_at <<<"$PASS_LOGIN"))" \
  || bad "login_setup_url: '$SETUP_URL'"
MAILS=$(ox mail.mail search_read "[[[\"model\",\"=\",\"belfry.pass\"],[\"res_id\",\"=\",$PASS_ID]]]" '{"fields":["recipient_ids","subject","body_html","state"]}')
MAIL_N=$(jq length <<<"$MAILS")
if [[ "$MAIL_N" == 1 ]] && jq -e --arg u "$SETUP_URL" '.[0].body_html | contains($u)' <<<"$MAILS" >/dev/null \
   && jq -e --argjson p "$PARTNER_ID" '.[0].recipient_ids == [$p]' <<<"$MAILS" >/dev/null; then
  ok "one activation mail queued to partner $PARTNER_ID with the link (state $(jq -r '.[0].state' <<<"$MAILS"))"
else
  bad "activation mail: $(jq -c 'map({recipient_ids, subject, state})' <<<"$MAILS")"
fi
[[ -n "$(jq -r '.login_mail_sent_at // empty' <<<"$PASS_LOGIN")" ]] && ok "login_mail_sent_at set" || bad "login_mail_sent_at empty"
# Deliver the queued mail now (the mail queue cron runs every few minutes) and read it back
# from Mailpit, Odoo's outgoing mail server in the local stack.
MAILPIT_URL=${MAILPIT_URL:-http://localhost:8025}
ox mail.mail send "[$(jq -c 'map(.id)' <<<"$MAILS")]" >/dev/null || true
sleep 1
MP=$(curl -sS --max-time 5 "$MAILPIT_URL/api/v1/search?query=to:$EMAIL" 2>/dev/null || echo '{}')
if [[ -z "$(jq -r '.messages_count // empty' <<<"$MP")" ]]; then
  echo "  note Mailpit not reachable at $MAILPIT_URL (a real SMTP relay in production); mail state: $(ox mail.mail read "[$(jq -c 'map(.id)' <<<"$MAILS")]" '{"fields":["state","failure_reason"]}' | jq -c 'map({state, failure_reason})')"
elif [[ "$(jq -r '.messages_count // 0' <<<"$MP")" -ge 1 ]]; then
  MP_ID=$(jq -r '.messages[0].ID' <<<"$MP")
  MP_BODY=$(curl -sS "$MAILPIT_URL/api/v1/message/$MP_ID" | jq -r '.HTML // .Text')
  grep -q "$SETUP_URL" <<<"$MP_BODY" && ok "mail delivered over SMTP to Mailpit with the setup link (from $(curl -sS "$MAILPIT_URL/api/v1/message/$MP_ID" | jq -r '.From.Address'))" || bad "delivered mail lacks the setup link"
else
  bad "no mail for $EMAIL in Mailpit: $(jq -c '{messages_count}' <<<"$MP")"
fi

# Drive the link like the browser: the flow only signs the pending user in (remember-me choice).
FLOW_TOKEN=${SETUP_URL#*flow_token=}
SETUP_EXEC="$AK_URL/api/v3/flows/executor/belfry-account-setup/?query=flow_token%3D$FLOW_TOKEN"
run_setup_flow() { # cookie jar -> prints the final component
  local jar=$1 ch comp body
  ch=$(curl -sS -b "$jar" -c "$jar" "$SETUP_EXEC")
  for _ in 1 2 3; do
    comp=$(jq -r '.component // empty' <<<"$ch")
    case "$comp" in
      ak-stage-user-login) body='{"component":"ak-stage-user-login","remember_me":false}' ;;
      *) break ;;
    esac
    ch=$(curl -sS -b "$jar" -c "$jar" -L -H 'Content-Type: application/json' -H "Referer: $AK_URL/" --data "$body" "$SETUP_EXEC")
  done
  jq -r '.component // "none"' <<<"$ch"
}
AK_SESSION=$(mktemp)   # the participant's browser; 6d continues in it
FINAL=$(run_setup_flow "$AK_SESSION")
# SAVE_AK_JAR=<file>: keep the participant's Authentik session for the browser checks of the
# apps' silent sign-in (chat-login-check.mjs, calendar-check.mjs with COOKIE_JAR).
[[ -n "${SAVE_AK_JAR:-}" ]] && cp "$AK_SESSION" "$SAVE_AK_JAR"
ME=$(curl -sS -b "$AK_SESSION" "$AK_URL/api/v3/core/users/me/" | jq -r '.user.pk // empty')
[[ "$FINAL" == xak-flow-redirect && "$ME" == "$AK_UID" ]] && ok "sign-in link: browser signed in as Authentik user $AK_UID, no password asked" || bad "sign-in flow ended with '$FINAL', session user '$ME'"
AK_PW_LOGIN=$(curl -sS -c /dev/null "$AK_URL/api/v3/flows/executor/belfry-magic-link/" | jq -r '[.password_fields // false] | .[0]')
[[ "$AK_PW_LOGIN" == false ]] && ok "the login page asks for no password (magic link or pass)" || bad "login page offers a password field"
# The link is one-time: a second use must not sign anyone in.
SJAR=$(mktemp)
FINAL=$(run_setup_flow "$SJAR")
ME2=$(curl -sS -b "$SJAR" "$AK_URL/api/v3/core/users/me/" | jq -r '.user.pk // empty')
[[ "$FINAL" != xak-flow-redirect && -z "$ME2" ]] && ok "consumed link is refused ($FINAL, no session)" || bad "consumed link ended with '$FINAL', session user '$ME2'"
rm -f "$SJAR"

step "6d. Keycast sign-in through Authentik (OIDC) in the browser the sign-in link opened"
if OUT=$(PRINT_TOKEN=1 KC_URL="$KC_URL" AK_URL="$AK_URL" EXPECT_EMAIL="$EMAIL" EXPECT_MEMBER="$MEMBER" "$INFRA_DIR/scripts/keycast-oidc-login.sh" "$AK_SESSION" "$KC_PUB" 2>&1); then
  KC_TOKEN=$(sed -n 's/^ACCESS_TOKEN=//p' <<<"$OUT"); KC_BUNKER=$(sed -n 's/^BUNKER_URL=//p' <<<"$OUT"); OUT=$(grep -v '^ACCESS_TOKEN=\|^BUNKER_URL=' <<<"$OUT")
  sed 's/^/  /' <<<"$OUT"; ok "Authentik login opens a Keycast session for $KC_PUB and signs"
else
  sed 's/^/  /' <<<"$OUT" >&2; bad "Keycast OIDC login"
fi
CB_STATUS=$(curl -sS -o /dev/null -w '%{http_code}' "$KC_URL/api/auth/oidc/callback?code=x&state=y")
[[ "$CB_STATUS" == 400 ]] && ok "callback without the flow cookie -> 400" || bad "callback without the flow cookie -> $CB_STATUS"

step "6l. silent sign-in for Belfry's apps: prompt=none through Keycast and Authentik, no consent for trusted apps"
# Keycast patch 0008; the apps (chat, calendar) redirect once per tab with prompt=none and show
# "Sign in with Open Source Village" on login_required. Same browser as 6c/6d (AK_SESSION).
if OUT=$(KC_URL="$KC_URL" AK_URL="$AK_URL" REDIRECT_URI="${CHAT_PUBLIC_URL:-http://localhost:3300}/oauth/callback" "$INFRA_DIR/scripts/silent-signin-check.sh" "$AK_SESSION" "$KC_PUB" 2>&1); then
  sed 's/^/  /' <<<"$OUT"; ok "silent sign-in and the sign-in button work for the chat app's client"
else
  sed 's/^/  /' <<<"$OUT" >&2; bad "silent sign-in"
fi

step "6o. village wallet: signs in silently, derives its seed and nutzap key from the participant's key"
if OUT=$(WALLET_ONLY=1 CLIENT_ID=belfry-wallet KC_URL="$KC_URL" AK_URL="$AK_URL" REDIRECT_URI="${WALLET_PUBLIC_URL:-http://localhost:3400}/oauth/callback" "$INFRA_DIR/scripts/silent-signin-check.sh" "$AK_SESSION" "$KC_PUB" 2>&1); then
  sed 's/^/  /' <<<"$OUT"; ok "the wallet's keys derive from the participant's Nostr key"
else
  sed 's/^/  /' <<<"$OUT" >&2; bad "wallet sign-in and derive_secret"
fi
WCODE=$(curl -sS -o /dev/null -w '%{http_code}' "${WALLET_PUBLIC_URL:-http://localhost:3400}/")
[[ "$WCODE" == 200 ]] && ok "wallet app served (${WALLET_PUBLIC_URL:-http://localhost:3400})" || bad "wallet app -> $WCODE"

step "6h. magic link: the login page mails a one-time link that signs the participant in"
MAGIC_EXEC="$AK_URL/api/v3/flows/executor/belfry-magic-link/"
magic_request() { # cookie jar, e-mail -> component after the identification stage
  curl -sS -b "$1" -c "$1" "$MAGIC_EXEC" >/dev/null
  curl -sS -b "$1" -c "$1" -L -H 'Content-Type: application/json' -H "Referer: $AK_URL/" \
    --data "$(jq -nc --arg e "$2" '{component:"ak-stage-identification",uid_field:$e}')" "$MAGIC_EXEC" | jq -c '{component, response_errors}'
}
magic_open() { # cookie jar, flow token -> final component (authentik asks to confirm before it uses the token)
  local q ch comp body
  q="query=$(jq -rn --arg v "flow_token=$2" '$v|@uri')"
  ch=$(curl -sS -b "$1" -c "$1" "$MAGIC_EXEC?$q")
  for _ in 1 2 3; do
    comp=$(jq -r '.component // empty' <<<"$ch")
    case "$comp" in
      ak-stage-consent) body=$(jq -c '{component:"ak-stage-consent", token}' <<<"$ch") ;;
      ak-stage-user-login) body='{"component":"ak-stage-user-login","remember_me":false}' ;;
      *) break ;;
    esac
    ch=$(curl -sS -b "$1" -c "$1" -L -H 'Content-Type: application/json' -H "Referer: $AK_URL/" --data "$body" "$MAGIC_EXEC?$q")
  done
  jq -r '.component // "none"' <<<"$ch"
}
MJAR=$(mktemp)
UNKNOWN=$(magic_request "$MJAR" "nobody-$STAMP@example.org")
[[ "$(jq -r '.component' <<<"$UNKNOWN")" == ak-stage-identification && "$UNKNOWN" == *"Failed to authenticate"* ]] \
  && ok "unknown address refused, no mail sent" || bad "unknown address: $UNKNOWN"
if [[ -n "$(curl -sS --max-time 5 "$MAILPIT_URL/api/v1/messages?limit=1" 2>/dev/null | jq -r '.total // empty' 2>/dev/null)" ]]; then
  rm -f "$MJAR"; MJAR=$(mktemp)
  REQ=$(magic_request "$MJAR" "$EMAIL")
  [[ "$(jq -r .component <<<"$REQ")" == ak-stage-email ]] && ok "link requested for $EMAIL (e-mail stage)" || bad "magic-link request: $REQ"
  ML=""
  for _ in 1 2 3 4 5 6; do
    sleep 1
    MP=$(curl -sS "$MAILPIT_URL/api/v1/search?query=$(jq -rn --arg v "to:$EMAIL subject:\"sign-in link\"" '$v|@uri')")
    [[ "$(jq -r '.messages_count // 0' <<<"$MP")" -ge 1 ]] || continue
    # The text part has CRLF line ends: strip them before taking the token.
    ML=$(curl -sS "$MAILPIT_URL/api/v1/message/$(jq -r '.messages[0].ID' <<<"$MP")" | jq -r '.Text' | tr -d '\r' | grep -o 'flow_token=[A-Za-z0-9]*' | head -n1 | cut -d= -f2)
    [[ -n "$ML" ]] && break
  done
  if [[ -n "$ML" ]]; then
    ok "magic-link mail delivered (subject 'Your Open Source Village sign-in link')"
    FINAL=$(magic_open "$MJAR" "$ML")
    ME=$(curl -sS -b "$MJAR" "$AK_URL/api/v3/core/users/me/" | jq -r '.user.pk // empty')
    [[ "$FINAL" == xak-flow-redirect && "$ME" == "$AK_UID" ]] && ok "magic link signed the browser in as Authentik user $AK_UID" || bad "magic link ended with '$FINAL', session user '$ME'"
    RJAR=$(mktemp); FINAL=$(magic_open "$RJAR" "$ML")
    ME=$(curl -sS -b "$RJAR" "$AK_URL/api/v3/core/users/me/" | jq -r '.user.pk // empty')
    [[ -z "$ME" ]] && ok "the magic link works once ($FINAL on reuse)" || bad "reused magic link signed in user $ME"
    rm -f "$RJAR"
  else
    bad "no magic-link mail for $EMAIL in Mailpit"
  fi
else
  echo "  note Mailpit not reachable at $MAILPIT_URL: the mail round trip is checked by hand on a public deployment"
fi
# Mail flooding: per account, at most five links in 30 minutes (deny stage before the mail stage).
SENT=0; DENIED=""
for _ in 1 2 3 4 5 6 7; do
  LJAR=$(mktemp); REQ=$(magic_request "$LJAR" "$EMAIL"); rm -f "$LJAR"
  case "$(jq -r .component <<<"$REQ")" in
    ak-stage-email) SENT=$((SENT + 1)) ;;
    ak-stage-access-denied) DENIED=$REQ; break ;;
    *) DENIED="unexpected: $REQ"; break ;;
  esac
done
[[ "$DENIED" == *"Too many sign-in links"* || "$(jq -r .component <<<"$DENIED" 2>/dev/null)" == ak-stage-access-denied ]] \
  && ok "sign-in links are limited per account: denied after $SENT more (at most five in 30 minutes)" || bad "magic-link limit: $SENT sent, then '$DENIED'"
step "6m. logout from an app ends the Keycast and the Authentik session (no silent sign-in after it)"
# In 6h's magic-link browser: the logout ends that Authentik session, and AK_SESSION is still
# needed (8b).
if [[ -n "${MJAR:-}" && -s "$MJAR" ]] && [[ -n "$(curl -sS -b "$MJAR" "$AK_URL/api/v3/core/users/me/" | jq -r '.user.pk // empty' 2>/dev/null)" ]]; then
  if OUT=$(LOGOUT_ONLY=1 LOGOUT_JAR="$MJAR" KC_URL="$KC_URL" AK_URL="$AK_URL" REDIRECT_URI="${CHAT_PUBLIC_URL:-http://localhost:3300}/oauth/callback" "$INFRA_DIR/scripts/silent-signin-check.sh" "$AK_SESSION" "$KC_PUB" 2>&1); then
    sed 's/^/  /' <<<"$OUT"; ok "logout from an app signs the participant out everywhere"
  else
    sed 's/^/  /' <<<"$OUT" >&2; bad "logout"
  fi
else
  echo "    (skipped: no magic-link session from 6h)"
fi
rm -f "$MJAR"

step "6f. Blossom: uploads need a signed BUD-11 event from a key with an active pass"
BLOSSOM_URL=${BLOSSOM_URL:-http://localhost:3100}
blossom_auth() { # verb [sha256] -> Authorization header value, signed through Keycast with the participant's key
  # BUD-02: an upload/delete token names the blob it is for in an "x" tag.
  local ev; ev=$(jq -nc --arg pk "$KC_PUB" --argjson t "$(date +%s)" --arg v "$1" --arg x "${2:-}" --arg s "${BLOSSOM_PUBLIC_DOMAIN:-localhost:3100}" \
    '{pubkey:$pk, created_at:$t, kind:24242, tags:([["t",$v],["expiration",(($t+300)|tostring)],["server",$s]] + (if $x != "" then [["x",$x]] else [] end)), content:("Authorize "+$v)}')
  curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' \
    -d "$(jq -nc --argjson e "$ev" '{method:"sign_event", params:[$e]}')" | jq -r '.result | @base64'
}
if [[ -n "${KC_TOKEN:-}" ]]; then
  BLOB=$(mktemp); printf 'belfry smoke %s' "$STAMP" > "$BLOB"; SHA=$(sha256sum "$BLOB" | cut -c1-64)
  BAUTH=$(blossom_auth upload "$SHA")
  [[ -n "$BAUTH" && "$BAUTH" != bnVsbA== ]] && ok "kind-24242 auth event signed through Keycast (policy allows it)" || bad "could not sign a kind-24242 event through Keycast: $BAUTH"
  NOAUTH=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BLOSSOM_URL/upload" -H 'Content-Type: text/plain' --data-binary "@$BLOB")
  [[ "$NOAUTH" == 401 ]] && ok "upload without authorization -> 401" || bad "upload without authorization -> $NOAUTH"
  UP=$(curl -sS -X PUT "$BLOSSOM_URL/upload" -H "Authorization: Nostr $BAUTH" -H 'Content-Type: text/plain' --data-binary "@$BLOB")
  [[ "$(jq -r '.sha256 // empty' <<<"$UP")" == "$SHA" ]] && ok "upload accepted: $(jq -r .url <<<"$UP")" || bad "upload: $UP"
  [[ "$(curl -sS "$BLOSSOM_URL/$SHA")" == "belfry smoke $STAMP" ]] && ok "blob readable without auth (content-addressed)" || bad "blob not readable"
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BLOSSOM_URL/mirror" -H "Authorization: Nostr $BAUTH" -H 'Content-Type: application/json' -d '{"url":"http://onboarding:8100/health"}')" == 404 ]] && ok "mirror endpoint blocked" || bad "mirror reachable"
  DEL=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$BLOSSOM_URL/$SHA" -H "Authorization: Nostr $BAUTH")
  [[ "$DEL" == 403 ]] && ok "delete with an upload token -> 403 (verb check)" || bad "delete with upload token -> $DEL"
else
  bad "no Keycast access token from step 6d"
fi

step "6g. policy kinds: calendar, photos, proposals and NIP-17 chat sign; others are refused"
if [[ -n "${KC_TOKEN:-}" ]]; then
  kc_sign() { # kind [tags json] -> "ok" or the error
    local ev; ev=$(jq -nc --arg pk "$KC_PUB" --argjson t "$(date +%s)" --argjson k "$1" --argjson tags "${2:-[]}" '{pubkey:$pk, created_at:$t, kind:$k, tags:$tags, content:"smoke"}')
    local r; r=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$ev" '{method:"sign_event", params:[$e]}')")
    [[ -n "$(jq -r '.result.sig // empty' <<<"$r")" ]] && echo ok || jq -r '.error // .message // .' <<<"$r" | head -c 120
  }
  for spec in "31923:calendar event (NIP-52)" "31925:calendar RSVP" "20:picture (NIP-68)" "1068:poll (NIP-88)" "1018:poll response" "1111:comment (NIP-22)" "13:chat seal (NIP-17)" "10050:DM relay list" "30402:marketplace offer (NIP-99)" "30078:directory consent (NIP-78)"; do
    k=${spec%%:*}; name=${spec#*:}; res=$(kc_sign "$k")
    [[ "$res" == ok ]] && ok "kind $k ($name) signed" || bad "kind $k ($name): $res"
  done
  res=$(kc_sign 9735); [[ "$res" != ok ]] && ok "kind 9735 (zap receipt, not allowed) refused: $res" || bad "kind 9735 was signed although it is not in the policy"
  ENC=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --arg pk "$KC_PUB" '{method:"nip44_encrypt", params:[$pk, "hello village"]}')")
  CT=$(jq -r '.result // empty' <<<"$ENC")
  [[ -n "$CT" ]] && ok "nip44_encrypt through the signer works (NIP-17 chat)" || bad "nip44_encrypt: $ENC"
  DEC=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --arg pk "$KC_PUB" --arg ct "$CT" '{method:"nip44_decrypt", params:[$pk, $ct]}')")
  [[ "$(jq -r '.result // empty' <<<"$DEC")" == "hello village" ]] && ok "nip44_decrypt round trip" || bad "nip44_decrypt: $DEC"
else
  bad "no Keycast access token from step 6d"
fi

step "6i. NIP-46: the bunker URL names the public relay and a remote-signing client connects through it"
# The relay inside bunker:// URLs and NIP-05 discovery is BUNKER_PUBLIC_RELAYS (Keycast patch 0007);
# the signer keeps the Compose-internal one. The client runs inside the onboarding container, so
# BUNKER_MAP rewrites the public host (empty on a public deployment, like WALLET_MAP).
BUNKER_MAP=${BUNKER_MAP-localhost:3080=keycast-relay:8080}
PUBLIC_RELAY=${BUNKER_PUBLIC_RELAYS:-ws://localhost:3080}
DISC_RELAY=$(curl -sS "$KC_URL/.well-known/nostr.json" | jq -r '.nip46.relay // empty')
[[ "$DISC_RELAY" == "$PUBLIC_RELAY" ]] && ok "NIP-05 discovery advertises the public relay $DISC_RELAY" || bad "NIP-05 discovery relay is '$DISC_RELAY', expected $PUBLIC_RELAY"
if [[ -n "${KC_BUNKER:-}" ]]; then
  BUNKER_RELAY=$(python3 -c 'import sys, urllib.parse as u; print((u.parse_qs(u.urlsplit(sys.argv[1]).query).get("relay") or [""])[0])' "$KC_BUNKER")
  [[ "$BUNKER_RELAY" == "$PUBLIC_RELAY" ]] && ok "the OAuth bunker URL names the public relay $BUNKER_RELAY" || bad "bunker URL relay is '$BUNKER_RELAY', expected $PUBLIC_RELAY"
  NIP46=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -m belfry_onboarding.nip46check "$KC_BUNKER" --expect-pubkey "$KC_PUB" --refused-kind 9735)
  [[ -n "$BUNKER_MAP" ]] && NIP46+=(--map "$BUNKER_MAP")
  if OUT=$("${NIP46[@]}" 2>&1); then sed 's/^/  /' <<<"$OUT"; ok "NIP-46 client: connect, get_public_key, sign_event through the bunker relay"
  else sed 's/^/  /' <<<"$OUT" >&2; bad "NIP-46 client through the bunker relay"; fi
else
  bad "no bunker URL from step 6d"
fi

step "6j. chat app (Coracle): served, built against the community relay and the bunker relay"
CHAT_URL=${CHAT_URL:-${CHAT_PUBLIC_URL:-http://localhost:3300}}
CHAT_HTML=$(curl -sS -o - -w '\n%{http_code}' "$CHAT_URL/")
[[ "${CHAT_HTML##*$'\n'}" == 200 && "$CHAT_HTML" == *"Open Source Village"* ]] && ok "chat app answers at $CHAT_URL with the Open Source Village title" || bad "chat app at $CHAT_URL: HTTP ${CHAT_HTML##*$'\n'}"
CHAT_JS=$(grep -oE 'src="[^"]*/assets/index[^"]*\.js"' <<<"$CHAT_HTML" | head -n1 | sed 's/src="//;s/"$//')
if [[ -n "$CHAT_JS" ]]; then
  BUNDLE=$(curl -sS "$CHAT_URL$CHAT_JS" | tr -d '\n')
  for want in "${COMMUNITY_RELAY_URL:-ws://localhost:3090}" "${BUNKER_PUBLIC_RELAYS:-ws://localhost:3080}"; do
    [[ "$BUNDLE" == *"$want"* ]] && ok "chat bundle carries $want" || bad "chat bundle lacks $want"
  done
  [[ "$BUNDLE" != *"relay.damus.io"* ]] && ok "no upstream default relays in the chat bundle" || bad "the chat bundle still names relay.damus.io"
else
  bad "no index bundle in the chat page"
fi
[[ -n "${SAVE_BUNKER_URL:-}" && -n "${KC_BUNKER:-}" ]] && printf '%s\n' "$KC_BUNKER" > "$SAVE_BUNKER_URL"
echo "  note the browser login is a separate check: infra/scripts/chat-login-check.mjs (needs Chromium)"

step "6k. calendar app (Calendar by Form*): served, built against the community relay and the bunker relay"
CAL_URL=${CALENDAR_URL:-${CALENDAR_PUBLIC_URL:-http://localhost:3200}}
CAL_HTML=$(curl -sS -o - -w '\n%{http_code}' "$CAL_URL/")
[[ "${CAL_HTML##*$'\n'}" == 200 && "$CAL_HTML" == *"<title>Open Source Village calendar</title>"* ]] && ok "calendar answers at $CAL_URL with the Open Source Village title" || bad "calendar at $CAL_URL: HTTP ${CAL_HTML##*$'\n'}"
CAL_JS=$(grep -oE 'src="/assets/index[^"]*\.js"' <<<"$CAL_HTML" | head -n1 | sed 's/src="//;s/"$//')
if [[ -n "$CAL_JS" ]]; then
  CAL_BUNDLE=$(curl -sS "$CAL_URL$CAL_JS" | tr -d '\n')
  for want in "${COMMUNITY_RELAY_URL:-ws://localhost:3090}" "${BUNKER_PUBLIC_RELAYS:-ws://localhost:3080}"; do
    [[ "$CAL_BUNDLE" == *"$want"* ]] && ok "calendar bundle carries $want" || bad "calendar bundle lacks $want"
  done
  [[ "$CAL_BUNDLE" != *"relay.formstr.app"* && "$CAL_BUNDLE" != *"api.formstr.app"* ]] && ok "no Formstr relays or mail API in the calendar bundle" || bad "the calendar bundle still names Formstr's relays or API"
else
  bad "no index bundle in the calendar page"
fi
echo "  note the browser check (bunker sign-in, calendar and event read back) is infra/scripts/calendar-check.mjs"

step "6n. polls app (services/vote): served with its artifacts; creating needs an active pass, votes need a valid proof"
VOTE_URL=${VOTE_URL:-${VOTE_PUBLIC_URL:-http://localhost:3500}}
VOTE_HTML=$(curl -sS -o - -w '\n%{http_code}' "$VOTE_URL/")
[[ "${VOTE_HTML##*$'\n'}" == 200 && "$VOTE_HTML" == *"<title>Open Source Village polls</title>"* ]] && ok "polls app answers at $VOTE_URL" || bad "polls app at $VOTE_URL: HTTP ${VOTE_HTML##*$'\n'}"
[[ "$(curl -sS "$VOTE_URL/api/health" | jq -r .ok 2>/dev/null)" == true ]] && ok "vote API healthy" || bad "vote API health"
ZKEY=$(curl -sS -o /dev/null -w '%{http_code} %{size_download}' "$VOTE_URL/artifacts/semaphore-10.zkey")
[[ "$ZKEY" == "200 2422238" ]] && ok "proving artifacts served by the app itself ($ZKEY)" || bad "artifacts: $ZKEY"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data '{}' "$VOTE_URL/api/polls")" == 401 ]] && ok "opening a poll without NIP-98 auth: 401" || bad "unauthenticated poll creation not refused"
# a well-formed NIP-98 token of a key without a pass (made inside the container, which knows PUBLIC_URL)
NOPASS=$("${COMPOSE[@]}" exec -T vote node --input-type=module -e '
import { finalizeEvent, generateSecretKey } from "nostr-tools/pure"
import { createHash } from "node:crypto"
const body = JSON.stringify({ title: "x", options: ["a", "b"], closes_at: Math.floor(Date.now() / 1000) + 3600 })
const ev = finalizeEvent({ kind: 27235, created_at: Math.floor(Date.now() / 1000), content: "", tags: [["u", process.env.PUBLIC_URL.replace(/\/$/, "") + "/api/polls"], ["method", "POST"], ["payload", createHash("sha256").update(body).digest("hex")]] }, generateSecretKey())
const r = await fetch("http://127.0.0.1:8080/api/polls", { method: "POST", body, headers: { "Content-Type": "application/json", Authorization: "Nostr " + Buffer.from(JSON.stringify(ev)).toString("base64") } })
console.log(r.status)' 2>/dev/null || true)
[[ "$NOPASS" == 403 ]] && ok "a signed request from a key without an active pass: 403" || bad "key without a pass: ${NOPASS:-no answer}"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data '{"proof":{}}' "$VOTE_URL/api/polls/nope/votes")" == 404 ]] && ok "a vote for an unknown poll: 404" || bad "vote for an unknown poll not refused"
echo "  note the browser check (silent sign-in, set up voting, open a poll, vote with a real proof) is infra/scripts/vote-check.mjs"

step "6p. directory and marketplace (services/village): served; only active passes learn who the participants are"
PEOPLE_URL=${PEOPLE_URL:-${PEOPLE_PUBLIC_URL:-http://localhost:3600}}
MARKET_URL=${MARKET_URL:-${MARKET_PUBLIC_URL:-http://localhost:3700}}
for app in "$PEOPLE_URL|Open Source Village directory" "$MARKET_URL|Open Source Village marketplace"; do
  u=${app%%|*}; t=${app#*|}; H=$(curl -sS -o - -w '\n%{http_code}' "$u/")
  [[ "${H##*$'\n'}" == 200 && "$H" == *"<title>$t</title>"* ]] && ok "$t answers at $u" || bad "$t at $u: HTTP ${H##*$'\n'}"
done
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$PEOPLE_URL/api/participants")" == 401 ]] && ok "participants list without NIP-98 auth: 401" || bad "participants list not refused without auth"
NOPASS=$("${COMPOSE[@]}" exec -T village node --input-type=module -e '
import { finalizeEvent, generateSecretKey } from "nostr-tools/pure"
const ev = finalizeEvent({ kind: 27235, created_at: Math.floor(Date.now() / 1000), content: "", tags: [["u", process.env.PEOPLE_PUBLIC_URL.replace(/\/$/, "") + "/api/participants"], ["method", "GET"]] }, generateSecretKey())
const r = await fetch("http://127.0.0.1:8080/api/participants", { headers: { Authorization: "Nostr " + Buffer.from(JSON.stringify(ev)).toString("base64") } })
console.log(r.status)' 2>/dev/null || true)
[[ "$NOPASS" == 403 ]] && ok "participants list for a key without an active pass: 403" || bad "key without a pass: ${NOPASS:-no answer}"
if [[ -n "${KC_TOKEN:-}" ]]; then
  # the participant's own NIP-98 token, signed by Keycast like the app does
  EV=$(jq -nc --arg pk "$KC_PUB" --argjson t "$(date +%s)" --arg u "${PEOPLE_URL%/}/api/participants" '{pubkey:$pk, created_at:$t, kind:27235, tags:[["u",$u],["method","GET"]], content:""}')
  SIGNED=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')" | jq -c '.result')
  LIST=$(curl -sS -H "Authorization: Nostr $(printf '%s' "$SIGNED" | base64 -w0)" "$PEOPLE_URL/api/participants")
  jq -e --arg pk "$KC_PUB" '.pubkeys | index($pk) != null' <<<"$LIST" >/dev/null && ok "the participant gets the list ($(jq '.pubkeys | length' <<<"$LIST") active keys)" || bad "participants list for the participant: $(head -c 200 <<<"$LIST")"
fi
echo "  note the browser check (directory opt-in, an offer with a photo, paying it through the wallet) is infra/scripts/village-check.mjs"

step "6q. photos (services/village): served on its own host; NIP-98 bound to it; pictures signed through the policy"
PHOTOS_URL=${PHOTOS_URL:-${PHOTOS_PUBLIC_URL:-http://localhost:3800}}
H=$(curl -sS -o - -w '\n%{http_code}' "$PHOTOS_URL/")
[[ "${H##*$'\n'}" == 200 && "$H" == *"<title>Open Source Village photos</title>"* ]] && ok "Open Source Village photos answers at $PHOTOS_URL" || bad "photos at $PHOTOS_URL: HTTP ${H##*$'\n'}"
JS=$(grep -o '/assets/index-[^"]*\.js' <<<"$H" | head -1)
BUNDLE=$(curl -sS "$PHOTOS_URL$JS")
[[ "$BUNDLE" == *"belfry-photos"* && "$BUNDLE" == *"${BLOSSOM_PUBLIC_URL:-http://localhost:3100}"* ]] && ok "bundle signs in as belfry-photos and uploads to ${BLOSSOM_PUBLIC_URL:-http://localhost:3100}" || bad "photos bundle lacks the client id or the Blossom URL"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$PHOTOS_URL/api/participants")" == 401 ]] && ok "participants list without NIP-98 auth: 401" || bad "participants list not refused without auth"
if [[ -n "${KC_TOKEN:-}" ]]; then
  EV=$(jq -nc --arg pk "$KC_PUB" --argjson t "$(date +%s)" --arg u "${PHOTOS_URL%/}/api/participants" '{pubkey:$pk, created_at:$t, kind:27235, tags:[["u",$u],["method","GET"]], content:""}')
  SIGNED=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')" | jq -c '.result')
  LIST=$(curl -sS -H "Authorization: Nostr $(printf '%s' "$SIGNED" | base64 -w0)" "$PHOTOS_URL/api/participants")
  jq -e --arg pk "$KC_PUB" '.pubkeys | index($pk) != null' <<<"$LIST" >/dev/null && ok "the participant gets the list from the photos host" || bad "participants list from the photos host: $(head -c 200 <<<"$LIST")"
  # a token signed for the directory's URL is refused here (NIP-98 names the host)
  EV=$(jq -nc --arg pk "$KC_PUB" --argjson t "$(date +%s)" --arg u "${PEOPLE_URL%/}/api/participants" '{pubkey:$pk, created_at:$t, kind:27235, tags:[["u",$u],["method","GET"]], content:""}')
  SIGNED=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')" | jq -c '.result')
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Nostr $(printf '%s' "$SIGNED" | base64 -w0)" "$PHOTOS_URL/api/participants")" == 401 ]] && ok "a NIP-98 token for another app's URL: 401" || bad "photos accepted a token for the directory's URL"
  for k in 20 7 1111; do
    EV=$(jq -nc --arg pk "$KC_PUB" --argjson t "$(date +%s)" --argjson k "$k" '{pubkey:$pk, created_at:$t, kind:$k, tags:[], content:"smoke"}')
    R=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $KC_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')")
    jq -e '.result.sig' <<<"$R" >/dev/null && ok "Keycast signs kind $k (picture, like, comment)" || bad "kind $k refused: $(head -c 200 <<<"$R")"
  done
fi
echo "  note the browser check (upload, share, like, comment, tip through the wallet) is infra/scripts/photos-check.mjs"

step "6e. steward asks for a new login link (admin endpoint behind the Odoo button)"
RESEND=$(onb_post "/admin/passes/$PASS_ID/login-link?token=$HOOK_TOKEN")
[[ "$(jq -r .result <<<"$RESEND")" == sent ]] && ok "POST /admin/passes/<id>/login-link -> sent" || bad "login-link endpoint: $RESEND"
SETUP_URL2=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["login_setup_url"]}' | jq -r '.[0].login_setup_url')
[[ -n "$SETUP_URL2" && "$SETUP_URL2" != "$SETUP_URL" ]] && ok "pass carries a new link" || bad "link unchanged: $SETUP_URL2"
MAIL_N2=$(ox mail.mail search_count "[[[\"model\",\"=\",\"belfry.pass\"],[\"res_id\",\"=\",$PASS_ID]]]")
[[ "$MAIL_N2" == 2 ]] && ok "second mail queued" || bad "mails for the pass: $MAIL_N2"
RESEND=$(onb_post_code "/admin/passes/$PASS_ID/login-link?token=wrong")
[[ "$RESEND" == 401 ]] && ok "wrong token -> 401" || bad "wrong token -> $RESEND"
if [[ -n "$ONB_ADMIN_EXEC" ]]; then
  PUB=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$ONB_URL/admin/passes/$PASS_ID/login-link?token=$HOOK_TOKEN")
  [[ "$PUB" == 404 ]] && ok "admin endpoint denied at the public proxy -> 404" || bad "admin endpoint public -> $PUB"
fi

step "8. pass certificate: claim link in the mail, offer minted, SD-JWT VC received by the test wallet"
# The test wallet (python -m belfry_onboarding.testwallet) runs inside the onboarding container
# and rewrites the public hosts of the offer and verifier URLs to the Compose service names.
WALLET=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -m belfry_onboarding.testwallet)
[[ -n "$WALLET_MAP" ]] && WALLET+=(--map "$WALLET_MAP")
CLAIM_TOKEN=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["claim_token","credential_status_index"]}' | jq -r '.[0].claim_token // empty')
[[ -n "$CLAIM_TOKEN" ]] && ok "pass carries a claim token (status list index $(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["credential_status_index"]}' | jq -r '.[0].credential_status_index'))" || die "no claim_token on the pass"
MAIL1=$(ox mail.mail search_read "[[[\"model\",\"=\",\"belfry.pass\"],[\"res_id\",\"=\",$PASS_ID]]]" '{"fields":["body_html"],"order":"id asc","limit":1}' | jq -r '.[0].body_html')
grep -q "/claim/$CLAIM_TOKEN" <<<"$MAIL1" && ok "activation mail links to the claim page" || bad "activation mail has no claim link"
CLAIM_PAGE=$(curl -sS -o /dev/null -w '%{http_code}' "$ONB_URL/claim/$CLAIM_TOKEN")
[[ "$CLAIM_PAGE" == 200 ]] && ok "GET /claim/<token> -> 200 (QR page)" || bad "claim page -> $CLAIM_PAGE"
OFFER=$(curl -sS "$ONB_URL/claim/$CLAIM_TOKEN/offer")
OFFER_URL=$(jq -r '.credential_offer // empty' <<<"$OFFER")
[[ "$OFFER_URL" == openid-credential-offer://* ]] && ok "offer minted: $(jq -c '{tier, date_start, date_end}' <<<"$OFFER")" || die "no offer: $OFFER"
CRED=$("${WALLET[@]}" receive "$OFFER_URL" --out /tmp/smoke-wallet.json 2>&1) || die "wallet receive failed: $CRED"
CRED=$(tail -n1 <<<"$CRED")
[[ "$(jq -r .header.typ <<<"$CRED")" == "dc+sd-jwt" && "$(jq -r '.header.x5c|length' <<<"$CRED")" == 2 ]] && ok "SD-JWT VC received, x5c chain of 2" || bad "credential header: $(jq -c .header <<<"$CRED")"
[[ "$(jq -r .payload.vct <<<"$CRED")" == "${AUTHENTIK_PUBLIC_URL%:9000}:8100/vct/village-pass/v1" || "$(jq -r .payload.vct <<<"$CRED")" == *"/vct/village-pass/v1" ]] && ok "vct $(jq -r .payload.vct <<<"$CRED")" || bad "vct: $(jq -r .payload.vct <<<"$CRED")"
[[ "$(jq -r .payload.pass_tier <<<"$CRED")" == week && "$(jq -r .disclosed.member_number <<<"$CRED")" == "$MEMBER" && "$(jq -r .disclosed.npub <<<"$CRED")" == "$NPUB" ]] && ok "claims: tier week, member $MEMBER, npub (disclosable)" || bad "claims: $(jq -c '{p: .payload.pass_tier, d: .disclosed}' <<<"$CRED")"
[[ "$(jq -r .payload.status.status_list.idx <<<"$CRED")" == "$PASS_ID" ]] && ok "status list index = pass id" || bad "status idx $(jq -c .payload.status <<<"$CRED")"
VCTM=$(curl -sS "$ONB_URL/vct/village-pass/v1" | jq -r .vct); [[ "$VCTM" == "$(jq -r .payload.vct <<<"$CRED")" ]] && ok "type metadata served at the vct URL" || bad "vctm: $VCTM"

step "8c. village tokens: minted at activation (50 per pass day), redeemable once, minting closed"
TOKENS=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["cashu_amount"]}' | jq -r '.[0].cashu_amount // 0')
[[ "$TOKENS" == 350 ]] && ok "week pass: 350 tokens minted at activation" || bad "cashu_amount on the pass: $TOKENS"
grep -q "/tokens/$CLAIM_TOKEN" <<<"$MAIL1" && ok "activation mail links to the tokens page" || bad "activation mail has no tokens link"
TPAGE=$(curl -sS "$ONB_URL/tokens/$CLAIM_TOKEN")
CASHU=$(grep -o '#token=cashuB[A-Za-z0-9_-]*' <<<"$TPAGE" | head -n1 | cut -d= -f2)
[[ -n "$CASHU" ]] && ok "tokens page links the wallet with the token (#token=cashuB…)" || bad "tokens page: $(sed 's/<[^>]*>/ /g' <<<"$TPAGE" | tr -s ' ' | head -c 200)"
# A wallet's redeem (swap) inside the onboarding container, against the mint on the internal network.
MINTPY=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -c)
REDEEM=$("${MINTPY[@]}" 'import os,sys
from belfry_onboarding.clients.mint import MintClient, decode_token
m = MintClient(os.environ["MINT_URL"], os.environ["MINT_PUBLIC_URL"])
t = sys.argv[1]
before = m.token_state(t)
fresh = m.swap_token(t)
print(before, m.token_state(t), sum(p.amount for p in decode_token(fresh)[2]))' "$CASHU" 2>&1 | tail -n1)
[[ "$REDEEM" == "unspent spent 350" ]] && ok "redeemed like a wallet: 350 tokens swapped, the bundle is spent" || bad "redeem: $REDEEM"
grep -q "are in a wallet" <<<"$(curl -sS "$ONB_URL/tokens/$CLAIM_TOKEN")" && ok "tokens page now says they are in a wallet" || bad "tokens page after redeem"
if [[ "${MINT_PUBLIC_URL:-}" == https://* ]]; then
  CODE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$MINT_PUBLIC_URL/v1/mint/quote/belfry" -H 'Content-Type: application/json' -d '{"amount":1,"unit":"osv"}')
  [[ "$CODE" == 403 ]] && ok "public mint: minting closed (403)" || bad "public mint quote -> $CODE"
fi

step "8b. sign-in offer (authorization code + issuer_state, as DIDroom uses it): Issuer2 login at Authentik"
SOFFER=$(curl -sS "$ONB_URL/claim/$CLAIM_TOKEN/offer?signin=1" | jq -r '.credential_offer // empty')
[[ "$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' "$SOFFER")" == *'"issuer_state"'* ]] \
  && ok "sign-in offer carries issuer_state" || bad "sign-in offer: $SOFFER"
# The wallet pushes the authorization request; a browser (the participant's Authentik session from
# 6c) follows the redirects through Issuer2's login at Authentik back to the wallet's redirect URI.
SIGNIN_STATE=/tmp/smoke-signin.json
AUTHZ_URL=$("${WALLET[@]}" signin-start "$SOFFER" --state "$SIGNIN_STATE" 2>&1) || die "signin-start failed: $AUTHZ_URL"
AUTHZ_URL=$(tail -n1 <<<"$AUTHZ_URL")
ok "PAR accepted, browser sent to $(sed 's/?.*//' <<<"$AUTHZ_URL")"
BROWSER=$(mktemp); cp "$AK_SESSION" "$BROWSER"
URL=$AUTHZ_URL; HOPS=""
for _ in $(seq 12); do
  read -r LAST_STATUS NEXT < <(curl -sS -b "$BROWSER" -c "$BROWSER" -o "$BROWSER.body" -w '%{http_code} %{redirect_url}\n' "$URL")
  [[ -z "$NEXT" ]] && { HOPS+="[$LAST_STATUS: $(head -c 300 "$BROWSER.body")]"; break; }
  HOPS+="$(sed -E 's#^(https?://[^/]+/[^?]*).*#\1#' <<<"$NEXT") "
  URL=$NEXT
  [[ "$URL" == https://wallet.belfry.test/* ]] && break
done
rm -f "$BROWSER" "$BROWSER.body"
[[ "$URL" == https://wallet.belfry.test/callback\?*code=* ]] && ok "Issuer2 login at Authentik -> wallet redirect with a code" \
  || die "sign-in flow ended at '$URL' (hops: $HOPS)"
[[ "$HOPS" == *"/application/o/authorize/"* ]] && ok "the participant passed Authentik (application belfry-issuer)" || bad "no Authentik hop: $HOPS"
SCRED=$("${WALLET[@]}" signin-finish "$URL" --state "$SIGNIN_STATE" --out /tmp/smoke-wallet-signin.json 2>&1) || die "signin-finish failed: $SCRED"
SCRED=$(tail -n1 <<<"$SCRED")
[[ "$(jq -r .payload.pass_tier <<<"$SCRED")" == week && "$(jq -r .disclosed.member_number <<<"$SCRED")" == "$MEMBER" && "$(jq -r .payload.status.status_list.idx <<<"$SCRED")" == "$PASS_ID" ]] \
  && ok "SD-JWT VC through the sign-in offer: tier week, member $MEMBER, status index $PASS_ID" || bad "sign-in credential: $(jq -c '{p: .payload.pass_tier, d: .disclosed, s: .payload.status}' <<<"$SCRED")"
if [[ -z "$WALLET_MAP" ]]; then
  # public deployment: the proxy refuses authorization requests without issuer_state (Issuer2
  # would issue the profile's placeholder data)
  NOSTATE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${ISSUER_PUBLIC_URL%/}/openid4vci/par" \
    -d response_type=code -d client_id=probe -d code_challenge=x -d code_challenge_method=S256)
  [[ "$NOSTATE" == 400 ]] && ok "PAR without issuer_state refused (400)" || bad "PAR without issuer_state -> $NOSTATE"
fi

step "9. door check: tier only, validity and status by policy"
DOOR=$(curl -sS -X POST "$ONB_URL/door/sessions"); DSID=$(jq -r .session_id <<<"$DOOR"); DURL=$(jq -r .authorization_request_url <<<"$DOOR")
[[ -n "$DSID" && "$DURL" == openid4vp://* ]] && ok "door session $DSID" || die "door session: $DOOR"
PRES=$("${WALLET[@]}" present "$DURL" --wallet /tmp/smoke-wallet.json --disclose pass_tier 2>&1 | tail -n1)
[[ "$(jq -r .status <<<"$PRES")" == 200 ]] && ok "wallet presented the pass (disclosed nothing beyond the tier)" || bad "present: $PRES"
for i in 1 2 3 4 5; do RES=$(curl -sS "$ONB_URL/door/sessions/$DSID"); [[ "$(jq -r .status <<<"$RES")" != pending ]] && break; sleep 1; done
[[ "$(jq -r .status <<<"$RES")" == valid && "$(jq -r .tier <<<"$RES")" == week ]] && ok "door: valid pass, tier week, until $(jq -r .valid_until <<<"$RES")" || bad "door result: $RES"
curl -sS -o /dev/null -w '%{http_code}' "$ONB_URL/door" | grep -q 200 && ok "GET /door -> 200 (QR page)" || bad "door page"

step "9b. the pass on the phone without a wallet: /me signs in through Authentik, /check at the door"
MEJAR=$(mktemp); cp "$AK_SESSION" "$MEJAR"
ME_PAGE=$(curl -sS -L -b "$MEJAR" -c "$MEJAR" "$ONB_URL/me")
[[ "$ME_PAGE" == *'id="qr"'* && "$ME_PAGE" == *"Week pass"* ]] && ok "/me: signed in through Authentik without a click, shows the renewing QR" || bad "/me: $(grep -o '<h1>[^<]*</h1>\|class="bad">[^<]*' <<<"$ME_PAGE" | head -2 | tr '\n' ' ')"
[[ "$(curl -sS -b "$MEJAR" "$ONB_URL/me/qr" | jq -r '.svg // empty' | cut -c1-18)" == data:image/svg+xml ]] && ok "/me/qr renews the code" || bad "/me/qr gave no code"
[[ "$(curl -sS -o /dev/null -w '%{redirect_url}' "$ONB_URL/me")" == */me/login ]] && ok "/me without a session goes to the sign-in" || bad "/me without a session not redirected"
qr_token() { docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -c "from belfry_onboarding import mypass; from belfry_onboarding.settings import Settings; from types import SimpleNamespace as N; print(mypass.qr_token(N(settings=Settings()), int('$1'))[0])"; }
QR_TOKEN=$(qr_token "$PASS_ID")
CHECK=$(curl -sS -H 'Accept: application/json' "$ONB_URL/check/$QR_TOKEN")
[[ "$(jq -c 'keys - ["date_end","date_start","reason","scanned_before","tier","tier_name","valid"]' <<<"$CHECK")" == "[]" ]] && ok "/check tells tier and dates only ($(jq -c 'keys' <<<"$CHECK"))" || bad "/check discloses more: $CHECK"
if [[ "$(date -u +%F)" < "$(jq -r .date_start <<<"$CHECK")" ]]; then
  # before the event the door must refuse the pass; valid passes and the second-scan warning are
  # covered by tests/test_mypass.py
  [[ "$(jq -r .valid <<<"$CHECK")" == false && "$(jq -r .reason <<<"$CHECK")" == "The pass starts on"* ]] && ok "/check: not valid yet ($(jq -r .reason <<<"$CHECK"))" || bad "/check before the start: $CHECK"
else
  [[ "$(jq -r .valid <<<"$CHECK")" == true && "$(jq -r .tier <<<"$CHECK")" == week && "$(jq -r .scanned_before <<<"$CHECK")" == null ]] && ok "/check: valid, week, first scan" || bad "/check: $CHECK"
  [[ "$(curl -sS -H 'Accept: application/json' "$ONB_URL/check/$QR_TOKEN" | jq -r '.scanned_before != null')" == true ]] && ok "/check: a second scan of the same code says when it was first scanned" || bad "/check: second scan not noticed"
fi
[[ "$(curl -sS "$ONB_URL/check/${QR_TOKEN%.*}.AAAAAAAAAAAAAAAAAAAAAA")" == *"Not valid"* ]] && ok "/check: a forged code is not valid" || bad "/check accepted a forged code"
rm -f "$MEJAR"

step "10. sign in to Authentik by presenting the pass (OpenID4VP-to-OIDC bridge)"
PJAR=$(mktemp); pj() { curl -sS -b "$PJAR" -c "$PJAR" "$@"; }
pj -o /dev/null "$AK_URL/if/flow/default-authentication-flow/"
LOGIN=$(pj -o /dev/null -w '%{redirect_url}' "$AK_URL/source/oauth/login/pass-bridge/")
[[ "$LOGIN" == "$ONB_URL/bridge/authorize?"* ]] && ok "Authentik sends the browser to the bridge" || die "source login redirected to '$LOGIN'"
BPAGE=$(pj "$LOGIN"); BSID=$(sed -n 's/.*data-sid="\([^"]*\)".*/\1/p' <<<"$BPAGE" | head -n1)
[[ -n "$BSID" ]] && ok "bridge shows the QR page (login $BSID)" || die "no bridge session in the page"
BREQ=$(sed -n 's/.*<a class="btn" href="\([^"]*\)">Open the wallet.*/\1/p' <<<"$BPAGE" | head -n1 | sed 's/&amp;/\&/g')
PRES=$("${WALLET[@]}" present "$BREQ" --wallet /tmp/smoke-wallet.json --disclose member_number,npub,pass_tier 2>&1 | tail -n1)
[[ "$(jq -r .status <<<"$PRES")" == 200 ]] && ok "wallet presented member number, npub and tier" || bad "present: $PRES"
for i in 1 2 3 4 5; do POLL=$(curl -sS "$ONB_URL/bridge/poll/$BSID"); [[ "$(jq -r .status <<<"$POLL")" != pending ]] && break; sleep 1; done
[[ "$(jq -r .status <<<"$POLL")" == done ]] && ok "bridge verified the pass and issued a code" || die "bridge poll: $POLL"
CB=$(jq -r .redirect <<<"$POLL")
read -r CB_CODE CB_NEXT < <(pj -o /dev/null -w '%{http_code} %{redirect_url}\n' "$CB")
[[ "$CB_CODE" == 302 && "$CB_NEXT" == *"/if/flow/default-source-authentication/"* ]] && ok "Authentik accepted the code (token + userinfo through the bridge)" || bad "callback -> $CB_CODE $CB_NEXT"
# The executor answers 302 once the plan is in the session; following it runs the user_write
# and user_login stages and ends in the xak-flow-redirect challenge.
EXEC="$AK_URL/api/v3/flows/executor/default-source-authentication/?query="
CH=$(pj -L "$EXEC")
for _ in 1 2 3; do
  case "$(jq -r '.component // empty' <<<"$CH")" in
    ak-stage-user-login) CH=$(pj -L -H 'Content-Type: application/json' -H "Referer: $AK_URL/" --data '{"component":"ak-stage-user-login","remember_me":false}' "$EXEC") ;;
    *) break ;;
  esac
done
ME=$(pj "$AK_URL/api/v3/core/users/me/" | jq -r '.user.pk // empty')
[[ "$ME" == "$AK_UID" ]] && ok "signed in to Authentik as the participant (user $AK_UID) with the pass, no password" || bad "session user '$ME' after flow $(jq -c '{component, response_errors}' <<<"$CH")"
LINKS=$(ak "$AK_URL/api/v3/sources/user_connections/oauth/?user=$AK_UID&source__slug=pass-bridge" | jq -c '.results | map(.identifier)')
[[ "$LINKS" == "[\"$MEMBER\"]" ]] && ok "pass-bridge link created with identifier $MEMBER" || bad "links: $LINKS"
rm -f "$PJAR"

step "10a. derived keys: the participant's key comes from the custody seed and comes back after loss"
# Keycast patch 0014. Needs the seed unlocked in Keycast (locally secrets/custody, written by
# gen-secrets.sh; on a deployment /run/belfry, scripts/custody-seed.sh unlock).
DC=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app)
DERIVED=""
if "${DC[@]}" exec -T keycast test -s /run/belfry/custody-seed 2>/dev/null; then
  DERIVED=1
  VINE="odoo-partner-$PARTNER_ID"
  # Keycast's database row for the participant is lost (the account, its key, its links) ...
  "${DC[@]}" exec -T postgres psql -U keycast -d keycast -qtAc "DELETE FROM users WHERE vine_id = '$VINE'" >/dev/null
  [[ -z "$(curl -sS "$KC_URL/.well-known/nostr.json?name=$USERNAME" | jq -r --arg n "$USERNAME" '.names[$n] // empty')" ]] \
    && ok "Keycast account of $USERNAME deleted (simulated loss)" || bad "account still resolves after the delete"
  # ... and comes back from the seed and Odoo with the same key.
  REST=$("${DC[@]}" exec -T onboarding python -m belfry_onboarding restore-keys 2>&1 | tail -n1)
  [[ "$(jq -r --arg m "$MEMBER" '.partners[$m] // empty' <<<"$REST")" == ok ]] && ok "restore-keys: $MEMBER ok" || bad "restore-keys: $REST"
  [[ "$(curl -sS "$KC_URL/.well-known/nostr.json?name=$USERNAME" | jq -r --arg n "$USERNAME" '.names[$n] // empty')" == "$KC_PUB" ]] \
    && ok "same npub after recovery (derived from the custody seed)" || bad "npub after recovery differs"
else
  echo "  note the custody seed is locked or not configured in Keycast: derived keys not checked"
fi

step "10b. self-custody: the participant takes their key, then moves to a key of their own"
# Irreversible for the smoke participant, so it runs after everything that signs with the managed
# key. Signing (export, challenge) happens in the onboarding container (onboarding-sign.sh).
# SMOKE_KEEP_PASS skips it: the kept participant is for browser checks with "Sign in with Open
# Source Village", which a self-custodial account no longer has.
if [[ -n "${SMOKE_KEEP_PASS:-}" ]]; then
  echo "  note SMOKE_KEEP_PASS set: self-custody not tested (the participant stays managed for browser checks)"
elif OUT=$(DERIVED="$DERIVED" SIGN="$INFRA_DIR/scripts/onboarding-sign.sh" KC_URL="$KC_URL" AK_URL="$AK_URL" CHAT_CB="${CHAT_PUBLIC_URL:-http://localhost:3300}/oauth/callback" "$INFRA_DIR/scripts/self-custody-check.sh" "$AK_SESSION" "$KC_PUB" "$USERNAME" 2>&1); then
  grep -v '^NEW_PUBKEY=' <<<"$OUT" | sed 's/^/  /'; ok "take over and bring a new key"
  NEW_PUB=$(sed -n 's/^NEW_PUBKEY=//p' <<<"$OUT")
  PASS_KEY=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["keycast_pubkey"]}' | jq -r '.[0].keycast_pubkey')
  [[ "$PASS_KEY" == "$NEW_PUB" ]] && ok "the pass carries the new key (relay whitelist follows)" || bad "pass key after the move: $PASS_KEY"
  KC_PUB=$NEW_PUB
  NPUB=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["npub"]}' | jq -r '.[0].npub')  # step 7's baseline
else
  sed 's/^/  /' <<<"$OUT" >&2; bad "self-custody"
fi

step "11. revoke the pass: status list bit flips, the door refuses the certificate"
if [[ -n "${SMOKE_KEEP_PASS:-}" ]]; then echo "  note SMOKE_KEEP_PASS set: pass $PASS_ID stays active (browser checks of the apps); revocation not tested"; else
# (printed for the log only; python decodes, since base64 -d fails on unpadded base64url)
STATUS_JWT=$(curl -sS "$ONB_URL/status/1"); python3 -c 'import sys,json,base64; t=sys.argv[1].split(".")[1]; p=json.loads(base64.urlsafe_b64decode(t+"="*(-len(t)%4))); print("  "+json.dumps({k: p.get(k) for k in ("iss","sub","ttl")} | {"bits": p["status_list"]["bits"]}))' "$STATUS_JWT" || true
bit() { docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -c "import sys,json,base64; from belfry_onboarding import credentials as c; t=sys.argv[1].split('.')[1]; p=json.loads(base64.urlsafe_b64decode(t+'='*(-len(t)%4))); print(c.status_at(p['status_list']['lst'], int(sys.argv[2])))" "$1" "$2"; }
[[ "$(bit "$STATUS_JWT" "$PASS_ID")" == 0 ]] && ok "status list: pass $PASS_ID valid (bit 0)" || bad "status bit before revoke: $(bit "$STATUS_JWT" "$PASS_ID")"
REV=$(onb_post "/admin/passes/$PASS_ID/revoke?token=$HOOK_TOKEN")
[[ "$(jq -r .result <<<"$REV")" == revoked ]] && ok "steward revoke -> revoked" || bad "revoke: $REV"
STATUS_JWT=$(curl -sS "$ONB_URL/status/1")
[[ "$(bit "$STATUS_JWT" "$PASS_ID")" == 1 ]] && ok "status list: pass $PASS_ID revoked (bit 1)" || bad "status bit after revoke: $(bit "$STATUS_JWT" "$PASS_ID")"
sleep "$(( ${STATUS_LIST_TTL:-5} + 1 ))"   # the verifier may cache the list for STATUS_LIST_TTL seconds
DOOR=$(curl -sS -X POST "$ONB_URL/door/sessions"); DSID=$(jq -r .session_id <<<"$DOOR")
"${WALLET[@]}" present "$(jq -r .authorization_request_url <<<"$DOOR")" --wallet /tmp/smoke-wallet.json --disclose pass_tier >/dev/null 2>&1 || true
for i in 1 2 3 4 5; do RES=$(curl -sS "$ONB_URL/door/sessions/$DSID"); [[ "$(jq -r .status <<<"$RES")" != pending ]] && break; sleep 1; done
[[ "$(jq -r .status <<<"$RES")" == invalid ]] && jq -e '.failed | index("credential-status")' <<<"$RES" >/dev/null && ok "door refuses the revoked certificate (credential-status)" || bad "door after revoke: $RES"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$ONB_URL/claim/$CLAIM_TOKEN/offer")" == 410 ]] && ok "claim page of a revoked pass -> 410" || bad "claim after revoke"
CHECK=$(curl -sS -H 'Accept: application/json' "$ONB_URL/check/$(qr_token "$PASS_ID")")
[[ "$(jq -r .valid <<<"$CHECK")" == false && "$(jq -r .reason <<<"$CHECK")" == *revoked* ]] && ok "/check: the phone pass of a revoked pass is refused at once" || bad "/check after revoke: $CHECK"
[[ "$(onb_post "/admin/passes/$PASS_ID/login-link?token=$HOOK_TOKEN" | jq -r .result)" == not_active ]] && ok "no login link for a revoked pass" || bad "login link after revoke"
if [[ -n "${KC_TOKEN:-}" ]]; then
  # The signer is suspended and the pass is gone: a fresh Blossom token is refused twice over.
  BAUTH2=$(blossom_auth upload "$(printf 'again' | sha256sum | cut -c1-64)")
  if [[ -z "$BAUTH2" || "$BAUTH2" == bnVsbA== ]]; then ok "revoked key can no longer sign a Blossom auth event (Keycast suspended)"; else
    UP2=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BLOSSOM_URL/upload" -H "Authorization: Nostr $BAUTH2" -H 'Content-Type: text/plain' --data-binary 'again')
    [[ "$UP2" == 403 ]] && ok "upload with a revoked pass -> 403 (gate)" || bad "upload after revoke -> $UP2"; fi
fi

fi

step "7. replay the webhook three times and check nothing was duplicated"
# step 10 linked the pass-bridge source, so count the links as they are now
LINKS_BEFORE_REPLAY=$(ak "$AK_URL/api/v3/sources/user_connections/oauth/?user=$AK_UID&source__slug=pass-bridge" | jq '.results | length')
for i in 1 2 3; do
  echo "  replay $i -> $(onb_post "/hooks/odoo/invoice-paid?token=$HOOK_TOKEN" "{\"_model\":\"account.move\",\"_id\":$INV_ID,\"_action\":\"smoke-replay\"}" | jq -r .result)"
done
sleep 2
PASS2=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","npub","keycast_pubkey","authentik_user_id"]}' | jq -c '.[0]')
[[ "$(jq -r .npub <<<"$PASS2")" == "$NPUB" && "$(jq -r .authentik_user_id <<<"$PASS2")" == "$AK_UID" ]] && ok "pass unchanged after replay" || bad "pass changed: $PASS2"
[[ "$(ox belfry.pass search_count "[[[\"invoice_id\",\"=\",$INV_ID]]]")" == 1 ]] && ok "one pass for the invoice" || bad "more than one pass for invoice $INV_ID"
[[ "$(ak "$AK_URL/api/v3/core/users/?username=$USERNAME" | jq '.pagination.count')" == 1 ]] && ok "still one Authentik user" || bad "Authentik user count changed"
[[ "$(ak "$AK_URL/api/v3/sources/user_connections/oauth/?user=$AK_UID&source__slug=pass-bridge" | jq '.results | length')" == "$LINKS_BEFORE_REPLAY" ]] && ok "source link count unchanged ($LINKS_BEFORE_REPLAY)" || bad "source link count changed"
[[ "$(curl -sS "$KC_URL/.well-known/nostr.json?name=$USERNAME" | jq -r --arg n "$USERNAME" '.names[$n]')" == "$KC_PUB" ]] && ok "same Keycast pubkey" || bad "Keycast pubkey changed"

printf '\n%d ok, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
