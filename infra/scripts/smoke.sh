#!/usr/bin/env bash
# Smoke test for slice 1 (docs/slice-1-onboarding.md, "Infra"): invite -> pay -> accounts
# created exactly once, replay-safe.
#
# Since 2026-11-19 the sign-in is Belfry's own (onboarding service: /login, /account,
# /api/oauth/*) and the keys are held by Belfry's signer (services/signer); the browser steps are
# driven with curl and a cookie jar.
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
. "$INFRA_DIR/scripts/secrets.sh"
# Public deployment: set the public URLs (ODOO_URL, ONB_URL, BLOSSOM_URL),
# WALLET_MAP="" (the test wallet then uses the public hostnames) and ONB_ADMIN_EXEC=1; MAILPIT_URL
# is optional there (the activation mail to the fake smoke address is refused by a real relay).
ODOO_URL=${ODOO_URL:-http://localhost:8069}
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

for v in ODOO_API_KEY SIGNER_TOKEN HOOK_TOKEN; do
  [[ -n "${!v:-}" ]] || { echo "$v is empty (infra/secrets/); run bootstrap first" >&2; exit 1; }
done

pass=0; fail=0
ok()   { echo "  ok   $*"; (( pass += 1 )) || true; }
bad()  { echo "  FAIL $*" >&2; (( fail += 1 )) || true; }
step() { printf '\n== %s\n' "$*"; }
die()  { echo "abort: $*" >&2; exit 1; }

# the body (with the admin password) on stdin and the token as a header file: arguments are
# visible to everyone in ps
rpc() {
  curl -fsS "$ODOO_URL/jsonrpc" -H 'Content-Type: application/json' --data-binary @- \
    <<<"{\"jsonrpc\":\"2.0\",\"method\":\"call\",\"id\":1,\"params\":{\"service\":\"$1\",\"method\":\"$2\",\"args\":$3}}"
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
osvc() { # same as ox, as the onboarding service's user (methods for the steward pages)
  local out uid; uid=$(rpc common authenticate "[\"$ODOO_DB\",\"$ODOO_ONBOARDING_LOGIN\",\"$ODOO_API_KEY\",{}]" | jq -r '.result')
  out=$(rpc object execute_kw "[\"$ODOO_DB\",$uid,\"$ODOO_API_KEY\",\"$1\",\"$2\",$3,${4:-{\}}]")
  if [[ $(jq -r 'has("error")' <<<"$out") == true ]]; then jq -r '.error.data.message // .error.message' <<<"$out" >&2; return 1; fi
  jq -c '.result' <<<"$out"
}
xmlid() { ox ir.model.data search_read "[[[\"module\",\"=\",\"$1\"],[\"name\",\"=\",\"$2\"]]]" '{"fields":["res_id"],"limit":1}' | jq -r '.[0].res_id // empty'; }

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
[[ "$(ox belfry.invite read "[[$INVITE_ID]]" '{"fields":["state"]}' | jq -r '.[0].state')" == reserved ]] && ok "invite reserved until the pass is paid" || bad "invite not reserved"
RESUME=$(curl -sS "$ODOO_URL/belfry/invite/$TOKEN")
grep -q 'id="belfry_continue_payment"' <<<"$RESUME" && ok "opening the invite again offers the payment page (refresh, back button)" || bad "reserved invite page: $(head -c 200 <<<"$RESUME")"
INV=$(ox account.move read "[[$INV_ID]]" '{"fields":["state","name","amount_total","currency_id","partner_id"]}' | jq -c '.[0]')
[[ "$(jq -r .state <<<"$INV")" == posted ]] && ok "invoice posted, total $(jq -r .amount_total <<<"$INV")" || bad "invoice state $(jq -r .state <<<"$INV")"
if [[ -n "${ACCOUNTING_ISSUER:-}" ]]; then
  # Belfry's invoice is internal: an order number (ORD/...) on the payment page, not a second invoice number
  INV_NAME=$(jq -r .name <<<"$INV")
  PAY_PAGE=$(curl -sSL "$(ox belfry.invite belfry_payment_link "[[$INVITE_ID]]" | jq -r .)")
  [[ "$INV_NAME" == ORD/* ]] && grep -qF "$INV_NAME" <<<"$PAY_PAGE" && ! grep -q 'INV/' <<<"$PAY_PAGE" \
    && ok "payment page shows the order number $INV_NAME" || bad "payment page reference: $INV_NAME"
fi

step "3b. pass API: the invite as JSON, an order from an agent, released again"
API_INVITE_ID=$(ox belfry.invite create '[{"note":"smoke api"}]')
API_URL="$ODOO_URL/belfry/api/invites/$(ox belfry.invite read "[[$API_INVITE_ID]]" '{"fields":["token"]}' | jq -r '.[0].token')"
OFFER=$(curl -sS "$API_URL")
[[ "$(jq -r '[.state, (.passes | map(.tier) | sort | join(","))] | join(" ")' <<<"$OFFER")" == "open day,full,week" ]] \
  && ok "GET /belfry/api/invites/<token>: open, three passes, week $(jq -r '.passes[] | select(.tier=="week") | .price' <<<"$OFFER") $(jq -r .currency <<<"$OFFER")" \
  || bad "API offer: $(head -c 300 <<<"$OFFER")"
API_BODY=$(mktemp)
API_CODE=$(curl -sS -o "$API_BODY" -w '%{http_code}' -X POST "$API_URL/order" -H 'Content-Type: application/json' \
  --data-binary "$(jq -nc --arg e "smoke-api-$STAMP@belfry.local" '{tier:"week", name:"Smoke Agent", email:$e}')")
[[ "$API_CODE" == 201 && "$(jq -r '.state + " " + .payment' "$API_BODY")" == "reserved unpaid" && "$(jq -r .payment_url "$API_BODY")" == http* ]] \
  && ok "POST order -> 201, invoice $(jq -r .invoice.number "$API_BODY") $(jq -r .invoice.amount_total "$API_BODY"), payment link" \
  || bad "POST order -> $API_CODE $(head -c 300 "$API_BODY")"
# payment_url goes straight to the provider's checkout (Mollie) or falls back to Odoo's payment link
PAY_TO=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' "$(jq -r .payment_url "$API_BODY")")
case "$PAY_TO" in
  "303 https://www.mollie.com/"*) ok "payment_url -> Mollie's checkout (${PAY_TO#303 })" ;;
  "303 $(jq -r .payment_link "$API_BODY")") ok "payment_url -> Odoo's payment link (no provider with its own checkout)" ;;
  *) bad "payment_url -> $PAY_TO" ;;
esac
API_AGAIN=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$API_URL/order" -H 'Content-Type: application/json' \
  --data-binary '{"tier":"day","name":"Twice","email":"twice@belfry.local"}')
[[ "$API_AGAIN" == 409 ]] && ok "a second order on the same invite -> 409" || bad "second order -> $API_AGAIN"
API_CODE=$(curl -sS -o "$API_BODY" -w '%{http_code}' -X POST "$API_URL/start-over")
[[ "$API_CODE" == 200 && "$(jq -r .state "$API_BODY")" == open ]] && ok "start-over -> 200, the invite is open again" || bad "start-over -> $API_CODE $(head -c 200 "$API_BODY")"
rm -f "$API_BODY"
ox belfry.invite action_revoke "[[$API_INVITE_ID]]" >/dev/null
SKILL=$(curl -sS "$ONB_URL/skill.md")
[[ "$SKILL" == "---"$'\n'"name: open-source-village"* && "$(jq -r .skill_url <<<"$OFFER")" == */skill.md ]] \
  && ok "the agent skill at /skill.md, named in the offer" || bad "skill: $(head -c 200 <<<"$SKILL")"

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
  PASS=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","npub","nostr_pubkey","date_start","date_end","stripe_payment_intent"]}' | jq -c '.[0]')
  [[ "$(jq -r .state <<<"$PASS")" == active ]] && break
  sleep 2
done
if [[ "$(jq -r .state <<<"$PASS")" != active ]]; then
  echo "  pass still $(jq -r .state <<<"$PASS") after 60s; calling the webhook directly"
  onb_post "/hooks/odoo/invoice-paid?token=$HOOK_TOKEN" "{\"_model\":\"account.move\",\"_id\":$INV_ID,\"_action\":\"smoke\"}" >/dev/null || true
  sleep 3
  PASS=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","npub","nostr_pubkey","date_start","date_end","stripe_payment_intent"]}' | jq -c '.[0]')
fi
[[ "$(jq -r .state <<<"$PASS")" == active ]] && ok "pass active: $PASS" || die "pass not active: $PASS"
[[ "$(ox belfry.invite read "[[$INVITE_ID]]" '{"fields":["state"]}' | jq -r '.[0].state')" == used ]] && ok "invite used once the pass is active" || bad "invite not marked used"
INV_PDF=$(ox account.move read "[[$INV_ID]]" '{"fields":["invoice_pdf_report_id"]}' | jq -r '.[0].invoice_pdf_report_id[0] // empty')
[[ -n "$INV_PDF" ]] && ok "the paid invoice has its own PDF (no proforma), attachment $INV_PDF" || bad "invoice $INV_ID has no PDF"
if [[ -n "${ACCOUNTING_ODOO_URL:-}" ]]; then
  # The ticket seller's Odoo books the ticket (onboarding's accounting.py), right after activation.
  for i in $(seq 1 15); do
    ACC=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["accounting_state","accounting_ref","accounting_error"]}' | jq -c '.[0]')
    [[ "$(jq -r .accounting_state <<<"$ACC")" != false ]] && break
    sleep 2
  done
  [[ "$(jq -r .accounting_state <<<"$ACC")" == done ]] && ok "booked at ${ACCOUNTING_ISSUER:-the ticket seller} as $(jq -r .accounting_ref <<<"$ACC")" || bad "accounting: $ACC"
  if [[ -n "${ACCOUNTING_ISSUER:-}" ]]; then
    N=$(ox mail.mail search_count "[[[\"model\",\"=\",\"belfry.pass\"],[\"res_id\",\"=\",$PASS_ID],[\"body_html\",\"ilike\",\"invoice comes from\"]]]")
    [[ "$N" -ge 1 ]] && ok "the activation mail leaves the invoice to $ACCOUNTING_ISSUER" || bad "activation mail without the issuer's note ($N)"
  fi
fi
NPUB=$(jq -r .npub <<<"$PASS"); MEMBER_PUB=$(jq -r .nostr_pubkey <<<"$PASS")
MEMBER=$(ox res.partner read "[[$PARTNER_ID]]" '{"fields":["belfry_member_number","belfry_npub"]}' | jq -r '.[0].belfry_member_number')
[[ -n "$MEMBER" && "$MEMBER" != null ]] && ok "member number $MEMBER" || bad "no member number on the partner"
USERNAME=$(tr '[:upper:]' '[:lower:]' <<<"$MEMBER")

COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app)

