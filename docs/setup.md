---
title: Setup
description: From a clean host to a working authoritative nameserver — requirements, the first sign-in, ports, TLS and a reverse proxy.
---

# Setup

## Requirements

- Docker Engine 24+ with the Compose plugin (`docker compose`, not
  `docker-compose`). Podman 5 with `podman compose` works too.
- About 1 GB of RAM and 2 GB of disk for the three images and the database.
- Ports **9191** (the panel) and **53** (DNS) free on the host. Port 53 is the
  one that usually is not — see [below](#something-already-owns-port-53).

Nothing else. There is no Node, Python or PowerDNS installation on the host: it
all happens in the images.

## Install

```bash
git clone https://github.com/timothestoifl24/powerdns.git
cd powerdns

./scripts/generate-secrets.sh
docker compose up -d --build
```

The first build takes a few minutes — Debian packages for PowerDNS, Python
wheels for the panel, and the Tabler theme, which is downloaded once at build
time and baked into the image.

When it finishes, open <http://localhost:9191> and sign in as `admin` with the
password the script printed. If the output scrolled away:

```bash
cat secrets/webui_admin_password
```

**Change that password under *My profile* straight away.** It is only used while
the user table is empty, but it is sitting in a file on disk.

### What the script created

| Path | Contents |
| --- | --- |
| `secrets/db_superuser_password` | PostgreSQL bootstrap superuser, used only by `initdb` |
| `secrets/pdns_db_password` | PostgreSQL password for the PowerDNS role |
| `secrets/webui_db_password` | PostgreSQL password for the panel's role |
| `secrets/pdns_api_key` | Shared secret for the PowerDNS HTTP API |
| `secrets/webui_secret_key` | Flask session signing key |
| `secrets/webui_admin_password` | Password for the first-run administrator |
| `.env` | Non-secret settings, copied from `.env.example` |

`secrets/` and `.env` are gitignored. The script is safe to re-run: it never
overwrites an existing secret, and it repairs the file permissions if an older
checkout left them wrong.

::: tip Secrets can be plain environment variables instead
Every secret accepts both `FOO` and `FOO_FILE`, and the file wins when both are
set — which is what makes the same images work unchanged with Docker Swarm
secrets or Kubernetes secret mounts.
:::

## Check that it actually works

Three things are worth confirming, in this order.

**The panel is up and can reach everything it needs:**

```bash
curl -s http://localhost:9191/readyz
```

You want `{"status": "ok", "database": true, "powerdns": true}`. A `503` with
`"powerdns": false` means the panel is running but the API is unreachable.

**DNS answers on port 53:**

```bash
dig @127.0.0.1 example.com SOA
```

A `REFUSED` here is the *correct* answer before you have created any zone — it
proves PowerDNS is listening and authoritative for nothing yet.

**A zone you create is really served.** Create one in the panel (**Zones → New
zone**), add an `A` record for `www`, then:

```bash
dig @127.0.0.1 www.example.com A +short
```

That round trip — panel → HTTP API → PostgreSQL → DNS answer — is the whole
system working.

## Configuration you will want on day one

Everything lives in `.env`, and `.env.example` documents every key. The values
most people change first:

```bash
SITE_NAME=DNS at example.com
WEBUI_PORT=9191
# Pre-fills the "new zone" form.
DEFAULT_NAMESERVERS=ns1.example.com,ns2.example.com
# The SOA used for new zones that do not override it.
PDNS_DEFAULT_SOA_CONTENT=ns1.example.com hostmaster.example.com 0 10800 3600 604800 3600
```

After editing `.env`:

```bash
docker compose up -d
```

Compose recreates only the containers whose configuration changed. The database
volume is untouched, so no zones are lost.

Single sign-on does not need `.env` at all: LDAP, OAuth/OIDC and SAML providers
can be added under **Administration → Sign-in providers** while the stack is
running. See [the guide](/guide#sign-in-providers) for how the two sources
interact, and [advanced configuration](/advanced-config#authentication) for the
settings themselves.

## Something already owns port 53

`failed to bind host port for 0.0.0.0:53: address already in use` means a local
resolver has it. On most Ubuntu and Fedora systems that is `systemd-resolved`,
which binds `127.0.0.53:53`.

Either publish DNS on a different host port:

```bash
echo 'DNS_PORT=5353' >> .env
docker compose up -d
dig @127.0.0.1 -p 5353 example.com SOA
```

…or free port 53 by turning off the stub listener — set `DNSStubListener=no` in
`/etc/systemd/resolved.conf` and restart `systemd-resolved`.

The container always serves on 53 internally; `DNS_PORT` only affects the host
side. For a nameserver that the outside world will query, 53 is not optional —
free it properly rather than remapping.

To keep DNS on the loopback interface while you are still testing:

```bash
DNS_BIND_ADDRESS=127.0.0.1
```

## Behind a reverse proxy

This is the normal way to run it in production: TLS terminates at the proxy, the
panel stays on an internal network.

```bash
BASE_URL=https://dns.example.com
SESSION_COOKIE_SECURE=true
TRUSTED_PROXY_COUNT=1
```

Three settings, three reasons:

- **`BASE_URL`** is the public URL, without a trailing slash. OAuth and SAML have
  to hand the identity provider an absolute redirect URL; without this the panel
  derives it from the request it sees, which behind a proxy is the internal
  hostname, and the provider's exact-match check fails.
- **`SESSION_COOKIE_SECURE=true`** is required over HTTPS and *breaks login over
  plain HTTP* — a secure cookie is never sent back, so sign-in appears to succeed
  and then bounces you to the login page.
- **`TRUSTED_PROXY_COUNT`** is the number of proxies in front of the panel. It
  defaults to `0`, meaning `X-Forwarded-For` is ignored entirely: any client can
  set that header, and trusting it blindly would let them forge the address in
  the audit log. Set it to the real number — not higher.

A minimal nginx server block:

```nginx
server {
    listen 443 ssl http2;
    server_name dns.example.com;

    ssl_certificate     /etc/letsencrypt/live/dns.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dns.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:9191;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then bind the panel to the loopback interface so it is only reachable through
the proxy — in `compose.yml`:

```yaml
  webui:
    ports:
      - "127.0.0.1:${WEBUI_PORT:-9191}:8080"
```

## Exposing it to the internet

If these nameservers are going to be public, a few things change:

- **Port 53 must be reachable over both UDP and TCP.** TCP is not optional: it is
  how large answers and zone transfers work, and DNSSEC answers are large.
- **Publish the panel on a private address or behind a VPN** where you can. It is
  an administrative interface; there is no reason for it to face the internet.
- **Register the nameservers as glue records** at your registrar, and point the
  zone's `NS` set at the names you actually run.
- **Keep `PDNS_VERSION_STRING=anonymous`** (the default) so `version.bind` does
  not advertise your exact build.
- **Consider rate limiting.** PowerDNS Authoritative does not do response rate
  limiting itself; put it behind a firewall rule or a front-end that does if you
  are worried about being used for amplification.

## Where things live

```
.
├── compose.yml               the three services, wired together
├── .env / .env.example       every setting, documented
├── secrets/                  generated, gitignored, never in the images
├── db/
│   ├── schema/powerdns.sql   the PowerDNS 4.9 gpgsql schema, verbatim
│   └── initdb/               roles and schema, applied on the first start only
├── pdns/                     Dockerfile, entrypoint, pdns.conf template
├── webui/                    the Flask panel, its tests and Dockerfile
└── scripts/generate-secrets.sh
```

The PostgreSQL data lives in the named volume `pgdata`, not in the project
directory. `docker compose down` keeps it; `docker compose down -v` deletes it,
and with it every zone.

## Next

- [Advanced configuration](/advanced-config) — LDAP, OAuth, SAML, PowerDNS
  settings passthrough, running the panel outside compose.
- [Guide](/guide) — how zones, records, DNSSEC and the audit log behave.
- [FAQ](/faq) — the things that go wrong first.
