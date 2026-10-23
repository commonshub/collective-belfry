#!/usr/bin/env bash
# Point every public URL in infra/.env at the reverse proxy hostnames of a public deployment.
#
#   infra/scripts/prod-env.sh <domain> <acme e-mail>
#
# Hostnames (proxy/Caddyfile): shop. (Odoo; ODOO_HOST_PREFIX=odoo keeps the old name), login., apps., relay., bunker., issuer., verifier.,
# pass., calendar.<domain>; NIP-05 names are <member>@<domain>. Re-run gen-secrets.sh afterwards so the
# issuer and verifier certificates carry the public hostnames, then bootstrap.
set -euo pipefail
DOMAIN=${1:?domain, e.g. belfry.example}
ACME=${2:?ACME e-mail (certificate notifications)}
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$INFRA_DIR/.env"
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE; run scripts/gen-secrets.sh first" >&2; exit 1; }

python3 - "$ENV_FILE" "$DOMAIN" "$ACME" "${ODOO_HOST_PREFIX:-shop}" <<'PY'
import re, sys
path, domain, acme, shop = sys.argv[1:]
values = {
    "BELFRY_DOMAIN": domain,
    "ACME_EMAIL": acme,
    "ODOO_HOST_PREFIX": shop,
    "ODOO_PUBLIC_URL": f"https://{shop}.{domain}",
    "AUTHENTIK_PUBLIC_URL": f"https://login.{domain}",
    "KEYCAST_APP_URL": f"https://apps.{domain}",
    "KEYCAST_TENANT_DOMAIN": f"apps.{domain}",
    "BUNKER_PUBLIC_RELAYS": f"wss://bunker.{domain}",
    "NIP05_DOMAIN": domain,
    "ISSUER_PUBLIC_URL": f"https://issuer.{domain}",
    "ISSUER_SAN": f"issuer.{domain}",
    "VERIFIER_PUBLIC_URL": f"https://verifier.{domain}",
    "VERIFIER_SAN": f"verifier.{domain}",
    "ONBOARDING_PUBLIC_URL": f"https://pass.{domain}",
    "STATUS_LIST_URL": f"https://pass.{domain}/status/1",
    "STATUS_LIST_TTL": "300",
    "MAIL_FROM": f"belfry@{domain}",
    "BLOSSOM_PUBLIC_DOMAIN": f"blossom.{domain}",
    "COMMUNITY_RELAY_URL": f"wss://relay.{domain}",
    "CALENDAR_PUBLIC_URL": f"https://calendar.{domain}",
}
lines = open(path).read().splitlines(keepends=True)
seen = set()
for i, line in enumerate(lines):
    m = re.match(r"^([A-Z0-9_]+)=([^#\n]*?)(\s*#.*)?\n?$", line)
    if m and m.group(1) in values:
        lines[i] = f"{m.group(1)}={values[m.group(1)]}{m.group(3) or ''}\n"
        seen.add(m.group(1))
missing = [k for k in values if k not in seen]
if missing:
    lines.append("\n# --- public deployment (scripts/prod-env.sh)\n")
    lines += [f"{k}={values[k]}\n" for k in missing]
open(path, "w").write("".join(lines))
print("set: " + ", ".join(values))
PY
echo "next: infra/scripts/gen-secrets.sh (certificates with the public hostnames; delete infra/issuer/keys/verifier.* and issuer.* first if they exist with the local names), then"
echo "      docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml up -d && infra/scripts/bootstrap.sh"
