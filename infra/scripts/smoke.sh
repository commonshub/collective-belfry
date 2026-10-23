#!/usr/bin/env bash
# Smoke test for slice 1 (docs/slice-1-onboarding.md, "Infra"): invite -> pay -> accounts
# created exactly once, replay-safe.
#
# STATUS (2026-10-23): ran green end to end against the full stack (slice 1, relay, Keycast
# login, login setup links). The Authentik
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
ODOO_URL=${ODOO_URL:-http://localhost:8069}
AK_URL=${AK_URL:-http://localhost:9000}
KC_URL=${KC_URL:-http://localhost:3000}
ONB_URL=${ONB_URL:-http://localhost:8100}
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
UID_=$(rpc common authenticate "[\"$ODOO_DB\",\"$ODOO_ONBOARDING_LOGIN\",\"$ODOO_API_KEY\",{}]" | jq -r '.result')
[[ "$UID_" =~ ^[0-9]+$ ]] || die "cannot authenticate to Odoo with ODOO_API_KEY"
ox() { # model method args_json [kwargs_json]
  local out; out=$(rpc object execute_kw "[\"$ODOO_DB\",$UID_,\"$ODOO_API_KEY\",\"$1\",\"$2\",$3,${4:-{\}}]")
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
  curl -sS -o /dev/null -X POST "$ONB_URL/hooks/odoo/invoice-paid?token=$HOOK_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"_model\":\"account.move\",\"_id\":$INV_ID,\"_action\":\"smoke\"}" || true
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
SYNC=$("${COMPOSE[@]}" exec -T onboarding python -m belfry_onboarding sync-relay 2>/dev/null || true)
[[ "$(jq -r '.changed' <<<"$SYNC" 2>/dev/null)" == false ]] && ok "sync-relay CLI: whitelist already up to date ($(jq -r .pubkeys <<<"$SYNC") pubkeys)" || bad "sync-relay CLI: $SYNC"

step "6c. login setup link: mailed by Odoo, sets the password through Authentik's flow"
# Participants have no Authentik password: activation stores a one-time account-setup link
# (authentik recovery flow belfry-account-setup) on the pass and Odoo mails it.
PASS_LOGIN=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["login_setup_url","login_setup_expires_at","login_mail_sent_at"]}' | jq -c '.[0]')
SETUP_URL=$(jq -r '.login_setup_url // empty' <<<"$PASS_LOGIN")
[[ "$SETUP_URL" == "${AUTHENTIK_PUBLIC_URL:-http://localhost:9000}/if/flow/belfry-account-setup/?flow_token="* ]] \
  && ok "pass carries a setup link on the public Authentik URL (valid until $(jq -r .login_setup_expires_at <<<"$PASS_LOGIN"))" \
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

# Drive the setup flow like the browser: prompt (password) -> user write -> login (remember me).
AK_PW="smoke-$(openssl rand -hex 12)"
FLOW_TOKEN=${SETUP_URL#*flow_token=}
SETUP_EXEC="$AK_URL/api/v3/flows/executor/belfry-account-setup/?query=flow_token%3D$FLOW_TOKEN"
run_setup_flow() { # password -> prints the final component; the session jar is $1's caller's
  local jar=$1 pw=$2 ch comp
  ch=$(curl -sS -b "$jar" -c "$jar" "$SETUP_EXEC")
  for _ in 1 2 3 4; do
    comp=$(jq -r '.component // empty' <<<"$ch")
    case "$comp" in
      ak-stage-prompt) body=$(jq -nc --arg p "$pw" '{component:"ak-stage-prompt",password:$p,password_repeat:$p}') ;;
      ak-stage-user-login) body='{"component":"ak-stage-user-login","remember_me":false}' ;;
      *) break ;;
    esac
    ch=$(curl -sS -b "$jar" -c "$jar" -L -H 'Content-Type: application/json' -H "Referer: $AK_URL/" --data "$body" "$SETUP_EXEC")
  done
  jq -r '.component // "none"' <<<"$ch"
}
SJAR=$(mktemp)
FINAL=$(run_setup_flow "$SJAR" "$AK_PW")
ME=$(curl -sS -b "$SJAR" "$AK_URL/api/v3/core/users/me/" | jq -r '.user.pk // empty')
[[ "$FINAL" == xak-flow-redirect && "$ME" == "$AK_UID" ]] && ok "setup link: password chosen, browser signed in as Authentik user $AK_UID" || bad "setup flow ended with '$FINAL', session user '$ME'"
rm -f "$SJAR"
# The link is one-time: a second use must not be able to set a password.
SJAR=$(mktemp)
FINAL=$(run_setup_flow "$SJAR" "evil-$(openssl rand -hex 8)")
[[ "$FINAL" == ak-stage-access-denied ]] && ok "consumed link is refused (access denied, no pending user)" || bad "consumed link ended with '$FINAL'"
rm -f "$SJAR"

step "6d. Keycast sign-in through Authentik (OIDC) with the chosen password"
if OUT=$(KC_URL="$KC_URL" AK_URL="$AK_URL" "$INFRA_DIR/scripts/keycast-oidc-login.sh" "$USERNAME" "$AK_PW" "$KC_PUB" 2>&1); then
  sed 's/^/  /' <<<"$OUT"; ok "Authentik login opens a Keycast session for $KC_PUB and signs"
else
  sed 's/^/  /' <<<"$OUT" >&2; bad "Keycast OIDC login"
fi
CB_STATUS=$(curl -sS -o /dev/null -w '%{http_code}' "$KC_URL/api/auth/oidc/callback?code=x&state=y")
[[ "$CB_STATUS" == 400 ]] && ok "callback without the flow cookie -> 400" || bad "callback without the flow cookie -> $CB_STATUS"

step "6e. steward asks for a new login link (admin endpoint behind the Odoo button)"
RESEND=$(curl -sS -X POST "$ONB_URL/admin/passes/$PASS_ID/login-link?token=$HOOK_TOKEN")
[[ "$(jq -r .result <<<"$RESEND")" == sent ]] && ok "POST /admin/passes/<id>/login-link -> sent" || bad "login-link endpoint: $RESEND"
SETUP_URL2=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["login_setup_url"]}' | jq -r '.[0].login_setup_url')
[[ -n "$SETUP_URL2" && "$SETUP_URL2" != "$SETUP_URL" ]] && ok "pass carries a new link" || bad "link unchanged: $SETUP_URL2"
MAIL_N2=$(ox mail.mail search_count "[[[\"model\",\"=\",\"belfry.pass\"],[\"res_id\",\"=\",$PASS_ID]]]")
[[ "$MAIL_N2" == 2 ]] && ok "second mail queued" || bad "mails for the pass: $MAIL_N2"
RESEND=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$ONB_URL/admin/passes/$PASS_ID/login-link?token=wrong")
[[ "$RESEND" == 401 ]] && ok "wrong token -> 401" || bad "wrong token -> $RESEND"

step "7. replay the webhook three times and check nothing was duplicated"
for i in 1 2 3; do
  curl -sS -o /dev/null -w "  replay $i -> %{http_code}\n" -X POST "$ONB_URL/hooks/odoo/invoice-paid?token=$HOOK_TOKEN" \
    -H 'Content-Type: application/json' -d "{\"_model\":\"account.move\",\"_id\":$INV_ID,\"_action\":\"smoke-replay\"}"
done
sleep 2
PASS2=$(ox belfry.pass read "[[$PASS_ID]]" '{"fields":["state","npub","keycast_pubkey","authentik_user_id"]}' | jq -c '.[0]')
[[ "$(jq -r .npub <<<"$PASS2")" == "$NPUB" && "$(jq -r .authentik_user_id <<<"$PASS2")" == "$AK_UID" ]] && ok "pass unchanged after replay" || bad "pass changed: $PASS2"
[[ "$(ox belfry.pass search_count "[[[\"invoice_id\",\"=\",$INV_ID]]]")" == 1 ]] && ok "one pass for the invoice" || bad "more than one pass for invoice $INV_ID"
[[ "$(ak "$AK_URL/api/v3/core/users/?username=$USERNAME" | jq '.pagination.count')" == 1 ]] && ok "still one Authentik user" || bad "Authentik user count changed"
[[ "$(ak "$AK_URL/api/v3/sources/user_connections/oauth/?user=$AK_UID&source__slug=pass-bridge" | jq '.results | length')" == "$LINKS_BEFORE" ]] && ok "source link count unchanged ($LINKS_BEFORE)" || bad "source link count changed"
[[ "$(curl -sS "$KC_URL/.well-known/nostr.json?name=$USERNAME" | jq -r --arg n "$USERNAME" '.names[$n]')" == "$KC_PUB" ]] && ok "same Keycast pubkey" || bad "Keycast pubkey changed"

printf '\n%d ok, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