step "6. check the signer account and the NIP-05 name"
SIGNER_ACC=$("${COMPOSE[@]}" exec -T onboarding python -c '
import json, sys
from belfry_onboarding.app import build_services
from belfry_onboarding.settings import Settings
print(json.dumps(build_services(Settings()).signer.account(sys.argv[1])))' "odoo-partner-$PARTNER_ID" 2>/dev/null | tail -n1)
[[ "$(jq -r .pubkey <<<"$SIGNER_ACC" 2>/dev/null)" == "$MEMBER_PUB" && "$(jq -r .status <<<"$SIGNER_ACC")" == active ]] \
  && ok "one signer account odoo-partner-$PARTNER_ID ($(jq -r .custody <<<"$SIGNER_ACC"), active) with the pass's key" || bad "signer account: $SIGNER_ACC"
NIP05P=$(curl -sS "$ONB_URL/.well-known/nostr.json?name=$USERNAME" | jq -r --arg n "$USERNAME" '.names[$n] // empty')
[[ "$NIP05P" == "$MEMBER_PUB" ]] && ok "NIP-05 $USERNAME -> $MEMBER_PUB (from Odoo)" || bad "NIP-05 pubkey for $USERNAME: '$NIP05P' (pass has $MEMBER_PUB)"
[[ "$(curl -sS "$ONB_URL/.well-known/nostr.json" | jq -c .names)" == "{}" ]] && ok "nostr.json without a name lists nobody" || bad "nostr.json lists names without being asked"

step "6b. community relay: whitelist rendered and the kind-0 profile stored"
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app)
RELAY_CONF=$("${COMPOSE[@]}" exec -T relay cat /rnostr/config/rnostr.toml 2>/dev/null || true)
WL_LINES=$(grep -c "^pubkey_whitelist = \[.*belfry:whitelist" <<<"$RELAY_CONF" || true)
[[ "$WL_LINES" == 2 ]] && ok "relay config has the two whitelist lines (req and event)" || bad "relay config whitelist lines: $WL_LINES"
[[ "$(grep -c "\"$MEMBER_PUB\"" <<<"$RELAY_CONF")" == 2 ]] && ok "pubkey $MEMBER_PUB whitelisted for req and event" || bad "pubkey not in both whitelists"
# Count instead of grep -q: an early grep exit makes docker logs die of SIGPIPE, and pipefail
# then reports a failure once the log is long.
RELOADS=$("${COMPOSE[@]}" logs --no-log-prefix relay 2>/dev/null | grep -ci "reload config success" || true)
[[ "${RELOADS:-0}" -gt 0 ]] && ok "rnostr reloaded its config ($RELOADS reloads logged)" || bad "no reload in the relay log"
PROFILE=$("${COMPOSE[@]}" exec -T relay rnostr export /rnostr/data/events -f "{\"authors\":[\"$MEMBER_PUB\"],\"kinds\":[0]}" 2>/dev/null | head -n1 || true)
if [[ -n "$PROFILE" ]]; then
  CONTENT=$(jq -r '.content' <<<"$PROFILE")
  [[ "$(jq -r .nip05 <<<"$CONTENT")" == "$USERNAME@"* && "$(jq -r .name <<<"$CONTENT")" == "$NAME" ]] && ok "kind-0 profile on the relay: $CONTENT" || bad "unexpected profile content: $CONTENT"
  [[ "$(jq -r 'keys | join(",")' <<<"$CONTENT")" == "display_name,name,nip05,picture" ]] && ok "profile carries only name, display_name, nip05, picture" || bad "profile has extra fields: $(jq -c keys <<<"$CONTENT")"
  PICTURE=$(jq -r .picture <<<"$CONTENT")
  [[ "$PICTURE" == */avatar/$MEMBER_PUB.png ]] && ok "picture is the default avatar: $PICTURE" || bad "picture: $PICTURE"
  AV=$(curl -sS -o /dev/null -w '%{http_code} %{content_type}' "$ONB_URL/avatar/$MEMBER_PUB.png")
  [[ "$AV" == "200 image/png" ]] && ok "default avatar served as PNG" || bad "avatar: $AV"
else
  bad "no kind-0 event for $MEMBER_PUB on the relay (check the onboarding log for 'not published')"
fi
# Coracle waits for profile, follows and relays after sign-in; NIP-17 senders need the DM relays.
LISTS=$("${COMPOSE[@]}" exec -T relay rnostr export /rnostr/data/events -f "{\"authors\":[\"$MEMBER_PUB\"],\"kinds\":[3,10002,10050]}" 2>/dev/null | jq -sc 'map({kind, tags}) | sort_by(.kind)' || true)
[[ "$(jq -c 'map(.kind)' <<<"${LISTS:-[]}")" == "[3,10002,10050]" ]] && ok "follow, relay and DM relay lists on the relay: $LISTS" || bad "starter lists missing on the relay: ${LISTS:-none}"
SYNC=$("${COMPOSE[@]}" exec -T onboarding python -m belfry_onboarding sync-relay 2>/dev/null || true)
[[ "$(jq -r '.changed' <<<"$SYNC" 2>/dev/null)" == false ]] && ok "sync-relay CLI: whitelist already up to date ($(jq -r .pubkeys <<<"$SYNC") pubkeys)" || bad "sync-relay CLI: $SYNC"

step "6c. sign-in link: mailed by Odoo, signs the browser in (no password)"
# Participants never get a password: activation stores a one-time sign-in link on the pass and
# Odoo mails it (<PUBLIC_URL>/signin/<secret>, signin.py); using it sets Belfry's session cookie.
PASS_LOGIN=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["login_setup_url","login_setup_expires_at","login_mail_sent_at"]}' | jq -c '.[0]')
SETUP_URL=$(jq -r '.login_setup_url // empty' <<<"$PASS_LOGIN")
[[ "$SETUP_URL" == "$ONB_URL/signin/"* ]] \
  && ok "pass carries a sign-in link on the public onboarding URL (valid until $(jq -r .login_setup_expires_at <<<"$PASS_LOGIN"))" \
  || bad "login_setup_url: '$SETUP_URL'"
SIGNIN_PAGE=$(curl -sS "$SETUP_URL")
grep -q "action=\"/signin/" <<<"$SIGNIN_PAGE" && ok "opening the link shows a page that signs in by script (mail scanners spend nothing)" || bad "sign-in page: $(head -c 200 <<<"$SIGNIN_PAGE")"
SESSION_JAR=$(mktemp)   # the participant's browser; the steps below continue in it
SIGNED=$(curl -sS -b "$SESSION_JAR" -c "$SESSION_JAR" -o /dev/null -w '%{http_code} %{redirect_url}' -X POST "$SETUP_URL")
[[ "$SIGNED" == "303 $ONB_URL/account" ]] && grep -q belfry_session "$SESSION_JAR" \
  && ok "using it signs the browser in and lands on the account page" || bad "sign-in link: '$SIGNED'"
# SAVE_SESSION_JAR=<file>: keep the participant's session for the browser checks of the apps'
# silent sign-in (chat-login-check.mjs, calendar-check.mjs with COOKIE_JAR).
[[ -n "${SAVE_SESSION_JAR:-}" ]] && cp "$SESSION_JAR" "$SAVE_SESSION_JAR"
AGAIN=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$SETUP_URL")
[[ "$AGAIN" == 410 ]] && ok "the link works once (second use -> 410)" || bad "second use -> $AGAIN"
ACCOUNT=$(curl -sS -b "$SESSION_JAR" "$ONB_URL/account")
[[ "$ACCOUNT" == *"$NPUB"* && "$ACCOUNT" == *"$USERNAME@"* && "$ACCOUNT" == *"Community apps"* ]] \
  && ok "account page: npub, NIP-05 name and the community apps" || bad "account page: $(head -c 300 <<<"$ACCOUNT")"
[[ "$(curl -sS -o /dev/null -w '%{redirect_url}' "$ONB_URL/account")" == *"/login?next=%2Faccount" ]] && ok "the account page without a session goes to the login page" || bad "account page without a session"
# The page the payer lands on after paying (Odoo's payment status page forwards there): its own
# secret, so signing in there leaves the mail's link usable.
WELCOME_TOKEN=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["welcome_token"]}' | jq -r '.[0].welcome_token // empty')
WELCOME=$(curl -sS "$ONB_URL/welcome/$WELCOME_TOKEN")
[[ -n "$WELCOME_TOKEN" ]] && grep -q "Open the apps" <<<"$WELCOME" && ok "welcome page after paying offers \"Open the apps\"" || bad "welcome page: $(head -c 200 <<<"$WELCOME")"
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
  for MP_ID in $(jq -r '.messages[:5][].ID' <<<"$MP"); do
    MP_BODY=$(curl -sS "$MAILPIT_URL/api/v1/message/$MP_ID" | jq -r '.HTML // .Text')
    grep -q "$SETUP_URL" <<<"$MP_BODY" && break
  done
  grep -q "$SETUP_URL" <<<"$MP_BODY" && ok "mail delivered over SMTP to Mailpit with the sign-in link (from $(curl -sS "$MAILPIT_URL/api/v1/message/$MP_ID" | jq -r '.From.Address'))" || bad "delivered mail lacks the sign-in link"
else
  bad "no mail for $EMAIL in Mailpit: $(jq -c '{messages_count}' <<<"$MP")"
fi
# Ordered by an app with a return_url: the welcome page signs in and goes back to the app.
RETURN_URL="http://localhost:8080/?invite=smoke-$STAMP"
ox belfry.pass write "[[$PASS_ID],{\"return_url\":\"$RETURN_URL\"}]" >/dev/null
WELCOME=$(curl -sS "$ONB_URL/welcome/$WELCOME_TOKEN")
RJAR=$(mktemp)
WELCOME_TO=$(curl -sS -b "$RJAR" -c "$RJAR" -o /dev/null -w '%{redirect_url}' -X POST "$ONB_URL/welcome/$WELCOME_TOKEN")
[[ "$WELCOME" == *'action="/welcome/'* && "$WELCOME_TO" == "$RETURN_URL" ]] && grep -q belfry_session "$RJAR" \
  && ok "with the order's return_url the welcome page signs in and returns to the app" \
  || bad "return to the app: page $(head -c 80 <<<"$WELCOME"), redirect '$WELCOME_TO'"
AGAIN_TO=$(curl -sS -o /dev/null -w '%{redirect_url}' "$ONB_URL/welcome/$WELCOME_TOKEN")
[[ "$AGAIN_TO" == "$RETURN_URL" ]] && ok "used once: the welcome page goes back to the app without signing in" || bad "welcome again -> '$AGAIN_TO'"
ox belfry.pass write "[[$PASS_ID],{\"return_url\":false}]" >/dev/null
rm -f "$RJAR"
LOGIN_PAGE=$(curl -sS "$ONB_URL/login")
[[ "$LOGIN_PAGE" == *'action="/login/email"'* && "$LOGIN_PAGE" != *'type="password"'* ]] && ok "the login page asks for an e-mail address, no password" || bad "login page: $(head -c 200 <<<"$LOGIN_PAGE")"

# The apps' sign-in, as an app does it (connect.py): authorize with PKCE in the participant's
# browser, then the token call from the app.
pkce() { PKCE_V=$(openssl rand -hex 32); PKCE_C=$(printf %s "$PKCE_V" | openssl dgst -sha256 -binary | base64 | tr '+/' '-_' | tr -d '='); }
qparam() { python3 -c 'import sys, urllib.parse as u; print((u.parse_qs(u.urlsplit(sys.argv[1]).query).get(sys.argv[2]) or [""])[0])' "$1" "$2"; }
app_connect() { # jar client_id redirect_uri [prompt] -> CONNECT (token response) and CONNECT_LOC (the redirect)
  local q code
  pkce
  q="client_id=$2&redirect_uri=$(jq -rn --arg v "$3" '$v|@uri')&scope=policy%3Abelfry-event&code_challenge=$PKCE_C&code_challenge_method=S256&state=smoke${4:+&prompt=$4}"
  CONNECT_LOC=$(curl -sS -b "$1" -c "$1" -o /dev/null -w '%{redirect_url}' "$ONB_URL/api/oauth/authorize?$q")
  CONNECT=""
  code=$(qparam "$CONNECT_LOC" code)
  [[ -n "$code" ]] || return 0
  CONNECT=$(curl -sS -X POST "$ONB_URL/api/oauth/token" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg c "$code" --arg id "$2" --arg r "$3" --arg v "$PKCE_V" '{grant_type:"authorization_code", code:$c, client_id:$id, redirect_uri:$r, code_verifier:$v}')")
}
app_rpc() { # access token, method, params (JSON) -> the response
  curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg m "$2" --argjson p "$3" '{method:$m, params:$p}')"
}
new_bunker() { # label -> a bunker URL from the account page's "Connect a Nostr app"
  curl -sS -b "$SESSION_JAR" -X POST "$ONB_URL/account/connections" --data-urlencode "label=$1" \
    | grep -o 'bunker://[^<"]*' | head -n1 | sed 's/&amp;/\&/g'
}
CHAT_CB="${CHAT_PUBLIC_URL:-http://localhost:3300}/oauth/callback"
WALLET_CB="${WALLET_PUBLIC_URL:-http://localhost:3400}/oauth/callback"

