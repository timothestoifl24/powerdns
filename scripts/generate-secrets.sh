#!/usr/bin/env bash
# Creates ./secrets/* and, if missing, .env from .env.example.
# Safe to re-run: existing secret files are never overwritten.
set -euo pipefail

cd "$(dirname "$0")/.."

SECRETS_DIR=secrets
mkdir -p "$SECRETS_DIR"
# The directory is what keeps other users on this host out.
chmod 0700 "$SECRETS_DIR"

# The secret files themselves have to be world-readable, because compose
# bind-mounts them into the containers with their host ownership intact and the
# processes that read them are unprivileged: postgres (uid 70) runs the initdb
# scripts, and the panel runs as uid 10001. Neither can read a 0600 file owned
# by the host user. Docker Swarm mounts secrets 0444 for the same reason.
# Confidentiality here comes from the 0700 directory above.
SECRET_MODE=0644

random() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-48}"; }

create() {
  local length="${2:-48}" path="$SECRETS_DIR/$1"
  if [ -s "$path" ]; then
    # Re-apply the mode: an existing file from an older checkout may be 0600,
    # which makes the containers fail to start with "Permission denied".
    chmod "$SECRET_MODE" "$path"
    echo "  keep   $path"
    return
  fi
  # No trailing newline: consumers strip one, but a bare value is unambiguous.
  printf '%s' "$(random "$length")" > "$path"
  chmod "$SECRET_MODE" "$path"
  echo "  create $path"
}

echo "Generating secrets in $SECRETS_DIR/"
create db_superuser_password 40
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

admin_user="$(sed -n 's/^BOOTSTRAP_ADMIN_USERNAME=//p' .env 2>/dev/null | tr -d '"' | head -1)"

echo
echo "Initial administrator login:"
echo "  username: ${admin_user:-admin}"
if [ -t 1 ]; then
  echo "  password: $(cat "$SECRETS_DIR/webui_admin_password")"
else
  # stdout is a pipe or a file -- a CI job log, `tee setup.log`, a ticket
  # attachment. Printing the password there puts it somewhere it will outlive
  # its usefulness, so show where to read it instead.
  echo "  password: not shown (stdout is not a terminal)"
  echo "            read it with: cat $SECRETS_DIR/webui_admin_password"
fi
echo
echo "Change this password after the first sign-in. The file stays on disk so"
echo "you can look it up again, but it is only used while no users exist."
echo
echo "Next: docker compose up -d --build"
