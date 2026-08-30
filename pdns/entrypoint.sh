#!/bin/bash
# Renders /etc/powerdns/pdns.conf from the template and starts PowerDNS.
#
# Every secret can be supplied either directly (FOO=value) or as a path to a
# file (FOO_FILE=/run/secrets/foo), which is what Docker/Swarm secrets and
# Kubernetes mounted secrets give you. The file form wins when both are set.
set -euo pipefail
# bash 5.2+ treats a bare & in a ${var//pat/repl} replacement as the matched
# text. Secrets legitimately contain &, so turn that behaviour off.
shopt -u patsub_replacement 2>/dev/null || true

TEMPLATE=/usr/share/powerdns/pdns.conf.template
CONFIG=/etc/powerdns/pdns.conf
OVERRIDES=/etc/powerdns/pdns.d/50-env-overrides.conf

log() { printf '%s entrypoint: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# If <NAME>_FILE points at a readable file, load its contents into <NAME>.
# The trailing newline is stripped, so `openssl rand -hex 32 > secret` works.
resolve_secret() {
  local name="$1" file_var="${1}_FILE" path
  path="${!file_var:-}"
  [ -n "$path" ] || return 0
  [ -r "$path" ] || die "${file_var}=${path} is not readable"
  printf -v "$name" '%s' "$(< "$path")"
  export "${name?}"
}

resolve_secret PDNS_GPGSQL_PASSWORD
resolve_secret PDNS_API_KEY

: "${PDNS_GPGSQL_HOST:=db}"
: "${PDNS_GPGSQL_PORT:=5432}"
: "${PDNS_GPGSQL_USER:=pdns}"
: "${PDNS_GPGSQL_DBNAME:=pdns}"
: "${PDNS_GPGSQL_DNSSEC:=yes}"
: "${PDNS_WEBSERVER_ADDRESS:=0.0.0.0}"
: "${PDNS_WEBSERVER_PORT:=8081}"
# Compose user-defined networks live in 172.16/12 by default. Narrow or widen
# this to match your deployment; it is what keeps the API off the public net.
: "${PDNS_WEBSERVER_ALLOW_FROM:=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
: "${PDNS_LOCAL_ADDRESS:=0.0.0.0}"
: "${PDNS_LOCAL_PORT:=53}"
: "${PDNS_DEFAULT_SOA_CONTENT:=ns1.example.com hostmaster.example.com 0 10800 3600 604800 3600}"
: "${PDNS_DEFAULT_TTL:=3600}"
: "${PDNS_VERSION_STRING:=anonymous}"
: "${PDNS_LOGLEVEL:=4}"

[ -n "${PDNS_GPGSQL_PASSWORD:-}" ] || die "set PDNS_GPGSQL_PASSWORD or PDNS_GPGSQL_PASSWORD_FILE"
[ -n "${PDNS_API_KEY:-}" ] || die "set PDNS_API_KEY or PDNS_API_KEY_FILE"
if [ "${#PDNS_API_KEY}" -lt 16 ]; then
  die "PDNS_API_KEY must be at least 16 characters (got ${#PDNS_API_KEY})"
fi

render_config() {
  local text key value value_var
  text="$(< "$TEMPLATE")"
  # Pure bash substitution: unlike sed, the replacement text here is literal,
  # so passwords containing & / $ ` or quotes survive untouched.
  for key in PGSQL_HOST:PDNS_GPGSQL_HOST \
             PGSQL_PORT:PDNS_GPGSQL_PORT \
             PGSQL_USER:PDNS_GPGSQL_USER \
             PGSQL_PASSWORD:PDNS_GPGSQL_PASSWORD \
             PGSQL_DBNAME:PDNS_GPGSQL_DBNAME \
             PGSQL_DNSSEC:PDNS_GPGSQL_DNSSEC \
             API_KEY:PDNS_API_KEY \
             WEBSERVER_ADDRESS:PDNS_WEBSERVER_ADDRESS \
             WEBSERVER_PORT:PDNS_WEBSERVER_PORT \
             WEBSERVER_ALLOW_FROM:PDNS_WEBSERVER_ALLOW_FROM \
             LOCAL_ADDRESS:PDNS_LOCAL_ADDRESS \
             LOCAL_PORT:PDNS_LOCAL_PORT \
             DEFAULT_SOA_CONTENT:PDNS_DEFAULT_SOA_CONTENT \
             DEFAULT_TTL:PDNS_DEFAULT_TTL \
             VERSION_STRING:PDNS_VERSION_STRING \
             LOGLEVEL:PDNS_LOGLEVEL; do
    value_var="${key#*:}"
    value="${!value_var}"
    if [[ "$value" == *$'\n'* ]]; then
      die "value for ${key#*:} must not contain a newline"
    fi
    text="${text//__${key%%:*}__/$value}"
  done
  printf '%s' "$text" > "$CONFIG"
}

install -d -m 0755 /etc/powerdns/pdns.d
render_config
# The rendered file holds the database password and the API key.
chown root:pdns "$CONFIG"
chmod 0640 "$CONFIG"

# PDNS_SETTING_<name>=value becomes the PowerDNS setting <name>, underscores
# turned into dashes. The escape hatch for settings the template does not
# cover, e.g. PDNS_SETTING_allow_axfr_ir=10.0.0.0/8 -> allow-axfr-ir=10.0.0.0/8
write_overrides() {
  local var setting value
  : > "$OVERRIDES"
  for var in $(compgen -v | grep '^PDNS_SETTING_' | sort); do
    setting="${var#PDNS_SETTING_}"
    value="${!var}"
    if [[ "$value" == *$'\n'* ]]; then
      die "value for ${var} must not contain a newline"
    fi
    printf '%s=%s\n' "${setting//_/-}" "$value" >> "$OVERRIDES"
    log "override: ${setting//_/-}"
  done
}
write_overrides
chown root:pdns "$OVERRIDES"
chmod 0640 "$OVERRIDES"

# PowerDNS exits immediately when the database is not up yet, and a
# crash-looping container makes for confusing logs. Wait for the port to
# accept connections; compose's healthcheck covers actual readiness.
wait_for_db() {
  local host="$PDNS_GPGSQL_HOST" port="$PDNS_GPGSQL_PORT" attempt=0 max="${PDNS_DB_WAIT_SECONDS:-60}"
  while [ "$attempt" -lt "$max" ]; do
    if timeout 2 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null; then
      log "database ${host}:${port} is accepting connections"
      return 0
    fi
    attempt=$((attempt + 1))
    [ $((attempt % 10)) -eq 0 ] && log "still waiting for ${host}:${port} (${attempt}s)"
    sleep 1
  done
  die "database ${host}:${port} was not reachable within ${max}s"
}
wait_for_db

# `docker run <image> pdnsutil list-all-zones` and friends keep working.
if [ "$#" -gt 0 ]; then
  case "$1" in
    -*) set -- /usr/sbin/pdns_server "$@" ;;
  esac
  log "starting: $*"
  exec "$@"
fi

log "starting PowerDNS Authoritative Server"
exec /usr/sbin/pdns_server --config-dir=/etc/powerdns
