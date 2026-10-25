#!/usr/bin/env bash
# The admin network: ZeroTier on the VM, so the admin pages (Odoo's back office, Authentik's
# admin, the steward pages) answer only over it (ADMIN_NETS in infra/.env, proxy/Caddyfile).
# Run on the VM as the deploy user (uses sudo). The network itself lives in ZeroTier Central
# (my.zerotier.com): create it there as private, then authorise the VM and each admin device.
#
#   zerotier.sh install                 ZeroTier from its signed apt repository
#   zerotier.sh join <network id>       join; prints the VM's address and the ADMIN_NETS line
#   zerotier.sh status                  node and network state, the VM's ZeroTier addresses
#   zerotier.sh dns [network id]        DNS on the VM's ZeroTier address: *.<BELFRY_DOMAIN>
#                                       answers with that address, other names are forwarded
#
# After join: authorise the VM in ZeroTier Central, run `zerotier.sh status` until it shows an
# address, put the printed ADMIN_NETS into infra/.env and restart the proxy (infra/DEPLOY.md,
# "Admin network"). Then `zerotier.sh dns` and enter the DNS settings it prints for the network
# in ZeroTier Central, or as custom DNS in each device's ZeroTier app.
set -euo pipefail

# ZeroTier, Inc. release signing key (rsa4096, 2016-06-02)
ZT_KEY_FPR=74A5E9C458E1A431F1DA57A71657198823E52A61
ZT_KEY_URL="https://download.zerotier.com/contact%40zerotier.com.gpg"
KEYRING=/usr/share/keyrings/zerotier.gpg
# The VM itself: bootstrap and the smoke test on the VM reach the proxy from its own public
# addresses (the proxy sees real source addresses) or from Docker's ranges
local_nets() {
  echo "127.0.0.1/8 172.16.0.0/12 $(ip -o addr show scope global | awk '$3 == "inet" && $2 !~ /^(zt|docker|br-|veth)/ {split($4, a, "/"); printf "%s/32 ", a[1]} $3 == "inet6" && $2 !~ /^(zt|docker|br-|veth)/ {split($4, a, "/"); printf "%s/128 ", a[1]}')"
}

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
DNS_CONF=/etc/dnsmasq.d/belfry-zerotier.conf

die() { echo "zerotier.sh: $*" >&2; exit 1; }

zt_dev() { # network id → its ZeroTier interface
  sudo zerotier-cli -j listnetworks | python3 -c '
import json, sys
for n in json.load(sys.stdin):
    if n["id"] == sys.argv[1]:
        print(n.get("portDeviceName", ""))' "$1"
}

dns_check() { # <server> <name>: the IPv4 address the server gives for the name
  python3 -c '
import random, socket, struct, sys
server, name = sys.argv[1:]
qid = random.randrange(65536)
q = struct.pack(">6H", qid, 0x0100, 1, 0, 0, 0)
q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.sendto(q, (server, 53))
r = s.recv(512)
if struct.unpack(">H", r[:2])[0] != qid or struct.unpack(">H", r[6:8])[0] == 0:
    sys.exit(1)
print(socket.inet_ntoa(r[-4:]))' "$1" "$2"
}

zt_nets() { # the networks' assigned IPv4 subnets (from the routes ZeroTier installs)
  sudo zerotier-cli -j listnetworks | python3 -c '
import ipaddress, json, sys
for n in json.load(sys.stdin):
    for a in n.get("assignedAddresses", []):
        if ":" not in a:
            print(n["id"], a, ipaddress.ip_interface(a).network)'
}

