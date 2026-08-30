---
layout: home

hero:
  name: PowerDNS Admin
  text: Authoritative DNS, one command away
  tagline: PowerDNS Authoritative, PostgreSQL and a Tabler-themed admin panel — wired together in a single compose file, with local, LDAP, OAuth 2.0 and SAML sign-in.
  image:
    src: /favicon.svg
    alt: PowerDNS Admin
  actions:
    - theme: brand
      text: Set it up
      link: /setup
    - theme: alt
      text: Read the guide
      link: /guide
    - theme: alt
      text: See the panel
      link: /screenshots

features:
  - icon: 📦
    title: Three services, one file
    details: A PostgreSQL 17 database, PowerDNS Authoritative 4.9 and the Flask panel. Health-gated start-up order, secrets as files, no manual schema loading.
    link: /setup
    linkText: Quick start
  - icon: 🔌
    title: The panel never touches the DNS tables
    details: Every zone and record change goes through the PowerDNS HTTP API, so serial bumping, DNSSEC signing and record validation stay where they belong.
    link: /guide#how-the-pieces-fit
    linkText: How it fits together
  - icon: 🔑
    title: Sign in the way you already do
    details: Local accounts, LDAP / Active Directory, OAuth 2.0 and OpenID Connect, and SAML 2.0 — added in the panel at runtime or declared in .env, with group-to-role mapping for each.
    link: /guide#sign-in-providers
    linkText: Add a provider
  - icon: 🛡️
    title: Separated by schema and by role
    details: No application role is a superuser, and neither can read the other's tables — CI asserts it. The PowerDNS API port is never published to the host.
    link: /guide#the-security-model
    linkText: The security model
  - icon: 🔏
    title: DNSSEC without the ceremony
    details: Enable signing on a zone and a combined signing key is created for you. The DS records to hand your registrar are shown ready to copy.
    link: /guide#dnssec
    linkText: Signing a zone
  - icon: 📋
    title: Every change is on the record
    details: An append-only audit log records who changed what, from which address — and entries survive the deletion of the user who made them.
    link: /guide#the-audit-log
    linkText: What gets logged
---

## Try it in two minutes

```bash
git clone https://github.com/timothestoifl24/powerdns.git
cd powerdns
./scripts/generate-secrets.sh
docker compose up -d --build
```

Open <http://localhost:9191>, sign in as `admin` with the password the script
printed, and check that DNS is actually answering:

```bash
dig @127.0.0.1 example.com SOA
```

That is the whole install. [The setup page](/setup) covers what the script
created, how to move the panel behind a reverse proxy, and what to do when
something already owns port 53.

## What this is, and what it is not

This is a small, self-contained authoritative DNS stack for people who would
rather run their own nameservers than click through a registrar's control
panel — a homelab, an internal network, a handful of public zones.

It is **not** a recursive resolver: it answers only for the zones you create,
and it will not resolve the rest of the internet for your clients. It is also
not a fork of the PowerDNS project's own admin tool; the panel here is a
purpose-built Flask application that speaks the same public HTTP API.

<div class="tip custom-block" style="padding-top: 8px">

New here? Start with the [guide](/guide) for the concepts, then [setup](/setup)
to get it running.

</div>
