"""Tests for entity canonicalization."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select
from src.schema.models import CanonicalEntity
from src.utils.ner import (
    _normalize_text,
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
        assert _normalize_text("POTUS Donald Trump") == "donald trump"
        assert _normalize_text("Prince Harry") == "harry"

    def test_honorifics_are_only_stripped_from_the_front(self):
        """A title word inside a name is part of the name.

        The unanchored version of this rule deleted "minister" out of "Foreign
        Minister" and would delete it out of any name containing it, which is how
        one person's name ends up filed as another's.
        """
        assert _normalize_text("Foreign Minister Yemis") == "foreign minister yemis"
        assert _normalize_text("Deputy Prime Minister Asad") == "deputy prime minister asad"

    def test_trailing_possessive_is_the_same_entity(self):
        assert _normalize_text("Andy Burnham's") == "andy burnham"
        assert _normalize_text("Nepal's") == "nepal"
        # Only a trailing genitive; a possessive inside the name stays part of it.
        assert _normalize_text("Sheriff's Department") == "sheriffs department"

    def test_leading_article_is_dropped_for_places_and_organizations(self):
        assert _normalize_text("the European Union", "ORG") == "ORG:european union"
        assert _normalize_text("the West Bank", "GPE") == "GPE:west bank"
        # No attested PERSON surface form starts with an article, so none is dropped.
        assert _normalize_text("the Rock", "PERSON") == "PERSON:the rock"

    def test_accents_fold(self):
        assert _normalize_text("Sébastien Lecornu") == "sebastien lecornu"
        assert _normalize_text("Jürgen Klopp") == "jurgen klopp"
        assert _normalize_text("Édouard Geffray") == "edouard geffray"
        assert _normalize_text("Laurent Nuñez") == "laurent nunez"

    def test_entity_type_is_part_of_the_key(self):
        assert _normalize_text("Washington", "GPE") != _normalize_text("Washington", "PERSON")

    def test_collapse_whitespace(self):
        assert _normalize_text("  Joe    Biden  ") == "joe biden"


class TestPlaceNames:
    """Nominatim display strings and the places they name.

    The geocoder hands back "London, London, City Of, United Kingdom" for a place
    the corpus also names bare as "London", and "Tabuk, Tabuk, Saudi Arabia" for
    "Tabuk". Both spellings reached canonicalization as separate entities, so the
    same place became two map pins and two stories.
    """

    def test_administrative_tail_is_dropped(self):
        assert canonical_surface("London, London, City Of, United Kingdom", "GPE") == "London"
        assert canonical_surface("Washington, Washington, United States", "GPE") == "Washington"
        assert canonical_surface("Tabuk, Tabuk, Saudi Arabia", "GPE") == "Tabuk"
        assert canonical_surface("Manchester, Manchester, United Kingdom", "GPE") == "Manchester"
        assert canonical_surface("New Delhi, Delhi, India", "GPE") == "New Delhi"

    def test_a_name_without_a_tail_is_untouched(self):
        assert canonical_surface("Mumbai", "GPE") == "Mumbai"
        assert canonical_surface("Texas, United States", "GPE") == "Texas"

    def test_only_places_drop_a_tail(self):
        assert canonical_surface("Smith, John and Co", "ORG") == "Smith, John and Co"
        assert canonical_surface("Smith, John", "PERSON") == "Smith, John"


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
        assert canonical_surface("POTUS Donald Trump", "PERSON") == "Donald Trump"

    def test_a_possessive_is_not_minted_as_a_display_name(self):
        assert canonical_surface("Andy Burnham's", "PERSON") == "Andy Burnham"
        assert canonical_surface("Nepal's", "GPE") == "Nepal"

    def test_unknown_and_wrongly_typed_surfaces_pass_through(self):
        assert canonical_surface("Tennessee", "GPE") == "Tennessee"
        # "UK" as an ORG is not the country, so the GPE row must not apply
        assert canonical_surface("UK", "ORG") == "UK"

    def test_seed_rows_are_consistent(self):
        """Every alias resolves to the canonical name it claims, and a canonical
        name is never rewritten into something else -- a table that disagrees with
        itself is a silent bug."""
        for entity_type, alias, canonical in ALIAS_SEED:
            assert canonical_surface(alias, entity_type) == canonical
            assert canonical_surface(canonical, entity_type) == canonical

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


class TestAliasResolution:
    """Surface forms that name one entity resolve to one canonical entity.

    Every surface form below is one the corpus actually contains, except where the
    test says otherwise. The pairs are the ones the pipeline got wrong: they either
    became two entities for one referent, or two referents became one.
    """

    @staticmethod
    async def _canonicalizer(session):
        canonicalizer = EntityCanonicalizer(session)
        await canonicalizer.initialize()
        return canonicalizer

    # --- PERSON

    @pytest.mark.asyncio
    async def test_person_title_possessive_case_and_accents_are_one_entity(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        mentions = [
            await canonicalizer.get_or_create(surface, "PERSON")
            for surface in ("POTUS Donald Trump", "Donald Trump", "donald trump", "Trump's")
        ]
        assert len({m.canonical_id for m in mentions}) == 1

        accented = [
            await canonicalizer.get_or_create(surface, "PERSON")
            for surface in ("Sébastien Lecornu", "Sebastien Lecornu")
        ]
        assert len({m.canonical_id for m in accented}) == 1
        # The display name keeps the accents the newsroom wrote.
        assert accented[0].canonical_name == "Sébastien Lecornu"

    @pytest.mark.asyncio
    async def test_a_bare_surname_reaches_the_only_person_it_names(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        burnham = await canonicalizer.get_or_create("Andy Burnham", "PERSON")
        await canonicalizer.get_or_create("Pete Hegseth", "PERSON")
        bare = await canonicalizer.get_or_create("Burnham", "PERSON")

        assert bare.canonical_id == burnham.canonical_id
        # Lower confidence than an exact key: this one is an inference, not a lookup.
        assert bare.confidence < 1.0

    @pytest.mark.asyncio
    async def test_a_surname_two_people_share_resolves_to_neither(self, db_session):
        """"Lee" names three people in the corpus, so it resolves to none of them.

        An earlier revision answered this with whichever Lee the cache happened to
        hold first, so Bill Lee's coverage was filed under Hoon Lee.
        """
        canonicalizer = await self._canonicalizer(db_session)

        people = [
            await canonicalizer.get_or_create(name, "PERSON")
            for name in ("Hoon Lee", "Bill Lee", "Lee Jae Myung")
        ]
        assert len({p.canonical_id for p in people}) == 3

        bare = await canonicalizer.get_or_create("Lee", "PERSON")
        assert bare.canonical_id not in {p.canonical_id for p in people}

    @pytest.mark.asyncio
    async def test_a_full_name_never_collapses_into_a_first_token(self, db_session):
        """"John Trump" is not "John", however many Johns there are.

        The substring fallback this replaces returned the first cached key that was
        contained in, or contained, the surface form and shared a token with it, so
        "John Trump" resolved to any entity named "John".
        """
        canonicalizer = await self._canonicalizer(db_session)

        john = await canonicalizer.get_or_create("John Kerry", "PERSON")
        john_trump = await canonicalizer.get_or_create("John Trump", "PERSON")

        assert john_trump.canonical_id != john.canonical_id
        assert john_trump.canonical_name == "John Trump"

    @pytest.mark.asyncio
    async def test_a_newly_named_person_becomes_a_surname_target_in_the_same_run(self, db_session):
        """A bare surname needs no second pass: the run that mints the full name
        answers the bare surname later in the same batch."""
        canonicalizer = await self._canonicalizer(db_session)

        full = await canonicalizer.get_or_create("Andreas Jansson", "PERSON")
        bare = await canonicalizer.get_or_create("Jansson", "PERSON")

        assert bare.canonical_id == full.canonical_id

    @pytest.mark.asyncio
    async def test_a_second_person_with_the_surname_retracts_the_claim(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        await canonicalizer.get_or_create("Pete Hegseth", "PERSON")
        await canonicalizer.get_or_create("Dan Hegseth", "PERSON")
        bare = await canonicalizer.get_or_create("Hegseth", "PERSON")

        assert bare.canonical_name == "Hegseth"

    @pytest.mark.asyncio
    async def test_a_person_and_a_place_of_the_same_name_stay_apart(self, db_session):
        """The brief's adversarial case: "Washington" is a state and a surname."""
        canonicalizer = await self._canonicalizer(db_session)

        place = await canonicalizer.get_or_create("Washington, Washington, United States", "GPE")
        assert place.canonical_name == "Washington"
        person = await canonicalizer.get_or_create("George Washington", "PERSON")

        bare_person = await canonicalizer.get_or_create("Washington", "PERSON")

        # The state and the man are two entities, and a mention the tagger called a
        # person resolves to the person: the entity type is part of the lookup key, so
        # no amount of shared spelling carries one type's answer into the other.
        assert place.canonical_id != person.canonical_id
        assert bare_person.canonical_id != place.canonical_id
        # "Washington" the surname reaches the one person the corpus has that way, and
        # says so by claiming less confidence than an exact key does.
        assert bare_person.canonical_id == person.canonical_id
        assert bare_person.confidence < 1.0

    # --- ORG

    @pytest.mark.asyncio
    async def test_organization_article_and_case_are_one_entity(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        mentions = [
            await canonicalizer.get_or_create(surface, "ORG")
            for surface in ("the Supreme Court", "the supreme court", "Supreme Court")
        ]
        assert len({m.canonical_id for m in mentions}) == 1

    @pytest.mark.asyncio
    async def test_an_acronym_resolves_to_its_long_form(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        nato = await canonicalizer.get_or_create("NATO", "ORG")
        long_form = await canonicalizer.get_or_create("the North Atlantic Treaty Organization", "ORG")

        assert nato.canonical_id == long_form.canonical_id
        assert nato.canonical_name == "North Atlantic Treaty Organization"

    @pytest.mark.asyncio
    async def test_one_word_of_an_organization_is_not_the_organization(self, db_session):
        """"the House" is not the White House.

        The generated aliases made every word of a multi-word name an alias of the
        whole, so "house", "white", "news", "border", "court" and "google" each
        resolved to whichever organization happened to contain them.
        """
        canonicalizer = await self._canonicalizer(db_session)

        white_house = await canonicalizer.get_or_create("the White House", "ORG")
        house = await canonicalizer.get_or_create("the House", "ORG")

        assert house.canonical_id != white_house.canonical_id

    # --- GPE

    @pytest.mark.asyncio
    async def test_place_with_and_without_an_administrative_tail_are_one_entity(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        long_form = await canonicalizer.get_or_create(
            "London, London, City Of, United Kingdom", "GPE"
        )
        bare = await canonicalizer.get_or_create("London", "GPE")

        assert long_form.canonical_id == bare.canonical_id
        assert long_form.canonical_name == bare.canonical_name == "London"

    @pytest.mark.asyncio
    async def test_place_possessive_and_article_are_one_entity(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        nepal = await canonicalizer.get_or_create("Nepal", "GPE")
        possessive = await canonicalizer.get_or_create("Nepal's", "GPE")
        west_bank = await canonicalizer.get_or_create("the West Bank", "GPE")

        west_bank_bare = await canonicalizer.get_or_create("West Bank", "GPE")

        assert possessive.canonical_id == nepal.canonical_id
        assert west_bank.canonical_id == west_bank_bare.canonical_id

    @pytest.mark.asyncio
    async def test_different_places_do_not_merge(self, db_session):
        canonicalizer = await self._canonicalizer(db_session)

        tabuk = await canonicalizer.get_or_create("Tabuk, Tabuk, Saudi Arabia", "GPE")
        riyadh = await canonicalizer.get_or_create("Riyadh, Riyadh, Saudi Arabia", "GPE")

        assert tabuk.canonical_id != riyadh.canonical_id


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
        # The entity and the one alias that named it, and nothing else: an earlier
        # revision also wrote every bare word of the name as an alias, which is how
        # "New" and "Entity" became organizations in their own right.
        assert mock_session.add.call_count == 2

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