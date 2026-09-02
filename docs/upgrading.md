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
docker compose exec -T db pg_dump -U postgres -d pdns --clean --if-exists \
  > pdns-$(date +%F).sql
```

Restoring into a fresh stack:

```bash
docker compose down -v
docker compose up -d db
docker compose exec -T db psql -U postgres -d pdns < pdns-2026-08-30.sql
docker compose up -d
```

The dump runs as the bootstrap superuser, and it has to: `pdns` and `pdnsadmin`
each own one schema and cannot read the other's, so a dump taken as either role
would quietly contain half the system.

Keep `secrets/` with the dump. The DNSSEC private keys are inside the database,
but the API key and the panel's session key are not — and without
`secrets/webui_db_password` the restored panel cannot log in to its own database.
`secrets/webui_secret_key` matters more than it looks: it is what decrypts the
sign-in provider secrets stored in `auth_providers`.

::: danger `down -v` deletes the volume
`docker compose down` stops the containers and keeps the data. `docker compose
down -v` deletes the `pgdata` volume, and with it every zone. Dump first.
:::

## Upgrading to v1.0.1

This one changes which server answers on port 53, so read it before pulling.

A PowerDNS Recursor is now the front door. It answers for your zones by
forwarding them to the authoritative server, and handles everything else
according to the panel's Forwarding page. The authoritative server moves behind
it and is no longer published on `DNS_PORT`.

```bash
git pull
./scripts/generate-secrets.sh     # creates secrets/recursor_api_key; existing files are untouched
docker compose up -d --build
```

Then check that your zones still answer:

```bash
dig @127.0.0.1 www.example.com A
```

What to expect:

- **The compose network is recreated.** The authoritative server needs a fixed
  address, because forward targets in PowerDNS are IP addresses and never names.
  That means the `backend` network gains a subnet (`172.29.0.0/24` by default)
  and is rebuilt, so every container restarts. Set `BACKEND_SUBNET` and
  `PDNS_STATIC_IP` together if that subnet collides with one your host uses.
- **`DNS_PORT` is now the recursor.** Add `AUTH_DNS_PORT=5300` if you want to
  keep querying the authoritative server directly.
- **A forward rule per zone appears.** The panel creates one for each
  authoritative zone, pointing at the authoritative server, so your zones stay
  answerable through the new front door. They are marked *local zone* on the
  Forwarding page and maintained for you.
- **The resolver is not open.** `RECURSOR_ALLOW_FROM` defaults to private
  networks and loopback. If clients on other networks query this server, widen
  it — to those networks, never to `0.0.0.0/0`.

### Staying authoritative-only

If you do not want a resolver at all, remove the `recursor` service from
`compose.yml`, unset `RECURSOR_API_URL` in the webui service, and publish the
authoritative server on `DNS_PORT` again. The Forwarding page then says it is
unavailable and the nav entry disappears; nothing else changes.

### One new column

The panel adds `role_locked` and `last_groups` to its `users` table, so a role
an administrator sets by hand is no longer overwritten by the directory's group
mapping at the next sign-in. Both are added automatically at start-up — watch
for `added missing column users.role_locked` in the log. Existing rows get the
defaults, so nothing changes for accounts you have not touched.

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

If you prefer published images to building locally, four are pushed to
`ghcr.io`: `pdns-webui`, `pdns`, `pdns-recursor` and `pdns-db`. Every merge to
`main` publishes `latest` and a commit SHA tag; every release additionally
publishes the version, so `1.0.1` and `1.0` name a build whose contents will
never change.

Pin a version in production and move it deliberately:

```yaml
services:
  webui:
    image: ghcr.io/timothestoifl24/pdns-webui:1.0.1
```

`1.0` follows the patch releases in that series, and `sha-1a2b3c4` pins one
exact build if you need to. `latest` is fine for a lab and a poor idea in
production, where an unattended `docker compose pull` should never be able to
change what you are running.

What changed between two versions is in the
[changelog](https://github.com/timothestoifl24/powerdns/blob/main/CHANGELOG.md),
and each [release](https://github.com/timothestoifl24/powerdns/releases) repeats
it with the full list of merged pull requests.

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
   docker compose exec -T db psql -U postgres -d pdns < 4.9-to-4.10.sql
   ```
4. Point `pdns/Dockerfile` at the new package source, rebuild, restart.
5. Verify: `dig @127.0.0.1 example.com SOA` and, for a signed zone,
   `dig @127.0.0.1 example.com DNSKEY +dnssec`.

