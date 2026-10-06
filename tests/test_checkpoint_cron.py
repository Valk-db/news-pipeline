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

import hashlib
import json
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import curation_ui.cron as cron_module
from curation_ui.cron import CRON_TOKEN_ENV_VAR, cron_limiter, router
from src.shared.config import get_settings
from src.transparency import signing
from datetime import UTC


def _seed(label: str) -> bytes:
    """A deterministic >= 32-byte signing seed for a test.

    generate_ed25519_signer refuses a seed shorter than MIN_SIGNING_SEED_BYTES,
    so tests that need a reproducible key pair cannot use a short label. This
    hashes the label to exactly 32 bytes, which is deterministic per label and
    satisfies the production constraint rather than bypassing it.
    """
    return hashlib.sha256(f"test-seed:{label}".encode()).digest()


# 32+ characters: F12 added a minimum token length, so a short fixture token
# would be rejected as "not configured" and every auth test would 503.
TOKEN = "test-cron-token-value-not-a-real-secret"


@pytest.fixture(autouse=True)
def _clean_cron_env(monkeypatch):
    """Every test starts with no token configured and an empty limiter.

    The unset case is itself a tested state, so it must not leak between tests,
    and a limiter carrying one test's failures would 429 the next one.
    """
    monkeypatch.delenv(CRON_TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv("TRANSPARENCY_SIGNING_KEY", raising=False)
    monkeypatch.delenv("TRANSPARENCY_SIGNER_DATABASE_URL", raising=False)
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


def _raw_request(authorization: str):
    """A Starlette Request carrying exactly the header bytes given.

    TestClient cannot be used for this: httpx encodes header values as ASCII and
    raises UnicodeEncodeError before the request is sent, so the F12 crash could
    not be provoked through it at all.
    """
    from starlette.requests import Request

    raw = [(b"authorization", authorization.encode("latin-1"))]
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b""}
    )


