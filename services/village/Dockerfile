# Belfry village apps: the directory (people.<domain>), the marketplace (market.<domain>) and the
# photos (photos.<domain>), one Node process serving all three. Build context: the repo root (brand/ for the app bar and icons).
FROM node:24-alpine AS build
WORKDIR /app
COPY services/village/package.json services/village/package-lock.json ./
RUN npm ci
COPY services/village/ ./
COPY brand/osv-bar.js web/people/public/osv-bar.js
COPY brand/osv-bar.js web/market/public/osv-bar.js
COPY brand/osv-bar.js web/photos/public/osv-bar.js
COPY brand/apps/people-192.png web/people/public/icon-192.png
COPY brand/apps/people-512.png web/people/public/icon-512.png
COPY brand/apps/people-maskable-512.png web/people/public/icon-maskable-512.png
COPY brand/apps/people-180.png web/people/public/apple-touch-icon.png
COPY brand/apps/market-192.png web/market/public/icon-192.png
COPY brand/apps/market-512.png web/market/public/icon-512.png
COPY brand/apps/market-maskable-512.png web/market/public/icon-maskable-512.png
COPY brand/apps/market-180.png web/market/public/apple-touch-icon.png
COPY brand/apps/photos-192.png web/photos/public/icon-192.png
COPY brand/apps/photos-512.png web/photos/public/icon-512.png
COPY brand/apps/photos-maskable-512.png web/photos/public/icon-maskable-512.png
COPY brand/apps/photos-180.png web/photos/public/apple-touch-icon.png
RUN npm test
# Sign-in through Keycast, the community relay, Blossom for listing photos and the photos app, links into the chat,
# the wallet and between the two apps, the app bar.
ARG VITE_KEYCAST_URL=http://localhost:3000
ARG VITE_RELAY_URL=ws://localhost:3090
ARG VITE_BLOSSOM_URL=http://localhost:3100
ARG VITE_CHAT_URL=http://localhost:3300
ARG VITE_WALLET_URL=http://localhost:3400
ARG VITE_PEOPLE_URL=http://localhost:3600
ARG VITE_APPS_URL=http://localhost:3000
ARG VITE_COMMUNITY_APPS=""
ARG VITE_BRAND_URL=http://localhost:8100/brand
ENV VITE_KEYCAST_URL=$VITE_KEYCAST_URL VITE_RELAY_URL=$VITE_RELAY_URL VITE_BLOSSOM_URL=$VITE_BLOSSOM_URL \
    VITE_CHAT_URL=$VITE_CHAT_URL VITE_WALLET_URL=$VITE_WALLET_URL VITE_PEOPLE_URL=$VITE_PEOPLE_URL \
    VITE_APPS_URL=$VITE_APPS_URL VITE_COMMUNITY_APPS=$VITE_COMMUNITY_APPS VITE_BRAND_URL=$VITE_BRAND_URL
RUN npm run build && npm prune --omit=dev

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production PEOPLE_PORT=8080 MARKET_PORT=8081 PHOTOS_PORT=8082
COPY --from=build /app/node_modules node_modules
COPY --from=build /app/package.json ./
COPY --from=build /app/server server
COPY --from=build /app/web/people/dist web/people/dist
COPY --from=build /app/web/market/dist web/market/dist
COPY --from=build /app/web/photos/dist web/photos/dist
USER node
EXPOSE 8080 8081 8082
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/api/health || exit 1
CMD ["node", "server/index.js"]
