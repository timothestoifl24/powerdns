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

### An LDAP user in the admin group signs in as a normal user

Open their entry under **Administration → Users**. It lists the groups the
directory reported at their last sign-in, and that list — not what the directory
holds — is what the mapping is compared against.

If the group is there, the mapping does not match it. Names are compared without
regard to case, and both the full DN and its first component count, so
`LDAP_ADMIN_GROUP=DNS-Admins` matches
`CN=DNS-Admins,OU=Groups,DC=example,DC=com`. A trailing space or a different
group of the same name in another OU will not.

If the list is empty the directory sent nothing, which is a lookup problem. The
common causes are a directory that records membership only on the group object
(set `LDAP_GROUP_SEARCH_BASE`) and nested Active Directory groups (`memberOf`
reports only direct membership). [Finding a user's
groups](/advanced-config#finding-a-user-s-groups) covers both.

### A role I set by hand went back to what the groups say

Roles set by hand are pinned and are not supposed to revert; releases before
v1.0.1 recomputed them from the group mapping on every sign-in. Upgrade, then
set the role again — the padlock beside it in the user list means it is pinned.

Note that pinning survives sign-ins but does not grant admission: with
`*_DEFAULT_ROLE=none`, an account that matches no mapped group is still refused.

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
callback from the request it sees, which behind a proxy is the internal hostname,
and providers match the registered URI exactly.

The URI to register is printed for you: on **Administration → Settings** for
providers declared in `.env`, and on the provider's own page under
**Administration → Sign-in providers** for ones added in the UI.

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

### A forwarded zone returns SERVFAIL to `dig` but works otherwise

That is DNSSEC. `dig` sets the DNSSEC OK bit by default; a resolver that
validates asks the root whether the forwarded name is signed, is told it does
not exist there, and SERVFAILs a correct answer. The stack ships with
validation off for this reason — see
[DNSSEC and forwarding](/guide#dnssec-and-forwarding). If you turned it on,
add the zone to `RECURSOR_NEGATIVE_TRUSTANCHORS`.

### I added a forward zone and it still returns the old answer

It should not: saving a forward zone flushes everything the resolver had
cached under that name. If you changed the forwarding another way — through
the recursor's API directly, or by editing its configuration — flush it
yourself:

```bash
docker compose exec recursor rec_control wipe-cache corp.internal$
```

The `$` makes it match the subtree rather than only the exact name.

### A zone resolves on the authoritative server but not through the recursor

The recursor needs a forward rule pointing at the authoritative server for each
zone this stack hosts. Open **Forwarding** — loading the page reconciles them —
or press *Re-check local zone forwarding*. The zone should appear with a **local
zone** badge.

### Forward zones vanished after `docker compose down`

They live in the `recursor-api` volume, which `down -v` deletes along with the
database. Without `-v` they survive.

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

Both, and deliberately. The stack runs two DNS servers: PowerDNS Authoritative
holds the zones you create, and PowerDNS Recursor sits in front on port 53. The
recursor answers for your zones by asking the authoritative server, and handles
everything else according to the [Forwarding](/guide#forwarding) page — either
forwarding to upstream resolvers you choose, or resolving from the root servers
itself.

They are separate because PowerDNS Authoritative genuinely cannot forward: the
`recursor=` setting was removed in version 4.1.

If you only want authoritative service, remove the `recursor` service from
`compose.yml`, unset `RECURSOR_API_URL` and publish the authoritative server on
`DNS_PORT`. The Forwarding page then says it is unavailable instead of failing.

### Can I forward a domain to our Active Directory servers?

Yes — that is what forward zones are for. Under **Forwarding**, add the domain
with your domain controllers' IP addresses as forwarders, and leave *Ask the
forwarder to recurse* off: they are authoritative for that zone. Add the reverse
zone the same way, for example `10.in-addr.arpa`.

Forwarders have to be IP addresses. A resolver reads them from its configuration
before it is capable of resolving a name, so `dc1.corp.internal` cannot work
there.

### Why does a forward zone for `192.168.in-addr.arpa` say the zone exists?

The recursor serves the RFC 1918 reverse zones itself, so it already knows that
name. Saving from the Forwarding page replaces it, so it just works; you only
see that error posting to the recursor's API by hand.

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

There is no supported MongoDB backend for PowerDNS: the in-tree one was removed
in 2013 and never replaced, so the only route would be the `remote` backend —
a second daemon of your own in the hot path of every query, re-implementing the
DNSSEC ordering callbacks. And the workload would not reward it. PowerDNS does
exact-match point lookups plus one ordered range scan on `ordername`; there are
no joins, no aggregations and no nested documents, which is precisely where a
document store offers nothing a B-tree index does not already give you.

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

### Do I have to restart the stack to add an identity provider?

No. **Administration → Sign-in providers** adds LDAP, OAuth/OIDC and SAML
providers at runtime, with a Test button that contacts the provider before anyone
tries to sign in. Providers are read per request, so a change applies to every
gunicorn worker immediately.

`.env` still works, and takes precedence: a provider declared there is read-only
in the UI, and a database entry that collides with it is ignored and shown as
*Shadowed*. Use `.env` when the configuration is deployed as code, or when you
need a provider configured before there is an administrator to sign in as.

### I rotated `SECRET_KEY` and my providers stopped working

Provider secrets are encrypted with a key derived from `SECRET_KEY`, so replacing
`secrets/webui_secret_key` makes them unreadable. The panel reports that on the
provider page rather than failing at sign-in; re-enter each secret and it works
again. Providers configured in `.env` are unaffected —
[details](/upgrading#rotating-secrets).

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
docker compose exec -T db psql -U pdnsadmin -d pdns \
  -c "UPDATE pdnsadmin.users SET role='admin' WHERE username='you'"
```

The panel reads the role on every request, so it takes effect immediately. Note
the role: `pdnsadmin` owns that schema, and the `pdns` role cannot read it.

### How do I change the admin password without the panel?

You cannot set a password with SQL — they are scrypt hashes, not plaintext. Use
the panel, or delete the user row and let the bootstrap administrator be created
again on the next start when no users remain.

## Operating it

### Where is the data?

In the named volume `pgdata`, not in the project directory. Both halves of the
system are in the one database: PowerDNS's tables in the `public` schema, the
panel's in `pdnsadmin`. A single `pg_dump` is a complete backup — taken as the
bootstrap superuser, because neither application role can read the other's
schema. See [backups](/upgrading#back-up-first).

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
