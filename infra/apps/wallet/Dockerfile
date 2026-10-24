# Open Source Village wallet: cashu.me (cashubtc/cashu.me, MIT) at a pinned commit plus Belfry's
# patch series (patches/), built as a PWA and served by Caddy. Build context: the repo root
# (icons and the app bar come from brand/). See infra/apps/wallet/README.md.
FROM node:24-alpine AS build
RUN apk add --no-cache git
ARG WALLET_COMMIT=002a0a367966ffda04bb29d1ca888fcbcd070e16
RUN git clone https://github.com/cashubtc/cashu.me.git /src \
 && cd /src && git checkout --quiet "$WALLET_COMMIT"
WORKDIR /src
COPY infra/apps/wallet/patches /patches
RUN git apply /patches/*.patch
RUN npm ci --no-audit --no-fund
COPY brand/apps/wallet-180.png brand/apps/wallet-192.png brand/apps/wallet-512.png \
     brand/apps/wallet-maskable-512.png public/belfry/
COPY brand/osv-bar.js public/osv-bar.js
COPY infra/apps/wallet/brand.mjs /tmp/brand.mjs
# The village mint (unit "osv"), Keycast for "Sign in with Open Source Village" (client
# belfry-wallet, redirect URI <origin>/oauth/callback; derive_secret for the seed and the nutzap
# key) and the community relay (NIP-61 nutzaps, NIP-42 AUTH signed by Keycast).
ARG BELFRY_MINT_URL=http://localhost:3338
ARG BELFRY_KEYCAST_URL=http://localhost:3000
ARG BELFRY_COMMUNITY_RELAY=ws://localhost:3090
# The app bar: home link (Keycast's apps page) and the other apps (Name|URL|Description;...).
ARG APPS_URL=http://localhost:3000
ARG COMMUNITY_APPS=""
RUN node /tmp/brand.mjs "$APPS_URL" "$COMMUNITY_APPS"
ENV BELFRY_MINT_URL=$BELFRY_MINT_URL BELFRY_KEYCAST_URL=$BELFRY_KEYCAST_URL \
    BELFRY_COMMUNITY_RELAY=$BELFRY_COMMUNITY_RELAY
RUN npx quasar build -m pwa

FROM caddy:2-alpine
COPY --from=build /src/dist/pwa /srv
COPY infra/apps/wallet/Caddyfile /etc/caddy/Caddyfile
