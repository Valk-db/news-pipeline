"""Tests for entity canonicalization."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.utils.ner import (
    _normalize_text,
    _generate_aliases,
    EntityCanonicalizer,
    CanonicalMention,
    resolve_entities_to_canonical,
    canonical_jaccard,
    get_primary_entity_set,
)


class TestNormalization:
    """Tests for text normalization."""

    def test_lowercase(self):
        assert _normalize_text("Joe Biden") == "joe biden"

    def test_strip_punctuation(self):
        assert _normalize_text("U.S.A.") == "usa"
        assert _normalize_text("E.U.") == "eu"

    def test_remove_honorifics(self):
        assert _normalize_text("President Joe Biden") == "joe biden"
        assert _normalize_text("Mr. Smith") == "smith"
        assert _normalize_text("Dr. Jane Doe") == "jane doe"

    def test_collapse_whitespace(self):
        assert _normalize_text("  Joe    Biden  ") == "joe biden"


class TestAliasGeneration:
    """Tests for alias generation."""

    def test_person_aliases(self):
        aliases = _generate_aliases("Joe Biden", "PERSON")
        assert "biden" in aliases
        assert "j biden" in aliases
        assert "joe biden" in aliases

    def test_org_aliases(self):
        aliases = _generate_aliases("European Union", "ORG")
        assert "eu" in aliases
        assert "e u" in aliases

    def test_gpe_aliases(self):
        aliases = _generate_aliases("United States", "GPE")
        assert "us" in aliases
        assert "usa" in aliases
        assert "america" in aliases

        aliases = _generate_aliases("United Kingdom", "GPE")
        assert "uk" in aliases
        assert "britain" in aliases

    def test_no_duplicates(self):
        aliases = _generate_aliases("European Union", "ORG")
        # Should not have duplicates
        assert len(aliases) == len(set(aliases))


class TestCanonicalJaccard:
    """Tests for Jaccard on canonical ID sets."""

    def test_identical(self):
        a = {"id1", "id2"}
        b = {"id1", "id2"}
        assert canonical_jaccard(a, b) == 1.0

    def test_disjoint(self):
        a = {"id1", "id2"}
        b = {"id3", "id4"}
        assert canonical_jaccard(a, b) == 0.0

    def test_partial(self):
        a = {"id1", "id2", "id3"}
        b = {"id2", "id3", "id4"}
        assert canonical_jaccard(a, b) == 2 / 4  # 0.5

    def test_empty_sets(self):
        assert canonical_jaccard(set(), set()) == 1.0
        assert canonical_jaccard({"id1"}, set()) == 0.0
        assert canonical_jaccard(set(), {"id1"}) == 0.0


class TestEntityCanonicalizer:
    """Tests for EntityCanonicalizer behavior."""

    @pytest.mark.asyncio
    async def test_resolve_known_alias(self):
        """Known alias resolves to canonical entity."""
        mock_session = AsyncMock()

        # Mock database response
        mock_canonical = MagicMock()
        mock_canonical.id = "canonical-uuid-1"
        mock_canonical.canonical_name = "Joe Biden"
        mock_canonical.entity_type = "PERSON"
        mock_canonical.aliases = [
            MagicMock(alias="Joe Biden"),
            MagicMock(alias="biden"),
            MagicMock(alias="j biden"),
        ]

        # The normalize function now includes entity_type in the key
        # PERSON:joe biden, PERSON:biden, PERSON:j biden

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_canonical]
        mock_session.execute.return_value = mock_result

        canonicalizer = EntityCanonicalizer(mock_session)
        await canonicalizer.initialize()

        # Should resolve "Biden" (last name) to canonical
        mention = canonicalizer.resolve("Biden", "PERSON")
        assert mention is not None
        assert mention.canonical_id == "canonical-uuid-1"
        assert mention.canonical_name == "Joe Biden"

    @pytest.mark.asyncio
    async def test_resolve_unknown_returns_none(self):
        """Unknown surface form returns None."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        canonicalizer = EntityCanonicalizer(mock_session)
        await canonicalizer.initialize()

        mention = canonicalizer.resolve("Unknown Entity", "PERSON")
        assert mention is None

    @pytest.mark.asyncio
    async def test_get_or_create_creates_new(self):
        """Unknown entity creates new canonical entity."""
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        canonicalizer = EntityCanonicalizer(mock_session)
        await canonicalizer.initialize()

        mention = await canonicalizer.get_or_create("New Entity", "ORG")

        assert mention.canonical_name == "New Entity"
        assert mention.entity_type == "ORG"
        assert mention.confidence == 1.0
        # Should have called session.add for canonical entity and aliases
        assert mock_session.add.call_count >= 2  # canonical + at least one alias

    @pytest.mark.asyncio
    async def test_get_or_create_reuses_existing(self):
        """Known entity returns existing canonical entity."""
        mock_session = AsyncMock()

        mock_canonical = MagicMock()
        mock_canonical.id = "canonical-uuid-1"
        mock_canonical.canonical_name = "Joe Biden"
        mock_canonical.entity_type = "PERSON"
        mock_canonical.aliases = [MagicMock(alias="Joe Biden")]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_canonical]
        mock_session.execute.return_value = mock_result

        canonicalizer = EntityCanonicalizer(mock_session)
        await canonicalizer.initialize()

        mention = await canonicalizer.get_or_create("Joe Biden", "PERSON")

        assert mention.canonical_id == "canonical-uuid-1"
        assert mention.confidence == 1.0
        # Should NOT create new entities
        mock_session.add.assert_not_called()


