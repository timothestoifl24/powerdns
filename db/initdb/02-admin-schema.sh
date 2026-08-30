#!/bin/bash
# Creates the role and schema used by the web admin panel.
#
# The panel never touches the PowerDNS tables directly -- it drives PowerDNS
# through its HTTP API -- so it gets its own role that only owns the
# "pdnsadmin" schema. Its tables (users, sessions, audit log, ...) are created
# by the application itself on first start.
set -euo pipefail

WEBUI_DB_USER="${WEBUI_DB_USER:-pdnsadmin}"
WEBUI_DB_SCHEMA="${WEBUI_DB_SCHEMA:-pdnsadmin}"

if [ -n "${WEBUI_DB_PASSWORD_FILE:-}" ] && [ -f "${WEBUI_DB_PASSWORD_FILE}" ]; then
  WEBUI_DB_PASSWORD="$(cat "${WEBUI_DB_PASSWORD_FILE}")"
fi

if [ -z "${WEBUI_DB_PASSWORD:-}" ]; then
  echo "db-init: WEBUI_DB_PASSWORD (or WEBUI_DB_PASSWORD_FILE) is not set" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname "${POSTGRES_DB}" \
     --set=user="${WEBUI_DB_USER}" \
     --set=schema="${WEBUI_DB_SCHEMA}" \
     --set=password="${WEBUI_DB_PASSWORD}" <<'EOSQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'user', :'password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user') \gexec

SELECT format('ALTER ROLE %I PASSWORD %L', :'user', :'password') \gexec

CREATE SCHEMA IF NOT EXISTS :"schema" AUTHORIZATION :"user";

GRANT CONNECT ON DATABASE :"DBNAME" TO :"user";
-- The panel needs to resolve built-in types etc., but no rights on the
-- PowerDNS tables that live in "public".
GRANT USAGE ON SCHEMA public TO :"user";
EOSQL

echo "db-init: role '${WEBUI_DB_USER}' and schema '${WEBUI_DB_SCHEMA}' are ready"
