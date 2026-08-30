# Why PostgreSQL, and not MongoDB

Short answer: PostgreSQL, for both halves of the system. MongoDB is not a
viable option for the DNS data and buys nothing for the panel's data.

## The DNS data: PowerDNS decides this, not us

PowerDNS Authoritative Server only stores zone data in backends it ships.
The storage backends are the generic SQL family (`gmysql`, `gpgsql`,
`gsqlite3`, `godbc`), `lmdb`, and `bind` (zone files). `gmysql` and `gpgsql`
are the two most widely deployed in production.

There is no MongoDB backend:

* PowerDNS carried an in-tree `mongodbbackend` around 2011-2012. It was
  **removed in January 2013** (commit `8ecbcd2`) because it no longer compiled
  against any current Mongo client library, at the original author's request.
* What remains are third-party hobby projects -- the most visible one has
  5 stars and 4 commits.
* The only supported route to Mongo is the `remote` backend, which speaks
  JSON/RPC over a Unix socket, pipe, HTTP or ZeroMQ. That means writing and
  operating a second daemon that we own, in the hot path of every DNS query,
  and re-implementing the DNSSEC ordering callbacks ourselves.

So choosing MongoDB for zone data means giving up the supported path and
maintaining a custom backend forever. That is a large, permanent cost with no
offsetting benefit.

## Even ignoring support, the workload is wrong for a document store

PowerDNS's access pattern is exact-match point lookups -- "give me the records
for this exact name and type", "give me everything for this domain_id" -- plus
one ordered range scan on `ordername` for DNSSEC/NSEC narrowing. There are no
joins, no aggregations and no nested documents. That is the pattern where a
document database offers nothing a B-tree index does not already give you.

The record shape is also fixed and tiny (name, type, content, ttl, prio,
disabled, ordername, auth). Schema flexibility -- MongoDB's main draw -- solves
a problem this data does not have. Meanwhile the constraint that *is* load
bearing, "a name must be lower case and belong to a zone that exists", is
expressed directly as a CHECK constraint and a foreign key in the SQL schema.

Database throughput is rarely the limit anyway: PowerDNS shields the backend
with a packet cache, a query cache, and a zone cache that keeps the zone list
in memory so unknown domains never reach the database at all.

## The panel's own data

Users, role assignments, per-zone grants and the audit log are textbook
relational data: a handful of small tables joined by foreign keys, with
uniqueness rules that matter for correctness. `UNIQUE (auth_provider,
external_id)` is what stops two simultaneous SSO logins from creating two
accounts for the same person, and `ON DELETE SET NULL` is what keeps the audit
trail intact after a user is deleted.

MongoDB could hold this -- it has unique indexes too -- but it would mean a
second stateful service to run, back up, monitor and secure, in exchange for
nothing. PostgreSQL is already in the stack because PowerDNS needs it. The
panel gets its own schema (`pdnsadmin`) and its own database role, so the two
concerns stay isolated inside one server.

## The alternative actually worth naming

`lmdb` is the fastest PowerDNS backend and a legitimate choice for a
read-heavy authoritative server. We are not using it because it is an embedded
key-value store on local disk: no network access for a separate admin
container, no SQL for ad-hoc inspection or reporting, and a much more awkward
backup and replication story. For a container stack with a separate web UI,
`gpgsql` is the better fit.

## Verdict

Keep PostgreSQL. One database server, two schemas: `public` for the PowerDNS
tables, `pdnsadmin` for the panel. This is what the repository already targets.

## Sources

- [Backends -- PowerDNS Authoritative Server documentation](https://doc.powerdns.com/authoritative/backends/)
- [Generic SQL Backends -- PowerDNS Authoritative Server documentation](https://doc.powerdns.com/authoritative/backends/generic-sql.html)
- [Remote Backend -- PowerDNS Authoritative Server documentation](https://doc.powerdns.com/authoritative/backends/remote.html)
- [`drop the mongodbbackend, as it does not compile with any recent Mongo...` (PowerDNS/pdns@8ecbcd2)](https://github.com/PowerDNS/pdns/commit/8ecbcd268e0b9d8ab37d8d06c03f457af8a4475c)
- [lanbugs/powerdns_mongodb_backend](https://github.com/lanbugs/powerdns_mongodb_backend)
- [Performance and Tuning -- PowerDNS Authoritative Server documentation](https://doc.powerdns.com/authoritative/performance.html)
- [Auth: Improve Performance for Random Subdomain Attacks with SQL Backends (issue #9326)](https://github.com/PowerDNS/pdns/issues/9326)
