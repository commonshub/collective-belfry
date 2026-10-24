#!/usr/bin/env bash
# The custody seed participant keys derive from (Keycast patch 0014), kept under the stewards'
# YubiKeys and on the VM only in memory. Run on a steward's laptop.
#
#   custody-seed.sh recipients                 list this laptop's YubiKey recipients (age1yubikey1…)
#   custody-seed.sh init <out.age> <recipient>…
#                                              create a new seed, encrypted to every recipient
#                                              (either YubiKey decrypts); the seed is never printed
#   custody-seed.sh unlock <ssh host> <seed.age> [identity file]…
#                                              decrypt with the YubiKey (touch/PIN) and put the seed
#                                              into /run/belfry/custody-seed on the host (memory)
#   custody-seed.sh status <ssh host>          is the seed unlocked on the host?
#   custody-seed.sh lock <ssh host>            remove it from the host's memory
#
# Needs age (https://age-encryption.org, `apt install age`) and age-plugin-yubikey
# (https://github.com/str4d/age-plugin-yubikey) with a PIV slot set up on each YubiKey:
# `age-plugin-yubikey` (interactive) once per key, then `age-plugin-yubikey --identity > id.txt`
# for the identity file `unlock` takes (it only names the YubiKey slot; the secret stays on it).
# AGE=<path> picks another age binary. Keep <seed.age> in the backups and with each steward:
# without it and a YubiKey nobody can recover participant keys.
set -euo pipefail
AGE=${AGE:-age}
die() { echo "custody-seed: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "$1 not found (see the header of this script)"; }

cmd=${1:-}; shift || true
case "$cmd" in
  recipients)
    need age-plugin-yubikey
    age-plugin-yubikey --list
    ;;
  init)
    out=${1:?usage: init <out.age> <recipient>…}; shift
    [[ $# -ge 1 ]] || die "name at least one recipient (two YubiKeys: two recipients)"
    [[ -e "$out" ]] && die "$out exists; a new seed would give every new participant a different key family"
    need "$AGE"
    args=(); for r in "$@"; do args+=(-r "$r"); done
    # 32 random bytes as hex, straight into age: the seed never touches the disk or the terminal.
    openssl rand -hex 32 | "$AGE" "${args[@]}" -o "$out"
    echo "wrote $out, decryptable by $# recipient(s). Copy it to every steward and into the backups."
    ;;
  unlock)
    host=${1:?usage: unlock <ssh host> <seed.age> [identity file]…}; file=${2:?seed file}; shift 2
    need "$AGE"
    ids=(); for i in "$@"; do ids+=(-i "$i"); done
    [[ ${#ids[@]} -gt 0 ]] || die "name the YubiKey identity file (age-plugin-yubikey --identity > id.txt)"
    seed=$("$AGE" -d "${ids[@]}" "$file") || die "could not decrypt $file"
    [[ "$seed" =~ ^[0-9a-f]{64}$ ]] || die "$file does not hold a custody seed"
    # /run is a tmpfs: the seed is gone after a reboot (signing keeps working, new accounts wait).
    printf '%s\n' "$seed" | ssh "$host" 'sudo install -d -m 700 /run/belfry && sudo install -m 400 /dev/stdin /run/belfry/custody-seed && echo "unlocked on $(hostname)"'
    unset seed
    ;;
  status)
    host=${1:?usage: status <ssh host>}
    ssh "$host" 'sudo test -s /run/belfry/custody-seed && echo "unlocked" || { echo "locked"; exit 1; }'
    ;;
  lock)
    host=${1:?usage: lock <ssh host>}
    ssh "$host" 'sudo rm -f /run/belfry/custody-seed && echo locked'
    ;;
  *)
    sed -n '2,21p' "$0"; exit 2
    ;;
esac
