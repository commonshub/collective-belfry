#!/usr/bin/env bash
# Belfry installer: a digital belfry for your community on a fresh VPS.
#
#   curl -fsSL https://belfry.sh/install.sh | sudo bash
#   curl -fsSL https://belfry.sh/install.sh | sudo bash -s -- --domain belfry.example.org
#
# It asks for what it needs at the start (or takes it from flags, BELFRY_* variables or a config
# file), checks the server and the DNS, and only then changes the server:
#   1. packages: Docker (get.docker.com when missing), ufw, git, age
#   2. ufw: SSH, 80, 443 and 25 (mail) in; everything else incoming denied
#   3. a user `belfry` (docker group, root's SSH keys), Belfry in /opt/belfry, a swap file on
#      small machines
#   4. settings and secrets: infra/.env and infra/secrets/ (every secret generated on this server)
#   5. Belfry's own proxy (Caddy) on 80 and 443: certificates, routing, admin and rate rules;
#      with --nginx, nginx owns 80 and 443 instead and passes TLS through to Caddy (shared servers)
#   6. the stack, bootstrap, the apps, the first steward, and the `belfry` command
# Run it again to resume or to change settings: it keeps what exists. Log: /var/log/belfry-install.log
#
# For agents, non-interactive: write the settings to a file (mode 600) and pass it with --config;
# secrets never go on the command line.
#
#   cat > /root/belfry.env <<'EOF'
#   BELFRY_DOMAIN=belfry.example.org          # required; A records for it and *.it → this server
#   BELFRY_EMAIL=ops@example.org              # optional; Let's Encrypt account (default admin@<domain>)
#   BELFRY_NAME="Example Commons"             # community name
#   BELFRY_RESEND_API_KEY=re_…                # mail (or BELFRY_SMTP_HOST/_PORT/_USER/_PASSWORD)
#   BELFRY_MAIL_FROM=belfry@example.org       # a sender your mail provider has verified
#   BELFRY_STRIPE_PUBLISHABLE_KEY=pk_test_…   # optional, paid passes
#   BELFRY_STRIPE_SECRET_KEY=sk_test_…
#   BELFRY_MOLLIE_API_KEY=test_…              # optional
#   BELFRY_ACCOUNTING_ODOO_URL=https://…      # optional: book paid passes in another Odoo
#   BELFRY_ACCOUNTING_ODOO_DB=…  BELFRY_ACCOUNTING_ODOO_API_KEY=…  BELFRY_ACCOUNTING_ODOO_ISSUER=…
#   BELFRY_CUSTODY_RECIPIENTS="age1… age1…"   # optional: stewards' age/YubiKey recipients
#   EOF
#   curl -fsSL https://belfry.sh/install.sh | bash -s -- --config /root/belfry.env --yes
#
# Later changes: `belfry config FILE` (same names), `belfry status`, `belfry help`.
# Docs: https://belfry.sh/docs/install/ · source: https://github.com/commonshub/collective-belfry
#
# Everything is inside main(), so a partial download never runs half a script.

set -euo pipefail

