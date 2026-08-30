#!/bin/bash
# Loads the PowerDNS gpgsql schema as the unprivileged "pdns" role.
#
# The schema itself is kept as plain SQL in /usr/local/share/powerdns/schema.sql
# so it stays diffable against upstream. It is loaded from here rather than
# dropped straight into /docker-entrypoint-initdb.d because the entrypoint runs
# .sql files as POSTGRES_USER -- the bootstrap superuser -- which would leave
# every table owned by a superuser. SET ROLE first, so the objects belong to
# the role PowerDNS actually connects as.
set -euo pipefail

PDNS_DB_USER="${PDNS_DB_USER:-pdns}"
SCHEMA_FILE=/usr/local/share/powerdns/schema.sql

[ -r "$SCHEMA_FILE" ] || { echo "db-init: ${SCHEMA_FILE} is missing from the image" >&2; exit 1; }

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname "${POSTGRES_DB}" \
     --set=pdns_user="${PDNS_DB_USER}" <<EOSQL
SET ROLE :"pdns_user";
\\i ${SCHEMA_FILE}
EOSQL

echo "db-init: PowerDNS schema loaded, owned by '${PDNS_DB_USER}'"
