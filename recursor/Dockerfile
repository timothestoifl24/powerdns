# PowerDNS Recursor: the forwarding half of the stack.
#
# The Authoritative Server cannot forward -- it dropped the `recursor=` setting
# in 4.1 -- so forward zones and global forwarders need this process. It is the
# front door on :53 and answers for the authoritative zones by forwarding them
# to the pdns container; see the admin panel's Forwarding page.
#
# Same base as the authoritative image so both track one Debian release.
FROM debian:trixie-slim

RUN set -eux; \
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; \
    chmod +x /usr/sbin/policy-rc.d; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        pdns-recursor \
        ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    rm -f /usr/sbin/policy-rc.d; \
    # The package ships a recursor.conf of its own. Recursor 5.2 tries the
    # YAML form first and refuses old-style settings without
    # --enable-old-settings, so leaving it behind means a failed parse at every
    # start. Remove both forms; the entrypoint writes the only configuration.
    rm -f /etc/powerdns/recursor.conf /etc/powerdns/recursor.yml; \
    rm -rf /etc/powerdns/recursor.d; \
    install -d -m 0755 -o pdns -g pdns /var/run/pdns-recursor

COPY recursor.yml.template /usr/share/pdns-recursor/recursor.yml.template
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# API zones are written here and read back at start, so it must be a volume:
# without one, every forward zone disappears when the container is replaced.
VOLUME ["/var/lib/powerdns-recursor/api"]

# 53 for DNS, 8082 for the HTTP API the admin panel drives.
EXPOSE 53/tcp 53/udp 8082/tcp

# Starts as root to bind :53 and read the secrets, then drops to the
# unprivileged "pdns" user via the setuid/setgid settings in the config.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
