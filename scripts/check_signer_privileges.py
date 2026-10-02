#!/usr/bin/env python3
"""What can `transparency_signer` actually do? Ask Postgres, as the role.

This exists because the whole class of bug it catches is invisible to a passing
test suite. The signer tests run as the table owner, who has BYPASSRLS, so a
grant list that is wrong in three different ways still produces 60 green tests
while the signer in production cannot read the log, cannot publish a checkpoint,
and cannot record an alert.

So the checks are:

  catalog    has_table_privilege('transparency_signer', <table>, <priv>) for
             every table in public, plus every policy and whether RLS is armed.
             Anything the role holds that is not in EXPECTED is a finding, and so
             is anything in EXPECTED that it does not hold.
  execution  the same questions asked the only way that counts: SET ROLE, then
             read the log, read the checkpoints, write a checkpoint, write an
             alert, and try the four things it must not be able to do. Each
             statement runs in its own savepoint so one refusal does not abort
             the rest of the probe.
  --sign     the real signing path, as the role: sign_next_checkpoint() with
             SqlAlchemyMerkleLog and an ed25519 signer over the actual log.
             Needs TRANSPARENCY_SIGNING_KEY. Prints the verdict, not the key.

Nothing is ever committed. Every statement runs inside one transaction that is
rolled back before the catalog is re-read, and the closing section re-reads it
to show the session left no residue. The connection has to be an owner or
superuser connection (it grants itself membership to SET ROLE); no credential is
ever printed, only row counts, boolean privilege answers, and SQLSTATE codes.

Usage:
    DATABASE_URL=... python scripts/check_signer_privileges.py
    DATABASE_URL=... TRANSPARENCY_SIGNING_KEY=... python scripts/check_signer_privileges.py --sign
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.shared.config import get_settings  # noqa: E402
from src.shared.database import get_session_for_url  # noqa: E402

ROLE = "transparency_signer"

# The entire intended privilege set. Everything else is a finding, in both
# directions: a grant that is not here is undeclared access, and an entry here
# that Postgres does not report is a broken feature.
EXPECTED: dict[str, set[str]] = {
    "merkle_log_entries": {"SELECT"},
    "transparency_checkpoints": {"SELECT", "INSERT"},
    "transparency_alerts": {"SELECT", "INSERT"},
}

# The tables the signer must have no reach into at all.
FORBIDDEN_TABLES = ("raw_articles", "stories", "reporting_units", "events")

PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

# (label, sql) run as the role, each in its own savepoint. `want` says what the
# answer must be, so a silent drift shows up as a FAIL and not as a curiosity.
PROBES = [
    ("read the log", "SELECT count(*) FROM merkle_log_entries", "rows"),
    ("read published checkpoints", "SELECT count(*) FROM transparency_checkpoints", "rows"),
    ("read alerts", "SELECT count(*) FROM transparency_alerts", "rows"),
    (
        "publish a checkpoint",
        "INSERT INTO transparency_checkpoints (id, tree_size, merkle_root, chain_hash, timestamp,"
        " signature, algorithm, key_id, checkpoint_format, key_name, previous_digest, log_id,"
        " created_at) VALUES (gen_random_uuid(), 999999, repeat('0',64), repeat('0',64), now(),"
        " repeat('0',128), 'probe', 'probe', 'c2sp-tlog-checkpoint-v2', NULL, NULL,"
        " 'probe.dev/transparency', now())",
        "ok",
    ),
    (
        "record an alert",
        "INSERT INTO transparency_alerts (kind, detail) VALUES ('probe', '{}'::jsonb)",
        "ok",
    ),
    ("edit a published checkpoint", "UPDATE transparency_checkpoints SET key_id='x' WHERE false", "denied"),
    ("delete a published checkpoint", "DELETE FROM transparency_checkpoints WHERE false", "denied"),
    ("truncate the log", "TRUNCATE merkle_log_entries", "denied"),
    ("append to the log", "INSERT INTO merkle_log_entries (index, hash_scheme, timestamp, payload,"
     " canonical_payload, leaf_hash, chain_hash) VALUES (999999, 'n1:sha256', now(),"
     " '{}'::json, '{}', repeat('0',64), repeat('0',64))", "denied"),
    ("read article rows", "SELECT count(*) FROM raw_articles", "denied"),
]

findings: list[str] = []


def note(ok: bool, message: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {message}")
    if not ok:
        findings.append(message)


async def privileges_held(conn: AsyncSession) -> dict[str, list[str]]:
    """Every table privilege the role holds, from the catalog, for every table."""
    held = await conn.execute(
        text(
            "SELECT c.relname, p.priv FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN LATERAL unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',"
            " 'REFERENCES','TRIGGER']) AS p(priv)"
            " WHERE n.nspname = 'public' AND c.relkind IN ('r','p')"
            " AND has_table_privilege(:role, c.oid, p.priv)"
            " ORDER BY c.relname, p.priv"
        ),
        {"role": ROLE},
    )
    out: dict[str, set[str]] = {}
    for table, priv in held.all():
        out.setdefault(table, set()).add(priv)
    return {table: sorted(privs) for table, privs in out.items()}


async def catalog(conn: AsyncSession) -> None:
    """Every privilege the role holds, against the declared set."""
    print("== catalog: has_table_privilege for every table in public ==")
    actual = await privileges_held(conn)

    for table in sorted(set(actual) | set(EXPECTED) | set(FORBIDDEN_TABLES)):
        got = actual.get(table, [])
        want = EXPECTED.get(table)
        if want is None:
            marker = "unreachable" if table in FORBIDDEN_TABLES else "not in EXPECTED"
            if got:
                note(False, f"{table}: {ROLE} holds {got} ({marker})")
            else:
                print(f"PASS  {table:26} no privileges ({marker})")
            continue
        if got == sorted(want):
            print(f"PASS  {table:26} {got}")
        else:
            note(False, f"{table}: want {sorted(want)}, got {got}")

    print("\n== row security and policies ==")
    rls = await conn.execute(
        text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
            " (SELECT count(*) FROM pg_policies p WHERE p.schemaname = n.nspname"
            "  AND p.tablename = c.relname)"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relname = ANY(:tables) ORDER BY c.relname"
        ),
        {"tables": list(EXPECTED)},
    )
    for table, enabled, forced, policies in rls.all():
        print(f"  {table:26} rls={enabled} force_rls={forced} policies={policies}")
        note(bool(enabled), f"{table}: row-level security is enabled")

    mine = await conn.execute(
        text("SELECT tablename, policyname, cmd, roles::text FROM pg_policies"
             " WHERE schemaname = 'public' ORDER BY tablename, policyname")
    )
    rows = mine.all()
    for table, name, cmd, roles in rows:
        print(f"  policy {table:26} {name:28} {cmd:6} TO {roles}")
    print()
    if rows:
        note(
            all(roles == "{transparency_signer}" for _, _, _, roles in rows),
            "every policy in public is scoped to transparency_signer alone",
        )
    else:
        note(False, "no policies in public: every grant on this role is inert")


async def execution(session: AsyncSession) -> None:
    """The same questions, asked as the role.

    The session is already SET ROLE'd by the caller, which is the point: every
    answer here is the role's own, not the owner's. A check run as the owner
    passes on a role that cannot do the job at all, which is exactly how the
    signer shipped broken.
    """
    who = (await session.execute(text("SELECT current_user"))).scalar_one()
    print(f"== execution: the same operations as {who} ==")
    if who != ROLE:
        note(False, f"probes must run as {ROLE}, not {who}")
        return
    for label, sql, want in PROBES:
        await session.execute(text("SAVEPOINT probe"))
        try:
            result = await session.execute(text(sql))
            rows = result.all() if result.returns_rows else []
        except Exception as exc:  # noqa: BLE001 - the point is to catch them all
            await session.execute(text("ROLLBACK TO SAVEPOINT probe"))
            state, detail = _refusal(exc)
            print(f"  {label:30} refused [{state}] {detail}")
            note(want == "denied", f"as {ROLE}: {label} -> refused ({state})")
            continue
        await session.execute(text("ROLLBACK TO SAVEPOINT probe"))
        shown = rows[0][0] if rows else "no rows"
        print(f"  {label:30} ok ({shown})")
        if want == "denied":
            note(False, f"as {ROLE}: {label} was ALLOWED and must not be")
        else:
            note(True, f"as {ROLE}: {label} -> {shown}")


def _refusal(exc: Exception) -> tuple[str, str]:
    """(why Postgres refused, the one-line message).

    Which mechanism refused is the interesting part: `no privilege` means the
    grant list is narrow enough, `row-level security` means a policy is doing
    the work, and `append-only trigger` means the role would have been allowed
    by the grants and was stopped by the trigger instead.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(exc, "sqlstate", None)
    message = str(getattr(orig, "message", None) or exc).splitlines()[0][:78]
    if sqlstate == "42501":
        return "no privilege", message
    if "row-level security" in message:
        return "row-level security", message
    if "append-only" in message or "transparency log is" in message:
        return "append-only trigger", message
    if "cannot truncate" in message:
        return "append-only trigger", message
    return f"sqlstate {sqlstate or '?'}", message