step "6d. apps sign in with Open Source Village: a bunker URL and tokens from Belfry's sign-in"
app_connect "$SESSION_JAR" belfry-chat "$CHAT_CB"
APP_TOKEN=$(jq -r '.access_token // empty' <<<"$CONNECT" 2>/dev/null); APP_BUNKER=$(jq -r '.bunker_url // empty' <<<"$CONNECT" 2>/dev/null)
APP_REFRESH=$(jq -r '.refresh_token // empty' <<<"$CONNECT" 2>/dev/null)
[[ "$CONNECT_LOC" == "$CHAT_CB?code="* && "$(qparam "$CONNECT_LOC" state)" == smoke ]] && ok "the chat (an own app) gets a code without a consent screen" || bad "authorize for the chat: '$CONNECT_LOC'"
[[ -n "$APP_TOKEN" && "$APP_BUNKER" == bunker://* && "$(jq -r .pubkey <<<"$CONNECT")" == "$MEMBER_PUB" ]] \
  && ok "token response: access and refresh token, bunker URL, the member's pubkey" || bad "token response: $CONNECT"
R=$(app_rpc "$APP_TOKEN" get_public_key '[]')
[[ "$(jq -r .result <<<"$R")" == "$MEMBER_PUB" ]] && ok "HTTP RPC get_public_key" || bad "get_public_key: $R"
R=$(app_rpc "$APP_TOKEN" sign_event "$(jq -nc --arg pk "$MEMBER_PUB" '[{pubkey:$pk, kind:1, content:"smoke", tags:[], created_at:now|floor}]')")
[[ "$(jq -r .result.pubkey <<<"$R")" == "$MEMBER_PUB" && -n "$(jq -r '.result.sig // empty' <<<"$R")" ]] && ok "HTTP RPC sign_event signs as the member" || bad "sign_event: $R"
R=$(app_rpc "$APP_TOKEN" derive_secret '["cashu-wallet-v1"]')
[[ "$(jq -r '.result // empty' <<<"$R")" == "" ]] && ok "the chat may not derive the wallet's secrets" || bad "chat derived a wallet secret: $R"
NOCOOKIE=$(mktemp)
app_connect "$NOCOOKIE" belfry-chat "$CHAT_CB" none
[[ "$(qparam "$CONNECT_LOC" error)" == login_required ]] && ok "silent sign-in without a session -> login_required" || bad "silent sign-in without a session: '$CONNECT_LOC'"
app_connect "$NOCOOKIE" belfry-chat "$CHAT_CB"
[[ "$CONNECT_LOC" == *"/login?next=%2Fapi%2Foauth%2Fauthorize"* ]] && ok "the sign-in button without a session goes to the login page and back" || bad "sign-in without a session: '$CONNECT_LOC'"
rm -f "$NOCOOKIE"
app_connect "$SESSION_JAR" village-notes "https://notes.example/callback" none
[[ "$(qparam "$CONNECT_LOC" error)" == consent_required ]] && ok "an outside app cannot sign in silently (consent_required)" || bad "outside app silently: '$CONNECT_LOC'"
CONSENT=$(curl -sS -b "$SESSION_JAR" "$ONB_URL/api/oauth/authorize?client_id=village-notes&redirect_uri=https%3A%2F%2Fnotes.example%2Fcallback&code_challenge=x&code_challenge_method=S256&state=s")
[[ "$CONSENT" == *"notes.example"* && "$CONSENT" == *'name="answer" value="allow"'* ]] && ok "an outside app gets the consent screen" || bad "consent screen: $(head -c 200 <<<"$CONSENT")"

step "6l. silent sign-in for Belfry's apps: prompt=none with a session, no consent for own apps"
app_connect "$SESSION_JAR" belfry-calendar "${CALENDAR_PUBLIC_URL:-http://localhost:3200}/oauth/callback" none
[[ -n "$(jq -r '.bunker_url // empty' <<<"$CONNECT" 2>/dev/null)" ]] && ok "the calendar signs in silently" || bad "silent sign-in of the calendar: '$CONNECT_LOC' $CONNECT"
for _ in 1 2 3 4 5 6; do app_connect "$SESSION_JAR" belfry-calendar "${CALENDAR_PUBLIC_URL:-http://localhost:3200}/oauth/callback" none; done
CONNS=$(curl -sS -b "$SESSION_JAR" "$ONB_URL/account" | grep -o '<strong>belfry-calendar</strong>' | wc -l)
[[ "$CONNS" -le 5 ]] && ok "each own app keeps at most five connections ($CONNS listed for the calendar)" || bad "calendar connections listed: $CONNS"

step "6o. village wallet: signs in silently, derives its seed and nutzap key from the participant's key"
app_connect "$SESSION_JAR" belfry-wallet "$WALLET_CB" none
WALLET_TOKEN=$(jq -r '.access_token // empty' <<<"$CONNECT" 2>/dev/null)
W1=$(app_rpc "$WALLET_TOKEN" derive_secret '["cashu-wallet-v1"]' | jq -r '.result // empty')
W2=$(app_rpc "$WALLET_TOKEN" derive_secret '["cashu-nutzap-v1"]' | jq -r '.result // empty')
[[ ${#W1} == 64 && ${#W2} == 64 && "$W1" != "$W2" ]] && ok "the wallet derives its seed and nutzap secret" || bad "wallet derive_secret: '$W1' '$W2'"
R=$(app_rpc "$WALLET_TOKEN" derive_secret '["something-else"]')
[[ -z "$(jq -r '.result // empty' <<<"$R")" ]] && ok "other purposes are refused" || bad "derive_secret of another purpose: $R"
# the key export (fresh sign-in) lets the test check the derivation is Keycast's, byte for byte
NSEC=$(curl -sS -b "$SESSION_JAR" -X POST "$ONB_URL/account/export" | grep -o 'nsec1[a-z0-9]*' | head -n1)
if [[ -n "$NSEC" ]]; then
  CHECK=$(python3 - "$NSEC" "$W1" "$W2" "$MEMBER_PUB" "$INFRA_DIR/scripts" <<'PY'
import hashlib, hmac, sys
nsec, w1, w2, pub, scripts = sys.argv[1:]
sys.path.insert(0, scripts)
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
data = [CHARSET.index(c) for c in nsec[nsec.rindex("1") + 1:-6]]
acc = bits = 0; out = []
for v in data:
    acc = (acc << 5) | v; bits += 5
    while bits >= 8:
        bits -= 8; out.append((acc >> bits) & 0xFF)
sk = bytes(out[:32])
import nostr_keys
derived_pub = nostr_keys.pubkey_hex(sk.hex())
def derive(purpose):  # HKDF-SHA256, salt "keycast/derive/v1", L = 32: one HMAC block
    prk = hmac.new(b"keycast/derive/v1", sk, hashlib.sha256).digest()
    return hmac.new(prk, purpose.encode() + b"\x01", hashlib.sha256).hexdigest()
print("ok" if derive("cashu-wallet-v1") == w1 and derive("cashu-nutzap-v1") == w2 and derived_pub == pub else "differs")
PY
)
  [[ "$CHECK" == ok ]] && ok "the exported key gives exactly the wallet's secrets (Keycast's derivation, so wallets keep their tokens)" || bad "derivation check: $CHECK"
else
  bad "no key export from the account page (fresh sign-in)"
fi
WCODE=$(curl -sS -o /dev/null -w '%{http_code}' "${WALLET_PUBLIC_URL:-http://localhost:3400}/")
[[ "$WCODE" == 200 ]] && ok "wallet app served (${WALLET_PUBLIC_URL:-http://localhost:3400})" || bad "wallet app -> $WCODE"

step "6h. login page: Odoo mails a one-time link and a 6-digit code; either signs the participant in"
login_mail() { # address -> "code secret" of the newest sign-in mail (Odoo's queue, else Mailpit)
  local m
  m=$(ox mail.mail search_read "[[[\"model\",\"=\",\"belfry.login.code\"],[\"email_to\",\"=\",\"$1\"]]]" '{"fields":["subject","body_html"],"order":"id desc","limit":1}' 2>/dev/null)
  if [[ "$(jq length <<<"$m")" == 1 ]]; then
    printf '%s %s\n' "$(jq -r '.[0].subject' <<<"$m" | grep -oE '^[0-9]{6}')" "$(jq -r '.[0].body_html' <<<"$m" | grep -o '/login/link/[A-Za-z0-9_-]*' | head -n1 | cut -d/ -f4)"
    return
  fi
  m=$(curl -sS --max-time 5 "$MAILPIT_URL/api/v1/search?query=$(jq -rn --arg v "to:$1 subject:\"sign-in code\"" '$v|@uri')" 2>/dev/null)
  [[ "$(jq -r '.messages_count // 0' <<<"$m" 2>/dev/null)" -ge 1 ]] || return 0
  local body; body=$(curl -sS "$MAILPIT_URL/api/v1/message/$(jq -r '.messages[0].ID' <<<"$m")" | jq -r '.HTML')
  printf '%s %s\n' "$(jq -r '.messages[0].Subject' <<<"$m" | grep -oE '^[0-9]{6}')" "$(grep -o '/login/link/[A-Za-z0-9_-]*' <<<"$body" | head -n1 | cut -d/ -f4)"
}
SENT=$(curl -sS -X POST "$ONB_URL/login/email" --data-urlencode "email=nobody-$STAMP@example.org" --data-urlencode "next=/account")
[[ "$SENT" == *"Check your mail"* && -z "$(login_mail "nobody-$STAMP@example.org")" ]] && ok "unknown address: the same answer, no mail" || bad "unknown address: $(head -c 120 <<<"$SENT")"
LJAR=$(mktemp)
SENT=$(curl -sS -b "$LJAR" -c "$LJAR" -X POST "$ONB_URL/login/email" --data-urlencode "email=$EMAIL" --data-urlencode "next=/me")
read -r LCODE LSECRET <<<"$(login_mail "$EMAIL")"
[[ "$SENT" == *"Check your mail"* && "$LCODE" =~ ^[0-9]{6}$ && -n "$LSECRET" ]] && ok "link and code mailed to $EMAIL" || bad "sign-in mail: code '$LCODE', link '$LSECRET'"
WRONG=$( [[ "$LCODE" == 000000 ]] && echo 000001 || echo 000000 )
R=$(curl -sS -b "$LJAR" -c "$LJAR" -X POST "$ONB_URL/login/code" --data-urlencode "email=$EMAIL" --data-urlencode "code=$WRONG" --data-urlencode "next=/me")
[[ "$R" == *"not right"* ]] && ok "a wrong code is refused" || bad "wrong code: $(head -c 120 <<<"$R")"
R=$(curl -sS -b "$LJAR" -c "$LJAR" -o /dev/null -w '%{http_code} %{redirect_url}' -X POST "$ONB_URL/login/code" --data-urlencode "email=$EMAIL" --data-urlencode "code=$LCODE" --data-urlencode "next=/me")
[[ "$R" == "303 /me" || "$R" == "303 $ONB_URL/me" ]] && grep -q belfry_session "$LJAR" && ok "the code signs the waiting browser in and goes on to where it was going" || bad "code sign-in: '$R'"
R=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$ONB_URL/login/link/$LSECRET")
[[ "$R" == 410 ]] && ok "after the code, the link of the same mail no longer works" || bad "link after code: $R"
curl -sS -X POST "$ONB_URL/login/email" --data-urlencode "email=$EMAIL" >/dev/null
read -r LCODE LSECRET <<<"$(login_mail "$EMAIL")"
KJAR=$(mktemp)
[[ "$(curl -sS "$ONB_URL/login/link/$LSECRET")" == *"/login/link/$LSECRET"* ]] && ok "opening the mail's link shows a page that signs in by script" || bad "link page"
R=$(curl -sS -b "$KJAR" -c "$KJAR" -o /dev/null -w '%{http_code}' -X POST "$ONB_URL/login/link/$LSECRET")
[[ "$R" == 303 ]] && grep -q belfry_session "$KJAR" && ok "the mail's link signs in any browser that opens it" || bad "link sign-in: $R"
R=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$ONB_URL/login/link/$LSECRET")
[[ "$R" == 410 ]] && ok "the link works once" || bad "link reuse: $R"
rm -f "$KJAR"
# Mail flooding: at most five sign-in mails per address in 30 minutes (Odoo, belfry.login.code).
LIMITED=""
for _ in 1 2 3 4 5 6; do
  R=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$ONB_URL/login/email" --data-urlencode "email=$EMAIL")
  [[ "$R" == 429 ]] && { LIMITED=yes; break; }
done
[[ -n "$LIMITED" ]] && ok "sign-in mails are limited per address (429 after five in half an hour)" || bad "no limit on sign-in mails"

step "6m. logout from an app ends the sign-in everywhere (no silent sign-in after it)"
app_connect "$LJAR" belfry-chat "$CHAT_CB" none
LREFRESH=$(jq -r '.refresh_token // empty' <<<"$CONNECT" 2>/dev/null); LACCESS=$(jq -r '.access_token // empty' <<<"$CONNECT" 2>/dev/null)
[[ -n "$LREFRESH" ]] && ok "the chat signed in silently in the code's browser" || bad "silent sign-in after the code: '$CONNECT_LOC'"
R=$(curl -sS -X POST "$ONB_URL/api/oauth/revoke" -H 'Content-Type: application/json' -d "$(jq -nc --arg t "$LREFRESH" '{token:$t, client_id:"belfry-chat"}')")
[[ "$(jq -r .ok <<<"$R")" == true ]] && ok "the app revokes its sign-in" || bad "revoke: $R"
R=$(app_rpc "$LACCESS" get_public_key '[]')
[[ -z "$(jq -r '.result // empty' <<<"$R")" ]] && ok "a revoked sign-in's token gets nothing" || bad "revoked token still answers: $R"
OUT_TO=$(curl -sS -b "$LJAR" -c "$LJAR" -o /dev/null -w '%{http_code} %{redirect_url}' "$ONB_URL/api/auth/oidc/logout?return_to=$(jq -rn --arg v "${CHAT_PUBLIC_URL:-http://localhost:3300}/" '$v|@uri')")
[[ "$OUT_TO" == "303 ${CHAT_PUBLIC_URL:-http://localhost:3300}/" ]] && ok "logout returns to the app" || bad "logout: '$OUT_TO'"
app_connect "$LJAR" belfry-chat "$CHAT_CB" none
[[ "$(qparam "$CONNECT_LOC" error)" == login_required ]] && ok "after the logout the apps no longer sign in silently" || bad "silent sign-in after logout: '$CONNECT_LOC'"
OUT_TO=$(curl -sS -o /dev/null -w '%{redirect_url}' "$ONB_URL/logout?return_to=https%3A%2F%2Fevil.example%2F")
[[ "$OUT_TO" == *"/login?logged_out=1" ]] && ok "logout goes back only to Belfry's own apps" || bad "logout to another site: '$OUT_TO'"
rm -f "$LJAR"

step "6f. Blossom: uploads need a signed BUD-11 event from a key with an active pass"
BLOSSOM_URL=${BLOSSOM_URL:-http://localhost:3100}
blossom_auth() { # verb [sha256] -> Authorization header value, signed by the signer with the participant's key
  # BUD-02: an upload/delete token names the blob it is for in an "x" tag.
  local ev; ev=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --arg v "$1" --arg x "${2:-}" --arg s "${BLOSSOM_PUBLIC_DOMAIN:-localhost:3100}" \
    '{pubkey:$pk, created_at:$t, kind:24242, tags:([["t",$v],["expiration",(($t+300)|tostring)],["server",$s]] + (if $x != "" then [["x",$x]] else [] end)), content:("Authorize "+$v)}')
  curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' \
    -d "$(jq -nc --argjson e "$ev" '{method:"sign_event", params:[$e]}')" | jq -r '.result | @base64'
}
if [[ -n "${APP_TOKEN:-}" ]]; then
  BLOB=$(mktemp); printf 'belfry smoke %s' "$STAMP" > "$BLOB"; SHA=$(sha256sum "$BLOB" | cut -c1-64)
  BAUTH=$(blossom_auth upload "$SHA")
  [[ -n "$BAUTH" && "$BAUTH" != bnVsbA== ]] && ok "kind-24242 auth event signed by the signer" || bad "could not sign a kind-24242 event: $BAUTH"
  NOAUTH=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BLOSSOM_URL/upload" -H 'Content-Type: text/plain' --data-binary "@$BLOB")
  [[ "$NOAUTH" == 401 ]] && ok "upload without authorization -> 401" || bad "upload without authorization -> $NOAUTH"
  UP=$(curl -sS -X PUT "$BLOSSOM_URL/upload" -H "Authorization: Nostr $BAUTH" -H 'Content-Type: text/plain' --data-binary "@$BLOB")
  [[ "$(jq -r '.sha256 // empty' <<<"$UP")" == "$SHA" ]] && ok "upload accepted: $(jq -r .url <<<"$UP")" || bad "upload: $UP"
  [[ "$(curl -sS "$BLOSSOM_URL/$SHA")" == "belfry smoke $STAMP" ]] && ok "blob readable without auth (content-addressed)" || bad "blob not readable"
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BLOSSOM_URL/mirror" -H "Authorization: Nostr $BAUTH" -H 'Content-Type: application/json' -d '{"url":"http://onboarding:8100/health"}')" == 404 ]] && ok "mirror endpoint blocked" || bad "mirror reachable"
  DEL=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$BLOSSOM_URL/$SHA" -H "Authorization: Nostr $BAUTH")
  [[ "$DEL" == 403 ]] && ok "delete with an upload token -> 403 (verb check)" || bad "delete with upload token -> $DEL"
else
  bad "no app access token from step 6d"
fi

step "6g. policy kinds: any kind signs (calendar, photos, proposals, NIP-17 chat, tasks, zaps)"
if [[ -n "${APP_TOKEN:-}" ]]; then
  kc_sign() { # kind [tags json] -> "ok" or the error
    local ev; ev=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --argjson k "$1" --argjson tags "${2:-[]}" '{pubkey:$pk, created_at:$t, kind:$k, tags:$tags, content:"smoke"}')
    local r; r=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$ev" '{method:"sign_event", params:[$e]}')")
    [[ -n "$(jq -r '.result.sig // empty' <<<"$r")" ]] && echo ok || jq -r '.error // .message // .' <<<"$r" | head -c 120
  }
  for spec in "31923:calendar event (NIP-52)" "31925:calendar RSVP" "20:picture (NIP-68)" "1068:poll (NIP-88)" "1018:poll response" "1111:comment (NIP-22)" "13:chat seal (NIP-17)" "10050:DM relay list" "30402:marketplace offer (NIP-99)" "30078:directory consent (NIP-78)" "2100:task (tasklist.sh)" "9735:zap receipt (outside the old list)"; do
    k=${spec%%:*}; name=${spec#*:}; res=$(kc_sign "$k")
    [[ "$res" == ok ]] && ok "kind $k ($name) signed" || bad "kind $k ($name): $res"
  done
  ENC=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --arg pk "$MEMBER_PUB" '{method:"nip44_encrypt", params:[$pk, "hello village"]}')")
  CT=$(jq -r '.result // empty' <<<"$ENC")
  [[ -n "$CT" ]] && ok "nip44_encrypt through the signer works (NIP-17 chat)" || bad "nip44_encrypt: $ENC"
  DEC=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --arg pk "$MEMBER_PUB" --arg ct "$CT" '{method:"nip44_decrypt", params:[$pk, $ct]}')")
  [[ "$(jq -r '.result // empty' <<<"$DEC")" == "hello village" ]] && ok "nip44_decrypt round trip" || bad "nip44_decrypt: $DEC"
else
  bad "no app access token from step 6d"
fi

step "6i. NIP-46: the bunker URL names the public relay and a remote-signing client connects through it"
# The relay inside bunker:// URLs is BUNKER_PUBLIC_RELAYS; the signer listens on the Compose-internal
# one. The client runs inside the onboarding container, so BUNKER_MAP rewrites the public host
# (empty on a public deployment, like WALLET_MAP).
BUNKER_MAP=${BUNKER_MAP-localhost:3080=bunker-relay:8080}
PUBLIC_RELAY=${BUNKER_PUBLIC_RELAYS:-ws://localhost:3080}
if [[ -n "${APP_BUNKER:-}" ]]; then
  BUNKER_RELAY=$(python3 -c 'import sys, urllib.parse as u; print((u.parse_qs(u.urlsplit(sys.argv[1]).query).get("relay") or [""])[0])' "$APP_BUNKER")
  [[ "$BUNKER_RELAY" == "$PUBLIC_RELAY" ]] && ok "the OAuth bunker URL names the public relay $BUNKER_RELAY" || bad "bunker URL relay is '$BUNKER_RELAY', expected $PUBLIC_RELAY"
  NIP46=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app exec -T onboarding python -m belfry_onboarding.nip46check "$APP_BUNKER" --expect-pubkey "$MEMBER_PUB")
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
[[ -n "${SAVE_BUNKER_URL:-}" && -n "${APP_BUNKER:-}" ]] && printf '%s\n' "$APP_BUNKER" > "$SAVE_BUNKER_URL"
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
# polls on Nostr: the vote service signs with the polls key, which the relay whitelist carries
if [[ -n "${POLLS_PUBKEY:-}" && "$POLLS_PUBKEY" != generated ]]; then
  VLOG=$("${COMPOSE[@]}" logs vote 2>/dev/null || true)
  [[ "$VLOG" == *"polls on Nostr as $POLLS_PUBKEY"* ]] && ok "the vote service announces polls as POLLS_PUBKEY" || bad "the vote service does not announce polls (POLLS_NSEC, RELAY_URL; rebuild/restart vote)"
  "${COMPOSE[@]}" exec -T onboarding grep -q "$POLLS_PUBKEY" /relay-config/rnostr.toml && ok "the relay whitelist carries the polls key" || bad "polls key not whitelisted (rebuild onboarding)"
fi
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
if [[ -n "${APP_TOKEN:-}" ]]; then
  # the participant's own NIP-98 token, signed by the signer like the app does
  EV=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --arg u "${PEOPLE_URL%/}/api/participants" '{pubkey:$pk, created_at:$t, kind:27235, tags:[["u",$u],["method","GET"]], content:""}')
  SIGNED=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')" | jq -c '.result')
  LIST=$(curl -sS -H "Authorization: Nostr $(printf '%s' "$SIGNED" | base64 -w0)" "$PEOPLE_URL/api/participants")
  jq -e --arg pk "$MEMBER_PUB" '.pubkeys | index($pk) != null' <<<"$LIST" >/dev/null && ok "the participant gets the list ($(jq '.pubkeys | length' <<<"$LIST") active keys)" || bad "participants list for the participant: $(head -c 200 <<<"$LIST")"
fi
echo "  note the browser check (directory opt-in, an offer with a photo, paying it through the wallet) is infra/scripts/village-check.mjs"

step "6s. text pages on pass.<domain>: privacy, terms, refunds, rules, help; linked from the landing page"
for page in "privacy|Privacy notice" "terms|Terms of participation" "refunds|Refund policy" "rules|Community rules" "help|Help"; do
  p=${page%%|*}; t=${page#*|}; H=$(curl -sS -o - -w '\n%{http_code}' "$ONB_URL/$p")
  [[ "${H##*$'\n'}" == 200 && "$H" == *"<title>$t</title>"* && "$H" != *"{{"* ]] && ok "/$p: $t" || bad "/$p: HTTP ${H##*$'\n'}"
done
HOME_PAGE=$(curl -sSL "$ONB_URL/")  # with SITE_URL, / forwards to the start page there
[[ "$HOME_PAGE" == *'href="/help"'* && "$HOME_PAGE" == *'href="/privacy"'* && "$HOME_PAGE" == *'href="/rules"'* ]] && ok "the landing page links help, rules and the legal pages" || bad "landing page links"
[[ "$(curl -sS "$ONB_URL/help")" == *"<strong>Chat</strong>"* ]] && ok "/help lists the community apps" || bad "/help has no app list (COMMUNITY_APPS)"

step "6q. photos (services/village): served on its own host; NIP-98 bound to it; pictures signed through the policy"
PHOTOS_URL=${PHOTOS_URL:-${PHOTOS_PUBLIC_URL:-http://localhost:3800}}
H=$(curl -sS -o - -w '\n%{http_code}' "$PHOTOS_URL/")
[[ "${H##*$'\n'}" == 200 && "$H" == *"<title>Open Source Village photos</title>"* ]] && ok "Open Source Village photos answers at $PHOTOS_URL" || bad "photos at $PHOTOS_URL: HTTP ${H##*$'\n'}"
JS=$(grep -o '/assets/index-[^"]*\.js' <<<"$H" | head -1)
BUNDLE=$(curl -sS "$PHOTOS_URL$JS")
[[ "$BUNDLE" == *"belfry-photos"* && "$BUNDLE" == *"${BLOSSOM_PUBLIC_URL:-http://localhost:3100}"* ]] && ok "bundle signs in as belfry-photos and uploads to ${BLOSSOM_PUBLIC_URL:-http://localhost:3100}" || bad "photos bundle lacks the client id or the Blossom URL"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$PHOTOS_URL/api/participants")" == 401 ]] && ok "participants list without NIP-98 auth: 401" || bad "participants list not refused without auth"
if [[ -n "${APP_TOKEN:-}" ]]; then
  EV=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --arg u "${PHOTOS_URL%/}/api/participants" '{pubkey:$pk, created_at:$t, kind:27235, tags:[["u",$u],["method","GET"]], content:""}')
  SIGNED=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')" | jq -c '.result')
  LIST=$(curl -sS -H "Authorization: Nostr $(printf '%s' "$SIGNED" | base64 -w0)" "$PHOTOS_URL/api/participants")
  jq -e --arg pk "$MEMBER_PUB" '.pubkeys | index($pk) != null' <<<"$LIST" >/dev/null && ok "the participant gets the list from the photos host" || bad "participants list from the photos host: $(head -c 200 <<<"$LIST")"
  # a token signed for the directory's URL is refused here (NIP-98 names the host)
  EV=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --arg u "${PEOPLE_URL%/}/api/participants" '{pubkey:$pk, created_at:$t, kind:27235, tags:[["u",$u],["method","GET"]], content:""}')
  SIGNED=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')" | jq -c '.result')
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Nostr $(printf '%s' "$SIGNED" | base64 -w0)" "$PHOTOS_URL/api/participants")" == 401 ]] && ok "a NIP-98 token for another app's URL: 401" || bad "photos accepted a token for the directory's URL"
  for k in 20 7 1111; do
    EV=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --argjson k "$k" '{pubkey:$pk, created_at:$t, kind:$k, tags:[], content:"smoke"}')
    R=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')")
    jq -e '.result.sig' <<<"$R" >/dev/null && ok "the signer signs kind $k (picture, like, comment)" || bad "kind $k refused: $(head -c 200 <<<"$R")"
  done
