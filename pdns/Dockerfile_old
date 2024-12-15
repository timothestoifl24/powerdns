FROM ubuntu:20.04

RUN apt-get update && \
    apt-get install -y pdns-server pdns-backend-pgsql && \
    apt-get clean

COPY pdns.conf /etc/powerdns/pdns.conf
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
