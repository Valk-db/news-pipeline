"""Tests for discovery: tier filter, date sort, topic search, near-dupe collapsing.

Search runs the ILIKE fallback here (SQLite has no pg_trgm); the trigram path
is pinned by TestTrigramSearchBranch, which compiles the real Postgres SQL,
and exercised live against dev Postgres during verification.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.schema.models import (
    Event,
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.shared import database as database_module
from src.shared.config import get_settings
from curation_ui.main import normalize_headline, _verification_badge


@pytest.fixture
def test_settings(monkeypatch):
    """Configure test settings with in-memory SQLite."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app_with_db(test_settings, db_engine):
    """Create app backed by the shared in-memory test database."""
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    # Reset the trigram probe cache: SQLite never has pg_trgm. The probe and its
    # cache live in curation_ui.discovery since the module split; patching the
    # old curation_ui.main home would silently stop taking effect.
    import curation_ui.discovery as discovery_module

    discovery_module._trigram_available_cache = False

    return app


def _make_rich_story(
    db_session,
    *,
    day,
    headline,
    tier_counts=(0, 0, 0, 0),
    owners=1,
    events=1,
    lang=None,
    headline_en=None,
):
    """Story with explicit tier mix and translatable headline.

    QUEUED, not PENDING: the public map and the public story page serve
    PUBLIC_STORY_STATUSES only, so a pending fixture is invisible to every
    endpoint below and every assertion here silently degrades to "no results".
    """
    t1, t2, t3, t4 = tier_counts
    story = Story(
        id=uuid.uuid4(),
        day=day,
        primary_entities=["test-entity"],
        status=Story.Status.QUEUED,
        tier1_unit_count=t1,
        tier2_unit_count=t2,
        tier3_unit_count=t3,
        tier4_unit_count=t4,
        distinct_owners=owners,
    )
    db_session.add(story)
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=day,
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={},
        owner_groups={},
        tier1_owner_groups={},
    )
    db_session.add(unit)
    article = RawArticle(
        id=unit.representative_article_id,
        url=f"https://example.com/{unit.id}",
        url_hash=str(unit.id).replace("-", ""),
        title=headline,
        title_en=headline_en,
        detected_language=lang,
        source_domain="example.com",
        source_tier=SourceTier.TIER1,
        reporting_unit_id=unit.id,
    )
    db_session.add(article)
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    for index in range(events):
        db_session.add(Event(
            id=uuid.uuid4(),
            story_id=story.id,
            latitude=50.45,
            longitude=30.52,
            location_name="Kyiv",
            location_type="city",
            radius_km=25.0,
            start_time=day + timedelta(hours=index),
            event_type=Event.EventType.CONFLICT,
            confidence=0.8,
            source_count=owners,
            tier1_source_count=t1,
            entities={"GPE": ["Kyiv"]},
        ))
    return story


class TestTierFilter:
    @pytest.mark.asyncio
    async def test_hide_tier3_drops_tier3_only_stories(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Tier one breakthrough in Geneva talks",
                         tier_counts=(2, 0, 0, 0), owners=2)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Viral rumor spreads on social forums",
                         tier_counts=(0, 0, 3, 0), owners=3)
        await db_session.commit()

        client = TestClient(app_with_db)
        all_resp = client.get("/api/map/stories?min_owners=0")
        assert all_resp.json()["count"] == 2

        filtered = client.get("/api/map/stories?min_owners=0&tiers=1,2")
        payload = filtered.json()
        assert payload["count"] == 1
        assert "Geneva" in payload["stories"][0]["headline"]

    @pytest.mark.asyncio
    async def test_tier_filter_keeps_mixed_stories(self, app_with_db, db_session):
        """A tier-1 story that also has tier-3 units survives tiers=1,2."""
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Mixed coverage of the summit",
                         tier_counts=(2, 0, 5, 0), owners=4)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories?min_owners=0&tiers=1,2").json()
        assert payload["count"] == 1

    @pytest.mark.asyncio
    async def test_bad_tiers_rejected(self, app_with_db, db_session):
        client = TestClient(app_with_db)
        resp = client.get("/api/map/stories?tiers=bogus")
        assert resp.status_code == 400


