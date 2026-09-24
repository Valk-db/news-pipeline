from datetime import datetime, timezone
from enum import Enum as PyEnum
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Enum, Index, UniqueConstraint, JSON
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, relationship
import uuid

Base = declarative_base()


class SourceTier(str, PyEnum):
    TIER1 = "tier1"      # BBC, Guardian, NPR, AP, Reuters (verified editorial)
    TIER2 = "tier2"      # National/regional outlets, reputable aggregators
    TIER3 = "tier3"      # Social, forums, unverified
    TIER4 = "tier4"      # Niche, hyperlocal, experimental sources


class RawArticle(Base):
    __tablename__ = "raw_articles"
    __table_args__ = (
        Index("ix_raw_articles_published_at", "published_at"),
        Index("ix_raw_articles_source_domain", "source_domain"),
        Index("ix_raw_articles_url_hash", "url_hash", unique=True),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url = Column(Text, nullable=False)
    url_hash = Column(String(64), nullable=False)  # SHA256 hex
    title = Column(Text, nullable=False)
    body_text = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    source_domain = Column(String(255), nullable=False)
    source_tier = Column(Enum(SourceTier), nullable=False, default=SourceTier.TIER3)
    published_at = Column(DateTime(timezone=True), nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    entities = Column(JSON, nullable=True)  # {"PERSON": [...], "ORG": [...], "GPE": [...]}
    minhash_signature = Column(JSON, nullable=True)  # MinHash serialized
    content_hash = Column(String(64), nullable=True)  # For exact dedup
    reporting_unit_id = Column(UUID(as_uuid=True), ForeignKey("reporting_units.id"), nullable=True)


class ReportingUnit(Base):
    """A cluster of near-duplicate articles (syndication, verbatim repub)."""
    __tablename__ = "reporting_units"
    __table_args__ = (
        Index("ix_reporting_units_day", "day"),
        Index("ix_reporting_units_rep_article_id", "representative_article_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    day = Column(DateTime(timezone=True), nullable=False)  # Date bucket (UTC midnight)
    representative_article_id = Column(UUID(as_uuid=True), ForeignKey("raw_articles.id"), nullable=False)
    article_count = Column(Integer, nullable=False, default=1)
    source_tiers = Column(JSON, nullable=False)  # {"tier1": 2, "tier2": 1}
    owner_groups = Column(JSON, nullable=False)  # {"AP": 1, "Sinclair": 3, ...}
    tier1_owner_groups = Column(JSON, nullable=False, default={})  # Only owners from tier-1 articles
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    representative = relationship("RawArticle", foreign_keys=[representative_article_id])


class Story(Base):
    """A semantic group of reporting units covering the same event."""
    __tablename__ = "stories"
    __table_args__ = (
        Index("ix_stories_day", "day"),
        Index("ix_stories_status", "status"),
        Index("ix_stories_viewpoint_cluster", "viewpoint_cluster_id"),
    )

    class Status(str, PyEnum):
        PENDING = "pending"      # Awaiting curation
        QUEUED = "queued"        # Approved, ready to post
        POSTED = "posted"        # Published
        REJECTED = "rejected"    # Curator rejected
        BLOCKED = "blocked"      # Failed gate (e.g., single-source)
        EXPIRED = "expired"      # Stale story auto-closed by cleanup job

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    day = Column(DateTime(timezone=True), nullable=False)
    primary_entities = Column(JSON, nullable=False)  # Top-N entity sets that defined this story
    tier1_unit_count = Column(Integer, nullable=False, default=0)
    tier2_unit_count = Column(Integer, nullable=False, default=0)
    tier3_unit_count = Column(Integer, nullable=False, default=0)
    tier4_unit_count = Column(Integer, nullable=False, default=0)
    distinct_owners = Column(Integer, nullable=False, default=0)
    viewpoint_cluster_id = Column(UUID(as_uuid=True), nullable=True)  # Groups stories by perspective
    status = Column(Enum(Status), nullable=False, default=Status.PENDING)
    gate_reason = Column(Text, nullable=True)  # Why blocked/queued
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    units = relationship(
        "ReportingUnit",
        secondary="story_unit_links",
        backref="stories",
        lazy="selectin",
    )


class StoryUnitLink(Base):
    """Many-to-many: stories ↔ reporting_units."""
    __tablename__ = "story_unit_links"
    __table_args__ = (UniqueConstraint("story_id", "unit_id", name="uq_story_unit"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    unit_id = Column(UUID(as_uuid=True), ForeignKey("reporting_units.id", ondelete="CASCADE"), nullable=False)


class CuratedPost(Base):
    """Final post ready for publishing."""
    __tablename__ = "curated_posts"
    __table_args__ = (Index("ix_curated_posts_status", "status"),)

    class Status(str, PyEnum):
        DRAFT = "draft"
        APPROVED = "approved"
        POSTED = "posted"
        FAILED = "failed"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id"), nullable=False)
    platform = Column(String(50), nullable=False)  # twitter, instagram, linkedin, threads, bluesky
    caption = Column(Text, nullable=False)
    media_urls = Column(JSON, nullable=True)  # [{"type": "image", "url": "...", "alt": "..."}]
    source_urls = Column(JSON, nullable=False)  # Canonical source URLs for attribution
    status = Column(Enum(Status, name="curated_post_status"), nullable=False, default=Status.DRAFT)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    posted_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class StatusLog(Base):
    """Pipeline run log — committed back to repo each run."""
    __tablename__ = "status_log"
    __table_args__ = (Index("ix_status_log_run_at", "run_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    phase = Column(String(50), nullable=False)  # ingest, verify, group, curate
    status = Column(String(20), nullable=False)  # ok, warn, error
    details = Column(JSON, nullable=True)
    commit_sha = Column(String(40), nullable=True)


class CanonicalEntity(Base):
    """Canonical entity representing a real-world entity with a stable ID."""
    __tablename__ = "canonical_entities"
    __table_args__ = (
        Index("ix_canonical_entities_type", "entity_type"),
        Index("ix_canonical_entities_name", "canonical_name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_name = Column(String(255), nullable=False)  # Preferred display name
    entity_type = Column(String(50), nullable=False)  # PERSON, ORG, GPE, LOC, EVENT, PRODUCT
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationship
    aliases = relationship("EntityAlias", back_populates="canonical_entity", cascade="all, delete-orphan")


class EntityAlias(Base):
    """Alias/alternative name for a canonical entity."""
    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("canonical_entity_id", "alias", name="uq_canonical_alias"),
        Index("ix_entity_aliases_alias", "alias"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    canonical_entity_id = Column(UUID(as_uuid=True), ForeignKey("canonical_entities.id", ondelete="CASCADE"), nullable=False)
    alias = Column(String(255), nullable=False)  # Alternative surface form
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    canonical_entity = relationship("CanonicalEntity", back_populates="aliases")


class ArticleEmbedding(Base):
    """Vector embedding for an article (for semantic search/similarity)."""
    __tablename__ = "article_embeddings"
    __table_args__ = (
        Index("ix_article_embeddings_article_id", "article_id"),
        Index("ix_article_embeddings_model", "model"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id = Column(UUID(as_uuid=True), ForeignKey("raw_articles.id", ondelete="CASCADE"), nullable=False)
    model = Column(String(100), nullable=False)  # e.g., "sentence-transformers/all-MiniLM-L6-v2"
    embedding = Column(JSON, nullable=False)  # Vector as JSON array (pgvector handles this)
    dimensions = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    article = relationship("RawArticle")


class StoryEmbedding(Base):
    """Vector embedding for a story (aggregated from articles)."""
    __tablename__ = "story_embeddings"
    __table_args__ = (
        Index("ix_story_embeddings_story_id", "story_id"),
        Index("ix_story_embeddings_model", "model"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    model = Column(String(100), nullable=False)
    embedding = Column(JSON, nullable=False)
    dimensions = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    story = relationship("Story")


class Snippet(Base):
    """Key quote/snippet extracted from an article with attribution."""
    __tablename__ = "snippets"
    __table_args__ = (
        Index("ix_snippets_story_id", "story_id"),
        Index("ix_snippets_article_id", "article_id"),
        Index("ix_snippets_type", "snippet_type"),
    )

    class SnippetType(str, PyEnum):
        QUOTE = "quote"          # Direct quote from article
        STAT = "stat"            # Statistical claim
        FACT = "fact"            # Factual claim
        SUMMARY = "summary"      # Summary sentence
        CLAIM = "claim"          # Assertion/claim

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    article_id = Column(UUID(as_uuid=True), ForeignKey("raw_articles.id", ondelete="CASCADE"), nullable=False)
    snippet_type = Column(Enum(SnippetType), nullable=False, default=SnippetType.QUOTE)
    text = Column(Text, nullable=False)
    position = Column(Integer, nullable=True)  # Character position in article
    entities = Column(JSON, nullable=True)  # Entities mentioned in snippet
    minhash_signature = Column(JSON, nullable=True)  # For dedup
    confidence = Column(Integer, nullable=False, default=100)  # 0-100 confidence
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    story = relationship("Story")
    article = relationship("RawArticle")


class MediaAsset(Base):
    """Media (image, video, audio) associated with an article or story."""
    __tablename__ = "media_assets"
    __table_args__ = (
        Index("ix_media_assets_article_id", "article_id"),
        Index("ix_media_assets_story_id", "story_id"),
        Index("ix_media_assets_type", "media_type"),
    )

    class MediaType(str, PyEnum):
        IMAGE = "image"
        VIDEO = "video"
        AUDIO = "audio"
        EMBED = "embed"  # Social media embed, iframe, etc.

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id = Column(UUID(as_uuid=True), ForeignKey("raw_articles.id", ondelete="CASCADE"), nullable=True)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=True)
    media_type = Column(Enum(MediaType), nullable=False)
    url = Column(Text, nullable=False)
    thumbnail_url = Column(Text, nullable=True)
    alt_text = Column(Text, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration_seconds = Column(Integer, nullable=True)  # For video/audio
    source = Column(String(100), nullable=True)  # youtube, vimeo, article, etc.
    source_id = Column(String(100), nullable=True)  # Platform-specific ID
    meta_data = Column(JSON, nullable=True)  # Additional platform-specific metadata
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    article = relationship("RawArticle")
    story = relationship("Story")