main() {
  local REPO_URL=${BELFRY_REPO:-https://github.com/commonshub/collective-belfry.git}
  local REF=${BELFRY_REF:-main}
  local DIR=${BELFRY_DIR:-/opt/belfry}
  local LOG=/var/log/belfry-install.log
  local ASSUME_YES=0 DRY_RUN=0 FORCE=0 HOST_ONLY=0 RUN_SMOKE=0 NGINX=${BELFRY_NGINX:-0} CUSTODY_ON_DISK=${BELFRY_CUSTODY_ON_DISK:-0}
  local CONFIG=''

  local bold='' dim='' green='' yellow='' red='' reset=''
  if [[ -t 1 ]]; then
    bold=$'\033[1m'; dim=$'\033[2m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; reset=$'\033[0m'
  fi
  ok()   { printf '  %s✓%s %s\n' "$green" "$reset" "$*"; }
  warn() { printf '  %s!%s %s\n' "$yellow" "$reset" "$*"; }
  bad()  { printf '  %s✗%s %s\n' "$red" "$reset" "$*"; }
  die()  { printf '\n%sbelfry install: %s%s\n' "$red" "$*" "$reset" >&2; exit 1; }
  step() { CURRENT_STEP="$*"; printf '\n%s%s%s\n' "$bold" "$*" "$reset"; }

  usage() {
    cat <<'EOF'
Usage: install.sh [options]          (as root; curl … | sudo bash -s -- [options])

  --domain DOMAIN          the belfry's domain (DNS: DOMAIN and *.DOMAIN → this server)
  --email EMAIL            the Let's Encrypt account (default admin@<domain>)
  --name NAME              the community's name
  --mail-from ADDRESS      sender of the mails (default belfry@DOMAIN)
  --config FILE            BELFRY_* settings, one KEY=VALUE per line (secrets go here)
  --custody-recipient R    an age or YubiKey recipient for the custody seed (repeat for each
                           steward); without one the seed stays on this server's disk
  --yes                    no questions: fail if something required is missing
  --dry-run                check the server and DNS, print the plan, change nothing
  --force                  continue despite failed checks (small server, DNS not ready)
  --odoo                   keep who is who in Odoo (the add-on: billing, volunteer shifts, CRM);
                           without it the belfry keeps its own register and runs no Odoo
  --nginx                  put nginx on 80/443 in front of Belfry's proxy, for a server that
                           also hosts other sites (default: Belfry's proxy, Caddy, on 80/443)
  --host-only              prepare the server (packages, ufw, nginx, user, code) without the stack
  --smoke                  run the end-to-end test at the end
  --ref REF                branch or tag of collective-belfry (default main)
  --dir DIR                where Belfry lives (default /opt/belfry)

Every option also works as a variable: BELFRY_DOMAIN, BELFRY_EMAIL, BELFRY_NAME, …
Secrets only from --config, the environment or a hidden prompt: BELFRY_RESEND_API_KEY,
BELFRY_SMTP_PASSWORD, BELFRY_STRIPE_SECRET_KEY, BELFRY_MOLLIE_API_KEY, BELFRY_ACCOUNTING_ODOO_API_KEY.
EOF
  }

  # ── Settings: config file, then environment, then flags ─────────────────────────────────────
  load_config() {
    local file=$1 line k v
    [[ -r "$file" ]] || die "cannot read $file"
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
      [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?(BELFRY_[A-Z0-9_]+)=(.*)$ ]] || die "$file: not a BELFRY_ setting: ${line%%=*}"
      k=${BASH_REMATCH[2]}; v=${BASH_REMATCH[3]}; v=${v%$'\r'}
      # a trailing comment after an unquoted value
      if [[ ! "$v" =~ ^[\"\'] ]]; then v=${v%%[[:space:]]#*}; fi
      v=${v%"${v##*[![:space:]]}"}
      if [[ "$v" =~ ^\"(.*)\"$ || "$v" =~ ^\'(.*)\'$ ]]; then v=${BASH_REMATCH[1]}; fi
      # the environment wins over the file
      [[ -n "${!k:-}" ]] || export "$k=$v"
    done < "$file"
  }

  local -a recipients=()
  local args=("$@") i=0
  while [[ $i -lt ${#args[@]} ]]; do
    local a=${args[$i]} val=${args[$((i+1))]:-}
    case "$a" in
      --domain) export BELFRY_DOMAIN=$val; i=$((i+1)) ;;
      --email) export BELFRY_EMAIL=$val; i=$((i+1)) ;;
      --name) export BELFRY_NAME=$val; i=$((i+1)) ;;
      --mail-from) export BELFRY_MAIL_FROM=$val; i=$((i+1)) ;;
      --config) CONFIG=$val; i=$((i+1)) ;;
      --custody-recipient) recipients+=("$val"); i=$((i+1)) ;;
      --custody-on-disk) CUSTODY_ON_DISK=1 ;;
      --ref) REF=$val; i=$((i+1)) ;;
      --dir) DIR=$val; i=$((i+1)) ;;
      --yes|-y) ASSUME_YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --force) FORCE=1 ;;
      --host-only) HOST_ONLY=1 ;;
      --smoke) RUN_SMOKE=1 ;;
      --nginx) NGINX=1 ;;
      --odoo) export BELFRY_REGISTER=odoo ;;
      -h|--help) usage; exit 0 ;;
      --*-key|--*-password|--*secret*) die "$a: secrets never go on the command line; use --config FILE or the BELFRY_* variable" ;;
      *) usage >&2; die "unknown option $a" ;;
    esac
    i=$((i+1))
  done
  [[ -n "$CONFIG" ]] && load_config "$CONFIG"
  if [[ ${#recipients[@]} -eq 0 && -n "${BELFRY_CUSTODY_RECIPIENTS:-}" ]]; then
    read -r -a recipients <<<"$BELFRY_CUSTODY_RECIPIENTS"
  fi

  # Questions go to the terminal even when the script itself comes from a pipe.
  local TTY=0
  if [[ $ASSUME_YES -eq 0 ]] && (: </dev/tty) 2>/dev/null; then TTY=1; fi
  ask() { # ask VAR "question" [default]
    local var=$1 q=$2 def=${3:-} ans=''
    [[ -n "${!var:-}" ]] && return 0
    if [[ $TTY -eq 1 ]]; then
      if [[ -n "$def" ]]; then printf '%s %s[%s]%s ' "$q" "$dim" "$def" "$reset" >/dev/tty; else printf '%s ' "$q" >/dev/tty; fi
      IFS= read -r ans </dev/tty || ans=''
    fi
    export "$var=${ans:-$def}"
  }
  ask_secret() { # ask_secret VAR "question": hidden, Enter skips
    local var=$1 q=$2 ans=''
    [[ -n "${!var:-}" || $TTY -eq 0 ]] && return 0
    printf '%s %s(hidden, Enter to skip)%s ' "$q" "$dim" "$reset" >/dev/tty
    IFS= read -rs ans </dev/tty || ans=''
    printf '\n' >/dev/tty
    [[ -n "$ans" ]] && export "$var=$ans"
    return 0
  }
  confirm() { # confirm "question" → 0 for yes
    [[ $ASSUME_YES -eq 1 ]] && return 0
    [[ $TTY -eq 1 ]] || die "no terminal to confirm on; run with --yes"
    local ans; printf '%s [y/N] ' "$1" >/dev/tty; IFS= read -r ans </dev/tty || ans=''
    [[ "$ans" =~ ^[yY] ]]
  }

  printf '\n%s  ▲  Belfry installer%s\n' "$bold" "$reset"
  printf '%s     a digital belfry for your community · https://belfry.sh%s\n' "$dim" "$reset"
  [[ $DRY_RUN -eq 1 ]] && printf '%s     dry run: nothing will be changed%s\n' "$yellow" "$reset"

  # ── 1. The server ────────────────────────────────────────────────────────────────────────────
  step "This server"
  local problems=0
  [[ "$(id -u)" -eq 0 ]] && ok "running as root" || { if [[ $DRY_RUN -eq 1 ]]; then warn "not root (needed for the real run)"; else die "run as root: curl -fsSL https://belfry.sh/install.sh | sudo bash"; fi; }
  local os_id='' os_ver='' os_name=''
  if [[ -r /etc/os-release ]]; then
    os_id=$(. /etc/os-release; echo "${ID:-}"); os_ver=$(. /etc/os-release; echo "${VERSION_ID:-}"); os_name=$(. /etc/os-release; echo "${PRETTY_NAME:-}")
  fi
  case "$os_id:$os_ver" in
    ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ok "$os_name" ;;
    ubuntu:*|debian:*) warn "$os_name: tested on Ubuntu 24.04/26.04 and Debian 12/13" ;;
    *) bad "${os_name:-unknown OS}: needs Ubuntu or Debian"; problems=$((problems+1)) ;;
  esac
  local arch; arch=$(uname -m)
  if [[ "$arch" == x86_64 ]]; then ok "amd64"; else bad "$arch: the relay image is amd64 only"; problems=$((problems+1)); fi
  local cpus mem_mb disk_gb swap_mb
  cpus=$(nproc 2>/dev/null || echo 1)
  mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
  swap_mb=$(awk '/SwapTotal/ {printf "%d", $2/1024}' /proc/meminfo)
  disk_gb=$(df -Pk / | awk 'NR==2 {printf "%d", $4/1048576}')
  # Measured on belfry.sh (2026-11): the whole stack runs in 3.8 GB with 1 GB of swap; 8 GB is
  # comfortable under real use.
  if [[ $mem_mb -ge 7400 ]]; then ok "$cpus vCPU, $((mem_mb/1024)) GB RAM"
  elif [[ $mem_mb -ge 3500 ]]; then warn "$cpus vCPU, $((mem_mb/1024)) GB RAM: it runs, tight; 8 GB is comfortable for real use"
  else bad "$cpus vCPU, ${mem_mb} MB RAM: the stack needs at least 4 GB (8 GB recommended)"; problems=$((problems+1)); fi
  # Images already on this server (a reinstall, or a second try) need no room again.
  local have_images=0
  if command -v docker >/dev/null 2>&1; then
    have_images=$(docker images --format '{{.Repository}}' 2>/dev/null | grep -c '^belfry-' || true)
  fi
  if [[ $disk_gb -ge 30 ]]; then ok "$disk_gb GB free on /"
  elif [[ $have_images -ge 8 && $disk_gb -ge 2 ]]; then ok "$disk_gb GB free on /, with Belfry's images already here ($have_images)"
  elif [[ $disk_gb -ge 15 ]]; then warn "$disk_gb GB free on /: enough with the prebuilt images (about 12 GB); a server under 25 GB builds one image at a time"
  else bad "$disk_gb GB free on /: needs 15 GB with the prebuilt images, 30 GB to build them here"; problems=$((problems+1)); fi
  local p busy=''
  for p in 80 443; do
    # free, or already ours: Belfry's proxy (docker-proxy) or, with --nginx, nginx
    if ss -Hltnp "sport = :$p" 2>/dev/null | grep -q . && ! ss -Hltnp "sport = :$p" 2>/dev/null | grep -qE '"docker-proxy"|"nginx"'; then busy="$busy $p"; fi
  done
  if [[ -z "$busy" ]]; then ok "ports 80 and 443 free (or already Belfry's)"
  else bad "port(s)$busy taken by another program; Belfry needs them (or --nginx, to share them through nginx)"; problems=$((problems+1)); fi
  local ip4 ip6
  ip4=$(curl -fsS4 --max-time 6 https://api.ipify.org 2>/dev/null || ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
  ip6=$(curl -fsS6 --max-time 6 https://api64.ipify.org 2>/dev/null || true)
  [[ "$ip6" == *:* ]] || ip6=''
  if [[ -n "$ip4" ]]; then ok "public address $ip4${ip6:+, $ip6}"; else bad "no public IPv4 address found"; problems=$((problems+1)); fi

  # ── 2. What the belfry needs to know ────────────────────────────────────────────────────────
  step "Your belfry"
  [[ $TTY -eq 1 ]] && printf '%sA domain like community.org, or a subdomain like belfry.community.org.%s\n' "$dim" "$reset"
  ask BELFRY_DOMAIN "Domain:"
  BELFRY_DOMAIN=$(printf '%s' "${BELFRY_DOMAIN:-}" | tr 'A-Z' 'a-z' | sed -E 's#^https?://##; s#/.*$##; s/^[[:space:]]+//; s/[[:space:]]+$//')
  [[ "$BELFRY_DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "a domain is required (--domain or BELFRY_DOMAIN); got \"$BELFRY_DOMAIN\""
  export BELFRY_DOMAIN
  # The first admin, mail, payments and the rest are set in the browser afterwards (the setup
  # link printed at the end); flags and BELFRY_* variables still set them here for agents.
  if [[ -n "${BELFRY_EMAIL:-}" && ! "$BELFRY_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then die "not an e-mail address: $BELFRY_EMAIL"; fi
  export BELFRY_EMAIL=${BELFRY_EMAIL:-admin@$BELFRY_DOMAIN}
  ask BELFRY_NAME "Community name:" "$BELFRY_DOMAIN"
  if [[ $HOST_ONLY -eq 0 && "${BELFRY_ASK_ALL:-}" == 1 ]]; then
    if [[ -z "${BELFRY_RESEND_API_KEY:-}${BELFRY_SMTP_HOST:-}" ]]; then
      [[ $TTY -eq 1 ]] && printf '%sMail sends the sign-in links. Resend: resend.com → API Keys (sending access).%s\n' "$dim" "$reset"
      ask_secret BELFRY_RESEND_API_KEY "Resend API key:"
    fi
    ask BELFRY_MAIL_FROM "Send mail as:" "belfry@$BELFRY_DOMAIN"
    if [[ -z "${BELFRY_STRIPE_SECRET_KEY:-}" && $TTY -eq 1 ]]; then
      printf '%sPaid passes need Stripe (or Mollie: BELFRY_MOLLIE_API_KEY). Add them later with belfry config.%s\n' "$dim" "$reset"
      ask_secret BELFRY_STRIPE_SECRET_KEY "Stripe secret key (sk_… or rk_…):"
      [[ -n "${BELFRY_STRIPE_SECRET_KEY:-}" ]] && ask BELFRY_STRIPE_PUBLISHABLE_KEY "Stripe publishable key (pk_…):"
    fi
    if [[ -z "${BELFRY_ACCOUNTING_ODOO_URL:-}" && $TTY -eq 1 ]]; then
      printf '%sBelfry brings its own Odoo. An Odoo elsewhere (for example the ticket seller) can also get each paid pass booked.%s\n' "$dim" "$reset"
      ask BELFRY_ACCOUNTING_ODOO_URL "That Odoo's URL (Enter to skip):"
      if [[ -n "${BELFRY_ACCOUNTING_ODOO_URL:-}" ]]; then
        ask BELFRY_ACCOUNTING_ODOO_DB "Its database name:"
        ask BELFRY_ACCOUNTING_ODOO_ISSUER "The organisation's name:"
        ask_secret BELFRY_ACCOUNTING_ODOO_API_KEY "Its API key:"
      fi
    fi
    if [[ ${#recipients[@]} -eq 0 && $CUSTODY_ON_DISK -eq 0 && $TTY -eq 1 ]]; then
      printf '%sMember keys derive from a custody seed. Give the age or YubiKey recipients to keep it\nencrypted to them and only in memory here, or press Enter to keep it on this disk for now.%s\n' "$dim" "$reset"
      local r=''
      ask r "Recipients (age1…, space separated):"
      [[ -n "$r" ]] && read -r -a recipients <<<"$r"
    fi
    local rr
    for rr in "${recipients[@]}"; do
      [[ "$rr" =~ ^age1[0-9a-z]+$ ]] || die "not an age recipient: $rr"
    done
  fi

  # ── 3. DNS ───────────────────────────────────────────────────────────────────────────────────
  step "DNS"
  local probe="relay.$BELFRY_DOMAIN" a w
  a=$(getent ahostsv4 "$BELFRY_DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)
  w=$(getent ahostsv4 "$probe" 2>/dev/null | awk 'NR==1 {print $1}' || true)
  local dns_ok=1
  if [[ "$a" == "$ip4" ]]; then ok "$BELFRY_DOMAIN → $a"; else bad "$BELFRY_DOMAIN → ${a:-nothing} (this server is $ip4)"; dns_ok=0; fi
  if [[ "$w" == "$ip4" ]]; then ok "*.$BELFRY_DOMAIN → $w ${dim}(checked with $probe)${reset}"; else bad "*.$BELFRY_DOMAIN → ${w:-nothing} (checked with $probe)"; dns_ok=0; fi
  if [[ $dns_ok -eq 0 ]]; then
    problems=$((problems+1))
    printf '\n  At your DNS provider, add:\n    %s%-28s A     %s%s\n    %s%-28s A     %s%s\n' "$bold" "$BELFRY_DOMAIN" "$ip4" "$reset" "$bold" "*.$BELFRY_DOMAIN" "$ip4" "$reset"
    [[ -n "$ip6" ]] && printf '    %-28s AAAA  %s\n    %-28s AAAA  %s\n' "$BELFRY_DOMAIN" "$ip6" "*.$BELFRY_DOMAIN" "$ip6"
    printf '  Certificates are requested per name, so both must point here before the stack starts.\n'
  fi

  # ── 4. The plan ──────────────────────────────────────────────────────────────────────────────
  step "Plan"
  local custody_desc
  if [[ ${#recipients[@]} -gt 0 ]]; then custody_desc="encrypted to ${#recipients[@]} recipient(s), in memory only on this server (unlock after each reboot)"
  else custody_desc="on this server's disk (move it under the stewards' YubiKeys later: infra/DEPLOY.md)"; fi
  cat <<EOF
  domain         https://$BELFRY_DOMAIN  (and belfry., chat., login., pass., apps., relay., …)
  community      $BELFRY_NAME
  first admin    created in the browser with the setup link (printed at the end)
  mail           $( if [[ -n "${BELFRY_RESEND_API_KEY:-}" ]]; then echo "Resend, from ${BELFRY_MAIL_FROM:-}"; elif [[ -n "${BELFRY_SMTP_HOST:-}" ]]; then echo "SMTP $BELFRY_SMTP_HOST, from ${BELFRY_MAIL_FROM:-}"; else echo "none yet: added on the setup page"; fi )
  payments       $( [[ -n "${BELFRY_STRIPE_SECRET_KEY:-}" ]] && printf 'Stripe (%s) ' "$( [[ "$BELFRY_STRIPE_SECRET_KEY" == *_live_* ]] && echo live || echo test )"; [[ -n "${BELFRY_MOLLIE_API_KEY:-}" ]] && printf 'Mollie '; [[ -z "${BELFRY_STRIPE_SECRET_KEY:-}${BELFRY_MOLLIE_API_KEY:-}" ]] && printf 'none: stewards mark payments by hand' ; echo )
  accounting     ${BELFRY_ACCOUNTING_ODOO_URL:-$( [[ "${BELFRY_REGISTER:-}" == odoo ]] && echo "the bundled Odoo only" || echo "none" )}
  custody seed   $custody_desc
  code           $REPO_URL @ $REF → $DIR
  server         Docker, ufw (SSH, 80, 443, 25 for mail), user belfry$( [[ $NGINX -eq 1 ]] && echo ", nginx on 80/443 → Belfry's proxy" )$( [[ $mem_mb -lt 12000 && $swap_mb -eq 0 && $disk_gb -ge 12 ]] && echo ", 4 GB swap" )
EOF
  [[ $HOST_ONLY -eq 1 ]] && printf '  %s--host-only: the stack is not started%s\n' "$yellow" "$reset"

  if [[ $DRY_RUN -eq 1 ]]; then
    if [[ $problems -eq 0 ]]; then printf '\n%sReady. Run again without --dry-run to install.%s\n\n' "$green" "$reset"
    else printf '\n%s%d thing(s) to fix first (or --force).%s\n\n' "$yellow" "$problems" "$reset"; fi
    return 0
  fi
  if [[ $problems -gt 0 && $FORCE -eq 0 ]]; then
    if [[ $HOST_ONLY -eq 1 ]]; then warn "continuing: --host-only does not start the stack"
    else die "$problems check(s) failed above. Fix them, or add --force to go on anyway (--host-only prepares the server without the stack)"; fi
  fi
  confirm "Install Belfry on this server?" || die "nothing changed"

  exec > >(tee -a "$LOG") 2>&1
  # main's locals are gone when the EXIT trap runs: it uses the global CURRENT_STEP only
  CURRENT_STEP='start'
  # shellcheck disable=SC2154
  trap 'rc=$?; if [[ $rc -ne 0 ]]; then printf "\nbelfry install: stopped during \"%s\" (exit %s). Fix what the lines above say and run the installer again: it resumes. Log: /var/log/belfry-install.log\n" "${CURRENT_STEP:-?}" "$rc"; fi' EXIT
  printf '\n--- %s install.sh %s\n' "$(date -Is)" "$*" >>"$LOG"

  # ── 5. Packages ─────────────────────────────────────────────────────────────────────────────
  step "Packages"
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
  apt-get update -qq
  local pkgs=(ca-certificates curl git ufw python3 openssl age jq unattended-upgrades)
  [[ $NGINX -eq 1 ]] && pkgs+=(nginx libnginx-mod-stream)
  apt-get install -y -qq "${pkgs[@]}" >/dev/null
  ok "${pkgs[*]:1}"
  if ! command -v docker >/dev/null; then
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh >>"$LOG" 2>&1 || die "Docker install failed: see $LOG"
    rm -f /tmp/get-docker.sh
  fi
  local cv; cv=$(docker compose version --short 2>/dev/null || echo 0)
  if ! printf '%s\n%s\n' 2.33.0 "${cv#v}" | sort -V -C; then
    die "Docker Compose $cv is too old (2.33 or newer); install Docker from docker.com (https://docs.docker.com/engine/install/)"
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), Compose $cv"

  if [[ $swap_mb -eq 0 && $((mem_mb)) -lt 12000 && ! -e /swapfile && $disk_gb -ge 12 ]]; then
    fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "4 GB swap file (the signer's build needs the room)"
  fi

  # ── 6. Firewall ─────────────────────────────────────────────────────────────────────────────
  step "Firewall (ufw)"
  local sshports
  sshports=$(sshd -T 2>/dev/null | awk '$1=="port" {print $2}' | sort -u || true)
  [[ -n "$sshports" ]] || sshports=22
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  for p in $sshports; do ufw allow "$p/tcp" comment 'SSH' >/dev/null; done
  ufw allow 80/tcp comment 'Belfry' >/dev/null
  ufw allow 443/tcp comment 'Belfry' >/dev/null
  [[ $NGINX -eq 1 ]] || ufw allow 443/udp comment 'Belfry (HTTP/3)' >/dev/null
  # incoming mail for name@ and channel@<domain> (the mail-in service; Docker publishes it too)
  ufw allow 25/tcp comment 'Belfry mail' >/dev/null
  ufw --force enable >/dev/null
  ok "in: SSH ($(echo $sshports | tr ' ' ',')), 80, 443, 25 (mail); everything else denied"
  # not `ufw limit` on SSH: agents open many connections in a row and would lock themselves out

  # ── 7. The belfry user and the code ─────────────────────────────────────────────────────────
  step "Code"
  if ! id belfry >/dev/null 2>&1; then
    useradd -m -s /bin/bash belfry
    ok "user belfry"
  fi
  usermod -aG docker belfry
  if [[ -s /root/.ssh/authorized_keys ]]; then
    install -d -m 700 -o belfry -g belfry /home/belfry/.ssh
    touch /home/belfry/.ssh/authorized_keys
    sort -u /root/.ssh/authorized_keys /home/belfry/.ssh/authorized_keys -o /home/belfry/.ssh/authorized_keys
    chown belfry:belfry /home/belfry/.ssh/authorized_keys && chmod 600 /home/belfry/.ssh/authorized_keys
    ok "root's SSH keys also open belfry@ (stewards unlock the custody seed as that user)"
  fi
  # custody-seed.sh unlock writes /run/belfry with sudo as this user
  printf 'belfry ALL=(root) NOPASSWD: /usr/bin/install\n' > /etc/sudoers.d/belfry
  chmod 440 /etc/sudoers.d/belfry
  as_belfry() { runuser -u belfry -- "$@"; }
  if [[ -d "$DIR/.git" ]]; then
    ok "$DIR exists ($(git -C "$DIR" rev-parse --short HEAD)); keeping it (belfry update moves it on)"
  else
    install -d -o belfry -g belfry "$DIR"
    as_belfry git clone -q --branch "$REF" "$REPO_URL" "$DIR"
    ok "$REPO_URL @ $REF → $DIR"
  fi
  ln -sf "$DIR/infra/scripts/belfry" /usr/local/bin/belfry
  ok "the belfry command (belfry help)"

  # ── 8. Settings and secrets ─────────────────────────────────────────────────────────────────
  step "Settings"
  cd "$DIR"
  local fresh=0
  [[ -f infra/.env ]] || fresh=1
  as_belfry infra/scripts/gen-secrets.sh >/dev/null
  as_belfry env MAIL_FROM="${BELFRY_MAIL_FROM:-}" infra/scripts/prod-env.sh "$BELFRY_DOMAIN" "$BELFRY_EMAIL" >/dev/null
  if [[ $fresh -eq 1 ]]; then
    # the issuer and verifier certificates must carry the public hostnames: new ones, before any
    # pass exists (DEPLOY.md, "First installation")
    rm -f infra/issuer/keys/issuer.* infra/issuer/keys/verifier.* infra/issuer/keys/ca.*
    as_belfry infra/scripts/gen-secrets.sh >/dev/null
  fi
  if [[ $NGINX -eq 1 ]]; then : > infra/.nginx && chown belfry:belfry infra/.nginx; else rm -f infra/.nginx; fi
  ok "infra/.env for $BELFRY_DOMAIN, secrets generated in infra/secrets/"
  # A new belfry's apex shows its own website (an nsite), and a placeholder that explains how to
  # publish it until then; HOMEPAGE in belfry.env (BELFRY_ENV_HOMEPAGE) chooses otherwise.
  if [[ $fresh -eq 1 ]]; then export BELFRY_ENV_HOMEPAGE=${BELFRY_ENV_HOMEPAGE:-nsite}; fi
  # New belfries wear Belfry's look and the community's name, and run without Open Source
  # Village's apps (OSV_APPS=0, written by `belfry apply`); the community's own colours, fonts
  # and logo: a theme folder, `belfry theme set DIR` (skills/belfry-theme).
  if [[ $fresh -eq 1 ]]; then export BELFRY_THEME=${BELFRY_THEME:-belfry}; fi
  # New belfries keep their own register (people, passes, invites) and run no Odoo; --odoo
  # keeps Odoo as the add-on (BELFRY_REGISTER=odoo).
  if [[ $fresh -eq 1 ]]; then export BELFRY_REGISTER=${BELFRY_REGISTER:-belfry}; fi
  # The admin pages answer this server's own addresses (bootstrap and smoke run here) and Docker's;
  # add the ZeroTier subnet later (DEPLOY.md, "Admin network").
  local admin_nets="127.0.0.1/8 172.16.0.0/12 $ip4/32${ip6:+ $ip6/128}"
  local zt; zt=$(sed -nE 's/^ADMIN_NETS="?([^"]*)"?/\1/p' infra/.env | tail -n1)
  [[ -n "$zt" ]] && admin_nets=$(printf '%s %s' "$zt" "$admin_nets" | tr ' ' '\n' | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/ $//')
  export BELFRY_ENV_ADMIN_NETS="$admin_nets"
  # custody seed
  if [[ ${#recipients[@]} -gt 0 ]]; then
    export BELFRY_ENV_CUSTODY_SEED_DIR=/run/belfry
    if [[ ! -f infra/secrets/custody-seed.age ]]; then
      local seedfile=infra/secrets/custody/custody-seed rargs=() rr
      for rr in "${recipients[@]}"; do rargs+=(-r "$rr"); done
      [[ -s "$seedfile" ]] || as_belfry sh -c "umask 077; mkdir -p infra/secrets/custody; openssl rand -hex 32 > $seedfile"
      as_belfry age "${rargs[@]}" -o infra/secrets/custody-seed.age "$seedfile"
      install -d -m 700 -o belfry -g belfry /run/belfry
      install -m 400 -o belfry -g belfry "$seedfile" /run/belfry/custody-seed
      shred -u "$seedfile"
      ok "custody seed encrypted to ${#recipients[@]} recipient(s): $DIR/infra/secrets/custody-seed.age (copy it to every steward); unlocked in memory"
    else
      ok "custody seed: infra/secrets/custody-seed.age exists, keeping it"
      [[ -s /run/belfry/custody-seed ]] || warn "the seed is locked: a steward runs infra/scripts/custody-seed.sh unlock belfry@$BELFRY_DOMAIN …"
    fi
  else
    export BELFRY_ENV_CUSTODY_SEED_DIR=''
    if [[ -s infra/secrets/custody/custody-seed ]]; then ok "custody seed on this disk (infra/secrets/custody/)"
    else warn "no custody seed on this disk and none unlocked: new members wait (infra/DEPLOY.md, \"Custody seed\")"; fi
  fi
  # everything else through the same code as `belfry config`
  BELFRY_NO_APPLY=1 runuser -u belfry -- \
    "$DIR/infra/scripts/belfry" config | sed 's/^/  /'

  # ── 9. nginx ────────────────────────────────────────────────────────────────────────────────
  if [[ $NGINX -eq 1 ]]; then
  step "nginx"
  install -d /etc/nginx/stream.d
  sed "s/@@DOMAIN@@/$BELFRY_DOMAIN/g" infra/nginx/belfry-stream.conf > /etc/nginx/stream.d/belfry.conf
  sed "s/@@DOMAIN@@/$BELFRY_DOMAIN/g" infra/nginx/belfry-http.conf > /etc/nginx/sites-available/belfry
  ln -sf /etc/nginx/sites-available/belfry /etc/nginx/sites-enabled/belfry
  if ! grep -q 'stream.d/\*.conf' /etc/nginx/nginx.conf; then
    printf '\n# Belfry (install.sh): TLS passthrough by name, /etc/nginx/stream.d/\nstream {\n    include /etc/nginx/stream.d/*.conf;\n}\n' >> /etc/nginx/nginx.conf
  fi
  # the stock site listens on 80 as default_server; Belfry's server block takes port 80 for its names
  if [[ "$(readlink -f /etc/nginx/sites-enabled/default 2>/dev/null)" == /etc/nginx/sites-available/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
  fi
  nginx -t 2>&1 | tail -1
  systemctl enable nginx >/dev/null 2>&1
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
  ok "80 → HTTPS (Let's Encrypt challenges to Belfry), 443 → Belfry's proxy, passed through with the visitor's address"
  fi

  if [[ $HOST_ONLY -eq 1 ]]; then
    step "Server ready"
    printf '  The stack is not started (--host-only). When the server has 8 GB: %sbelfry apply%s\n\n' "$bold" "$reset"
    return 0
  fi

  # ── 10. The stack ───────────────────────────────────────────────────────────────────────────
  step "The stack (the first start builds the signer: 15 to 30 minutes)"
  runuser -u belfry -- "$DIR/infra/scripts/belfry" apply
  "$DIR/infra/scripts/belfry" setup-units >/dev/null || warn "the setup page's watcher is not installed; run: belfry setup-units"
  SETUP_LINK=$(runuser -u belfry -- "$DIR/infra/scripts/belfry" setup-code </dev/null | sed -n 's/^  \(https:.*\)/\1/p') || true
  if [[ $RUN_SMOKE -eq 1 ]]; then
    step "Smoke test"
    runuser -u belfry -- "$DIR/infra/scripts/belfry" smoke || warn "the smoke test failed: see above and $LOG"
  fi

  step "Your belfry is up"
  if [[ -n "${SETUP_LINK:-}" ]]; then
    printf '\n  %sOpen this link to finish (valid 24 hours):%s\n\n    %s\n\n' "$bold" "$reset" "$SETUP_LINK"
    printf '  It makes you the first admin, then asks for the key of a mail provider (Lettermint or\n  Resend) so members can get their sign-in codes by e-mail.\n\n'
  fi
  cat <<EOF
  https://belfry.$BELFRY_DOMAIN/setup     settings (belfry setup-code makes a new link)
  https://belfry.$BELFRY_DOMAIN           this belfry's status and members
  https://$BELFRY_DOMAIN                  the homepage (a placeholder until you publish one)
  https://$BELFRY_DOMAIN/.well-known/belfry.json   what apps and agents read
  https://pass.$BELFRY_DOMAIN/steward     steward pages (admin network only: infra/DEPLOY.md)

  belfry status       what runs and what is configured
  belfry config FILE  add mail, Stripe, Mollie or accounting later (BELFRY_* lines)
  belfry help         everything else

  Still to do by hand (infra/DEPLOY.md): the admin network (ZeroTier), backups, and moving the
  custody seed under the stewards' YubiKeys if it is still on this disk.
EOF
  true
  printf '\n'
}

main "$@"
