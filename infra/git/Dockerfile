# Git server: ngit-grasp (MIT, Rust; a GRASP relay and Git Smart HTTP server for NIP-34) at a
# pinned release with Belfry's patches (patches/), run as a private GRASP-08 service whose
# members are the active passes (README.md). Built like upstream's Dockerfile.

FROM rust:1.96-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# v3.1.0
ARG NGIT_GRASP_COMMIT=c71db76167db1389ef38607af222ac6f85fdd97e
ARG NGIT_GRASP_REPO=https://relay.ngit.dev/npub15qydau2hjma6ngxkl2cyar74wzyjshvl65za5k5rl69264ar2exs5cyejr/ngit-grasp.git
RUN git init -q . \
    && git remote add origin "$NGIT_GRASP_REPO" \
    && git fetch -q --depth 1 origin "$NGIT_GRASP_COMMIT" \
    && git checkout -q FETCH_HEAD
COPY patches/ /tmp/patches/
RUN for p in /tmp/patches/*.patch; do echo "applying $p"; git apply "$p" || exit 1; done
ENV NGIT_BUILD_REVISION=${NGIT_GRASP_COMMIT}
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    cargo build --locked --release -p ngit-grasp

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git libssl3 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 ngit-grasp \
    && useradd --system --uid 10001 --gid ngit-grasp --home-dir /data --no-create-home ngit-grasp \
    && install -d -m 0750 -o ngit-grasp -g ngit-grasp /data /data/git /data/relay
COPY --from=build /src/target/release/ngit-grasp /usr/local/bin/ngit-grasp
COPY --from=build /src/deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV HOME=/data NGIT_GIT_DATA_PATH=/data/git NGIT_RELAY_DATA_PATH=/data/relay
WORKDIR /data
USER 10001:10001
EXPOSE 7334
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
