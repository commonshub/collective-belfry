#!/usr/bin/env bash
# End-to-end check of the Keycast fork's Authentik login, driven with curl like a browser:
#
#   app -> Keycast /api/oauth/authorize (popup shows "Sign in with Belfry")
#       -> /api/auth/oidc/start -> Authentik authorize with an existing Authentik session
#       -> Keycast /api/auth/oidc/callback (session cookie) -> back to /api/oauth/authorize
#       -> consent -> code -> /api/oauth/token -> sign a kind-1 event with the participant's key
#
# Usage: keycast-oidc-login.sh <authentik session cookie jar> <expected pubkey hex>
#   EXPECT_EMAIL / EXPECT_MEMBER: also check the account e-mail and the pass card endpoint;
#   SAVE_KC_JAR=<file>: keep the Keycast session cookies (screenshots).
# Participants have no password (magic link, blueprint belfry-magic-link-flow), so the caller
# signs in first (the smoke test: the one-time sign-in link of the activation mail) and hands
# over that browser's cookie jar; it is copied, not changed.
# Prints one line per step; exits non-zero at the first failure. Needs curl, jq, openssl.
set -euo pipefail

SESSION_JAR=${1:?cookie jar of a signed-in Authentik session}
EXPECT_PUB=${2:?expected Keycast pubkey (hex)}
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
[[ -s "$SESSION_JAR" ]] || fail "no cookie jar at $SESSION_JAR"
cp "$SESSION_JAR" "$AK_JAR"
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

# 3. The Authentik session belongs to the participant (signed in before, no password involved).
WHO=$(akc "$AK_URL/api/v3/core/users/me/" | jq -r '.user.username // empty')
[[ -n "$WHO" ]] || fail "the cookie jar holds no Authentik session"
say "Authentik session of $WHO"

# 4. Authentik authorize (implicit consent) -> Keycast callback with a code.
CB=$(akc -o /dev/null -w '%{redirect_url}' "$LOC")
[[ "$CB" == "$KC_URL/api/auth/oidc/callback?"* ]] || fail "Authentik authorize redirected to '$CB' (participant not in belfry-participants?)"
say "Authentik authorize -> Keycast callback"

# 5. Callback: session cookie, back to the OAuth request.
read -r CODE_STATUS BACK < <(kc -o "$JAR/cb.html" -w '%{http_code} %{redirect_url}\n' "$CB")
[[ "$CODE_STATUS" == 303 && "$BACK" == "$KC_URL$AUTHZ" ]] || fail "callback answered $CODE_STATUS -> '$BACK': $(sed 's/<[^>]*>/ /g' "$JAR/cb.html" | tr -s ' ' | head -c 300)"
grep -q keycast_session "$KC_JAR" || fail "no keycast_session cookie after the callback"
say "callback -> session cookie, back to /api/oauth/authorize"

# 5b. Belfry fork, patch 0005: the account has no password, so the fresh OIDC session alone
#     unlocks the key export (within OIDC_REAUTH_WINDOW_SECS) and the account status says so.
ACCT=$(kc "$KC_URL/api/user/account")
[[ "$(jq -r '.has_password' <<<"$ACCT")" == false ]] && say "account status: has_password=false (OIDC-only account)" || fail "account status: $(head -c 200 <<<"$ACCT")"
EXPORT=$(kc -X POST "$KC_URL/api/user/export-key" -H 'Content-Type: application/json' -d '{}')
[[ "$(jq -r '.key // empty' <<<"$EXPORT")" == nsec1* ]] && say "key export with the fresh session returns the nsec (not printed)" || fail "export-key: $(head -c 200 <<<"$EXPORT")"

# 5c. Belfry fork, patch 0006: the ID token's e-mail is stored verified (EXPECT_EMAIL), and the
#     dashboard's "Your pass" card reads the pass from the onboarding service (EXPECT_MEMBER).
if [[ -n "${EXPECT_EMAIL:-}" ]]; then
  [[ "$(jq -r '.email // empty' <<<"$ACCT")" == "$EXPECT_EMAIL" && "$(jq -r '.email_verified' <<<"$ACCT")" == true ]] \
    && say "account e-mail $EXPECT_EMAIL taken from the ID token, verified" \
    || fail "account e-mail after the OIDC login: $(jq -c '{email, email_verified}' <<<"$ACCT")"
fi
if [[ -n "${EXPECT_MEMBER:-}" ]]; then
  PASS=$(kc "$KC_URL/api/user/pass")
  [[ "$(jq -r '.member_number // empty' <<<"$PASS")" == "$EXPECT_MEMBER" && "$(jq -r '.state' <<<"$PASS")" == active ]] \
    && say "/api/user/pass: $(jq -r '.tier_name' <<<"$PASS") $EXPECT_MEMBER, active, claim link $(jq -r 'if .claim_url then "present" else "absent" end' <<<"$PASS")" \
    || fail "/api/user/pass: $(head -c 300 <<<"$PASS")"
fi
[[ -n "${SAVE_KC_JAR:-}" ]] && cp "$KC_JAR" "$SAVE_KC_JAR"

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
