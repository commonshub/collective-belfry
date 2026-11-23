# Belfry community relay: rnostr (https://github.com/rnostr/rnostr) at a pinned commit, with
# Belfry's patches (patches/*.patch, applied in order) and a committed Cargo.lock.
#
#   docker build -t belfry-relay:private-<short commit>-1 infra/relay/build
#   docker build --target test infra/relay/build      # the private-kinds unit tests
#
# The final image keeps the official image's layout: user rnostr, /rnostr/config,
# /rnostr/data, ENTRYPOINT rnostr, CMD relay --watch -c ./config/rnostr.toml.
# Built without NIP-50 search (charabia dictionaries) and NIP-77 negentropy.

ARG RUST_IMAGE=rust:1.100.0-slim-bookworm@sha256:7c7d4eb4d05b7781187a327809466cfddf29753f6defdc3d19a0e291951b7a37
ARG RUNTIME_IMAGE=debian:bookworm-slim

FROM ${RUST_IMAGE} AS source
# main on 2026-11-23: has the `outgoing` extension hook (PR #51) the private-kinds rule needs.
ARG RNOSTR_COMMIT=2f219797bdd3296f8e8ad89ef3179b46f808b2e0
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q . \
    && git remote add origin https://github.com/rnostr/rnostr.git \
    && git fetch -q --depth 1 origin "${RNOSTR_COMMIT}" \
    && git checkout -q FETCH_HEAD
COPY patches/ /patches/
RUN git apply --verbose /patches/*.patch
COPY Cargo.lock /src/Cargo.lock
# One job: the build machines are small.
ENV CARGO_BUILD_JOBS=1 \
    CARGO_INCREMENTAL=0 \
    CARGO_TERM_COLOR=never

FROM source AS test
RUN cargo test --locked -p nostr-extensions --no-default-features \
        --features metrics,rate_limiter,count private_kinds \
    && cargo test --locked -p nostr-extensions --no-default-features \
        --features metrics,rate_limiter,count auth::

FROM source AS builder
RUN cargo build --locked --release --no-default-features --bin rnostr \
    && strip target/release/rnostr

FROM ${RUNTIME_IMAGE} AS final
ARG HOME_DIR=/rnostr
ARG USER=rnostr
RUN adduser --home "${HOME_DIR}" --shell /bin/bash --disabled-login --gecos "${USER} user" ${USER} \
    && mkdir ${HOME_DIR}/data ${HOME_DIR}/config \
    && chown ${USER}:${USER} ${HOME_DIR}/data ${HOME_DIR}/config
COPY --from=builder --chown=${USER}:${USER} /src/target/release/rnostr /usr/local/bin/rnostr
COPY --from=builder --chown=${USER}:${USER} /src/rnostr.example.toml ${HOME_DIR}/config/rnostr.toml
ENV RNOSTR_NETWORK__HOST=0.0.0.0
WORKDIR ${HOME_DIR}
USER ${USER}
VOLUME ${HOME_DIR}/data
VOLUME ${HOME_DIR}/config
EXPOSE 8080
ENTRYPOINT ["rnostr"]
CMD ["relay", "--watch", "-c", "./config/rnostr.toml"]
