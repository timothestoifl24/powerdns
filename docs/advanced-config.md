---
title: Advanced configuration
description: Every environment variable, all four authentication backends, PowerDNS settings passthrough, and running the panel outside compose.
---

# Advanced configuration

Everything is configured through the environment. `.env` holds the non-secret
settings; anything that is a password or a key lives in a file under `secrets/`
and is mounted into the container.

::: tip Every secret takes a file too
Each secret accepts both `FOO` and `FOO_FILE`, and the file wins when both are
set. That is what makes these images work unchanged with Docker Swarm secrets and
Kubernetes secret mounts — no value ever has to appear in `docker inspect`.
:::

## Core

| Variable | Default | What it does |
| --- | --- | --- |
| `SITE_NAME` | `PowerDNS Admin` | Shown in the header and page titles. |
| `WEBUI_PORT` | `9191` | Host port for the panel. |
| `DNS_PORT` | `53` | Host port for DNS. The container always serves on 53 internally. |
| `DNS_BIND_ADDRESS` | `0.0.0.0` | Host address DNS binds to. `127.0.0.1` keeps it local. |
| `BASE_URL` | *(empty)* | Public URL, no trailing slash. Required for OAuth and SAML. |
| `SESSION_COOKIE_SECURE` | `false` | Set to `true` behind HTTPS. Breaks login over plain HTTP. |
| `SESSION_LIFETIME_MINUTES` | `480` | How long a session survives. |
| `TRUSTED_PROXY_COUNT` | `0` | Number of reverse proxies in front of the panel. |
| `LOG_LEVEL` | `INFO` | Panel log level. |

## Database

| Variable | Default | What it does |
| --- | --- | --- |
| `DB_NAME` | `pdns` | Database holding both schemas. |
| `DB_SUPERUSER` | `postgres` | Bootstrap role: runs `initdb` and the scripts in `db/initdb/`, and is never connected as afterwards. |
| `PDNS_DB_USER` | `pdns` | Role PowerDNS uses for the `public` schema. `NOSUPERUSER NOCREATEDB NOCREATEROLE`. |
| `WEBUI_DB_USER` | `pdnsadmin` | Role the panel uses, with the same restrictions. It cannot read the PowerDNS tables, and `pdns` cannot read its. |
| `WEBUI_DB_SCHEMA` | `pdnsadmin` | Schema the panel's tables live in. |
| `DATABASE_URL` | *(derived)* | A full SQLAlchemy URL, overriding the discrete parts above. Useful when pointing the panel at a database it does not share with PowerDNS's container. |

## Zone defaults

| Variable | Default | What it does |
| --- | --- | --- |
| `DEFAULT_NAMESERVERS` | *(empty)* | Comma-separated, pre-filled on the new-zone form. |
| `DEFAULT_TTL` | `3600` | Pre-filled TTL in the record editor. |
| `DEFAULT_SOA_EDIT_API` | `DEFAULT` | The `SOA-EDIT-API` policy applied to zones the panel creates — how PowerDNS bumps the serial. `INCEPTION-INCREMENT` gives date-based serials. |
| `PDNS_DEFAULT_SOA_CONTENT` | `ns1.example.com hostmaster.example.com 0 10800 3600 604800 3600` | The SOA PowerDNS uses for new zones that do not supply one. |

## Login and the first administrator

| Variable | Default | What it does |
| --- | --- | --- |
| `LOCAL_AUTH_ENABLED` | `true` | Set to `false` once SSO works, to force everyone through the provider. |
| `LOGIN_MAX_ATTEMPTS` | `10` | Failed logins allowed before a lockout, per username and address. |
| `LOGIN_LOCKOUT_SECONDS` | `300` | How long that lockout lasts. |
| `BOOTSTRAP_ADMIN_USERNAME` | `admin` | First-run administrator. Ignored once any user exists. |
| `BOOTSTRAP_ADMIN_PASSWORD` | *(from `secrets/`)* | Their initial password. |
| `BOOTSTRAP_ADMIN_EMAIL` | *(empty)* | Optional. |

The lockout counter lives in each gunicorn worker's memory, so with four workers
the effective allowance is roughly four times `LOGIN_MAX_ATTEMPTS`. It is a speed
bump against online guessing, not a distributed rate limiter.

