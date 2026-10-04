"""Deployment health: anonymous liveness, authenticated diagnosis.

GET /healthz answers monitoring with "is this up" and nothing else: an anonymous
caller used to get the python version, which providers were configured, the
database topology and live row counts, which is reconnaissance for free and never
needed by a probe. GET /healthz/details keeps that detail for a curator with
credentials. Neither ever returns a secret.
"""

import os
import sys

from fastapi import APIRouter, Depends
from sqlalchemy import text

from src.shared.config import get_settings
from src.shared.database import describe_database_url, get_session
from src.transparency.checkpoint import ed25519_available
from curation_ui.security import require_auth

router = APIRouter()


def _scrub(message: str, raw_url: str) -> str:
    """Trim an error message and hide the DB host if it shows up in it."""
    try:
        from sqlalchemy.engine import make_url
        host = make_url(raw_url.strip()).host
        if host:
            message = message.replace(host, "<db-host>")
    except Exception:
        pass
    return message[:300]


async def transparency_status() -> dict:
    """Checkpoint freshness and recent signer alerts, for the details view.

    Deliberately counts and ages rather than exposing content: this is the
    operator's own view, but a signer refusal alert carries the reason codes and
    root prefixes that a diagnosis needs, and nothing here needs a leaf payload.
    """
    from src.transparency import signing

    settings = get_settings()
    out: dict = {
        "origin": settings.transparency_origin,
        "max_interval_hours": settings.transparency_max_checkpoint_interval_hours,
        "signing_key_configured": bool((os.environ.get("TRANSPARENCY_SIGNING_KEY") or "").strip()),
        "cron_token_configured": bool((os.environ.get("TRANSPARENCY_CRON_TOKEN") or "").strip()),
        "ed25519_available": ed25519_available(),
        "least_privilege_dsn": bool(settings.transparency_signer_database_url.strip()),
    }
    async with get_session() as session:
        age = await signing.checkpoint_age_hours(session)
        out["checkpoint_age_hours"] = None if age is None else round(age, 3)
        healthy, verdict = signing.watchdog_verdict(
            age, max_interval_hours=settings.transparency_max_checkpoint_interval_hours
        )
        out["verdict"] = "ok" if healthy else "unhealthy"
        out["reason"] = verdict
        out["checkpoint_count"] = (
            await session.execute(text("select count(*) from transparency_checkpoints"))
        ).scalar()
        try:
            rows = await session.execute(
                text(
                    "select kind, created_at from transparency_alerts order by created_at desc limit 5"
                )
            )
            out["recent_alerts"] = [
                {"kind": str(kind), "created_at": str(created)} for kind, created in rows.all()
            ]
        except Exception:
            # The alerts table arrived with the v2 signer migration; a database
            # without it still gets a real freshness verdict.
            out["recent_alerts"] = []
    return out


@router.get("/healthz")
async def healthz():
    """Liveness for uptime monitoring.

    Deliberately answers 200 either way: this says whether the app process is
    serving, and a database blip is not a reason to fail a health check. The
    degraded state is in the body, and the detail behind it is on
    /healthz/details.
    """
    s = get_settings()
    if not s.has_database:
        return {"status": "degraded", "database": "not_configured"}
    try:
        async with get_session() as session:
            await session.execute(text("select 1"))
    except Exception:
        return {"status": "degraded", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


@router.get("/healthz/details")
async def healthz_details(user: str = Depends(require_auth)):
    """Why the deployment is or is not working. Requires the curator credentials."""
    s = get_settings()
    report = {
        "python": sys.version.split()[0],
        "on_vercel": bool(os.environ.get("VERCEL")),
        "env_set": {
            "DATABASE_URL": s.has_database,
            "GROQ_API_KEY or CEREBRAS_API_KEY": s.has_llm,
            "CURATION_USER and CURATION_PASSWORD": s.has_curation_auth,
        },
    }
    if not s.has_database:
        report["verdict"] = "DATABASE_URL is not set in this deployment's environment variables"
        return report

    report["database_url"] = describe_database_url(s.database_url)
    if "parse_error" in report["database_url"]:
        report["verdict"] = "DATABASE_URL cannot be parsed (special characters in the password must be URL-encoded)"
        return report

    try:
        async with get_session() as session:
            await session.execute(text("select 1"))
        report["db_connect"] = "ok"
    except Exception as exc:
        report["db_connect"] = f"FAILED: {type(exc).__name__}: {_scrub(str(exc), s.database_url)}"
        report["verdict"] = "The app boots, but cannot connect to the database"
        return report

    try:
        async with get_session() as session:
            rows = (await session.execute(text("select status, count(*) from stories group by status"))).all()
        report["stories_by_status"] = {str(status): count for status, count in rows}
    except Exception as exc:
        report["stories_table"] = f"FAILED: {type(exc).__name__}: {_scrub(str(exc), s.database_url)}"
        report["verdict"] = "Connected, but the tables are missing (run scripts/migrate.py against this database)"
        return report

    # Transparency: how old the newest published checkpoint is, and any signer
    # refusals. This is the surface the dead man's switch reads from -- a cron
    # that stopped firing is invisible everywhere else.
    #
    # This block used to also report `approved_posts`, a count of
    # curated_posts rows with status='APPROVED', on the grounds that the home
    # page and /posts filtered that table and so a column with the wrong enum
    # type would be caught here. Both of those readers are gone, so the metric
    # was reporting on a table nothing writes and a code path that cannot
    # happen. The curated_posts table itself is deliberately still there: see
    # DECISIONS.md. What matters here now is the checkpoint.
    try:
        report["transparency"] = await transparency_status()
    except Exception as exc:
        report["transparency"] = f"FAILED: {type(exc).__name__}: {_scrub(str(exc), s.database_url)}"

# The verdict is derived from the transparency block and nothing else. It
    # used to be set inside the try that counted curated_posts rows: the approve
    # /reject/edit flow that wrote them was removed on 2026-10-02, so the count
    # was never displayed and its query only ever ran to be able to overwrite
    # this verdict with "run the SQL migration" -- an instruction about a flow
    # that no longer exists, raised by a table the app never reads.
    transparency = report.get("transparency")
    if isinstance(transparency, dict) and transparency.get("verdict") != "ok":
        report["verdict"] = "App and database are fine, but transparency checkpoint signing is behind"
    else:
        report["verdict"] = "ok"
    return report