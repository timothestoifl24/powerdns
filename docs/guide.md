---
title: Guide
description: What the stack is made of, how the panel talks to PowerDNS, and how the day-to-day work — zones, records, DNSSEC, users and the audit log — actually behaves.
---

# Guide

This page explains what you are running and how it behaves. If you would rather
get it up first and read afterwards, go to [setup](/setup).

## What you get

Three containers, started by one compose file:

| Service | Built from | What it does |
| --- | --- | --- |
| `db` | `postgres:17-alpine` | Holds both halves of the data: PowerDNS's zone tables in `public`, the panel's own tables in the `pdnsadmin` schema. Each half has its own [unprivileged role](#no-application-role-is-a-superuser). |
| `pdns` | Debian trixie + `pdns-server`, `pdns-backend-pgsql` | PowerDNS Authoritative 4.9, serving DNS on port 53 and its HTTP API on 8081. |
| `webui` | `python:3.13-slim` + Flask + gunicorn | The admin panel, on port 8080 in the container and 9191 on the host. |

Start-up order is enforced by health checks rather than by luck: `pdns` and
`webui` both wait for PostgreSQL to report healthy, so nothing starts before the
schema is loaded.

## How the pieces fit

```
                       clients :53 tcp/udp
                              │
                              ▼
┌──────────────┐  HTTP  ┌──────────────┐   forwards local zones
│    webui     │ ─────► │   recursor   │ ──────────────┐
│  Flask +     │        │  PowerDNS    │               │
│  Tabler      │        │  Recursor    │ ──► upstream  │
└──────┬───────┘        └──────────────┘    forwarders │
       │                                               ▼
       │  HTTP API, X-API-Key                 ┌───────────────┐
       └────────────────────────────────────► │     pdns      │
       │                                      │   PowerDNS    │
       │  schema: pdnsadmin                   │ Authoritative │
       │  (users, grants, audit log)          └───────┬───────┘
       │                                              │ schema: public
       └───────────────────┬──────────────────────────┘ (domains, records)
                           ▼
                    ┌──────────────┐
                    │      db      │  PostgreSQL 17
                    └──────────────┘
```

Two DNS servers, because they do different jobs. The **authoritative server**
holds your zones and answers for them and nothing else. The **recursor** is
what clients query: it answers for your zones by asking the authoritative
server, and forwards or resolves everything else. Forwarding has to live there
— PowerDNS Authoritative removed its `recursor=` setting in 4.1 and has no way
to forward at all.

The most important property of this design: **the panel never writes DNS data
itself.** Every zone and record change is a call to the PowerDNS HTTP API, which
means:

- SOA serials are bumped by PowerDNS, using its own `SOA-EDIT-API` rules.
- DNSSEC signing, `ordername` maintenance and the NSEC/NSEC3 records are handled
  by PowerDNS, the only component that understands them.
- Record syntax is validated by the same code that validates a zone transfer, so
  the panel cannot smuggle in something that breaks a zone.

If the panel is down, DNS keeps answering. If PowerDNS is unreachable, the panel
says so on the dashboard and stays usable rather than returning a 500.

## Roles

| Role | Can do |
| --- | --- |
| **admin** | Everything: all zones, user administration, the audit log and the settings page. |
| **operator** | Create, edit and delete every zone. No user administration. |
| **user** | Read and edit only the zones explicitly granted to them. |

Grants for the `user` role are managed per account under **Administration →
Users → *(a user)* → Zone access**. A user with no grants signs in and sees an
empty zone list — that is the intended starting state, not a fault.

Role names are also what the LDAP, OAuth and SAML group mappings resolve to; see
[authentication](/advanced-config#authentication).

## Working with zones

### Creating one

**Zones → New zone** asks for the name, the kind, and the nameservers to seed
the zone with.

- **Native** — the default. PowerDNS serves the zone straight out of PostgreSQL
  and does no zone transfers. If your secondaries share the same database, this
  is what you want.
- **Master** — this server is authoritative and sends `NOTIFY` to secondaries,
  which then transfer the zone with `AXFR`.
- **Slave** — this server transfers the zone from a master. It needs at least
  one master address; the nameserver list is not used, because the master
  supplies the records.

A zone that is not a slave needs at least one nameserver, otherwise it would be
created without an `NS` set and would not resolve. Pre-fill that field for
everyone with `DEFAULT_NAMESERVERS` in `.env`.

Ticking **Enable DNSSEC** on the form signs the zone from the start.

### Editing records

The zone page lists record *sets* — every record sharing one name and type,
which is how DNS actually works and how the API expects changes. The SOA and NS
sets are pinned to the top; the rest follow alphabetically.

Things worth knowing:

- **One value per line.** Two `A` records for `www` are two lines in the same
  set, not two separate rows.
- **`@` means the zone apex.** Names are relative to the zone unless you type a
  fully qualified name ending in a dot.
- **Renaming is a delete plus a write.** PowerDNS has no rename operation, so the
  panel removes the old name/type pair and writes the new one in one submission.
- **CNAME conflicts are caught before the API is called**, so you get a clear
  message instead of a raw API error.
- **`RRSIG`, `NSEC`, `NSEC3`, `NSEC3PARAM`, `DNSKEY`, `CDS` and `CDNSKEY` are
  read-only.** PowerDNS maintains them; they are shown in a separate block so you
  can see the signing state without being able to break it.
- **The SOA cannot be deleted**, only edited.
- **A record can be disabled** instead of deleted: it stays in the zone, stops
  being served, and can be switched back on.
- **Comments** (up to 512 characters) are stored with the record set through the
  API, so they live in the database rather than only in the UI.

### Deleting a zone

Deleting a zone removes every record in it, so the panel makes you type the zone
name to confirm. Only operators and admins can do it.

### Export, notify, retrieve

- **Export** downloads the zone in standard zone-file format, as
  `example.com.zone`. Anyone who can see the zone can export it.
- **Send NOTIFY** tells the secondaries that a master zone changed.
- **Retrieve from master** asks PowerDNS to transfer a slave zone now, rather
  than waiting for the refresh timer.

## Forwarding

Under **Forwarding**, for operators and administrators.

### Global forwarders

Where anything with no more specific rule goes. Set them to your upstream
resolvers to make this a forwarding resolver for the whole network; leave them
empty and the recursor walks down from the root servers itself.

Global forwarders always ask the upstream to recurse, because most public
resolvers refuse a query that does not set that bit.

### Forward zones

One namespace sent somewhere specific:

| Zone | Forward to | Use |
| --- | --- | --- |
| `corp.internal` | `10.0.0.5` | An internal domain another server owns |
| `10.in-addr.arpa` | `10.0.0.5` | Reverse lookups for a private range |
| `ad.example.com` | `10.0.0.5, 10.0.0.6` | Active Directory, two controllers |

A forward zone covers everything under it, so `corp.internal` also covers
`host.corp.internal`. The most specific match wins, and a zone this stack is
authoritative for always wins over both.

Forwarders must be **IP addresses**, not host names — a resolver reads them from
its configuration before it can resolve anything, so a name here would produce a
zone that silently never answers. The form rejects one with that explanation
rather than saving something broken. Add `:port` for anything other than 53, and
write IPv6 with a port as `[2001:db8::1]:5353`.

Leave *Ask the forwarder to recurse* **off** when the target is authoritative
for the zone, such as a domain controller. Turn it **on** when the target is
itself a resolver.

### Your own zones keep working

Because the recursor is the front door, a zone this stack hosts would otherwise
be looked up on the public internet. The panel maintains one forward rule per
authoritative zone pointing at the authoritative server, shown with a **local
zone** badge. Creating or deleting a zone updates them, opening this page
reconciles them, and *Re-check local zone forwarding* does it on demand.

A rule you made yourself is never touched by that reconciliation, even when it
covers a zone of the same name: only rules pointing at the authoritative server
count as the panel's.

### Changes take effect immediately

A resolver does not re-evaluate what it already has cached, and that cache
includes failures. Saving a forward zone therefore also flushes everything
cached under that name — otherwise a query made moments before the change
would keep being answered from the old data, or from a cached `SERVFAIL`, for
as long as the entry lives. Global forwarders flush the whole tree, because
changing where *everything* goes invalidates everything.

If the flush itself fails, the change still stands: the forwarding is correct
and only the rollout is slow. The panel logs a warning saying so.

The resolver hands a changed zone map to its worker threads asynchronously, so
a query made in the same instant as the change can still be answered the old
way — and, being an answer, cached. It settles within a second or two. If you
are testing a rule the moment you save it and see the old answer, query again
rather than concluding the rule is wrong; `rec_control wipe-cache 'zone$'`
inside the recursor container clears it immediately.

### Which half answered?

When a name resolves wrongly, ask each server separately. Publish the
authoritative server with `AUTH_DNS_PORT=5300`, then:

```bash
dig @127.0.0.1 -p 5300 www.example.com    # the authoritative server alone
dig @127.0.0.1 www.example.com            # through the recursor, as clients see it
```

An answer from the first and not the second means the forward rule is missing —
open Forwarding, which reconciles them.

### Not an open resolver

`RECURSOR_ALLOW_FROM` defaults to private networks and loopback. A recursor
reachable from the internet gets found within days and used to amplify
denial-of-service attacks against other people, so widen it only to networks you
control.

## DNSSEC

The DNSSEC page of a zone shows whether signing is on and lists the keys.

**Enabling** does two things in one step: it turns on signing for the zone and,
if the zone has no keys yet, creates an active **CSK** (combined signing key).
That matters — a zone marked signed with no key serves broken answers, which is
worse than an unsigned zone.

The page then shows the **DS records** to hand to your registrar or parent zone.
Until those are published, the zone is signed but the chain of trust is not
complete.

**Disabling** deletes every signing key and then unsets DNSSEC on the zone.
Remove the DS records from the parent *first* and wait for them to expire from
caches, or validating resolvers will fail the zone rather than fall back to
unsigned.

## Users and provisioning

Local accounts are created under **Administration → Users**. Passwords are hashed
with scrypt, and everyone can change their own under **My profile**.

Accounts that come from LDAP, OAuth or SAML are created on first sign-in — from
a provider configured either [in the UI](#sign-in-providers) or in `.env` — and
their name, e-mail and role are refreshed from the provider on *every* sign-in,
so a group change takes effect the next time the person logs in. Two rules are
worth knowing:

- Returning users are matched on the provider's stable subject identifier, not on
  their username. A rename at the identity provider does not create a second
  account or lose their zone grants.
- Single sign-on will **not** adopt an existing local account with the same name.
  Otherwise anyone able to create a matching username at the identity provider
  could take over the local administrator.

## The audit log

Every state-changing action through the panel is appended to `audit_log` in the
panel's own schema: who did it, what to, when, from which address, and whether it
succeeded. Failed attempts are recorded too — a rejected zone deletion is exactly
the sort of thing you want to find later.

Entries survive the deletion of the user who made them: the actor's name is
stored on the row, not only as a foreign key. Admins read the log under
**Administration → Audit log** (200 entries by default, up to 1000), and the ten
most recent also appear on the dashboard.

::: warning The address in the log is only as good as your proxy configuration
`TRUSTED_PROXY_COUNT` defaults to `0`, which means `X-Forwarded-For` is ignored
and the recorded address is whatever connected to the panel. Behind a reverse
proxy that is the proxy's own address until you set the count — and setting it
higher than the number of proxies you actually run lets clients forge the header.
See [running behind a reverse proxy](/setup#behind-a-reverse-proxy).
:::

## Sign-in providers

**Administration → Sign-in providers** is where LDAP, OAuth/OpenID Connect and
SAML providers are added, edited, tested and disabled — at runtime, with no
restart and no redeploy. Providers are read per request, so a change takes effect
across every gunicorn worker immediately.

The same providers can still be declared in `.env`, and both sources work at
once. **The environment always wins**: a provider from `.env` is listed here
read-only, and a database entry that collides with it is ignored and labelled
*Shadowed* rather than silently applied — so adopting the UI never puts a
configuration-as-code deployment at the mercy of whoever holds an administrator
account.

Four properties are worth knowing:

- **Test contacts the provider straight away** — binding to the directory,
  reading the OIDC discovery document, or fetching the IdP metadata. A new
  provider gets verified without asking someone to attempt a sign-in and
  interpret the failure.
- **Configuration is validated on save**, not at sign-in. A provider missing its
  token URL, base DN or IdP certificate is refused with the reason.
- **Secrets are encrypted before they are stored**, with a key derived from
  `SECRET_KEY` via HKDF, so a database dump does not hand over client secrets,
  bind passwords or SAML private keys. The trade-off is real and worth planning
  for: [rotating `SECRET_KEY` makes them unreadable](/upgrading#rotating-secrets).
  They are write-only from the browser too — never rendered back into a form, an
  empty field on save keeps the stored value, and an explicit *Clear* checkbox is
  the only way to remove one.
- **One broken provider cannot take sign-in down.** A provider that fails to
  build is skipped with a log line, and the sign-in page still renders.

Every change is written to the audit log.

## The settings page

**Administration → Settings** is a read-only view of how the panel is configured:
whether PowerDNS answers and which version it reports, the session lifetime, the
cookie flags, the proxy count, and which backends the *environment* enables.

Nothing there is editable, and that is the point: those settings come from the
environment, so the page reports the truth about the running process rather than
keeping a second copy that could drift from it. What you can change at runtime
lives on the Sign-in providers page above.

## The security model

- The PowerDNS API port is **not** published to the host. Only the `webui`
  container reaches it, over the compose network, and PowerDNS's own
  `webserver-allow-from` narrows it further.
- The panel runs as an unprivileged user (uid 10001). PowerDNS starts as root
  only to bind port 53 and read its secrets, then drops to the `pdns` user.
- Every state-changing request needs a CSRF token. The SAML assertion consumer is
  the single exemption — it is a cross-site POST by design, authenticated by the
  assertion's own XML signature.
- Failed logins are rate limited per username and per address. The counter lives
  in each process, so with several gunicorn workers the effective allowance is
  roughly `LOGIN_MAX_ATTEMPTS × workers`.
- Provider secrets are encrypted at rest with a key derived from `SECRET_KEY`, so
  the `auth_providers` table does not expose OAuth client secrets, LDAP bind
  passwords or SAML private keys to anyone holding a database dump.
- Tabler is vendored into the image at build time, so the Content-Security-Policy
  allows no external origins and the panel works on an isolated network.

### No application role is a superuser

`POSTGRES_USER` names a bootstrap role that exists only to run `initdb` and the
scripts in `/docker-entrypoint-initdb.d`; nothing connects as it afterwards.
PowerDNS and the panel each get their own `NOSUPERUSER NOCREATEDB NOCREATEROLE`
role:

| Role | Owns | Can read |
| --- | --- | --- |
| `postgres` | nothing at runtime | — (bootstrap only) |
| `pdns` | schema `public` and the PowerDNS tables | its own tables |
| `pdnsadmin` | schema `pdnsadmin` | its own tables |

`NOCREATEROLE` matters as much as `NOSUPERUSER`: a role that can create roles can
grant itself anything. Neither application role can read the other's tables, and
CI proves it rather than trusting the grants — the smoke test asserts that both
cross-schema reads fail with `permission denied`.

::: warning An existing deployment keeps the old arrangement
Those roles are created by `db/initdb/00-roles.sh`, and everything in
`/docker-entrypoint-initdb.d` runs only on the first start of an empty data
directory. A stack created before this change still runs PowerDNS as the
bootstrap superuser until it is
[migrated by hand](/upgrading#moving-off-the-superuser-role).
:::

## Health endpoints

Both are unauthenticated and safe to point a monitor at:

| Endpoint | Meaning |
| --- | --- |
| `GET /healthz` | The process is alive. Returns `{"status": "ok", "version": …}`. |
| `GET /readyz` | The panel can reach **both** PostgreSQL and the PowerDNS API. Returns `200` with `{"database": true, "powerdns": true}`, or `503` and `"status": "degraded"` when either fails. |

`/readyz` is the one to alert on: it goes degraded when the panel is running but
cannot do anything useful.

## Next

- [Setup](/setup) — install, first sign-in, reverse proxy, port conflicts.
- [Advanced configuration](/advanced-config) — every environment variable, all
  four authentication backends, passing settings through to PowerDNS.
- [Upgrading](/upgrading) — new images, schema changes, backups.
