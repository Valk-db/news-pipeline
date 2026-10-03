"""Cron endpoints for transparency-log checkpoint signing, and the watchdog.

Two GET routes, because Vercel Cron issues GET requests with an
`Authorization: Bearer <CRON_SECRET>` header and nothing else -- there is no
body, no custom header, and no way to pass a request payload.

    GET /api/cron/checkpoint              sign a checkpoint, or refuse with a reason
    GET /api/cron/checkpoint/watchdog     is the newest checkpoint recent enough?

Why a watchdog at all, given the cron is what signs: because the failure nobody
sees is the cron never firing. On Vercel Hobby a scheduled invocation that fails
sends no notification and posts no failure email, so a signer that stopped
running three weeks ago looks exactly like a signer with nothing new to sign.
The only honest dead man's switch is one that measures the artifact, not the
scheduler: the watchdog compares the newest published checkpoint's timestamp
against TRANSPARENCY_MAX_CHECKPOINT_INTERVAL_HOURS and reports unhealthy, and
writes a cron_missed alert so the failure is visible on /healthz/details rather
than only in an HTTP response nobody reads.

What this module does NOT do is self-alert by email or SMS. An independent
ping-based monitor (a healthchecks.io or UptimeRobot probe pointed at this
endpoint) is the thing that pages a human when Vercel itself is the thing that
is broken, and that needs an account on a third-party service. That is a
deployment decision for Tyler, documented in DECISIONS.md, not something to
create from inside the app.

Auth: bearer token, constant-time comparison, throttled per client with the same
FailureLimiter curation_ui/security.py uses for passwords. Every failure returns
a plain JSON body saying what is missing, because a cron integration that cannot
be debugged from its own logs is not much of a safety net.
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from curation_ui.security import FailureLimiter, client_key
from src.shared.config import get_settings
from src.shared.database import get_session_for_url
from src.transparency import signing
from src.transparency.checkpoint import (
    FORMAT_C2SP_V2,
    SignedCheckpoint,
    ed25519_available,
    generate_ed25519_signer,
)
from src.transparency.log import SqlAlchemyMerkleLog

logger = logging.getLogger(__name__)

router = APIRouter()

CRON_TOKEN_ENV_VAR = "TRANSPARENCY_CRON_TOKEN"

# Cron callers are a machine, not a person guessing at a password, so the
# window is long and the budget small: Vercel fires this on a schedule and a
# wrong token means a misconfiguration, not a person. Still worth limiting,
# because the endpoint is public and the token is the only thing on it.
CRON_WINDOW_SECONDS = 300.0
CRON_MAX_FAILURES = 5
cron_limiter = FailureLimiter(CRON_WINDOW_SECONDS, CRON_MAX_FAILURES)


def _configured_token() -> str:
    """The expected bearer token, read from the environment.

    os.environ directly, not Settings: a stale cached Settings object from a
    warm serverless instance must not decide whether a rotated token is
    accepted, and this avoids ever holding the token in a model that could be
    dumped.
    """
    return (os.environ.get(CRON_TOKEN_ENV_VAR) or "").strip()


async def require_cron_token(request: Request) -> str:
    """Check `Authorization: Bearer <token>`; 401 when it does not match.

    Two things are deliberate. The comparison is secrets.compare_digest, so a
    caller cannot learn the token one byte at a time from response timing. And
    the missing-token case is a 503, not a 401: "TRANSPARENCY_CRON_TOKEN is not
    set" is a deployment bug, and answering 401 would send whoever is debugging
    it looking for the wrong thing entirely.
    """
    expected = _configured_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{CRON_TOKEN_ENV_VAR} is not set, so the checkpoint cron is refusing "
                "every caller rather than accepting an unauthenticated one"
            ),
        )
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    key = client_key(request)
    if scheme.lower() != "bearer" or not presented or not secrets.compare_digest(presented.strip(), expected):
        if cron_limiter.blocked(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed cron authentications.",
                headers={"Retry-After": str(int(CRON_WINDOW_SECONDS))},
            )
        cron_limiter.record_failure(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid cron bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    cron_limiter.record_success(key)
    return "cron"


def _signer_or_error() -> object:
    """Build the production signer from configuration, or raise HTTPException.

    Refuses rather than falling back to a dev HMAC key. An HMAC-signed
    checkpoint on a public proof page would look signed to the page while
    proving nothing to any third party, which is the exact failure the key
    work exists to remove.
    """
    if not ed25519_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Ed25519 signing is unavailable: the 'cryptography' package is not "
                "installed. requirements.txt must list it, or the Vercel bundle cannot sign."
            ),
        )
    seed = (os.environ.get("TRANSPARENCY_SIGNING_KEY") or "").strip()
    if not seed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "TRANSPARENCY_SIGNING_KEY is not set, so no checkpoint can be signed. "
                "The route does not fall back to the development HMAC key."
            ),
        )
    return generate_ed25519_signer(seed.encode("utf-8"))


def _database_url(purpose: str) -> str:
    """The least-privilege signer DSN. Raises 503 rather than degrading.

    F4: this used to fall back to `settings.database_url` when
    transparency_signer_database_url was unset. That fallback is the defect,
    not a convenience. The whole point of the dedicated role (see
    supabase/migrations/20261002230000_transparency_signer_rbac.sql) is that the
    signing path holds a role with SELECT on the log plus INSERT on the two
    transparency tables and nothing else -- no UPDATE, no DELETE, no reach into
    any other table. Falling back to the app DSN handed the signing job the
    app's broad-write credentials and silently gave that property away, and
    nothing in the code, the tests, or the health check could tell: the route
    reported success either way. The minimal-role property depended on a config
    step that no code enforced and no failure ever surfaced.

    So the fallback is gone. Unset is not "use something broader", it is "this
    deployment has not been configured to sign", and it must fail loudly. The
    caller names the purpose so the 503 says which surface refused.
    """
    settings = get_settings()
    dedicated = (settings.transparency_signer_database_url or "").strip()
    if not dedicated:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "transparency_signer_database_url is not set, so checkpoint signing "
                f"({purpose}) has no least-privilege DSN and will not fall back to the "
                "app's broad-write DATABASE_URL. Configure it to the transparency_signer "
                "role; see supabase/migrations/20261002230000_transparency_signer_rbac.sql."
            ),
        )
    return dedicated


def _result_body(result: signing.SigningResult) -> dict:
    """The response body for a signing run.

    The published artifact is included on success (the signed-note document plus
    its fields) so the endpoint doubles as the publication step: whatever
    answers this call has the note a third party can verify, without a second
    lookup.
    """
    body: dict = {
        "verdict": result.status,
        "reason": result.reason,
        "tree_size": result.tree_size,
        "merkle_root": result.merkle_root,
        "previous_digest": result.previous_digest,
        "detail": result.detail,
    }
    signed: SignedCheckpoint | None = result.signed
    if signed is not None and signed.checkpoint.format == FORMAT_C2SP_V2:
        note = signed.signed_note_document()
        if note is not None:
            body["checkpoint"] = signed.to_dict()
            body["signed_note"] = note
    return body


@router.get("/api/cron/checkpoint")
async def cron_checkpoint(_: str = Depends(require_cron_token)):
    """Sign a checkpoint over the whole log. Idempotent: safe to re-run."""
    signer = _signer_or_error()
    database_url = _database_url("signing")
    settings = get_settings()
    # get_session_for_url commits on clean exit and rolls back on an exception.
    # The advisory lock lives in sign_next_checkpoint's transaction, so an
    # overlapping fire skips rather than double-signing, and it is released
    # whether this run commits, refuses, or raises.
    async with get_session_for_url(database_url) as session:
        result = await signing.sign_next_checkpoint(
            session,
            SqlAlchemyMerkleLog(session),
            signer,
            origin=settings.transparency_origin,
        )
        if result.status == "refused":
            await signing.record_alert(session, result.reason or "refused", detail=result.to_dict())
    body = _result_body(result)
    if result.status == "refused":
        # 409, not 500: the system is working and deliberately declined. A 500
        # would tell the caller something is broken when the opposite is true.
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=body)
    return body


@router.get("/api/cron/checkpoint/watchdog")
async def cron_checkpoint_watchdog(_: str = Depends(require_cron_token)):
    """Report whether the newest published checkpoint is recent enough.

    Measures the artifact rather than the schedule: if no checkpoint is newer
    than the interval, the deployment is unhealthy no matter what the cron
    believes it did. Returns 200 with status "unhealthy" rather than a 5xx, so
    an external probe can read the verdict from the body of a successful
    response instead of only alerting on non-200s.
    """
    database_url = _database_url("watchdog")
    settings = get_settings()
    age_hours = None
    alerts = []
    async with get_session_for_url(database_url) as session:
        try:
            age_hours = await signing.checkpoint_age_hours(session)
            rows = await session.execute(
                text(
                    "select kind, created_at from transparency_alerts "
                    "order by created_at desc limit 5"
                )
            )
            alerts = [{"kind": str(kind), "created_at": str(created)} for kind, created in rows.all()]
        except Exception as exc:
            # The alerts table is added by a later migration than the rest of
            # the schema; a database without it still gets a real watchdog
            # answer, just without the alert history.
            logger.info(f"Could not read transparency_alerts: {type(exc).__name__}")
            age_hours = await signing.checkpoint_age_hours(session)
    healthy, verdict = signing.watchdog_verdict(
        age_hours, max_interval_hours=settings.transparency_max_checkpoint_interval_hours
    )
    return {
        "verdict": "ok" if healthy else "unhealthy",
        "checkpoint_age_hours": None if age_hours is None else round(age_hours, 3),
        "max_interval_hours": settings.transparency_max_checkpoint_interval_hours,
        "reason": verdict,
        "recent_alerts": alerts,
    }
