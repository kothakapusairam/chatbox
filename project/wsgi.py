"""WSGI entry point for gunicorn:  gunicorn -w 2 -b 0.0.0.0:8000 wsgi:app"""
import os

from app import create_app

app = create_app(os.environ.get("FLASK_ENV", "production"))
