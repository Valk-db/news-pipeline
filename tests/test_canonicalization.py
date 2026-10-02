"""Tests for entity canonicalization."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select
from src.schema.models import CanonicalEntity
from src.utils.ner import (
    _normalize_text,
    _generate_aliases,
    ALIAS_SEED,
    EntityCanonicalizer,
    CanonicalMention,
    canonical_surface,
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


class TestEnglishAliasNormalization:
    """The English alias seed, applied at canonicalization time."""

    def test_aliases_map_to_canonical_surface(self):
        assert canonical_surface("US", "GPE") == "United States"
        assert canonical_surface("U.S.", "GPE") == "United States"
        assert canonical_surface("the US", "GPE") == "United States"
        assert canonical_surface("UK", "GPE") == "United Kingdom"
        assert canonical_surface("UAE", "GPE") == "United Arab Emirates"
        assert canonical_surface("FBI", "ORG") == "Federal Bureau of Investigation"
        assert canonical_surface("Netanyahu", "PERSON") == "Benjamin Netanyahu"
        assert canonical_surface("Trump", "PERSON") == "Donald Trump"

    def test_honorifics_are_stripped_before_the_lookup(self):
        assert canonical_surface("President Trump", "PERSON") == "Donald Trump"

    def test_unknown_and_wrongly_typed_surfaces_pass_through(self):
        assert canonical_surface("Tennessee", "GPE") == "Tennessee"
        # "UK" as an ORG is not the country, so the GPE row must not apply
        assert canonical_surface("UK", "ORG") == "UK"

    def test_seed_rows_are_consistent(self):
        """Every row normalizes to its own canonical surface, and every alias is
        reachable from it -- a table that disagrees with itself is a silent bug."""
        for entity_type, alias, canonical in ALIAS_SEED:
            assert canonical_surface(alias, entity_type) == canonical
            assert _normalize_text(alias) in _generate_aliases(canonical, entity_type)

    @pytest.mark.asyncio
    async def test_alias_and_long_form_share_one_canonical_entity(self, db_session):
        """'US' then 'United States' resolve to one entity, not two."""
        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()

        short = await canonicalizer.get_or_create("US", "GPE")
        long = await canonicalizer.get_or_create("United States", "GPE")

        assert short.canonical_id == long.canonical_id
        assert short.canonical_name == long.canonical_name == "United States"

    @pytest.mark.asyncio
    async def test_long_form_first_also_shares_one_entity(self, db_session):
        """Order does not matter: whichever spelling lands first wins."""
        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()

        long = await canonicalizer.get_or_create("United States", "GPE")
        short = await canonicalizer.get_or_create("US", "GPE")

        assert short.canonical_id == long.canonical_id
        assert short.canonical_name == "United States"

    @pytest.mark.asyncio
    async def test_only_one_row_is_written_per_entity(self, db_session):
        """The merge is real rows merged, not two rows that resolve alike."""
        from sqlalchemy import func, select

        from src.schema.models import CanonicalEntity

        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()

        ids, _ = await resolve_entities_to_canonical(
            {"GPE": ["US", "United States", "USA"], "PERSON": ["Netanyahu", "Benjamin Netanyahu"]},
            canonicalizer,
        )
        await db_session.commit()

        assert len(ids) == 2
        result = await db_session.execute(
            select(func.count()).select_from(CanonicalEntity)
        )
        assert result.scalar() == 2

    @pytest.mark.asyncio
    async def test_story_grouping_no_longer_fragments(self, db_session):
        """Two stories about the same event with different spellings now group.

        Before the alias seed, 'US'/'United States' and 'Netanyahu'/'Benjamin
        Netanyahu' were four distinct canonical IDs: Jaccard was 0/6 = 0.0, well
        under the 0.4 attach threshold, so one event became two stories. Now the
        two aliases share IDs and the units attach.
        """
        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()

        bbc, _ = await resolve_entities_to_canonical(
            {"GPE": ["US"], "PERSON": ["Netanyahu"], "ORG": ["BBC"]}, canonicalizer
        )
        guardian, _ = await resolve_entities_to_canonical(
            {"GPE": ["United States"], "PERSON": ["Benjamin Netanyahu"], "ORG": ["Guardian"]},
            canonicalizer,
        )

        assert len(bbc & guardian) == 2
        assert canonical_jaccard(bbc, guardian) == 0.5 >= 0.4


class TestEntityGeocoding:
    """Creating a canonical place entity stores where that place is.

    This is what makes the globe possible: nothing else in the pipeline knows
    where a canonical entity is, so a place that was never geocoded can never
    reach the map.
    """

    class _StubGeocoder:
        def __init__(self, result):
            self.result = result
            self.asked = []

        async def geocode(self, name):
            self.asked.append(name)
            return self.result

    @staticmethod
    def _stub(monkeypatch, result):
        from src.enrichment import geocoder

        stub = TestEntityGeocoding._StubGeocoder(result)
        monkeypatch.setattr(geocoder, "get_geocoder", lambda: stub)
        return stub

    @pytest.mark.asyncio
    async def test_new_gpe_is_stored_with_its_coordinates(self, db_session, monkeypatch):
        """A place the pipeline has never seen gets a point on first sight."""
        from src.enrichment.geocoder import GeoResult

        self._stub(
            monkeypatch, GeoResult("Tel Aviv", 32.0853, 34.7818, "city", importance=0.72)
        )

        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()
        await canonicalizer.get_or_create("Tel Aviv", "GPE")
        await db_session.commit()

        entity = (
            await db_session.execute(
                select(CanonicalEntity).where(CanonicalEntity.canonical_name == "Tel Aviv")
            )
        ).scalar_one()
        assert entity.latitude == 32.0853
        assert entity.longitude == 34.7818
        assert entity.location_type == "city"
        assert entity.geo_importance == 0.72

    @pytest.mark.asyncio
    async def test_the_canonical_name_is_what_gets_geocoded(self, db_session, monkeypatch):
        """'US' is geocoded as 'United States', so the entity gets real coordinates."""
        from src.enrichment.geocoder import GeoResult

        stub = self._stub(monkeypatch, GeoResult("United States", 39.8, -98.6, "country"))

        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()
        mention = await canonicalizer.get_or_create("US", "GPE")

        assert stub.asked == ["United States"]
        assert mention.canonical_name == "United States"

    @pytest.mark.asyncio
    async def test_people_and_orgs_are_never_geocoded(self, db_session, monkeypatch):
        """A name that is not a place costs no geocoder request."""
        from src.enrichment.geocoder import GeoResult

        stub = self._stub(monkeypatch, GeoResult("nowhere", 0.0, 0.0, "city"))

        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()
        await canonicalizer.get_or_create("Joe Biden", "PERSON")
        await canonicalizer.get_or_create("BBC", "ORG")

        assert stub.asked == []

    @pytest.mark.asyncio
    async def test_a_place_nobody_can_find_still_becomes_an_entity(self, db_session, monkeypatch):
        """An unresolved name is stored unlocated, never dropped and never fatal."""
        self._stub(monkeypatch, None)

        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()
        mention = await canonicalizer.get_or_create("Wakanda", "GPE")
        await db_session.commit()

        entity = (
            await db_session.execute(
                select(CanonicalEntity).where(CanonicalEntity.canonical_name == "Wakanda")
            )
        ).scalar_one()
        assert mention.canonical_name == "Wakanda"
        assert entity.latitude is None
        assert entity.longitude is None

    @pytest.mark.asyncio
    async def test_an_existing_entity_is_not_geocoded_again(self, db_session, monkeypatch):
        """Coordinates are looked up once per place, not once per mention."""
        from src.enrichment.geocoder import GeoResult

        stub = self._stub(monkeypatch, GeoResult("Israel", 30.8124, 34.8595, "country"))

        canonicalizer = EntityCanonicalizer(db_session)
        await canonicalizer.initialize()
        await canonicalizer.get_or_create("Israel", "GPE")
        await canonicalizer.get_or_create("Israel", "GPE")
        await canonicalizer.get_or_create("Israel", "GPE")

        assert stub.asked == ["Israel"]


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