class TestResolveEntitiesToCanonical:
    """Tests for resolve_entities_to_canonical function."""

    @pytest.mark.asyncio
    async def test_resolves_multiple_entities(self):
        """Multiple entities resolved to canonical IDs."""
        mock_session = AsyncMock()
        canonicalizer = EntityCanonicalizer(mock_session)

        # Mock resolve to return known mentions
        mention1 = CanonicalMention(
            surface_form="Joe Biden",
            canonical_id="id1",
            canonical_name="Joe Biden",
            entity_type="PERSON",
        )
        mention2 = CanonicalMention(
            surface_form="White House",
            canonical_id="id2",
            canonical_name="White House",
            entity_type="GPE",
        )
        canonicalizer.resolve = MagicMock(side_effect=[mention1, mention2])

        entities = {"PERSON": ["Joe Biden"], "GPE": ["White House"]}
        canonical_ids, mentions = await resolve_entities_to_canonical(entities, canonicalizer)

        assert len(canonical_ids) == 2
        assert "id1" in canonical_ids
        assert "id2" in canonical_ids
        assert len(mentions) == 2

    @pytest.mark.asyncio
    async def test_empty_entities(self):
        """Empty entity dict returns empty results."""
        mock_session = AsyncMock()
        canonicalizer = EntityCanonicalizer(mock_session)

        entities = {}
        canonical_ids, mentions = await resolve_entities_to_canonical(entities, canonicalizer)

        assert canonical_ids == set()
        assert mentions == []


