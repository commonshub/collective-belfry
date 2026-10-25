# Sourced by the host scripts (bootstrap, smoke, gen-issuer-pki): exports every secret in
# infra/secrets/ as its variable (file odoo_api_key -> ODOO_API_KEY), unless the variable is set
# already. Containers get the same files as Compose secrets. infra/README.md, "Secrets".
# set_secret NAME VALUE writes one (bootstrap's tokens).
_secrets_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets"
if [[ ! -f "$_secrets_dir/postgres_password" ]] || grep -qE '^(POSTGRES_PASSWORD|ODOO_API_KEY|HOOK_TOKEN)=' "$_secrets_dir/../.env" 2>/dev/null; then
  echo "infra/secrets/ is missing or infra/.env still holds secrets: run infra/scripts/gen-secrets.sh" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ -d "$_secrets_dir" ]]; then
  for _f in "$_secrets_dir"/*; do
    [[ -f "$_f" ]] || continue
    _n=$(basename "$_f" | tr '[:lower:]' '[:upper:]')
    [[ "$_n" =~ ^[A-Z0-9_]+$ ]] || continue
    [[ -n "${!_n:-}" ]] || export "$_n=$(cat "$_f")"
  done
fi
set_secret() {
  [[ "$1" =~ ^[A-Z0-9_]+$ ]] || { echo "set_secret: bad name $1" >&2; return 1; }
  local f; f="$_secrets_dir/$(tr '[:upper:]' '[:lower:]' <<<"$1")"
  # in place, not by rename: a running container's bind mount keeps the file it was started with
  (umask 027; printf '%s' "$2" > "$f")
  export "$1=$2"
}
unset _f _n
