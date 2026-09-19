# PostgreSQL pre-loaded with the PowerDNS schema and the admin panel's role.
#
# The scripts in /docker-entrypoint-initdb.d only run when the data directory
# is empty, i.e. on the very first start of a fresh volume. Changing them later
# has no effect on an existing database -- see README, "Upgrading the schema".

# The registry is spelled out because Podman has no implicit docker.io: an
# unqualified name fails the build outright on a host with no
# unqualified-search-registries in /etc/containers/registries.conf. Docker
# resolves the long form identically.
FROM docker.io/library/postgres:18-alpine

# PostgreSQL 18 moved the data directory. The image's PGDATA is now
# /var/lib/postgresql/18/docker and the declared volume is the parent,
# /var/lib/postgresql -- the layout pg_ctlcluster uses, which keeps each major
# version in its own subdirectory so pg_upgrade can run without crossing a
# mount boundary. compose.yml mounts pgdata at /var/lib/postgresql to match.
# A volume still mounted at the old /var/lib/postgresql/data makes the
# entrypoint refuse to start, deliberately: see docs/upgrading.md.

# The schema lives outside initdb/ on purpose: the entrypoint would run a
# .sql file there as the bootstrap superuser, leaving every table owned by
# one. 01-load-powerdns-schema.sh loads it as the unprivileged pdns role.
COPY schema/powerdns.sql /usr/local/share/powerdns/schema.sql
COPY initdb/ /docker-entrypoint-initdb.d/

# 01 must run before 02: the entrypoint executes files in lexical order.
RUN chmod 0755 /docker-entrypoint-initdb.d/*.sh
