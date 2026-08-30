#!/bin/bash
# Creates the two unprivileged roles the application uses.
#
# POSTGRES_USER is the cluster's bootstrap superuser. It exists to run initdb
# and these scripts, and nothing connects as it afterwards. PowerDNS and the
# admin panel each get their own NOSUPERUSER role instead:
#
#   pdns       owns schema "public" and the PowerDNS tables in it
#   pdnsadmin  owns schema "pdnsadmin" and nothing else
#
# Neither can read the other's tables, and neither can touch the rest of the
# cluster. The panel drives PowerDNS through its HTTP API, so it never needs
# rights on the DNS data.
#
# Runs only on the first start of an empty data directory. Existing
# deployments need the migration in README, "Moving off the superuser role".
set -euo pipefail

PDNS_DB_USER="${PDNS_DB_USER:-pdns}"
WEBUI_DB_USER="${WEBUI_DB_USER:-pdnsadmin}"
WEBUI_DB_SCHEMA="${WEBUI_DB_SCHEMA:-pdnsadmin}"

# read_secret <target-var> <path-var-name> -- load a password from its file,
# with errors that say what to do rather than failing inside psql.
read_secret() {
  local target="$1" path_var="$2" path="${!2:-}"
  if [ -z "$path" ]; then
    echo "db-init: ${path_var} is not set" >&2
    exit 1
  fi
  if [ ! -e "$path" ]; then
    echo "db-init: ${path} does not exist." >&2
    echo "db-init: run ./scripts/generate-secrets.sh before starting the stack." >&2
    exit 1
  fi
  if [ ! -r "$path" ]; then
    # This script runs as the unprivileged postgres user, so a 0600 secret
    # owned by the host user is unreadable here.
    echo "db-init: ${path} is not readable by $(id -un) (uid $(id -u))." >&2
    echo "db-init: secret files must be world-readable. Fix with:" >&2
    echo "db-init:   chmod 0644 secrets/*   (the 0700 secrets/ directory is what protects them)" >&2
    exit 1
  fi
  printf -v "$target" '%s' "$(cat "$path")"
}

# Declared here so it is obvious where they come from; read_secret fills them
# via printf -v, which static analysis cannot follow.
pdns_password=""
webui_password=""
read_secret pdns_password PDNS_DB_PASSWORD_FILE
read_secret webui_password WEBUI_DB_PASSWORD_FILE

for name in pdns_password webui_password; do
  if [ -z "${!name}" ]; then
    echo "db-init: ${name} is empty" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname "${POSTGRES_DB}" \
     --set=pdns_user="${PDNS_DB_USER}" \
     --set=pdns_password="${pdns_password}" \
     --set=webui_user="${WEBUI_DB_USER}" \
     --set=webui_password="${webui_password}" \
     --set=webui_schema="${WEBUI_DB_SCHEMA}" <<'EOSQL'
-- Roles are created explicitly rather than by POSTGRES_USER, so that neither
-- of them is the bootstrap superuser. NOCREATEROLE matters as much as
-- NOSUPERUSER: a role that can create roles can grant itself anything.
SELECT format(
  'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L',
  :'pdns_user', :'pdns_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'pdns_user') \gexec

SELECT format(
  'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L',
  :'webui_user', :'webui_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'webui_user') \gexec

-- Keep the stored password in step with the secret file on every start.
SELECT format('ALTER ROLE %I PASSWORD %L', :'pdns_user', :'pdns_password') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'webui_user', :'webui_password') \gexec

GRANT CONNECT ON DATABASE :"DBNAME" TO :"pdns_user", :"webui_user";

-- PowerDNS owns "public" and the tables the next init script creates in it.
-- The database is dedicated to this stack, so owning its default schema is
-- the least-privilege option that still lets PowerDNS manage its own tables.
ALTER SCHEMA public OWNER TO :"pdns_user";

-- The panel owns its own schema and nothing else.
CREATE SCHEMA IF NOT EXISTS :"webui_schema" AUTHORIZATION :"webui_user";

-- USAGE lets the panel resolve built-in types; it grants no access to any
-- table in "public". PostgreSQL 15+ already removes CREATE from PUBLIC here,
-- but say it explicitly so the intent survives a change of base image.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"webui_user";
EOSQL

echo "db-init: roles '${PDNS_DB_USER}' and '${WEBUI_DB_USER}' created (neither is a superuser)"
