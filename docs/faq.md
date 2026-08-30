---
title: FAQ
description: The questions and failure modes that come up first — port 53, login loops, OAuth redirects, permissions on secrets, and what this stack is not.
---

# FAQ

## Things that go wrong first

### `failed to bind host port for 0.0.0.0:53: address already in use`

Something already listens on port 53. On most Ubuntu and Fedora systems that is
`systemd-resolved`, which binds `127.0.0.53:53`.

Publish DNS somewhere else:

```bash
echo 'DNS_PORT=5353' >> .env
docker compose up -d
dig @127.0.0.1 -p 5353 example.com SOA
```

…or free port 53 properly by setting `DNSStubListener=no` in
`/etc/systemd/resolved.conf` and restarting `systemd-resolved`. For a nameserver
the outside world will query, 53 is not optional.

### Login appears to succeed, then bounces back to the sign-in page

`SESSION_COOKIE_SECURE=true` over plain HTTP. A secure cookie is never sent back
by the browser, so the next request has no session. Set it to `false` for HTTP,
or put the panel behind TLS.

### `Permission denied` reading `/run/secrets/…`, and the database exits

The files under `secrets/` must be world-readable (`0644`). Compose bind-mounts
them with their host ownership intact, and the processes reading them are
unprivileged: `postgres` (uid 70) runs the initdb scripts, and the panel runs as
uid 10001. The `0700` directory is what keeps other users on the host out.

`./scripts/generate-secrets.sh` sets this correctly and repairs an existing
directory. By hand:

```bash
chmod 0700 secrets && chmod 0644 secrets/*
```

### The stack started once with an error, and now the panel cannot connect

PostgreSQL runs the scripts in `db/initdb/` **only while the data directory is
empty**. If one of them fails, the directory is left half-initialised, and every
later start logs *"Database directory appears to contain a database; Skipping
initialization"* — so the panel's role and schema never get created.

Start over with a clean volume:

```bash
docker compose down -v && docker compose up -d --build
```

