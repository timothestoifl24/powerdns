#!/usr/bin/env bash
# Creates ./secrets/* and, if missing, .env from .env.example.
# Safe to re-run: existing secret files are never overwritten.
set -euo pipefail

cd "$(dirname "$0")/.."

SECRETS_DIR=secrets
mkdir -p "$SECRETS_DIR"
chmod 0700 "$SECRETS_DIR"

random() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-48}"; }

create() {
  local name="$1" length="${2:-48}" path="$SECRETS_DIR/$1"
  if [ -s "$path" ]; then
    echo "  keep   $path"
    return
  fi
  # No trailing newline: consumers strip one, but a bare value is unambiguous.
  printf '%s' "$(random "$length")" > "$path"
  chmod 0600 "$path"
  echo "  create $path"
}

echo "Generating secrets in $SECRETS_DIR/"
create pdns_db_password 40
create webui_db_password 40
create pdns_api_key 48
create webui_secret_key 64
create webui_admin_password 24

if [ ! -f .env ]; then
  cp .env.example .env
  echo "  create .env (copied from .env.example)"
else
  echo "  keep   .env"
fi

echo
echo "Initial administrator login:"
echo "  username: $(grep -E '^BOOTSTRAP_ADMIN_USERNAME=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"
echo "  password: $(cat "$SECRETS_DIR/webui_admin_password")"
echo
echo "Next: docker compose up -d --build"
