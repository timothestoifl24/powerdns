#!/usr/bin/env bash
# Manages the powerdns-secrets Secret for deploy/kubernetes.
#
#   k8s-secrets.sh [-n NAMESPACE] create          generate every secret in the cluster
#   k8s-secrets.sh [-n NAMESPACE] set KEY         add or replace one key (prompts)
#   k8s-secrets.sh [-n NAMESPACE] admin-password  show the first-run admin password
#   k8s-secrets.sh [-n NAMESPACE] forget-admin-password
#                                                 drop it once you have signed in
#
# Nothing is written to disk and no value ever appears on a command line, where
# it would land in shell history and in `ps` for every user on the machine: the
# values are generated here and handed to kubectl on stdin. The Secret is made
# with `kubectl create`, not `apply`, because apply would keep a second full
# copy of it in the last-applied-configuration annotation.
set -euo pipefail

NAMESPACE=powerdns
SECRET=powerdns-secrets

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while getopts 'n:h' opt; do
  case "$opt" in
    n) NAMESPACE="$OPTARG" ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))
[ "$#" -ge 1 ] || usage

command -v kubectl >/dev/null || { echo "kubectl is not on PATH" >&2; exit 1; }

random() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-48}"; }
b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
secret_exists() { kubectl -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1; }

create() {
  if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    kubectl create namespace "$NAMESPACE"
  fi
  if secret_exists; then
    # Never regenerate: the database roles were created with the old
    # passwords on the first start and would no longer match.
    echo "secret/${SECRET} already exists in ${NAMESPACE}; leaving it untouched"
    return
  fi
  {
    printf 'apiVersion: v1\nkind: Secret\ntype: Opaque\n'
    printf 'metadata:\n  name: %s\n  namespace: %s\n' "$SECRET" "$NAMESPACE"
    printf 'data:\n'
    local key length
    for key in db_superuser_password:40 pdns_db_password:40 webui_db_password:40 \
               pdns_api_key:48 recursor_api_key:48 webui_secret_key:64 \
               webui_admin_password:24; do
      length="${key#*:}"
      printf '  %s: %s\n' "${key%%:*}" "$(b64 "$(random "$length")")"
    done
  } | kubectl create -f -
  echo
  echo "Read the first-run admin password with:"
  echo "  $0 -n ${NAMESPACE} admin-password"
}

set_key() {
  local key="${1:-}" value
  [[ "$key" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "usage: $0 set KEY" >&2; exit 2; }
  secret_exists || { echo "secret/${SECRET} does not exist; run '$0 create' first" >&2; exit 1; }
  if [ -t 0 ]; then
    read -rsp "Value for ${key}: " value
    echo >&2
  else
    # Piped in, e.g. from a password manager's CLI. One line, newline dropped.
    IFS= read -r value || true
  fi
  [ -n "$value" ] || { echo "empty value; nothing changed" >&2; exit 1; }
  printf '{"data":{"%s":"%s"}}' "$key" "$(b64 "$value")" |
    kubectl -n "$NAMESPACE" patch secret "$SECRET" --type=merge --patch-file=/dev/stdin
}

admin_password() {
  local encoded
  encoded="$(kubectl -n "$NAMESPACE" get secret "$SECRET" \
    -o jsonpath='{.data.webui_admin_password}')"
  if [ -z "$encoded" ]; then
    echo "no webui_admin_password in secret/${SECRET}: it has already been removed." >&2
    exit 1
  fi
  if [ ! -t 1 ]; then
    # Same rule as generate-secrets.sh: a pipe or a file outlives the moment.
    echo "refusing to print the password anywhere but a terminal" >&2
    exit 1
  fi
  printf '%s' "$encoded" | base64 -d
  echo
}

forget_admin_password() {
  kubectl -n "$NAMESPACE" patch secret "$SECRET" --type=json \
    -p='[{"op":"remove","path":"/data/webui_admin_password"}]'
  echo "The panel only reads it while no user exists, so nothing needs a restart."
}

case "$1" in
  create) create ;;
  set) shift; set_key "$@" ;;
  admin-password) admin_password ;;
  forget-admin-password) forget_admin_password ;;
  *) usage ;;
esac