`down -v` deletes every zone along with the volume. On a system with real data,
[dump the database first](/upgrading#back-up-first).

### The OAuth provider rejects the redirect URI

Set `BASE_URL` to the public URL of the panel. Without it the panel derives the
callback from the request it sees, which behind a proxy is the internal
hostname, and providers match the registered URI exactly.
**Administration → Settings** prints the exact URI to register.

### The panel says PowerDNS is unreachable

Usually the API key. `pdns` and `webui` read the same `secrets/pdns_api_key`; if
you replaced it, restart both. Otherwise check that the panel can reach the API
at all:

```bash
docker compose exec webui python -c \
  "import urllib.request; print(urllib.request.urlopen('http://pdns:8081').status)"
```

A connection error points at the compose network or at
`PDNS_WEBSERVER_ALLOW_FROM` being narrower than the network the containers are
on.

### A record I added is not being served

Check in this order:

1. Is the record disabled? Disabled records stay in the database and are not
   served.
2. Did you query the right port? With `DNS_PORT=5353` you need
   `dig -p 5353`.
3. Is the zone's `NS` set pointing at nameservers you actually run? PowerDNS will
   serve the zone regardless, but nothing else will find it.
4. For a slave zone, has the transfer happened? **Actions → Retrieve from
   master**.

## About the design

### Is this a recursive resolver?

No. It is authoritative only: it answers for the zones you create and refuses
everything else. Pointing a client's resolver at it will not resolve the
internet. Run Unbound, `dnsdist` or PowerDNS Recursor for that.

### Why does the panel go through the HTTP API instead of writing to the database?

Because the database is not the interface. Serial bumping, DNSSEC signing,
`ordername` maintenance and record validation all live in PowerDNS; a panel that
wrote rows directly would have to reimplement them and would drift out of step at
every upgrade. Going through the API also means the panel's database role needs
no rights at all on the DNS tables — which is what makes the privilege separation
real rather than decorative.

### Why PostgreSQL and not MySQL?

Both are first-class PowerDNS backends and either would work. PostgreSQL was
chosen for the schema separation the panel depends on, and because the panel's
own tables want proper constraints. Moving to `gmysql` means swapping the backend
in `pdns/entrypoint.sh` and the schema in `db/initdb/`; the panel itself would
need its SQLAlchemy URL changed. Nobody has done it, so treat it as unsupported.

### Why not MongoDB?

There is no supported MongoDB backend for PowerDNS — the in-tree one was removed
in 2013 — and the workload is exact-match point lookups, which is precisely where
a document store offers nothing. The long version is on
[its own page](/database-choice).

### Can I run several nameservers from this?

Yes, two ways:

- **Shared database.** Run the `pdns` container on several hosts against the same
  PostgreSQL, with zones of kind `Native`. Every server serves the same data with
  no transfers.
- **Master and slaves.** Keep this stack as the master and let other
  nameservers — any implementation — transfer the zones with `AXFR`. Set the
  zones to `Master` and allow transfers with
  `PDNS_SETTING_allow_axfr_ir=<their addresses>`.

### Does it do DNSSEC properly?

PowerDNS does the signing; the panel is a control surface for it. Enabling
DNSSEC creates an active CSK, and the page shows the DS records to publish in the
parent zone. Key rollovers beyond that — separate KSK/ZSK, scheduled rollovers —
are `pdnsutil` territory, and the panel does not get in their way.

### Is there an API?

Not on the panel — it is a server-rendered application. What you want is the
PowerDNS API underneath it, which is a documented, stable HTTP interface. It is
not published to the host by design, so reach it from inside the network, or add
a port mapping and firewall it yourself.

The panel exposes two unauthenticated endpoints for monitoring: `/healthz`
(liveness) and `/readyz` (database and PowerDNS reachable).

### Can I disable local accounts entirely?

Yes: `LOCAL_AUTH_ENABLED=false` once an external provider works. Do it in that
order — with no working backend, nobody can sign in, and the fix is editing
`.env` and restarting.

Note that single sign-on will not adopt an existing local account with the same
username. That is deliberate: otherwise anyone able to create a matching username
at the identity provider could take over the local administrator.

### I deleted my only administrator

The bootstrap administrator is recreated only while the user table is *empty*, so
that will not help. Promote an existing account directly:

```bash
docker compose exec -T db psql -U pdns -d pdns \
  -c "UPDATE pdnsadmin.users SET role='admin' WHERE username='you'"
```

The panel reads the role on every request, so it takes effect immediately.

### How do I change the admin password without the panel?

You cannot set a password with SQL — they are scrypt hashes, not plaintext. Use
the panel, or delete the user row and let the bootstrap administrator be created
again on the next start when no users remain.

## Operating it

### Where is the data?

In the named volume `pgdata`, not in the project directory. Both halves of the
system are in the one database: PowerDNS's tables in the `public` schema, the
panel's in `pdnsadmin`. A single `pg_dump` is a complete backup.

### How do I see what someone changed?

**Administration → Audit log**: every state-changing action, who did it, from
which address, whether it succeeded. Entries survive the deletion of the user who
made them.

If the address column shows your proxy rather than the real client, set
`TRUSTED_PROXY_COUNT` to the number of proxies you actually run.

### What resources does it need?

Small. The three containers idle at a few hundred megabytes of RAM between them,
and a zone of a few thousand records is nothing for PostgreSQL. PowerDNS
Authoritative comfortably serves tens of thousands of queries per second on
modest hardware; the panel will never be the bottleneck, because it is not in the
query path at all.

### Is it safe to expose the panel to the internet?

It is an administrative interface — prefer a VPN or a private network. If you
must publish it: HTTPS with `SESSION_COOKIE_SECURE=true`, `TRUSTED_PROXY_COUNT`
set correctly, single sign-on with `LOCAL_AUTH_ENABLED=false`, and
`*_DEFAULT_ROLE=none` so only members of a mapped group get an account.

The DNS port is a different question: that one is meant to be public if you are
running public nameservers.

### Something else is broken

Logs first:

```bash
docker compose logs --tail 100 webui
docker compose logs --tail 100 pdns
docker compose ps
```

Then [open an issue](https://github.com/timothestoifl24/powerdns/issues) with
what you ran, what you expected and what the logs said. Redact the API key.
