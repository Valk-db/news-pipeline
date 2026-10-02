"""The signer role can actually do its job: grants, policies, and fail-closed.

`transparency_signer` shipped with a grant list that was wrong in three ways and
sixty passing tests, because every signer test runs as the table owner. The
owner has BYPASSRLS, reads everything, and is allowed to update; the role is
none of those things. Measured as the role on dev before this batch:

    SELECT merkle_log_entries        -> 0 rows (the owner sees 13)
    SELECT transparency_checkpoints  -> permission denied for table
    SELECT transparency_alerts       -> permission denied for table
    INSERT transparency_checkpoints  -> new row violates row-level security policy
    INSERT transparency_alerts       -> new row violates row-level security policy

so the signer could not read the log, could not chain a checkpoint, could not
publish one, and could not record a refusal. The tests could not see any of it.

Two classes here, and the split is deliberate:

  TestMigrationIsTheBoundary   reads the SQL. Runs everywhere, no database, and
                               pins the things a green test suite never looks at:
                               that every policy is dropped before it is created,
                               that every policy is scoped to the role, that the
                               file cannot disable RLS, and that the role is
                               granted nothing beyond the five entries below.
  TestRoleOnPostgres           asks Postgres, as the role, in a scratch schema.
                               Skipped without DATABASE_URL. This is the class
                               that would have caught the original bug: it reads
                               the log, publishes a checkpoint, records an alert,
                               tries the four things the role must not do, and
                               asserts the privilege set is EXACTLY the intended
                               one -- no more, so a future grant shows up here.

Fail-closed is pinned at both levels. With no policies the log is invisible to
the role, the signer sees an empty log, and the correct answer is a refusal
(`empty_log`), never a checkpoint over nothing. The unit suite has
`test_an_empty_log_is_refused` for the in-memory log; this is the same guard with
the visibility bug that caused it in place, which is the only version of it that
was ever actually load-bearing.
"""

import os
import re
import urllib.parse
from pathlib import Path

import pytest

MIGRATION_NAME = "20261002230000_transparency_signer_rbac.sql"
MIGRATION = (
    Path(__file__).resolve().parent.parent / "supabase" / "migrations" / MIGRATION_NAME
)
SQL = MIGRATION.read_text()
SCHEMA = "signer_rbac_scratch"
ROLE = "transparency_signer"

# The entire intended privilege set, in one place. The SQL tests and the live
# tests both read this, so the declared intent and the asserted behaviour cannot
# drift apart.
EXPECTED: dict[str, set[str]] = {
    "merkle_log_entries": {"SELECT"},
    "transparency_checkpoints": {"SELECT", "INSERT"},
    "transparency_alerts": {"SELECT", "INSERT"},
}

TABLES = list(EXPECTED)

CREATE_POLICY = re.compile(
    r"CREATE POLICY\s+(?P<name>\w+)\s+ON\s+(?P<table>\w+)\s+"
    r"FOR\s+(?P<cmd>\w+)\s+TO\s+(?P<roles>[\w,\s]+?)\s+USING|WITH CHECK",
    re.I,
)


def _policies() -> list[dict[str, str]]:
    """Every CREATE POLICY in the migration, with its table, command and roles."""
    found = []
    for match in re.finditer(
        r"CREATE POLICY\s+(\w+)\s+ON\s+(\w+)\s+FOR\s+(\w+)\s+TO\s+([\w,\s]+?)\s+(USING|WITH CHECK)\s*\(true\)",
        SQL,
        re.I,
    ):
        name, table, cmd, roles, kind = match.groups()
        found.append(
            {
                "name": name,
                "table": table,
                "cmd": cmd.upper(),
                "roles": roles.strip(),
                "clause": kind.upper(),
            }
        )
    return found


