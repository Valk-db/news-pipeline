"""GET /healthz: shows why the deployment is (or isn't) working. Never returns secrets."""

import os
import sys

from fastapi import APIRouter
from sqlalchemy import text

from src.shared.config import get_settings
from src.shared.database import describe_database_url, get_session

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


@router.get("/healthz")
async def healthz():
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
        report["verdict"] = "Connected, but the tables are missing (run scripts/init_db.py against this database)"
        return report

    # The home page and /posts filter curated_posts on 'APPROVED'; this fails if the column
    # was created with the wrong enum type.
    try:
        async with get_session() as session:
            report["approved_posts"] = (
                await session.execute(text("select count(*) from curated_posts where status = 'APPROVED'"))
            ).scalar()
        report["verdict"] = "ok"
    except Exception as exc:
        report["curated_posts"] = f"FAILED: {type(exc).__name__}: {_scrub(str(exc), s.database_url)}"
        report["verdict"] = "curated_posts.status has the wrong enum type: run the SQL migration"
    return report