"""Tests for claim extraction functionality."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.verification.claims import extract_claims_for_story
from src.schema.models import Story, ReportingUnit, RawArticle, SourceTier
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock(spec=AsyncSession)
    return session


@pytest.fixture
def sample_story():
    """Create a sample story."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=["entity1", "entity2"],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.QUEUED,
    )


@pytest.fixture
def sample_units():
    """Create sample reporting units."""
    units = []
    for i in range(4):
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            representative_article_id=uuid.uuid4(),
            article_count=1,
            source_tiers={"tier1": 1, "tier2": 0, "tier3": 0} if i < 2 else {"tier2": 1},
            owner_groups={"BBC": 1, "Guardian": 1} if i < 2 else {"NYT": 1},
            tier1_owner_groups={"BBC": 1, "Guardian": 1} if i < 2 else {},
        )
        units.append(unit)
    return units


@pytest.fixture
def sample_articles():
    """Create sample raw articles with different perspectives."""
    articles = []
    perspectives = [
        "The government announced new economic policies today. Officials say this will boost growth and create jobs. Proponents praise the measures as timely and necessary for recovery.",
        "Opposition leaders criticize the new economic policies as insufficient. They argue the measures don't address inequality and will benefit corporations more than workers.",
        "International observers view the economic policies as a positive step but note structural challenges remain. The IMF welcomed the reforms while urging further action.",
        "Local business owners express mixed views on the economic policies. Some welcome tax relief while others worry about regulatory changes affecting small businesses.",
    ]

    for i, text in enumerate(perspectives):
        article = RawArticle(
            id=uuid.uuid4(),
            url=f"https://example.com/article{i}",
            url_hash=f"hash{i}",
            title=f"Economic Policy Article {i}",
            body_text=text,
            source_domain="bbc.com" if i < 2 else ("nytimes.com" if i == 2 else "localnews.com"),
            source_tier=SourceTier.TIER1 if i < 2 else SourceTier.TIER2,
            published_at=datetime.now(timezone.utc),
            entities={"PERSON": ["Official", "Leader"], "ORG": ["Government", "IMF"], "GPE": ["Country"]},
            content_hash=f"content{i}",
        )
        articles.append(article)
    return articles


@pytest.mark.asyncio
async def test_extract_claims_for_story_basic(mock_session, sample_story, sample_units, sample_articles):
    """Test basic claim extraction for a story."""
    story_id = sample_story.id

    # Mock the database queries
    mock_session.execute = AsyncMock()

    # 1. Story query
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = sample_story

    # 2. Story with units for _gather_unit_texts_for_story
    story_with_units_result = MagicMock()
    story_with_units = MagicMock()
    story_with_units.units = sample_units
    story_with_units_result.scalar_one_or_none.return_value = story_with_units

    # 3. Article queries for each unit
    article_results = []
    for article in sample_articles:
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = article
        article_results.append(article_result)

    mock_session.execute.side_effect = [
        story_result,
        story_with_units_result,
    ] + article_results

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    # Mock LLM client
    with patch('src.shared.llm.get_llm_client') as mock_get_llm:
        mock_llm = MagicMock()
        mock_llm.chat_completion = AsyncMock()
        mock_get_llm.return_value = mock_llm

        # Mock LLM response with claims
        unit_id_strs = [str(u.id) for u in sample_units]
        claims_response = {
            "claims": [
                {
                    "text": "The government announced new economic policies.",
                    "claim_type": "fact",
                    "evidence": [
                        {"unit_id": unit_id_strs[0], "stance": "supports", "confidence": 90},
                        {"unit_id": unit_id_strs[1], "stance": "supports", "confidence": 85},
                        {"unit_id": unit_id_strs[2], "stance": "neutral", "confidence": 50},
                        {"unit_id": unit_id_strs[3], "stance": "disputes", "confidence": 70},
                    ]
                },
                {
                    "text": "The policies will boost growth and create jobs.",
                    "claim_type": "prediction",
                    "evidence": [
                        {"unit_id": unit_id_strs[0], "stance": "supports", "confidence": 80},
                        {"unit_id": unit_id_strs[1], "stance": "disputes", "confidence": 75},
                    ]
                },
            ]
        }
        import json
        mock_llm.chat_completion = AsyncMock(return_value={
            "choices": [{"message": {"content": json.dumps(claims_response)}}]
        })
        mock_llm._parse_json_response.return_value = claims_response

        # Call the function
        result = await extract_claims_for_story(mock_session, story_id)

        # Verify results
        assert isinstance(result, dict)
        assert result["story_id"] == str(story_id)
        assert result["claims_created"] == 2
        assert result["evidence_created"] == 6  # 4 + 2
        assert len(result["errors"]) == 0

        # Verify LLM was called
        mock_llm.chat_completion.assert_called_once()
        # Verify _parse_json_response was used (not raw json.loads)
        mock_llm._parse_json_response.assert_called_once()

        # Verify session operations
        assert mock_session.add.call_count == 8  # 2 claims + 6 evidence
        mock_session.flush.assert_called()
        mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_extract_claims_for_story_not_found(mock_session):
    """Test claim extraction when story doesn't exist."""
    story_id = uuid.uuid4()

    mock_session.execute = AsyncMock()
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = story_result

    result = await extract_claims_for_story(mock_session, story_id)

    assert result["story_id"] == str(story_id)
    assert result["claims_created"] == 0
    assert result["evidence_created"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0] == "Story not found"


