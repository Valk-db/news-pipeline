"""Tests for stealth-edit and correction tracking (src/verification/revisions.py).

Uses the shared in-memory SQLite session from conftest.py, so these run the real
INSERTs against the real article_revisions / article_corrections tables rather than a
mock: the interesting failure modes here are a row written when none should be, and a
row missing when one should exist.
"""

import uuid
from datetime import datetime, timedelta, UTC
from typing import Dict, List, Optional

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typing import AsyncGenerator

from src.schema.models import (
    ArticleCorrection,
    ArticleRevision,
    RawArticle,
    SourceTier,
    StatusLog,
)
from src.utils.trafilatura_extract import compute_content_hash
from src.verification.revisions import (
    RevisionOutcome,
    RevisionScanResult,
    detect_corrections,
    extract_displayed_timestamp,
    record_revision,
    scan_revisions,
    summarize_diff,
)

ORIGINAL_BODY = (
    "The city council voted 12 to 3 on Tuesday to approve the transit plan.\n"
    "\n"
    "Council member Ada Reyes said the funding gap would be closed by spring.\n"
    "\n"
    "The plan takes effect in July."
)

SILENT_EDIT_BODY = (
    "The city council voted 9 to 6 on Tuesday to approve the transit plan.\n"
    "\n"
    "Council member Ada Reyes said the funding gap would be closed by spring.\n"
    "\n"
    "The plan takes effect in July."
)

CORRECTED_BODY = (
    "Correction: an earlier version said the vote was 12 to 3. It was 9 to 6.\n"
    "\n"
    "The city council voted 9 to 6 on Tuesday to approve the transit plan.\n"
    "\n"
    "Council member Ada Reyes said the funding gap would be closed by spring.\n"
    "\n"
    "The plan takes effect in July."
)

TIMESTAMPED_BODY = (
    "The city council voted 9 to 6 on Tuesday to approve the transit plan.\n"
    "\n"
    "Updated: 2026-09-30 14:03\n"
    "\n"
    "Council member Ada Reyes said the funding gap would be closed by spring.\n"
    "\n"
    "The plan takes effect in July."
)

