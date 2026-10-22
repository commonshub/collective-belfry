#!/bin/bash
# Runs once, on first initialisation of the postgres volume (docker-entrypoint-initdb.d).
# Creates one role and one database per service. Passwords come from the container env.
set -euo pipefail

create_role() {
  local name="$1" password="$2" extra="${3:-}"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -c "CREATE ROLE \"$name\" WITH LOGIN PASSWORD '$password' $extra;"
}
create_db() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -c "CREATE DATABASE \"$1\" OWNER \"$1\" TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';"
}

# Odoo: role only. The "odoo" database is created by bootstrap (odoo -d odoo -i ...): an empty
# database that matches the server's dbfilter makes every request fail with KeyError 'ir.http'.
# CREATEDB also covers the throwaway test database (make test-odoo).
create_role odoo "$ODOO_DB_PASSWORD" CREATEDB
create_role authentik "$AUTHENTIK_DB_PASSWORD"; create_db authentik
create_role keycast "$KEYCAST_DB_PASSWORD"; create_db keycast