@pytest.mark.asyncio
async def test_extract_claims_for_story_insufficient_units(mock_session, sample_story):
    """Test claim extraction with insufficient units."""
    story_id = sample_story.id

    mock_session.execute = AsyncMock()

    # 1. Story query
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = sample_story

    # 2. Story with units - only 1 unit with text
    story_with_units_result = MagicMock()
    story_with_units = MagicMock()
    # Create 1 unit with body_text
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"BBC": 1},
        tier1_owner_groups={"BBC": 1},
    )
    story_with_units.units = [unit]
    story_with_units_result.scalar_one_or_none.return_value = story_with_units

    # 3. Article query - one article
    article_result = MagicMock()
    article = RawArticle(
        id=unit.representative_article_id,
        url="https://example.com/article",
        url_hash="hash",
        title="Test Article",
        body_text="Some text.",
        source_domain="bbc.com",
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(timezone.utc),
        content_hash="content",
    )
    article_result.scalar_one_or_none.return_value = article

    mock_session.execute.side_effect = [
        story_result,
        story_with_units_result,
        article_result,
    ]

    result = await extract_claims_for_story(mock_session, story_id)

    # Should return early with no claims (not enough units)
    assert result["story_id"] == str(story_id)
    assert result["claims_created"] == 0
    assert result["evidence_created"] == 0
    assert len(result["errors"]) == 0


@pytest.mark.asyncio
async def test_extract_claims_malformed_llm_response(mock_session, sample_story, sample_units, sample_articles):
    """Test that malformed LLM response doesn't crash the batch."""
    story_id = sample_story.id

    mock_session.execute = AsyncMock()

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = sample_story

    story_with_units_result = MagicMock()
    story_with_units = MagicMock()
    story_with_units.units = sample_units
    story_with_units_result.scalar_one_or_none.return_value = story_with_units

    article_results = []
    for article in sample_articles:
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = article
        article_results.append(article_result)

    mock_session.execute.side_effect = [
        story_result,
        story_with_units_result,
    ] + article_results

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    with patch('src.shared.llm.get_llm_client') as mock_get_llm:
        mock_llm = MagicMock()
        mock_llm.chat_completion = AsyncMock()
        mock_get_llm.return_value = mock_llm

        # Malformed response - missing required fields
        mock_llm.chat_completion = AsyncMock(return_value={
            "choices": [{"message": {"content": "not valid json"}}]
        })
        mock_llm._parse_json_response.side_effect = ValueError("Invalid JSON")

        result = await extract_claims_for_story(mock_session, story_id)

        assert result["story_id"] == str(story_id)
        assert result["claims_created"] == 0
        assert result["evidence_created"] == 0
        assert len(result["errors"]) == 1
        assert "Invalid JSON" in result["errors"][0]


@pytest.mark.asyncio
async def test_extract_claims_malformed_evidence_skipped(mock_session, sample_story, sample_units, sample_articles):
    """Test that malformed evidence row doesn't drop the whole claim."""
    story_id = sample_story.id

    mock_session.execute = AsyncMock()

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = sample_story

    story_with_units_result = MagicMock()
    story_with_units = MagicMock()
    story_with_units.units = sample_units
    story_with_units_result.scalar_one_or_none.return_value = story_with_units

    article_results = []
    for article in sample_articles:
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = article
        article_results.append(article_result)

    mock_session.execute.side_effect = [
        story_result,
        story_with_units_result,
    ] + article_results

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    with patch('src.shared.llm.get_llm_client') as mock_get_llm:
        mock_llm = MagicMock()
        mock_llm.chat_completion = AsyncMock()
        mock_get_llm.return_value = mock_llm

        unit_id_strs = [str(u.id) for u in sample_units]
        # One claim with one valid and one invalid evidence row
        claims_response = {
            "claims": [
                {
                    "text": "Valid claim text.",
                    "claim_type": "fact",
                    "evidence": [
                        {"unit_id": unit_id_strs[0], "stance": "supports", "confidence": 80},
                        {"unit_id": "not-a-valid-uuid", "stance": "supports", "confidence": 50},  # Invalid UUID
                        {"missing_unit_id": "foo", "stance": "neutral"},  # Missing unit_id
                    ]
                }
            ]
        }
        import json
        mock_llm.chat_completion = AsyncMock(return_value={
            "choices": [{"message": {"content": json.dumps(claims_response)}}]
        })
        mock_llm._parse_json_response.return_value = claims_response

        result = await extract_claims_for_story(mock_session, story_id)

        # Should create 1 claim and 1 valid evidence (2 invalid skipped)
        assert result["claims_created"] == 1
        assert result["evidence_created"] == 1
        assert len(result["errors"]) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])