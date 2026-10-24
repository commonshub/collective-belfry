#!/usr/bin/env bash
# Self-custody (Keycast patch 0012), driven with curl: a participant takes custody of their key,
# then moves the account to another key of their own.
#
#   fresh Keycast session (OIDC, the participant's Authentik browser) -> export the nsec
#   -> challenge, signed with that key -> POST /api/user/self-custody: Keycast deletes its copy
#   -> apps refuse "Sign in with Open Source Village" (access_denied, self_custody)
#   -> challenge, signed with a new key -> the account moves: NIP-05 name and sign-in follow,
#      the onboarding service moves the pass (checked by the caller).
#
# Usage: self-custody-check.sh <authentik session cookie jar> <pubkey hex> <nip05 name>
#   SIGN: command that reads {"secret": hex, "event": {...}} on stdin and prints the signed event
#   (the smoke test: python in the onboarding container). Prints NEW_PUBKEY=<hex> at the end.
# Irreversible for that participant: run it last.
set -euo pipefail

SESSION_JAR=${1:?cookie jar of a signed-in Authentik session}
EXPECT_PUB=${2:?the account pubkey (hex)}
NAME=${3:?the NIP-05 name}
KC_URL=${KC_URL:-http://localhost:3000}
AK_URL=${AK_URL:-http://localhost:9000}
CHAT_CB=${CHAT_CB:-http://localhost:3300/oauth/callback}
: "${SIGN:?SIGN must name a signing command}"

say()  { echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
urlenc() { jq -rn --arg v "$1" '$v|@uri'; }
JAR=$(mktemp -d); trap 'rm -rf "$JAR"' EXIT
cp "$SESSION_JAR" "$JAR/ak"; touch "$JAR/kc"
kc() { curl -sS -b "$JAR/kc" -c "$JAR/kc" "$@"; }

# A fresh Keycast session through Authentik (the key export and the custody change need one).
URL="$KC_URL/api/auth/oidc/start?return_to=%2F"
for _ in $(seq 1 8); do
  if [[ "$URL" == "$AK_URL"* ]]; then J="$JAR/ak"; else J="$JAR/kc"; fi
  NEXT=$(curl -sS -b "$J" -c "$J" -o /dev/null -w '%{redirect_url}' "$URL")
  [[ -z "$NEXT" || "$NEXT" == "$KC_URL/" ]] && break
  URL=$NEXT
done
grep -q keycast_session "$JAR/kc" || fail "no fresh Keycast session"
[[ "$(kc "$KC_URL/api/user/account" | jq -r '.public_key')" == "$EXPECT_PUB" ]] || fail "session is not $EXPECT_PUB"
say "fresh Keycast session for $EXPECT_PUB"

NSEC=$(kc -X POST "$KC_URL/api/user/export-key" -H 'Content-Type: application/json' -d '{"format":"hex"}' | jq -r '.key // empty')
[[ "$NSEC" =~ ^[0-9a-f]{64}$ ]] || NSEC=$(kc -X POST "$KC_URL/api/user/export-key" -H 'Content-Type: application/json' -d '{}' | jq -r '.key // empty')
[[ -n "$NSEC" ]] || fail "key export failed"
say "key exported"

custody() { # custody <secret: hex or nsec>
  local ch ev
  ch=$(kc "$KC_URL/api/user/self-custody/challenge" | jq -r '.challenge // empty')
  [[ -n "$ch" ]] || fail "no challenge"
  ev=$(jq -nc --arg s "$1" --arg c "$ch" --argjson t "$(date +%s)" \
    '{secret:$s, event:{kind:27235, created_at:$t, tags:[["challenge",$c]], content:""}}' | $SIGN)
  kc -X POST "$KC_URL/api/user/self-custody" -H 'Content-Type: application/json' -d "$(jq -nc --argjson e "$ev" '{event:$e}')"
}

# 1. Take over: the same key signs. Keycast deletes its copy.
R=$(custody "$NSEC")
[[ "$(jq -r .outcome <<<"$R")" == took_custody ]] && say "take over: Keycast deleted its copy ($(jq -r .authorizations_revoked <<<"$R") app authorizations revoked)" || fail "take over: $R"
[[ "$(kc "$KC_URL/api/user/account" | jq -r .key_held)" == false ]] && say "account status: key_held=false" || fail "key_held still true"
[[ -z "$(kc -X POST "$KC_URL/api/user/export-key" -H 'Content-Type: application/json' -d '{}' | jq -r '.key // empty' 2>/dev/null)" ]] && say "no key export any more" || fail "export still works"
LOC=$(kc -o /dev/null -w '%{redirect_url}' "$KC_URL/api/oauth/authorize?client_id=belfry-chat&redirect_uri=$(urlenc "$CHAT_CB")&scope=policy%3Abelfry-event&state=sc&code_challenge=x&code_challenge_method=S256&prompt=none")
[[ "$LOC" == *error=access_denied*self_custody* ]] && say "apps: silent sign-in answers access_denied (self_custody)" || fail "authorize after custody: $LOC"
CODE=$(kc -o "$JAR/page" -w '%{http_code}' "$KC_URL/api/oauth/authorize?client_id=belfry-chat&redirect_uri=$(urlenc "$CHAT_CB")&scope=policy%3Abelfry-event&state=sc&code_challenge=x&code_challenge_method=S256")
[[ "$CODE" == 403 ]] && grep -q "Use your own signer" "$JAR/page" && say "apps: the button shows 'Use your own signer'" || fail "interactive authorize after custody: HTTP $CODE"

# 2. Bring a new key: another key signs. The account moves to it.
NEW_SECRET=$(openssl rand -hex 32)
R=$(custody "$NEW_SECRET")
NEW_PUB=$(jq -r '.pubkey // empty' <<<"$R")
[[ "$(jq -r .outcome <<<"$R")" == moved_to_own_key && "$NEW_PUB" != "$EXPECT_PUB" ]] && say "new key: the account moved to $NEW_PUB" || fail "move: $R"
[[ "$(jq -r .pass_updated <<<"$R")" == true ]] && say "the onboarding service took the change (pass_updated)" || fail "pass not updated: $R"
[[ "$(curl -sS "$KC_URL/.well-known/nostr.json?name=$NAME" | jq -r --arg n "$NAME" '.names[$n] // empty')" == "$NEW_PUB" ]] \
  && say "NIP-05 $NAME now names the new key" || fail "NIP-05 after the move"
# signing in again lands on the moved account
rm -f "$JAR/kc"; touch "$JAR/kc"
URL="$KC_URL/api/auth/oidc/start?return_to=%2F"
for _ in $(seq 1 8); do
  if [[ "$URL" == "$AK_URL"* ]]; then J="$JAR/ak"; else J="$JAR/kc"; fi
  NEXT=$(curl -sS -b "$J" -c "$J" -o /dev/null -w '%{redirect_url}' "$URL")
  [[ -z "$NEXT" || "$NEXT" == "$KC_URL/" ]] && break
  URL=$NEXT
done
[[ "$(kc "$KC_URL/api/user/account" | jq -r '.public_key + " " + (.key_held|tostring)')" == "$NEW_PUB false" ]] \
  && say "signing in again lands on the moved account (no key held)" || fail "sign-in after the move: $(kc "$KC_URL/api/user/account" | head -c 200)"
echo "NEW_PUBKEY=$NEW_PUB"
