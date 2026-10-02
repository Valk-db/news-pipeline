"""Regression tests for the entity-resolution / dedupe P1 fixes (skeptic audit V-P1-3..7).

Each class pins one finding so the defect cannot silently return:
- V-P1-5: resolve() falsely merged distinct people via the substring heuristic
- V-P1-6: _generate_aliases promoted bare common nouns to ORG aliases
- V-P1-7: session-less EntityCanonicalizer minted a fresh UUID per mention
- V-P1-3: containment_from_jaccard was unclamped (J=0.9, sizes (100,10) -> 5.211)
- V-P1-4: union-find on one-sided containment chained aggregator+briefs into one unit
"""

import pytest

from src.utils.minhash_utils import (
    containment_from_jaccard,
    compute_containment,
    cluster_articles_by_containment,
    shingle_text,
)
from src.utils.ner import EntityCanonicalizer


def _person_entity(name, alias=None):
    """Build a mock CanonicalEntity row for a person."""
    from unittest.mock import MagicMock

    entity = MagicMock()
    entity.id = f"id-{name.replace(' ', '-').lower()}"
    entity.canonical_name = name
    entity.entity_type = "PERSON"
    alias_row = MagicMock()
    alias_row.alias = alias or name
    entity.aliases = [alias_row]
    return entity


def _mock_session_with_people(*people):
    """AsyncMock session whose initialize() loads the given person entities."""
    from unittest.mock import AsyncMock, MagicMock

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = list(people)
    mock_session.execute.return_value = mock_result
    return mock_session


class TestV_P1_5_NoFalsePersonMerges:
    """'Harris Smith' must not resolve to Kamala Harris; 'Andrew Trump' to nobody."""

    @pytest.mark.asyncio
    async def test_harris_smith_does_not_merge_into_kamala_harris(self):
        session = _mock_session_with_people(_person_entity("Kamala Harris"))
        canon = EntityCanonicalizer(session)
        await canon.initialize()

        # resolve() declines: a two-token name is never matched on surname alone
        assert canon.resolve("Harris Smith", "PERSON") is None

        # get_or_create() therefore mints a distinct entity, not a merge
        mention = await canon.get_or_create("Harris Smith", "PERSON")
        assert mention.canonical_name == "Harris Smith"
        assert mention.canonical_id != "id-kamala-harris"
        assert mention.confidence == 1.0

    @pytest.mark.asyncio
    async def test_andrew_trump_does_not_merge_into_donald_trump(self):
        session = _mock_session_with_people(_person_entity("Donald Trump"))
        canon = EntityCanonicalizer(session)
        await canon.initialize()

        assert canon.resolve("Andrew Trump", "PERSON") is None

        mention = await canon.get_or_create("Andrew Trump", "PERSON")
        assert mention.canonical_name == "Andrew Trump"
        assert mention.canonical_id != "id-donald-trump"

    @pytest.mark.asyncio
    async def test_bare_surname_resolves_with_reduced_confidence(self):
        """A bare, unambiguous surname still reaches its person, but at 0.8 --
        threaded through get_or_create so a fuzzy match is distinguishable
        from a 1.0 exact match downstream. (Uses a name outside the curated
        seed table, so the surname-fallback path -- not a seed hit -- fires.)"""
        session = _mock_session_with_people(_person_entity("Andy Burnham"))
        canon = EntityCanonicalizer(session)
        await canon.initialize()

        resolved = canon.resolve("Burnham", "PERSON")
        assert resolved is not None
        assert resolved.canonical_name == "Andy Burnham"
        assert resolved.confidence == 0.8

        mention = await canon.get_or_create("Burnham", "PERSON")
        assert mention.canonical_id == "id-andy-burnham"
        assert mention.confidence == 0.8
        # No new entity minted for a resolved mention
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_seed_surname_hit_is_exact_confidence(self):
        """Seed-table hits ('Trump' -> 'Donald Trump') stay at confidence 1.0,
        distinct from the 0.8 surname-fallback path above."""
        session = _mock_session_with_people(_person_entity("Donald Trump"))
        canon = EntityCanonicalizer(session)
        await canon.initialize()

        mention = await canon.get_or_create("Trump", "PERSON")
        assert mention.canonical_name == "Donald Trump"
        assert mention.confidence == 1.0


