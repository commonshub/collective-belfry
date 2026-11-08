#!/bin/sh
# Renders Coracle's build-time .env for Belfry (Vite inlines VITE_* into the bundle).
# Inputs (build args): CHAT_PUBLIC_URL, COMMUNITY_RELAY_URL, BUNKER_PUBLIC_RELAYS, BLOSSOM_PUBLIC_URL,
# KEYCAST_APP_URL (the signer for "Sign in with Open Source Village"; empty turns the button and the
# silent sign-in off).
# Palette: brand/tokens.css (Open Source Village, dark only; the light theme gets the same values).
set -eu
: "${CHAT_PUBLIC_URL:?}" "${COMMUNITY_RELAY_URL:?}" "${BUNKER_PUBLIC_RELAYS:?}" "${BLOSSOM_PUBLIC_URL:?}"
THEME="accent:#d6ff52,neutral-50:#ffffff,neutral-100:#eef2ef,neutral-200:#dfe5e1,neutral-300:#c9d1cb,neutral-400:#a7b1aa,neutral-500:#8e9a91,neutral-600:#5c6b62,neutral-700:#44534a,neutral-800:#2b3a31,neutral-900:#243128,neutral-950:#1a231d,success:#12d2b0,tinted-100:#eef2ef,tinted-200:#dfe5e1,tinted-400:#a7b1aa,tinted-500:#8e9a91,tinted-600:#5c6b62,tinted-700:#44534a,tinted-800:#243128,warning:#f89b0e,danger:#dc0c0c"
cat <<ENV
VITE_DARK_THEME="$THEME"
VITE_LIGHT_THEME="$THEME"
VITE_DEFAULT_RELAYS=$COMMUNITY_RELAY_URL
VITE_INDEXER_RELAYS=$COMMUNITY_RELAY_URL
VITE_SEARCH_RELAYS=$COMMUNITY_RELAY_URL
VITE_SIGNER_RELAYS=$BUNKER_PUBLIC_RELAYS
VITE_DVM_RELAYS=
VITE_DEFAULT_FOLLOWS=
VITE_ONBOARDING_LISTS=
VITE_BLOSSOM_URLS=$BLOSSOM_PUBLIC_URL
VITE_DUFFLEPUD_URL=
VITE_PLATFORM_ZAP_SPLIT=0
VITE_PLATFORM_PUBKEY=8ec86ac9e10979998652068ee6b00223b8e3265aabb3fe28fb6b3b6e294adc96
VITE_ENABLE_ZAPS=false
VITE_ENABLE_MARKET=false
VITE_BLUR_CONTENT=false
VITE_APP_NAME=Open Source Village
VITE_APP_URL=$CHAT_PUBLIC_URL
VITE_APP_LOGO=/images/logo.png
VITE_APP_WORDMARK_DARK=/images/wordmark-dark.png
VITE_APP_WORDMARK_LIGHT=/images/wordmark-light.png
VITE_APP_DESCRIPTION="Chat for Open Source Village, run by Belfry"
VITE_CLIENT_NAME=Coracle
VITE_CLIENT_ID=31990:97c70a44366a6535c145b333f973ea86dfdc2d7a99da618c40c64705ad98e322:1685968093690
VITE_BUILD_HASH=${CORACLE_COMMIT:-}
VITE_LOG_LEVEL=info
VITE_KEYCAST_URL=${KEYCAST_APP_URL:-}
VITE_OAUTH_CLIENT_ID=belfry-chat
ENV
