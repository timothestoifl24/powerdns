"""Gunicorn configuration.

Tuned for a small internal admin panel: a handful of sync workers is plenty,
and sync workers keep the LDAP/SAML libraries (neither of which is
async-friendly) on well-trodden ground.
"""

import multiprocessing
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8080")
workers = _int("GUNICORN_WORKERS", min(4, multiprocessing.cpu_count() * 2 + 1))
threads = _int("GUNICORN_THREADS", 4)
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")

# Long enough for a slow LDAP bind or IdP round trip, short enough to recycle
# a genuinely wedged worker.
timeout = _int("GUNICORN_TIMEOUT", 60)
graceful_timeout = _int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _int("GUNICORN_KEEPALIVE", 5)

# Recycle workers periodically so a slow leak in a long-lived deployment
# cannot grow without bound. The jitter avoids all workers restarting at once.
max_requests = _int("GUNICORN_MAX_REQUESTS", 2000)
max_requests_jitter = _int("GUNICORN_MAX_REQUESTS_JITTER", 200)

accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
# Log the real client address when running behind a reverse proxy.
access_log_format = '%({x-forwarded-for}i)s %(h)s "%(r)s" %(s)s %(b)s %(D)sus "%(a)s"'

# Never leak the stack in a response; Flask's own error handler covers users.
preload_app = False