Local passwords must be at least 12 characters, must not equal the username, and
must not contain any of the obvious strings (`password`, `changeme`, `qwerty`,
and friends).

## Authentication

Backends can be combined. With local and LDAP both on, the sign-in form tries the
local account first and falls back to the directory.

There are two places to configure an external provider, and both work at once:

- **Administration → Sign-in providers**, in the panel. Add, edit, test and
  disable providers at runtime, with no restart. This is the quicker route, and
  the only one with a Test button.
- **`.env`**, documented below. This is what a configuration-as-code deployment
  wants, and it is the only way to configure a provider before the panel has an
  administrator to sign in as.

**The environment always wins.** A provider declared in `.env` appears in the UI
read-only, and a database entry that collides with it is ignored and labelled
*Shadowed* rather than silently applied. The settings below therefore describe
both routes: each UI field is the same setting under a different name.

The rest of this section is the `.env` form; see
[the guide](/guide#sign-in-providers) for how the UI behaves, including secret
encryption and what a `SECRET_KEY` rotation does to stored secrets.

**Administration → Settings** shows which backends the environment enables, what
the group mapping resolves to, and the exact redirect URL to register with each
provider. When something does not work, check there first — it reports the
running configuration rather than what you meant to write.

### Group-to-role mapping

Every external backend maps provider groups onto the panel's three roles, using
the same four variables with a different prefix:

```bash
LDAP_ADMIN_GROUP=DNS-Admins
LDAP_OPERATOR_GROUP=DNS-Operators
LDAP_USER_GROUP=DNS-Users
LDAP_DEFAULT_ROLE=none
```

The rules, which are worth reading once:

- The **highest** matching role wins: someone in both `DNS-Admins` and
  `DNS-Users` is an admin.
- Group names may be given bare (`DNS-Admins`) or as a full DN
  (`CN=DNS-Admins,OU=Groups,DC=example,DC=com`). LDAP hands back `memberOf` as
  DNs, and both forms are matched, case-insensitively.
- `*_DEFAULT_ROLE=none` **refuses** anyone who matched no group. That is how you
  restrict the panel to one directory group.
- If no group mapping is configured at all, everyone who authenticates gets
  `*_DEFAULT_ROLE`, which itself defaults to `user`.
- Setting an external user's role by hand **pins** it, and the mapping stops
  overwriting it on later sign-ins. Admission is still the mapping's decision:
  see [pinning a role by hand](/guide#pinning-a-role-by-hand).

### LDAP / Active Directory

```bash
LDAP_ENABLED=true
LDAP_URI=ldaps://dc1.example.com:636
LDAP_BIND_DN=CN=svc-dns,OU=Service Accounts,DC=example,DC=com
LDAP_BIND_PASSWORD=…
LDAP_BASE_DN=DC=example,DC=com
LDAP_USERNAME_ATTRIBUTE=sAMAccountName   # OpenLDAP: uid
LDAP_ADMIN_GROUP=DNS-Admins
LDAP_DEFAULT_ROLE=none
```

The panel binds as the service account to find the user, then binds **as that
user** to check the password — it never reads a password hash, and the service
account needs nothing more than read access to the directory.

Other settings: `LDAP_START_TLS`, `LDAP_TLS_VERIFY`, `LDAP_CA_CERT_FILE`,
`LDAP_EMAIL_ATTRIBUTE`, `LDAP_DISPLAY_NAME_ATTRIBUTE`, `LDAP_GROUP_ATTRIBUTE`,
`LDAP_GROUP_SEARCH_BASE`, `LDAP_GROUP_FILTER`, `LDAP_CONNECT_TIMEOUT`, and
`LDAP_USER_FILTER` — in which `{username}` and `{username_attribute}` are
substituted before the search.

::: warning Use `ldaps://` or StartTLS
The panel sends the user's password to the directory to verify it. Over plain
`ldap://` that crosses the network in the clear.
:::

### Finding a user's groups

The role mapping can only work with the groups the directory actually hands
back, and directories disagree about how to publish them. Each user's page under
**Administration → Users** lists what their provider reported at their last
sign-in — read that first, because an empty list and a wrong list have different
fixes.

The panel tries three things, in order:

1. **`LDAP_GROUP_ATTRIBUTE` on the user entry**, matched without regard to case,
   so a directory that returns `memberof` is read the same as one returning
   `memberOf`.
2. **Attributes other directories publish instead**, if that one came back
   empty: `memberOf`, `isMemberOf`, `nsRole` and `groupMembership`. This covers
   389/Red Hat DS, eDirectory and OpenLDAP with the `memberof` overlay without
   any configuration.
3. **A search of the group objects**, if `LDAP_GROUP_SEARCH_BASE` is set. Some
   directories record membership only on the group, never on the user, and no
   attribute on the user entry will ever show it. The default filter covers the
   three usual schemas:

   ```
   (|(member={dn})(uniqueMember={dn})(memberUid={username}))
   ```

   Override it with `LDAP_GROUP_FILTER`; `{dn}` and `{username}` are substituted
   before the search.

Both the group's own name and its full DN are recorded, and the mapping matches
either, ignoring case — so a configured `DNS-Admins` matches
`CN=DNS-Admins,OU=Groups,DC=example,DC=com`. A sign-in that finds no groups at
all is logged as a warning naming the attributes that were tried.

#### Nested Active Directory groups

`memberOf` reports only *direct* membership, so someone in a group that is
itself a member of `DNS-Admins` looks like a member of nothing. Active Directory
can resolve the chain server-side if you ask it to — set a group search base and
this filter:

```bash
LDAP_GROUP_SEARCH_BASE=OU=Groups,DC=example,DC=com
LDAP_GROUP_FILTER=(member:1.2.840.113556.1.4.1941:={dn})
```

That is AD's `LDAP_MATCHING_RULE_IN_CHAIN`. It is an Active Directory extension;
other directories will reject it.

### OAuth 2.0 / OpenID Connect

Name your providers, then configure each one. The name becomes both the
environment prefix and the URL segment.

```bash
OAUTH_PROVIDERS=keycloak,github

# OpenID Connect: one discovery URL is enough.
OAUTH_KEYCLOAK_DISPLAY_NAME=Company SSO
OAUTH_KEYCLOAK_CLIENT_ID=powerdns-admin
OAUTH_KEYCLOAK_CLIENT_SECRET=…
OAUTH_KEYCLOAK_DISCOVERY_URL=https://sso.example.com/realms/main/.well-known/openid-configuration
OAUTH_KEYCLOAK_GROUPS_CLAIM=groups
OAUTH_KEYCLOAK_ADMIN_GROUP=dns-admins

# Plain OAuth 2.0, for providers without a discovery document.
OAUTH_GITHUB_CLIENT_ID=…
OAUTH_GITHUB_CLIENT_SECRET=…
OAUTH_GITHUB_AUTHORIZE_URL=https://github.com/login/oauth/authorize
OAUTH_GITHUB_TOKEN_URL=https://github.com/login/oauth/access_token
OAUTH_GITHUB_USERINFO_URL=https://api.github.com/user
OAUTH_GITHUB_USERNAME_CLAIM=login
OAUTH_GITHUB_ICON=ti-brand-github
```

Register this redirect URI with the provider:

```
${BASE_URL}/auth/oauth/<name>/callback
```

**`BASE_URL` must be set.** Without it the panel derives the callback from the
request it sees, which behind a proxy is the internal hostname, and providers
match the registered URI exactly.

Claim names default sensibly per provider style: `preferred_username` for OIDC,
`login` for plain OAuth 2.0. Override with `_USERNAME_CLAIM`, `_EMAIL_CLAIM`,
`_NAME_CLAIM` and `_GROUPS_CLAIM`. `_ICON` takes any
[Tabler icon](https://tabler.io/icons) name for the sign-in button.

Known-good discovery URLs:

| Provider | `_DISCOVERY_URL` |
| --- | --- |
| Keycloak | `https://host/realms/<realm>/.well-known/openid-configuration` |
| Authentik | `https://host/application/o/<slug>/.well-known/openid-configuration` |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration` |
| Google | `https://accounts.google.com/.well-known/openid-configuration` |
| Okta | `https://<org>.okta.com/.well-known/openid-configuration` |

### SAML 2.0

```bash
SAML_ENABLED=true
BASE_URL=https://dns.example.com
SAML_IDP_METADATA_URL=https://sso.example.com/realms/main/protocol/saml/descriptor
SAML_ATTR_GROUPS=groups
SAML_ADMIN_GROUP=dns-admins
SAML_DEFAULT_ROLE=none
```

Give the identity provider the SP metadata served at
`${BASE_URL}/auth/saml/metadata`; the assertion consumer service is
`${BASE_URL}/auth/saml/acs`.

Signature validation is on by default (`SAML_STRICT`,
`SAML_WANT_ASSERTIONS_SIGNED`). If the IdP requires *signed requests*, supply an
SP keypair with `SAML_SP_X509_CERT` and `SAML_SP_PRIVATE_KEY`. Without a metadata
URL, configure the IdP by hand with `SAML_IDP_ENTITY_ID`, `SAML_IDP_SSO_URL` and
`SAML_IDP_X509_CERT` — the certificate is then mandatory, because it is what
validates the assertion.

Attribute names are configurable: `SAML_ATTR_USERNAME`, `SAML_ATTR_EMAIL`,
`SAML_ATTR_NAME`, `SAML_ATTR_GROUPS`.

## Configuring PowerDNS itself

Common settings have their own variables: `PDNS_DEFAULT_SOA_CONTENT`,
`PDNS_VERSION_STRING`, `PDNS_LOGLEVEL`. Anything else PowerDNS understands can be
set with a `PDNS_SETTING_` prefix, where underscores become dashes:

```yaml
services:
  pdns:
    environment:
      PDNS_SETTING_allow_axfr_ir: 10.0.0.0/8       # -> allow-axfr-ir=10.0.0.0/8
      PDNS_SETTING_max_tcp_connections: "100"      # -> max-tcp-connections=100
      PDNS_SETTING_default_ttl: "1800"             # -> default-ttl=1800
```

These are written to `/etc/powerdns/pdns.d/50-env-overrides.conf`, which PowerDNS
reads after the main configuration file, so they win. The full list of settings
is in the [PowerDNS documentation](https://doc.powerdns.com/authoritative/settings.html).

`PDNS_WEBSERVER_ALLOW_FROM` deserves a mention: it defaults to loopback plus the
private ranges, which is what keeps the API reachable from the `webui` container
and nowhere else. Narrow it if your compose network is on a known subnet.

### Using a different PowerDNS build

`pdns/Dockerfile` installs `pdns-server` and `pdns-backend-pgsql` from Debian
trixie, which carries PowerDNS Authoritative 4.9.x. To track upstream releases
more closely, add `repo.powerdns.com` in that Dockerfile instead. The schema in
`db/schema/powerdns.sql` matches 4.9 — check the upstream schema before
moving to a different major version.

## Configuring the recursor

Common settings have their own variables. Anything else the recursor
understands goes in `RECURSOR_EXTRA_YAML`, which is written verbatim into the
recursor's include directory:

```yaml
services:
  recursor:
    environment:
      RECURSOR_ALLOW_FROM: 10.0.0.0/8,192.168.0.0/16
      # Validation is off by default on purpose; see the guide.
      RECURSOR_DNSSEC: process
      RECURSOR_NEGATIVE_TRUSTANCHORS: corp.internal,10.in-addr.arpa
      RECURSOR_THREADS: "4"
      RECURSOR_EXTRA_YAML: |
        recordcache:
          max_entries: 2000000
        recursor:
          serve_rfc1918: false
```

It is real recursor YAML rather than a syntax of ours, so the
[settings reference](https://doc.powerdns.com/recursor/yamlsettings.html)
applies directly. Note the sections: settings are grouped under `incoming`,
`recursor`, `webservice`, `dnssec`, `logging` and so on.

### Why YAML and not the classic settings file

PowerDNS Recursor 5.2 stopped reading the old `key=value` syntax unless
`--enable-old-settings` is passed, and that option is documented as going away
in a future release. Debian trixie ships 5.2, so this container writes YAML.
Old-style names map onto YAML as `section.name` with dashes becoming
underscores: `allow-from` is `incoming.allow_from`, `api-config-dir` is
`webservice.api_dir`, `dnssec` is `dnssec.validation`.

| Variable | Default | Notes |
| --- | --- | --- |
| `RECURSOR_ALLOW_FROM` | private networks + loopback | Who may use the resolver. **Never `0.0.0.0/0`** — see below. |
| `RECURSOR_DNSSEC` | `process-no-validate` | Validation is off on purpose; see [DNSSEC and forwarding](/guide#dnssec-and-forwarding). |
| `RECURSOR_NEGATIVE_TRUSTANCHORS` | — | Zones not to validate, needed if you set `process`. |
| `RECURSOR_THREADS` | `2` | |
| `RECURSOR_LOGLEVEL` | `4` | |
| `RECURSOR_VERSION_STRING` | `anonymous` | What a `version.bind` query returns. |
| `AUTH_DNS_BIND_ADDRESS` / `AUTH_DNS_PORT` | `127.0.0.1` / `5300` | Publishes the authoritative server directly, for debugging. |
| `BACKEND_SUBNET` | `172.29.0.0/24` | The compose network. |
| `PDNS_STATIC_IP` | `172.29.0.10` | The authoritative server's fixed address; must be inside `BACKEND_SUBNET`. |
| `RECURSOR_EXTRA_YAML` | — | Extra recursor YAML, merged after the generated config. |

### Why the authoritative server has a fixed address

Forward targets in PowerDNS are IP addresses, never names: the recursor parses
them at configuration time, before it has any way to resolve a name. So the
recursor cannot be pointed at `pdns`, and the authoritative server gets a fixed
address on the compose network instead — otherwise every local-zone forward rule
would break the moment that container was recreated with a new address.

Change `BACKEND_SUBNET` and `PDNS_STATIC_IP` together if the default subnet
collides with a network your host already uses.

### Do not make it an open resolver

`RECURSOR_ALLOW_FROM` defaults to loopback and the private ranges. A recursor
that answers the whole internet is found by scanners within days and used to
amplify denial-of-service attacks against third parties, with your bandwidth.
Widen it only to networks you control. The CI smoke test fails the build if the
running configuration allows `0.0.0.0/0`.

### Where forward zones are stored

The recursor writes them into an `apizones` file in `webservice.api_dir` and
reads it back at start, which is what makes them survive a restart. That
directory is the `recursor-api` volume, and it is deliberately *not* the same
as `recursor.include_dir` — under YAML settings the recursor requires the two
to differ. The panel keeps no copy of its own, so what the Forwarding page
shows is always what the resolver is actually doing.

## Running the panel outside compose

The panel is an ordinary WSGI application; nothing ties it to this compose file.
It needs three things: a PostgreSQL URL, the PowerDNS API endpoint and key, and a
secret key. Add `RECURSOR_API_URL`, `RECURSOR_API_KEY` and `PDNS_DNS_ADDRESS` to
enable the Forwarding page; leave them unset and it explains that forwarding is
unavailable instead of failing.

```bash
cd webui
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL="postgresql+psycopg://pdnsadmin:$(cat ../secrets/webui_db_password)@localhost:5432/pdns"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export PDNS_API_URL=http://localhost:8081
export PDNS_API_KEY="$(cat ../secrets/pdns_api_key)"
export SESSION_COOKIE_SECURE=false

python -m app.cli init
flask --app wsgi:application run --debug
```

`python -m app.cli init` creates the panel's tables and the bootstrap
administrator; it is what the container entrypoint runs before gunicorn.

The compose file does not publish 5432 or 8081 to the host, so add those ports
temporarily if you want to develop this way.

## Tests and linting

```bash
cd webui
pip install -r requirements.txt pytest pytest-cov ruff

pytest                       # no database and no PowerDNS needed
ruff check app tests
ruff format app tests
```

The suite runs against in-memory SQLite and a fake PowerDNS that speaks the real
HTTP API, so the client's URL building and error handling are exercised too. CI
additionally brings the whole stack up with compose, creates a zone through the
API and resolves a record with `dig` — the end-to-end path this repository exists
to provide.

## See also

- [Setup](/setup) — first install, reverse proxy, port conflicts.
- [Upgrading](/upgrading) — what happens to the schema when you pull new images.
