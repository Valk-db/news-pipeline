"""App-level state every router shares: the Jinja environment, the error page, and
the two availability checks.

The checks themselves stay in curation_ui.main because they close over the
module-level `settings` object captured when the app factory ran (the test suite
replaces it after import). Routers reach them through app.state rather than
importing curation_ui.main, which imports them: that would be a circular import.
"""

import os

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def render_error_page(request: Request, message: str, status_code: int = 503) -> HTMLResponse:
    """Render a friendly error page when a feature is unavailable."""
    return templates.TemplateResponse(
        request,
        "error.html",
        {"request": request, "message": message},
        status_code=status_code
    )


def check_database(request: Request) -> tuple[bool, str]:
    """(available, error_message) for the database, as the app factory sees it."""
    return request.app.state.check_database_available()


def check_llm(request: Request) -> tuple[bool, str]:
    """(available, error_message) for the LLM, as the app factory sees it."""
    return request.app.state.check_llm_available()