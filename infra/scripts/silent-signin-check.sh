#!/usr/bin/env bash
# Silent sign-in and logout for Belfry's apps (Keycast patches 0008, 0010), driven with curl like a
# browser:
#
#   app -> Keycast /api/oauth/authorize?prompt=none
#       -> no Keycast session: /api/auth/oidc/start?prompt=none -> Authentik authorize prompt=none
#          -> Keycast callback (session, or oidc_failed) -> back to /api/oauth/authorize
#       -> app redirect URI with ?code=… (trusted app, no consent screen) or ?error=login_required
#   app logout -> POST /api/oauth/revoke, then Keycast /api/auth/oidc/logout -> Authentik
#       end-session (invalidation flow belfry-signer-logout) -> Keycast /logged-out -> the app
#
# Usage: silent-signin-check.sh <authentik session cookie jar> <expected pubkey hex>
#   CLIENT_ID / REDIRECT_URI: a trusted app (default belfry-chat at CHAT_PUBLIC_URL).
#   LOGOUT_ONLY=1 LOGOUT_JAR=<jar>: only the logout check (10), in that Authentik session, which
#   it ends; give it a session of its own (the smoke test: 6h's magic-link browser).
# The jar is copied, not changed. Prints one line per check; exits non-zero at the first failure.
# Needs curl, jq, openssl.
set -euo pipefail