def _grant_statements() -> list[tuple[str, set[str], set[str]]]:
    """(verb, privileges, tables) for every GRANT/REVOKE inside a FOREACH block.

    Parsed by block rather than by scanning for GRANT: the tables live in the
    FOREACH ... ARRAY[...] and the verb in the format() call below it, so a
    whole-block parse is the only reading that cannot pair a grant with the
    wrong table list when the file is edited.
    """
    found = []
    for block in re.finditer(
        r"FOREACH\s+\w+\s+IN ARRAY ARRAY\[(.*?)\]\s*LOOP(.*?)(?=FOREACH|\Z)", SQL, re.S
    ):
        tables = set(re.findall(r"'(\w+)'", block.group(1)))
        for stmt in re.finditer(r"(GRANT|REVOKE)\s+([A-Z, ]+?)\s+ON\s+%I", block.group(2)):
            privs = {p.strip() for p in stmt.group(2).split(",") if p.strip()}
            found.append((stmt.group(1), privs, tables))
    return found


class TestMigrationIsTheBoundary:
    """The SQL itself: a grant list nobody checks is a grant list that rots."""

    def test_every_policy_is_dropped_before_it_is_created(self):
        """Re-running the file must be a no-op, not a duplicate-key error."""
        created = {p["name"] for p in _policies()}
        assert created, "the migration defines no policies, so every grant is inert"
        for name in created:
            assert f"DROP POLICY IF EXISTS {name}" in SQL, f"{name} is created but never dropped"

    def test_every_policy_is_scoped_to_the_signer_role(self):
        """`TO transparency_signer` is the only thing making these policies safe.

        A policy with no TO clause applies to every role, which would be a
        blanket hole in the RLS that 20261002220000 put on every table.
        """
        for policy in _policies():
            assert policy["roles"] == ROLE, f"{policy['name']} is not scoped to the role"

    def test_the_policy_set_is_exactly_the_commands_the_role_holds(self):
        """One policy per (table, command) the role is granted, and no others."""
        got = {(p["table"], p["cmd"].upper()) for p in _policies()}
        want = {(table, priv.upper()) for table, privs in EXPECTED.items() for priv in privs}
        assert got == want

    def test_no_policy_uses_a_predicate_narrower_than_everything(self):
        """A predicate here would let the role sign a root over a subset.

        The root covers all leaves [0, tree_size) and the equivocation check
        re-derives the prefix an earlier checkpoint covers, so a narrower
        predicate would produce a root that is not the log's.
        """
        for policy in _policies():
            assert policy["clause"] in ("USING", "WITH CHECK")
            assert "(true)" in SQL.split(f"CREATE POLICY {policy['name']}")[1][:200]

    def test_the_role_is_granted_exactly_the_intended_privileges(self):
        """The granted set, read out of the SQL rather than trusted."""
        granted: dict[str, set[str]] = {}
        for verb, privs, tables in _grant_statements():
            if verb != "GRANT":
                continue
            for table in tables:
                granted.setdefault(table, set()).update(privs)
        assert granted == EXPECTED, f"the migration grants {granted}"

    def test_raw_articles_is_revoked_rather_than_granted(self):
        """The signer needs no article row: the content hash is in the payload.

        20261002200000 granted SELECT on raw_articles as an accident of looping
        over two table names, and its own header comment then claimed the role
        had no access to it. One of the two was a lie; now the grant is gone and
        the comment is true.
        """
        revoked_all = {table for verb, privs, tables in _grant_statements()
                       if verb == "REVOKE" and "ALL" in privs for table in tables}
        assert "raw_articles" in revoked_all, f"revoked wholesale: {sorted(revoked_all)}"
        for verb, privs, tables in _grant_statements():
            if verb == "GRANT":
                assert "raw_articles" not in tables, f"raw_articles is still granted {sorted(privs)}"

    def test_the_migration_cannot_disable_row_level_security(self):
        """F4 was a table with RLS off. This file must never be able to do that."""
        lowered = SQL.lower()
        assert "disable row level security" not in lowered
        assert "no row level security" not in lowered
        for table in TABLES:
            assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in SQL

    def test_no_mutation_privilege_is_granted_anywhere(self):
        """The append-only guarantee is a grant list, not just a trigger."""
        for match in re.finditer(r"GRANT\s+([A-Z,\s]+?)\s+ON", SQL):
            privs = {p.strip() for p in match.group(1).split(",") if p.strip()}
            assert not privs & {"UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "ALL"}, (
                f"the migration grants {sorted(privs)}"
            )

    def test_the_role_is_never_dropped_or_altered(self):
        """Dev carries a durable membership on this role; reversing it needs DROP ROLE.

        Which fails with DependentObjectsStillExist. The role stays, and so does
        the NOLOGIN, non-superuser shape.
        """
        assert "DROP ROLE" not in SQL.upper()
        assert "CREATE ROLE transparency_signer NOLOGIN" in SQL
        assert "ALTER ROLE" not in SQL.upper()


