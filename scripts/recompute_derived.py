#!/usr/bin/env python3
"""Recompute derived pipeline state for one stage, and everything downstream of it.

The pipeline is a chain of derived tables hanging off the immutable ``raw_articles`` layer. Until
now a bug fix in clustering or story assembly had no way to say which existing rows it should
apply to, so the only honest options were "do nothing" or "re-ingest everything". Every derived
row now carries the analyzer version that produced it and a hash of the input it was computed
from (see ``src/shared/analyzer_versions.py``), and this script turns those two columns into an
operation:

    audit (default)      read-only. Counts, per table, how many rows are current and how many are
                         stale -- stale meaning ``analyzer_version`` is NULL (written before this
                         bookkeeping existed, so unaccounted for) or is not one of the versions
                         that table's writer is allowed to emit.
    --only-stale         delete the stale rows of the stage and its downstream stages, then re-run
                         the stage. Rows already at the current version are left alone.
    --from-scratch       delete *every* row of the stage and its downstream stages, then re-run.
                         Use this when you doubt the current version's output entirely.

Deletion is ordered by the foreign keys themselves (a topological sort over the model metadata,
reversed), so a reference never outlives the row it points at, and it stops before touching anything
that a no-action foreign key still points into. ``--include-downstream`` re-runs the downstream
stages too, in dependency order, after the selected one.

raw_articles is never modified... except for one thing, and the script refuses to guess about it.
``raw_articles.reporting_unit_id`` is a pointer the dedupe stage *writes* -- it is not raw content,
it is the output of stage one -- and it is both a foreign key to ``reporting_units`` (so the rows
cannot be deleted while it dangles) and the very filter ``build_reporting_units`` uses to decide
what is still unclustered (so the stage cannot re-run until it is cleared). Recomputing ``dedupe``
is therefore impossible without clearing it, which is a write to a table this brief declares off
limits. Rather than quietly do it, ``--stage dedupe`` in a deleting mode requires
``--reset-raw-pointers``, and the run reports exactly how many pointers it cleared. The real fix is
a separate article<->unit mapping table that dedupe owns rather than a column on the raw layer;
that is tracked as a follow-up, not smuggled in here.

Usage:
    python -u scripts/recompute_derived.py --stage dedupe
    python -u scripts/recompute_derived.py --stage dedupe --from-scratch \\
        --reset-raw-pointers --yes
    python -u scripts/recompute_derived.py --stage cluster --only-stale --include-downstream --yes
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, or_, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.schema.models import Base, RawArticle  # noqa: E402
from src.shared.analyzer_versions import (  # noqa: E402
    STAGE_ORDER,
    downstream_stages,
    downstream_tables,
    expected_versions,
    resolve_stage,
    stage_version,
)
from src.shared.config import get_settings  # noqa: E402
from src.shared.database import prepare_database_url  # noqa: E402

logger = logging.getLogger("recompute_derived")


# =============================================================================
# Table -> model, and the staleness predicate.
# =============================================================================


def model_by_table() -> dict[str, type]:
    """Every mapped class keyed by its table name.

    Derived from the metadata rather than a hand-written list, so a table added to the models but
    forgotten here shows up as a missing key at import time rather than as a silent no-op delete.
    """
    return {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}


def delete_order(tables: Sequence[str]) -> list[str]:
    """`tables` ordered so a foreign key never outlives the row it points at.

    A topological sort over the model metadata, computed on this set of tables only, then reversed
    because children have to go before parents. Derived from the models rather than hand-listed, so
    a new foreign key cannot be introduced without the delete order following it.

    This is deliberately stricter than the declared ON DELETE rules: several of these FKs are
    CASCADE in the models but were created NO ACTION in the earliest migration, and an order that
    is safe only where the database agrees with the models is not an order.

    The sort is done here rather than with ``Base.metadata.sorted_tables`` because the full
    metadata has two FK cycles (events <-> event_geometries, raw_articles <-> reporting_units).
    That makes SQLAlchemy's sort warn and give up on those constraints. Restricted to the tables
    being deleted the cycles do not close -- the other side of each is outside the set -- and a
    cycle that *did* close would be a real "there is no valid order" answer, raised rather than
    papered over.
    """
    wanted = list(dict.fromkeys(tables))
    positions = {name: i for i, name in enumerate(wanted)}
    metadata = Base.metadata

    # "parents[name]" is every table inside this set that `name` references.
    parents: dict[str, set[str]] = {name: set() for name in wanted}
    for name in wanted:
        table = metadata.tables.get(name)
        if table is None:
            raise KeyError(f"{name!r} is not a mapped table")
        for fk in table.foreign_keys:
            target = fk.column.table.name
            if target in positions and target != name:
                parents[name].add(target)

    # Kahn's algorithm. Among tables that are equally free to go, keep the caller's order, so the
    # output is stable between runs and diffable in a test.
    remaining = list(wanted)
    ordered: list[str] = []
    while remaining:
        ready = [n for n in remaining if not (parents[n] - set(ordered))]
        if not ready:
            stuck = sorted(remaining)
            raise ValueError(
                f"cannot order deletes for {stuck}: they reference each other in a cycle"
            )
        chosen = min(ready, key=positions.__getitem__)
        ordered.append(chosen)
        remaining.remove(chosen)
    return list(reversed(ordered))


def stale_clause(table: str) -> Any:
    """SQL predicate selecting the rows of `table` that are not at a current analyzer_version.

    NULL is treated as stale on purpose: a NULL means the row predates this bookkeeping, so there
    is no evidence it was produced by the current analyzer. Claiming otherwise would let an entire
    pre-migration table masquerade as current forever. The explicit ``IS NULL`` arm is also what
    makes the predicate correct SQL: ``NOT IN`` evaluates to NULL for a NULL input, not to true, so
    without this arm those rows would fall through both branches and never be selected.
    """
    model = model_by_table()[table]
    return or_(
        model.analyzer_version.is_(None),
        model.analyzer_version.not_in(expected_versions(table)),
    )


@dataclass
class TableAudit:
    """What one derived table looks like right now, relative to the code that writes it."""

    table: str
    expected: tuple[str, ...]
    total: int
    current: int
    stale: int
    versions: dict[str, int] = field(default_factory=dict)


async def audit_table(session: AsyncSession, table: str) -> TableAudit:
    model = model_by_table()[table]
    expected = expected_versions(table)

    total = await session.scalar(select(func.count()).select_from(model))
    stale = await session.scalar(
        select(func.count()).select_from(model).where(stale_clause(table))
    )
    rows = await session.execute(
        select(model.analyzer_version, func.count())
        .select_from(model)
        .group_by(model.analyzer_version)
    )
    versions = {
        ("<null>" if v is None else v): n
        for v, n in rows.all()
    }
    total = int(total or 0)
    stale = int(stale or 0)
    return TableAudit(
        table=table,
        expected=expected,
        total=total,
        current=total - stale,
        stale=stale,
        versions=dict(sorted(versions.items())),
    )


async def audit(session: AsyncSession, tables: Sequence[str]) -> list[TableAudit]:
    return [await audit_table(session, t) for t in tables]


# =============================================================================
# Deletion.
# =============================================================================


@dataclass
class DeleteResult:
    table: str
    deleted: int
    mode: str


async def clear_raw_pointers(session: AsyncSession, table: str, only_stale: bool) -> int:
    """Unlink raw articles from the rows about to be deleted from `table`.

    Only meaningful for ``reporting_units``. Touches exactly two columns of ``raw_articles``, both
    of which the pipeline writes: ``reporting_unit_id`` (must be NULL or the FK blocks the delete,
    and must be NULL or ``build_reporting_units`` skips the article) and ``terminal_state`` where
    it holds a ``unit:``/``story:`` pointer. No raw content column is read or written here.

    Both updates are scoped to the article ids this delete is about to orphan, so an unrelated
    article whose terminal_state happens to be a dangling ``unit:`` pointer is left alone rather
    than silently reset to ``pending`` (which would put it back in front of the pipeline).
    """
    if table != "reporting_units":
        return 0

    unit_model = model_by_table()[table]
    stmt = select(unit_model.id)
    if only_stale:
        stmt = stmt.where(stale_clause(table))
    unit_ids = [row[0] for row in (await session.execute(stmt)).all()]
    if not unit_ids:
        return 0

    article_ids = [
        row[0]
        for row in (
            await session.execute(
                select(RawArticle.id).where(RawArticle.reporting_unit_id.in_(unit_ids))
            )
        ).all()
    ]
    if not article_ids:
        return 0

    await session.execute(
        update(RawArticle)
        .where(RawArticle.id.in_(article_ids))
        .values(reporting_unit_id=None)
    )
    # terminal_state is set by src/shared/ledger.py, whose docstring examples use unit:/story:
    # pointers. Nothing in the running pipeline sets them today, so this is normally a no-op; it
    # is here so that turning the ledger on later does not strand this script's deletes.
    await session.execute(
        update(RawArticle)
        .where(
            RawArticle.id.in_(article_ids),
            or_(
                RawArticle.terminal_state.like("unit:%"),
                RawArticle.terminal_state.like("story:%"),
            ),
        )
        .values(terminal_state="pending")
    )
    return len(article_ids)


@dataclass
class Blocker:
    """A table that is not being deleted but that references a table that is."""

    outside: str
    inside: str
    child_column: str
    rows: int


def reference_pairs(tables: Sequence[str]) -> list[tuple[str, str, str, str]]:
    """`(outside_table, inside_table, child_column, parent_column)` for FKs that would *block* a delete.

    A delete of `inside` fails while any row in `outside` still points at it, unless the database
    cascades or nulls the reference for us. Only NO ACTION references are returned here, because
    those are the ones where the rebuild has to stop and a human has to decide -- curation rows
    are not something a rebuild should quietly delete. Every other FK into these tables declares
    ON DELETE CASCADE or SET NULL in supabase/migrations, so the database handles it.

    Across the derived tables that leaves exactly one: ``curated_posts.story_id`` (see
    20260923000000_core_schema.sql). That is still the correct answer -- the foreign key is
    NO ACTION -- but note that nothing writes curated_posts any more and it holds zero rows
    on the dev project, so in practice it blocks nothing. It is reported rather than special-cased
    because the correct behaviour if a row ever does appear is to stop and ask, not to delete
    somebody's curation work. See DECISIONS.md. ``raw_articles.reporting_unit_id`` is the other
    NO ACTION reference in the whole graph; it is excluded because ``clear_raw_pointers`` handles
    it deliberately, under ``--reset-raw-pointers``, and counting it here would report every
    unclustered article as a blocker.
    """
    wanted = set(tables)
    for name in wanted:
        if name not in Base.metadata.tables:
            raise KeyError(f"{name!r} is not a mapped table")
    pairs: list[tuple[str, str, str, str]] = []
    for outside in sorted(Base.metadata.tables):
        if outside in wanted or outside == "raw_articles":
            continue
        for fk in Base.metadata.tables[outside].foreign_keys:
            inside = fk.column.table.name
            if inside in wanted and fk.ondelete is None:
                pairs.append((outside, inside, fk.parent.name, fk.column.name))
    return pairs


async def count_blockers(session: AsyncSession, tables: Sequence[str]) -> list[Blocker]:
    metadata = Base.metadata
    blockers: list[Blocker] = []
    for outside, inside, child_column, parent_column in reference_pairs(tables):
        outside_table = metadata.tables[outside]
        rows = await session.scalar(
            select(func.count())
            .select_from(outside_table)
            .where(
                outside_table.c[child_column].in_(
                    select(metadata.tables[inside].c[parent_column])
                )
            )
        )
        blockers.append(
            Blocker(outside=outside, inside=inside, child_column=child_column, rows=int(rows or 0))
        )
    return [b for b in blockers if b.rows]


async def delete_stage_rows(
    session: AsyncSession, tables: Sequence[str], only_stale: bool, reset_raw_pointers: bool
) -> list[DeleteResult]:
    """Delete the stage's rows in FK-safe order.

    raw_articles is deliberately not in `tables`; the only raw-layer writes happen inside
    clear_raw_pointers and only for reporting_units.
    """
    ordered = delete_order(tables)
    results: list[DeleteResult] = []
    for table in ordered:
        pointers_cleared = await clear_raw_pointers(session, table, only_stale)
        if pointers_cleared:
            logger.warning(
                "cleared %d raw_articles.reporting_unit_id pointers for %s", pointers_cleared, table
            )
        elif table == "reporting_units" and reset_raw_pointers:
            logger.warning("no raw_articles pointers referenced %s", table)

        model = model_by_table()[table]
        stmt = delete(model)
        if only_stale:
            stmt = stmt.where(stale_clause(table))
        result = await session.execute(stmt)
        results.append(
            DeleteResult(table=table, deleted=int(result.rowcount or 0), mode="stale" if only_stale else "all")
        )
    await session.commit()
    return results


# =============================================================================
# Re-running the stages themselves.
#
# Each entry point is the existing stage function, called exactly as the pipeline calls it. The
# imports are function-local on purpose: importing src.utils.ner pulls in spacy, and a dedupe
# rebuild should not pay for a model load it never calls.
# =============================================================================

Runner = Callable[..., Coroutine[Any, Any, Any]]

# Stages whose batch entry point takes a session factory rather than an open session, because it
# opens one session per story. They get the sessionmaker and manage their own sessions.
FACTORY_STAGES = {"embeddings", "topics", "narrative", "claims"}


async def _run_dedupe(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    from src.verification.units import build_reporting_units

    # build_reporting_units hardcodes its own 24h window over fetched_at, so a rebuild re-derives
    # the trailing day and cannot be widened from here without editing the stage.
    return await build_reporting_units(session)


async def _run_cluster(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    # No standalone entry point exists: canonical_entities and entity_aliases are written by
    # resolve_entities_to_canonical() as build_stories resolves each unit's entities. Re-running
    # the stories stage is therefore the only way to re-derive them, and since a dedupe rebuild
    # leaves every unit unlinked, build_stories does visit all of them.
    from src.verification.stories import build_stories

    return await build_stories(session)


async def _run_stories(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    from src.verification.stories import build_stories

    return await build_stories(session)


async def _run_geocode(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    # backfill_events opens its own session from the shared engine (src/shared/database.py), not
    # the one handed in, so the session argument is deliberately unused here.
    from scripts.backfill_globe_events import backfill_events

    return await backfill_events(dry_run=False)


async def _run_embeddings(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    from src.enrichment.pipeline import enrich_recent_stories

    return await enrich_recent_stories(
        factory, hours_back=hours_back, max_stories=max_stories
    )


async def _run_topics(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    from src.verification.topics import assign_topic_groups_for_recent_stories

    return await assign_topic_groups_for_recent_stories(
        factory, hours_back=hours_back, max_stories=max_stories
    )


async def _run_narrative(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    from src.verification.narrative import link_narrative_arcs_for_recent_stories

    return await link_narrative_arcs_for_recent_stories(
        factory, hours_back=hours_back, max_stories=max_stories
    )


async def _run_claims(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    from src.verification.claims import extract_claims_for_recent_stories

    return await extract_claims_for_recent_stories(
        factory, hours_back=hours_back, max_stories=max_stories
    )


async def _run_reliability(session: AsyncSession, factory: Any, hours_back: int, max_stories: int) -> Any:
    from src.reliability.consensus_analyzer import compute_daily_reliability_snapshots

    return await compute_daily_reliability_snapshots(session)


RUNNERS: dict[str, tuple[str, Runner]] = {
    "dedupe": ("build_reporting_units", _run_dedupe),
    "cluster": ("build_stories", _run_cluster),
    "stories": ("build_stories", _run_stories),
    "geocode": ("backfill_globe_events.backfill_events", _run_geocode),
    "embeddings": ("enrich_recent_stories", _run_embeddings),
    "topics": ("assign_topic_groups_for_recent_stories", _run_topics),
    "narrative": ("link_narrative_arcs_for_recent_stories", _run_narrative),
    "claims": ("extract_claims_for_recent_stories", _run_claims),
    "reliability": ("compute_daily_reliability_snapshots", _run_reliability),
}


def summarize(result: Any) -> str:
    """One line describing what a stage run produced, for a stage that returns anything at all."""
    if result is None:
        return "no result"
    if isinstance(result, bool):
        return str(result)
    if isinstance(result, int):
        return f"{result}"
    if isinstance(result, list):
        return f"{len(result)} items"
    if isinstance(result, dict):
        return f"{len(result)} keys"
    return str(result)


# =============================================================================
# Entry point.
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stage",
        required=True,
        help=f"stage to recompute; one of {', '.join(STAGE_ORDER)} (common aliases accepted)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--only-stale",
        action="store_true",
        help="delete only rows whose analyzer_version is not current, then re-run the stage",
    )
    mode.add_argument(
        "--from-scratch",
        action="store_true",
        help="delete every row of the stage and its downstream stages, then re-run the stage",
    )
    parser.add_argument(
        "--include-downstream",
        action="store_true",
        help="also re-run the downstream stages, in dependency order",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm a destructive run; without it the script audits and exits",
    )
    parser.add_argument(
        "--reset-raw-pointers",
        action="store_true",
        help="allow clearing raw_articles.reporting_unit_id (required by --stage dedupe deletes)",
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=168,
        help="look-back window for the batch stages (default: 168)",
    )
    parser.add_argument(
        "--max-stories",
        type=int,
        default=100,
        help="story cap for the batch stages (default: 100)",
    )
    return parser


def print_audit(stage: str, tables: Sequence[str], reports: Sequence[TableAudit]) -> None:
    print(f"stage {stage!r} (analyzer_version {stage_version(stage)})")
    print(f"  {'table':<32} {'total':>7} {'current':>8} {'stale':>7}  analyzer_version counts")
    for r in reports:
        counts = ", ".join(f"{v}={n}" for v, n in r.versions.items()) or "-"
        if len(r.expected) > 1:
            counts += f"  (current: {', '.join(r.expected)})"
        print(f"  {r.table:<32} {r.total:>7} {r.current:>8} {r.stale:>7}  {counts}")
    stale_total = sum(r.stale for r in reports)
    print(f"  {'TOTAL':<32} {sum(r.total for r in reports):>7} "
          f"{sum(r.current for r in reports):>8} {stale_total:>7}")


async def run(args: argparse.Namespace) -> int:
    stage = resolve_stage(args.stage)
    deleting = args.only_stale or args.from_scratch
    tables = downstream_tables(stage)

    settings = get_settings()
    if not settings.has_database:
        print("ERROR: DATABASE_URL is not configured", file=sys.stderr)
        return 1

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    url, connect_args = prepare_database_url(settings.database_url)
    engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as session:
            before = await audit(session, tables)
        print_audit(stage, tables, before)

        if not deleting:
            print(
                "\naudit only. Re-run with --only-stale to drop the stale rows, or "
                "--from-scratch to drop all of them, plus --yes to confirm."
            )
            return 0

        if not args.yes:
            print(
                f"\nrefusing to delete without --yes "
                f"(would touch {len(tables)} table(s) in {stage!r})",
                file=sys.stderr,
            )
            return 2

        if stage == "dedupe" and not args.reset_raw_pointers:
            print(
                "ERROR: recomputing 'dedupe' has to clear raw_articles.reporting_unit_id, "
                "because build_reporting_units both filters on it and is blocked by its "
                "foreign key. Pass --reset-raw-pointers to allow that, or recompute a "
                "downstream stage instead.",
                file=sys.stderr,
            )
            return 2

        only_stale = args.only_stale
        async with factory() as session:
            blockers = await count_blockers(session, tables)
        if blockers:
            print("\nrefusing to delete: rows outside this stage still reference it", file=sys.stderr)
            for b in blockers:
                print(f"  {b.outside}.{b.child_column} -> {b.inside}: {b.rows} row(s)", file=sys.stderr)
            print(
                "  these are curation/provenance rows, not derived state; clear or reassign them "
                "first. raw_articles.reporting_unit_id is excluded here and handled by "
                "--reset-raw-pointers.",
                file=sys.stderr,
            )
            return 3

        print(
            f"\ndeleting ({'stale rows only' if only_stale else 'all rows'}), "
            f"in FK-safe order:"
        )
        async with factory() as session:
            deleted = await delete_stage_rows(
                session, tables, only_stale, args.reset_raw_pointers
            )
        for d in deleted:
            print(f"  {d.table:<32} deleted {d.deleted}")

        if only_stale and sum(d.deleted for d in deleted) == 0:
            print("\nnothing stale; re-running the stage anyway is a no-op you did not ask for.")
            return 0

        order = (stage, *downstream_stages(stage)) if args.include_downstream else (stage,)
        print(f"\nre-running: {', '.join(order)}")
        for key in order:
            label, runner = RUNNERS[key]
            print(f"  {key} -> {label}")
            async with factory() as session:
                result = await runner(session, factory, args.hours_back, args.max_stories)
                await session.commit()
            print(f"    {summarize(result)}")

        async with factory() as session:
            after = await audit(session, tables)
        print()
        print_audit(stage, tables, after)
        remaining = sum(r.stale for r in after)
        if remaining:
            print(f"\n{remaining} row(s) still stale after the run.")
        return 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.max_stories <= 0 or args.hours_back <= 0:
        print("ERROR: --hours-back and --max-stories must be positive", file=sys.stderr)
        return 1
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())