class TestIntegration:
    """Integration-style tests showing canonicalization fixes grouping."""

    @pytest.mark.asyncio
    async def test_same_person_different_surface_forms_merge(self):
        """
        Two reporting units with "Biden" and "President Biden"
        should merge after canonicalization (same canonical ID).
        """
        mock_session = AsyncMock()

        mock_canonical = MagicMock()
        mock_canonical.id = "canonical-biden"
        mock_canonical.canonical_name = "Joe Biden"
        mock_canonical.entity_type = "PERSON"
        mock_canonical.aliases = [
            MagicMock(alias="Joe Biden"),
            MagicMock(alias="biden"),
            MagicMock(alias="president biden"),
            MagicMock(alias="potus"),
        ]
        # The normalize function now includes entity_type in the key:
        # PERSON:joe biden, PERSON:biden, PERSON:president biden, PERSON:potus

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_canonical]
        mock_session.execute.return_value = mock_result

        canonicalizer = EntityCanonicalizer(mock_session)
        await canonicalizer.initialize()

        # Unit 1 mentions "Biden"
        mention1 = canonicalizer.resolve("Biden", "PERSON")
        # Unit 2 mentions "President Biden"
        mention2 = canonicalizer.resolve("President Biden", "PERSON")

        assert mention1 is not None
        assert mention2 is not None
        assert mention1.canonical_id == mention2.canonical_id == "canonical-biden"

        # Jaccard on canonical IDs should be 1.0 (same entity)
        set1 = {mention1.canonical_id}
        set2 = {mention2.canonical_id}
        assert canonical_jaccard(set1, set2) == 1.0

    @pytest.mark.asyncio
    async def test_different_entities_with_same_surface_dont_merge(self):
        """
        Two distinct entities sharing a surface string should NOT merge.
        E.g., "Washington" (person) vs "Washington" (place).
        """
        mock_session = AsyncMock()

        mock_person = MagicMock()
        mock_person.id = "canonical-washington-person"
        mock_person.canonical_name = "George Washington"
        mock_person.entity_type = "PERSON"
        mock_person.aliases = [MagicMock(alias="Washington")]
        # Normalized keys: PERSON:washington

        mock_gpe = MagicMock()
        mock_gpe.id = "canonical-washington-place"
        mock_gpe.canonical_name = "Washington, D.C."
        mock_gpe.entity_type = "GPE"
        mock_gpe.aliases = [MagicMock(alias="Washington")]
        # Normalized keys: GPE:washington

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_person, mock_gpe]
        mock_session.execute.return_value = mock_result

        canonicalizer = EntityCanonicalizer(mock_session)
        await canonicalizer.initialize()

        # Unit 1: Washington (person)
        mention1 = canonicalizer.resolve("Washington", "PERSON")
        # Unit 2: Washington (place)
        mention2 = canonicalizer.resolve("Washington", "GPE")

        assert mention1 is not None
        assert mention2 is not None
        # Different canonical IDs because entity types differ
        assert mention1.canonical_id != mention2.canonical_id
        assert mention1.canonical_id == "canonical-washington-person"
        assert mention2.canonical_id == "canonical-washington-place"

        # Jaccard should be 0 (no overlap in canonical IDs)
        set1 = {mention1.canonical_id}
        set2 = {mention2.canonical_id}
        assert canonical_jaccard(set1, set2) == 0.0

    @pytest.mark.asyncio
    async def test_org_acronym_resolution(self):
        """EU / European Union resolve to same canonical."""
        mock_session = AsyncMock()

        mock_canonical = MagicMock()
        mock_canonical.id = "canonical-eu"
        mock_canonical.canonical_name = "European Union"
        mock_canonical.entity_type = "ORG"
        mock_canonical.aliases = [
            MagicMock(alias="European Union"),
            MagicMock(alias="eu"),
            MagicMock(alias="e u"),
        ]
        # Normalized keys: ORG:european union, ORG:eu, ORG:e u

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_canonical]
        mock_session.execute.return_value = mock_result

        canonicalizer = EntityCanonicalizer(mock_session)
        await canonicalizer.initialize()

        mention1 = canonicalizer.resolve("European Union", "ORG")
        mention2 = canonicalizer.resolve("EU", "ORG")

        assert mention1 is not None
        assert mention2 is not None
        assert mention1.canonical_id == mention2.canonical_id == "canonical-eu"


class TestGetPrimaryEntitySet:
    """Tests for existing get_primary_entity_set function."""

    def test_combines_person_org_gpe(self):
        entities = {
            "PERSON": ["Biden", "Trump"],
            "ORG": ["White House", "Congress"],
            "GPE": ["Washington", "America"],
            "LOC": ["Potomac River"],  # Should be ignored
        }
        result = get_primary_entity_set(entities)
        expected = {"Biden", "Trump", "White House", "Congress", "Washington", "America"}
        assert result == expected

    def test_missing_labels(self):
        entities = {"PERSON": ["Biden"]}
        result = get_primary_entity_set(entities)
        assert result == {"Biden"}

    def test_empty(self):
        entities = {}
        result = get_primary_entity_set(entities)
        assert result == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])