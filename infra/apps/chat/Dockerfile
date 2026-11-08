# syntax=docker/dockerfile:1.7
# Community chat: Coracle (coracle-social/coracle, MIT) at a pinned commit, built with Belfry's
# relays and the Open Source Village look, served by nginx (upstream's own nginx.conf).
# Build args: CORACLE_COMMIT, CHAT_PUBLIC_URL, COMMUNITY_RELAY_URL, BUNKER_PUBLIC_RELAYS,
# BLOSSOM_PUBLIC_URL, KEYCAST_APP_URL (see make-env.sh), COMMUNITY_APPS (the app bar); build context
# "brand" = the repo's brand/ folder.

FROM node:22-bookworm-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
WORKDIR /app

ARG CORACLE_COMMIT=2bdb17f5545f923959d09824e9af7db636b0255c
RUN git init -q . \
    && git remote add origin https://github.com/coracle-social/coracle.git \
    && git fetch -q --depth 1 origin "$CORACLE_COMMIT" \
    && git checkout -q FETCH_HEAD
# Belfry's patches (patches/): "Sign in with Open Source Village" and the silent sign-in check.
COPY patches/ /tmp/patches/
RUN for p in /tmp/patches/*.patch; do echo "applying $p"; git apply "$p" || exit 1; done
RUN pnpm install --frozen-lockfile

# Open Source Village look: logo, wordmark and the accent fix (the env below sets the palette).
# The chat's own icon (brand/apps, brand/make-app-icons.py): favicons, PWA and home-screen icons
# are generated from it, with Belfry's pwa-assets config (no padding, no white background).
COPY --from=brand apps/chat-maskable-512.png public/images/logo.png
COPY pwa-assets.config.js pwa-assets.config.js
COPY --from=brand osv-logo-horizontal-dark.png public/images/wordmark-dark.png
COPY --from=brand osv-logo-horizontal-dark.png public/images/wordmark-light.png
COPY osv.css /tmp/osv.css
RUN cat /tmp/osv.css >> src/app.css
# No analytics to Coracle's Plausible, and the page names the village.
RUN sed -i -e '/plausible.coracle.social\/js/d' -e 's/ plausible.coracle.social//' \
        -e 's/content="Coracle - Nostr Client"/content="Open Source Village chat"/g' \
        -e 's|<head>|<head>\n    <title>Open Source Village chat</title>|' index.html \
    && ! grep -q plausible index.html && grep -q "<title>Open Source Village chat" index.html
# Installed on a home screen it is "Chat" with the bar's dark green, not the lime accent.
RUN sed -i -e 's/^\( *\)name: process.env.VITE_APP_NAME,/\1name: "Open Source Village chat",/' \
        -e 's/^\( *\)short_name: process.env.VITE_APP_NAME,/\1short_name: "Chat",\n\1background_color: "#1a231d",/' \
        -e 's/theme_color: accentColor/theme_color: "#1a231d"/' \
        -e 's/{name: "theme-color", content: accentColor}/{name: "theme-color", content: "#1a231d"}/' vite.config.js \
    && grep -q 'short_name: "Chat"' vite.config.js && grep -q 'content: "#1a231d"' vite.config.js

ARG CHAT_PUBLIC_URL
ARG COMMUNITY_RELAY_URL
ARG BUNKER_PUBLIC_RELAYS
ARG BLOSSOM_PUBLIC_URL
ARG KEYCAST_APP_URL
# The Open Source Village app bar (brand/osv-bar.js): back to the apps page (KEYCAST_APP_URL) and
# to the other apps (COMMUNITY_APPS, Name|URL|Description;...).
ARG COMMUNITY_APPS=""
COPY --from=brand osv-bar.js public/osv-bar.js
COPY add-app-bar.mjs /tmp/add-app-bar.mjs
RUN node /tmp/add-app-bar.mjs "$KEYCAST_APP_URL" "$COMMUNITY_APPS" && grep -q 'src="/osv-bar.js"' index.html
COPY make-env.sh /tmp/make-env.sh
RUN CORACLE_COMMIT="$CORACLE_COMMIT" sh /tmp/make-env.sh > .env && cat .env

# Upstream's own bundle has needed more than the default heap.
RUN NODE_OPTIONS=--max_old_space_size=4096 pnpm run build:web

FROM alpine:3.21
RUN apk add --no-cache nginx nginx-mod-http-brotli && mkdir -p /run/nginx
COPY --from=builder /app/nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
