#!/bin/sh
# Renders zooid's config for the one virtual relay Belfry runs and starts zooid.
# Inputs: GROUPS_RELAY_URL (the relay as browsers reach it; zooid serves the relay only on that
# Host header), GROUPS_API_PUBKEY (the onboarding service's key: owner and the only API caller),
# GROUPS_BRAND_ICON, and the secret files groups_relay_nsec (the relay's own key, hex) and
# zooid_db_password.
set -eu

: "${GROUPS_RELAY_URL:?}" "${GROUPS_API_PUBKEY:?}"
secret() { tr -d ' \n' < "/run/secrets/$1"; }

host=$(printf '%s' "$GROUPS_RELAY_URL" | sed -E 's#^wss?://##; s#/.*$##')
relay_secret=$(secret groups_relay_nsec)
printf '%s' "$relay_secret" | grep -Eq '^[0-9a-f]{64}$' || { echo "groups_relay_nsec is not a hex key" >&2; exit 1; }
printf '%s' "$GROUPS_API_PUBKEY" | grep -Eq '^[0-9a-f]{64}$' || { echo "GROUPS_API_PUBKEY is not a hex key" >&2; exit 1; }

mkdir -p "$CONFIG" "$MEDIA"
# The schema names the relay's data and must never change; the file name is the API's relay id.
cat > "$CONFIG/groups.toml" <<TOML
host = "$host"
schema = "osv_groups"
secret = "$relay_secret"

[info]
name = "Open Source Village"
icon = "${GROUPS_BRAND_ICON:-}"
pubkey = "$GROUPS_API_PUBKEY"
description = "Groups of Open Source Village, run by Belfry. Every participant with an active pass is a member."

[policy]
public_join = false
public_read = false

[groups]
enabled = true
member_create = true

[management]
enabled = true

[blossom]
enabled = false

[push]
enabled = false
TOML

# The member API is reachable only on the internal host name; Caddy forwards the public one.
export API_HOST="${GROUPS_API_HOST:-groups-relay:3334}"
export API_WHITELIST="$GROUPS_API_PUBKEY"
export DATABASE_URL="postgres://zooid:$(secret zooid_db_password)@${ZOOID_DB_HOST:-postgres}:5432/zooid?sslmode=disable"
exec /bin/zooid
