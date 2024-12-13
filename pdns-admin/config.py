import os

SQLA_DB_USER = os.getenv('PDNS_PGSQL_USER')
SQLA_DB_PASSWORD = open('/run/secrets/postgres_password').read().strip()
SQLA_DB_HOST = os.getenv('PDNS_PGSQL_HOST')
SQLA_DB_NAME = os.getenv('PDNS_PGSQL_DBNAME')
SQLALCHEMY_TRACK_MODIFICATIONS = False

BIND_ADDRESS = '0.0.0.0'
PORT = int(os.getenv('PDNS_ADMIN_PORT', 80))
HSTS_ENABLED = False

PDNS_STATS_URL = f'http://{SQLA_DB_HOST}:8081/'
PDNS_API_KEY = open('/run/secrets/pdns_api_key').read().strip()
PDNS_VERSION = '4.1.1'
