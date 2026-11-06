"""Vercel serverless entrypoint: re-export the FastAPI app as `app` (ASGI)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app  # noqa: F401,E402  (Vercel looks for `app`)
