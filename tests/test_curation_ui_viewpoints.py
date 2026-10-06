"""Tests for /api/stories/{story_id}/viewpoints endpoint."""

import pytest
import uuid
from datetime import datetime, UTC
from fastapi.testclient import TestClient
from src.shared.config import get_settings
from src.shared import database as database_module
from src.schema.models import Story, ReportingUnit, RawArticle, StoryUnitLink, SourceTier


@pytest.fixture
def test_settings(monkeypatch):
    """Configure test settings."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app_with_db(test_settings, db_engine):
    """Create app with test database."""
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    return app


class TestStoryViewpointsEndpoint:
    """Tests for GET /api/stories/{story_id}/viewpoints."""

    @pytest.mark.asyncio
    async def test_viewpoints_endpoint_returns_200_for_valid_story(self, app_with_db, db_session):
        """GET /api/stories/{story_id}/viewpoints returns 200 with data for valid story."""
        # Create a parent story
        parent_story = Story(
            id=uuid.uuid4(),
            day=datetime.now(UTC),
            primary_entities=["test-entity"],
            status=Story.Status.PENDING,
            tier1_unit_count=2,
            tier2_unit_count=1,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=2,
        )
        db_session.add(parent_story)
        await db_session.flush()

        # Create a reporting unit
        article = RawArticle(
            id=uuid.uuid4(),
            url="https://apnews.com/test-1",
            url_hash="hash1",
            title="Test Article 1",
            body_text="Test body text about politics",
            source_domain="apnews.com",
            source_tier=SourceTier.TIER1,
            published_at=datetime.now(UTC),
            entities={"PERSON": ["John Doe"], "ORG": ["AP"], "GPE": ["United States"]},
        )
        db_session.add(article)
        await db_session.flush()

        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(UTC),
            representative_article_id=article.id,
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={"AP": 1},
            tier1_owner_groups={"AP": 1},
        )
        db_session.add(unit)
        await db_session.flush()

        # Link unit to story
        link = StoryUnitLink(story_id=parent_story.id, unit_id=unit.id)
        db_session.add(link)
        await db_session.commit()

        # Test the endpoint
        client = TestClient(app_with_db)
        response = client.get(
            f"/api/stories/{parent_story.id}/viewpoints",
            auth=("testuser", "testpass")
        )

        assert response.status_code == 200
        data = response.json()
        assert "story_id" in data
        assert "viewpoints" in data
        assert "total_viewpoints" in data
        assert len(data["viewpoints"]) >= 1  # At least the overview

    @pytest.mark.asyncio
    async def test_viewpoints_endpoint_returns_404_for_invalid_story(self, app_with_db):
        """GET /api/stories/{story_id}/viewpoints returns 404 for non-existent story."""
        client = TestClient(app_with_db)
        fake_id = uuid.uuid4()
        response = client.get(
            f"/api/stories/{fake_id}/viewpoints",
            auth=("testuser", "testpass")
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_viewpoints_endpoint_with_viewpoint_subcluster(self, app_with_db, db_session):
        """GET /api/stories/{story_id}/viewpoints includes viewpoint sub-clusters."""
        # Create a parent story
        parent_story = Story(
            id=uuid.uuid4(),
            day=datetime.now(UTC),
            primary_entities=["test-entity"],
            status=Story.Status.PENDING,
            tier1_unit_count=2,
            tier2_unit_count=1,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=2,
        )
        db_session.add(parent_story)
        await db_session.flush()

        # Create reporting units for parent
        article1 = RawArticle(
            id=uuid.uuid4(),
            url="https://apnews.com/test-1",
            url_hash="hash1",
            title="Test Article 1",
            body_text="Test body text about politics",
            source_domain="apnews.com",
            source_tier=SourceTier.TIER1,
            published_at=datetime.now(UTC),
            entities={"PERSON": ["John Doe"], "ORG": ["AP"], "GPE": ["United States"]},
        )
        db_session.add(article1)

        article2 = RawArticle(
            id=uuid.uuid4(),
            url="https://reuters.com/test-2",
            url_hash="hash2",
            title="Test Article 2",
            body_text="Test body text about politics",
            source_domain="reuters.com",
            source_tier=SourceTier.TIER1,
            published_at=datetime.now(UTC),
            entities={"PERSON": ["Jane Smith"], "ORG": ["Reuters"], "GPE": ["United States"]},
        )
        db_session.add(article2)
        await db_session.flush()

        unit1 = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(UTC),
            representative_article_id=article1.id,
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={"AP": 1},
            tier1_owner_groups={"AP": 1},
        )
        db_session.add(unit1)

        unit2 = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(UTC),
            representative_article_id=article2.id,
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={"Reuters": 1},
            tier1_owner_groups={"Reuters": 1},
        )
        db_session.add(unit2)
        await db_session.flush()

        # Link units to parent story
        link1 = StoryUnitLink(story_id=parent_story.id, unit_id=unit1.id)
        link2 = StoryUnitLink(story_id=parent_story.id, unit_id=unit2.id)
        db_session.add(link1)
        db_session.add(link2)

        # Create a viewpoint sub-cluster story
        viewpoint_story = Story(
            id=uuid.uuid4(),
            day=datetime.now(UTC),
            primary_entities=["test-entity"],
            status=Story.Status.PENDING,
            viewpoint_cluster_id=parent_story.id,
            tier1_unit_count=1,
            tier2_unit_count=0,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=1,
        )
        db_session.add(viewpoint_story)
        await db_session.flush()

        # Create unit for viewpoint
        article3 = RawArticle(
            id=uuid.uuid4(),
            url="https://bbc.com/test-3",
            url_hash="hash3",
            title="Test Article 3 - Different Perspective",
            body_text="Test body text with different view",
            source_domain="bbc.com",
            source_tier=SourceTier.TIER1,
            published_at=datetime.now(UTC),
            entities={"PERSON": ["Bob Wilson"], "ORG": ["BBC"], "GPE": ["United Kingdom"]},
        )
        db_session.add(article3)
        await db_session.flush()

        viewpoint_unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(UTC),
            representative_article_id=article3.id,
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={"BBC": 1},
            tier1_owner_groups={"BBC": 1},
        )
        db_session.add(viewpoint_unit)
        await db_session.flush()

        # Link unit to viewpoint story
        vp_link = StoryUnitLink(story_id=viewpoint_story.id, unit_id=viewpoint_unit.id)
        db_session.add(vp_link)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(
            f"/api/stories/{parent_story.id}/viewpoints",
            auth=("testuser", "testpass")
        )

        assert response.status_code == 200
        data = response.json()
        assert "story_id" in data
        assert "viewpoints" in data
        assert data["total_viewpoints"] >= 2  # Overview + at least 1 viewpoint

        # Check that overview has correct structure
        overview = next((v for v in data["viewpoints"] if v["type"] == "overview"), None)
        assert overview is not None
        assert overview["label"] == "All Perspectives"
        assert overview["unit_count"] == 2

        # Check that viewpoint has correct structure
        viewpoint = next((v for v in data["viewpoints"] if v["type"] == "viewpoint"), None)
        assert viewpoint is not None
        assert viewpoint["label"] == "Perspective"
        assert viewpoint["unit_count"] >= 1