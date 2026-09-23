"""Smoke tests for curation_ui health endpoint and auth boundary."""

import pytest
import sys
from fastapi.testclient import TestClient
from src.shared.config import get_settings


class TestCurationUIImports:
    """Verify curation_ui modules import without error.

    This would have caught the Story/StoryModel bug from Task 2:
    if `curation_ui.main` had an unused `Story as StoryModel` import
    that was removed by autofix, the real `Story` import would break
    and this import test would fail.
    """

    def test_import_curation_ui_main(self):
        """Import curation_ui.main and assert it constructs the app."""
        from curation_ui import main
        assert main.app is not None
        assert main.app.title == "News Pipeline Curation"

    def test_import_curation_ui_health(self):
        """Import curation_ui.health and assert router exists."""
        from curation_ui import health
        assert health.router is not None


class TestHealthEndpoint:
    """Tests for GET /healthz endpoint."""

    def test_healthz_without_database_url(
        self, monkeypatch
    ):
        """GET /healthz with DATABASE_URL unset returns env_set.DATABASE_URL: false and verdict."""
        # Clear settings cache and unset DATABASE_URL
        # Use setenv("") instead of delenv to override .env file values
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        monkeypatch.delenv("CURATION_USER", raising=False)
        monkeypatch.delenv("CURATION_PASSWORD", raising=False)
        get_settings.cache_clear()

        # Import after cache clear so module-level settings pick up new env
        from curation_ui.main import app
        client = TestClient(app)

        response = client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["env_set"]["DATABASE_URL"] is False
        assert "verdict" in data
        assert isinstance(data["verdict"], str)
        assert len(data["verdict"]) > 0


class TestAuthBoundary:
    """Tests for HTTP Basic auth on GET / (triage page)."""

    def test_get_root_without_credentials_returns_401(
        self, monkeypatch
    ):
        """GET / without HTTP Basic credentials returns 401."""
        # Configure minimal settings: no auth, in-memory DB
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.delenv("CURATION_USER", raising=False)
        monkeypatch.delenv("CURATION_PASSWORD", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        get_settings.cache_clear()

        from curation_ui.main import app
        client = TestClient(app)

        response = client.get("/")
        assert response.status_code == 401
        # Should be a 401 with WWW-Authenticate header (not a 500)
        assert "www-authenticate" in response.headers
        assert "Basic" in response.headers["www-authenticate"]

    def test_get_root_without_configured_auth_returns_503_detail(
        self, monkeypatch
    ):
        """GET / with credentials but CURATION_USER/PASSWORD unset returns 503 detail, not 500."""
        # DB configured but auth not configured
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("CURATION_USER", "")
        monkeypatch.setenv("CURATION_PASSWORD", "")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        get_settings.cache_clear()

        # Import after cache clear so module-level settings in curation_ui.main
        # picks up the new env values (it calls get_settings() at line 42)
        import curation_ui.main as main_module
        from curation_ui.main import app

        # CRITICAL: The module-level `settings` variable in curation_ui.main was
        # assigned at import time from the OLD cache. We must replace it with the
        # new settings instance so require_auth() uses the updated values.
        main_module.settings = get_settings()

        # Debug: check what settings the app actually has
        s = get_settings()
        print(f"DEBUG test_get_root_without_configured_auth: has_curation_auth={s.has_curation_auth}, user='{s.curation_user}', pass='{s.curation_password}'", file=sys.stderr)

        client = TestClient(app)

        # Provide some credentials, but server has none configured
        response = client.get("/", auth=("anyuser", "anypass"))
        print(f"DEBUG test_get_root_without_configured_auth: response.status_code={response.status_code}, response.text={response.text[:500]}", file=sys.stderr)
        assert response.status_code == 503
        data = response.json()
        assert "detail" in data
        assert "not configured" in data["detail"].lower()
        assert "CURATION_USER" in data["detail"]

    @pytest.mark.asyncio
    async def test_get_root_with_valid_credentials_and_empty_db_returns_200(
        self, monkeypatch, db_engine
    ):
        """GET / with valid credentials against empty in-memory DB returns 200."""
        # Use the test fixtures' engine and session - they share the same in-memory DB
        # Override auth settings - must use setenv to override .env file values
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("CURATION_USER", "testuser")
        monkeypatch.setenv("CURATION_PASSWORD", "testpass")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)

        # Must clear cache BEFORE importing curation_ui.main because that module
        # calls get_settings() at module level (line 42) and caches it.
        from src.shared.config import get_settings as _get_settings
        _get_settings.cache_clear()

        # Also need to reset the global engine/session_maker in database.py
        # because they're module-level globals that get created on first import
        import src.shared.database as database_module
        database_module._engine = db_engine
        database_module._async_session_maker = None  # Will be recreated from db_engine

        # NOW import curation_ui.main - it will use the test engine
        import curation_ui.main as main_module
        from curation_ui.main import app

        # CRITICAL: The module-level `settings` variable in curation_ui.main was
        # assigned at import time from the OLD cache. We must replace it with the
        # new settings instance so require_auth() uses the updated values.
        main_module.settings = _get_settings()

        # Debug: check what settings the app actually has
        s = _get_settings()
        print(f"DEBUG: has_curation_auth={s.has_curation_auth}, user='{s.curation_user}', pass='{s.curation_password}'", file=sys.stderr)

        # The db_engine fixture creates tables on a shared in-memory SQLite
        # (using StaticPool) so all connections see the same tables.
        # No need to re-create tables here.

        from fastapi.testclient import TestClient
        client = TestClient(app)

        response = client.get("/", auth=("testuser", "testpass"))
        print(f"DEBUG: response.status_code={response.status_code}, response.text={response.text[:500]}", file=sys.stderr)
        assert response.status_code == 200
        # Should return HTML (Jinja2 template)
        assert "text/html" in response.headers.get("content-type", "")
        # Should contain the empty state or stories grid
        assert "News Pipeline Curation" in response.text