class TestV_P1_6_NoCommonNounAliases:
    """Minting 'Federal Bureau of Investigation' must not also mint
    'Bureau' (or 'Federal', 'Investigation') as ORG aliases."""

    @pytest.mark.asyncio
    async def test_only_the_surface_form_becomes_an_alias(self):
        from unittest.mock import AsyncMock, MagicMock

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        canon = EntityCanonicalizer(mock_session)
        await canon.initialize()

        mention = await canon.get_or_create("Federal Bureau of Investigation", "ORG")
        assert mention.canonical_name == "Federal Bureau of Investigation"

        added = [call.args[0] for call in mock_session.add.call_args_list]
        aliases = [obj for obj in added if hasattr(obj, "alias")]
        # Exactly one alias row: the entity itself plus its one naming alias.
        assert mock_session.add.call_count == 2
        assert len(aliases) == 1
        assert aliases[0].alias == "Federal Bureau of Investigation"
        assert "Bureau" not in {a.alias for a in aliases}


class TestV_P1_7_NoSessionRaises:
    """Without a DB session the canonicalizer must fail loudly instead of
    minting a fresh UUID per mention (which silently split every story)."""

    @pytest.mark.asyncio
    async def test_initialize_without_session_raises(self):
        canon = EntityCanonicalizer(session=None)
        with pytest.raises(RuntimeError, match="database session"):
            await canon.initialize()

    @pytest.mark.asyncio
    async def test_get_or_create_without_session_raises(self):
        canon = EntityCanonicalizer(session=None)
        with pytest.raises(RuntimeError, match="database session"):
            await canon.get_or_create("Some Entity", "ORG")


class TestV_P1_3_ContainmentBounded:
    def test_audit_example_refused(self):
        """The audit's exact numbers: J=0.9 with sizes (100, 10) inverts to an
        implied intersection of 52.1 against a smaller set of 10 -- incoherent,
        so the pair is refused (0.0) instead of merging at 5.211."""
        assert containment_from_jaccard(0.9, 100, 10) == 0.0

    def test_slight_overshoot_clamped_not_refused(self):
        """Estimator noise that pushes a genuine near-duplicate a hair past
        the set-algebra cap still merges (clamped to 1.0), not refused."""
        # sizes (100, 95), J=0.955 -> implied intersection 95.24 vs min 95:
        # overshoot 0.24, well within 3 sigma of the estimate (~2.8)
        result = containment_from_jaccard(0.955, 100, 95)
        assert result == 1.0

    def test_result_never_exceeds_one(self):
        for jaccard, a, b in [(0.99, 50, 50), (0.999, 1000, 999), (0.75, 10, 12)]:
            assert 0.0 <= containment_from_jaccard(jaccard, a, b) <= 1.0

    def test_degenerate_sizes(self):
        assert containment_from_jaccard(0.5, 0, 10) == 0.0
        assert containment_from_jaccard(0.5, 10, 0) == 0.0

    def test_exact_compare_for_short_texts(self):
        """Short token sets skip MinHash entirely: exact, deterministic."""
        assert compute_containment({"a", "b", "c"}, {"a", "b", "c"}) == 1.0
        assert compute_containment({"a", "b", "c"}, {"x", "y", "z"}) == 0.0
        # 2 of 3 shared -> 2/3
        assert abs(compute_containment({"a", "b", "c"}, {"b", "c", "d"}) - 2 / 3) < 1e-9


class TestV_P1_4_NoTransitiveChaining:
    def test_aggregator_does_not_chain_unrelated_briefs(self):
        """One long aggregator 90%-contains two short briefs that share
        nothing with each other. One-sided containment unioned all three;
        the bidirectional predicate must yield three units."""
        long_tokens = {f"tok{i}" for i in range(1000)}
        brief1 = {f"tok{i}" for i in range(9)} | {"brief1-unique"}
        brief2 = {f"tok{i}" for i in range(100, 109)} | {"brief2-unique"}
        assert len(brief1 & brief2) == 0  # the briefs are unrelated to each other

        clusters = cluster_articles_by_containment(
            [("long", long_tokens), ("b1", brief1), ("b2", brief2)],
            threshold=0.9,
        )
        assert len(clusters) == 3

    def test_true_syndication_still_merges(self):
        """Two near-identical wire copies (490/500 shingles shared) must
        still land in one unit: high containment in both directions."""
        a = {f"w{i}" for i in range(500)}
        b = {f"w{i}" for i in range(490)} | {f"v{i}" for i in range(10)}

        clusters = cluster_articles_by_containment(
            [("a", a), ("b", b)], threshold=0.9
        )
        assert len(clusters) == 1
        assert set(clusters[0]) == {"a", "b"}

    def test_exact_duplicates_still_merge(self):
        articles = [
            ("1", shingle_text("Exact same article content here")),
            ("2", shingle_text("Exact same article content here")),
            ("3", shingle_text("Different article entirely")),
        ]
        clusters = cluster_articles_by_containment(articles, threshold=0.9)
        assert len(clusters) == 2
        large = next(c for c in clusters if len(c) == 2)
        assert set(large) == {"1", "2"}
