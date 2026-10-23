#!/usr/bin/env bash
# Store one secret on the deployment server as infra/secrets/<name> (mode 600, gitignored).
# The value travels only over the SSH connection's stdin: it is never a command-line argument,
# never in shell history and never in .env (whose parsers would break on #, quotes or spaces).
#
#   infra/scripts/put-secret.sh belfry@<host> smtp_password            # prompts, input hidden
#   pass show mail/belfry | infra/scripts/put-secret.sh belfry@<host> smtp_password
#
# Secrets read this way: smtp_password (bootstrap.sh, Odoo's outgoing mail server).
# Optional third argument: the repo checkout on the server (default: belfry).
set -euo pipefail
TARGET=${1:?ssh target, e.g. belfry@188.245.110.191}
NAME=${2:?secret name, e.g. smtp_password}
REPO=${3:-belfry}
[[ "$NAME" =~ ^[a-z0-9_]+$ ]] || { echo "secret name: lower-case letters, digits, _ only" >&2; exit 1; }

if [[ -t 0 ]]; then
  read -rsp "$NAME: " value; echo
  read -rsp "$NAME (again): " again; echo
  [[ "$value" == "$again" ]] || { echo "the two entries differ" >&2; exit 1; }
else
  value=$(cat)
  value=${value%$'\n'}   # a trailing newline from echo or a password manager is not part of it
fi
[[ -n "$value" ]] || { echo "empty value" >&2; exit 1; }

printf '%s' "$value" | ssh "$TARGET" \
  "umask 077 && mkdir -p '$REPO/infra/secrets' && cat > '$REPO/infra/secrets/$NAME.tmp' && mv '$REPO/infra/secrets/$NAME.tmp' '$REPO/infra/secrets/$NAME'"
echo "stored $REPO/infra/secrets/$NAME on $TARGET (${#value} characters)"