SESSION_JAR=${1:?cookie jar of a signed-in Authentik session}
EXPECT_PUB=${2:?expected Keycast pubkey (hex)}
KC_URL=${KC_URL:-http://localhost:3000}
AK_URL=${AK_URL:-http://localhost:9000}
CLIENT_ID=${CLIENT_ID:-belfry-chat}
REDIRECT_URI=${REDIRECT_URI:-${CHAT_PUBLIC_URL:-http://localhost:3300}/oauth/callback}
SCOPE=policy:belfry-event

say()  { echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
urlenc() { jq -rn --arg v "$1" '$v|@uri'; }
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

JAR=$(mktemp -d); trap 'rm -rf "$JAR"' EXIT
[[ -s "$SESSION_JAR" ]] || fail "no cookie jar at $SESSION_JAR"

# authorize_url client redirect_uri state challenge [prompt]
authorize_url() {
  local u="$KC_URL/api/oauth/authorize?client_id=$(urlenc "$1")&redirect_uri=$(urlenc "$2")&scope=$(urlenc "$SCOPE")&state=$3&code_challenge=$4&code_challenge_method=S256"
  [[ -n "${5:-}" ]] && u+="&prompt=$5"
  echo "$u"
}

# follow <keycast jar> <authentik jar> <url>: follows redirects one by one (each host with its own
# jar), stops at the app's redirect URI. Prints "<final url> <hosts visited>"; HTTP_STATUS=<code>
# when a page was rendered instead.
follow() {
  local kj=$1 aj=$2 url=$3 hops="" status loc jar i
  for i in $(seq 1 12); do
    if [[ "$url" == "$AK_URL"* ]]; then jar=$aj; hops+="A"; else jar=$kj; hops+="K"; fi
    read -r status loc < <(curl -sS -b "$jar" -c "$jar" -o "$JAR/page" -w '%{http_code} %{redirect_url}\n' "$url")
    if [[ -z "$loc" ]]; then echo "HTTP_STATUS=$status $hops"; return; fi
    [[ "$loc" == "$REDIRECT_URI"* || "$loc" == "${OTHER_REDIRECT:-none}"* ]] && { echo "$loc $hops"; return; }
    url=$loc
  done
  echo "TOO_MANY_REDIRECTS $hops"
}
param() { sed -nE "s/.*[?&]$1=([^&]*).*/\1/p" <<<"$2"; }

VERIFIER=$(openssl rand 32 | b64url)
CHALLENGE=$(printf '%s' "$VERIFIER" | openssl dgst -sha256 -binary | b64url)

if [[ -z "${LOGOUT_ONLY:-}" ]]; then
# 1. A browser without any session: the silent request ends at the app with login_required,
#    after one round through Authentik, without rendering a page.
touch "$JAR/kc0" "$JAR/ak0"
read -r END HOPS < <(follow "$JAR/kc0" "$JAR/ak0" "$(authorize_url "$CLIENT_ID" "$REDIRECT_URI" s1 "$CHALLENGE" none)")
[[ "$END" == "$REDIRECT_URI?error=login_required&state=s1" && "$HOPS" == *A* ]] \
  && say "no session: Keycast asked Authentik silently ($HOPS), the app gets error=login_required" \
  || fail "no session ended at '$END' ($HOPS)"

# 2. The participant's browser (Authentik session, no Keycast session yet): a code, no page.
cp "$SESSION_JAR" "$JAR/ak1"; touch "$JAR/kc1"
read -r END HOPS < <(follow "$JAR/kc1" "$JAR/ak1" "$(authorize_url "$CLIENT_ID" "$REDIRECT_URI" s2 "$CHALLENGE" none)")
CODE=$(param code "$END")
[[ -n "$CODE" && "$(param state "$END")" == s2 && "$HOPS" == *A* ]] \
  && say "Authentik session only: silent sign-in through Authentik returns a code ($HOPS), no consent screen" \
  || fail "Authentik session ended at '$END' ($HOPS): $(sed 's/<[^>]*>/ /g' "$JAR/page" | tr -s ' ' | head -c 200)"
grep -q keycast_session "$JAR/kc1" && say "Keycast session cookie set on the way" || fail "no keycast_session after the silent login"

TOKEN_RESP=$(curl -sS -X POST "$KC_URL/api/oauth/token" -H 'Content-Type: application/json' -H "Origin: ${REDIRECT_URI%/oauth/callback}" \
  -D "$JAR/token.h" -d "$(jq -nc --arg c "$CODE" --arg id "$CLIENT_ID" --arg r "$REDIRECT_URI" --arg v "$VERIFIER" \
        '{grant_type:"authorization_code", code:$c, client_id:$id, redirect_uri:$r, code_verifier:$v}')")
BUNKER=$(jq -r '.bunker_url // empty' <<<"$TOKEN_RESP")
TOKEN=$(jq -r '.access_token // empty' <<<"$TOKEN_RESP")
[[ "$BUNKER" == bunker://* && -n "$TOKEN" ]] && say "code -> token with a bunker URL" || fail "token exchange: $(head -c 300 <<<"$TOKEN_RESP")"
grep -qi '^access-control-allow-origin:' "$JAR/token.h" && say "token endpoint answers the app's origin (CORS)" || fail "no CORS header on the token response"
SIGNED=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg pk "$EXPECT_PUB" --argjson t "$(date +%s)" '{method:"sign_event", params:[{pubkey:$pk, created_at:$t, kind:1, tags:[], content:"silent sign-in check"}]}')")
[[ "$(jq -r '.result.pubkey // empty' <<<"$SIGNED")" == "$EXPECT_PUB" ]] && say "the new authorization signs for $EXPECT_PUB" || fail "sign_event: $(head -c 300 <<<"$SIGNED")"

# 2b. Keycast patch 0011: derive_secret only for the wallet client. CLIENT_ID=belfry-wallet (the
#     smoke test's 6o, WALLET_ONLY=1): the derived secret is 32 bytes and stable; any other client
#     is refused.
derive() { curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg p "$1" '{method:"derive_secret", params:[$p]}')"; }
D1=$(derive cashu-wallet-v1)
if [[ "$CLIENT_ID" == belfry-wallet ]]; then
  D2=$(derive cashu-wallet-v1); N=$(derive cashu-nutzap-v1); X=$(derive something-else)
  [[ "$(jq -r '.result // empty' <<<"$D1")" =~ ^[0-9a-f]{64}$ && "$(jq -r .result <<<"$D1")" == "$(jq -r .result <<<"$D2")" ]] \
    && say "derive_secret: the wallet gets a stable 32-byte seed secret" || fail "derive_secret for the wallet: $(head -c 200 <<<"$D1")"
  [[ "$(jq -r '.result // empty' <<<"$N")" =~ ^[0-9a-f]{64}$ && "$(jq -r .result <<<"$N")" != "$(jq -r .result <<<"$D1")" ]] \
    && say "derive_secret: a separate nutzap key secret" || fail "derive_secret nutzap: $(head -c 200 <<<"$N")"
  [[ -z "$(jq -r '.result // empty' <<<"$X" 2>/dev/null)" ]] && say "derive_secret: a purpose not configured is refused" || fail "derive_secret other purpose: $X"
  [[ -n "${WALLET_ONLY:-}" ]] && exit 0
else
  [[ -z "$(jq -r '.result // empty' <<<"$D1" 2>/dev/null)" ]] && say "derive_secret refused for $CLIENT_ID (wallet only)" || fail "derive_secret for $CLIENT_ID: $D1"
fi

# 3. Same browser, Keycast session now present: a code straight away, Authentik not involved.
touch "$JAR/ak2"
read -r END HOPS < <(follow "$JAR/kc1" "$JAR/ak2" "$(authorize_url "$CLIENT_ID" "$REDIRECT_URI" s3 "$CHALLENGE" none)")
[[ -n "$(param code "$END")" && "$HOPS" == K ]] && say "Keycast session: code in one hop" || fail "Keycast session ended at '$END' ($HOPS)"

# 4. An app that is neither trusted nor approved gets consent_required, never a page.
OTHER_REDIRECT=http://localhost:3097/callback
read -r END HOPS < <(follow "$JAR/kc1" "$JAR/ak2" "$(authorize_url belfry-silent-check "$OTHER_REDIRECT" s4 "$CHALLENGE" none)")
[[ "$END" == "$OTHER_REDIRECT?error=consent_required&state=s4" ]] && say "unapproved app: error=consent_required" || fail "unapproved app ended at '$END' ($HOPS)"
unset OTHER_REDIRECT

# 5. A trusted client id with a foreign redirect URI is refused outright (registered client).
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -b "$JAR/kc1" "$(authorize_url "$CLIENT_ID" https://evil.example/cb s5 "$CHALLENGE" none)")
[[ "$STATUS" == 400 ]] && say "$CLIENT_ID with a foreign redirect URI -> 400" || fail "foreign redirect URI -> HTTP $STATUS"

# 6. Interactive (the button), Authentik session, fresh Keycast browser: Keycast goes straight to
#    Authentik (trusted app, password login hidden) and back with a code: no page, no consent.
cp "$SESSION_JAR" "$JAR/ak3"; touch "$JAR/kc3"
read -r END HOPS < <(follow "$JAR/kc3" "$JAR/ak3" "$(authorize_url "$CLIENT_ID" "$REDIRECT_URI" s6 "$CHALLENGE")")
[[ -n "$(param code "$END")" && "$(param state "$END")" == s6 ]] \
  && say "button: Keycast -> Authentik -> code, no Keycast page and no consent screen ($HOPS)" \
  || fail "interactive sign-in ended at '$END' ($HOPS): $(sed 's/<[^>]*>/ /g' "$JAR/page" | tr -s ' ' | head -c 200)"

# 7. Two sign-ins at once in one browser (a silent try in a second tab while a magic link is
#    pending): each keeps its own flow cookie, so the first one still finishes.
cp "$SESSION_JAR" "$JAR/ak4"; touch "$JAR/kc4"
start_flow() {  # start_flow <state>: Keycast start -> the Authentik authorize URL (not followed)
  local start
  start=$(curl -sS -b "$JAR/kc4" -c "$JAR/kc4" -o /dev/null -w '%{redirect_url}' "$(authorize_url "$CLIENT_ID" "$REDIRECT_URI" "$1" "$CHALLENGE")")
  curl -sS -b "$JAR/kc4" -c "$JAR/kc4" -o /dev/null -w '%{redirect_url}' "$start"
}
FIRST=$(start_flow s7a); SECOND=$(start_flow s7b)
[[ "$FIRST" == "$AK_URL"* && "$SECOND" == "$AK_URL"* ]] || fail "parallel sign-ins did not reach Authentik: '$FIRST' '$SECOND'"
[[ $(grep -c 'keycast_oidc_' "$JAR/kc4") -ge 2 ]] || fail "expected two flow cookies, jar has: $(grep -o 'keycast_oidc[^[:space:]]*' "$JAR/kc4" | tr '\n' ' ')"
read -r END HOPS < <(follow "$JAR/kc4" "$JAR/ak4" "$FIRST")
[[ -n "$(param code "$END")" && "$(param state "$END")" == s7a ]] \
  && say "two sign-ins at once: the first one still finishes (own flow cookie)" \
  || fail "first of two parallel sign-ins ended at '$END' ($HOPS): $(sed 's/<[^>]*>/ /g' "$JAR/page" | tr -s ' ' | head -c 200)"

# 8. A callback this browser did not start (magic link opened elsewhere): a branded page that
#    offers to sign in again, not a bare error.
STATUS=$(curl -sS -o "$JAR/err.html" -w '%{http_code}' "$KC_URL/api/auth/oidc/callback?code=x&state=nosuchflowstate1")
[[ "$STATUS" == 400 ]] && grep -q 'osv-logo.svg' "$JAR/err.html" && grep -q 'href="/api/auth/oidc/start' "$JAR/err.html" \
  && say "unknown sign-in: branded page with 'Sign in again' (HTTP 400)" \
  || fail "unknown sign-in page: HTTP $STATUS $(sed 's/<[^>]*>/ /g' "$JAR/err.html" | tr -s ' ' | head -c 200)"

# 9. App logout, part 1: the app revokes its own authorization with its refresh token.
REFRESH=$(jq -r '.refresh_token // empty' <<<"$TOKEN_RESP")
[[ -n "$REFRESH" ]] || fail "token response without refresh_token"
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$KC_URL/api/oauth/revoke" -H 'Content-Type: application/json' \
  -H "Origin: ${REDIRECT_URI%/oauth/callback}" -d "$(jq -nc --arg t "$REFRESH" --arg c "$CLIENT_ID" '{token:$t, client_id:$c}')")
[[ "$STATUS" == 200 ]] || fail "revoke answered HTTP $STATUS"
# Keycast's HTTP RPC caches a token's authorization and rechecks it every 5 s, so allow for that.
for i in $(seq 1 10); do
  SIGNED=$(curl -sS -X POST "$KC_URL/api/nostr" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg pk "$EXPECT_PUB" --argjson t "$(date +%s)" '{method:"sign_event", params:[{pubkey:$pk, created_at:$t, kind:1, tags:[], content:"after revoke"}]}')")
  [[ -z "$(jq -r '.result.sig // empty' <<<"$SIGNED" 2>/dev/null)" ]] && break
  sleep 1
done
[[ -z "$(jq -r '.result.sig // empty' <<<"$SIGNED" 2>/dev/null)" ]] \
  && say "revoke: the app's authorization stops signing (after ${i}s at most)" || fail "still signing 10 s after revoke: $(head -c 200 <<<"$SIGNED")"

fi

[[ -n "${LOGOUT_JAR:-}" ]] || exit 0
# 10. App logout, part 2: Keycast's logout ends the Keycast and the Authentik session and returns
#     to the app. The Authentik invalidation flow is a JS page, so its executor API is driven here.
APP_HOME="${REDIRECT_URI%/oauth/callback}/"
[[ -s "$LOGOUT_JAR" ]] || fail "no cookie jar at $LOGOUT_JAR"
cp "$LOGOUT_JAR" "$JAR/ak5"; touch "$JAR/kc5"
read -r END HOPS < <(follow "$JAR/kc5" "$JAR/ak5" "$(authorize_url "$CLIENT_ID" "$REDIRECT_URI" s9 "$CHALLENGE" none)")
[[ -n "$(param code "$END")" ]] && grep -q keycast_session "$JAR/kc5" || fail "logout check: no silent sign-in first ('$END', $HOPS)"
LOC=$(curl -sS -b "$JAR/kc5" -c "$JAR/kc5" -o /dev/null -w '%{redirect_url}' "$KC_URL/api/auth/oidc/logout?return_to=$(urlenc "$APP_HOME")")
[[ "$LOC" == "$AK_URL"*end-session* ]] || fail "logout redirected to '$LOC'"
grep -q $'keycast_session\t[^[:space:]]' "$JAR/kc5" && fail "keycast_session still set after logout" || true
FLOW_URL=$(curl -sS -b "$JAR/ak5" -c "$JAR/ak5" -o /dev/null -w '%{redirect_url}' "$LOC")
[[ "$FLOW_URL" == *"/if/flow/belfry-signer-logout/"* ]] || fail "end-session went to '$FLOW_URL' (invalidation flow not belfry-signer-logout?)"
QUERY=${FLOW_URL#*\?}; [[ "$QUERY" == "$FLOW_URL" ]] && QUERY=""
TO=""
for _ in 1 2 3 4 5; do
  CH=$(curl -sS -b "$JAR/ak5" -c "$JAR/ak5" "$AK_URL/api/v3/flows/executor/belfry-signer-logout/?query=$(urlenc "$QUERY")")
  if [[ "$(jq -r '.component // empty' <<<"$CH")" == xak-flow-redirect ]]; then TO=$(jq -r '.to' <<<"$CH"); break; fi
done
[[ "$TO" == "$KC_URL/api/auth/oidc/logged-out" ]] || fail "invalidation flow ended with: $(head -c 300 <<<"$CH")"
BACK=$(curl -sS -b "$JAR/kc5" -c "$JAR/kc5" -o /dev/null -w '%{redirect_url}' "$TO")
[[ "$BACK" == "$APP_HOME" ]] && say "logout: Keycast -> Authentik end-session -> Keycast -> back at $APP_HOME" || fail "logged-out returned to '$BACK'"
[[ -z "$(curl -sS -b "$JAR/ak5" "$AK_URL/api/v3/core/users/me/" | jq -r '.user.username // empty' 2>/dev/null)" ]] \
  && say "logout: the Authentik session has ended" || fail "Authentik session still alive after logout"
read -r END HOPS < <(follow "$JAR/kc5" "$JAR/ak5" "$(authorize_url "$CLIENT_ID" "$REDIRECT_URI" s10 "$CHALLENGE" none)")
[[ "$END" == "$REDIRECT_URI?error=login_required&state=s10" ]] \
  && say "after logout the silent check answers login_required (no automatic sign-in)" || fail "after logout: '$END' ($HOPS)"
STATUS=$(curl -sS -o /dev/null -w '%{redirect_url}' -b "$JAR/kc5" "$KC_URL/api/auth/oidc/logged-out")
[[ "$STATUS" == "$KC_URL/" ]] && say "logged-out without a logout cookie returns to Keycast's own page" || fail "logged-out without cookie -> '$STATUS'"
