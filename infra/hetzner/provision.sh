#!/usr/bin/env bash
# Create Belfry's VM on Hetzner Cloud with the hcloud CLI (active context = the project's token).
# Idempotent: existing resources with the same names are kept.
#
#   infra/hetzner/provision.sh            create SSH key, firewall and server, print DNS records
#   DRY_RUN=1 infra/hetzner/provision.sh  only print what would be created
#
# Settings (env): SERVER_NAME (belfry), SERVER_TYPE (cx33: 4 vCPU, 8 GB), LOCATION (nbg1; cx33 was sold out in fsn1 and hel1 on 2026-10-23),
# IMAGE (ubuntu-24.04), SSH_PUBKEY (~/.ssh/id_ed25519.pub), SSH_KEY_NAME (belfry-$USER),
# BELFRY_DOMAIN (belfry.tofauti.net; only used for the DNS records printed at the end).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_NAME=${SERVER_NAME:-belfry}
SERVER_TYPE=${SERVER_TYPE:-cx33}
LOCATION=${LOCATION:-nbg1}
IMAGE=${IMAGE:-ubuntu-24.04}
SSH_PUBKEY=${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}
SSH_KEY_NAME=${SSH_KEY_NAME:-belfry-$USER}
FIREWALL=${FIREWALL:-belfry}
BELFRY_DOMAIN=${BELFRY_DOMAIN:-belfry.tofauti.net}

run() { if [[ -n "${DRY_RUN:-}" ]]; then echo "+ $*"; else "$@"; fi; }
exists() { hcloud "$1" describe "$2" >/dev/null 2>&1; }

command -v hcloud >/dev/null || { echo "hcloud CLI not found" >&2; exit 1; }
hcloud context active >/dev/null || { echo "no active hcloud context (hcloud context create belfry)" >&2; exit 1; }

if exists ssh-key "$SSH_KEY_NAME"; then
  echo "ssh key $SSH_KEY_NAME exists"
else
  run hcloud ssh-key create --name "$SSH_KEY_NAME" --public-key-from-file "$SSH_PUBKEY"
fi

if exists firewall "$FIREWALL"; then
  echo "firewall $FIREWALL exists"
else
  run hcloud firewall create --name "$FIREWALL" --rules-file "$HERE/firewall.json"
fi

if exists server "$SERVER_NAME"; then
  echo "server $SERVER_NAME exists"
else
  run hcloud server create --name "$SERVER_NAME" --type "$SERVER_TYPE" --location "$LOCATION" \
    --image "$IMAGE" --ssh-key "$SSH_KEY_NAME" --firewall "$FIREWALL" \
    --user-data-from-file "$HERE/cloud-init.yaml" --label app=belfry
fi

[[ -n "${DRY_RUN:-}" ]] && exit 0
ipv4=$(hcloud server ip "$SERVER_NAME")
ipv6=$(hcloud server ip -6 "$SERVER_NAME")
cat <<EOF

Server $SERVER_NAME: $ipv4 / $ipv6
DNS records (at the domain's DNS provider):
  $BELFRY_DOMAIN.      A     $ipv4
  $BELFRY_DOMAIN.      AAAA  $ipv6
  *.$BELFRY_DOMAIN.    A     $ipv4
  *.$BELFRY_DOMAIN.    AAAA  $ipv6
Cloud-init is done when this succeeds:  ssh belfry@$ipv4 test -f /var/lib/belfry-cloud-init-done
EOF
