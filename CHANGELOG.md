# Changelog

Notable changes to this stack, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

Each released version is also a
[GitHub release](https://github.com/timothestoifl24/powerdns/releases), where
the entry below is followed by the full list of merged pull requests, generated
from their labels. Upgrade instructions live in
[docs/upgrading.md](docs/upgrading.md).

## [Unreleased]

Everything here is additive: no setting changes meaning, and a stack that
pulls without ticking any of the new boxes behaves exactly as it did. The
panel creates two tables of its own, `reverse_links` and `zone_reverse_links`,
on start-up.

### Added

- **Each zone has a settings page**, at **Actions → Zone settings**, for the
  zone itself rather than the records in it: its kind and a slave's master
  addresses, the apex nameservers, the SOA — primary nameserver, the
  administrator's email address, and the refresh, retry, expire and
  negative-TTL timers — and the reverse zones it is linked to.

  The administrator's address is edited as an address and stored as DNS wants
  it: the `@` becomes a dot and a dot before the `@` is escaped, so
  `first.last@example.com` is written as `first\.last.example.com.` and read
  back as an address rather than as a name with an extra label. The serial is
  shown but not editable, because PowerDNS bumps it on every change and a hand
  edit would either be overwritten or, going backwards, stop secondaries
  transferring. A slave zone takes its nameservers and SOA from its master, so
  those fields are not offered for one and a hand-posted value is ignored
  rather than written over the next transfer.

  Anyone who can edit a zone's records can use the page -- the record editor
  already reaches the same `NS` and `SOA` sets. Changing the kind stays with
  operators, since that decides how the zone is served rather than what it
  answers.

- **A zone can be linked to its reverse zones**, on that same page: tick the
  ones it should use, or (as an operator) create one from a network and link it
  in the same save. The pairing makes the reverse record a default rather than
  something to remember -- the record editor in a linked zone opens with *Also
  create a matching PTR record* already ticked, and names the zone the `PTR`
  will go into -- and it decides where the `PTR` goes when more than one zone
  could take it: linked zones are consulted first, anything else on the server
  after. A zone linked to `0.192.in-addr.arpa` therefore writes there even if
  an unlinked `2.0.192.in-addr.arpa` would have been the narrower match, while
  an address no linked zone covers still finds any reverse zone that does.
  Linking never restricts what can be written, and unticking a zone leaves
  every existing `PTR` following its record -- it only stops new records
  defaulting there.

- **A zone can be created together with its reverse zones.** Tick *Also create
  the reverse zone for this zone's networks* on the new-zone form and give the
  networks in CIDR notation, one per line. Each becomes an `in-addr.arpa` or
  `ip6.arpa` zone created alongside the forward one, with the same kind,
  nameservers and DNSSEC setting — `192.0.2.0/24` is `2.0.192.in-addr.arpa`,
  `2001:db8::/48` is `0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa`.

  Reverse delegation lands on whole octets (IPv4) or nibbles (IPv6), so a
  prefix that does not is resolved to the ones that do: a /23 becomes the two
  /24s it spans, while a /26 becomes the /24 it sits inside, because a /26 has
  no reverse zone of its own without RFC 2317 delegation — an arrangement with
  whoever delegates the /24 rather than something to infer from a prefix. A
  network needing more than 16 zones is refused rather than expanded, on the
  grounds that it is far likelier to be a typo than a request. A mistyped
  network is a form error, so nothing is created; a reverse zone that fails on
  its own (usually because it already exists) is reported without taking the
  forward zone down with it.

- **An `A`/`AAAA` record can own its `PTR`, and keep it in step.** Tick *Also
  create a matching PTR record* in the record editor and the panel writes the
  PTR into whichever reverse zone on this server covers the address.
  From then on the reverse side follows the forward record: change the address
  and the PTR moves, rename the record and the PTR answers with the new name,
  add a second address and it gets its own PTR, disable or delete the record
  and the PTR goes with it. Unticking the box removes the PTR and leaves the
  address record alone. Both ends are marked on the zone page.

  Nothing is created behind your back: if no reverse zone covers the address,
  the forward record is saved and the panel says which zone is missing. Zone
  access applies to both ends — a user granted the forward zone but not the
  reverse one gets their record saved and a note that the PTR was left alone,
  rather than a refusal. An address has one reverse answer, so pointing a
  second record at a linked address takes the PTR over and says so. Editing a
  PTR by hand ends the link: it becomes yours, and the panel neither updates
  nor deletes it afterwards.

  The link is panel metadata in the panel's own schema. Both records live in
  PowerDNS and are written through its API like every other change, so losing
  the links would leave DNS exactly as it is and only stop the pairs being kept
  in step.

- **LDAP accepts more than one server, for failover.** `LDAP_URI` takes a list
  separated by commas, and the *Server URIs* field in the web UI is one per
  line. They are tried in order — the list is a preference, not load balancing
  — and the first that answers handles the sign-in. A server that cannot be
  reached is left out for a minute rather than retried on every attempt, and
  rejoins by itself when it recovers, so a dead domain controller costs one
  connect timeout instead of one per sign-in. The search and the password bind
  always go to the same server, so a replica that has not caught up cannot
  reject an account the other one just returned. `ldaps://` and `ldap://`
  entries can be mixed: StartTLS is negotiated only where it is needed. A
  single URI behaves exactly as before, and *Test* now names the server that
  answered.

## [1.1.0] — 2026-09-19

Moves the database to PostgreSQL 18, which existing deployments cannot take by
pulling: the data has to be dumped and reloaded, and the volume's mount point
changes with it. Read
[upgrading](https://powerdns.stoifl.app/upgrading#upgrading-to-postgresql-18)
before you pull, and take the dump while 17 is still running.

Rehearse the reload on a copy before you do it for real. An empty volume starts
on 18 with nothing to do — that case is covered by CI — so the restore into the
new cluster is the only part that can genuinely go wrong, and the only part
nothing here can test for you.

A new stack is unaffected — `docker compose up -d --build` just starts on 18.

### Breaking changes

- **The `db` service is PostgreSQL 18**, up from 17. PostgreSQL will not start
  against a data directory written by an older major version, so an existing
  `pgdata` volume must be dumped with `pg_dumpall`, deleted and reloaded. The
  upgrade guide has the exact sequence, including how to check the dump before
  the destructive step.
- **The `pgdata` volume is mounted at `/var/lib/postgresql`**, not at
  `/var/lib/postgresql/data`. The 18 image keeps the cluster in
  `/var/lib/postgresql/18/docker` and declares the parent directory as its
  volume, which is the layout `pg_ctlcluster` uses and what allows a future
  `pg_upgrade --link` to see both clusters without crossing a mount boundary.
  Anyone running a modified `compose.yml` needs to move this mount by hand.

  Left at the old path, the container stops with *in 18+, these Docker images
  are configured to store database data in a format which is compatible with
  "pg_ctlcluster"* rather than starting on an empty database beside the real
  one. That refusal is upstream's, and it is the good outcome: the data is
  still there.

### Changed

- Dependabot's deliberate pin on the `postgres` major now holds the stack at
  18; the jump to 19 stays a planned dump and restore rather than a merged
  pull request.
- The upgrade guide gained a PostgreSQL 18 section covering the dump, the moved
  mount, verification afterwards, how to defer the upgrade, and why a rollback
  needs the 17 dump rather than a fresh one.

### Fixed

- **Podman's own DNS no longer collides with the recursor.** Documented, not
  changed in code: Podman resolves container names with aardvark-dns, which
  binds port 53 on the bridge address (`172.29.0.1` for the `backend`
  network). The default `DNS_BIND_ADDRESS=0.0.0.0` claims port 53 on every
  address including that one, so the two cannot both start — with
  `systemd-resolved` running the recursor fails to publish, and with it
  stopped the recursor wins and aardvark-dns loses, leaving a stack that comes
  up but cannot resolve `db`. That second failure looks like stopping the
  resolver caused it, which is why it was worth writing down. Naming a real
  address for `DNS_BIND_ADDRESS` avoids both and lets `systemd-resolved` stay
  running; `setup.md` also covers moving aardvark-dns with
  `dns_bind_port`. The default stays `0.0.0.0`, which is correct on Docker —
  its embedded DNS answers inside the container namespace and never competes
  for a host address.
- **The images build under Podman.** Every `FROM` now names its registry in
  full (`docker.io/library/…`). Podman has no implicit `docker.io`, so on a host
  without `unqualified-search-registries` in `/etc/containers/registries.conf`
  the bare names failed with *short-name … did not resolve to an alias*, and
  `podman compose up --build` reported it only as `Build command failed` at the
  end of the run. Docker resolves the fully qualified form identically, so
  nothing changes there.

## [1.0.1] — 2026-09-06

The stack as it now stands. v1.0.0 tagged a much earlier codebase, and
everything below has landed since; if you are coming from it, read
[upgrading](https://powerdns.stoifl.app/upgrading) rather than pulling.

### Added

- **Images are published for `linux/arm64` as well as `linux/amd64`.** Every
  tag is now a manifest list, so `docker pull` picks the right build by itself
  on Graviton, Ampere, a 64-bit Raspberry Pi or Apple silicon. `arm64` and
  `aarch64` are the same architecture, so there is no third image to look for.
  Each platform is built natively rather than under QEMU, and the end-to-end
  compose smoke test now runs on both, so the ARM images are exercised over
  real DNS rather than only compiled.
- **The stack itself.** PostgreSQL 17, PowerDNS Authoritative 4.9 on the
  `gpgsql` backend, PowerDNS Recursor 5.2 and a Flask admin panel, started by
  one `compose.yml`. The schema is loaded on first start and start-up order is
  enforced by health checks rather than by timing.
- **An admin panel** on the Tabler theme: zones, record sets, DNSSEC, an audit
  log, per-user zone grants, and three roles (`admin`, `operator`, `user`). It
  never writes DNS data itself — every change is a call to the PowerDNS HTTP
  API, so PowerDNS keeps ownership of SOA serials, signing and record
  validation.
- **Forward zones and global forwarders**, on the bundled recursor and managed
  from the panel's **Forwarding** page. Global forwarders catch everything with
  no more specific rule; a forward zone sends one namespace to servers you
  name. The zones this stack is authoritative for are forwarded to the
  authoritative server automatically, and reconciled whenever the page is
  opened. Forwarding lives in the recursor because PowerDNS Authoritative
  removed its `recursor=` setting in 4.1 and cannot forward at all.
- **Sign-in with LDAP/Active Directory, OAuth 2.0/OpenID Connect and SAML 2.0**,
  with group-to-role mapping for each. Providers can be added, tested and
  disabled from **Administration → Sign-in providers** at runtime, with no
  restart; their client secrets are encrypted at rest with `SECRET_KEY`.
- **A role can be pinned by hand.** Choosing an external user's role stops the
  group mapping from recomputing it, and a padlock marks it in the user list.
  Admission is still the mapping's decision, so pinning is not a way to keep
  access after losing every mapped group.
- **The groups a directory reported** at a user's last sign-in are recorded and
  shown on their page — the list the role mapping is actually compared against,
  which is what makes a mapping that does not match diagnosable.
- **A documentation site** at [powerdns.stoifl.app](https://powerdns.stoifl.app),
  built with VitePress from `docs/`.
- **A security policy** (`.github/SECURITY.md`) and Dependabot updates across
  all four dependency surfaces.

### Fixed

- **A manually assigned role no longer reverts on the next sign-in.** External
  accounts had their role recomputed from the group mapping every time, so an
  administrator appointed by hand was demoted the moment they logged in again.
- **LDAP group membership is found the way directories actually publish it.**
  Attribute names are matched without regard to case; when the configured
  attribute comes back empty the panel tries `memberOf`, `isMemberOf`, `nsRole`
  and `groupMembership`; and setting a group search base runs a search against
  the group objects for directories that record membership only there. A
  sign-in that finds no groups at all now logs a warning naming what was tried,
  instead of silently handing out the default role. This is why an account in
  the mapped admin group could sign in as a plain user.
- **Forwarded zones are no longer DNSSEC-validated.** A zone answered outside
  the public chain has no signature to find, so a validating resolver returned
  SERVFAIL for a perfectly correct answer — including for every zone this stack
  hosts, since each one is forwarded to the authoritative server. Validation
  defaults to `process-no-validate`; turn it on with `RECURSOR_DNSSEC` and list
  your internal zones in `RECURSOR_NEGATIVE_TRUSTANCHORS`.
- **Saving or deleting a forward zone flushes the resolver's cache** for that
  name and everything under it, so a changed rule takes effect immediately
  rather than after the old answer's TTL.
- **The recursor is configured in YAML.** Recursor 5.2 no longer reads the
  classic `key=value` settings file unless explicitly asked to, and a config it
  cannot parse stops the container.

### Security

- **Neither application role is a superuser.** PowerDNS and the panel each own
  one schema and are denied the other's tables; a separate bootstrap role does
  the privileged setup. CI proves the isolation rather than assuming it.
- **Secrets are files, not environment variables**, generated by
  `scripts/generate-secrets.sh` and never committed.
- **The recursor is not an open resolver.** `RECURSOR_ALLOW_FROM` defaults to
  loopback and the private ranges, and CI fails the build if that default ever
  widens to `0.0.0.0/0`.
- CSRF protection on every state-changing form, scrypt password hashing, and an
  audit log that records failed attempts as well as successful ones.
- `TRUSTED_PROXY_COUNT` defaults to `0`, so `X-Forwarded-For` is ignored until
  you state how many proxies you actually run.

### Changed

- **Port 53 is the recursor.** The authoritative server moved behind it and is
  no longer published on `DNS_PORT`; publish it separately with
  `AUTH_DNS_PORT` if you want to query it directly.
- The panel's `users` table gained `role_locked` and `last_groups`. They are
  added on start-up, so no manual migration is needed.

[Unreleased]: https://github.com/timothestoifl24/powerdns/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/timothestoifl24/powerdns/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/timothestoifl24/powerdns/compare/v1.0.0...v1.0.1
