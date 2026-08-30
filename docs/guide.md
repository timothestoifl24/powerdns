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
| `db` | `postgres:17-alpine` | Holds both halves of the data: PowerDNS's zone tables in `public`, the panel's own tables in the `pdnsadmin` schema. |
| `pdns` | Debian trixie + `pdns-server`, `pdns-backend-pgsql` | PowerDNS Authoritative 4.9, serving DNS on port 53 and its HTTP API on 8081. |
| `webui` | `python:3.13-slim` + Flask + gunicorn | The admin panel, on port 8080 in the container and 9191 on the host. |

Start-up order is enforced by health checks rather than by luck: `pdns` and
`webui` both wait for PostgreSQL to report healthy, so nothing starts before the
schema is loaded.

## How the pieces fit

```
┌──────────────┐        HTTP API         ┌───────────────┐
│    webui     │ ──────────────────────► │     pdns      │  :53 tcp/udp
│  Flask +     │       X-API-Key         │   PowerDNS    │
│  Tabler      │                         │ Authoritative │
└──────┬───────┘                         └───────┬───────┘
       │  schema: pdnsadmin                      │  schema: public
       │  (users, grants, audit log)             │  (domains, records, DNSSEC)
       └────────────────┬────────────────────────┘
                        ▼
                 ┌──────────────┐
                 │      db      │  PostgreSQL 17
                 └──────────────┘
```

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

Accounts that come from LDAP, OAuth or SAML are created on first sign-in, and
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

## The settings page

**Administration → Settings** is a read-only view of how the panel is configured:
which authentication backends are on, what each group maps to, whether PowerDNS
answers and which version it reports, the session lifetime, the cookie flags and
the proxy count. It also prints the exact redirect URI to register with each
OAuth provider — the value people most often get wrong.

Nothing there is editable. Configuration lives in the environment, so the page
shows the truth about the running process rather than a second copy that could
drift from it.

## The security model

- The PowerDNS API port is **not** published to the host. Only the `webui`
  container reaches it, over the compose network, and PowerDNS's own
  `webserver-allow-from` narrows it further.
- The panel runs as an unprivileged user (uid 10001). PowerDNS starts as root
  only to bind port 53 and read its secrets, then drops to the `pdns` user.
- The panel's database role owns only its own schema and has no rights on the
  PowerDNS tables.
- Every state-changing request needs a CSRF token. The SAML assertion consumer is
  the single exemption — it is a cross-site POST by design, authenticated by the
  assertion's own XML signature.
- Failed logins are rate limited per username and per address. The counter lives
  in each process, so with several gunicorn workers the effective allowance is
  roughly `LOGIN_MAX_ATTEMPTS × workers`.
- Tabler is vendored into the image at build time, so the Content-Security-Policy
  allows no external origins and the panel works on an isolated network.

One known limitation, stated plainly: the role PowerDNS uses for its own tables
is the one the postgres image creates from `POSTGRES_USER`, which is a superuser.
That is how nearly every PowerDNS container guide is set up, but it is more
privilege than PowerDNS needs. The panel's role is *not* a superuser and owns
only its own schema, so the separation the panel relies on still holds.

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
