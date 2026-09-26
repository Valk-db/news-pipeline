from datetime import datetime, timezone
from enum import Enum as PyEnum
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Enum, Index, UniqueConstraint, JSON, Boolean, Float
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, relationship, backref
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

    # Geolocation fields for globe visualization
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_type = Column(String(50), nullable=True)
    geonames_id = Column(String(50), nullable=True)
    geojson = Column(JSON, nullable=True)
    geometry_id = Column(UUID(as_uuid=True), ForeignKey("event_geometries.id", ondelete="SET NULL"), nullable=True)
    layer_id = Column(UUID(as_uuid=True), ForeignKey("event_layers.id", ondelete="SET NULL"), nullable=True)

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


class EventType(str, PyEnum):
    CONFLICT = "conflict"
    PROTEST = "protest"
    ELECTION = "election"
    DISASTER = "disaster"
    ACCIDENT = "accident"
    POLITICAL = "political"
    ECONOMIC = "economic"
    HEALTH = "health"
    ENVIRONMENTAL = "environmental"
    CRIME = "crime"
    SPORTS = "sports"
    CULTURAL = "cultural"
    SCIENTIFIC = "scientific"
    OTHER = "other"


class EventGeometryType(str, PyEnum):
    POINT = "point"
    POLYGON = "polygon"
    LINESTRING = "linestring"
    MULTIPOINT = "multipoint"
    MULTIPOLYGON = "multipolygon"
    MULTILINESTRING = "multilinestring"
    GEOMETRYCOLLECTION = "geometrycollection"


class EventGeometry(Base):
    """Geometry data for events (points, polygons, etc.)."""
    __tablename__ = "event_geometries"
    __table_args__ = (
        Index("ix_event_geometries_event_id", "event_id"),
    )

    GeometryType = EventGeometryType

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    geometry_type = Column(Enum(EventGeometryType, name="geometrytype"), nullable=False)
    geojson = Column(JSON, nullable=False)  # Full GeoJSON geometry object
    properties = Column(JSON, nullable=True)  # Additional properties
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    # Relationship - explicitly specify foreign_keys to disambiguate
    event = relationship("Event", back_populates="geometry", foreign_keys="Event.geometry_id")


