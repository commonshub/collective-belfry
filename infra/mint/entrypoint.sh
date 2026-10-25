#!/bin/sh
# Belfry: render the mint's config and import it into its database, then run the mint.
set -eu
# The seed is a Compose secret; cdk-mintd reads it from this variable only (config.toml: env:).
CDK_MINTD_SEED=$(cat /run/secrets/mint_seed)
export CDK_MINTD_SEED
WORK=/data
mkdir -p "$WORK"
sed -e "s|@MINT_PUBLIC_URL@|${MINT_PUBLIC_URL:?}|" -e "s|@BRAND_URL@|${BELFRY_BRAND_URL:?}|" \
    -e "s|@MINT_UNIT@|${MINT_UNIT:-osv}|" /config/config.toml.tmpl > "$WORK/config.toml"
if cdk-mintd --work-dir "$WORK" config show >/dev/null 2>&1; then
  cdk-mintd --work-dir "$WORK" config apply --file "$WORK/config.toml"
else
  cdk-mintd --work-dir "$WORK" config init --new-mint --file "$WORK/config.toml"
fi
exec cdk-mintd --work-dir "$WORK"
