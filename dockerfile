FROM ubuntu:latest

RUN apt-get update && apt-get install -y \
    mariadb-client \
    powerdns \
    powerdns-backend-mysql \
    && rm -rf /var/lib/apt/lists/*

COPY pdns.conf /etc/powerdns/pdns.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