fi
echo "  note the browser check (upload, share, like, comment, tip through the wallet) is infra/scripts/photos-check.mjs"

step "6r. reports: a participant reports a photo, Odoo stores it, a steward hides it, the apps learn it"
if [[ -n "${APP_TOKEN:-}" ]]; then
  R_REF=$(openssl rand -hex 32); R_AUTHOR=$(openssl rand -hex 32)
  R_BODY=$(jq -nc --arg r "$R_REF" --arg a "$R_AUTHOR" '{target_kind:"photo", target_ref:$r, target_pubkey:$a, reason:"spam", note:"smoke test report", target_excerpt:"Buy now"}')
  EV=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --arg u "${PHOTOS_URL%/}/api/report" --arg h "$(printf '%s' "$R_BODY" | sha256sum | cut -c1-64)" \
    '{pubkey:$pk, created_at:$t, kind:27235, tags:[["u",$u],["method","POST"],["payload",$h]], content:""}')
  SIGNED=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')" | jq -c '.result')
  RES=$(curl -sS -X POST "$PHOTOS_URL/api/report" -H "Authorization: Nostr $(printf '%s' "$SIGNED" | base64 -w0)" -H 'Content-Type: application/json' --data-binary "$R_BODY")
  R_ID=$(jq -r '.id // empty' <<<"$RES")
  [[ -n "$R_ID" ]] && ok "report sent through the photos app (report $R_ID)" || bad "report: $RES"
  if [[ -n "$R_ID" ]]; then
    REP=$(ox belfry.report read "[[$R_ID]]" '{"fields":["state","app","target_ref","reporter_pass_id","reason"]}' | jq -c '.[0]')
    [[ "$(jq -r .state <<<"$REP")" == open && "$(jq -r .app <<<"$REP")" == photos && "$(jq -r '.reporter_pass_id[0]' <<<"$REP")" == "$PASS_ID" ]] \
      && ok "Odoo holds the report: app photos, reporter's pass $PASS_ID, open" || bad "report in Odoo: $REP"
    [[ "$(ox belfry.report belfry_report_handle "[[$R_ID],\"hide\",\"smoke@belfry.local\"]")" == '"hidden"' ]] && ok "a steward hides the content" || bad "hide the reported content"
    for _ in 1 2 3 4 5 6 7; do curl -sS "$PHOTOS_URL/api/hidden" | jq -e --arg r "$R_REF" '.hidden | index($r)' >/dev/null && break; sleep 5; done
    curl -sS "$PHOTOS_URL/api/hidden" | jq -e --arg r "$R_REF" '.hidden | index($r)' >/dev/null && ok "the apps' hidden list carries it" || bad "hidden list lacks the reported photo"
    ox belfry.report belfry_report_handle "[[$R_ID],\"dismiss\",\"smoke@belfry.local\"]" >/dev/null || true
  fi
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$PHOTOS_URL/api/report" --data-binary "$R_BODY")" == 401 ]] && ok "an unsigned report: 401" || bad "unsigned report not refused"
fi

