"""WSGI entry point: `gunicorn wsgi:application`."""

from app import create_app

application = create_app()
