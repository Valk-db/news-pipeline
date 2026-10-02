"""Tests for approve_story and save_story - they should only act on PENDING stories."""

import pytest
import uuid
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from src.shared.config import get_settings
from src.shared import database as database_module
from src.schema.models import Story, CuratedPost


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


class TestApproveSaveStoryPENDINGCheck:
    """Tests that approve_story and save_story only act on PENDING stories."""

    @pytest.mark.asyncio
    async def test_approve_story_fails_on_non_pending_story(self, app_with_db, db_session, csrf_headers):
        """POST /story/{id}/approve should fail if story is not PENDING."""
        # Create a QUEUED story (already approved)
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.QUEUED,  # Not PENDING
            tier1_unit_count=2,
            distinct_owners=2,
        )
        db_session.add(story)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.post(
        f"/story/{story.id}/approve", auth=("testuser", "testpass"), headers=csrf_headers
    )

        # Should fail with 400 or 409 - not 200
        assert response.status_code != 200, "approve_story should not succeed on non-PENDING story"
        assert response.status_code in (400, 409, 422), f"Expected 400/409/422, got {response.status_code}"

    @pytest.mark.asyncio
    async def test_approve_story_fails_on_rejected_story(self, app_with_db, db_session, csrf_headers):
        """POST /story/{id}/approve should fail if story is REJECTED."""
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.REJECTED,  # Not PENDING
            tier1_unit_count=2,
            distinct_owners=2,
        )
        db_session.add(story)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.post(
        f"/story/{story.id}/approve", auth=("testuser", "testpass"), headers=csrf_headers
    )

        assert response.status_code != 200
        assert response.status_code in (400, 409, 422)

    @pytest.mark.asyncio
    async def test_approve_story_fails_on_posted_story(self, app_with_db, db_session, csrf_headers):
        """POST /story/{id}/approve should fail if story is POSTED."""
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.POSTED,  # Not PENDING
            tier1_unit_count=2,
            distinct_owners=2,
        )
        db_session.add(story)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.post(
        f"/story/{story.id}/approve", auth=("testuser", "testpass"), headers=csrf_headers
    )

        assert response.status_code != 200
        assert response.status_code in (400, 409, 422)

    @pytest.mark.asyncio
    async def test_save_story_fails_on_non_pending_story(self, app_with_db, db_session, csrf_headers):
        """POST /story/{id}/save should fail if story is not PENDING."""
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.QUEUED,  # Not PENDING
            tier1_unit_count=2,
            distinct_owners=2,
        )
        db_session.add(story)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.post(
            f"/story/{story.id}/save",
            data={"caption": "Test caption", "platform": "twitter"},
            auth=("testuser", "testpass"),
            headers=csrf_headers,
        )

        assert response.status_code != 200
        assert response.status_code in (400, 409, 422), f"Expected 400/409/422, got {response.status_code}"

    @pytest.mark.asyncio
    async def test_approve_story_succeeds_on_pending_story(self, app_with_db, db_session, csrf_headers):
        """POST /story/{id}/approve should succeed on PENDING story."""
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.PENDING,
            tier1_unit_count=2,
            distinct_owners=2,
        )
        db_session.add(story)
        await db_session.commit()

        # Mock the LLM to avoid actual API calls - must do this before creating TestClient
        import src.shared.llm as llm_module
        from unittest.mock import AsyncMock
        mock_llm = AsyncMock()
        mock_llm.generate_caption = AsyncMock(return_value="Test caption with source https://example.com")
        llm_module._llm_client = mock_llm

        client = TestClient(app_with_db)
        response = client.post(
        f"/story/{story.id}/approve", auth=("testuser", "testpass"), headers=csrf_headers
    )

        assert response.status_code == 200, f"approve_story should succeed on PENDING story: {response.text[:200]}"

        # Verify story status changed to QUEUED
        from sqlalchemy import select
        stmt = select(Story).where(Story.id == story.id)
        result = await db_session.execute(stmt)
        updated_story = result.scalar_one()
        # Refresh to see committed changes from the endpoint's session
        await db_session.refresh(updated_story)
        assert updated_story.status == Story.Status.QUEUED

    @pytest.mark.asyncio
    async def test_save_story_succeeds_on_pending_story(self, app_with_db, db_session, csrf_headers):
        """POST /story/{id}/save should succeed on PENDING story."""
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.PENDING,
            tier1_unit_count=2,
            distinct_owners=2,
        )
        db_session.add(story)
        await db_session.commit()

        # Mock the LLM for validate_caption - must do this before creating TestClient
        import src.shared.llm as llm_module
        from unittest.mock import AsyncMock
        mock_llm = AsyncMock()
        llm_module._llm_client = mock_llm

        client = TestClient(app_with_db)
        response = client.post(
            f"/story/{story.id}/save",
            data={"caption": "Test caption with source https://example.com", "platform": "twitter"},
            auth=("testuser", "testpass"),
            headers=csrf_headers,
        )

        assert response.status_code == 200, f"save_story should succeed on PENDING story: {response.text[:200]}"

        # Verify story status changed to QUEUED
        from sqlalchemy import select
        stmt = select(Story).where(Story.id == story.id)
        result = await db_session.execute(stmt)
        updated_story = result.scalar_one()
        # Refresh to see committed changes from the endpoint's session
        await db_session.refresh(updated_story)
        assert updated_story.status == Story.Status.QUEUED

    @pytest.mark.asyncio
    async def test_approve_story_idempotent_on_already_queued(self, app_with_db, db_session, csrf_headers):
        """Second approve on already QUEUED story should be idempotent (not create duplicate post)."""
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.PENDING,
            tier1_unit_count=2,
            distinct_owners=2,
        )
        db_session.add(story)
        await db_session.commit()

        # Mock the LLM - must do this before creating TestClient
        import src.shared.llm as llm_module
        from unittest.mock import AsyncMock
        mock_llm = AsyncMock()
        mock_llm.generate_caption = AsyncMock(return_value="Test caption with source https://example.com")
        llm_module._llm_client = mock_llm

        client = TestClient(app_with_db)
        # First approve - should succeed
        response1 = client.post(
        f"/story/{story.id}/approve", auth=("testuser", "testpass"), headers=csrf_headers
    )
        assert response1.status_code == 200

        # Verify story is now QUEUED and CuratedPost was created
        from sqlalchemy import select
        stmt = select(Story).where(Story.id == story.id)
        result = await db_session.execute(stmt)
        updated_story = result.scalar_one()
        await db_session.refresh(updated_story)
        assert updated_story.status == Story.Status.QUEUED

        # Second approve should fail (story is no longer PENDING, and CuratedPost exists)
        response2 = client.post(
        f"/story/{story.id}/approve", auth=("testuser", "testpass"), headers=csrf_headers
    )
        # Should return 409 (conflict) since story is now QUEUED and CuratedPost exists
        assert response2.status_code == 409

        # Should NOT create a second CuratedPost
        from sqlalchemy import select, func
        stmt = select(func.count(CuratedPost.id)).where(CuratedPost.story_id == story.id)
        result = await db_session.execute(stmt)
        post_count = result.scalar()
        assert post_count == 1, "Should not create duplicate CuratedPost"