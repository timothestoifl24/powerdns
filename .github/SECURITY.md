# Security policy

This repository ships an authoritative DNS server and an administrative web
panel that holds directory bind passwords, OAuth client secrets and SAML
signing material. Vulnerabilities here matter, and reports are welcome.

## Reporting a vulnerability

**Report privately, not in a public issue.**

Use GitHub's private vulnerability reporting:
<https://github.com/timothestoifl24/powerdns/security/advisories/new>. It opens
a draft advisory visible only to you and the maintainers, and it is the channel
that gets read first.

If that page is unavailable to you, open a public issue that says only that you
have a security report and asks for a private channel — no details, no
reproducer — and you will be invited to a draft advisory.

### What to include

A report is actionable when it contains:

* **Which component** — the panel (`webui/`), the PowerDNS container
  (`pdns/`), the database bootstrap (`db/`), the compose files,
  `scripts/generate-secrets.sh`, or the CI workflows.
* **The commit** you tested, or the image digest if you used a published one.
* **Steps to reproduce**, ideally starting from the quick start in the README so
  the setup is known. A `curl` sequence, a failing test, or a patch that
  demonstrates the flaw are all fine.
* **The impact you can actually demonstrate** — what an attacker holding what
  position ends up able to read, change or deny. An unauthenticated request that
  edits a zone is a very different report from one that needs an administrator
  session.
* **Any configuration** the issue depends on: which auth backends were enabled,
  whether `SESSION_COOKIE_SECURE` was set, whether the panel was behind a
  reverse proxy.

Please do not run automated scanners against `powerdns.stoifl.app` (the
documentation site) or any deployment you do not own. Test against your own
`docker compose up`.

### What to expect

| | |
| --- | --- |
| First response | within 5 days |
| Assessment and severity | within 10 days of the first response |
| Fix for a confirmed high-severity issue | as fast as it can be validated, typically days |
| Public disclosure | when the fix is on `main`, or 90 days after the report, whichever comes first |

This is a small project maintained in spare time, not a vendor with an on-call
rotation. If a deadline slips you will hear about it rather than be left in
silence, and a reporter who wants to disclose on their own timeline after 90
days will not be argued with.

Credit is given in the advisory unless you ask to stay anonymous.

## Supported versions

The project is released from `main`. Container images are published to
`ghcr.io/timothestoifl24/*` by CI on every push to `main`, and there are no
maintenance branches.

| Version | Supported |
| --- | --- |
| Current `main` and the images built from it | Yes |
| Any earlier commit or previously pulled image | No — fixes land on `main` only |

There are no backports. Staying current means rebuilding, or pulling the current
images and restarting the stack. If you pin a digest, plan for how you will move
off it when an advisory lands.

Note that upgrades do not re-run the database bootstrap: scripts in
`db/initdb/` execute only against an empty data directory. A fix that changes
role privileges or the schema requires the manual migration described in the
README, and the advisory will say so.

## Scope

**In scope**

* The Flask panel in `webui/` — authentication (local, LDAP/AD, OAuth 2.0/OIDC,
  SAML 2.0), session handling, CSRF, the role model and per-zone grants, the
  audit log, and the encryption of stored provider secrets.
* Anything that lets a `user` or `operator` account reach data or actions
  reserved for `admin`, or reach a zone they hold no grant on.
* Recovery of a stored secret — bind password, OAuth client secret — from the
  database, a dump, a replica, a log line, or a rendered page.
* The PowerDNS HTTP API key handling, and any path that lets the panel be used
  to make PowerDNS do something the caller was not authorised to do.
* The database role separation in `db/` — the panel's role reaching PowerDNS
  tables, either role holding rights it should not, or either being a superuser.
* `scripts/generate-secrets.sh`: weak generation, predictable values, secrets
  written world-readable or left somewhere they outlive their use.
* Container configuration in `pdns/`, `webui/` and the compose files that
  weakens isolation — unnecessary privilege, secrets exposed through the
  environment or the image, a service listening where it should not.
* The CI workflows in `.github/workflows/`, particularly anything that would let
  a pull request from a fork obtain the registry token or push an image.

**Out of scope**

