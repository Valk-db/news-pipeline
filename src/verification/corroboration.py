"""Corroboration evidence with wire copies collapsed: the one place owners are counted.

The corroboration gate asks one question -- *how many independent reporting organizations back
this story?* -- and answers it by counting ownership groups. Two things made that count wrong,
and both are fixed here, once, in this module:

1. **Wire copies counted as independent outlets.** `src/ingestion/run.py` deliberately lets
   syndicated copies coexist in the database (same `content_hash`, different publishers), because
   the fact that five papers carried a story is evidence worth keeping. But nothing ever told the
   gate that those five rows are one piece of reporting: five papers republishing one AP story
   counted as five corroborating outlets, and the gate passed on them. The rule here is purely
   mechanical -- *this article's `content_hash` matches an article whose `source_domain` is a
   `SourceCategory.WIRE_SERVICE` source in `src/ingestion/source_registry.py`, so this article is
   a copy of that wire's reporting and is attributed to the wire's owner group instead of its own.*
   No byline parsing, no similarity model, no NLP: content hash plus a registry lookup, so the
   result is explainable in a sentence and reproducible in a test.

   The registry's wire sources are currently **disabled** (`enabled=False` for apnews.com and
   reuters.com; RSS pulled 2026-09-21, GDELT disabled). So in today's data there is usually no
   wire-origin row to match, and no collapse happens. The mechanism is live and correct; it is
   waiting for wire ingestion. That limitation is stated here rather than papered over.

2. **Two places counting, one answer.** `recompute_story_counters()` persists `distinct_owners`
   and the gate's own unit walk builds the `(unit_id, owner)` pairs `evaluate_tier1_gate()` decides
   on. When two places count independently they drift, and a decision cannot be explained by the
   counter printed beside it. `load_corroboration()` is the single resolution both paths call,
   and the `Corroboration` object it returns is the only thing either of them counts from.

What is deliberately *not* here: any change to the thresholds. Collapsing wire copies is the only
behavior change, and it reaches both gates' arithmetic through the one number this module owns:
`evaluate_tier1_gate` decides on the post-collapse pairs, and `compute_admission_score`'s
independence factors read the post-collapse `distinct_owners` — because with the dynamic gate
enabled the score *is* the admission decision, so a score counting collapsed copies as
corroboration would admit what the boolean gate holds out. `tier1_unit_count` still counts tier-1
articles and stays the story's raw reporting volume; it is not a count of independent outlets, and
nothing that measures independence is built on it.

Collapsing is also strictly a *narrowing* of what counts as independent. A story that lost
owners loses corroboration, never gains it, so this can only block stories that were passing on
syndication alone.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.ingestion.source_registry import ALL_SOURCES, SourceCategory
from src.schema.models import RawArticle, SourceTier
from src.verification.units import get_owner_group


# Every source the registry files under WIRE_SERVICE. Read from the registry rather than
# restated, so enabling a wire feed is all it takes for collapsing to apply to it.
WIRE_SERVICE_DOMAINS: frozenset[str] = frozenset(
    config.domain
    for config in ALL_SOURCES.values()
    if config.category is SourceCategory.WIRE_SERVICE
)

# Wire domain -> the owner group a copy of its reporting is attributed to. Resolved through
# OWNERSHIP_GROUPS rather than invented here, so a copy of an AP story is attributed to the same
# group name ("AP") that an article fetched straight from apnews.com counts as, and the
# histogram and the collapse note cannot use two spellings for one group.
WIRE_SERVICE_OWNERS: dict[str, str] = {
    domain: get_owner_group(domain) for domain in sorted(WIRE_SERVICE_DOMAINS)
}

# Ordering for serialized evidence: tier-1 first, because that is what the gate counts on, then
# domain, url and id so the same story always serializes the same way and two runs of the gate
# over unchanged input produce byte-identical rows.
_TIER_RANK = {
    SourceTier.TIER1.value: 1,
    SourceTier.TIER2.value: 2,
    SourceTier.TIER3.value: 3,
    SourceTier.TIER4.value: 4,
}


@dataclass(frozen=True)
class UnitRef:
    """What the resolver needs to know about one reporting unit.

    Carries the unit's stored `tier1_owner_groups` as a *fallback*, used only for a unit whose
    member rows cannot be resolved at all (every member predating `reporting_unit_id`, and the
    representative row gone too). Those owners are already collapsed -- they came out of
    `build_reporting_units()` via `get_owner_group()` -- so the fallback is conservative in the
    safe direction: it can never invent independence, only decline to subtract it.
    """

    unit_id: uuid.UUID
    representative_article_id: uuid.UUID | None = None
    tier1_owner_groups: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, unit) -> "UnitRef":
        """Build a ref from a ReportingUnit row."""
        return cls(
            unit_id=unit.id,
            representative_article_id=unit.representative_article_id,
            tier1_owner_groups=dict(unit.tier1_owner_groups or {}),
        )


@dataclass(frozen=True)
class ArticleEvidence:
    """One article behind a decision, with the owner the gate counted it under."""

    article_id: uuid.UUID
    unit_id: uuid.UUID | None
    url: str
    source_domain: str
    tier: str
    content_hash: str | None
    wire_origin: str | None  # wire-service domain this copy came from; None if not a copy
    owner: str  # effective owner group, post-collapse

    @property
    def is_tier1(self) -> bool:
        return self.tier == SourceTier.TIER1.value

    @property
    def is_wire_copy(self) -> bool:
        return self.wire_origin is not None

    def as_json(self) -> dict:
        """The row shape stored in `gate_decisions.contributing_articles`."""
        return {
            "article_id": str(self.article_id),
            "url": self.url,
            "source_domain": self.source_domain,
            "tier": self.tier,
            "wire_origin": self.wire_origin,
            "content_hash": self.content_hash,
        }

    @property
    def sort_key(self) -> tuple:
        return (
            _TIER_RANK.get(self.tier, 99),
            self.source_domain or "",
            self.url or "",
            str(self.article_id),
        )


@dataclass(frozen=True)
class Corroboration:
    """Every article the gate counted over, grouped by the unit that carries it.

    Immutable and reusable: one load per gate run serves the counter recompute, the gate's own
    unit walk and the recorded decision, which is what keeps the three from disagreeing.
    """

    by_unit: Mapping[uuid.UUID, tuple[ArticleEvidence, ...]] = field(default_factory=dict)
    refs: tuple[UnitRef, ...] = ()

    @classmethod
    def empty(cls, refs: Sequence[UnitRef] = ()) -> "Corroboration":
        return cls(by_unit={}, refs=tuple(refs))

    # -- accessors -------------------------------------------------------------------

    def articles(self, units: Iterable) -> list[ArticleEvidence]:
        """Every resolved article of `units`, deterministically ordered."""
        return sorted(
            (article for unit in units for article in self.by_unit.get(unit_id_of(unit), ())),
            key=lambda article: article.sort_key,
        )

    def wire_copies(self, units: Iterable) -> list[ArticleEvidence]:
        """Articles whose corroboration was attributed to a wire service."""
        return [a for a in self.articles(units) if a.is_wire_copy]

    def _tier1_owners(self, units: Iterable) -> list[str]:
        """One owner group per tier-1 article, post-collapse.

        The single count both the histogram and the gate's pair list are built from, so
        `distinct_owners` and the rule `evaluate_tier1_gate` applies cannot disagree.

        A unit whose member rows resolve to no tier-1 article falls back to the histogram
        `build_reporting_units()` stored for it: subtracting corroboration we cannot see would
        drop owners that are really there. Those stored owners are already collapsed (units.py
        resolves them through `get_owner_group()`), so the fallback is conservative in the safe
        direction -- it can never invent independence, only decline to subtract it.
        """
        units = list(units)
        owners: list[str] = []
        unresolved: set[uuid.UUID] = set()
        for unit in units:
            unit_id = unit_id_of(unit)
            resolved = [
                article.owner
                for article in self.by_unit.get(unit_id, ())
                if article.is_tier1
            ]
            if resolved:
                owners.extend(resolved)
            else:
                unresolved.add(unit_id)

        for ref in self.refs:
            if ref.unit_id not in unresolved or not ref.tier1_owner_groups:
                continue
            for owner, count in ref.tier1_owner_groups.items():
                owners.extend([owner] * int(count))
        return owners

    def owner_histogram(self, units: Iterable) -> dict[str, int]:
        """Owner group -> number of tier-1 articles attributed to it, post-collapse.

        The histogram the gate counted: `len(...)` is what `distinct_owners` means everywhere in
        this codebase, including the column stored on the story row.
        """
        histogram = Counter(self._tier1_owners(units))
        return {owner: histogram[owner] for owner in sorted(histogram)}

    def distinct_owners(self, units: Iterable) -> int:
        return len(self.owner_histogram(units))

    def tier1_pairs(self, units: Iterable) -> list[tuple[uuid.UUID, str]]:
        """The `(unit_id, owner)` pairs `evaluate_tier1_gate()` counts: one per (unit, owner).

        One pair per distinct tier-1 owner of each unit, post wire-collapse -- never one per
        article. A reporting unit is one reporting event plus its syndications, so two tier-1
        articles inside one unit are one piece of reporting however many outlets reprinted it;
        collapsing to one pair per (unit, owner) is what keeps a single syndicated event from
        clearing the ">= 2 tier-1 units" bar by itself. The gate counts distinct units and
        distinct owners off this list.
        """
        return self._tier1_unit_owners(units)

    def _tier1_unit_owners(self, units: Iterable) -> list[tuple[uuid.UUID, str]]:
        """One `(unit_id, owner)` per distinct tier-1 owner of each unit, post-collapse.

        The gate's unit walk, next to `_tier1_owners`' per-article list that feeds the histogram:
        the two answer different questions (how many units back this vs. how many articles
        attributed to each owner) and are kept as separate code on purpose. The unresolved-unit
        fallback mirrors `_tier1_owners`' -- the stored histogram `build_reporting_units()` wrote,
        already collapsed -- but contributes one pair per stored owner: the unit still counts as
        one unit, never as many as its stored article count.
        """
        units = list(units)
        pairs: list[tuple[uuid.UUID, str]] = []
        unresolved: set[uuid.UUID] = set()
        for unit in units:
            unit_id = unit_id_of(unit)
            owners = {
                article.owner
                for article in self.by_unit.get(unit_id, ())
                if article.is_tier1
            }
            if owners:
                pairs.extend((unit_id, owner) for owner in sorted(owners))
            else:
                unresolved.add(unit_id)

        for ref in self.refs:
            if ref.unit_id not in unresolved or not ref.tier1_owner_groups:
                continue
            pairs.extend(
                (ref.unit_id, owner) for owner in sorted(ref.tier1_owner_groups)
            )
        return pairs

    def tier1_article_count(self, units: Iterable) -> int:
        return len(self._tier1_owners(units))

    # -- the sentence ----------------------------------------------------------------

    def collapse_note(self, units: Iterable) -> str:
        """Human-readable summary of what was collapsed, e.g. "3 wire copies collapsed to AP".

        Empty when nothing collapsed, which is the normal case while wire feeds are disabled --
        the gate then has nothing extra to say, rather than a clause that always reads zero.
        """
        counts: Counter[str] = Counter()
        units = list(units)
        for article in self.wire_copies(units):
            counts[article.wire_origin] += 1
        if not counts:
            return ""
        parts = [
            f"{count} wire {'copy' if count == 1 else 'copies'} collapsed to "
            f"{WIRE_SERVICE_OWNERS.get(domain, domain)}"
            for domain, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return "; ".join(parts)

    def with_note(self, reason: str, units: Iterable) -> str:
        """`reason` with the collapse note appended when there is one."""
        note = self.collapse_note(units)
        return f"{reason} ({note})" if note else reason


# =====================================================================================
# Resolution
# =====================================================================================


def effective_owner(source_domain: str | None, wire_origin: str | None) -> str:
    """The owner group an article is counted under.

    A wire copy belongs to the wire service that reported it; anything else belongs to whoever
    published it. Pure, so a test can state the rule in one line.
    """
    if wire_origin:
        return WIRE_SERVICE_OWNERS.get(wire_origin) or get_owner_group(wire_origin)
    return get_owner_group(source_domain or "")


def unit_id_of(unit) -> uuid.UUID:
    """The id of a unit, whether it arrived as a ReportingUnit row or as a UnitRef.

    The gate's callers hold ORM units; `recompute_story_counters()` only has the id columns it
    selected, so it passes refs. Both are units to this module.
    """
    return unit.unit_id if isinstance(unit, UnitRef) else unit.id


def _tier_value(tier) -> str:
    """Normalize a source_tier that may arrive as an enum or as its stored string."""
    if isinstance(tier, SourceTier):
        return tier.value
    return str(tier).strip().lower() if tier else SourceTier.TIER3.value


async def resolve_wire_origins(
    session: AsyncSession, content_hashes: Sequence[str]
) -> dict[str, str]:
    """Map each content hash to the wire-service domain that published that same content.

    One query for the whole run. The wire rows may live in any unit (or in none -- an AP feed row
    that never clustered), so this deliberately looks outside the units being counted: what makes
    an article a wire copy is that the same content exists under a wire domain, nothing more.

    Deterministic: when several wire services carried identical bytes, the lexicographically
    first domain wins, so the same input always produces the same attribution.
    """
    hashes = sorted({h for h in content_hashes if h})
    if not hashes:
        return {}

    stmt = select(RawArticle.content_hash, RawArticle.source_domain).where(
        RawArticle.content_hash.in_(hashes),
        RawArticle.source_domain.in_(WIRE_SERVICE_DOMAINS),
    )
    result = await session.execute(stmt)

    origins: dict[str, str] = {}
    for content_hash, domain in sorted(
        (row for row in result.all() if row[0] and row[1]),
        key=lambda row: (row[0], row[1]),
    ):
        origins.setdefault(content_hash, domain)
    return origins


async def load_corroboration(
    session: AsyncSession, refs: Sequence[UnitRef]
) -> Corroboration:
    """Resolve every article behind `refs`, with wire copies already attributed to their wire.

    Two queries for the whole run, both batched across all units rather than per unit:

    1. the articles of the units, matched by `reporting_unit_id` and additionally by
       representative id -- membership is recorded in `reporting_unit_id`, and a member row
       written before that column existed would otherwise leave a unit looking as though it cited
       nothing. Matching a representative can only add the article its own unit stands for, never
       a different unit's.
    2. the wire origins for the content hashes those articles carry.

    Articles arrive in tier/domain/url/id order, so a row written twice for an unchanged story
    serializes identically both times.
    """
    refs = tuple(refs)
    unit_ids = {ref.unit_id for ref in refs}
    representative_ids = [ref.representative_article_id for ref in refs if ref.representative_article_id]
    if not unit_ids:
        return Corroboration.empty(refs)

    article_stmt = select(
        RawArticle.id,
        RawArticle.reporting_unit_id,
        RawArticle.url,
        RawArticle.source_domain,
        RawArticle.source_tier,
        RawArticle.content_hash,
    ).where(
        or_(
            RawArticle.reporting_unit_id.in_(unit_ids),
            RawArticle.id.in_(representative_ids) if representative_ids else False,
        )
    )
    rows = (await session.execute(article_stmt)).all()

    representative_owner = {
        ref.representative_article_id: ref.unit_id
        for ref in refs
        if ref.representative_article_id
    }
    origins = await resolve_wire_origins(
        session, [row[5] for row in rows if row[5]]
    )

    grouped: defaultdict[uuid.UUID, list[ArticleEvidence]] = defaultdict(list)
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for article_id, unit_id, url, source_domain, tier, content_hash in rows:
        owning_unit = unit_id if unit_id in unit_ids else representative_owner.get(article_id)
        if owning_unit is None:
            continue
        if (owning_unit, article_id) in seen:
            continue
        seen.add((owning_unit, article_id))
        # An article published *by* the wire is the origin, not a copy of it, so it keeps its own
        # owner: the collapse must be a no-op on the original rather than relabel it.
        wire_origin = origins.get(content_hash)
        if wire_origin == source_domain:
            wire_origin = None
        grouped[owning_unit].append(
            ArticleEvidence(
                article_id=article_id,
                unit_id=owning_unit,
                url=url,
                source_domain=source_domain,
                tier=_tier_value(tier),
                content_hash=content_hash,
                wire_origin=wire_origin,
                owner=effective_owner(source_domain, wire_origin),
            )
        )

    return Corroboration(
        by_unit={unit_id: tuple(sorted(items, key=lambda a: a.sort_key)) for unit_id, items in grouped.items()},
        refs=refs,
    )
