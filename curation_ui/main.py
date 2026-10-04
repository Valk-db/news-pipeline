"""FastAPI + HTMX curation UI for story triage.

This module is the app factory and nothing else: the middleware, the two
availability checks, and the router registrations. The routes live next door,
one module per surface, so curation_ui/health.py can gate a route without
importing main and each surface can be read on its own:

  cron.py           /api/cron/checkpoint (+ watchdog): bearer-token checkpoint signing
curation.py       the auth-gated read-only queue: / and /story/{story_id}
  story_api.py      the auth-gated per-story JSON APIs
  globe.py          public /api/globe/* JSON (the /globe page was removed 2026-10-02)
  map_api.py        public /api/map/* JSON plus the freshness and story serializers
  public_pages.py   public /stories/{id}, /proof/{id}, /map
  health.py         /healthz and the authenticated /healthz/details
  security.py       require_auth, the failed-auth limiter, CSRF
  discovery.py      the read filter/ranking rules the map, story and queue surfaces share
  events.py         the event query and GeoJSON Feature serializer shared by events and replay
  proofs.py         the /proof/{id} view model, rendered by public_pages
  cache.py          the edge Cache-Control middleware, registered below
  app_state.py      the Jinja environment, the error page, and the database check

There are no state-changing routes: approve, reject, edit and save were removed
on 2026-10-02, so nothing here mutates a row and require_csrf guards nothing
(tests/test_route_table.py pins both).
"""

import logging
import os
import secrets
import socket

# Force IPv4-only DNS resolution to avoid Vercel's lack of outbound IPv6 routes
# This patches the resolver asyncio (and asyncpg through it) calls underneath
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from curation_ui.app_state import BASE_DIR, templates  # noqa: F401  (re-exported: tests import main.templates)
from curation_ui.cache import map_read_cache
from curation_ui.cron import router as cron_router
from curation_ui.curation import router as curation_router
from curation_ui.discovery import (  # noqa: F401  (public read API, re-exported: tests import these from main)
    MAP_EVENTS_MAX_LIMIT,
    normalize_headline,
    _verification_badge,
)
from curation_ui.globe import router as globe_router
from curation_ui.health import router as health_router
from curation_ui.map_api import format_freshness_stamp  # noqa: F401  (re-exported: tests import it from main)
from curation_ui.map_api import router as map_router
from curation_ui.public_pages import router as public_pages_router
from curation_ui.story_api import router as story_api_router
from src.shared.config import get_settings

app = FastAPI(title="News Pipeline Curation")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

settings = get_settings()

logger = logging.getLogger(__name__)


def check_database_available() -> tuple[bool, str]:
    """Check if database is available, return (available, error_message)."""
    if not settings.has_database:
        return False, "Database not configured. Set DATABASE_URL environment variable."
    return True, ""


def check_llm_available() -> tuple[bool, str]:
    """Check if LLM is available, return (available, error_message).

    Availability is either a configured provider API key, or an already
    -initialized/injected client (e.g. the mock LLMClient tests set on
    src.shared.llm._llm_client). Gating on settings.has_llm alone made this
    return False even when a working client was already in place.

    No route calls this any more. It was the gate the approve and edit routes
    opened with, and both were removed on 2026-10-02, so the deterministic-
    caption fallback this used to describe is gone with them
    (tests/test_no_llm_fallback.py records that build_deterministic_caption is
    no longer part of any flow). Kept because /healthz/details still reports
    whether an LLM key is configured and the next surface that needs the check
    should reach it the same way.
    """
    import src.shared.llm as llm_module
    if not settings.has_llm and llm_module._llm_client is None:
        return False, "No LLM configured. Set GROQ_API_KEY or CEREBRAS_API_KEY environment variable."
    return True, ""
# Router order matches the order the routes appeared when they all lived here, so
# a path that used to be matched by an earlier literal still is. No two of these
# patterns overlap, so the order is a readability property rather than a dispatch
# one; tests/test_route_table.py pins the whole set anyway.
app.include_router(health_router)
app.include_router(cron_router)
app.include_router(curation_router)
app.include_router(story_api_router)
app.include_router(globe_router)
app.include_router(map_router)
app.include_router(public_pages_router)

# The routers cannot import this module (it imports them), so they reach the
# database availability check through app.state. Assigning the function, not a
# snapshot of its result, keeps it reading main's module-level `settings` —
# which the test suite replaces after import — at call time.
app.state.check_database_available = check_database_available

# Content-Security-Policy for every response.
#
# script-src carries a per-response nonce rather than 'unsafe-inline': the only
# inline scripts are the theme/keyboard handlers in the templates, and they all
# take the nonce from request.state.csp_nonce. style-src does need
# 'unsafe-inline' because a nonce cannot cover inline style attributes, and
# Leaflet and the templates both position elements with style="".
#
# img-src is https: wide on purpose: story thumbnails and map imagery come from
# arbitrary outlet and tile domains, and narrowing it would drop real article
# images. Everything else is pinned to the origins the pages actually load
# from, and object/frame/form/base are locked down.
CSP_TEMPLATE = "; ".join([
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self' 'nonce-{nonce}' https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com",
    "img-src 'self' data: blob: https:",
    "font-src 'self' data: https://fonts.gstatic.com",
    "connect-src 'self'",
    "worker-src 'self' blob:",
])


@app.middleware("http")
async def content_security_policy(request: Request, call_next):
    """Attach the CSP header, and mint the nonce the templates' inline scripts use."""
    request.state.csp_nonce = secrets.token_urlsafe(16)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP_TEMPLATE.format(
        nonce=request.state.csp_nonce
    )
    return response


# Registered second so it is the OUTERMOST of the two: Starlette's add_middleware
# inserts at position 0, so the last registered wraps everything below it. The
# request therefore reaches the router and comes back through the CSP middleware
# first (CSP header attached), then through this one, which reads the body to
# decide whether it was an error and only then adds Cache-Control. Both headers
# end up on the same response.
app.middleware("http")(map_read_cache)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)