class EventLayer(Base):
    """Layer configuration for globe visualization."""
    __tablename__ = "event_layers"
    __table_args__ = (
        Index("ix_event_layers_name", "name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    filter_criteria = Column(JSON, nullable=False, default={})  # e.g., {"event_type": ["conflict"], "min_confidence": 0.7}
    style = Column(JSON, nullable=False, default={})  # e.g., {"color": "red", "radius": 10000}
    is_default = Column(Boolean, nullable=False, default=False)
    is_visible = Column(Boolean, nullable=False, default=True)
    min_zoom = Column(Integer, nullable=False, default=0)
    max_zoom = Column(Integer, nullable=False, default=20)
    color = Column(String(7), nullable=False, default="#3b82f6")  # Hex color
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    # Relationship
    events = relationship("Event", back_populates="layer")


class Event(Base):
    """Event on the globe - linked to stories and geography."""
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_story_id", "story_id"),
        Index("ix_events_layer_id", "layer_id"),
        Index("ix_events_event_type", "event_type"),
        Index("ix_events_location", "latitude", "longitude"),
        Index("ix_events_start_time", "start_time"),
    )

    class EventType(str, PyEnum):
        CONFLICT = "conflict"
        PROTEST = "protest"
        ELECTION = "election"
        DISASTER = "disaster"
        ACCIDENT = "accident"
        POLITICAL = "political"
        ECONOMIC = "economic"
        HEALTH = "health"
        ENVIRONMENTAL = "environmental"
        CRIME = "crime"
        SPORTS = "sports"
        CULTURAL = "cultural"
        SCIENTIFIC = "scientific"
        OTHER = "other"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    location_name = Column(String(255), nullable=True)
    location_type = Column(String(50), nullable=True)  # city, country, region, etc.
    radius_km = Column(Float, nullable=True)
    start_time = Column(DateTime(timezone=True), nullable=False)
    event_type = Column(Enum(EventType, name="eventtype"), nullable=False, default=EventType.OTHER)
    confidence = Column(Float, nullable=False, default=0.5)
    source_count = Column(Integer, nullable=False, default=0)
    tier1_source_count = Column(Integer, nullable=False, default=0)
    entities = Column(JSON, nullable=True)  # Aggregated entities from articles
    geometry_id = Column(UUID(as_uuid=True), ForeignKey("event_geometries.id", ondelete="SET NULL"), nullable=True)
    layer_id = Column(UUID(as_uuid=True), ForeignKey("event_layers.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    # Relationships - explicitly specify foreign_keys
    story = relationship("Story", backref="events")
    geometry = relationship("EventGeometry", back_populates="event", uselist=False, foreign_keys=[geometry_id])
    layer = relationship("EventLayer", back_populates="events", foreign_keys=[layer_id])


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


class SourceReliabilitySnapshot(Base):
    """Daily reliability score snapshot for a source."""
    __tablename__ = "source_reliability_snapshots"
    __table_args__ = (
        Index("ix_source_reliability_source_date", "source_domain", "snapshot_date"),
        Index("ix_source_reliability_date", "snapshot_date"),
        UniqueConstraint("source_domain", "snapshot_date", name="uq_source_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_domain = Column(String(255), nullable=False)
    snapshot_date = Column(DateTime(timezone=True), nullable=False)  # UTC midnight

    # Core scores (0-100)
    factual_accuracy = Column(Integer, nullable=True)      # % verified claims rated True/Mostly True
    correction_rate = Column(Integer, nullable=True)       # Corrections per 100 articles (inverted)
    consensus_alignment = Column(Integer, nullable=True)   # Cosine similarity vs tier-1 median
    transparency_score = Column(Integer, nullable=True)    # Binary flags composite

    # Composite score (0-100)
    reliability_score = Column(Integer, nullable=False)    # Weighted composite

    # Supporting metrics
    total_claims_verified = Column(Integer, nullable=False, default=0)
    claims_true = Column(Integer, nullable=False, default=0)
    claims_false = Column(Integer, nullable=False, default=0)
    claims_mixed = Column(Integer, nullable=False, default=0)
    corrections_count = Column(Integer, nullable=False, default=0)
    articles_sampled = Column(Integer, nullable=False, default=0)

    # Metadata
    tier_at_snapshot = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class FactCheckRecord(Base):
    """Record of a fact-check performed on a claim from a source."""
    __tablename__ = "fact_check_records"
    __table_args__ = (
        Index("ix_fact_check_source_date", "source_domain", "checked_at"),
        Index("ix_fact_check_verdict", "verdict"),
        Index("ix_fact_check_claim_hash", "claim_hash"),
    )

    class Verdict(str, PyEnum):
        TRUE = "true"
        MOSTLY_TRUE = "mostly_true"
        MIXED = "mixed"
        MOSTLY_FALSE = "mostly_false"
        FALSE = "false"
        UNVERIFIED = "unverified"

    class FactChecker(str, PyEnum):
        CLAIMBUSTER = "claimbuster"
        LLM_VERIFIER = "llm_verifier"
        CLAIMREVIEW = "claimreview"
        MANUAL = "manual"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_domain = Column(String(255), nullable=False)
    article_id = Column(UUID(as_uuid=True), ForeignKey("raw_articles.id", ondelete="SET NULL"), nullable=True)
    claim = Column(Text, nullable=False)
    claim_hash = Column(String(64), nullable=False)  # SHA256 for dedup

    verdict = Column(Enum(Verdict), nullable=False)
    confidence = Column(Integer, nullable=False, default=50)  # 0-100

    fact_checker = Column(Enum(FactChecker), nullable=False)
    fact_checker_url = Column(Text, nullable=True)  # URL to fact-check source
    explanation = Column(Text, nullable=True)

    checked_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    claim_date = Column(DateTime(timezone=True), nullable=True)  # When claim was made

    article = relationship("RawArticle")


class CorrectionRecord(Base):
    """Record of a correction issued by a source."""
    __tablename__ = "correction_records"
    __table_args__ = (
        Index("ix_correction_source_date", "source_domain", "correction_date"),
        Index("ix_correction_article", "article_id"),
        Index("ix_correction_severity", "severity"),
    )

    class Severity(str, PyEnum):
        MINOR = "minor"          # Typos, formatting
        MODERATE = "moderate"    # Factual errors corrected
        MAJOR = "major"          # Significant factual changes
        RETRACTION = "retraction"  # Article retracted

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_domain = Column(String(255), nullable=False)
    article_id = Column(UUID(as_uuid=True), ForeignKey("raw_articles.id", ondelete="SET NULL"), nullable=True)

    original_text = Column(Text, nullable=False)
    corrected_text = Column(Text, nullable=False)
    correction_summary = Column(Text, nullable=True)

    severity = Column(Enum(Severity), nullable=False, default=Severity.MODERATE)
    correction_date = Column(DateTime(timezone=True), nullable=False)
    correction_url = Column(Text, nullable=True)  # URL to correction notice

    detected_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    article = relationship("RawArticle")


class ClaimType(str, PyEnum):
    FACT = "fact"
    ALLEGATION = "allegation"
    PREDICTION = "prediction"
    QUOTE = "quote"


class Claim(Base):
    """An atomic factual assertion extracted from a story's reporting units.

    Distinct from FactCheckRecord (verdicts from external fact-checkers) --
    this is the pipeline's own claim-extraction output, feeding the per-story
    claim matrix described in GRAND_PLAN.md §2.
    """
    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_story_id", "story_id"),
        Index("ix_claims_claim_type", "claim_type"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text, nullable=False)
    claim_type = Column(Enum(ClaimType), nullable=False, default=ClaimType.FACT)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    story = relationship("Story")
    evidence = relationship("ClaimEvidence", back_populates="claim", cascade="all, delete-orphan")


class ClaimStance(str, PyEnum):
    SUPPORTS = "supports"
    DISPUTES = "disputes"
    NEUTRAL = "neutral"


class ClaimEvidence(Base):
    """Edge: which reporting unit said what about which claim.

    This is the "source A says X, source B says Y" matrix -- one row per
    (claim, unit) pair.
    """
    __tablename__ = "claim_evidence"
    __table_args__ = (
        UniqueConstraint("claim_id", "unit_id", name="uq_claim_unit"),
        Index("ix_claim_evidence_unit_id", "unit_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    claim_id = Column(UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    unit_id = Column(UUID(as_uuid=True), ForeignKey("reporting_units.id", ondelete="CASCADE"), nullable=False)
    stance = Column(Enum(ClaimStance), nullable=False, default=ClaimStance.NEUTRAL)
    confidence = Column(Integer, nullable=False, default=50)  # 0-100, matches FactCheckRecord's convention
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    claim = relationship("Claim", back_populates="evidence")
    unit = relationship("ReportingUnit")


class EdgePredicate(str, PyEnum):
    CORROBORATES = "corroborates"
    DISPUTES = "disputes"
    SAME_EVENT_AS = "same_event_as"
    PART_OF_NARRATIVE = "part_of_narrative"
    CAUSED_BY = "caused_by"


class EntityEdge(Base):
    """Generic typed edge between two things in the graph.

    Polymorphic on purpose (subject_type/object_type + a UUID, no FK
    constraint on subject_id/object_id) so this one table can hold
    story-to-story narrative links now and claim-to-claim or entity-to-entity
    edges later without a schema change. Phase 2 (P2-D) only populates
    subject_type="story"/object_type="story" edges -- validate the type
    strings at the application layer, not the database layer.
    """
    __tablename__ = "entity_edges"
    __table_args__ = (
        Index("ix_entity_edges_subject", "subject_type", "subject_id"),
        Index("ix_entity_edges_object", "object_type", "object_id"),
        Index("ix_entity_edges_predicate", "predicate"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_type = Column(String(20), nullable=False)  # "story" for now; "claim"/"entity" later
    subject_id = Column(UUID(as_uuid=True), nullable=False)
    predicate = Column(Enum(EdgePredicate), nullable=False)
    object_type = Column(String(20), nullable=False)
    object_id = Column(UUID(as_uuid=True), nullable=False)
    confidence = Column(Integer, nullable=False, default=50)  # 0-100
    source_unit_id = Column(UUID(as_uuid=True), ForeignKey("reporting_units.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class TopicGroup(Base):
    """Hierarchical topic group, e.g. Geopolitics -> Middle East -> Israel-Gaza."""
    __tablename__ = "topic_groups"
    __table_args__ = (
        UniqueConstraint("name", "parent_group_id", name="uq_topic_group_name_parent"),
        Index("ix_topic_groups_parent", "parent_group_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    parent_group_id = Column(UUID(as_uuid=True), ForeignKey("topic_groups.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    children = relationship("TopicGroup", backref=backref("parent", remote_side=[id]))


class StoryTopicGroup(Base):
    """Many-to-many: stories <-> topic_groups, with an assignment confidence."""
    __tablename__ = "story_topic_groups"
    __table_args__ = (UniqueConstraint("story_id", "topic_group_id", name="uq_story_topic_group"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    topic_group_id = Column(UUID(as_uuid=True), ForeignKey("topic_groups.id", ondelete="CASCADE"), nullable=False)
    confidence = Column(Integer, nullable=True)  # 0-100, null if manually assigned
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class SourceTopicReliability(Base):
    """Per-(source_domain, topic_group) reliability score.

    Generalizes SourceReliabilitySnapshot (global per-source) to per-topic.
    Populated by P3-B claim-consensus scoring job, not FactCheckRecord.
    """
    __tablename__ = "source_topic_reliability"
    __table_args__ = (
        UniqueConstraint("source_domain", "topic_group_id", "snapshot_date", name="uq_str_sd_tg"),
        Index("ix_str_lookup", "source_domain", "topic_group_id"),
        Index("ix_str_date", "snapshot_date"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_domain = Column(String(255), nullable=False)
    topic_group_id = Column(UUID(as_uuid=True), ForeignKey("topic_groups.id", ondelete="CASCADE"), nullable=False)
    score = Column(Integer, nullable=False)  # 0-100, same convention as SourceReliabilitySnapshot
    sample_size = Column(Integer, nullable=False, default=0)
    snapshot_date = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