# --------------------------------------------------------------------------
# Live: the same questions, asked of Postgres as the role.
# --------------------------------------------------------------------------

DDL = {
    "merkle_log_entries": """
        CREATE TABLE {s}.merkle_log_entries (
            id uuid NOT NULL,
            index integer NOT NULL,
            hash_scheme varchar(32) NOT NULL,
            timestamp timestamptz NOT NULL,
            payload jsonb NOT NULL,
            canonical_payload text NOT NULL,
            leaf_hash varchar(64) NOT NULL,
            chain_hash varchar(64) NOT NULL,
            PRIMARY KEY (id)
        )
    """,
    "transparency_checkpoints": """
        CREATE TABLE {s}.transparency_checkpoints (
            id uuid NOT NULL,
            tree_size integer NOT NULL,
            merkle_root varchar(64) NOT NULL,
            chain_hash varchar(64) NOT NULL,
            timestamp timestamptz NOT NULL,
            signature text NOT NULL,
            algorithm varchar(64) NOT NULL,
            key_id varchar(128) NOT NULL,
            checkpoint_format varchar(32),
            key_name varchar(128),
            previous_digest varchar(64),
            log_id varchar(128),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        )
    """,
    "transparency_alerts": """
        CREATE TABLE {s}.transparency_alerts (
            id uuid NOT NULL,
            kind text NOT NULL,
            detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        )
    """,
    # Present only so "the role has no reach into raw_articles" is a measured
    # fact rather than an absence of evidence.
    "raw_articles": "CREATE TABLE {s}.raw_articles (id uuid NOT NULL, body text)",
}


def _asyncpg_dsn() -> tuple[str, dict]:
    """DATABASE_URL as asyncpg wants it.

    The repo's convention is a libpq-style URL with `?sslmode=require` (see
    src/shared/database.py: prepare_database_url), and asyncpg rejects that
    query parameter outright -- it would try to set it as a server runtime
    parameter. So it is popped here and handed to asyncpg as a real connect
    argument, which is what SQLAlchemy's asyncpg dialect does with it.
    """
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    query = dict(url.query)
    sslmode = query.pop("sslmode", "prefer")
    connect_args = {"ssl": sslmode} if sslmode != "disable" else {"ssl": False}
    # Rebuilt by hand rather than via str(url): SQLAlchemy renders the password
    # unescaped, and any credential with a URL-significant character in it
    # arrives as a different password.
    dsn = (
        f"postgresql://{urllib.parse.quote(url.username or 'postgres', safe='')}"
        f":{urllib.parse.quote(url.password or '', safe='')}"
        f"@{url.host}:{url.port or 5432}/{url.database}"
    )
    return dsn, connect_args


