"""Vercel serverless entry point: serve the FastAPI curation UI as an ASGI app."""

from curation_ui.main import app  # noqa: F401  (Vercel serves the `app` variable)
