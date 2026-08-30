#!/bin/sh
# Prepares the database, then hands off to gunicorn.
#
# Schema creation and the first-run administrator happen here, once, rather
# than in each gunicorn worker -- concurrent workers racing to CREATE TABLE
# produce confusing errors on first boot.
set -eu

echo "webui: preparing database"
python -m app.cli init

echo "webui: starting $*"
exec "$@"
