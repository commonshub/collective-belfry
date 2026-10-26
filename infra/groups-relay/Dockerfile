# Groups relay: zooid (coracle/zooid, MIT, a Khatru relay with NIP-29 groups) at a pinned commit
# with Belfry's patches (patches/), the relay behind the groups app (Flotilla, infra/apps/groups).
# The config is rendered at start from the environment and the secret files (entrypoint.sh), so
# the runtime image is Alpine with a shell instead of upstream's distroless one.

FROM golang:1.27 AS build
WORKDIR /app
ARG ZOOID_COMMIT=0f737e1c5648831f87e9a6d917ef0e1f35996210
RUN git init -q . \
    && git remote add origin https://gitea.coracle.social/coracle/zooid.git \
    && git fetch -q --depth 1 origin "$ZOOID_COMMIT" \
    && git checkout -q FETCH_HEAD
COPY patches/ /tmp/patches/
RUN for p in /tmp/patches/*.patch; do echo "applying $p"; git apply "$p" || exit 1; done
RUN go mod download && CGO_ENABLED=0 go build -o bin/zooid cmd/relay/main.go

FROM alpine:3.21
COPY --from=build /app/bin/zooid /bin/zooid
COPY --from=build /app/templates /templates
COPY --from=build /app/static /static
COPY entrypoint.sh /entrypoint.sh
ENV CONFIG=/app/config MEDIA=/app/media DATA=/app/data
EXPOSE 3334
ENTRYPOINT ["/entrypoint.sh"]