::: warning `db/initdb/` only runs once
Scripts in `db/initdb/` — `00-roles.sh` and `01-load-powerdns-schema.sh` — are
executed by PostgreSQL **only when the data directory is empty**, i.e. the first
start of a fresh volume. Editing them, or `db/schema/powerdns.sql`, does nothing
to an existing database. This catches people out: the files look like the schema
definition, but on a running system they are only a historical record of how the
database began.
:::

## Moving off the superuser role

Stacks created before the roles were split run PowerDNS as the cluster's
bootstrap superuser: `POSTGRES_USER` used to be `pdns`, and the postgres image
makes that name the superuser that owns the cluster. Because `db/initdb/` runs
only on an empty data directory, pulling the new images does **not** change an
existing deployment.

It keeps working as it is. To migrate without recreating the volume:

```bash
# 1. Create the new secret. Existing secret files are left untouched.
./scripts/generate-secrets.sh
```

```bash
# 2. Create the bootstrap superuser, using the role that currently is one.
docker compose up -d db
docker compose exec -T db psql -U pdns -d pdns \
  -v pw="$(cat secrets/db_superuser_password)" <<'SQL'
SELECT format('CREATE ROLE postgres LOGIN SUPERUSER PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') \gexec
SQL
```

```bash
# 3. Demote PowerDNS's role. It keeps ownership of its tables, so it retains
#    full access to them — it just loses the rest of the cluster.
docker compose exec -T db psql -U postgres -d pdns -c \
  "ALTER ROLE pdns NOSUPERUSER NOCREATEDB NOCREATEROLE;
   ALTER SCHEMA public OWNER TO pdns;
   REVOKE CREATE ON SCHEMA public FROM PUBLIC;"
```

```bash
# 4. Restart on the new configuration.
docker compose down && docker compose up -d
```

Verify — both rows should read `f | f`:

```bash
docker compose exec -T db psql -U postgres -d pdns -c \
  "SELECT rolname, rolsuper, rolcreaterole FROM pg_roles
   WHERE rolname IN ('pdns','pdnsadmin')"
```

If you have no zones worth keeping, `docker compose down -v` and a fresh `up`
gets there in one step — and destroys all DNS data.

## Upgrading PostgreSQL

A major version bump (17 → 18) cannot be done by changing the tag: PostgreSQL
will not start against a data directory written by an older major version. Dump
and restore instead:

```bash
docker compose exec -T db pg_dumpall -U postgres > all-$(date +%F).sql
docker compose down -v
# edit db/Dockerfile to the new major version, then:
docker compose up -d --build db
docker compose exec -T db psql -U postgres -d postgres < all-2026-08-30.sql
docker compose up -d
```

Minor updates (17.4 → 17.5) are just a rebuild and restart.

## Rotating secrets

| Secret | How |
| --- | --- |
| `webui_admin_password` | Only used while the user table is empty. After that, change the password in the panel under **My profile**. |
| `webui_secret_key` | Replace the file and restart `webui`. Every session is invalidated — and so is every stored provider secret, see below. |
| `pdns_api_key` | Replace the file and restart **both** `pdns` and `webui` — they must agree, and the panel reports the API as unreachable until they do. |
| `db_superuser_password` / `pdns_db_password` / `webui_db_password` | Change the role's password in PostgreSQL with `ALTER ROLE … PASSWORD …` first, then update the file and restart. Changing only the file locks the service out of its own database. |

::: danger Rotating `SECRET_KEY` invalidates stored provider secrets
Sign-in provider secrets — OAuth client secrets, LDAP bind passwords, SAML
private keys — are encrypted with a key derived from `SECRET_KEY`. Replacing
`secrets/webui_secret_key` makes them unreadable. The panel says so plainly on
the provider page rather than failing at sign-in, and re-entering each secret
fixes it, but plan the rotation for a moment when you have those values to hand.

Providers configured in `.env` are unaffected: they are never encrypted, because
they are never stored.
:::

## Rolling back

Since the panel migrates its own schema forward, a rollback to an older panel
image can meet tables it does not expect. The reliable route is the dump:

```bash
docker compose down -v
git checkout <previous-tag>
docker compose up -d db
docker compose exec -T db psql -U postgres -d pdns < pdns-before-upgrade.sql
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
