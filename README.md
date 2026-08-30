# PowerDNS with PostgreSQL and a Tabler admin panel

An authoritative DNS server you can run with one command, backed by PostgreSQL,
with a web admin panel themed with [Tabler](https://tabler.io) that supports
local accounts, LDAP / Active Directory, OAuth 2.0 / OpenID Connect and SAML 2.0.

```
┌──────────────┐        HTTP API         ┌──────────────┐
│    webui     │ ──────────────────────► │     pdns     │  :53 tcp/udp
│  Flask +     │                         │  PowerDNS    │
│  Tabler      │                         │  Authoritative│
└──────┬───────┘                         └──────┬───────┘
       │  schema: pdnsadmin                     │  schema: public
       │  (users, grants, audit log)            │  (domains, records, DNSSEC)
       └────────────────┬───────────────────────┘
                        ▼
                 ┌──────────────┐
                 │      db      │  PostgreSQL 17
                 └──────────────┘
```

The panel never writes DNS data itself. Every zone and record change goes
through the PowerDNS HTTP API, so serial bumping, DNSSEC signing and record
validation stay where they belong. The two halves of the database are separated
by schema *and* by role: the panel's PostgreSQL user has no rights on the
PowerDNS tables.

## Quick start

```bash
git clone https://github.com/timothestoifl24/powerdns.git
cd powerdns

./scripts/generate-secrets.sh     # writes secrets/ and .env, prints the admin password
docker compose up -d --build
```

Then open <http://localhost:9191> and sign in as `admin` with the password the
script printed. Change it under **My profile** straight away.

Check that DNS is actually answering:

```bash
dig @127.0.0.1 -p 53 example.com SOA
```

### What the script created

| Path | Contents |
| --- | --- |
| `secrets/pdns_db_password` | PostgreSQL password for the PowerDNS role |
| `secrets/webui_db_password` | PostgreSQL password for the panel's role |
| `secrets/pdns_api_key` | Shared secret for the PowerDNS HTTP API |
| `secrets/webui_secret_key` | Flask session signing key |
| `secrets/webui_admin_password` | Password for the first-run administrator |
| `.env` | Non-secret settings, copied from `.env.example` |

`secrets/` and `.env` are gitignored. Every secret can also be supplied as a
plain environment variable instead of a file — each one accepts both `FOO` and
`FOO_FILE`, and the file wins when both are set.

## Layout

```
.
├── compose.yml               the three services, wired together
├── .env.example              every setting, documented
├── db/
│   ├── Dockerfile            postgres:17-alpine
│   └── initdb/
│       ├── 01-powerdns-schema.sql   PowerDNS 4.9 gpgsql schema
│       └── 02-admin-schema.sh       the panel's role and schema
├── pdns/
│   ├── Dockerfile            Debian trixie + pdns-server + pdns-backend-pgsql
│   ├── entrypoint.sh         renders pdns.conf, waits for the database
│   └── pdns.conf.template
├── webui/
│   ├── Dockerfile            Tabler vendored at build time, then the Flask app
│   ├── app/                  the panel
│   └── tests/                192 tests, incl. an in-memory PowerDNS
├── scripts/generate-secrets.sh
└── docs/database-choice.md   why PostgreSQL and not MongoDB
```

## Roles

| Role | Can do |
| --- | --- |
| **admin** | Everything, including user administration and settings |
| **operator** | Create, edit and delete every zone; no user administration |
| **user** | Read and edit only the zones explicitly granted to them |

Grants for the `user` role are managed per account under
**Administration → Users → *(a user)* → Zone access**.

## Authentication

Backends can be combined. With local and LDAP both on, the sign-in form tries
the local account first and falls back to the directory.

Everything is configured in `.env`; `.env.example` documents every key with a
worked example. The **Administration → Settings** page shows what is actually
active, including the group-to-role mapping and the exact redirect URL to
register with each provider.

### Local accounts

On by default. Passwords are hashed with scrypt. Set `LOCAL_AUTH_ENABLED=false`
once single sign-on works, to force everyone through the identity provider.

### LDAP / Active Directory

```bash
LDAP_ENABLED=true
LDAP_URI=ldaps://dc1.example.com:636
LDAP_BIND_DN=CN=svc-dns,OU=Service Accounts,DC=example,DC=com
LDAP_BIND_PASSWORD=…
LDAP_BASE_DN=DC=example,DC=com
LDAP_USERNAME_ATTRIBUTE=sAMAccountName   # OpenLDAP: uid
LDAP_ADMIN_GROUP=DNS-Admins
LDAP_DEFAULT_ROLE=none                   # refuse anyone in no mapped group
```

The panel binds as the service account to find the user, then binds as that
user to check the password — it never reads a password hash. Group names may be
given bare (`DNS-Admins`) or as a full DN; `memberOf` values are matched either
way, case-insensitively.

### OAuth 2.0 / OpenID Connect

Name your providers, then configure each one. The name becomes the environment
prefix and the URL segment.

```bash
OAUTH_PROVIDERS=keycloak,github

# OpenID Connect: one discovery URL is enough
OAUTH_KEYCLOAK_DISPLAY_NAME=Company SSO
OAUTH_KEYCLOAK_CLIENT_ID=powerdns-admin
OAUTH_KEYCLOAK_CLIENT_SECRET=…
OAUTH_KEYCLOAK_DISCOVERY_URL=https://sso.example.com/realms/main/.well-known/openid-configuration
OAUTH_KEYCLOAK_GROUPS_CLAIM=groups
OAUTH_KEYCLOAK_ADMIN_GROUP=dns-admins

# Plain OAuth 2.0, for providers without discovery
OAUTH_GITHUB_CLIENT_ID=…
OAUTH_GITHUB_CLIENT_SECRET=…
OAUTH_GITHUB_AUTHORIZE_URL=https://github.com/login/oauth/authorize
OAUTH_GITHUB_TOKEN_URL=https://github.com/login/oauth/access_token
OAUTH_GITHUB_USERINFO_URL=https://api.github.com/user
OAUTH_GITHUB_USERNAME_CLAIM=login
OAUTH_GITHUB_ICON=ti-brand-github
```

Register the redirect URI `${BASE_URL}/auth/oauth/<name>/callback` with the
provider. **`BASE_URL` must be set** for this to work behind a reverse proxy —
otherwise the panel hands the provider the internal URL it sees, and the
provider's exact-match check fails.

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
`${BASE_URL}/auth/saml/metadata`. The assertion consumer service is
`${BASE_URL}/auth/saml/acs`. Signature validation is on by default; if the IdP
requires signed requests, supply `SAML_SP_X509_CERT` and `SAML_SP_PRIVATE_KEY`.

### How external accounts are provisioned

The first time someone signs in through LDAP, OAuth or SAML, an account is
created for them. Their name, e-mail and role are refreshed from the provider
on **every** sign-in, so a group change takes effect immediately.

Two rules are worth knowing:

- Returning users are matched on the provider's stable subject identifier, not
  on their username, so a rename at the identity provider does not create a
  second account or lose their zone grants.
- Single sign-on will **not** adopt an existing local account with the same
  name. Otherwise anyone who could create a matching username at the identity
  provider could take over the local administrator.

Set `*_DEFAULT_ROLE=none` to refuse anyone who is not in a mapped group. If no
group mapping is configured at all, everyone who authenticates gets
`*_DEFAULT_ROLE` (which defaults to `user`).

## Running behind a reverse proxy

```bash
BASE_URL=https://dns.example.com
SESSION_COOKIE_SECURE=true     # required over HTTPS
TRUSTED_PROXY_COUNT=1          # number of proxies in front of the panel
```

`TRUSTED_PROXY_COUNT` defaults to `0`, meaning `X-Forwarded-For` is ignored —
any client can set that header, and trusting it blindly would let them forge
the address recorded in the audit log. Set it to the real number of proxies.

Leave `SESSION_COOKIE_SECURE=false` only for plain HTTP: a secure cookie is
never sent over HTTP, so login appears to succeed and then immediately fails.

## Configuring PowerDNS

Common settings have their own variables (see `.env.example`). Anything else
can be set with a `PDNS_SETTING_` prefix, where underscores become dashes:

```yaml
environment:
  PDNS_SETTING_allow_axfr_ir: 10.0.0.0/8       # -> allow-axfr-ir=10.0.0.0/8
  PDNS_SETTING_max_tcp_connections: "100"      # -> max-tcp-connections=100
```

These are written to `/etc/powerdns/pdns.d/50-env-overrides.conf`, which
PowerDNS reads after the main config, so they win.

### Using a different PowerDNS build

`pdns/Dockerfile` installs `pdns-server` and `pdns-backend-pgsql` from Debian
trixie, which carries PowerDNS Authoritative 4.9.x. To track upstream releases
more closely, add `repo.powerdns.com` in that Dockerfile instead. The schema in
`db/initdb/01-powerdns-schema.sql` matches 4.9 — check the upstream schema
before moving to a different major version.

### Upgrading the schema

Scripts in `db/initdb/` run **only** when the data directory is empty, i.e. on
the first start of a fresh volume. Changing them does nothing to an existing
database; apply PowerDNS schema migrations by hand from the upstream release
notes. The panel's own tables are created by the application at start-up, so
those keep themselves up to date.

## Development

```bash
cd webui
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-cov ruff

pytest                       # 192 tests, no database or PowerDNS needed
ruff check app tests
ruff format app tests
```

The suite runs against in-memory SQLite and a fake PowerDNS that speaks the
real HTTP API, so the client's URL building and error handling are exercised
too. CI additionally brings the whole stack up with `docker compose`, creates a
zone through the API and resolves a record with `dig` — the end-to-end path
this repository exists to provide.

To run the panel directly against a compose stack:

```bash
export DATABASE_URL="postgresql+psycopg://pdnsadmin:$(cat ../secrets/webui_db_password)@localhost:5432/pdns"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export PDNS_API_URL=http://localhost:8081 PDNS_API_KEY="$(cat ../secrets/pdns_api_key)"
export SESSION_COOKIE_SECURE=false
python -m app.cli init && flask --app wsgi:application run --debug
```

(The compose file does not publish 5432 or 8081 to the host; add the ports
temporarily if you want to develop this way.)

## Security notes

- The PowerDNS API port is **not** published to the host. Only the `webui`
  container reaches it, over the compose network, and
  `webserver-allow-from` restricts it further.
- The panel runs as an unprivileged user. PowerDNS starts as root only to bind
  port 53 and read its secrets, then drops to the `pdns` user.
- Every state-changing request needs a CSRF token. The SAML assertion consumer
  is the one exemption — it is a cross-site POST by design, authenticated by
  the assertion's own XML signature.
- Failed logins are rate limited per username and address. The counter is
  per-process, so with several gunicorn workers the effective allowance is
  roughly `LOGIN_MAX_ATTEMPTS × workers`.
- Tabler is vendored into the image at build time, so the Content-Security
  -Policy needs no external origins and the panel works on an isolated network.
- Every change made through the panel is written to an append-only audit log,
  visible under **Administration → Audit log**. Entries survive deletion of
  the user who made them.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
