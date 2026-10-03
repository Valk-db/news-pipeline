"""Tests for the checkpoint cron routes and the watchdog.

The routes are the only unauthenticated-by-default surface this app adds, so
what is tested here is mostly refusal: no token, wrong token, throttling, no
signing key, no database. A route that signs when it is not configured is worse
than a route that is down, because the log would carry checkpoints whose key
nobody can check.

The signing path itself is covered in tests/test_transparency_signer_v2.py
against the service layer; these tests drive it through HTTP with a stubbed
session so the transport, the auth, and the status codes are pinned too.
"""

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import curation_ui.cron as cron_module
from curation_ui.cron import CRON_TOKEN_ENV_VAR, cron_limiter, router
from src.shared.config import get_settings
from src.transparency import signing

TOKEN = "test-cron-token-value"


@pytest.fixture(autouse=True)
def _clean_cron_env(monkeypatch):
    """Every test starts with no token configured and an empty limiter.

    The unset case is itself a tested state, so it must not leak between tests,
    and a limiter carrying one test's failures would 429 the next one.
    """
    monkeypatch.delenv(CRON_TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv("TRANSPARENCY_SIGNING_KEY", raising=False)
    cron_limiter.reset()
    yield
    cron_limiter.reset()


def _client() -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _authorize() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestCronAuth:
    """Bearer token, compared in constant time, throttled per client."""

    def test_an_unset_token_refuses_everyone_with_503(self):
        """The safe state: no token means no access, not open access."""
        response = _client().get("/api/cron/checkpoint")
        assert response.status_code == 503
        assert CRON_TOKEN_ENV_VAR in response.json()["detail"]

    def test_a_wrong_token_is_401(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        response = _client().get(
            "/api/cron/checkpoint", headers={"Authorization": f"Bearer {TOKEN[:-3]}XYZ"}
        )
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_a_missing_token_is_401_not_503(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        assert _client().get("/api/cron/checkpoint").status_code == 401

    def test_a_non_bearer_scheme_is_401(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        response = _client().get(
            "/api/cron/checkpoint", headers={"Authorization": f"Basic {TOKEN}"}
        )
        assert response.status_code == 401

    def test_the_token_is_rotatable_at_runtime(self, monkeypatch):
        """Read from os.environ per request, not from a cached Settings object.

        A warm serverless instance holds a cached Settings; if the token were
        read from there, a rotation would either keep accepting the old value or
        need a redeploy to take effect.
        """
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, "old-token")
        client = _client()
        assert client.get(
            "/api/cron/checkpoint", headers={"Authorization": "Bearer old-token"}
        ).status_code != 401
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, "new-token")
        assert client.get(
            "/api/cron/checkpoint", headers={"Authorization": "Bearer old-token"}
        ).status_code == 401
        assert client.get(
            "/api/cron/checkpoint", headers={"Authorization": "Bearer new-token"}
        ).status_code != 401

    def test_repeated_failures_are_throttled(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        client = _client()
        statuses = [
            client.get("/api/cron/checkpoint", headers={"Authorization": "Bearer nope"}).status_code
            for _ in range(8)
        ]
        assert 429 in statuses
        assert statuses[-1] == 429

    def test_a_correct_token_is_not_throttled_by_earlier_failures(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        client = _client()
        for _ in range(4):
            client.get("/api/cron/checkpoint", headers={"Authorization": "Bearer nope"})
        assert client.get("/api/cron/checkpoint", headers=_authorize()).status_code != 429


class TestCronConfigurationRefusals:
    """Missing configuration refuses; it never falls back to a dev key."""

    def test_no_signing_key_is_503_and_does_not_fall_back_to_hmac(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNING_KEY", "   ")
        response = _client().get("/api/cron/checkpoint", headers=_authorize())
        assert response.status_code == 503
        body = response.json()["detail"]
        assert "TRANSPARENCY_SIGNING_KEY" in body
        assert "HMAC" in body.upper()

    def test_no_database_is_503(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNING_KEY", "seed")
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "")
        get_settings.cache_clear()
        saved = get_settings().database_url
        monkeypatch.setattr(get_settings(), "database_url", "", raising=False)
        response = _client().get("/api/cron/checkpoint", headers=_authorize())
        assert response.status_code == 503
        monkeypatch.setattr(get_settings(), "database_url", saved, raising=False)


class TestCronSigningOverHttp:
    """The happy path and the refusal status code, over HTTP."""

    @pytest.fixture
    def stubbed(self, monkeypatch):
        """Replace the session, the signer, and the service call.

        The service layer has its own tests against a real session; what is
        under test here is the transport: the response shape, the published note
        in the body, and 409 for a refusal rather than 500.
        """
        state = {"result": None}

        @asynccontextmanager
        async def fake_session(_url):
            class FakeSession:
                def add(self, row):  # noqa: D401
                    pass

                async def flush(self):
                    pass

                async def commit(self):
                    pass

            yield FakeSession()

        monkeypatch.setattr(cron_module, "get_session_for_url", fake_session)
        monkeypatch.setattr(cron_module, "_signer_or_error", lambda: object())

        async def fake_sign(session, log, signer, *, origin, lock=True):
            return state["result"]

        monkeypatch.setattr(signing, "sign_next_checkpoint", fake_sign)
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "postgresql://stub/db")
        alerts = []

        async def fake_alert(session, kind, *, detail=None):
            alerts.append(kind)

        monkeypatch.setattr(signing, "record_alert", fake_alert)
        state["alerts"] = alerts
        return state

    def test_a_signed_run_returns_200_with_the_published_note(self, stubbed):
        from src.transparency.checkpoint import (
            FORMAT_C2SP_V2,
            Checkpoint,
            generate_ed25519_signer,
            sign_checkpoint,
        )
        from datetime import datetime, timezone

        signer = generate_ed25519_signer(seed=b"http-test")
        checkpoint = Checkpoint(
            tree_size=3,
            merkle_root=bytes(range(32)),
            chain_hash=bytes(range(32, 64)),
            timestamp=datetime(2026, 10, 2, tzinfo=timezone.utc),
            format=FORMAT_C2SP_V2,
        )
        signed = sign_checkpoint(checkpoint, signer)
        stubbed["result"] = signing.SigningResult(
            signed=signed,
            status="signed",
            tree_size=3,
            merkle_root=checkpoint.merkle_root.hex(),
            previous_digest=None,
            detail={"key_id": signed.key_id},
        )
        response = _client().get("/api/cron/checkpoint", headers=_authorize())
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == "signed"
        # The endpoint doubles as publication: the response carries the note a
        # third party can verify, so nothing has to fetch it back out of the DB.
        assert body["signed_note"] == signed.signed_note_document()
        assert body["checkpoint"]["tree_size"] == 3
        assert stubbed["alerts"] == []

    def test_a_refusal_is_409_and_records_an_alert(self, stubbed):
        stubbed["result"] = signing.SigningResult(
            signed=None,
            status="refused",
            reason=signing.REFUSAL_INCONSISTENT_HISTORY,
            tree_size=8,
            detail={"previous_tree_size": 4},
        )
        response = _client().get("/api/cron/checkpoint", headers=_authorize())
        # 409, not 500: the signer worked and deliberately declined.
        assert response.status_code == 409
        body = response.json()
        assert body["verdict"] == "refused"
        assert body["reason"] == signing.REFUSAL_INCONSISTENT_HISTORY
        assert "signed_note" not in body
        assert stubbed["alerts"] == [signing.REFUSAL_INCONSISTENT_HISTORY]

    def test_an_already_signed_run_is_200(self, stubbed):
        stubbed["result"] = signing.SigningResult(
            signed=None, status="already_signed", tree_size=8, merkle_root="ab" * 32
        )
        response = _client().get("/api/cron/checkpoint", headers=_authorize())
        assert response.status_code == 200
        assert response.json()["verdict"] == "already_signed"


class TestWatchdogRoute:
    """The dead man's switch, over HTTP."""

    @pytest.fixture
    def stubbed_session(self, monkeypatch):
        @asynccontextmanager
        async def fake_session(_url):
            class FakeSession:
                async def execute(self, statement):
                    class Result:
                        def all(self):
                            return [("inconsistent_history", "2026-10-02 06:00:00+00")]

                        def scalar(self):
                            return None

                    return Result()

            yield FakeSession()

        monkeypatch.setattr(cron_module, "get_session_for_url", fake_session)
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "postgresql://stub/db")

        async def age(_session, *, now=None):
            return 30.0

        monkeypatch.setattr(signing, "checkpoint_age_hours", age)
        return None

    def test_a_stale_checkpoint_reports_unhealthy(self, stubbed_session):
        response = _client().get("/api/cron/checkpoint/watchdog", headers=_authorize())
        # 200 with an unhealthy verdict: an external probe reads the body of a
        # successful response, not just the status code.
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == "unhealthy"
        assert body["checkpoint_age_hours"] == 30.0
        assert "26.0h interval" in body["reason"]
        assert body["recent_alerts"][0]["kind"] == "inconsistent_history"

    def test_the_watchdog_is_itself_behind_the_token(self, stubbed_session):
        assert _client().get("/api/cron/checkpoint/watchdog").status_code == 401

    def test_the_watchdog_route_exists_at_the_path_vercel_calls(self):
        """The path in vercel.json must be a real route, not a 404."""
        paths = {route.path for route in router.routes}
        assert "/api/cron/checkpoint" in paths
        assert "/api/cron/checkpoint/watchdog" in paths
        # Both are GET: Vercel Cron issues GET and cannot issue anything else.
        methods = {route.path: route.methods for route in router.routes}
        assert methods["/api/cron/checkpoint"] == {"GET"}
        assert methods["/api/cron/checkpoint/watchdog"] == {"GET"}

    def test_vercel_json_schedules_both_routes(self):
        """A cron entry pointing at a missing route is a silent no-op forever."""
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "vercel.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        scheduled = {entry["path"] for entry in config.get("crons", [])}
        assert "/api/cron/checkpoint" in scheduled
        assert "/api/cron/checkpoint/watchdog" in scheduled
        for entry in config["crons"]:
            assert entry["path"] in paths_of_app()
            assert entry["schedule"].count(" ") == 4  # five cron fields


def paths_of_app() -> set[str]:
    from curation_ui.main import app

    def walk(routes):
        for route in routes:
            nested = getattr(route, "routes", None)
            if nested is None:
                nested = getattr(getattr(route, "original_router", None), "routes", None)
            if nested:
                yield from walk(nested)
            elif getattr(route, "path", None):
                yield route.path

    return set(walk(app.routes))
