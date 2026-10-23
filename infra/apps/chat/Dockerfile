# syntax=docker/dockerfile:1.7
# Community chat: Coracle (coracle-social/coracle, MIT) at a pinned commit, built with Belfry's
# relays and the Open Source Village look, served by nginx (upstream's own nginx.conf).
# Build args: CORACLE_COMMIT, CHAT_PUBLIC_URL, COMMUNITY_RELAY_URL, BUNKER_PUBLIC_RELAYS,
# BLOSSOM_PUBLIC_URL (see make-env.sh); build context "brand" = the repo's brand/ folder.

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
RUN pnpm install --frozen-lockfile

# Open Source Village look: logo, wordmark and the accent fix (the env below sets the palette).
COPY --from=brand web-app-manifest-512x512.png public/images/logo.png
COPY --from=brand osv-logo-horizontal-dark.png public/images/wordmark-dark.png
COPY --from=brand osv-logo-horizontal-dark.png public/images/wordmark-light.png
COPY osv.css /tmp/osv.css
RUN cat /tmp/osv.css >> src/app.css
# No analytics to Coracle's Plausible, and the page names the village.
RUN sed -i -e '/plausible.coracle.social\/js/d' -e 's/ plausible.coracle.social//' \
        -e 's/content="Coracle - Nostr Client"/content="Open Source Village chat"/g' \
        -e 's|<head>|<head>\n    <title>Open Source Village chat</title>|' index.html \
    && ! grep -q plausible index.html && grep -q "<title>Open Source Village chat" index.html

ARG CHAT_PUBLIC_URL
ARG COMMUNITY_RELAY_URL
ARG BUNKER_PUBLIC_RELAYS
ARG BLOSSOM_PUBLIC_URL
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
