#!/bin/bash
# Renders /etc/powerdns/recursor.yml from the template and starts the
# PowerDNS Recursor.
#
# YAML rather than the classic key=value settings: Recursor 5.2 stopped
# reading old-style configuration unless --enable-old-settings is passed on the
# command line, and that option is documented as going away in a future
# release.
#
# Every secret can be supplied either directly (FOO=value) or as a path to a
# file (FOO_FILE=/run/secrets/foo), which is what Docker/Swarm secrets and
# Kubernetes mounted secrets give you. The file form wins when both are set.
set -euo pipefail
# bash 5.2+ treats a bare & in a ${var//pat/repl} replacement as the matched
# text. Secrets legitimately contain &, so turn that behaviour off.
shopt -u patsub_replacement 2>/dev/null || true

TEMPLATE=/usr/share/pdns-recursor/recursor.yml.template
CONFIG=/etc/powerdns/recursor.yml

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
: "${RECURSOR_API_DIR:=/var/lib/powerdns-recursor/api}"
# Deliberately not the same directory as the API's: under YAML settings the
# recursor requires them to differ.
: "${RECURSOR_INCLUDE_DIR:=/etc/powerdns/recursor.d}"
: "${RECURSOR_LOGLEVEL:=4}"
: "${RECURSOR_QUIET:=true}"
: "${RECURSOR_THREADS:=2}"
: "${RECURSOR_VERSION_STRING:=anonymous}"

[ -n "${RECURSOR_API_KEY:-}" ] || die "set RECURSOR_API_KEY or RECURSOR_API_KEY_FILE"
if [ "${#RECURSOR_API_KEY}" -lt 16 ]; then
  die "RECURSOR_API_KEY must be at least 16 characters (got ${#RECURSOR_API_KEY})"
fi
if [ "$RECURSOR_API_DIR" = "$RECURSOR_INCLUDE_DIR" ]; then
  die "RECURSOR_API_DIR and RECURSOR_INCLUDE_DIR must be different directories"
fi

# A YAML single-quoted scalar: the only escape inside one is '' for a literal
# quote, so this is safe for every character an API key can contain. Newlines
# are rejected separately below.
yaml_string() {
  local value="$1"
  printf "'%s'" "${value//\'/\'\'}"
}

# A YAML flow sequence of quoted strings, from a comma-separated list.
yaml_list() {
  local raw="$1" item out=""
  local IFS=,
  for item in $raw; do
    item="${item#"${item%%[![:space:]]*}"}"   # trim leading space
    item="${item%"${item##*[![:space:]]}"}"   # trim trailing space
    [ -n "$item" ] || continue
    [ -z "$out" ] || out+=", "
    out+="$(yaml_string "$item")"
  done
  printf '[%s]' "$out"
}

render_config() {
  local text key value value_var
  text="$(< "$TEMPLATE")"

  # Every value that reaches the file is rendered as a quoted YAML scalar or
  # sequence, so a key containing : # { } & or a quote cannot break the
  # document or inject a setting.
  local -A rendered=(
    [LISTEN]="$(yaml_list "$RECURSOR_LOCAL_ADDRESS")"
    [ALLOW_FROM]="$(yaml_list "$RECURSOR_ALLOW_FROM")"
    [WEBSERVER_ALLOW_FROM]="$(yaml_list "$RECURSOR_WEBSERVER_ALLOW_FROM")"
    [API_KEY]="$(yaml_string "$RECURSOR_API_KEY")"
    [DNSSEC]="$(yaml_string "$RECURSOR_DNSSEC")"
    [WEBSERVER_ADDRESS]="$(yaml_string "$RECURSOR_WEBSERVER_ADDRESS")"
    [VERSION_STRING]="$(yaml_string "$RECURSOR_VERSION_STRING")"
    [API_DIR]="$(yaml_string "$RECURSOR_API_DIR")"
    [INCLUDE_DIR]="$(yaml_string "$RECURSOR_INCLUDE_DIR")"
    [LOCAL_PORT]="$RECURSOR_LOCAL_PORT"
    [WEBSERVER_PORT]="$RECURSOR_WEBSERVER_PORT"
    [THREADS]="$RECURSOR_THREADS"
    [LOGLEVEL]="$RECURSOR_LOGLEVEL"
    [QUIET]="$RECURSOR_QUIET"
  )

  # The numeric and boolean settings are unquoted in the YAML, so they have to
  # be exactly that -- anything else would be read as a string and rejected by
  # the settings parser with a less obvious message.
  for key in LOCAL_PORT WEBSERVER_PORT THREADS LOGLEVEL; do
    [[ "${rendered[$key]}" =~ ^[0-9]+$ ]] || die "${key} must be a number, got '${rendered[$key]}'"
  done
  case "${rendered[QUIET]}" in
    true|false) ;;
    *) die "RECURSOR_QUIET must be true or false, got '${rendered[QUIET]}'" ;;
  esac

  for key in "${!rendered[@]}"; do
    value_var="${rendered[$key]}"
    if [[ "$value_var" == *$'\n'* ]]; then
      die "value for ${key} must not contain a newline"
    fi
    text="${text//__${key}__/$value_var}"
  done
  printf '%s\n' "$text" > "$CONFIG"
}

install -d -m 0755 /etc/powerdns
render_config
# The rendered file holds the API key.
chown root:pdns "$CONFIG"
chmod 0640 "$CONFIG"

# The recursor drops to the pdns user but has to write zone fragments here, so
# the directory has to belong to that user. On a fresh named volume it is
# root-owned, which would make every attempt to save a forward zone fail with
# a permission error from inside the API.
install -d -m 0750 -o pdns -g pdns "$RECURSOR_API_DIR"
chown pdns:pdns "$RECURSOR_API_DIR"

# The control socket lives here and the process refuses to start if the
# directory is missing. /var/run is a tmpfs in some runtimes, so recreating it
# on every start is not redundant: the image's copy can be masked at runtime.
install -d -m 0755 -o pdns -g pdns /var/run/pdns-recursor

# Anything the template does not cover. The whole value is written as a YAML
# document into the include directory, so it is ordinary recursor YAML rather
# than a syntax of ours:
#
#   RECURSOR_EXTRA_YAML: |
#     recordcache:
#       max_entries: 2000000
#     recursor:
#       serve_rfc1918: false
install -d -m 0755 "$RECURSOR_INCLUDE_DIR"
if [ -n "${RECURSOR_EXTRA_YAML:-}" ]; then
  printf '%s\n' "$RECURSOR_EXTRA_YAML" > "${RECURSOR_INCLUDE_DIR}/50-env-overrides.yml"
  log "wrote ${RECURSOR_INCLUDE_DIR}/50-env-overrides.yml from RECURSOR_EXTRA_YAML"
else
  rm -f "${RECURSOR_INCLUDE_DIR}/50-env-overrides.yml"
fi
chmod 0644 "${RECURSOR_INCLUDE_DIR}"/*.yml 2>/dev/null || true

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
