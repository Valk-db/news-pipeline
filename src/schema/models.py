from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Enum, Index, UniqueConstraint, Boolean, JSON
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import JSON
from pgvector.sqlalchemy import Vector
import uuid

Base = declarative_base()


class SourceTier(str, PyEnum):
    TIER1 = "tier1"      # BBC, Guardian, NPR, AP, Reuters (verified editorial)
    TIER2 = "tier2"      # Other outlets, aggregators
    TIER3 = "tier3"      # Social, forums, unverified


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
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
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
    day = Column(DateTime, nullable=False)  # Date bucket (UTC midnight)
    representative_article_id = Column(UUID(as_uuid=True), ForeignKey("raw_articles.id"), nullable=False)
    article_count = Column(Integer, nullable=False, default=1)
    source_tiers = Column(JSON, nullable=False)  # {"tier1": 2, "tier2": 1}
    owner_groups = Column(JSON, nullable=False)  # {"AP": 1, "Sinclair": 3, ...}
    tier1_owner_groups = Column(JSON, nullable=False, default={})  # Only owners from tier-1 articles
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    representative = relationship("RawArticle", foreign_keys=[representative_article_id])


class Story(Base):
    """A semantic group of reporting units covering the same event."""
    __tablename__ = "stories"
    __table_args__ = (
        Index("ix_stories_day", "day"),
        Index("ix_stories_status", "status"),
    )

    class Status(str, PyEnum):
        PENDING = "pending"      # Awaiting curation
        QUEUED = "queued"        # Approved, ready to post
        POSTED = "posted"        # Published
        REJECTED = "rejected"    # Curator rejected
        BLOCKED = "blocked"      # Failed gate (e.g., single-source)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    day = Column(DateTime, nullable=False)
    primary_entities = Column(JSON, nullable=False)  # Top-N entity sets that defined this story
    tier1_unit_count = Column(Integer, nullable=False, default=0)
    tier2_unit_count = Column(Integer, nullable=False, default=0)
    distinct_owners = Column(Integer, nullable=False, default=0)
    status = Column(Enum(Status), nullable=False, default=Status.PENDING)
    gate_reason = Column(Text, nullable=True)  # Why blocked/queued
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    status = Column(Enum(Status), nullable=False, default=Status.DRAFT)
    scheduled_at = Column(DateTime, nullable=True)
    posted_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class StatusLog(Base):
    """Pipeline run log — committed back to repo each run."""
    __tablename__ = "status_log"
    __table_args__ = (Index("ix_status_log_run_at", "run_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    phase = Column(String(50), nullable=False)  # ingest, verify, group, curate
    status = Column(String(20), nullable=False)  # ok, warn, error
    details = Column(JSON, nullable=True)
    commit_sha = Column(String(40), nullable=True)