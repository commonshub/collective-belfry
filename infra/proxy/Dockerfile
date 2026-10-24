# Belfry's reverse proxy: Caddy with the rate-limit module (github.com/mholt/caddy-ratelimit),
# pinned. Runs as an unprivileged user; the binary keeps the capability to bind 80 and 443.
FROM caddy:2.11.7-builder AS build
RUN xcaddy build v2.11.7 --with github.com/mholt/caddy-ratelimit@v0.1.0

FROM caddy:2.11.7
COPY --from=build /usr/bin/caddy /usr/bin/caddy
RUN apk add --no-cache libcap && setcap cap_net_bind_service=+ep /usr/bin/caddy && apk del libcap \
 && caddy list-modules | grep -q '^http.handlers.rate_limit$'
