#!/usr/bin/env bash
# Silent sign-in for Belfry's apps (Keycast patch 0008), driven with curl like a browser:
#
#   app -> Keycast /api/oauth/authorize?prompt=none
#       -> no Keycast session: /api/auth/oidc/start?prompt=none -> Authentik authorize prompt=none
#          -> Keycast callback (session, or oidc_failed) -> back to /api/oauth/authorize
#       -> app redirect URI with ?code=… (trusted app, no consent screen) or ?error=login_required
#
# Usage: silent-signin-check.sh <authentik session cookie jar> <expected pubkey hex>
#   CLIENT_ID / REDIRECT_URI: a trusted app (default belfry-chat at CHAT_PUBLIC_URL).
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
