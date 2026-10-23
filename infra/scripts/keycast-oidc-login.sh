#!/usr/bin/env bash
# End-to-end check of the Keycast fork's Authentik login, driven with curl like a browser:
#
#   app -> Keycast /api/oauth/authorize (popup shows "Sign in with Belfry")
#       -> /api/auth/oidc/start -> Authentik login (flow executor API) -> Authentik authorize
#       -> Keycast /api/auth/oidc/callback (session cookie) -> back to /api/oauth/authorize
#       -> consent -> code -> /api/oauth/token -> sign a kind-1 event with the participant's key
#
# Usage: keycast-oidc-login.sh <authentik username> <authentik password> <expected pubkey hex>
# Prints one line per step; exits non-zero at the first failure. Needs curl, jq, openssl.
set -euo pipefail

USERNAME=${1:?authentik username}
PASSWORD=${2:?authentik password}
EXPECT_PUB=${3:?expected Keycast pubkey (hex)}
KC_URL=${KC_URL:-http://localhost:3000}
AK_URL=${AK_URL:-http://localhost:9000}
CLIENT_ID=${CLIENT_ID:-belfry-oidc-check}
REDIRECT_URI=${REDIRECT_URI:-http://localhost:3099/callback}
SCOPE=${SCOPE:-policy:belfry-event}

say()  { echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
urlenc() { jq -rn --arg v "$1" '$v|@uri'; }
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

JAR=$(mktemp -d); trap 'rm -rf "$JAR"' EXIT
KC_JAR="$JAR/keycast"; AK_JAR="$JAR/authentik"
kc() { curl -sS -b "$KC_JAR" -c "$KC_JAR" "$@"; }
akc() { curl -sS -b "$AK_JAR" -c "$AK_JAR" "$@"; }

VERIFIER=$(openssl rand 32 | b64url)
CHALLENGE=$(printf '%s' "$VERIFIER" | openssl dgst -sha256 -binary | b64url)
APP_STATE=$(openssl rand -hex 8)
AUTHZ="/api/oauth/authorize?client_id=$CLIENT_ID&redirect_uri=$(urlenc "$REDIRECT_URI")&scope=$(urlenc "$SCOPE")&code_challenge=$CHALLENGE&code_challenge_method=S256&state=$APP_STATE"

# 1. The popup offers the Authentik login.
kc "$KC_URL$AUTHZ" | grep -q 'Sign in with ' && say "authorize page offers 'Sign in with …'" || fail "no OIDC button on $KC_URL$AUTHZ"

# 2. Start: Keycast sets the signed flow cookie and sends us to Authentik.
LOC=$(kc -o /dev/null -w '%{redirect_url}' "$KC_URL/api/auth/oidc/start?return_to=$(urlenc "$AUTHZ")")
[[ "$LOC" == "$AK_URL/application/o/authorize/?"* ]] || fail "start redirected to '$LOC'"
grep -q keycast_oidc "$KC_JAR" || fail "no keycast_oidc cookie"
say "start -> Authentik authorize, flow cookie set"

# 3. Authentik login through the flow executor (what the login page does).
akc -o /dev/null "$LOC"   # unauthenticated: sets the session and points at the auth flow
NEXT=${LOC#"$AK_URL"}
EXEC="$AK_URL/api/v3/flows/executor/default-authentication-flow/?query=$(urlenc "next=$NEXT")"
CH=$(akc "$EXEC")
for _ in 1 2 3 4 5 6; do
  comp=$(jq -r '.component // empty' <<<"$CH")
  case "$comp" in
    ak-stage-identification)
      body=$(jq -nc --arg u "$USERNAME" --arg p "$PASSWORD" '{component:"ak-stage-identification",uid_field:$u,password:$p}') ;;
    ak-stage-password)
      body=$(jq -nc --arg p "$PASSWORD" '{component:"ak-stage-password",password:$p}') ;;
    xak-flow-redirect) break ;;
    *) fail "unexpected Authentik challenge: $(jq -c '{component, response_errors}' <<<"$CH")" ;;
  esac
  # Each POST answers 302 to the GET of the next challenge (no -X, so curl follows with GET).
  CH=$(akc -L -H 'Content-Type: application/json' -H "Referer: $AK_URL/" --data "$body" "$EXEC")
  errs=$(jq -c '.response_errors // empty' <<<"$CH")
  [[ -z "$errs" || "$errs" == "{}" ]] || fail "Authentik refused the login: $errs"
done
[[ "$(jq -r .component <<<"$CH")" == xak-flow-redirect ]] || fail "Authentik flow did not finish: $(jq -c . <<<"$CH")"
say "Authentik login as $USERNAME"

# 4. Authentik authorize (implicit consent) -> Keycast callback with a code.
CB=$(akc -o /dev/null -w '%{redirect_url}' "$LOC")
[[ "$CB" == "$KC_URL/api/auth/oidc/callback?"* ]] || fail "Authentik authorize redirected to '$CB' (participant not in belfry-participants?)"
say "Authentik authorize -> Keycast callback"

# 5. Callback: session cookie, back to the OAuth request.
read -r CODE_STATUS BACK < <(kc -o "$JAR/cb.html" -w '%{http_code} %{redirect_url}\n' "$CB")
[[ "$CODE_STATUS" == 303 && "$BACK" == "$KC_URL$AUTHZ" ]] || fail "callback answered $CODE_STATUS -> '$BACK': $(sed 's/<[^>]*>/ /g' "$JAR/cb.html" | tr -s ' ' | head -c 300)"
grep -q keycast_session "$KC_JAR" || fail "no keycast_session cookie after the callback"
say "callback -> session cookie, back to /api/oauth/authorize"

# 6. Consent, or an immediate redirect with a code when this app is already approved for the
#    participant (Keycast remembers authorizations per origin and policy).
read -r AZ_STATUS AZ_LOC < <(kc -o "$JAR/authz.html" -w '%{http_code} %{redirect_url}\n' "$KC_URL$AUTHZ")
if [[ "$AZ_LOC" == "$REDIRECT_URI?code="* ]]; then
  CODE=$(sed -E 's/.*[?&]code=([^&]+).*/\1/' <<<"$AZ_LOC")
  say "app already approved: Keycast redirected with a code"
else
  [[ "$AZ_STATUS" == 200 ]] && grep -qi 'authorize' "$JAR/authz.html" && say "consent screen shown" \
    || fail "no consent screen after login (HTTP $AZ_STATUS)"
  APPROVE=$(jq -nc --arg c "$CLIENT_ID" --arg r "$REDIRECT_URI" --arg s "$SCOPE" --arg st "$APP_STATE" --arg ch "$CHALLENGE" \
    '{client_id:$c, redirect_uri:$r, scope:$s, state:$st, approved:true, code_challenge:$ch, code_challenge_method:"S256"}')
  APPROVED=$(kc -X POST "$KC_URL/api/oauth/authorize" -H 'Content-Type: application/json' -d "$APPROVE")
  CODE=$(jq -r '.code // empty' <<<"$APPROVED" 2>/dev/null || true)
  [[ -n "$CODE" ]] || fail "approve returned no code: $(head -c 300 <<<"$APPROVED")"
fi
TOKEN_RESP=$(curl -sS -X POST "$KC_URL/api/oauth/token" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg c "$CODE" --arg id "$CLIENT_ID" --arg r "$REDIRECT_URI" --arg v "$VERIFIER" \
        '{grant_type:"authorization_code", code:$c, client_id:$id, redirect_uri:$r, code_verifier:$v}')")
TOKEN=$(jq -r '.access_token // empty' <<<"$TOKEN_RESP" 2>/dev/null || true)
[[ -n "$TOKEN" ]] || fail "token exchange returned no access_token: $(head -c 300 <<<"$TOKEN_RESP")"
say "code -> access token"
# PRINT_TOKEN=1: hand the access token to the caller (the smoke test signs a Blossom auth event with it).
[[ "${PRINT_TOKEN:-}" == 1 ]] && echo "ACCESS_TOKEN=$TOKEN"

# 7. Sign with the participant's key.
EVENT=$(jq -nc --arg pk "$EXPECT_PUB" --argjson t "$(date +%s)" \
  '{pubkey:$pk, created_at:$t, kind:1, tags:[], content:"hello from the Keycast OIDC check"}')
SIGNED=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -nc --argjson e "$EVENT" '{method:"sign_event", params:[$e]}')")
[[ "$(jq -r '.result.pubkey // empty' <<<"$SIGNED")" == "$EXPECT_PUB" && -n "$(jq -r '.result.sig // empty' <<<"$SIGNED")" ]] \
  && say "kind-1 signed by $EXPECT_PUB" || fail "sign_event: $SIGNED"
