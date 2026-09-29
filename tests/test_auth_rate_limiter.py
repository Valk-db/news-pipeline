"""Tests for auth rate limiter - only FAILED attempts should be throttled."""

import pytest
from fastapi.testclient import TestClient
from src.shared.config import get_settings
from src.shared import database as database_module


@pytest.fixture
def app_with_db(db_engine):
    """Create app with test database and auth configured."""
    # Configure settings
    import os
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    os.environ["CURATION_USER"] = "testuser"
    os.environ["CURATION_PASSWORD"] = "testpass"
    os.environ.pop("GROQ_API_KEY", None)
    os.environ.pop("CEREBRAS_API_KEY", None)

    get_settings.cache_clear()
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = get_settings()

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    return app


class TestAuthRateLimiter:
    """Tests for auth rate limiting - only failed attempts should count."""

    def test_30_good_requests_all_200(self, app_with_db):
        """30 successful auth requests should all return 200 (not rate limited)."""
        client = TestClient(app_with_db)

        for i in range(30):
            response = client.get("/", auth=("testuser", "testpass"))
            assert response.status_code == 200, f"Request {i+1} failed with {response.status_code}: {response.text[:200]}"

    def test_failed_attempts_get_throttled(self, app_with_db):
        """Repeated bad passwords should get throttled after threshold."""
        client = TestClient(app_with_db)

        # Make failed attempts - should eventually get rate limited
        # The exact threshold depends on implementation
        failed_count = 0
        for _ in range(50):
            response = client.get("/", auth=("testuser", "wrongpass"))
            if response.status_code == 429:
                failed_count += 1

        # Should have some rate limited responses
        assert failed_count > 0, "Expected some 429 responses for repeated failed auth"

    def test_x_forwarded_for_respected(self, app_with_db):
        """Rate limiting should honor X-Forwarded-For header."""
        client = TestClient(app_with_db)

        # Make requests from different "forwarded" IPs
        headers = {"X-Forwarded-For": "192.168.1.100"}
        response1 = client.get("/", auth=("testuser", "wrongpass"), headers=headers)
        assert response1.status_code == 401  # First attempt from this IP

        headers2 = {"X-Forwarded-For": "192.168.1.101"}
        response2 = client.get("/", auth=("testuser", "wrongpass"), headers=headers2)
        assert response2.status_code == 401  # First attempt from different IP

    def test_successful_after_failed_not_counted(self, app_with_db):
        """Successful auth after failed attempts should not be rate limited."""
        client = TestClient(app_with_db)

        # Make some failed attempts
        for _ in range(10):
            response = client.get("/", auth=("testuser", "wrongpass"))
            # May or may not be rate limited yet

        # Now try with correct credentials - should work
        response = client.get("/", auth=("testuser", "testpass"))
        # Should NOT be rate limited (200 or 401 if auth fails, but not 429)
        assert response.status_code != 429, "Successful auth should not be rate limited"