class TestSortAndWindow:
    @pytest.mark.asyncio
    async def test_newest_oldest_sort_orders_by_latest(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=10),
                         headline="Older story about the harbor bridge",
                         tier_counts=(2, 0, 0, 0), owners=2)
        _make_rich_story(db_session, day=now - timedelta(hours=1),
                         headline="Newer story about the peace summit",
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        newest = client.get("/api/map/stories?min_owners=0&sort=newest").json()
        assert "peace summit" in newest["stories"][0]["headline"]
        oldest = client.get("/api/map/stories?min_owners=0&sort=oldest").json()
        assert "harbor bridge" in oldest["stories"][0]["headline"]

    @pytest.mark.asyncio
    async def test_bad_sort_rejected(self, app_with_db, db_session):
        client = TestClient(app_with_db)
        assert client.get("/api/map/stories?sort=chaos").status_code == 400

    @pytest.mark.asyncio
    async def test_explicit_date_window(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(days=5),
                         headline="Ancient history piece",
                         tier_counts=(2, 0, 0, 0), owners=2)
        _make_rich_story(db_session, day=now - timedelta(hours=1),
                         headline="Fresh wire update",
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        from urllib.parse import quote
        start = quote((now - timedelta(hours=6)).isoformat(), safe="")
        payload = client.get(f"/api/map/stories?min_owners=0&start={start}").json()
        assert payload["count"] == 1
        assert "Fresh wire" in payload["stories"][0]["headline"]


class TestTopicSearch:
    @pytest.mark.asyncio
    async def test_search_matches_headline(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Earthquake relief efforts in the valley",
                         tier_counts=(2, 0, 0, 0), owners=2)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Parliament debates the new budget",
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories?min_owners=0&q=earthquake").json()
        assert payload["count"] == 1
        assert "Earthquake" in payload["stories"][0]["headline"]

    @pytest.mark.asyncio
    async def test_search_uses_english_translation(self, app_with_db, db_session):
        """An English query finds a French-headline story via its translation."""
        now = datetime.now(timezone.utc)
        _make_rich_story(
            db_session, day=now - timedelta(hours=2),
            headline="Le président annonce des mesures économiques",
            headline_en="The president announces economic measures",
            lang="fr",
            tier_counts=(2, 0, 0, 0), owners=2,
        )
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories?min_owners=0&q=economic measures").json()
        assert payload["count"] == 1
        story = payload["stories"][0]
        assert story["headline"] == "The president announces economic measures"
        assert story["translated"] is True
        assert story["lang"] == "fr"

    @pytest.mark.asyncio
    async def test_search_no_match_returns_empty(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Parliament debates the new budget",
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories?min_owners=0&q=xyznonexistent").json()
        assert payload["count"] == 0

    @pytest.mark.asyncio
    async def test_topic_word_inside_a_long_headline_matches(self, app_with_db, db_session):
        """A topic term must find a headline that merely contains it.

        This is the case the whole-string `%` comparison got wrong on Postgres:
        "trump" against "UK tries to stop Trump's diesel export ban" scores
        about 0.10, under the operator's 0.3 threshold, so the pg_trgm branch
        returned nothing for nearly every real topic word. On dev before the
        fix, 28 of the 30 most common tokens in the pending queue matched zero
        stories.
        """
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="UK tries to stop Trump's diesel export ban",
                         tier_counts=(2, 0, 0, 0), owners=2)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Parliament debates the new budget",
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories?min_owners=0&q=trump").json()
        assert payload["count"] == 1
        assert "Trump" in payload["stories"][0]["headline"]


class TestTrigramSearchBranch:
    """The pg_trgm branch is unreachable from SQLite, so pin it by compilation.

    Every other search test here takes the ILIKE fallback, which means this
    branch shipped having matched almost nothing in production while the suite
    stayed green. These assert the compiled Postgres SQL, and the live
    behaviour is verified against dev Postgres during the batch.
    """

    @staticmethod
    def _compiled(stmt) -> str:
        from sqlalchemy.dialects import postgresql
        return str(stmt.compile(dialect=postgresql.dialect()))

    @staticmethod
    def _rep_join():
        from sqlalchemy import select
        return (
            select(ReportingUnit.id, StoryUnitLink.story_id)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .subquery()
        )

    def test_ranks_by_word_similarity_not_whole_string_similarity(self):
        from curation_ui.discovery import _search_stmt_trigram
        sql = self._compiled(_search_stmt_trigram(self._rep_join(), "trump", 50))
        assert "word_similarity(" in sql
        # The regression this guards: whole-string similarity against a topic
        # word scores ~0.10 and matches nothing.
        assert "similarity(" not in sql.replace("word_similarity(", "")

    def test_matches_with_the_word_similarity_operator(self):
        from curation_ui.discovery import _search_stmt_trigram
        sql = self._compiled(_search_stmt_trigram(self._rep_join(), "ukraine", 50))
        assert "<%" in sql

    def test_still_matches_by_containment_like_the_fallback(self):
        """Both branches must select the same stories, or prod diverges from tests."""
        from curation_ui.discovery import _search_stmt_ilike, _search_stmt_trigram
        for stmt in (
            _search_stmt_trigram(self._rep_join(), "trump", 50),
            _search_stmt_ilike(self._rep_join(), "trump", 50),
        ):
            sql = self._compiled(stmt)
            assert "ILIKE" in sql
            # Both the translated and the original headline, in both branches.
            assert "title_en" in sql
            assert "title" in sql

    def test_only_the_trigram_branch_uses_trigram_functions(self):
        from curation_ui.discovery import _search_stmt_ilike, _search_stmt_trigram
        ilike_sql = self._compiled(_search_stmt_ilike(self._rep_join(), "trump", 50))
        assert "word_similarity" not in ilike_sql
        trigram_sql = self._compiled(_search_stmt_trigram(self._rep_join(), "trump", 50))
        assert "word_similarity" in trigram_sql


class TestEnglishHeadlineCoalescing:
    """Null title_en is the design for English rows, so every reader must fall back.

    A foreign article carries title_en; an English one never does, because
    duplicating the original into title_en would store the same text twice. Every
    path that shows a headline therefore has to read title_en or title, and a
    regression here is invisible on the translated stories alone: these fixtures
    are the English rows the coalescing exists for.
    """

    @pytest.mark.asyncio
    async def test_story_list_falls_back_to_the_original_headline(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Parliament debates the new budget",
                         lang="en", headline_en=None,
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        story = client.get("/api/map/stories?min_owners=0").json()["stories"][0]
        assert story["headline"] == "Parliament debates the new budget"
        assert story["headline_original"] == "Parliament debates the new budget"
        assert story["translated"] is False
        assert story["lang"] == "en"

    @pytest.mark.asyncio
    async def test_story_page_falls_back_to_the_original_headline(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Parliament debates the new budget",
                         lang="en", headline_en=None,
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        story_id = client.get("/api/map/stories?min_owners=0").json()["stories"][0]["story_id"]
        page = client.get(f"/stories/{story_id}")
        assert page.status_code == 200
        assert "Parliament debates the new budget" in page.text
        # The FR->EN tag is driven by `translated`, so an English row never shows it.
        assert "→EN" not in page.text


class TestMapPageDiscovery:
    @pytest.mark.asyncio
    async def test_map_page_renders_discovery_controls(self, app_with_db, db_session):
        """The /map page ships the search bar, tier chips, window and sort,
        with initial state taken from the query string."""
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Earthquake relief efforts in the valley",
                         tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get("/map?tiers=1,2&sort=newest&q=earthquake&hours=168")
        assert response.status_code == 200
        html = response.text
        assert 'id="map-search"' in html
        assert 'value="earthquake"' in html
        assert 'id="map-window"' in html
        assert 'id="map-sort"' in html
        assert 'class="map-tier-chip is-on"' in html
        # T1 and T2 on, T3 and T4 off
        import re
        chips = re.findall(r'class="map-tier-chip[^"]*"[^>]*aria-pressed="(\w+)"', html)
        assert chips == ["true", "true", "false", "false"]
        # Verification badge rendered for the story
        assert 'map-verify-badge' in html


class TestNearDupeCollapsing:
    @pytest.mark.asyncio
    async def test_same_headline_collapses_to_one(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        for i in range(3):
            _make_rich_story(db_session, day=now - timedelta(hours=2),
                             headline="Macron et Milei veulent renforcer les liens économiques",
                             tier_counts=(0, 0, 2, 0), owners=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories?min_owners=0").json()
        assert payload["count"] == 1

    def test_normalize_headline(self):
        assert normalize_headline("Hello,  World!") == "hello world"
        assert normalize_headline("Macron et Milei...") == "macron et milei"
        assert normalize_headline(None) == ""
        assert normalize_headline("") == ""


class TestVerificationBadge:
    def test_badge_corroborated(self):
        story = Story(tier1_unit_count=2, tier2_unit_count=1,
                      tier3_unit_count=3, tier4_unit_count=0, distinct_owners=4)
        badge = _verification_badge(story)
        assert badge["corroborated"] is True
        assert badge["tier_mix"] == {"t1": 2, "t2": 1, "t3": 3, "t4": 0}
        assert badge["best_tier"] == 1
        assert "Corroborated" in badge["label"]

    def test_badge_single_outlet(self):
        story = Story(tier1_unit_count=0, tier2_unit_count=0,
                      tier3_unit_count=1, tier4_unit_count=0, distinct_owners=1)
        badge = _verification_badge(story)
        assert badge["corroborated"] is False
        assert badge["best_tier"] == 3
        assert "Single outlet" in badge["label"]

    @pytest.mark.asyncio
    async def test_summary_carries_verification(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_rich_story(db_session, day=now - timedelta(hours=2),
                         headline="Badge check story",
                         tier_counts=(2, 1, 0, 0), owners=3)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories?min_owners=0").json()
        v = payload["stories"][0]["verification"]
        assert v["outlets"] == 3
        assert v["tier_mix"]["t1"] == 2