step "6t. programme (services/village): served on its own host, follows the programme key; RSVPs and comments signed"
PROGRAMME_URL=${PROGRAMME_URL:-${PROGRAMME_PUBLIC_URL:-http://localhost:3900}}
H=$(curl -sS -o - -w '\n%{http_code}' "$PROGRAMME_URL/")
[[ "${H##*$'\n'}" == 200 && "$H" == *"<title>Open Source Village programme</title>"* ]] && ok "Open Source Village programme answers at $PROGRAMME_URL" || bad "programme at $PROGRAMME_URL: HTTP ${H##*$'\n'}"
JS=$(grep -o '/assets/index-[^"]*\.js' <<<"$H" | head -1)
BUNDLE=$(curl -sS "$PROGRAMME_URL$JS")
[[ "$BUNDLE" == *"belfry-programme"* ]] && ok "bundle signs in as belfry-programme" || bad "programme bundle lacks its client id"
if [[ -n "${PROGRAMME_PUBKEY:-}" ]]; then
  [[ "$BUNDLE" == *"$PROGRAMME_PUBKEY"* ]] && ok "bundle follows the programme key" || bad "programme bundle lacks PROGRAMME_PUBKEY (rebuild village after gen-secrets)"
fi
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$PROGRAMME_URL/api/participants")" == 401 ]] && ok "participants list without NIP-98 auth: 401" || bad "participants list not refused without auth"
if [[ -n "${APP_TOKEN:-}" ]]; then
  for k in 31925 1111; do
    EV=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --argjson k "$k" '{pubkey:$pk, created_at:$t, kind:$k, tags:[], content:"smoke"}')
    R=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')")
    jq -e '.result.sig' <<<"$R" >/dev/null && ok "the signer signs kind $k (RSVP, comment)" || bad "kind $k refused: $(head -c 200 <<<"$R")"
  done
fi
[[ "$BUNDLE" == *"$VOTE_URL"* ]] && ok "bundle links the polls app ($VOTE_URL)" || bad "programme bundle lacks the polls app's URL"
PH=$(curl -sS -D - -o /dev/null -H "Origin: $PROGRAMME_URL" "$VOTE_URL/api/polls?session=31923:$(printf '%064d' 0):smoke")
grep -qi '^access-control-allow-origin: \*' <<<"$PH" && ok "the polls about a session are readable from the programme (CORS)" || bad "polls list without CORS: $(head -c 200 <<<"$PH")"
echo "  note the browser check (day list, search, RSVP, comment, photos, announcing, polls, the calendar app) is infra/scripts/programme-check.mjs"

