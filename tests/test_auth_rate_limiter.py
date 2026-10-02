"""Tests for the failed-auth limiter: one client's failures must actually block.

The limiter used to key buckets on the first X-Forwarded-For entry, so a client
rotating that header got a fresh bucket per request and the 10-failure threshold
never fired; the bucket map also grew without bound. These tests pin the
properties that make it a real limiter: the threshold holds for a single client,
the key comes from the direct peer unless a trusted proxy is configured, buckets
expire, and the map stays capped.
"""

import pytest
from fastapi.testclient import TestClient
from src.shared.config import get_settings
from src.shared import database as database_module

AUTH = ("testuser", "testpass")


@pytest.fixture
def app_with_db(db_engine, monkeypatch):
    """Create app with test database and auth configured."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("CURATION_TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)

    get_settings.cache_clear()
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    import curation_ui.security as security_module
    from curation_ui.main import app

    main_module.settings = get_settings()

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    # The limiter is process state; start every test from an empty map.
    security_module.auth_limiter.reset()
    yield app
    security_module.auth_limiter.reset()


class TestAuthRateLimiter:
    """Throttling applies to one client's failures, and nothing else."""

    def test_10_bad_attempts_from_one_client_block(self, app_with_db):
        """The tenth consecutive failure from one client is answered with 429."""
        from curation_ui.security import auth_limiter

        client = TestClient(app_with_db)
        statuses = [
            client.get("/", auth=("testuser", "wrongpass")).status_code
            for _ in range(12)
        ]

        # The threshold allows 10 failures; everything after that is refused.
        assert statuses[:10] == [401] * 10, statuses
        assert statuses[10:] == [429, 429], statuses
        assert auth_limiter.blocked("testclient")

    def test_blocked_client_gets_retry_after(self, app_with_db):
        """A blocked client is told when to come back, not just that it is blocked."""
        client = TestClient(app_with_db)
        for _ in range(10):
            client.get("/", auth=("testuser", "wrongpass"))

        blocked = client.get("/", auth=("testuser", "wrongpass"))
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0

    def test_good_credentials_always_pass(self, app_with_db):
        """Only failures count: a curator already signed in is never locked out."""
        client = TestClient(app_with_db)
        for _ in range(25):
            assert client.get("/", auth=AUTH).status_code == 200

    def test_correct_password_passes_even_after_the_bucket_is_full(self, app_with_db):
        """A blocked attacker does not get to lock the real curator out."""
        client = TestClient(app_with_db)
        for _ in range(10):
            client.get("/", auth=("testuser", "wrongpass"))
        assert client.get("/", auth=("testuser", "wrongpass")).status_code == 429

        assert client.get("/", auth=AUTH).status_code == 200

    def test_success_clears_the_bucket(self, app_with_db):
        """One correct login drops the accumulated failures."""
        from curation_ui.security import auth_limiter

        client = TestClient(app_with_db)
        for _ in range(9):
            client.get("/", auth=("testuser", "wrongpass"))
        assert client.get("/", auth=AUTH).status_code == 200

        # Nine more failures must still be allowed before the tenth blocks.
        statuses = [
            client.get("/", auth=("testuser", "wrongpass")).status_code
            for _ in range(9)
        ]
        assert statuses == [401] * 9, statuses
        assert not auth_limiter.blocked("testclient")


class TestClientKey:
    """X-Forwarded-For is only trusted when a proxy we control set it."""

    def test_forwarded_header_is_ignored_by_default(self, app_with_db):
        """With no trusted proxy configured, rotating the header buys a new bucket
        of exactly nothing: every request is the same peer, so it still blocks."""
        from curation_ui.security import auth_limiter

        client = TestClient(app_with_db)
        statuses = []
        for index in range(12):
            response = client.get(
                "/",
                auth=("testuser", "wrongpass"),
                headers={"X-Forwarded-For": f"10.0.0.{index}"},
            )
            statuses.append(response.status_code)

        assert statuses[:10] == [401] * 10, statuses
        assert statuses[10:] == [429, 429], statuses
        # One bucket, keyed on the peer, not one per forged header value.
        assert len(auth_limiter._failures) == 1

    def test_trusted_proxy_forwards_the_client_key(self, app_with_db, monkeypatch):
        """With the peer trusted, the chain decides the key, so two clients
        behind one proxy throttle independently."""
        from curation_ui.security import auth_limiter, client_key

        monkeypatch.setenv("CURATION_TRUSTED_PROXIES", "10.0.0.1")
        get_settings.cache_clear()

        class _Client:
            host = "10.0.0.1"

        class _Request:
            client = _Client()

            def __init__(self, forwarded):
                self._forwarded = forwarded

            @property
            def headers(self):
                return {"X-Forwarded-For": self._forwarded}

        # The last hop that is not one of ours is the caller: a proxy we trust
        # further down the chain is skipped, a hop we do not trust is not.
        assert client_key(_Request("203.0.113.7, 10.0.0.1")) == "203.0.113.7"
        assert client_key(_Request("203.0.113.9")) == "203.0.113.9"
        assert client_key(_Request("203.0.113.7, 10.0.0.2")) == "10.0.0.2"
        # No header, or a chain that is all proxies: fall back to the peer.
        assert client_key(_Request("")) == "10.0.0.1"
        assert client_key(_Request("10.0.0.1")) == "10.0.0.1"
        assert auth_limiter.blocked("203.0.113.7") is False


class TestFailureLimiterUnit:
    """Window expiry and the client cap, on the limiter itself."""

    def test_failures_outside_the_window_are_forgotten(self):
        from curation_ui.security import FailureLimiter

        limiter = FailureLimiter(window_seconds=60, max_failures=10)
        for _ in range(9):
            limiter.record_failure("1.2.3.4", now=1000.0)
        assert limiter.blocked("1.2.3.4", now=1000.0) is False
        limiter.record_failure("1.2.3.4", now=1000.0)
        assert limiter.blocked("1.2.3.4", now=1000.0) is True

        # Past the window the bucket is empty again and drops out of the map.
        assert limiter.blocked("1.2.3.4", now=1061.0) is False
        assert "1.2.3.4" not in limiter._failures

    def test_reading_never_creates_a_bucket(self):
        from curation_ui.security import FailureLimiter

        limiter = FailureLimiter(window_seconds=60, max_failures=10)
        for _ in range(50):
            assert limiter.blocked("9.9.9.9") is False
        assert limiter._failures == {}

    def test_client_count_is_capped(self):
        """Rotating source addresses cannot grow the map without bound."""
        from curation_ui.security import FailureLimiter

        limiter = FailureLimiter(window_seconds=60, max_failures=10, max_clients=5)
        for index in range(50):
            limiter.record_failure(f"10.0.0.{index}", now=1000.0)
        assert len(limiter._failures) == 5

    def test_cap_prefers_evicting_expired_buckets(self):
        """At the cap, a bucket nothing is using goes before a live one."""
        from curation_ui.security import FailureLimiter

        limiter = FailureLimiter(window_seconds=60, max_failures=10, max_clients=2)
        limiter.record_failure("10.0.0.1", now=1000.0)
        limiter.record_failure("10.0.0.2", now=1061.0)
        assert set(limiter._failures) == {"10.0.0.1", "10.0.0.2"}

        # At the cap again: the stale bucket goes, the in-window one stays.
        limiter.record_failure("10.0.0.3", now=1061.0)
        assert set(limiter._failures) == {"10.0.0.2", "10.0.0.3"}

        # Every bucket live: the oldest one still has to go, so the cap holds.
        limiter.record_failure("10.0.0.4", now=1061.0)
        assert len(limiter._failures) == 2