* Vulnerabilities in upstream PowerDNS, PostgreSQL, Debian, Python or the
  packages in `webui/requirements.txt`. Report those to their maintainers —
  PowerDNS security contact details are at
  <https://doc.powerdns.com/authoritative/security.html>. If a released upstream
  fix has not yet been picked up here, a normal issue or pull request is the
  right route, not an advisory.
* The documentation site at `powerdns.stoifl.app` and the VitePress tree in
  `docs/`. It is static HTML with no backend and no user data.
* Results that depend on a deployment choice the project documents as unsafe:
  exposing port 9191 to the internet without TLS, running with
  `SESSION_COOKIE_SECURE=false` over HTTPS, reusing the printed first-run admin
  password, committing `secrets/` or `.env`, or granting `admin` to people who
  should hold `operator`.
* The quick start binding to `localhost` over plain HTTP. That is the documented
  local-evaluation path; production guidance is in the README.
* Scanner output with no demonstrated impact — a missing header on a page that
  carries nothing, a self-signed certificate in a local test stack, a
  theoretical dependency CVE in code path that is never reached.
* Denial of service through sheer volume against a service you have stood up
  yourself, and anything requiring physical access, a compromised host, or an
  already-compromised administrator account.

## Security model

Worth knowing before you decide whether something is a bug:

* **The panel is an administrative interface.** It is expected to sit behind
  TLS and to be reachable only by people who administer DNS. It is not designed
  to be safely exposed to anonymous users on the internet.
* **The panel never writes DNS data directly.** Every zone and record change
  goes through the PowerDNS HTTP API, so a flaw in the panel cannot bypass
  PowerDNS's own validation, serial handling or DNSSEC signing.
* **The two halves of the database are separated by schema and by role.** The
  panel's PostgreSQL role has no rights on the PowerDNS tables, and neither
  application role is a superuser. CI asserts both on every run.
* **Stored provider secrets are encrypted** with a key derived from `SECRET_KEY`
  via HKDF, and are write-only from the browser — never rendered back into a
  form.
* **The PowerDNS API key is a shared secret** between the two containers. Anyone
  who can reach the API port with it holds full control of the zone data; the
  compose file does not publish that port.
* **Configuration in `.env` outranks the database.** A provider declared in the
  environment cannot be overridden from the UI, so an administrator account
  cannot quietly repoint authentication at an identity provider they control.

A report that shows one of these properties does not hold is a good report.

## Dependencies and supply chain

Dependabot watches the Python requirements, the base images, the compose
images, the GitHub Actions and the docs toolchain (`.github/dependabot.yml`).
Version updates sit behind a cooldown so that other people's regressions surface
first; **security updates are never delayed by cooldown**. Every update runs the
full CI suite — unit tests, ruff, shellcheck, and a compose smoke test that
builds all three images, resolves a record over real DNS and exercises the login
and CSRF paths — so a bump that breaks the stack fails its pull request instead
of reaching a deployment.

Base image majors (`postgres`, `debian`, `python`) are pinned deliberately,
because moving them requires a planned `pg_upgrade` or changes the PowerDNS
version. That means OS-level fixes arrive through minor and patch bumps; if a
fix exists only in a newer major, say so in your report.

## Hardening a deployment

Not a vulnerability report, but the checklist that prevents most of them:

1. Change the first-run administrator password immediately, and keep
   `secrets/` and `.env` out of version control.
2. Terminate TLS in front of the panel, set `BASE_URL` to the external HTTPS
   URL, and set `SESSION_COOKIE_SECURE=true`.
3. Do not publish PowerDNS's API port (8081) outside the compose network.
4. Set `LOCAL_AUTH_ENABLED=false` once single sign-on works, and set
   `LDAP_DEFAULT_ROLE=none` so an unmapped directory account gets nothing.
5. Grant `admin` sparingly; `operator` plus per-zone grants covers most people.
6. Keep the images current, and check the audit log.

## Safe harbour

Good-faith research that stays within the scope above, against a deployment you
control, and that follows this policy will not be met with a legal complaint or
a takedown request. Do not access other people's data, do not degrade a service
someone else depends on, and do not disclose details before the coordinated date
you and the maintainers agree on.
