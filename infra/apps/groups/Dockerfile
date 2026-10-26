# syntax=docker/dockerfile:1.7
# Groups: Flotilla (coracle/flotilla, MIT) at a pinned commit in platform mode on Belfry's groups
# relay, with the Open Source Village look, served by nginx. Build args: FLOTILLA_COMMIT,
# GROUPS_PUBLIC_URL, GROUPS_RELAY_URL, COMMUNITY_RELAY_URL, BUNKER_PUBLIC_RELAYS,
# BLOSSOM_PUBLIC_URL, KEYCAST_APP_URL (see make-env.sh), COMMUNITY_APPS (the app bar); build
# context "brand" = the repo's brand/ folder.

FROM node:24-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates perl \
    && rm -rf /var/lib/apt/lists/*
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
WORKDIR /app

ARG FLOTILLA_COMMIT=6143abdf336081a780883cc9fca646a0902f220a
RUN git init -q . \
    && git remote add origin https://gitea.coracle.social/coracle/flotilla.git \
    && git fetch -q --depth 1 origin "$FLOTILLA_COMMIT" \
    && git checkout -q FETCH_HEAD
# Belfry's patches (patches/): no third-party services, members create rooms, "Sign in with
# Open Source Village" with the silent check and logout everywhere.
COPY patches/ /tmp/patches/
RUN for p in /tmp/patches/*.patch; do echo "applying $p"; git apply "$p" || exit 1; done
RUN pnpm install --frozen-lockfile

# Open Source Village look: the groups icon (brand/apps, brand/make-app-icons.py) as the platform
# logo, from which the build generates favicons and PWA icons, and the palette (osv.css).
COPY --from=brand apps/groups-maskable-512.png static/logo.png
COPY osv.css /tmp/osv.css
RUN cat /tmp/osv.css >> src/app.css

ARG GROUPS_PUBLIC_URL
ARG GROUPS_RELAY_URL
ARG COMMUNITY_RELAY_URL
ARG BUNKER_PUBLIC_RELAYS
ARG BLOSSOM_PUBLIC_URL
ARG KEYCAST_APP_URL
ARG SITE_URL
# The Open Source Village app bar (brand/osv-bar.js): back to the apps page (KEYCAST_APP_URL) and
# to the other apps (COMMUNITY_APPS, Name|URL|Description;...).
ARG COMMUNITY_APPS=""
COPY --from=brand osv-bar.js static/osv-bar.js
RUN node -e ' \
      const fs = require("fs"); \
      const [home, apps] = process.argv.slice(1); \
      const esc = s => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"); \
      const tag = `<script src="/osv-bar.js" data-home="${esc(home)}" data-apps="${esc(apps)}"></script>`; \
      const html = fs.readFileSync("src/app.html", "utf8"); \
      if (!html.includes("</head>")) throw new Error("src/app.html has no </head>"); \
      fs.writeFileSync("src/app.html", html.replace("</head>", `  ${tag}\n  </head>`)); \
    ' "$KEYCAST_APP_URL" "$COMMUNITY_APPS" \
    && grep -q 'src="/osv-bar.js"' src/app.html
COPY make-env.sh /tmp/make-env.sh
RUN FLOTILLA_COMMIT="$FLOTILLA_COMMIT" sh /tmp/make-env.sh > .env && cat .env

RUN bash scripts/build/web.sh \
    && ! grep -rqE "plausible\.coracle|dufflepud\.coracle|nps\.flotilla|npb\.coracle|pomade\." build

FROM alpine:3.21
RUN apk add --no-cache nginx
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /app/build /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
