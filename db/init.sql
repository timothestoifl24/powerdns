CREATE DATABASE IF NOT EXISTS pdns;
USE pdns;
SOURCE /docker-entrypoint-initdb.d/schema.sql;
GRANT ALL PRIVILEGES ON pdns.* TO 'pdns'@'%' IDENTIFIED BY 'changeme';
FLUSH PRIVILEGES;