case "${1:-}" in
  install)
    command -v zerotier-cli >/dev/null && { echo "ZeroTier is installed: $(zerotier-cli -v)"; exit 0; }
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    curl -fsSL "$ZT_KEY_URL" -o "$tmp/key.asc"
    fpr=$(gpg --show-keys --with-colons "$tmp/key.asc" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')
    [[ "$fpr" == "$ZT_KEY_FPR" ]] || die "unexpected signing key fingerprint '$fpr'"
    gpg --dearmor <"$tmp/key.asc" | sudo tee "$KEYRING" >/dev/null
    codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
    echo "deb [signed-by=$KEYRING] https://download.zerotier.com/debian/$codename $codename main" \
      | sudo tee /etc/apt/sources.list.d/zerotier.list >/dev/null
    sudo apt-get update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q zerotier-one
    sudo systemctl enable --now zerotier-one
    echo "installed: $(zerotier-cli -v); node id $(sudo zerotier-cli info | awk '{print $3}')"
    ;;
  join)
    nwid=${2:?network id (16 hex characters, from my.zerotier.com)}
    [[ "$nwid" =~ ^[0-9a-f]{16}$ ]] || die "not a ZeroTier network id: $nwid"
    sudo zerotier-cli join "$nwid"
    echo "Joined $nwid. Authorise node $(sudo zerotier-cli info | awk '{print $3}') in ZeroTier Central,"
    echo "then run: $0 status"
    ;;
  status)
    sudo zerotier-cli info
    sudo zerotier-cli listnetworks
    nets=$(zt_nets)
    if [[ -n "$nets" ]]; then
      echo
      echo "VM's ZeroTier addresses (admins map the public hostnames to it in their hosts file):"
      awk '{print "  " $1 "  " $2}' <<<"$nets"
      echo "Put this into infra/.env, then restart the proxy:"
      echo "  ADMIN_NETS=\"$(awk '{print $3}' <<<"$nets" | sort -u | tr '\n' ' ')$(local_nets | sed 's/ *$//')\""
      if [[ -f "$DNS_CONF" ]]; then
        ip=$(sed -n 's|^address=/[^/]*/||p' "$DNS_CONF")
        domain=$(sed -n 's|^address=/\([^/]*\)/.*|\1|p' "$DNS_CONF")
        if got=$(dns_check "$ip" "pass.$domain" 2>/dev/null); then
          echo "DNS on $ip: pass.$domain → $got"
        else
          echo "DNS on $ip: no answer (systemctl status dnsmasq)"
        fi
      fi
    else
      echo "No ZeroTier address yet: authorise this node in ZeroTier Central (and give the network an IPv4 pool)."
    fi
    ;;
  dns)
    domain=$(sed -n 's/^BELFRY_DOMAIN=//p' "$ROOT/infra/.env" | tr -d '"' | tail -1)
    [[ -n "$domain" ]] || die "BELFRY_DOMAIN is not set in infra/.env"
    nets=$(zt_nets)
    [[ -n "$nets" ]] || die "no ZeroTier address yet (run: $0 status)"
    [[ -z "${2:-}" ]] || nets=$(awk -v id="$2" '$1 == id' <<<"$nets")
    read -r nwid addr _ <<<"$(head -1 <<<"$nets")"
    [[ -n "${nwid:-}" ]] || die "not a member of network $2"
    ip=${addr%/*}
    dev=$(zt_dev "$nwid")
    [[ -n "$dev" ]] || die "no interface for network $nwid"
    # Only on the ZeroTier interface, never the public one (no open resolver); bind-dynamic, so
    # dnsmasq starts at boot before ZeroTier has brought the interface up. Other names go to
    # systemd-resolved's stub. Written before dnsmasq is installed: its first start then does not
    # try port 53 on every address.
    sudo mkdir -p /etc/dnsmasq.d
    printf '%s\n' "# Written by infra/scripts/zerotier.sh dns: the admin network's DNS (infra/DEPLOY.md)." \
      "interface=$dev" except-interface=lo bind-dynamic no-resolv no-hosts server=127.0.0.53 \
      domain-needed bogus-priv cache-size=1000 "address=/$domain/$ip" \
      | sudo tee "$DNS_CONF" >/dev/null
    if ! dpkg -s dnsmasq >/dev/null 2>&1; then
      sudo apt-get update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q dnsmasq
    fi
    sudo systemctl enable -q dnsmasq
    sudo systemctl restart dnsmasq
    got=$(dns_check "$ip" "pass.$domain") || die "dnsmasq does not answer on $ip (systemctl status dnsmasq)"
    [[ "$got" == "$ip" ]] || die "pass.$domain resolves to $got on $ip, expected $ip"
    echo "DNS on $ip ($dev): *.$domain → $ip, other names forwarded."
    echo "ZeroTier Central, network $nwid, DNS: search domain $domain, server $ip"
    echo "(or in a device's ZeroTier app: custom DNS for this network, server $ip)"
    ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