# Same edit, no notice and no "Updated" wording: the displayed timestamp arrives
# out of band (a meta tag, or the extractor's date field), which is the case where
# the timestamp comparison is the only disclosure evidence there is.
METADATA_STAMPED_BODY = (
    "The city council voted 9 to 6 on Tuesday to approve the transit plan.\n"
    "\n"
    "Council member Ada Reyes said the funding gap would be closed by spring.\n"
    "\n"
    "The plan takes effect in July."
)


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Use the test database session from conftest.py"""
    async_session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


async def _make_article(
    session: AsyncSession,
    body: Optional[str] = ORIGINAL_BODY,
    url: Optional[str] = None,
    domain: str = "apnews.com",
    fetched_at: Optional[datetime] = None,
    content_hash: Optional[str] = None,
) -> RawArticle:
    """An ingested article: body_text and content_hash are what the page served then.

    body=None reproduces an article whose body was NULLed by the retention job while
    its content_hash stayed: the hash is still enough to notice a change.
    """
    article = RawArticle(
        id=uuid.uuid4(),
        url=url or f"https://{domain}/article/{uuid.uuid4().hex[:8]}",
        url_hash=uuid.uuid4().hex,
        title="Council approves transit plan",
        body_text=body,
        source_domain=domain,
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(UTC) - timedelta(hours=6),
        fetched_at=fetched_at or datetime.now(UTC) - timedelta(hours=5),
        content_hash=content_hash or compute_content_hash(body or ""),
    )
    session.add(article)
    await session.flush()
    return article


async def _count(session: AsyncSession, model) -> int:
    rows = await session.execute(select(func.count()).select_from(model))
    return int(rows.scalar_one())


async def _revisions(session: AsyncSession, article_id) -> List[ArticleRevision]:
    stmt = (
        select(ArticleRevision)
        .where(ArticleRevision.article_id == article_id)
        .order_by(ArticleRevision.revision_number)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _corrections(session: AsyncSession, article_id) -> List[ArticleCorrection]:
    stmt = select(ArticleCorrection).where(ArticleCorrection.article_id == article_id)
    return list((await session.execute(stmt)).scalars().all())


class TestRecordRevision:
    """The recorder: hash first, write only when the hash moved."""

    @pytest.mark.asyncio
    async def test_identical_refetch_creates_no_revision(self, db_session):
        """Same bytes as ingestion: nothing to record, and nothing is written."""
        article = await _make_article(db_session)

        outcome = await record_revision(db_session, article, ORIGINAL_BODY)

        assert outcome.changed is False
        assert outcome.reason == "unchanged"
        assert outcome.revision_id is None
        assert outcome.content_hash == article.content_hash
        assert await _count(db_session, ArticleRevision) == 0
        assert await _count(db_session, ArticleCorrection) == 0

    @pytest.mark.asyncio
    async def test_repeated_refetch_of_an_unchanged_page_still_writes_nothing(
        self, db_session
    ):
        """A second identical refetch after a recorded edit is also a no-op."""
        article = await _make_article(db_session)
        await record_revision(db_session, article, SILENT_EDIT_BODY)
        assert await _count(db_session, ArticleRevision) == 1

        outcome = await record_revision(db_session, article, SILENT_EDIT_BODY)

        assert outcome.changed is False
        assert await _count(db_session, ArticleRevision) == 1

    @pytest.mark.asyncio
    async def test_changed_content_creates_a_revision_with_a_diff_summary(self, db_session):
        """A changed paragraph produces one row carrying the diff summary."""
        article = await _make_article(db_session)

        outcome = await record_revision(db_session, article, SILENT_EDIT_BODY)
        await db_session.commit()

        assert outcome.changed is True
        assert outcome.revision_id is not None
        assert outcome.content_hash == compute_content_hash(SILENT_EDIT_BODY)

        revisions = await _revisions(db_session, article.id)
        assert len(revisions) == 1
        revision = revisions[0]
        assert revision.revision_number == 1
        assert revision.previous_revision_id is None
        assert revision.content_hash == compute_content_hash(SILENT_EDIT_BODY)
        assert revision.changed_paragraphs == 1
        assert "-The city council voted 12 to 3" in revision.diff_excerpt
        assert "+The city council voted 9 to 6" in revision.diff_excerpt
        assert revision.diff_truncated is False
        assert revision.correction_count == 0
        assert revision.log_index is None
        assert revision.fetched_at is not None

    @pytest.mark.asyncio
    async def test_a_second_change_links_to_the_previous_revision(self, db_session):
        """Revisions chain by number and previous_revision_id."""
        article = await _make_article(db_session)
        first = await record_revision(db_session, article, SILENT_EDIT_BODY)
        second = await record_revision(
            db_session,
            article,
            SILENT_EDIT_BODY + "\n\nAn extra paragraph added later.",
            previous_content=SILENT_EDIT_BODY,
        )
        await db_session.commit()

        revisions = await _revisions(db_session, article.id)
        assert [r.revision_number for r in revisions] == [1, 2]
        assert revisions[1].previous_revision_id == first.revision_id
        assert second.revision_id == revisions[1].id
        # The caller supplied the previous text, so the diff is computable.
        assert revisions[1].changed_paragraphs == 1

    @pytest.mark.asyncio
    async def test_repeated_whitespace_is_not_an_edit(self, db_session):
        """A re-wrap of the same sentences leaves the content hash alone."""
        article = await _make_article(db_session)
        rewrapped = " ".join(ORIGINAL_BODY.split())

        outcome = await record_revision(db_session, article, rewrapped)

        assert outcome.changed is False
        assert await _count(db_session, ArticleRevision) == 0

    @pytest.mark.asyncio
    async def test_empty_extraction_is_not_an_edit(self, db_session):
        """A page that extracts to nothing is no observation, not a deletion."""
        article = await _make_article(db_session)

        outcome = await record_revision(db_session, article, "   ")

        assert outcome.changed is False
        assert outcome.reason == "empty_content"
        assert await _count(db_session, ArticleRevision) == 0

    @pytest.mark.asyncio
    async def test_accepts_an_article_id_and_a_url(self, db_session):
        """The recorder resolves an article from an id or a url."""
        article = await _make_article(db_session)

        by_id = await record_revision(db_session, str(article.id), SILENT_EDIT_BODY)
        by_url = await record_revision(
            db_session,
            article.url,
            CORRECTED_BODY,
            previous_content=SILENT_EDIT_BODY,
        )
        await db_session.commit()

        assert by_id.article_id == article.id
        assert by_url.article_id == article.id
        assert (await _count(db_session, ArticleRevision)) == 2

    @pytest.mark.asyncio
    async def test_unknown_article_raises(self, db_session):
        with pytest.raises(ValueError):
            await record_revision(db_session, "https://nowhere.test/a", SILENT_EDIT_BODY)


class TestClassification:
    """Acknowledged vs stealth, and the correction rows that come with them."""

    @pytest.mark.asyncio
    async def test_correction_notice_is_acknowledged_and_records_a_correction(
        self, db_session
    ):
        """A correction notice at the top acknowledges the edit and is its own event."""
        article = await _make_article(db_session)

        outcome = await record_revision(db_session, article, CORRECTED_BODY)
        await db_session.commit()

        assert outcome.changed is True
        assert outcome.change_kind == ArticleRevision.ChangeKind.ACKNOWLEDGED
        assert outcome.reason == "correction_notice"
        assert [s.label for s in outcome.corrections] == ["correction"]

        revision = (await _revisions(db_session, article.id))[0]
        assert revision.change_kind == ArticleRevision.ChangeKind.ACKNOWLEDGED
        assert revision.correction_count == 1

        corrections = await _corrections(db_session, article.id)
        assert len(corrections) == 1
        correction = corrections[0]
        assert correction.signal == "correction"
        assert correction.location == "top"
        assert "Correction: an earlier version said the vote was 12 to 3" in correction.snippet
        # The correction is linked to both the article and the revision it was found in.
        assert correction.article_id == article.id
        assert correction.revision_id == revision.id
        assert correction.detected_at is not None

    @pytest.mark.asyncio
    async def test_silent_change_is_stealth_and_records_no_correction(self, db_session):
        """Same factual change, no notice: STEALTH, and no correction row."""
        article = await _make_article(db_session)

        outcome = await record_revision(db_session, article, SILENT_EDIT_BODY)
        await db_session.commit()

        assert outcome.changed is True
        assert outcome.change_kind == ArticleRevision.ChangeKind.STEALTH
        assert outcome.reason == "silent_change"
        assert outcome.corrections == []

        revision = (await _revisions(db_session, article.id))[0]
        assert revision.change_kind == ArticleRevision.ChangeKind.STEALTH
        assert revision.correction_count == 0
        assert revision.changed_paragraphs == 1
        assert await _count(db_session, ArticleCorrection) == 0

    @pytest.mark.asyncio
    async def test_visible_updated_line_is_acknowledged(self, db_session):
        """An "Updated:" line at the top of the page is a disclosure on its own."""
        article = await _make_article(db_session)

        outcome = await record_revision(db_session, article, TIMESTAMPED_BODY)
        await db_session.commit()

        assert outcome.change_kind == ArticleRevision.ChangeKind.ACKNOWLEDGED
        assert outcome.displayed_at == datetime(2026, 9, 30, 14, 3, tzinfo=UTC)
        assert "updated" in [s.label for s in outcome.corrections]

    @pytest.mark.asyncio
    async def test_newer_displayed_timestamp_is_acknowledged(self, db_session):
        """A timestamp newer than the previous revision's acknowledges the edit,
        with no notice in the text to back it up."""
        article = await _make_article(db_session)
        earlier = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
        later = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

        first = await record_revision(
            db_session,
            article,
            METADATA_STAMPED_BODY,
            previous_content=ORIGINAL_BODY,
            displayed_at=earlier,
        )
        second = await record_revision(
            db_session,
            article,
            METADATA_STAMPED_BODY + "\n\nThe plan now starts in September.",
            previous_content=METADATA_STAMPED_BODY,
            displayed_at=later,
        )
        await db_session.commit()

        # No previous revision to compare against, so a stamp alone proves nothing.
        assert first.change_kind == ArticleRevision.ChangeKind.STEALTH
        assert first.corrections == []
        assert first.displayed_at == earlier
        # A stamp newer than the previous revision's is an admission of change.
        assert second.change_kind == ArticleRevision.ChangeKind.ACKNOWLEDGED
        assert second.reason == "displayed_timestamp"
        assert await _count(db_session, ArticleCorrection) == 0

    @pytest.mark.asyncio
    async def test_no_previous_text_still_classifies_and_says_so(self, db_session):
        """Without the old body (retention NULLed it) the verdict survives, the diff does not."""
        article = await _make_article(
            db_session, body=None, content_hash=compute_content_hash(ORIGINAL_BODY)
        )

        outcome = await record_revision(db_session, article, SILENT_EDIT_BODY)
        await db_session.commit()

        assert outcome.changed is True
        assert outcome.change_kind == ArticleRevision.ChangeKind.STEALTH
        assert outcome.reason == "silent_change_without_previous_text"
        assert outcome.diff is None
        revision = (await _revisions(db_session, article.id))[0]
        assert revision.changed_paragraphs == 0
        assert revision.diff_excerpt is None

    @pytest.mark.asyncio
    async def test_a_log_records_the_revision_and_the_row_keeps_its_index(self, db_session):
        """Hand the revision to the transparency log and keep the entry index on the row."""
        from src.transparency.log import InMemoryMerkleLog, verify_chain

        log = InMemoryMerkleLog()
        article = await _make_article(db_session)

        outcome = await record_revision(db_session, article, SILENT_EDIT_BODY, log=log)
        await db_session.commit()

        assert outcome.log_index == 0
        revision = (await _revisions(db_session, article.id))[0]
        assert revision.log_index == 0

        entries = await log.entries()
        assert verify_chain(entries)
        payload = entries[0].payload
        assert payload["type"] == "article_revision"
        assert payload["content_hash"] == revision.content_hash
        assert payload["change_kind"] == "stealth"
        # The fetch time is inside the payload: the log does not vouch for its own
        # timestamp column, so an entry that claims one must carry it in the hash.
        assert payload["fetched_at"] == revision.fetched_at.replace(
            tzinfo=UTC
        ).isoformat()

    @pytest.mark.asyncio
    async def test_corrections_block_counts_as_a_disclosure(self, db_session):
        """A corrections heading deep in the article acknowledges the change."""
        article = await _make_article(db_session)
        body = ORIGINAL_BODY + "\n\nCorrections\n\nAn earlier version misstated the vote count."

        outcome = await record_revision(db_session, article, body)
        await db_session.commit()

        assert outcome.change_kind == ArticleRevision.ChangeKind.ACKNOWLEDGED
        correction = (await _corrections(db_session, article.id))[0]
        assert correction.location == "corrections_block"
        assert "misstated the vote count" in correction.snippet

    @pytest.mark.asyncio
    async def test_configurable_patterns_replace_the_defaults(self, db_session):
        """A source-specific vocabulary can be passed in instead of the default set."""
        from src.verification.revisions import CorrectionPattern

        pattern = CorrectionPattern.compile("nachricht", r"\bnachricht\b")
        body = "Nachricht aktualisiert.\n\n" + SILENT_EDIT_BODY
        defaults_article = await _make_article(db_session, url="https://apnews.com/article/de")
        custom_article = await _make_article(db_session, url="https://apnews.com/article/fe")
        await db_session.commit()

        default_outcome = await record_revision(db_session, defaults_article, body)
        outcome = await record_revision(
            db_session,
            custom_article,
            body,
            patterns=(pattern,),
            top_patterns=(),
        )
        await db_session.commit()

        assert default_outcome.change_kind == ArticleRevision.ChangeKind.STEALTH
        assert outcome.change_kind == ArticleRevision.ChangeKind.ACKNOWLEDGED
        assert [s.label for s in outcome.corrections] == ["nachricht"]


class TestScanRevisions:
    """The batch job: dry runs write nothing, real runs record and report."""

    @staticmethod
    def _fetcher(bodies: Dict[str, str]):
        async def fetch(url: str) -> Optional[str]:
            return bodies.get(url)

        return fetch

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, db_session):
        """A dry run classifies and counts, but leaves every table untouched."""
        changed = await _make_article(db_session, url="https://apnews.com/article/changed")
        unchanged = await _make_article(
            db_session,
            url="https://reuters.com/article/unchanged",
            domain="reuters.com",
        )
        await db_session.commit()

        result = await scan_revisions(
            db_session,
            limit=10,
            fetch_text=self._fetcher(
                {
                    changed.url: SILENT_EDIT_BODY,
                    unchanged.url: ORIGINAL_BODY,
                }
            ),
            dry_run=True,
        )

        assert result.articles_scanned == 2
        assert result.revisions_created == 1
        assert result.revisions_stealth == 1
        assert result.articles_unchanged == 1
        assert result.fetch_failures == 0
        assert await _count(db_session, ArticleRevision) == 0
        assert await _count(db_session, ArticleCorrection) == 0
        # A dry run does not log the run either.
        assert await _count(db_session, StatusLog) == 0

    @pytest.mark.asyncio
    async def test_records_revisions_and_a_status_log_entry(self, db_session):
        """A real run writes the rows, counts them, and records the run."""
        edited = await _make_article(db_session, url="https://apnews.com/article/stealth")
        corrected = await _make_article(
            db_session, url="https://bbc.com/article/corrected", domain="bbc.com"
        )
        stable = await _make_article(
            db_session, url="https://npr.org/article/stable", domain="npr.org"
        )
        await db_session.commit()

        result = await scan_revisions(
            db_session,
            limit=10,
            fetch_text=self._fetcher(
                {
                    edited.url: SILENT_EDIT_BODY,
                    corrected.url: CORRECTED_BODY,
                    stable.url: ORIGINAL_BODY,
                }
            ),
        )

        assert result.articles_scanned == 3
        assert result.revisions_created == 2
        assert result.revisions_stealth == 1
        assert result.revisions_acknowledged == 1
        assert result.corrections_recorded == 1
        assert result.articles_unchanged == 1
        assert result.stealth_rate == pytest.approx(0.5)
        assert any("stealth" in detail for detail in result.details)

        assert await _count(db_session, ArticleRevision) == 2
        assert await _count(db_session, ArticleCorrection) == 1
        assert "acknowledged" in result.summary()

        log_rows = (
            await db_session.execute(
                select(StatusLog).where(StatusLog.phase == "revision_scan")
            )
        ).scalars().all()
        assert len(log_rows) == 1
        assert log_rows[0].details["revisions_created"] == 2

    @pytest.mark.asyncio
    async def test_fetch_failures_and_empty_extractions_do_not_write(self, db_session):
        """A dead URL is counted, not recorded, and does not end the batch."""
        dead = await _make_article(db_session, url="https://apnews.com/article/dead")
        await _make_article(db_session, url="https://apnews.com/article/gone")
        await db_session.commit()

        async def fetch(url: str) -> Optional[str]:
            if url == dead.url:
                raise RuntimeError("connection reset")
            return None

        result = await scan_revisions(db_session, limit=10, fetch_text=fetch)

        assert result.fetch_failures == 2
        assert result.revisions_created == 0
        assert await _count(db_session, ArticleRevision) == 0

    @pytest.mark.asyncio
    async def test_articles_outside_the_window_are_not_candidates(self, db_session):
        """Only recently ingested articles are re-fetched."""
        old = await _make_article(
            db_session,
            url="https://apnews.com/article/old",
            fetched_at=datetime.now(UTC) - timedelta(days=30),
        )
        recent = await _make_article(db_session, url="https://apnews.com/article/recent")
        await db_session.commit()
        fetched: List[str] = []

        async def fetch(url: str) -> Optional[str]:
            fetched.append(url)
            return SILENT_EDIT_BODY

        result = await scan_revisions(
            db_session, window_hours=24, limit=10, fetch_text=fetch
        )

        assert fetched == [recent.url]
        assert old.id not in {r.article_id for r in await _revisions(db_session, old.id)}
        assert result.revisions_created == 1

    @pytest.mark.asyncio
    async def test_limit_caps_the_batch(self, db_session):
        articles = [
            await _make_article(db_session, url=f"https://apnews.com/article/{index}")
            for index in range(3)
        ]
        await db_session.commit()

        async def fetch(url: str) -> Optional[str]:
            return SILENT_EDIT_BODY

        result = await scan_revisions(db_session, limit=1, fetch_text=fetch)

        assert result.articles_scanned == 1
        assert await _count(db_session, ArticleRevision) == 1
        assert len(articles) == 3

    @pytest.mark.asyncio
    async def test_rejects_a_non_positive_window_or_limit(self, db_session):
        with pytest.raises(ValueError):
            await scan_revisions(db_session, window_hours=0)
        with pytest.raises(ValueError):
            await scan_revisions(db_session, limit=0)


class TestHelpers:
    """Unit tests for the pure helpers the recorder is built on."""

    def test_detect_corrections_ignores_a_mid_article_updated(self):
        body = "Lead.\n\nSecond.\n\nThird.\n\nFourth.\n\nThe tally was updated after filing."
        assert detect_corrections(body) == []

    def test_detect_corrections_finds_a_top_notice(self):
        signals = detect_corrections("Updated: the vote was 9 to 6.\n\nBody paragraph.")
        assert [s.label for s in signals] == ["updated"]
        assert signals[0].location == "top"
        assert signals[0].paragraph_index == 0

    def test_detect_corrections_respects_the_signal_cap(self):
        body = "\n\n".join(["Updated"] * 9)
        assert len(detect_corrections(body, max_signals=2)) == 2

    def test_extract_displayed_timestamp_reads_the_last_match(self):
        content = "Published 2026-01-01\n\nUpdated: 2026-02-02 09:15\n\nUpdated: 2026-03-03 11:00"
        assert extract_displayed_timestamp(content) == datetime(
            2026, 3, 3, 11, 0, tzinfo=UTC
        )

    def test_extract_displayed_timestamp_returns_none_without_a_timestamp(self):
        assert extract_displayed_timestamp("Just a body paragraph.") is None

    def test_summarize_diff_caps_the_excerpt(self):
        previous = "\n\n".join(f"Paragraph {i} of the old text." for i in range(60))
        current = "\n\n".join(f"Paragraph {i} of the new text." for i in range(60))
        summary = summarize_diff(previous, current, max_excerpt_chars=200)

        assert summary.changed_paragraphs == 60
        assert summary.truncated is True
        assert len(summary.excerpt) <= 200
        assert "truncated at 200 chars" in summary.excerpt

    def test_scan_result_defaults(self):
        result = RevisionScanResult()
        assert result.revisions_created == 0
        assert result.details == []
        assert result.stealth_rate == 0.0
        assert "revisions: 0" in result.summary()

    def test_outcome_describe_reports_the_finding(self):
        outcome = RevisionOutcome(
            article_id=uuid.uuid4(),
            url="https://apnews.com/article/x",
            content_hash="a" * 64,
            changed=False,
            reason="unchanged",
        )
        assert outcome.correction_count == 0
        assert outcome.describe() == "https://apnews.com/article/x: unchanged"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
