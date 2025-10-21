#!/bin/bash
set -e

# Set PowerDNS configuration
sed -i "s/gmysql-host=.*/gmysql-host=$PDNS_gmysql_host/" /etc/powerdns/pdns.conf
sed -i "s/gmysql-user=.*/gmysql-user=$PDNS_gmysql_user/" /etc/powerdns/pdns.conf
sed -i "s/gmysql-dbname=.*/gmysql-dbname=$PDNS_gmysql_dbname/" /etc/powerdns/pdns.conf
sed -i "s/gmysql-password=.*/gmysql-password=$(cat /run/secrets/mysql_password)/" /etc/powerdns/pdns.conf

# Start PowerDNS
exec /usr/sbin/pdns_server --daemon=no --guardian=no --loglevel=9
