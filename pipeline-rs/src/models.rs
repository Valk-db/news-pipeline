use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::FromRow;
use uuid::Uuid;

/// MinHash signature for near-duplicate detection
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MinHashSignature {
    pub hashvalues: Vec<u64>,
    pub num_perm: usize,
}

/// Source tier enum matching database
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "source_tier", rename_all = "lowercase")]
pub enum SourceTier {
    Tier1,
    Tier2,
    Tier3,
    Tier4,
}

impl Default for SourceTier {
    fn default() -> Self {
        SourceTier::Tier3
    }
}

/// Story status enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "story_status", rename_all = "lowercase")]
pub enum StoryStatus {
    Pending,
    Queued,
    Posted,
    Rejected,
    Blocked,
    Expired,
}

impl Default for StoryStatus {
    fn default() -> Self {
        StoryStatus::Pending
    }
}

/// Curated post status enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "curated_post_status", rename_all = "lowercase")]
pub enum CuratedPostStatus {
    Draft,
    Approved,
    Posted,
    Failed,
}

impl Default for CuratedPostStatus {
    fn default() -> Self {
        CuratedPostStatus::Draft
    }
}

/// Event type enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "event_type", rename_all = "lowercase")]
pub enum EventType {
    Conflict,
    Protest,
    Election,
    Disaster,
    Accident,
    Political,
    Economic,
    Health,
    Environmental,
    Crime,
    Sports,
    Cultural,
    Scientific,
    Other,
}

impl Default for EventType {
    fn default() -> Self {
        EventType::Other
    }
}

/// Event geometry type enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "geometry_type", rename_all = "lowercase")]
pub enum EventGeometryType {
    Point,
    Polygon,
    Linestring,
    MultiPoint,
    MultiPolygon,
    MultiLinestring,
    GeometryCollection,
}

/// Snippet type enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "snippet_type", rename_all = "lowercase")]
pub enum SnippetType {
    Quote,
    Stat,
    Fact,
    Summary,
    Claim,
}

/// Media type enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "media_type", rename_all = "lowercase")]
pub enum MediaType {
    Image,
    Video,
    Audio,
    Embed,
}

/// Fact check verdict enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "fact_check_verdict", rename_all = "lowercase")]
pub enum FactCheckVerdict {
    True,
    MostlyTrue,
    Mixed,
    MostlyFalse,
    False,
    Unverified,
}

/// Fact checker enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "fact_checker", rename_all = "lowercase")]
pub enum FactChecker {
    ClaimBuster,
    LlmVerifier,
    ClaimReview,
    Manual,
}

/// Correction severity enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "correction_severity", rename_all = "lowercase")]
pub enum CorrectionSeverity {
    Minor,
    Moderate,
    Major,
    Retraction,
}

/// Claim type enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "claim_type", rename_all = "lowercase")]
pub enum ClaimType {
    Fact,
    Allegation,
    Prediction,
    Quote,
}

/// Claim stance enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "claim_stance", rename_all = "lowercase")]
pub enum ClaimStance {
    Supports,
    Disputes,
    Neutral,
}

/// Edge predicate enum
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, sqlx::Type)]
#[sqlx(type_name = "edge_predicate", rename_all = "lowercase")]
pub enum EdgePredicate {
    Corroborates,
    Disputes,
    SameEventAs,
    PartOfNarrative,
    CausedBy,
}

/// RawArticle matching the raw_articles table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct RawArticle {
    pub id: Uuid,
    pub url: String,
    pub url_hash: String,
    pub title: String,
    pub body_text: Option<String>,
    pub summary: Option<String>,
    pub source_domain: String,
    pub source_tier: SourceTier,
    pub published_at: Option<DateTime<Utc>>,
    pub fetched_at: DateTime<Utc>,
    pub entities: Option<serde_json::Value>, // {"PERSON": [...], "ORG": [...], "GPE": [...]}
    pub minhash_signature: Option<serde_json::Value>,
    pub content_hash: Option<String>,
    pub reporting_unit_id: Option<Uuid>,
}

/// ReportingUnit matching the reporting_units table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct ReportingUnit {
    pub id: Uuid,
    pub day: DateTime<Utc>,
    pub representative_article_id: Uuid,
    pub article_count: i32,
    pub source_tiers: serde_json::Value, // {"tier1": 2, "tier2": 1}
    pub owner_groups: serde_json::Value, // {"AP": 1, "Sinclair": 3, ...}
    pub tier1_owner_groups: serde_json::Value,
    pub created_at: DateTime<Utc>,
}