async def signing_run(session: AsyncSession) -> None:
    """The real signer path, as the role. Rolled back like everything else."""
    from src.transparency.checkpoint import ed25519_available, generate_ed25519_signer
    from src.transparency.log import SqlAlchemyMerkleLog
    from src.transparency import signing

    seed = (os.environ.get("TRANSPARENCY_SIGNING_KEY") or "").strip()
    if not seed:
        print("\n== signing path: SKIPPED, TRANSPARENCY_SIGNING_KEY is not set ==")
        return
    if not ed25519_available():
        print("\n== signing path: SKIPPED, cryptography is not installed ==")
        return

    print("\n== signing path: sign_next_checkpoint() as the role ==")
    signer = generate_ed25519_signer(seed.encode("utf-8"))
    result = await signing.sign_next_checkpoint(
        session,
        SqlAlchemyMerkleLog(session),
        signer,
        origin=get_settings().transparency_origin,
    )
    for key, value in result.to_dict().items():
        if key == "detail" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "signature"}
        print(f"  {key:16} {value}")
    note(
        result.status in ("signed", "already_signed"),
        f"the signing path as {ROLE} returned {result.status}"
        + (f" ({result.reason})" if result.reason else ""),
    )
    # A refusal that is not a deliberate guard is a broken path, not a pass:
    # empty_log here would mean the role cannot see the log at all.
    if result.status == "refused":
        note(False, f"the signing path refused: {result.reason} {result.detail}")


