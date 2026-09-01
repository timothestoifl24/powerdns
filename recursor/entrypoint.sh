#!/bin/bash
# Renders /etc/powerdns/recursor.conf from the template and starts the
# PowerDNS Recursor.
#
# Every secret can be supplied either directly (FOO=value) or as a path to a
# file (FOO_FILE=/run/secrets/foo), which is what Docker/Swarm secrets and
# Kubernetes mounted secrets give you. The file form wins when both are set.
set -euo pipefail
# bash 5.2+ treats a bare & in a ${var//pat/repl} replacement as the matched
# text. Secrets legitimately contain &, so turn that behaviour off.
shopt -u patsub_replacement 2>/dev/null || true

TEMPLATE=/usr/share/pdns-recursor/recursor.conf.template
CONFIG=/etc/powerdns/recursor.conf

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

resolve_secret RECURSOR_API_KEY

: "${RECURSOR_LOCAL_ADDRESS:=0.0.0.0}"
: "${RECURSOR_LOCAL_PORT:=53}"
# Private networks only. See the comment in the template: widening this to
# 0.0.0.0/0 turns the container into an open resolver.
: "${RECURSOR_ALLOW_FROM:=127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7,fe80::/10}"
: "${RECURSOR_DNSSEC:=process}"
: "${RECURSOR_WEBSERVER_ADDRESS:=0.0.0.0}"
: "${RECURSOR_WEBSERVER_PORT:=8082}"
# Compose user-defined networks live in 172.16/12 by default. Narrow or widen
# this to match your deployment; it is what keeps the API off the public net.
: "${RECURSOR_WEBSERVER_ALLOW_FROM:=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
: "${RECURSOR_API_CONFIG_DIR:=/var/lib/powerdns-recursor/api}"
: "${RECURSOR_LOGLEVEL:=4}"
: "${RECURSOR_QUIET:=yes}"
: "${RECURSOR_THREADS:=2}"
: "${RECURSOR_VERSION_STRING:=anonymous}"

[ -n "${RECURSOR_API_KEY:-}" ] || die "set RECURSOR_API_KEY or RECURSOR_API_KEY_FILE"
if [ "${#RECURSOR_API_KEY}" -lt 16 ]; then
  die "RECURSOR_API_KEY must be at least 16 characters (got ${#RECURSOR_API_KEY})"
fi

render_config() {
  local text key value value_var
  text="$(< "$TEMPLATE")"
  # Pure bash substitution: unlike sed, the replacement text here is literal,
  # so keys containing & / $ ` or quotes survive untouched.
  for key in LOCAL_ADDRESS:RECURSOR_LOCAL_ADDRESS \
             LOCAL_PORT:RECURSOR_LOCAL_PORT \
             ALLOW_FROM:RECURSOR_ALLOW_FROM \
             DNSSEC:RECURSOR_DNSSEC \
             API_KEY:RECURSOR_API_KEY \
             WEBSERVER_ADDRESS:RECURSOR_WEBSERVER_ADDRESS \
             WEBSERVER_PORT:RECURSOR_WEBSERVER_PORT \
             WEBSERVER_ALLOW_FROM:RECURSOR_WEBSERVER_ALLOW_FROM \
             API_CONFIG_DIR:RECURSOR_API_CONFIG_DIR \
             LOGLEVEL:RECURSOR_LOGLEVEL \
             QUIET:RECURSOR_QUIET \
             THREADS:RECURSOR_THREADS \
             VERSION_STRING:RECURSOR_VERSION_STRING; do
    value_var="${key#*:}"
    value="${!value_var}"
    if [[ "$value" == *$'\n'* ]]; then
      die "value for ${key#*:} must not contain a newline"
    fi
    text="${text//__${key%%:*}__/$value}"
  done
  printf '%s' "$text" > "$CONFIG"
}

render_config
# The rendered file holds the API key.
chown root:pdns "$CONFIG"
chmod 0640 "$CONFIG"

# The recursor drops to the pdns user but has to write zone fragments here, so
# the directory has to belong to that user. On a fresh named volume it is
# root-owned, which would make every attempt to save a forward zone fail with
# a permission error from inside the API.
install -d -m 0750 -o pdns -g pdns "$RECURSOR_API_CONFIG_DIR"
chown pdns:pdns "$RECURSOR_API_CONFIG_DIR"

# The control socket lives here and the process refuses to start if the
# directory is missing. /var/run is a tmpfs in some runtimes, so recreating it
# on every start is not redundant: the image's copy can be masked at runtime.
install -d -m 0755 -o pdns -g pdns /var/run/pdns-recursor

# RECURSOR_SETTING_<name>=value becomes the recursor setting <name>, with
# underscores turned into dashes. The escape hatch for settings the template
# does not cover, e.g. RECURSOR_SETTING_max_cache_entries=2000000
write_overrides() {
  local var setting value overrides="${RECURSOR_API_CONFIG_DIR}/00-env-overrides.conf"
  : > "$overrides"
  for var in $(compgen -v | grep '^RECURSOR_SETTING_' | sort); do
    setting="${var#RECURSOR_SETTING_}"
    value="${!var}"
    if [[ "$value" == *$'\n'* ]]; then
      die "value for ${var} must not contain a newline"
    fi
    printf '%s=%s\n' "${setting//_/-}" "$value" >> "$overrides"
    log "override: ${setting//_/-}"
  done
  chown pdns:pdns "$overrides"
  chmod 0640 "$overrides"
}
write_overrides

# `docker run <image> rec_control get-all` and friends keep working.
if [ "$#" -gt 0 ]; then
  case "$1" in
    -*) set -- /usr/sbin/pdns_recursor "$@" ;;
  esac
  log "starting: $*"
  exec "$@"
fi

log "starting PowerDNS Recursor on ${RECURSOR_LOCAL_ADDRESS}:${RECURSOR_LOCAL_PORT}"
exec /usr/sbin/pdns_recursor --config-dir=/etc/powerdns
