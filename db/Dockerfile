# PostgreSQL pre-loaded with the PowerDNS schema and the admin panel's role.
#
# The scripts in /docker-entrypoint-initdb.d only run when the data directory
# is empty, i.e. on the very first start of a fresh volume. Changing them later
# has no effect on an existing database -- see README, "Upgrading the schema".
FROM postgres:17-alpine

# The schema lives outside initdb/ on purpose: the entrypoint would run a
# .sql file there as the bootstrap superuser, leaving every table owned by
# one. 01-load-powerdns-schema.sh loads it as the unprivileged pdns role.
COPY schema/powerdns.sql /usr/local/share/powerdns/schema.sql
COPY initdb/ /docker-entrypoint-initdb.d/

# 01 must run before 02: the entrypoint executes files in lexical order.
RUN chmod 0755 /docker-entrypoint-initdb.d/*.sh