async def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("no DATABASE_URL: this check is against a real Postgres, as a real role")
        return 1

    async with get_session_for_url(database_url) as session:
        me = (await session.execute(text("SELECT current_user"))).scalar_one()
        print(f"connected as {me}; checking role {ROLE}\n")
        try:
            await session.execute(text(f"GRANT {ROLE} TO CURRENT_USER"))
        except Exception as exc:  # noqa: BLE001
            print(f"cannot grant myself membership: {type(exc).__name__}: {exc}")
            return 1
        await catalog(session)

        # From here on the session IS the role. The catalog has to be read as an
        # owner (the admin is not a member of the role it is auditing); the
        # execution probes must not be, or they prove nothing -- run as the
        # owner they pass on a role that cannot do the job at all, which is
        # precisely how the signer shipped broken.
        await session.execute(text(f"SET ROLE {ROLE}"))
        await execution(session)
        if "--sign" in sys.argv:
            await signing_run(session)

        # Nothing above is committed. Roll back, then prove the session left the
        # database the way it found it.
        await session.rollback()
        who = (await session.execute(text("SELECT current_user"))).scalar_one()
        print(f"\nrolled back; current_user is {who}")

    print("== residue check: re-read after the rollback ==")
    async with get_session_for_url(database_url) as session:
        for label, sql in [
            ("merkle_log_entries", "SELECT count(*) FROM merkle_log_entries WHERE index = 999999"),
            ("transparency_checkpoints",
             "SELECT count(*) FROM transparency_checkpoints WHERE tree_size = 999999"),
            ("transparency_alerts", "SELECT count(*) FROM transparency_alerts WHERE kind = 'probe'"),
        ]:
            row = (await session.execute(text(sql))).scalar_one()
            note(row == 0, f"{label}: no probe row survived the rollback")
        summary = await privileges_held(session)
        for table, want in EXPECTED.items():
            note(
                summary.get(table) == sorted(want),
                f"{table}: privileges unchanged after the probe "
                f"(want {sorted(want)}, got {summary.get(table)})",
            )
        for table in FORBIDDEN_TABLES:
            note(
                table not in summary,
                f"{table}: {ROLE} holds nothing after the probe (got {summary.get(table)})",
            )
        pol = (await session.execute(
            text("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        )).scalar_one()
        print(f"  policies in public: {pol}")
        member = (await session.execute(
            # A literal, not a bind: asyncpg cannot parse ':name::regrole'.
            text("SELECT r.rolname, m.admin_option FROM pg_auth_members m"
                 " JOIN pg_roles r ON r.oid = m.member"
                 f" WHERE m.roleid = '{ROLE}'::regrole"),
        )).all()
        print(f"  roles that can assume {ROLE}: {member or '(nobody)'}")

    print()
    if findings:
        print(f"{len(findings)} FINDING(S):")
        for item in findings:
            print(f"  - {item}")
        return 1
    print("CLEAN: " + ROLE + " holds exactly "
          + ", ".join(f"{t}: {'+'.join(sorted(p))}" for t, p in EXPECTED.items())
          + ", and can do the signing job under RLS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
