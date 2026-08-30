---
title: Upgrading
description: Pulling new images, what happens to each schema, backups and restores, and moving to a new PowerDNS major version.
---

# Upgrading

Three things in this stack version independently: the panel, PowerDNS, and
PostgreSQL. They upgrade differently, and only one of them is genuinely
delicate.

## Back up first

The whole system lives in one database. A dump of it is a complete backup —
zones, records, DNSSEC keys, users, grants and the audit log:

```bash
docker compose exec -T db pg_dump -U pdns -d pdns --clean --if-exists \
  > pdns-$(date +%F).sql
```

Restoring into a fresh stack:

```bash
docker compose down -v
docker compose up -d db
docker compose exec -T db psql -U pdns -d pdns < pdns-2026-08-30.sql
docker compose up -d
```

Keep `secrets/` with the dump. The DNSSEC private keys are inside the database,
but the API key and the panel's session key are not — and without
`secrets/webui_db_password` the restored panel cannot log in to its own database.

::: danger `down -v` deletes the volume
`docker compose down` stops the containers and keeps the data. `docker compose
down -v` deletes the `pgdata` volume, and with it every zone. Dump first.
:::

## Upgrading the panel

The ordinary case, and the safe one:

```bash
git pull
docker compose up -d --build webui
```

The panel creates and migrates its own tables at start-up, in the `pdnsadmin`
schema, so there is nothing to run by hand. Watch it come up:

```bash
docker compose logs -f webui
```

You want `database schema is up to date` followed by gunicorn booting its
workers. Sessions do not survive the restart if `SECRET_KEY` changed; with the
same secret file, they do.

If you prefer published images to building locally, the CI pipeline pushes
`ghcr.io/timothestoifl24/pdns-webui`, `…/pdns` and `…/pdns-db` on every merge to
`main`, tagged `latest` and by commit SHA. Pin the SHA in production and move it
deliberately:

```yaml
services:
  webui:
    image: ghcr.io/timothestoifl24/pdns-webui:sha-1a2b3c4
```

## Upgrading PowerDNS

Rebuilding the `pdns` image picks up whatever Debian trixie currently ships,
which stays within PowerDNS Authoritative 4.9.x:

```bash
docker compose build --no-cache pdns
docker compose up -d pdns
```

DNS is unavailable for the second or two the container takes to restart. If that
matters, run a second nameserver — which is what the `NS` set on every zone is
promising anyway.

### Moving to a new major version

This is the one that needs care. **The gpgsql schema changes between major
versions, and nothing in this stack migrates it for you.**

1. Read the [upstream upgrade notes](https://doc.powerdns.com/authoritative/upgrading.html)
   for every version you are stepping over — they list the exact SQL.
2. Take a dump (above).
3. Apply the schema changes by hand:
   ```bash
   docker compose exec -T db psql -U pdns -d pdns < 4.9-to-4.10.sql
   ```
4. Point `pdns/Dockerfile` at the new package source, rebuild, restart.
5. Verify: `dig @127.0.0.1 example.com SOA` and, for a signed zone,
   `dig @127.0.0.1 example.com DNSKEY +dnssec`.

::: warning `db/initdb/` only runs once
Scripts in `db/initdb/` are executed by PostgreSQL **only when the data
directory is empty** — the first start of a fresh volume. Editing
`01-powerdns-schema.sql` does nothing to an existing database. This catches
people out: the file looks like the schema definition, but on a running system
it is only a historical record of how the database began.
:::

## Upgrading PostgreSQL

A major version bump (17 → 18) cannot be done by changing the tag: PostgreSQL
will not start against a data directory written by an older major version. Dump
and restore instead:

```bash
docker compose exec -T db pg_dumpall -U pdns > all-$(date +%F).sql
docker compose down -v
# edit db/Dockerfile to the new major version, then:
docker compose up -d --build db
docker compose exec -T db psql -U pdns -d postgres < all-2026-08-30.sql
docker compose up -d
```

Minor updates (17.4 → 17.5) are just a rebuild and restart.

## Rotating secrets

| Secret | How |
| --- | --- |
| `webui_admin_password` | Only used while the user table is empty. After that, change the password in the panel under **My profile**. |
| `webui_secret_key` | Replace the file and restart `webui`. Every session is invalidated; everyone signs in again. |
| `pdns_api_key` | Replace the file and restart **both** `pdns` and `webui` — they must agree, and the panel reports the API as unreachable until they do. |
| `pdns_db_password` / `webui_db_password` | Change the role's password in PostgreSQL with `ALTER ROLE … PASSWORD …` first, then update the file and restart. Changing only the file locks the service out of its own database. |

## Rolling back

Since the panel migrates its own schema forward, a rollback to an older panel
image can meet tables it does not expect. The reliable route is the dump:

```bash
docker compose down -v
git checkout <previous-tag>
docker compose up -d db
docker compose exec -T db psql -U pdns -d pdns < pdns-before-upgrade.sql
docker compose up -d --build
```

Which is the real argument for taking the dump before you upgrade, not after
something goes wrong.

## Checking the result

```bash
curl -s http://localhost:9191/readyz
dig @127.0.0.1 example.com SOA +short
```

`readyz` returning `{"database": true, "powerdns": true}` and a zone answering
its SOA means both halves came back. The panel's version is in the footer of
every page and in `/healthz`.
