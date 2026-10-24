#!/bin/sh
# Fetches the Semaphore proving artifacts for one tree depth into web/public/artifacts, so the
# browser never loads them from a third party, and checks their SHA-256.
# Usage: scripts/fetch-artifacts.sh [depth]   (10: up to 1024 voters per poll)
set -eu
DEPTH=${1:-10}
VERSION=4.13.0   # the artifact version @semaphore-protocol/proof 4.14.x is built against
DIR=$(dirname "$0")/../web/public/artifacts
mkdir -p "$DIR"
for ext in wasm zkey; do
  curl -fsSL -o "$DIR/semaphore-$DEPTH.$ext" "https://snark-artifacts.pse.dev/semaphore/$VERSION/semaphore-$DEPTH.$ext"
done
if [ "$DEPTH" = 10 ]; then
  (cd "$DIR" && sha256sum -c - <<SUMS
1e9e1e3ad9917164bc96c751e32fae724400b92063f5a2cfd22ca64674bf4f96  semaphore-10.wasm
f644af6753be9d48454d8f9bb74bf2e9a416dd7d1a704c3cf1d50e535969a19a  semaphore-10.zkey
SUMS
  )
fi
