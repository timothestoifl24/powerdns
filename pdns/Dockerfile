# PowerDNS Authoritative Server with the PostgreSQL (gpgsql) backend.
#
# Debian trixie carries PowerDNS Authoritative 4.9.x, so no third-party apt
# repository and no signing key to manage. To track upstream releases more
# closely, swap this for repo.powerdns.com -- see README, "Using a different
# PowerDNS build".
FROM debian:trixie-slim

# Debian's postinst wants to start the service; there is no init in a build.
RUN set -eux; \
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; \
    chmod +x /usr/sbin/policy-rc.d; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        pdns-server \
        pdns-backend-pgsql \
        ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    rm -f /usr/sbin/policy-rc.d; \
    # The packages drop sample backend configs in here; include-dir would read
    # them alongside ours and launch backends we never configured.
    rm -f /etc/powerdns/pdns.d/*; \
    rm -f /etc/powerdns/pdns.conf; \
    install -d -m 0755 /var/run/pdns

COPY pdns.conf.template /usr/share/powerdns/pdns.conf.template
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# 53 for DNS, 8081 for the HTTP API the admin panel talks to.
EXPOSE 53/tcp 53/udp 8081/tcp

# Starts as root to bind :53 and read the secrets, then drops to the unprivileged
# "pdns" user via the setuid/setgid settings in the generated config.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