def _run(coro):
    """Drive one coroutine to completion (the routes are plain async functions)."""
    import asyncio

    return asyncio.run(coro)


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
        # Both tokens meet F12's minimum length, so this test measures rotation
        # and nothing else.
        old_token = "old-cron-token-aaaaaaaaaaaaaaaaa"
        new_token = "new-cron-token-bbbbbbbbbbbbbbbbbb"
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, old_token)
        client = _client()
        assert client.get(
            "/api/cron/checkpoint", headers={"Authorization": f"Bearer {old_token}"}
        ).status_code != 401
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, new_token)
        assert client.get(
            "/api/cron/checkpoint", headers={"Authorization": f"Bearer {old_token}"}
        ).status_code == 401
        assert client.get(
            "/api/cron/checkpoint", headers={"Authorization": f"Bearer {new_token}"}
        ).status_code != 401

    def test_a_non_ascii_token_is_rejected_not_a_crash(self, monkeypatch):
        """F12: secrets.compare_digest raises TypeError on a non-ASCII str.

        Both operands used to be str, so a header carrying a non-ASCII byte hit
        "comparing strings with non-ASCII characters is not supported" and
        escaped as an unhandled 500 -- reachable by anyone, on an endpoint whose
        whole purpose is to be unattended, and a 500 reads as "the signer is
        broken" rather than "you sent nonsense". The comparison now runs on
        encoded bytes, which is total over every possible input and still
        constant-time.

        Driven through require_cron_token directly rather than through
        TestClient: httpx encodes header values as ASCII and would refuse to
        send the request at all, which would make this test pass for the wrong
        reason. A real HTTP server is more permissive -- Starlette decodes header
        bytes as latin-1, so a raw 0xE9 byte arrives as U+00E9 and is exactly the
        str that used to crash compare_digest. The scope dict is the minimum
        client_key(request) needs.
        """
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        for raw in (b"\xe9", b"\xff\xfe", "токен".encode(), b"caf\xe9"):
            request = _raw_request(f"Bearer {raw.decode('latin-1')}")
            with pytest.raises(HTTPException) as excinfo:
                _run(cron_module.require_cron_token(request))
            assert excinfo.value.status_code == 401, raw
            assert not isinstance(excinfo.value, TypeError)

    def test_a_non_ascii_token_against_a_non_ascii_configured_one_still_compares(
        self, monkeypatch
    ):
        """The other direction: a configured token that is itself non-ASCII.

        Encoding only the presented token would fix the crash and break
        correctness here, so both sides go through the same encoder.
        """
        unicode_token = "jeton-transparence-non-ascii-écure"
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, unicode_token)
        assert (
            _run(cron_module.require_cron_token(_raw_request(f"Bearer {unicode_token}")))
            == "cron"
        )
        with pytest.raises(HTTPException) as excinfo:
            _run(
                cron_module.require_cron_token(
                    _raw_request(f"Bearer {unicode_token[:-1]}X")
                )
            )
        assert excinfo.value.status_code == 401

    def test_a_short_configured_token_refuses_everyone(self, monkeypatch, caplog):
        """F12: no minimum token length existed.

        Any non-empty string authenticated the transparency cron, so a placeholder
        like "x" -- the shape a half-finished deploy leaves behind -- was a
        working credential for the endpoint that publishes signed checkpoints.
        There is no way to tell a placeholder from a deliberate weak token from
        inside the process, so a short one is treated as not configured at all and
        the answer is the 503 that says the deployment is not ready, not a 401
        that implies the caller was wrong.

        Driven through require_cron_token directly, not the route: with the floor
        removed the route still returns 503, because the next thing it does is
        notice that no signing key is configured. Through the HTTP layer this test
        passes for the wrong reason and pins nothing.
        """
        for short in ("x", "changeme", "a" * (cron_module.MIN_CRON_TOKEN_LENGTH - 1)):
            monkeypatch.setenv(CRON_TOKEN_ENV_VAR, short)
            with caplog.at_level("ERROR"):
                with pytest.raises(HTTPException) as excinfo:
                    _run(
                        cron_module.require_cron_token(
                            _raw_request(f"Bearer {short}")
                        )
                    )
            assert excinfo.value.status_code == 503, short
        assert "is not a secret" in caplog.text

    def test_a_token_at_exactly_the_minimum_length_is_accepted(self, monkeypatch):
        """The floor is a floor, not a near-miss check."""
        exact = "a" * cron_module.MIN_CRON_TOKEN_LENGTH
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, exact)
        assert (
            _run(cron_module.require_cron_token(_raw_request(f"Bearer {exact}"))) == "cron"
        )

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
        monkeypatch.setenv("TRANSPARENCY_SIGNING_KEY", _seed("no-db").hex())
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "")
        get_settings.cache_clear()
        saved = get_settings().database_url
        monkeypatch.setattr(get_settings(), "database_url", "", raising=False)
        response = _client().get("/api/cron/checkpoint", headers=_authorize())
        assert response.status_code == 503
        monkeypatch.setattr(get_settings(), "database_url", saved, raising=False)

    def test_an_unset_signer_dsn_does_not_fall_back_to_the_app_dsn(self, monkeypatch):
        """F4: the app's broad-write DSN must never be used to sign.

        The original defect only shows up when DATABASE_URL *is* set, which is
        every real deployment -- so a test that blanks both (as the old one did)
        cannot see it. Here the app DSN is deliberately populated and pointed at
        a URL that must never be opened: if the fallback survives, the route
        tries to connect and the failure is a driver error rather than the 503.
        """
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        # 32 bytes: below the entropy floor the signer refuses, and this
        # test is about the DSN, so the seed must not be the thing that fails.
        monkeypatch.setenv("TRANSPARENCY_SIGNING_KEY", _seed("f4-dsn").hex())
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "")
        get_settings.cache_clear()
        monkeypatch.setattr(
            get_settings(),
            "database_url",
            "postgresql://app-user:pw@127.0.0.1:1/app-db-that-must-not-be-opened",
            raising=False,
        )

        opened = []

        def _never(url):
            opened.append(url)
            raise AssertionError(f"the app DSN was opened for signing: {url}")

        monkeypatch.setattr(cron_module, "get_session_for_url", _never)

        response = _client().get("/api/cron/checkpoint", headers=_authorize())
        assert response.status_code == 503
        assert "transparency_signer_database_url" in response.json()["detail"]
        assert opened == []

    def test_the_watchdog_also_refuses_without_a_signer_dsn(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "")
        get_settings.cache_clear()
        response = _client().get("/api/cron/checkpoint/watchdog", headers=_authorize())
        assert response.status_code == 503
        assert "transparency_signer_database_url" in response.json()["detail"]

    def test_the_signer_dsn_is_the_only_dsn_used_when_set(self, monkeypatch):
        """The positive control: with the DSN set, that is the URL used."""
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "postgresql://signer/pw@h/db")
        get_settings.cache_clear()
        assert cron_module._database_url("signing") == "postgresql://signer/pw@h/db"
        assert cron_module._database_url("watchdog") == "postgresql://signer/pw@h/db"

    def test_a_signer_dsn_is_required_for_both_surfaces(self, monkeypatch):
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "  ")
        get_settings.cache_clear()
        for purpose in ("signing", "watchdog"):
            with pytest.raises(HTTPException) as excinfo:
                cron_module._database_url(purpose)
            assert excinfo.value.status_code == 503
            assert purpose in excinfo.value.detail


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

        async def fake_sign(
            session, log, signer, *, origin, lock=True, trusted_keys=None,
            head=None, genesis_confirmed=False,
        ):
            # Recorded rather than ignored: these are the F1/F2/F3 trust inputs
            # the route now has to supply, and a route that stopped passing them
            # would still pass every assertion in this file if the stub swallowed
            # them.
            state["trust_inputs"] = {
                "trusted_keys": sorted(trusted_keys or {}),
                "head": head,
                "genesis_confirmed": genesis_confirmed,
            }
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
        from datetime import datetime

        signer = generate_ed25519_signer(seed=_seed("http-test"))
        checkpoint = Checkpoint(
            tree_size=3,
            merkle_root=bytes(range(32)),
            chain_hash=bytes(range(32, 64)),
            timestamp=datetime(2026, 10, 2, tzinfo=UTC),
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
                async def rollback(self):
                    return None

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

        async def lag(_session):
            # 2 entries waiting, so "stale" and not "idle" (F13): an old
            # checkpoint is only the signer having died if there is something
            # left for it to sign.
            return 3, 5

        monkeypatch.setattr(signing, "checkpoint_lag", lag)
        return None

    def test_a_stale_checkpoint_reports_unhealthy(self, stubbed_session):
        response = _client().get("/api/cron/checkpoint/watchdog", headers=_authorize())
        # 200 with an unhealthy verdict: an external probe reads the body of a
        # successful response, not just the status code.
        assert response.status_code == 200
        body = response.json()
        assert body["verdict"] == "unhealthy"
        assert body["state"] == "stale"
        assert body["checkpoint_age_hours"] == 30.0
        assert "26.0h interval" in body["reason"]
        assert body["published_tree_size"] == 3
        assert body["log_size"] == 5
        assert body["recent_alerts"][0]["kind"] == "inconsistent_history"

    def test_an_old_checkpoint_on_an_idle_log_is_not_unhealthy(self, monkeypatch):
        """F13: idle and dead are different states and an operator acts on them
        differently.

        A log with no new entries has nothing to sign, so an old checkpoint there
        is the pipeline being quiet. Reporting that as unhealthy is how people
        learn to ignore the watchdog, which defeats the dead man's switch.
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session(_url):
            class FakeSession:
                async def rollback(self):
                    return None

                async def execute(self, statement):
                    class Result:
                        def all(self):
                            return []

                    return Result()

            yield FakeSession()

        monkeypatch.setattr(cron_module, "get_session_for_url", fake_session)
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "postgresql://stub/db")

        async def age(_session, *, now=None):
            return 900.0

        async def lag(_session):
            return 3, 3  # checkpoint covers every entry: nothing waiting

        monkeypatch.setattr(signing, "checkpoint_age_hours", age)
        monkeypatch.setattr(signing, "checkpoint_lag", lag)
        body = _client().get("/api/cron/checkpoint/watchdog", headers=_authorize()).json()
        assert body["state"] == "idle"
        assert body["verdict"] == "ok"

    def test_a_read_failure_does_not_turn_the_watchdog_into_a_500(self, monkeypatch):
        """F13: the re-query after an aborted transaction was the 500.

        A failed statement leaves the Postgres transaction aborted, so the old
        code's `except: age_hours = await checkpoint_age_hours(session)` raised
        InFailedSqlTransaction and the "graceful degradation" path was itself
        the crash. This pins the fix: every read rolls back and reports its own
        failure, and the endpoint still answers with state "unknown".
        """
        from contextlib import asynccontextmanager

        rollbacks = []

        @asynccontextmanager
        async def fake_session(_url):
            class FakeSession:
                async def rollback(self):
                    rollbacks.append(1)

                async def execute(self, statement):
                    raise RuntimeError("InFailedSqlTransaction")

            yield FakeSession()

        monkeypatch.setattr(cron_module, "get_session_for_url", fake_session)
        monkeypatch.setenv(CRON_TOKEN_ENV_VAR, TOKEN)
        monkeypatch.setenv("TRANSPARENCY_SIGNER_DATABASE_URL", "postgresql://stub/db")

        async def boom(*args, **kwargs):
            raise RuntimeError("relation does not exist")

        monkeypatch.setattr(signing, "checkpoint_age_hours", boom)
        monkeypatch.setattr(signing, "checkpoint_lag", boom)
        response = _client().get("/api/cron/checkpoint/watchdog", headers=_authorize())
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "unknown"
        assert body["checkpoint_age_hours"] is None
        assert body["recent_alerts"] == []
        # One rollback per failed read. Reusing the aborted session without one
        # is the defect; this is the guard against it coming back.
        assert len(rollbacks) == 3

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