@pytest.fixture
async def scratch():
    """A scratch schema with RLS on, the migration applied, and the role usable.

    Mirrors the state 20261002220000 leaves dev in: RLS enabled, no policies.
    That is the state in which every grant on the role was inert, so the tests
    below would fail here if the migration only granted and never policied.
    """
    asyncpg = pytest.importorskip("asyncpg")
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("no DATABASE_URL: the role's behaviour is a Postgres question")
    dsn, connect_args = _asyncpg_dsn()
    try:
        conn = await asyncpg.connect(dsn, **connect_args)
    except Exception as exc:  # noqa: BLE001 - cause varies: refused, no server, bad password
        pytest.skip(f"cannot reach Postgres: {exc}")

    created_role = False
    granted_membership = False
    try:
        me = await conn.fetchval("SELECT current_user")
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", ROLE):
            try:
                await conn.execute(f"CREATE ROLE {ROLE} NOLOGIN")
                created_role = True
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"cannot create the {ROLE} role: {exc}")
        # Only grant membership if SET ROLE would not already work. Two traps
        # here, both paid for on dev:
        #   - pg_auth_members can hold a row for (me, role) that does not permit
        #     SET ROLE: PG 16+ rows carry inherit_option/set_option, and the
        #     durable row dev inherited from a pooled in-transaction grant has
        #     set_option = false. `EXISTS (pg_auth_members)` is therefore the
        #     wrong test; pg_has_role(..., 'USAGE') is the right one.
        #   - re-granting a membership that already has a row ADDS a second row
        #     (a different grantor), and that table is cluster-wide. A test that
        #     tidies up after itself has to notice it never dirtied anything;
        #     REVOKE removes only the caller's own row, so the fixture revokes
        #     exactly what it granted and nothing else.
        if not await conn.fetchval(
            "SELECT pg_has_role($1, $2, 'USAGE')", me, ROLE
        ):
            try:
                await conn.execute(f"GRANT {ROLE} TO {me}")
                granted_membership = True
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"cannot become {ROLE}: {exc}")

        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {SCHEMA}")
        for ddl in DDL.values():
            await conn.execute(ddl.format(s=SCHEMA))
        for table in TABLES:
            await conn.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
        # The migration names `public.` in its existence guards and bare table
        # names in its GRANT/REVOKE/POLICY statements, so the guards are
        # rewritten to the scratch schema and the bare names are resolved by
        # search_path. Without this the GRANTs would land on the real public
        # tables -- a test that silently edits the database it is auditing.
        await conn.execute(f"SET search_path = {SCHEMA}")
        await conn.execute(SQL.replace("public.", f"{SCHEMA}."))
        # The one thing the migration cannot do here: it grants USAGE on the
        # literal `public` schema, which every migration in this directory does
        # (the tables live there). In the scratch schema USAGE has to come from
        # the test, and it is the whole reason a table grant in a schema
        # without USAGE is unusable.
        await conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}")
        yield conn
    finally:
        try:
            # The role is still in force if a test set it: drop the schema as the
            # owner, or Postgres refuses with "must be owner of schema".
            await conn.execute("RESET ROLE")
            await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            if granted_membership:
                me = await conn.fetchval("SELECT current_user")
                await conn.execute(f"REVOKE {ROLE} FROM {me}")
        finally:
            if created_role:
                await conn.execute(f"DROP ROLE IF EXISTS {ROLE}")
            await conn.close()


async def _seed_log(conn, rows: int = 3) -> None:
    for i in range(rows):
        await conn.execute(
            f"INSERT INTO {SCHEMA}.merkle_log_entries (id, index, hash_scheme, timestamp, payload,"
            " canonical_payload, leaf_hash, chain_hash)"
            " VALUES (gen_random_uuid(), $1, 'n1:sha256', now(), $2::jsonb, $3, $4, $5)",
            i,
            f'{{"n": "{i}"}}',
            f'{{"n":"{i}"}}',
            f"{i:064x}",
            f"{i + 1:064x}",
        )


