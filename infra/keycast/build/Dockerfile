# Builds belfry-keycast:local from github.com/divinevideo/keycast at a pinned commit, plus the
# Belfry patch series in patches/ (git format-patch output, applied in order; see
# infra/keycast/README.md "The Belfry fork").
#
# Why not `build: https://github.com/divinevideo/keycast.git#<commit>` in compose: the upstream
# repo has a gitlink `divine-funnelcake` without a .gitmodules entry, so BuildKit's git context
# fails with "No url found for submodule path 'divine-funnelcake'". This file clones without
# submodules in a first stage; the remaining stages are the upstream Dockerfile verbatim, with
# COPY sources pointing at that stage. Cold build: about 15 minutes, 460 MB image.
ARG KEYCAST_REPO=https://github.com/divinevideo/keycast.git
ARG KEYCAST_COMMIT=fe9c6460c89e82e22c3aab4ba1dc79b7f0dd54c0

FROM alpine/git:2.47.2 AS src
ARG KEYCAST_REPO
ARG KEYCAST_COMMIT
WORKDIR /src
COPY patches/ /patches/
RUN git init -q . && git remote add origin "$KEYCAST_REPO" \
    && git fetch -q --depth 1 origin "$KEYCAST_COMMIT" \
    && git checkout -q FETCH_HEAD \
    && for p in /patches/*.patch; do echo "applying $p"; git apply --whitespace=nowarn "$p" || exit 1; done \
    && rm -rf .git

# ---- upstream Dockerfile from here on (sources from the src stage) ----

# Build stage for Rust API
FROM rust:1.93-slim-bookworm AS rust-builder

RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=src /src/api ./api
COPY --from=src /src/signer ./signer
COPY --from=src /src/core ./core
COPY --from=src /src/keycast ./keycast
COPY --from=src /src/cluster-hashring ./cluster-hashring
COPY --from=src /src/tools ./tools
COPY --from=src /src/database/migrations ./database/migrations
COPY --from=src /src/Cargo.toml ./Cargo.toml
COPY --from=src /src/Cargo.lock ./Cargo.lock

RUN cargo build --release --bin keycast
RUN cargo build --release --example migrate-vine-users

# Build stage for Bun frontend
FROM oven/bun:1 AS web-builder

RUN apt-get update && apt-get install -y \
    python3 \
    python-is-python3 \
    make \
    g++ \
    && rm -rf /var/lib/apt/lists/*

RUN bun add -g node-gyp

ENV NODE_OPTIONS="--max-old-space-size=2048"
ENV CI=true
ENV NODE_ENV=production
ENV VITE_BUILD_MODE=production
ENV PATH=/app/node_modules/.bin:$PATH
ENV VITE_DISABLE_CHUNK_SPLITTING=true
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /app

COPY --from=src /src/keycast-login ./keycast-login
WORKDIR /app/keycast-login
RUN bun install
RUN bun run build

WORKDIR /app/web
COPY --from=src /src/web .
COPY --from=src /src/scripts ./scripts

RUN bun install
RUN if [ "$(uname -m)" = "aarch64" ]; then \
    bun add -d @rollup/rollup-linux-arm64-gnu; \
    fi
RUN bunx svelte-kit sync
RUN bun run build

# Final stage
FROM debian:bookworm-slim AS runtime

LABEL service="keycast"
WORKDIR /app

RUN apt-get update && apt-get install -y \
    sqlite3 \
    ca-certificates \
    netcat-openbsd \
    bash \
    curl \
    unzip \
    iproute2 \
    procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://bun.sh/install | bash

RUN mkdir -p /app/database /data

COPY --from=rust-builder /app/target/release/keycast ./
COPY --from=rust-builder /app/target/release/examples/migrate-vine-users ./
COPY --from=web-builder /app/web/build ./web
COPY --from=web-builder /app/web/package.json ./
COPY --from=web-builder /app/web/node_modules ./node_modules

COPY --from=src /src/database/migrations ./database/migrations
COPY --from=src /src/examples ./examples
COPY --from=web-builder /app/keycast-login/dist ./keycast-login/dist

ENV NODE_ENV=production \
    BUN_ENV=production \
    PATH=/root/.bun/bin:$PATH \
    WEB_BUILD_DIR=/app/web

EXPOSE 3000 5173

COPY --from=src /src/scripts/healthcheck.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/healthcheck.sh

COPY --from=src /src/scripts/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh", "unified"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["unified"]
