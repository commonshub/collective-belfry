# belfry-signer: Rust, built here (about 3 minutes cold), no upstream patches.
FROM rust:1.93-slim-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked && cp target/release/belfry-signer /belfry-signer

FROM debian:bookworm-slim
COPY --from=build /belfry-signer /usr/local/bin/belfry-signer
# the signer's data (SQLite) lives in the volume mounted at /data, owned by APP_UID 10001
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8200
HEALTHCHECK --interval=10s --timeout=5s --retries=10 CMD ["belfry-signer", "healthcheck"]
ENTRYPOINT ["belfry-signer"]