async def _as_role(conn, sql: str, *args):
    """Run one statement as the role, in a transaction that is always rolled back.

    Returns the rows on success, or the exception. Every probe is independent:
    a refusal aborts its transaction, and one refusal must not hide the answers
    to the other nine questions. asyncpg is in autocommit mode, so the
    transaction is opened and closed here rather than wrapping the whole test --
    which also means a probe never blocks another connection, and a seeded row
    is visible to a second connection (the fail-closed test needs exactly that).
    """
    await conn.execute("BEGIN")
    try:
        return await conn.fetch(sql.format(s=SCHEMA), *args)
    except Exception as exc:  # noqa: BLE001 - the refusal IS the answer
        return exc
    finally:
        await conn.execute("ROLLBACK")


async def _set_role(conn) -> None:
    await conn.execute(f"SET ROLE {ROLE}")


def _refused(result) -> bool:
    return isinstance(result, Exception)


class TestRoleOnPostgres:
    """What the role can do, measured as the role."""

    async def test_the_privilege_set_is_exactly_the_intended_one(self, scratch):
        """Every table, every privilege, both directions of drift.

        A grant that is not in EXPECTED is undeclared access to a public
        transparency log. An entry in EXPECTED that Postgres does not report is
        the bug this batch exists to fix, and it is invisible to any test that
        runs as the owner.
        """
        held: dict[str, set[str]] = {}
        for table, priv in await scratch.fetch(
            "SELECT c.relname, p.priv FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN LATERAL unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',"
            " 'REFERENCES','TRIGGER']) AS p(priv)"
            " WHERE n.nspname = $1 AND has_table_privilege($2, c.oid, p.priv)",
            SCHEMA,
            ROLE,
        ):
            held.setdefault(table, set()).add(priv)
        assert held == EXPECTED, f"grants drifted: {held}"

    async def test_the_role_has_no_reach_into_raw_articles(self, scratch):
        await _seed_log(scratch)
        await _set_role(scratch)
        assert _refused(await _as_role(scratch, "SELECT count(*) FROM {s}.raw_articles"))

    async def test_the_role_reads_the_whole_log(self, scratch):
        """The grant alone is not enough: without the policy this is 0 rows.

        This is the assertion that would have failed before the batch. The
        signer derives a root over every leaf, so "some rows" is not the
        requirement -- every row is.
        """
        await _seed_log(scratch)
        await _set_role(scratch)
        rows = await _as_role(scratch, "SELECT index FROM {s}.merkle_log_entries ORDER BY index")
        assert not _refused(rows), f"the role cannot read the log: {rows}"
        assert [r["index"] for r in rows] == [0, 1, 2]

    async def test_the_role_reads_published_checkpoints_and_alerts(self, scratch):
        """Both are needed by the code, not by the ticket.

        checkpoints: the chain, the go-backwards check, the equivocation check.
        alerts: the watchdog reads refusals through the same least-privilege DSN.
        """
        await scratch.execute(
            f"INSERT INTO {SCHEMA}.transparency_checkpoints (id, tree_size, merkle_root, chain_hash,"
            " timestamp, signature, algorithm, key_id) VALUES (gen_random_uuid(), 2, repeat('a',64),"
            " repeat('b',64), now(), 'sig', 'ed25519', 'k')"
        )
        await scratch.execute(
            f"INSERT INTO {SCHEMA}.transparency_alerts (id, kind, detail)"
            " VALUES (gen_random_uuid(), 'empty_log', '{}'::jsonb)"
        )
        await _set_role(scratch)
        checkpoints = await _as_role(scratch, "SELECT key_id FROM {s}.transparency_checkpoints")
        alerts = await _as_role(scratch, "SELECT kind FROM {s}.transparency_alerts")
        assert not _refused(checkpoints), f"cannot read checkpoints: {checkpoints}"
        assert not _refused(alerts), f"cannot read alerts: {alerts}"
        assert [r["key_id"] for r in checkpoints] == ["k"]

    async def test_the_role_publishes_a_checkpoint_and_records_an_alert(self, scratch):
        """The write half, as the role. Before the batch both were refused by RLS."""
        await _set_role(scratch)
        published = await _as_role(
            scratch,
            "INSERT INTO {s}.transparency_checkpoints (id, tree_size, merkle_root, chain_hash,"
            " timestamp, signature, algorithm, key_id, checkpoint_format, key_name, log_id)"
            " VALUES (gen_random_uuid(), 3, repeat('a',64), repeat('b',64), now(), 'sig', 'ed25519',"
            " 'k', 'c2sp-tlog-checkpoint-v2', 'procmon.dev/transparency', 'procmon.dev/transparency')",
        )
        assert not _refused(published), f"cannot publish: {published}"
        alert = await _as_role(
            scratch,
            "INSERT INTO {s}.transparency_alerts (id, kind, detail)"
            " VALUES (gen_random_uuid(), 'log_shrank', $1::jsonb)",
            '{"tree_size": 1}',
        )
        assert not _refused(alert), f"cannot record an alert: {alert}"

    @pytest.mark.parametrize(
        "label,sql",
        [
            ("edit a published checkpoint",
             "UPDATE {s}.transparency_checkpoints SET key_id = 'x' WHERE false"),
            ("delete a published checkpoint", "DELETE FROM {s}.transparency_checkpoints WHERE false"),
            ("truncate the log", "TRUNCATE {s}.merkle_log_entries"),
            ("append to the log",
             "INSERT INTO {s}.merkle_log_entries (id, index, hash_scheme, timestamp, payload,"
             " canonical_payload, leaf_hash, chain_hash) VALUES (gen_random_uuid(), 9, 'n1:sha256',"
             " now(), '{}'::jsonb, '{}', repeat('0',64), repeat('0',64))"),
            ("edit an alert", "UPDATE {s}.transparency_alerts SET kind = 'x' WHERE false"),
            ("delete the log", "DELETE FROM {s}.merkle_log_entries WHERE false"),
        ],
    )
    async def test_the_role_cannot_mutate_the_log(self, scratch, label, sql):
        await _seed_log(scratch)
        await _set_role(scratch)
        assert _refused(await _as_role(scratch, sql)), f"the role was allowed to {label}"

    async def test_an_empty_visible_log_is_refused_not_signed(self, scratch):
        """Fail-closed, with the visibility bug in place rather than imagined.

        Drop the policies and the role sees an empty log. The signer must refuse
        (`empty_log`) rather than publish a checkpoint over nothing: an empty
        root is a valid-looking artifact, and a signer that produced one on a
        log it simply cannot read would be indistinguishable from a signer
        working on an empty log.
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        from src.shared.database import prepare_database_url
        from src.transparency import signing
        from src.transparency.checkpoint import generate_ed25519_signer
        from src.transparency.log import SqlAlchemyMerkleLog

        await _seed_log(scratch)
        for policy in ("transparency_signer_read", "transparency_signer_insert"):
            await scratch.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.merkle_log_entries")
            await scratch.execute(f"DROP POLICY IF EXISTS {policy} ON {SCHEMA}.transparency_checkpoints")

        url, connect_args = prepare_database_url(os.environ["DATABASE_URL"])
        engine = create_async_engine(url, connect_args=connect_args)
        signer = generate_ed25519_signer(seed=b"fail-closed")
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await session.execute(text(f"SET search_path = {SCHEMA}"))
                await session.execute(text(f"GRANT {ROLE} TO CURRENT_USER"))
                await session.execute(text(f"SET ROLE {ROLE}"))
                result = await signing.sign_next_checkpoint(
                    session, SqlAlchemyMerkleLog(session), signer,
                    origin="procmon.dev/transparency", lock=False,
                )
                assert result.status == "refused"
                assert result.reason == signing.REFUSAL_EMPTY_LOG
                assert result.signed is None
                # Nothing was published on the way out.
                rows = await session.execute(
                    text(f"SELECT count(*) FROM {SCHEMA}.transparency_checkpoints")
                )
                assert rows.scalar_one() == 0
                await session.rollback()
        finally:
            await engine.dispose()
