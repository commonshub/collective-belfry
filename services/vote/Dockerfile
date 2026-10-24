# Belfry vote service: anonymous polls (Semaphore) at vote.<domain>, API and web app in one Node
# process. Build context: the repo root (brand/ for the app bar and the icons).
FROM node:24-alpine AS build
RUN apk add --no-cache curl
WORKDIR /app
COPY services/vote/package.json services/vote/package-lock.json ./
RUN npm ci
COPY services/vote/ ./
# Proving artifacts for tree depth 10 (up to 1024 voters per poll), checked against pinned hashes
# and served by the app itself, so browsers never fetch them from a third party.
RUN scripts/fetch-artifacts.sh 10
COPY brand/osv-bar.js web/public/osv-bar.js
COPY brand/apps/vote-192.png web/public/icon-192.png
COPY brand/apps/vote-512.png web/public/icon-512.png
COPY brand/apps/vote-maskable-512.png web/public/icon-maskable-512.png
COPY brand/apps/vote-180.png web/public/apple-touch-icon.png
RUN npm test
# Sign-in through Keycast (client belfry-vote), the app bar's links, the brand URL.
ARG VITE_KEYCAST_URL=http://localhost:3000
ARG VITE_APPS_URL=http://localhost:3000
ARG VITE_COMMUNITY_APPS=""
ARG VITE_BRAND_URL=http://localhost:8100/brand
ENV VITE_KEYCAST_URL=$VITE_KEYCAST_URL VITE_APPS_URL=$VITE_APPS_URL \
    VITE_COMMUNITY_APPS=$VITE_COMMUNITY_APPS VITE_BRAND_URL=$VITE_BRAND_URL
RUN npm run build && npm prune --omit=dev

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production PORT=8080 DB_PATH=/data/vote.sqlite
COPY --from=build /app/node_modules node_modules
COPY --from=build /app/package.json ./
COPY --from=build /app/server server
COPY --from=build /app/web/dist web/dist
RUN mkdir -p /data && chown node:node /data
USER node
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/api/health || exit 1
CMD ["node", "server/index.js"]