/// Story matching the stories table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Story {
    pub id: Uuid,
    pub day: DateTime<Utc>,
    pub primary_entities: serde_json::Value, // Top-N entity sets
    pub tier1_unit_count: i32,
    pub tier2_unit_count: i32,
    pub tier3_unit_count: i32,
    pub tier4_unit_count: i32,
    pub distinct_owners: i32,
    pub viewpoint_cluster_id: Option<Uuid>,
    pub status: StoryStatus,
    pub gate_reason: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// CuratedPost matching the curated_posts table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct CuratedPost {
    pub id: Uuid,
    pub story_id: Uuid,
    pub platform: String,
    pub caption: String,
    pub media_urls: Option<serde_json::Value>,
    pub source_urls: serde_json::Value,
    pub status: CuratedPostStatus,
    pub scheduled_at: Option<DateTime<Utc>>,
    pub posted_at: Option<DateTime<Utc>>,
    pub error: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// StatusLog matching the status_log table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct StatusLog {
    pub id: i32,
    pub run_at: DateTime<Utc>,
    pub phase: String,
    pub status: String,
    pub details: Option<serde_json::Value>,
    pub commit_sha: Option<String>,
}

/// CanonicalEntity matching the canonical_entities table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct CanonicalEntity {
    pub id: Uuid,
    pub canonical_name: String,
    pub entity_type: String, // PERSON, ORG, GPE, LOC, EVENT, PRODUCT
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub latitude: Option<f64>,
    pub longitude: Option<f64>,
    pub location_type: Option<String>,
    pub geonames_id: Option<String>,
    pub geojson: Option<serde_json::Value>,
    pub geometry_id: Option<Uuid>,
    pub layer_id: Option<Uuid>,
}

/// EntityAlias matching the entity_aliases table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct EntityAlias {
    pub id: i32,
    pub canonical_entity_id: Uuid,
    pub alias: String,
    pub created_at: DateTime<Utc>,
}

/// EventGeometry matching the event_geometries table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct EventGeometry {
    pub id: Uuid,
    pub event_id: Uuid,
    pub geometry_type: EventGeometryType,
    pub geojson: serde_json::Value,
    pub properties: Option<serde_json::Value>,
    pub created_at: DateTime<Utc>,
}

/// EventLayer matching the event_layers table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct EventLayer {
    pub id: Uuid,
    pub name: String,
    pub description: Option<String>,
    pub filter_criteria: serde_json::Value,
    pub style: serde_json::Value,
    pub is_default: bool,
    pub is_visible: bool,
    pub min_zoom: i32,
    pub max_zoom: i32,
    pub color: String,
    pub created_at: DateTime<Utc>,
}

/// Event matching the events table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Event {
    pub id: Uuid,
    pub story_id: Uuid,
    pub latitude: f64,
    pub longitude: f64,
    pub location_name: Option<String>,
    pub location_type: Option<String>,
    pub radius_km: Option<f64>,
    pub start_time: DateTime<Utc>,
    pub event_type: EventType,
    pub confidence: f64,
    pub source_count: i32,
    pub tier1_source_count: i32,
    pub entities: Option<serde_json::Value>,
    pub geometry_id: Option<Uuid>,
    pub layer_id: Option<Uuid>,
    pub created_at: DateTime<Utc>,
}

/// ArticleEmbedding matching the article_embeddings table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct ArticleEmbedding {
    pub id: Uuid,
    pub article_id: Uuid,
    pub model: String,
    pub embedding: serde_json::Value, // Vector as JSON array
    pub dimensions: i32,
    pub created_at: DateTime<Utc>,
}

/// StoryEmbedding matching the story_embeddings table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct StoryEmbedding {
    pub id: Uuid,
    pub story_id: Uuid,
    pub model: String,
    pub embedding: serde_json::Value,
    pub dimensions: i32,
    pub created_at: DateTime<Utc>,
}

/// Snippet matching the snippets table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Snippet {
    pub id: Uuid,
    pub story_id: Uuid,
    pub article_id: Uuid,
    pub snippet_type: SnippetType,
    pub text: String,
    pub position: Option<i32>,
    pub entities: Option<serde_json::Value>,
    pub minhash_signature: Option<serde_json::Value>,
    pub confidence: i32,
    pub created_at: DateTime<Utc>,
}

