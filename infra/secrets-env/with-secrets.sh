#!/bin/sh
# Entrypoint wrapper for images that read their secrets only from environment variables (Odoo,
# Keycast, the mint). SECRETS_ENV lists VAR=file pairs: each file under /run/secrets (a Compose
# secret) is exported as VAR, then the image's own entrypoint runs. The values are then in the
# process environment of this container only, not in its configuration (docker inspect) or in
# infra/.env. infra/README.md, "Secrets".
set -eu
for pair in ${SECRETS_ENV:-}; do
  var=${pair%%=*}
  file=/run/secrets/${pair#*=}
  [ -r "$file" ] || { echo "with-secrets: cannot read $file for $var" >&2; exit 1; }
  value=$(cat "$file")
  export "$var=$value"
done
unset SECRETS_ENV
exec "$@"