step "6u. groups (Flotilla on zooid): app served without third-party services; the relay's members are the active passes"
GROUPS_URL=${GROUPS_URL:-${GROUPS_PUBLIC_URL:-http://localhost:3150}}
GROUPS_RELAY=${GROUPS_RELAY_URL:-ws://localhost:3190}
GROUPS_RELAY_HTTP=$(sed 's#^ws://#http://#; s#^wss://#https://#' <<<"$GROUPS_RELAY")
H=$(curl -sS -o - -w '\n%{http_code}' "$GROUPS_URL/")
[[ "${H##*$'\n'}" == 200 && "$H" == *"<title>Open Source Village groups</title>"* ]] && ok "Open Source Village groups answers at $GROUPS_URL" || bad "groups at $GROUPS_URL: HTTP ${H##*$'\n'}"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$GROUPS_URL/spaces/x/about")" == 200 ]] && ok "app routes fall back to the app (SPA)" || bad "app route not served"
GROUPS_JS=""
for f in $(grep -oE '/?_app/immutable/(entry|chunks|nodes)/[^"]+\.js' <<<"$H" | sed 's#^/##' | sort -u); do GROUPS_JS+=$(curl -sS "$GROUPS_URL/$f"); done
ENV_CHUNK=$(grep -oE 'chunks/[A-Za-z0-9_-]+\.js' <<<"$GROUPS_JS" | sort -u | while read -r c; do curl -sS "$GROUPS_URL/_app/immutable/$c" | grep -q VITE_PLATFORM_RELAYS && echo "$c" && break; done)
GROUPS_ENV=$([[ -n "$ENV_CHUNK" ]] && curl -sS "$GROUPS_URL/_app/immutable/$ENV_CHUNK")
[[ "$GROUPS_ENV" == *"belfry-groups"* && "$GROUPS_ENV" == *"$GROUPS_RELAY"* ]] && ok "bundle signs in as belfry-groups on the space $GROUPS_RELAY" || bad "groups bundle lacks its client id or relay ($ENV_CHUNK)"
[[ "$GROUPS_ENV" != *"dufflepud.coracle"* && "$GROUPS_ENV" != *"nps.flotilla"* && "$GROUPS_ENV" != *"pomade."* && "$H" != *"plausible"* ]] && ok "no Coracle proxy, push, Pomade or analytics in the groups build" || bad "the groups build still names a third-party service"
NIP11=$(curl -sS -H 'Accept: application/nostr+json' "$GROUPS_RELAY_HTTP/")
jq -e '.limitation.auth_required == true and (.supported_nips | map(tostring) | index("29"))' <<<"$NIP11" >/dev/null && ok "groups relay: NIP-29, AUTH required ($(jq -r .name <<<"$NIP11"))" || bad "groups relay NIP-11: $(head -c 200 <<<"$NIP11")"
GROUPS_OWNER=$(jq -r .pubkey <<<"$NIP11")
[[ -z "${GROUPS_API_PUBKEY:-}" || "$GROUPS_OWNER" == "$GROUPS_API_PUBKEY" ]] && ok "groups relay owner is the groups API key" || bad "groups relay owner $GROUPS_OWNER is not GROUPS_API_PUBKEY"
# a member check over zooid's member API, from inside the onboarding service (internal host name)
groups_member() {
  "${COMPOSE[@]}" exec -T onboarding python -c '
import sys
from belfry_onboarding.app import build_services
from belfry_onboarding.settings import Settings
g = build_services(Settings()).groups
print("yes" if g and sys.argv[1] in g.members() else "no")' "$1" 2>/dev/null | tail -n1
}
if [[ -n "${MEMBER_PUB:-}" ]]; then
  [[ "$(groups_member "$MEMBER_PUB")" == yes ]] && ok "the participant is a member of the groups relay" || bad "participant $MEMBER_PUB not a member of the groups relay"
fi
if [[ -n "${APP_TOKEN:-}" ]]; then
  for k in 9 9007 9021 28934; do
    EV=$(jq -nc --arg pk "$MEMBER_PUB" --argjson t "$(date +%s)" --argjson k "$k" '{pubkey:$pk, created_at:$t, kind:$k, tags:[["h","smoke"]], content:""}')
    R=$(curl -sS -X POST "$ONB_URL/api/nostr" -H "Authorization: Bearer $APP_TOKEN" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$EV" '{method:"sign_event", params:[$e]}')")
    jq -e '.result.sig' <<<"$R" >/dev/null && ok "the signer signs kind $k (groups)" || bad "kind $k refused: $(head -c 200 <<<"$R")"
  done
fi
echo "  note the browser check (silent sign-in, a room of one's own, a message read back) is infra/scripts/groups-check.mjs"

step "6v. an agent acts for the participant with nothing but the bunker URL: invite by e-mail, photo, posts, groups"
# agentcheck signs every step through the participant's bunker (NIP-46): an invite by e-mail and a code on
# /api/invites (NIP-98), a photo on Blossom, a note, reply, reaction and repost on the community
# relay, a private group with a member and a public group on the groups relay. AGENT_MAP rewrites
# public hosts for the transport inside the onboarding container (empty on a public deployment).
AGENT_MAP=${AGENT_MAP-localhost:8069=odoo:8069,localhost:3080=bunker-relay:8080,localhost:3090=relay:8080,localhost:3190=groups-relay:3334,localhost:3100=blossom-gate:80}
AGENT_INVITEE="agent-invitee-$(date +%s)@example.org"
# The agent gets a bunker of its own: a bunker secret binds to the first client key that connects
# (step 6i used the app's), so the participant connects one more app on the account page.
AGENT_BUNKER=$(new_bunker "Smoke agent")
if [[ -n "$AGENT_BUNKER" && "$AGENT_BUNKER" != "${APP_BUNKER:-}" ]]; then
  AGENT=("${COMPOSE[@]}" exec -T onboarding python -m belfry_onboarding.agentcheck "$AGENT_BUNKER"
    --pass-url "${ONBOARDING_PUBLIC_URL:-http://localhost:8100}" --relay "${COMMUNITY_RELAY_URL:-ws://localhost:3090}"
    --groups-relay "${GROUPS_RELAY_URL:-ws://localhost:3190}" --blossom "${BLOSSOM_PUBLIC_URL:-http://localhost:3100}"
    --invite "$AGENT_INVITEE")
  [[ -n "$AGENT_MAP" ]] && AGENT+=(--map "$AGENT_MAP")
  if OUT=$("${AGENT[@]}" 2>&1); then sed 's/^/  /' <<<"$OUT"; ok "agent: invite, photo, posts and groups, all signed through the bunker"
  else sed 's/^/  /' <<<"$OUT" >&2; bad "agent through the bunker"; fi
  AGENT_INVITE=$(sed -n 's/^INVITE_ID=//p' <<<"$OUT")
  if [[ -n "$AGENT_INVITE" ]]; then
    N=$(ox mail.mail search_count "[[[\"model\",\"=\",\"belfry.invite\"],[\"res_id\",\"=\",$AGENT_INVITE],[\"email_to\",\"=\",\"$AGENT_INVITEE\"]]]")
    [[ "$N" == 1 ]] && ok "the invite link is mailed to $AGENT_INVITEE" || bad "invite mails to $AGENT_INVITEE: $N"
    ox belfry.invite action_revoke "[[$AGENT_INVITE]]" >/dev/null  # leave no open invite behind
  fi
else
  bad "no second bunker URL for the agent ('$AGENT_BUNKER')"
fi
# The one-action command (belfry_onboarding.agent) keeps its client key between runs, so one
# bunker URL serves every call: me, invite, status, revoke, each a separate connection.
CLI_BUNKER=$(new_bunker "Smoke agent command")
if [[ -n "$CLI_BUNKER" ]]; then
  CLI_KEY="/tmp/belfry-agent-$(date +%s).key"
  cli() { "${COMPOSE[@]}" exec -T -e BELFRY_BUNKER="$CLI_BUNKER" onboarding python -m belfry_onboarding.agent \
    --domain "${BELFRY_DOMAIN:-localhost}" --pass-url "${ONBOARDING_PUBLIC_URL:-http://localhost:8100}" \
    --key-file "$CLI_KEY" ${AGENT_MAP:+--map "$AGENT_MAP"} "$@" 2>&1; }
  OUT=$(cli me); [[ "$(jq -r .member <<<"$OUT" 2>/dev/null)" == true ]] && ok "agent me: member" || bad "agent me: $(head -c 200 <<<"$OUT")"
  CLI_INVITEE="agent-cli-$(date +%s)@example.org"
  OUT=$(cli invite "$CLI_INVITEE" --name "CLI Invitee"); CLI_ID=$(jq -r '.id // empty' <<<"$OUT" 2>/dev/null)
  [[ -n "$CLI_ID" ]] && ok "agent invite $CLI_INVITEE -> $CLI_ID (same client key, second connection)" || bad "agent invite: $(head -c 200 <<<"$OUT")"
  if [[ -n "$CLI_ID" ]]; then
    OUT=$(cli status "$CLI_ID"); [[ "$(jq -r .state <<<"$OUT")" == open ]] && ok "agent status: open" || bad "agent status: $(head -c 200 <<<"$OUT")"
    OUT=$(cli revoke "$CLI_ID"); [[ "$(jq -r .state <<<"$OUT")" == revoked ]] && ok "agent revoke: revoked" || bad "agent revoke: $(head -c 200 <<<"$OUT")"
  fi
  "${COMPOSE[@]}" exec -T onboarding rm -f "$CLI_KEY"
else
  bad "no bunker URL for the agent command"
fi

step "6x. git server (ngit-grasp): members only; an agent announces a repository, pushes, clones, opens an issue"
GIT_URL=${GIT_PUBLIC_URL:-http://localhost:3170}
NIP11=$(curl -sS -H 'Accept: application/nostr+json' "$GIT_URL/")
jq -e '.supported_grasps | index("GRASP-08")' <<<"$NIP11" >/dev/null && ok "git server: private (GRASP-08), $(jq -r .name <<<"$NIP11")" || bad "git server NIP-11: $(head -c 200 <<<"$NIP11")"
if [[ "$GIT_URL" == https://* ]]; then  # behind the proxy: browsers get Belfry's page, not ngit-grasp's
  H=$(curl -sS -H 'Accept: text/html' "$GIT_URL/")
  [[ "$H" == *"<title>Git server</title>"* && "$H" == *"Open Source Village"* && "$H" != *"gitworkshop.dev"* ]] && ok "git server landing page is Belfry's (no gitworkshop.dev link)" || bad "git landing page: $(head -c 200 <<<"$H")"
  H=$(curl -sS -H 'Accept: text/html' "$GIT_URL/npub1x/some-repo.git")
  [[ "$H" == *"<title>Git server</title>"* ]] && ok "repository pages show the same page to browsers" || bad "git repository page for browsers: $(head -c 200 <<<"$H")"
fi
GIT_MEMBERS=$("${COMPOSE[@]}" exec -T onboarding sh -c 'cat /git-members/members.txt' 2>/dev/null)
[[ -n "${MEMBER_PUB:-}" ]] && { grep -qx "$MEMBER_PUB" <<<"$GIT_MEMBERS" && ok "the participant is in the git server's members file ($(grep -c '^[0-9a-f]' <<<"$GIT_MEMBERS") keys)" || bad "participant $MEMBER_PUB not in the git members file"; }
# The agent signs through a bunker of its own (one more "Connect a Nostr app", as in 6v).
GIT_MAP=${GIT_MAP-localhost:3170=git:7334,localhost:3080=bunker-relay:8080}
GIT_BUNKER=$(new_bunker "Smoke git agent")
if [[ -n "$GIT_BUNKER" ]]; then
  GITCHECK=("${COMPOSE[@]}" exec -T onboarding python -m belfry_onboarding.gitcheck "$GIT_BUNKER" --git "$GIT_URL")
  [[ -n "$GIT_MAP" ]] && GITCHECK+=(--map "$GIT_MAP")
  if OUT=$("${GITCHECK[@]}" 2>&1); then sed 's/^/  /' <<<"$OUT"; ok "agent on the git server: refused stranger, push, clone, issue and comment through the bunker"
  else sed 's/^/  /' <<<"$OUT" >&2; bad "agent on the git server"; fi
else
  bad "no bunker URL for the git agent"
fi

step "6w. stewards get a free pass: add-steward activates a full-access pass on a zero invoice"
STEWARD_EMAIL="steward-$(date +%s)@example.org"
OUT=$("${COMPOSE[@]}" exec -T onboarding python -m belfry_onboarding add-steward "$STEWARD_EMAIL" "Smoke Steward" 2>/dev/null | tail -n1)
SP_ID=$(jq -r '.pass.pass_id // empty' <<<"$OUT")
[[ "$(jq -r .pass.state <<<"$OUT")" == active ]] && ok "add-steward: steward and free pass $SP_ID active ($(jq -r .pass.result <<<"$OUT"))" || bad "add-steward: $OUT"
if [[ -n "$SP_ID" ]]; then
  SP=$(ox belfry.pass read "[[$SP_ID]]" '{"fields":["state","tier","steward_pass","invoice_id"]}')
  jq -e '.[0] | .state == "active" and .tier == "full" and .steward_pass' <<<"$SP" >/dev/null && ok "the steward pass is active, full access" || bad "steward pass: $SP"
  SINV=$(ox account.move read "[[$(jq -r '.[0].invoice_id[0]' <<<"$SP")]]" '{"fields":["amount_total","payment_state"]}')
  jq -e '.[0] | .amount_total == 0 and .payment_state == "paid"' <<<"$SINV" >/dev/null && ok "on a paid invoice of 0" || bad "steward invoice: $SINV"
  AGAIN=$("${COMPOSE[@]}" exec -T onboarding python -m belfry_onboarding add-steward "$STEWARD_EMAIL" 2>/dev/null | tail -n1)
  [[ "$(jq -r .pass.result <<<"$AGAIN")" == already_active && "$(jq -r .pass.pass_id <<<"$AGAIN")" == "$SP_ID" ]] && ok "add-steward again keeps the one pass" || bad "add-steward again: $AGAIN"
  SREV=$(onb_post "/admin/passes/$SP_ID/revoke?token=$HOOK_TOKEN")
  [[ "$(jq -r .result <<<"$SREV")" == revoked ]] && ok "smoke steward's pass revoked again" || bad "steward pass cleanup: $SREV"
fi
# the steward signs in to the steward pages with Belfry's sign-in (a fresh sign-in link)
SPARTNER=$(jq -r '.partner_id // empty' <<<"$OUT")
[[ -n "$SPARTNER" && "$(ox res.partner read "[[$SPARTNER]]" '{"fields":["belfry_steward"]}' | jq -r '.[0].belfry_steward')" == true ]] \
  && ok "the steward is an Odoo contact marked steward (partner $SPARTNER)" || bad "steward contact: $OUT"
# the smoke steward is no steward any more (steward-passes would give it a pass back)
ox res.partner write "[[${SPARTNER:-0}],{\"belfry_steward\":false}]" >/dev/null 2>&1 || true
[[ "$("${COMPOSE[@]}" exec -T onboarding python -c '
from belfry_onboarding import core
from belfry_onboarding.app import build_services
from belfry_onboarding.settings import Settings
import sys
print(sys.argv[1] in core.steward_emails(build_services(Settings())))' "$STEWARD_EMAIL" 2>/dev/null | tail -n1)" == False ]] \
  && ok "smoke steward is no steward any more" || bad "smoke steward still a steward"

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
[[ "$(jq -r .payload.vct <<<"$CRED")" == "${ONBOARDING_PUBLIC_URL:-$ONB_URL}/vct/village-pass/v1" || "$(jq -r .payload.vct <<<"$CRED")" == *"/vct/village-pass/v1" ]] && ok "vct $(jq -r .payload.vct <<<"$CRED")" || bad "vct: $(jq -r .payload.vct <<<"$CRED")"
[[ "$(jq -r .payload.pass_tier <<<"$CRED")" == week && "$(jq -r .disclosed.member_number <<<"$CRED")" == "$MEMBER" && "$(jq -r .disclosed.npub <<<"$CRED")" == "$NPUB" ]] && ok "claims: tier week, member $MEMBER, npub (disclosable)" || bad "claims: $(jq -c '{p: .payload.pass_tier, d: .disclosed}' <<<"$CRED")"
[[ "$(jq -r .payload.status.status_list.idx <<<"$CRED")" == "$PASS_ID" ]] && ok "status list index = pass id" || bad "status idx $(jq -c .payload.status <<<"$CRED")"
VCTM=$(curl -sS "$ONB_URL/vct/village-pass/v1" | jq -r .vct); [[ "$VCTM" == "$(jq -r .payload.vct <<<"$CRED")" ]] && ok "type metadata served at the vct URL" || bad "vctm: $VCTM"
# Signed issuer metadata, as the EUDI reference wallet requires it (Accept: application/jwt): with
# the EUDI registrar's access certificate and issuer_info when infra/issuer/keys/eudi/ has them
# (eudi-registry-certs.sh), else with Belfry's issuer key. Locally there is no proxy in front of
# Issuer2, so the wallet reads the onboarding service's metadata route directly.
EUDI_X5C="$INFRA_DIR/issuer/keys/eudi/wrpac.x5c.json"
ISSUER_ID=$(python3 -c 'import json,sys,urllib.parse as u; print(json.loads(u.parse_qs(u.urlsplit(sys.argv[1]).query)["credential_offer"][0])["credential_issuer"])' "$OFFER_URL")
META_ARGS=(); [[ -n "$WALLET_MAP" ]] && META_ARGS=(--url http://localhost:8100/issuer-metadata)
META=$("${WALLET[@]}" metadata "$ISSUER_ID" "${META_ARGS[@]}" 2>&1 | tail -n1)
if [[ -f "$EUDI_X5C" ]]; then
  WRPAC_HASH=$(python3 -c 'import base64,hashlib,json,sys; print(base64.urlsafe_b64encode(hashlib.sha256(base64.b64decode(json.load(open(sys.argv[1]))[0])).digest()).rstrip(b"=").decode())' "$EUDI_X5C")
  [[ "$(jq -r .x509_hash <<<"$META")" == "$WRPAC_HASH" && "$(jq -c .issuer_info <<<"$META")" == '["registration_cert"]' ]] \
    && ok "issuer metadata signed with the EUDI access certificate, registration certificate in issuer_info" || bad "signed metadata: $META"
else
  [[ "$(jq -r .signed <<<"$META")" == true && "$(jq -r .sub <<<"$META")" == "$ISSUER_ID" && "$(jq -r .x5c <<<"$META")" == 2 ]] \
    && ok "issuer metadata signed (application/jwt) with Belfry's issuer certificate" || bad "signed metadata: $META"
fi

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

step "8b. sign-in offer (authorization code + issuer_state, as DIDroom uses it): Issuer2's login at Belfry"
SOFFER=$(curl -sS "$ONB_URL/claim/$CLAIM_TOKEN/offer?signin=1" | jq -r '.credential_offer // empty')
[[ "$(python3 -c 'import sys,urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' "$SOFFER")" == *'"issuer_state"'* ]] \
  && ok "sign-in offer carries issuer_state" || bad "sign-in offer: $SOFFER"
# The wallet pushes the authorization request; a browser (the participant's session from 6c)
# follows the redirects through Issuer2's login at Belfry (/oidc/authorize) back to the wallet.
SIGNIN_STATE=/tmp/smoke-signin.json
AUTHZ_URL=$("${WALLET[@]}" signin-start "$SOFFER" --state "$SIGNIN_STATE" 2>&1) || die "signin-start failed: $AUTHZ_URL"
AUTHZ_URL=$(tail -n1 <<<"$AUTHZ_URL")
ok "PAR accepted, browser sent to $(sed 's/?.*//' <<<"$AUTHZ_URL")"
BROWSER=$(mktemp); cp "$SESSION_JAR" "$BROWSER"
URL=$AUTHZ_URL; HOPS=""
for _ in $(seq 12); do
  read -r LAST_STATUS NEXT < <(curl -sS -b "$BROWSER" -c "$BROWSER" -o "$BROWSER.body" -w '%{http_code} %{redirect_url}\n' "$URL")
  [[ -z "$NEXT" ]] && { HOPS+="[$LAST_STATUS: $(head -c 300 "$BROWSER.body")]"; break; }
  HOPS+="$(sed -E 's#^(https?://[^/]+/[^?]*).*#\1#' <<<"$NEXT") "
  URL=$NEXT
  [[ "$URL" == https://wallet.belfry.test/* ]] && break
done
rm -f "$BROWSER" "$BROWSER.body"
[[ "$URL" == https://wallet.belfry.test/callback\?*code=* ]] && ok "Issuer2's login at Belfry -> wallet redirect with a code" \
  || die "sign-in flow ended at '$URL' (hops: $HOPS)"
[[ "$HOPS" == *"/oidc/authorize"* ]] && ok "the participant passed Belfry's sign-in (client belfry-issuer), signed in already" || bad "no hop through Belfry's sign-in: $HOPS"
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
if [[ -f "$EUDI_X5C" ]]; then
  [[ "$(jq -r .client_id <<<"$PRES")" == "x509_hash:$WRPAC_HASH" && "$(jq -c .verifier_info <<<"$PRES")" == '["registration_cert"]' ]] \
    && ok "door request signed with the EUDI access certificate (x509_hash), registration certificate in verifier_info" || bad "door request: $(jq -c '{client_id, verifier_info}' <<<"$PRES")"
else
  [[ "$(jq -r .client_id <<<"$PRES")" == x509_san_dns:* && "$(jq -c .verifier_info <<<"$PRES")" == '[]' ]] \
    && ok "door request signed under Belfry's CA ($(jq -r .client_id <<<"$PRES"))" || bad "door request: $(jq -c '{client_id, verifier_info}' <<<"$PRES")"
fi
for i in 1 2 3 4 5; do RES=$(curl -sS "$ONB_URL/door/sessions/$DSID"); [[ "$(jq -r .status <<<"$RES")" != pending ]] && break; sleep 1; done
[[ "$(jq -r .status <<<"$RES")" == valid && "$(jq -r .tier <<<"$RES")" == week ]] && ok "door: valid pass, tier week, until $(jq -r .valid_until <<<"$RES")" || bad "door result: $RES"
curl -sS -o /dev/null -w '%{http_code}' "$ONB_URL/door" | grep -q 200 && ok "GET /door -> 200 (QR page)" || bad "door page"

step "9b. the pass on the phone without a wallet: /me with Belfry's session, /check at the door"
MEJAR=$(mktemp); cp "$SESSION_JAR" "$MEJAR"
ME_PAGE=$(curl -sS -L -b "$MEJAR" -c "$MEJAR" "$ONB_URL/me")
[[ "$ME_PAGE" == *'id="qr"'* && "$ME_PAGE" == *"Week pass"* ]] && ok "/me: signed in without a click, shows the renewing QR" || bad "/me: $(grep -o '<h1>[^<]*</h1>\|class="bad">[^<]*' <<<"$ME_PAGE" | head -2 | tr '\n' ' ')"
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
# Inviting a friend from the pass page: a link that names the participant, revoked again.
INV_PAGE=$(curl -sS -L -b "$MEJAR" -c "$MEJAR" --data-urlencode "note=smoke friend" "$ONB_URL/me/invites")
FRIEND_URL=$(grep -o 'data-copy="[^"]*/belfry/invite/[^"]*"' <<<"$INV_PAGE" | head -n1 | sed 's/^data-copy="//; s/"$//')
FRIEND_ID=$(grep -o 'action="/me/invites/[0-9]*/revoke"' <<<"$INV_PAGE" | head -n1 | grep -o '[0-9]\+')
[[ "$INV_PAGE" == *"Invite link created"* && -n "$FRIEND_URL" ]] && ok "/me: invite link for a friend created" || bad "/me invite: $(grep -o 'class="bad">[^<]*' <<<"$INV_PAGE" | head -1)"
[[ "$(curl -sS "$FRIEND_URL")" == *"You were invited by <strong>$NAME</strong>"* ]] && ok "the friend's invite page names $NAME" || bad "friend's invite page does not name the participant ($FRIEND_URL)"
[[ -n "$FRIEND_ID" && "$(curl -sS -L -b "$MEJAR" --data "" "$ONB_URL/me/invites/$FRIEND_ID/revoke")" == *"Invite revoked"* ]] && ok "the participant revoked the unused invite" || bad "invite $FRIEND_ID not revoked"
rm -f "$MEJAR"

step "9c. the start page and the feedback page"
if [[ -n "${SITE_URL:-}" ]]; then
  [[ "$(curl -sS -o /dev/null -w '%{redirect_url}' "$ONB_URL/")" == "${SITE_URL%/}"* ]] && ok "$ONB_URL/ forwards to the start page $SITE_URL" || bad "$ONB_URL/ does not forward to $SITE_URL"
  START=$(curl -sS "$SITE_URL/")
else
  START=$(curl -sS "$ONB_URL/")
fi
[[ "$START" == *"How to join"* && "$START" == *"Get an invite"* ]] && ok "start page explains how to join" || bad "start page: $(grep -o '<h1>[^<]*' <<<"$START" | head -1)"
if [[ -n "${TEST_RUN_NOTICE:-}" ]]; then
  [[ "$START" == *"test-notice"* ]] && ok "test-run notice shown" || bad "TEST_RUN_NOTICE set but not on the start page"
  [[ "$(curl -sS "$FRIEND_URL")" == *"belfry-test-notice"* ]] && ok "test-run notice on Odoo's invite page" || bad "no test-run notice on Odoo's invite page"
else
  ok "no test-run notice configured"
fi
[[ "$(curl -sS "$ONB_URL/feedback?from=$ONB_URL/me")" == *"What happened?"* ]] && ok "feedback form served" || bad "no feedback form"
[[ "$(curl -sS "$ONB_URL/apply")" == *'action="/apply"'* ]] && ok "application form served" || bad "no application form"

step "9d. volunteers: a pending volunteer pass, three shifts on the shift page, the pass activates"
VDAY=$(date -u -d '+20 days' +%F)
SHIFT_IDS=()
for h in 08 12 16; do
  SHIFT_IDS+=("$(osvc belfry.shift belfry_steward_shift_create "[{\"name\":\"Smoke shift $h\",\"start\":\"$VDAY $h:00:00\",\"end\":\"$VDAY $h:30:00\",\"capacity\":2}]")")
done
VOL_EMAIL="smoke-vol-$STAMP@belfry.local"
VOL_PASS=$(osvc belfry.pass belfry_steward_volunteer_add "[\"Smoke Volunteer $STAMP\",\"$VOL_EMAIL\"]")
VOL_TOKEN=$(osvc belfry.pass read "[[$VOL_PASS],[\"volunteer_token\"]]" | jq -r '.[0].volunteer_token // empty')
[[ -n "$VOL_TOKEN" ]] && ok "volunteer pass $VOL_PASS pending, shift page link made" || bad "volunteer pass: '$VOL_PASS'"
[[ "$(curl -sS "$ONB_URL/shifts/$VOL_TOKEN")" == *"0 of 3"* ]] && ok "shift page: 0 of 3 shifts" || bad "shift page"
LAST=""
for sid in "${SHIFT_IDS[@]}"; do
  LAST=$(curl -sS -o /dev/null -w '%{redirect_url}' -X POST "$ONB_URL/shifts/$VOL_TOKEN/$sid/signup")
done
VSTATE=$(ox belfry.pass read "[[$VOL_PASS]]" '{"fields":["state","tier","date_start","date_end","login_setup_url"]}' | jq -c '.[0]')
[[ "$LAST" == *"msg=active" && "$(jq -r .state <<<"$VSTATE")" == active && "$(jq -r .login_setup_url <<<"$VSTATE")" == *"/signin/"* ]] \
  && ok "the third shift activates the volunteer pass ($(jq -r '.date_start + " to " + .date_end' <<<"$VSTATE")), sign-in mail with it" \
  || bad "volunteer activation: $LAST $VSTATE"
VREV=$(onb_post "/admin/passes/$VOL_PASS/revoke?token=$HOOK_TOKEN")
osvc belfry.shift belfry_steward_shift_cancel "[[$(IFS=,; echo "${SHIFT_IDS[*]}")]]" >/dev/null
[[ "$(jq -r .result <<<"$VREV")" == revoked ]] && ok "smoke volunteer revoked, shifts removed" || bad "volunteer cleanup: $VREV"

step "10. sign in by presenting the pass (OpenID4VP, from the login page)"
PJAR=$(mktemp); pj() { curl -sS -b "$PJAR" -c "$PJAR" "$@"; }
BPAGE=$(pj "$ONB_URL/login/pass?next=%2Fme"); BSID=$(sed -n 's/.*data-sid="\([^"]*\)".*/\1/p' <<<"$BPAGE" | head -n1)
[[ -n "$BSID" ]] && ok "the login page shows the pass QR (sign-in $BSID)" || die "no pass sign-in in the page: $(head -c 200 <<<"$BPAGE")"
BREQ=$(sed -n 's/.*<a class="btn" href="\([^"]*\)">Open the wallet.*/\1/p' <<<"$BPAGE" | head -n1 | sed 's/&amp;/\&/g')
PRES=$("${WALLET[@]}" present "$BREQ" --wallet /tmp/smoke-wallet.json --disclose member_number,npub,pass_tier 2>&1 | tail -n1)
[[ "$(jq -r .status <<<"$PRES")" == 200 ]] && ok "wallet presented member number, npub and tier" || bad "present: $PRES"
for i in 1 2 3 4 5; do POLL=$(curl -sS "$ONB_URL/login/pass/poll/$BSID"); [[ "$(jq -r .status <<<"$POLL")" != pending ]] && break; sleep 1; done
[[ "$(jq -r .status <<<"$POLL")" == done ]] && ok "the pass is verified (Belfry's issuer, not revoked)" || die "pass sign-in poll: $POLL"
DONE_URL="$ONB_URL$(jq -r .redirect <<<"$POLL")"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' "$DONE_URL")" == 400 ]] && ok "another browser cannot finish the sign-in" || bad "pass sign-in finished in another browser"
R=$(pj -o /dev/null -w '%{http_code} %{redirect_url}' "$DONE_URL")
[[ "$R" == "303 $ONB_URL/me" ]] && grep -q belfry_session "$PJAR" && ok "signed in with the pass, no password, on to /me" || bad "pass sign-in: '$R'"
[[ "$(pj "$ONB_URL/me")" == *'id="qr"'* ]] && ok "the pass's session opens /me" || bad "/me after the pass sign-in"
rm -f "$PJAR"

step "10a. derived keys: the participant's key comes from the custody seed and comes back after loss"
# Needs the seed unlocked (locally secrets/custody, written by gen-secrets.sh; on a deployment
# /run/belfry, scripts/custody-seed.sh unlock).
DC=(docker compose -f "$INFRA_DIR/docker-compose.yml" --profile app)
if "${DC[@]}" exec -T signer test -s /run/belfry/custody-seed 2>/dev/null; then
  KEYV=$(ox res.partner read "[[$PARTNER_ID]]" '{"fields":["belfry_key_version"]}' | jq -r '.[0].belfry_key_version // 0')
  SEEDPUB=$("${DC[@]}" exec -T signer belfry-signer derive "odoo-partner-$PARTNER_ID" "$KEYV" 2>/dev/null | tr -d '\r')
  [[ "$SEEDPUB" == "$MEMBER_PUB" ]] && ok "the seed gives the participant's key again (odoo-partner-$PARTNER_ID, version $KEYV)" || bad "the seed gives '$SEEDPUB', the pass has $MEMBER_PUB"
  REST=$("${DC[@]}" exec -T onboarding python -m belfry_onboarding restore-keys 2>/dev/null | tail -n1)
  [[ "$(jq -r --arg m "$MEMBER" '.partners[$m] // empty' <<<"$REST")" == ok ]] && ok "restore-keys: $MEMBER ok (the signer's account matches Odoo)" || bad "restore-keys: $REST"
else
  echo "  note the custody seed is locked or not configured: derived keys not checked"
fi

step "10b. self-custody: the participant moves to a key of their own"
# Irreversible for the smoke participant, so it runs after everything that signs with the managed
# key. The move needs a fresh sign-in: the new sign-in link from 6e. The new key and its
# signature are made in the onboarding container (coincurve).
# SMOKE_KEEP_PASS skips it: the kept participant is for browser checks with "Sign in with Open
# Source Village", which a self-custodial account no longer has.
if [[ -n "${SMOKE_KEEP_PASS:-}" ]]; then
  echo "  note SMOKE_KEEP_PASS set: self-custody not tested (the participant stays managed for browser checks)"
else
  FJAR=$(mktemp)
  curl -sS -b "$FJAR" -c "$FJAR" -o /dev/null -X POST "$SETUP_URL2"
  NSEC2=$(curl -sS -b "$FJAR" -X POST "$ONB_URL/account/export" | grep -o 'nsec1[a-z0-9]*' | head -n1)
  [[ -n "$NSEC2" ]] && ok "after a fresh sign-in the key can be exported" || bad "no key export after a fresh sign-in"
  CHALLENGE=$(curl -sS -b "$FJAR" "$ONB_URL/account/self-custody/challenge" | jq -r '.challenge // empty')
  MOVE=$("${DC[@]}" exec -T onboarding python - "$CHALLENGE" <<'PY'
import json, sys, time
from coincurve import PrivateKey
from belfry_onboarding.blossom import event_id
key = PrivateKey()
ev = {"pubkey": key.public_key_xonly.format().hex(), "created_at": int(time.time()), "kind": 27235,
      "tags": [["challenge", sys.argv[1]]], "content": ""}
ev["id"] = event_id(ev)
ev["sig"] = key.sign_schnorr(bytes.fromhex(ev["id"])).hex()
print(json.dumps({"event": ev}))
PY
)
  NEW_PUB=$(jq -r .event.pubkey <<<"$MOVE")
  R=$(curl -sS -b "$FJAR" -X POST "$ONB_URL/account/self-custody" -H 'Content-Type: application/json' -d "$MOVE")
  [[ "$(jq -r .outcome <<<"$R")" == moved_to_own_key && "$(jq -r .pubkey <<<"$R")" == "$NEW_PUB" ]] && ok "moved to a key of the participant's own" || bad "self-custody: $R"
  PASS_KEY=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["nostr_pubkey"]}' | jq -r '.[0].nostr_pubkey')
  [[ "$PASS_KEY" == "$NEW_PUB" ]] && ok "the pass carries the new key (relay whitelist follows)" || bad "pass key after the move: $PASS_KEY"
  [[ "$(ox res.partner read "[[$PARTNER_ID]]" '{"fields":["belfry_key_custody"]}' | jq -r '.[0].belfry_key_custody')" == self ]] && ok "the partner holds their own key (custody self)" || bad "custody after the move"
  app_connect "$FJAR" belfry-chat "$CHAT_CB" none
  [[ "$(qparam "$CONNECT_LOC" error_description)" == self_custody ]] && ok "apps are told to use the participant's own signer (access_denied, self_custody)" || bad "authorize after the move: '$CONNECT_LOC'"
  [[ "$(curl -sS -b "$FJAR" "$ONB_URL/account")" == *"You hold this key yourself"* ]] && ok "the account page says the participant holds the key" || bad "account page after the move"
  rm -f "$FJAR"
  MEMBER_PUB=$NEW_PUB
  NPUB=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["npub"]}' | jq -r '.[0].npub')  # step 7's baseline
fi

step "10c. joining with an own Nostr key: a signed proof on the invite page, a keyless signer account"
# A second participant who already uses Nostr. The key and its signature (the NIP-98 event the
# invite page's script asks the browser's signer for) are made in the onboarding container.
# SMOKE_KEEP_PASS keeps this participant active; SAVE_OWN_KEY=<file> then writes OWN_SECRET= and
# OWN_TOKEN= (the pass's tokens) for infra/scripts/wallet-own-signer-check.mjs.
OWN_INVITE=$(ox belfry.invite create '[{"note":"smoke own key"}]')
OWN_TOKEN=$(ox belfry.invite read "[[$OWN_INVITE]]" '{"fields":["token"]}' | jq -r '.[0].token')
OWN=$("${DC[@]}" exec -T onboarding python - "/belfry/invite/$OWN_TOKEN" <<'PY'
import json, sys, time
from coincurve import PrivateKey
from belfry_onboarding.blossom import event_id
key = PrivateKey()
pub = key.public_key_xonly.format().hex()
ev = {"pubkey": pub, "created_at": int(time.time()), "kind": 27235, "content": "Join Open Source Village with this key",
      "tags": [["u", "https://smoke.example" + sys.argv[1]], ["method", "POST"]]}
ev["id"] = event_id(ev)
ev["sig"] = key.sign_schnorr(bytes.fromhex(ev["id"])).hex()
bad = dict(ev, sig=PrivateKey().sign_schnorr(bytes.fromhex(ev["id"])).hex())
print(json.dumps({"pubkey": pub, "secret": key.secret.hex(), "proof": json.dumps(ev), "bad": json.dumps(bad)}))
PY
)
OWN_PUB=$(jq -r .pubkey <<<"$OWN")
OWN_BODY=$(mktemp)
own_post() { # proof -> http code; the page's body into $OWN_BODY
  local jar; jar=$(mktemp)
  local csrf; csrf=$(curl -sS -c "$jar" -b "$jar" "$ODOO_URL/belfry/invite/$OWN_TOKEN" | sed -n 's/.*name="csrf_token"[^>]*value="\([^"]*\)".*/\1/p' | head -n1)
  curl -sS -b "$jar" -o "$OWN_BODY" -w '%{http_code}' -X POST "$ODOO_URL/belfry/invite/$OWN_TOKEN" \
    --data-urlencode "csrf_token=$csrf" --data-urlencode "name=Smoke Own Key $STAMP" \
    --data-urlencode "email=smoke-own-$STAMP@belfry.local" --data-urlencode "product_id=$PRODUCT_ID" \
    --data-urlencode "own_key=1" --data-urlencode "nostr_pubkey=$OWN_PUB" --data-urlencode "nostr_proof=$1"
  rm -f "$jar"
}
CODE=$(own_post "$(jq -r .bad <<<"$OWN")")
[[ "$CODE" == 200 ]] && grep -q 'does not match the key' "$OWN_BODY" && ok "a signature by another key is refused on the invite page" || bad "forged proof -> $CODE"
CODE=$(own_post "$(jq -r .proof <<<"$OWN")")
[[ "$CODE" =~ ^30[1-8]$ ]] && ok "with the proof -> $CODE (payment link)" || { head -c 400 "$OWN_BODY" >&2; bad "own key POST -> $CODE"; }
rm -f "$OWN_BODY"
OWN_PASS=$(ox belfry.pass search_read "[[[\"invite_id\",\"=\",$OWN_INVITE]]]" '{"fields":["id","invoice_id","partner_id","own_pubkey"],"limit":1}' | jq -c '.[0] // empty')
OWN_PASS_ID=$(jq -r .id <<<"$OWN_PASS"); OWN_INV=$(jq -r '.invoice_id[0]' <<<"$OWN_PASS"); OWN_PARTNER=$(jq -r '.partner_id[0]' <<<"$OWN_PASS")
[[ "$(jq -r .own_pubkey <<<"$OWN_PASS")" == "$OWN_PUB" ]] && ok "pass $OWN_PASS_ID pending with the own key" || bad "own key on the pass: $OWN_PASS"
OWN_INV_ROW=$(ox account.move read "[[$OWN_INV]]" '{"fields":["amount_total","currency_id","partner_id"]}' | jq -c '.[0]')
ox payment.transaction create "[{\"provider_id\":$PROVIDER_ID,${PM_ID:+\"payment_method_id\":$PM_ID,}\"reference\":\"SMOKE-OWN-$STAMP\",\"amount\":$(jq -r .amount_total <<<"$OWN_INV_ROW"),\"currency_id\":$(jq -r '.currency_id[0]' <<<"$OWN_INV_ROW"),\"partner_id\":$(jq -r '.partner_id[0]' <<<"$OWN_INV_ROW"),\"operation\":\"online_direct\",\"invoice_ids\":[[6,0,[$OWN_INV]]],\"provider_reference\":\"pi_smoke_own_$STAMP\",\"state\":\"done\"}]" >/dev/null
WIZ=$(ox account.payment.register create '[{}]' "{\"context\":{\"active_model\":\"account.move\",\"active_ids\":[$OWN_INV]}}")
ox account.payment.register action_create_payments "[[$WIZ]]" >/dev/null
for i in $(seq 1 30); do
  OWN_ROW=$(ox belfry.pass read "[[$OWN_PASS_ID]]" '{"fields":["state","nostr_pubkey","npub"]}' | jq -c '.[0]')
  [[ "$(jq -r .state <<<"$OWN_ROW")" == active ]] && break
  [[ $i == 20 ]] && onb_post "/hooks/odoo/invoice-paid?token=$HOOK_TOKEN" "{\"_model\":\"account.move\",\"_id\":$OWN_INV,\"_action\":\"smoke\"}" >/dev/null || true
  sleep 2
done
[[ "$(jq -r '.state + " " + .nostr_pubkey' <<<"$OWN_ROW")" == "active $OWN_PUB" ]] && ok "active with the participant's own key" || bad "own-key pass: $OWN_ROW"
[[ "$(ox res.partner read "[[$OWN_PARTNER]]" '{"fields":["belfry_key_custody"]}' | jq -r '.[0].belfry_key_custody')" == self ]] && ok "partner holds their own key (custody self)" || bad "partner custody not self"
OWN_ACC=$("${DC[@]}" exec -T onboarding python -c '
import json, sys
from belfry_onboarding.app import build_services
from belfry_onboarding.settings import Settings
print(json.dumps(build_services(Settings()).signer.account(sys.argv[1])))' "odoo-partner-$OWN_PARTNER" 2>/dev/null | tail -n1)
[[ "$(jq -r '.custody + " " + .pubkey' <<<"$OWN_ACC" 2>/dev/null)" == "own $OWN_PUB" ]] && ok "signer account without a key of Belfry's" || bad "signer account for the own key: $OWN_ACC"
"${DC[@]}" exec -T relay cat /rnostr/config/rnostr.toml | grep -q "$OWN_PUB" && ok "own key on the relay whitelist" || bad "own key not whitelisted"
if [[ -n "${SMOKE_KEEP_PASS:-}" ]]; then
  if [[ -n "${SAVE_OWN_KEY:-}" ]]; then
    (umask 077; printf 'OWN_SECRET=%s\nOWN_TOKEN=%s\n' "$(jq -r .secret <<<"$OWN")" \
      "$(ox belfry.pass read "[[$OWN_PASS_ID]]" '{"fields":["cashu_token"]}' | jq -r '.[0].cashu_token // ""')" > "$SAVE_OWN_KEY")
    echo "  note own-key participant kept active; key and tokens in $SAVE_OWN_KEY"
  fi
else
  ox belfry.pass action_revoke "[[$OWN_PASS_ID]]" >/dev/null 2>&1 || onb_post "/admin/passes/$OWN_PASS_ID/revoke" '{"reason":"smoke cleanup"}' >/dev/null || true
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
[[ "$(groups_member "$MEMBER_PUB")" == no ]] && ok "a revoked pass leaves the groups relay" || bad "revoked participant still a member of the groups relay"
CHECK=$(curl -sS -H 'Accept: application/json' "$ONB_URL/check/$(qr_token "$PASS_ID")")
[[ "$(jq -r .valid <<<"$CHECK")" == false && "$(jq -r .reason <<<"$CHECK")" == *revoked* ]] && ok "/check: the phone pass of a revoked pass is refused at once" || bad "/check after revoke: $CHECK"
[[ "$(onb_post "/admin/passes/$PASS_ID/login-link?token=$HOOK_TOKEN" | jq -r .result)" == not_active ]] && ok "no login link for a revoked pass" || bad "login link after revoke"
if [[ -n "${APP_TOKEN:-}" ]]; then
  # The signer is suspended and the pass is gone: a fresh Blossom token is refused twice over.
  BAUTH2=$(blossom_auth upload "$(printf 'again' | sha256sum | cut -c1-64)")
  if [[ -z "$BAUTH2" || "$BAUTH2" == bnVsbA== ]]; then ok "revoked key can no longer sign a Blossom auth event (signer suspended, connections ended)"; else
    UP2=$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BLOSSOM_URL/upload" -H "Authorization: Nostr $BAUTH2" -H 'Content-Type: text/plain' --data-binary 'again')
    [[ "$UP2" == 403 ]] && ok "upload with a revoked pass -> 403 (gate)" || bad "upload after revoke -> $UP2"; fi
fi

fi

step "7. replay the webhook three times and check nothing was duplicated"
for i in 1 2 3; do
  echo "  replay $i -> $(onb_post "/hooks/odoo/invoice-paid?token=$HOOK_TOKEN" "{\"_model\":\"account.move\",\"_id\":$INV_ID,\"_action\":\"smoke-replay\"}" | jq -r .result)"
done
sleep 2
PASS2=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","npub","nostr_pubkey"]}' | jq -c '.[0]')
[[ "$(jq -r .npub <<<"$PASS2")" == "$NPUB" && "$(jq -r .nostr_pubkey <<<"$PASS2")" == "$MEMBER_PUB" ]] && ok "pass unchanged after replay" || bad "pass changed: $PASS2"
[[ "$(ox belfry.pass search_count "[[[\"invoice_id\",\"=\",$INV_ID]]]")" == 1 ]] && ok "one pass for the invoice" || bad "more than one pass for invoice $INV_ID"
SIGNER_ACC2=$("${COMPOSE[@]}" exec -T onboarding python -c '
import json, sys
from belfry_onboarding.app import build_services
from belfry_onboarding.settings import Settings
print(json.dumps(build_services(Settings()).signer.account(sys.argv[1])))' "odoo-partner-$PARTNER_ID" 2>/dev/null | tail -n1)
[[ "$(jq -r .pubkey <<<"$SIGNER_ACC2" 2>/dev/null)" == "$MEMBER_PUB" ]] && ok "same key at the signer" || bad "signer account changed: $SIGNER_ACC2"

if [[ -n "${ACCOUNTING_ODOO_URL:-}" ]]; then
  step "12. a full refund becomes a credit note at the ticket seller"
  REF_TX=$(ox payment.transaction create "[{\"provider_id\":$PROVIDER_ID,${PM_ID:+\"payment_method_id\":$PM_ID,}\"reference\":\"SMOKE-$STAMP-R\",\"amount\":-$(jq -r .amount_total <<<"$INV"),\"currency_id\":$(jq -r '.currency_id[0]' <<<"$INV"),\"partner_id\":$(jq -r '.partner_id[0]' <<<"$INV"),\"operation\":\"refund\",\"source_transaction_id\":$TX_ID,\"provider_reference\":\"re_smoke_$STAMP\",\"state\":\"done\"}]")
  R=$(onb_post "/hooks/odoo/refund?token=$HOOK_TOKEN" "{\"_model\":\"payment.transaction\",\"_id\":$REF_TX,\"_action\":\"smoke\"}" | jq -r .result)
  ACC=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","accounting_state","accounting_refund_ref","accounting_error"]}' | jq -c '.[0]')
  [[ "$(jq -r .accounting_state <<<"$ACC")" == refunded ]] && ok "refund ($R): credit note $(jq -r .accounting_refund_ref <<<"$ACC") at ${ACCOUNTING_ISSUER:-the ticket seller}" || bad "refund at the seller: $ACC"
fi

printf '\n%d ok, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