/// MediaAsset matching the media_assets table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct MediaAsset {
    pub id: Uuid,
    pub article_id: Option<Uuid>,
    pub story_id: Option<Uuid>,
    pub media_type: MediaType,
    pub url: String,
    pub thumbnail_url: Option<String>,
    pub alt_text: Option<String>,
    pub width: Option<i32>,
    pub height: Option<i32>,
    pub duration_seconds: Option<i32>,
    pub source: Option<String>,
    pub source_id: Option<String>,
    pub meta_data: Option<serde_json::Value>,
    pub created_at: DateTime<Utc>,
}

/// SourceReliabilitySnapshot matching the source_reliability_snapshots table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct SourceReliabilitySnapshot {
    pub id: i32,
    pub source_domain: String,
    pub snapshot_date: DateTime<Utc>,
    pub factual_accuracy: Option<i32>,
    pub correction_rate: Option<i32>,
    pub consensus_alignment: Option<i32>,
    pub transparency_score: Option<i32>,
    pub reliability_score: i32,
    pub total_claims_verified: i32,
    pub claims_true: i32,
    pub claims_false: i32,
    pub claims_mixed: i32,
    pub corrections_count: i32,
    pub articles_sampled: i32,
    pub tier_at_snapshot: Option<String>,
    pub created_at: DateTime<Utc>,
}

/// FactCheckRecord matching the fact_check_records table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct FactCheckRecord {
    pub id: Uuid,
    pub source_domain: String,
    pub article_id: Option<Uuid>,
    pub claim: String,
    pub claim_hash: String,
    pub verdict: FactCheckVerdict,
    pub confidence: i32,
    pub fact_checker: FactChecker,
    pub fact_checker_url: Option<String>,
    pub explanation: Option<String>,
    pub checked_at: DateTime<Utc>,
    pub claim_date: Option<DateTime<Utc>>,
}

/// CorrectionRecord matching the correction_records table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct CorrectionRecord {
    pub id: Uuid,
    pub source_domain: String,
    pub article_id: Option<Uuid>,
    pub original_text: String,
    pub corrected_text: String,
    pub correction_summary: Option<String>,
    pub severity: CorrectionSeverity,
    pub correction_date: DateTime<Utc>,
    pub correction_url: Option<String>,
    pub detected_at: DateTime<Utc>,
}

/// Claim matching the claims table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Claim {
    pub id: Uuid,
    pub story_id: Uuid,
    pub text: String,
    pub claim_type: ClaimType,
    pub first_seen_at: DateTime<Utc>,
    pub created_at: DateTime<Utc>,
}

/// ClaimEvidence matching the claim_evidence table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct ClaimEvidence {
    pub id: i32,
    pub claim_id: Uuid,
    pub unit_id: Uuid,
    pub stance: ClaimStance,
    pub confidence: i32,
    pub created_at: DateTime<Utc>,
}

/// EntityEdge matching the entity_edges table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct EntityEdge {
    pub id: i32,
    pub subject_type: String,
    pub subject_id: Uuid,
    pub predicate: EdgePredicate,
    pub object_type: String,
    pub object_id: Uuid,
    pub confidence: i32,
    pub source_unit_id: Option<Uuid>,
    pub created_at: DateTime<Utc>,
}

/// TopicGroup matching the topic_groups table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct TopicGroup {
    pub id: Uuid,
    pub name: String,
    pub description: Option<String>,
    pub parent_group_id: Option<Uuid>,
    pub created_at: DateTime<Utc>,
}

/// StoryTopicGroup matching the story_topic_groups table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct StoryTopicGroup {
    pub id: i32,
    pub story_id: Uuid,
    pub topic_group_id: Uuid,
    pub confidence: Option<i32>,
    pub created_at: DateTime<Utc>,
}

/// SourceTopicReliability matching the source_topic_reliability table
#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct SourceTopicReliability {
    pub id: Uuid,
    pub source_domain: String,
    pub topic_group_id: Uuid,
    pub score: i32,
    pub sample_size: i32,
    pub snapshot_date: DateTime<Utc>,
    pub created_at: DateTime<Utc>,
}