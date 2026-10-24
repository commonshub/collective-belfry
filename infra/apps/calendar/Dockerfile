# Open Source Village calendar: Calendar by Form* (formstr-hq/nostr-calendar, MIT, NIP-52) at a
# pinned commit plus Belfry's patch series (patches/), built as a static site and served by Caddy.
# Build context: the repo root (the OSV logo comes from brand/). See infra/apps/calendar/README.md.
FROM node:22-alpine AS build
# pnpm 10: the workspace file is in its format; pnpm 11+ refuses the dependencies' install
# scripts (ERR_PNPM_IGNORED_BUILDS), pnpm 10 skips them, which the web build does not need.
RUN apk add --no-cache git && npm install -g pnpm@10.18.3
ARG CALENDAR_COMMIT=313596c75cce4a676202f6da05f96a3337bb4e6e
RUN git clone https://github.com/formstr-hq/nostr-calendar.git /src \
 && cd /src && git checkout --quiet "$CALENDAR_COMMIT"
WORKDIR /src
COPY infra/apps/calendar/patches /patches
RUN git apply /patches/*.patch
COPY brand/osv-logo-horizontal-white.svg public/osv-logo.svg
COPY brand/osv-mark-white.svg public/icon.svg
# Home-screen icons (brand/make-app-icons.py) and the Open Source Village app bar (patch 0002).
COPY brand/apps/calendar-192.png public/pwa-192x192.png
COPY brand/apps/calendar-512.png public/pwa-512x512.png
COPY brand/apps/calendar-maskable-512.png public/pwa-maskable-512x512.png
COPY brand/apps/calendar-180.png public/apple-touch-icon.png
COPY brand/osv-bar.js public/osv-bar.js
RUN pnpm install --frozen-lockfile
# The community relay (NIP-42 whitelist of active passes) is the only relay for reading and
# writing; VITE_TEST_RELAY is the app's single-relay switch. VITE_SIGNER_RELAYS: Keycast's NIP-46
# relay for "nostrconnect" logins. VITE_MAIL_API_BASE_URL empty: no lookups at api.formstr.app.
ARG VITE_TEST_RELAY=ws://localhost:3090
ARG VITE_SIGNER_RELAYS=ws://localhost:3080
ARG VITE_BRAND_URL=http://localhost:8100/brand
# The sidebar's Apps link (Keycast's apps page, where participants copy their bunker URL) and
# About link; with VITE_APPS_URL set, Formstr's privacy policy and contact form are hidden.
ARG VITE_APPS_URL=http://localhost:3000
ARG VITE_ABOUT_URL=https://opensourcevillage.org
# The app bar's links to the other apps (Name|URL|Description;..., as Keycast's
# BELFRY_COMMUNITY_APPS); the bar's home link is VITE_APPS_URL.
ARG VITE_COMMUNITY_APPS=""
# "Sign in with Open Source Village": Keycast's OAuth (client belfry-calendar, redirect URI
# <origin>/oauth/callback) yields the bunker URL; empty disables the button and the silent check.
ARG VITE_KEYCAST_URL=http://localhost:3000
ENV VITE_TEST_RELAY=$VITE_TEST_RELAY VITE_SIGNER_RELAYS=$VITE_SIGNER_RELAYS \
    VITE_BRAND_URL=$VITE_BRAND_URL VITE_APPS_URL=$VITE_APPS_URL VITE_ABOUT_URL=$VITE_ABOUT_URL \
    VITE_KEYCAST_URL=$VITE_KEYCAST_URL VITE_COMMUNITY_APPS=$VITE_COMMUNITY_APPS \
    VITE_MAIL_API_BASE_URL=""
RUN pnpm typecheck && pnpm build

FROM caddy:2-alpine
COPY --from=build /src/dist /srv
COPY infra/apps/calendar/Caddyfile /etc/caddy/Caddyfile
