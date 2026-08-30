# ---------------------------------------------------------------------------
# Stage 1: fetch the Tabler theme.
#
# Vendored at build time rather than loaded from a CDN: an internal DNS admin
# panel should not make requests to the public internet from the operator's
# browser, and it must keep working on an isolated network.
# ---------------------------------------------------------------------------
FROM debian:trixie-slim AS assets

ARG TABLER_VERSION=1.4.0
ARG TABLER_ICONS_VERSION=3.46.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /assets
RUN set -eux; \
    mkdir -p tabler/fonts; \
    curl -fsSL "https://registry.npmjs.org/@tabler/core/-/core-${TABLER_VERSION}.tgz" -o core.tgz; \
    tar -xzf core.tgz --strip-components=3 -C tabler \
        package/dist/css/tabler.min.css \
        package/dist/js/tabler.min.js \
        package/dist/js/tabler-theme.min.js; \
    curl -fsSL "https://registry.npmjs.org/@tabler/icons-webfont/-/icons-webfont-${TABLER_ICONS_VERSION}.tgz" -o icons.tgz; \
    tar -xzf icons.tgz --strip-components=2 -C tabler \
        package/dist/tabler-icons.min.css \
        package/dist/fonts/tabler-icons.woff2 \
        package/dist/fonts/tabler-icons.woff \
        package/dist/fonts/tabler-icons.ttf; \
    rm -f core.tgz icons.tgz; \
    printf '%s\n' "@tabler/core ${TABLER_VERSION}" "@tabler/icons-webfont ${TABLER_ICONS_VERSION}" > tabler/VERSIONS; \
    # Fail loudly here rather than shipping an image with a broken stylesheet.
    test -s tabler/tabler.min.css; \
    test -s tabler/tabler-icons.min.css; \
    test -s tabler/fonts/tabler-icons.woff2

# ---------------------------------------------------------------------------
# Stage 2: Python dependencies.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-trixie AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /wheels
COPY requirements.txt .
# psycopg, lxml and xmlsec all publish manylinux wheels, so no compiler and no
# libxmlsec1-dev are needed. --only-binary keeps it that way: if a wheel ever
# disappears the build fails here instead of silently pulling in a toolchain.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --only-binary=:all: -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 3: runtime.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-trixie

LABEL org.opencontainers.image.title="PowerDNS admin panel" \
      org.opencontainers.image.description="Tabler-themed web UI for PowerDNS with local, LDAP, OAuth and SAML sign-in" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# xmlsec's wheel bundles its crypto, but libxml2 runtime bits still come from
# the distro for lxml's C extension.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin webui

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root app/ ./app/
COPY --chown=root:root wsgi.py gunicorn.conf.py entrypoint.sh ./
COPY --from=assets --chown=root:root /assets/tabler ./app/static/vendor/tabler
RUN chmod 0755 /app/entrypoint.sh

# Runs unprivileged: the panel binds an unreserved port and needs no capabilities.
USER webui
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:application"]
