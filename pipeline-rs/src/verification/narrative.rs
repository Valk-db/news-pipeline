//! Narrative arc linking: connect stories across time via entity overlap.
//!
//! This module creates EntityEdge rows linking a story to earlier stories
//! covering the same ongoing situation, beyond the 48h story-grouping window.

use crate::database::PgPool;
use crate::models::{EntityEdge, EdgePredicate, Story};
use crate::verification::utils::entity_jaccard;
use serde::Serialize;
use sqlx::Row;
use std::collections::HashSet;
use uuid::Uuid;

// Thresholds for narrative arc linking
const SAME_EVENT_AS_THRESHOLD: f64 = 0.5;   // Jaccard >= 0.5 -> SAME_EVENT_AS
const PART_OF_NARRATIVE_THRESHOLD: f64 = 0.2; // Jaccard >= 0.2 -> PART_OF_NARRATIVE
const LOOKBACK_DAYS: i64 = 90; // Look back this many days for narrative arcs

/// Result of narrative arc linking
#[derive(Debug, Serialize)]
pub struct NarrativeLinkResult {
    pub subject_id: String,
    pub object_id: String,
    pub predicate: String,
    pub confidence: i32,
    pub created: bool,
    pub error: Option<String>,
}

/// Link a newly-created or newly-gated story to earlier stories
pub async fn link_narrative_arcs(
    pool: &PgPool,
    story_id: Uuid,
) -> Vec<NarrativeLinkResult> {
    let mut results = Vec::new();

    // Get the target story
    let story_row = sqlx::query("SELECT * FROM stories WHERE id = $1")
        .bind(story_id)
        .fetch_optional(pool)
        .await;

    let story = match story_row {
        Ok(Some(row)) => {
            Story {
                id: row.get("id"),
                day: row.get("day"),
                primary_entities: row.get("primary_entities"),
                tier1_unit_count: row.get("tier1_unit_count"),
                tier2_unit_count: row.get("tier2_unit_count"),
                tier3_unit_count: row.get("tier3_unit_count"),
                tier4_unit_count: row.get("tier4_unit_count"),
                distinct_owners: row.get("distinct_owners"),
                viewpoint_cluster_id: row.get("viewpoint_cluster_id"),
                status: row.get("status"),
                gate_reason: row.get("gate_reason"),
                created_at: row.get("created_at"),
                updated_at: row.get("updated_at"),
            }
        }
        Ok(None) => {
            return vec![NarrativeLinkResult {
                subject_id: story_id.to_string(),
                object_id: "".to_string(),
                predicate: "".to_string(),
                confidence: 0,
                created: false,
                error: Some("Story not found".to_string()),
            }];
        }
        Err(e) => {
            return vec![NarrativeLinkResult {
                subject_id: story_id.to_string(),
                object_id: "".to_string(),
                predicate: "".to_string(),
                confidence: 0,
                created: false,
                error: Some(format!("Database error: {}", e)),
            }];
        }
    };

    let target_entities: HashSet<String> = story.primary_entities
        .as_array()
        .map(|arr| arr.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect())
        .unwrap_or_default();

    if target_entities.is_empty() {
        return results;
    }

    // Look back for older stories
    let cutoff = chrono::Utc::now() - chrono::Duration::days(LOOKBACK_DAYS);
    let older_stories = get_stories_with_entities(pool, cutoff, Some(story_id)).await;

    for (older_story_id, older_entities) in older_stories {
        let jaccard = entity_jaccard(&target_entities, &older_entities);

        let (predicate, confidence) = if jaccard >= SAME_EVENT_AS_THRESHOLD {
            (EdgePredicate::SameEventAs, (jaccard * 100.0).min(100.0) as i32)
        } else if jaccard >= PART_OF_NARRATIVE_THRESHOLD {
            (EdgePredicate::PartOfNarrative, (jaccard * 100.0).min(100.0) as i32)
        } else {
            continue; // No significant overlap
        };

        // Check if edge already exists (idempotent)
        let existing = sqlx::query(
            r#"
            SELECT confidence FROM entity_edges
            WHERE subject_type = 'story' AND subject_id = $1
            AND predicate = $2 AND object_type = 'story' AND object_id = $3
            "#
        )
        .bind(story_id)
        .bind(predicate as EdgePredicate)
        .bind(older_story_id)
        .fetch_optional(pool)
        .await;

        if let Ok(Some(row)) = existing {
            let existing_confidence: i32 = row.get("confidence");
            results.push(NarrativeLinkResult {
                subject_id: story_id.to_string(),
                object_id: older_story_id.to_string(),
                predicate: predicate.to_string(),
                confidence: existing_confidence,
                created: false,
                error: None,
            });
            continue;
        }

        // Create the edge (story -> older_story, so subject is newer, object is older)
        if let Err(e) = sqlx::query(
            r#"
            INSERT INTO entity_edges (subject_type, subject_id, predicate, object_type, object_id, confidence, source_unit_id, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            "#
        )
        .bind("story")
        .bind(story_id)
        .bind(predicate as EdgePredicate)
        .bind("story")
        .bind(older_story_id)
        .bind(confidence)
        .bind(Option::<Uuid>::None)
        .bind(chrono::Utc::now())
        .execute(pool)
        .await {
            tracing::warn!("Failed to insert narrative edge: {}", e);
            results.push(NarrativeLinkResult {
                subject_id: story_id.to_string(),
                object_id: older_story_id.to_string(),
                predicate: predicate.to_string(),
                confidence: 0,
                created: false,
                error: Some(e.to_string()),
            });
            continue;
        }

        results.push(NarrativeLinkResult {
            subject_id: story_id.to_string(),
            object_id: older_story_id.to_string(),
            predicate: predicate.to_string(),
            confidence,
            created: true,
            error: None,
        });
    }

    results
}

/// Get QUEUED stories from lookback period with their primary_entities
async fn get_stories_with_entities(
    pool: &PgPool,
    cutoff: chrono::DateTime<chrono::Utc>,
    exclude_story_id: Option<Uuid>,
) -> Vec<(Uuid, HashSet<String>)> {
    let rows = if let Some(exclude_id) = exclude_story_id {
        sqlx::query(
            r#"
            SELECT id, primary_entities FROM stories
            WHERE status = 'queued' AND created_at >= $1 AND id != $2
            ORDER BY created_at DESC
            "#
        )
        .bind(cutoff)
        .bind(exclude_id)
        .fetch_all(pool)
        .await
        .unwrap_or_default()
    } else {
        sqlx::query(
            r#"
            SELECT id, primary_entities FROM stories
            WHERE status = 'queued' AND created_at >= $1
            ORDER BY created_at DESC
            "#
        )
        .bind(cutoff)
        .fetch_all(pool)
        .await
        .unwrap_or_default()
    };

    let mut story_entities = Vec::new();
    for row in rows {
        let story_id: Uuid = row.get("id");
        let entities: Option<serde_json::Value> = row.get("primary_entities");

        if let Some(entities) = entities {
            if let Some(arr) = entities.as_array() {
                let entity_set: HashSet<String> = arr.iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect();
                if !entity_set.is_empty() {
                    story_entities.push((story_id, entity_set));
                }
            }
        }
    }

    story_entities
}

/// Batch narrative arc linking for recent QUEUED stories
pub async fn link_narrative_arcs_for_recent_stories(
    pool: &PgPool,
    hours_back: i64,
    max_stories: usize,
) -> Vec<NarrativeLinkResult> {
    let cutoff = chrono::Utc::now() - chrono::Duration::hours(hours_back);

    let rows = sqlx::query(
        r#"
        SELECT * FROM stories
        WHERE status = 'queued' AND created_at >= $1
        ORDER BY created_at DESC
        LIMIT $2
        "#
    )
    .bind(cutoff)
    .bind(max_stories as i64)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    if rows.is_empty() {
        tracing::info!("No QUEUED stories to link narrative arcs for");
        return Vec::new();
    }

    tracing::info!("Linking narrative arcs for {} QUEUED stories", rows.len());

    let mut results = Vec::new();
    for row in rows {
        let story_id: Uuid = row.get("id");
        let story_results = link_narrative_arcs(pool, story_id).await;
        results.extend(story_results);
    }

    results
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn test_same_event_as_threshold() {
        let mut set_a = HashSet::new();
        set_a.insert("1".to_string());
        set_a.insert("2".to_string());
        set_a.insert("3".to_string());
        set_a.insert("4".to_string());

        let mut set_b = HashSet::new();
        set_b.insert("1".to_string());
        set_b.insert("2".to_string());
        set_b.insert("3".to_string());

        let j = entity_jaccard(&set_a, &set_b);
        assert!(j >= SAME_EVENT_AS_THRESHOLD);
    }

    #[test]
    fn test_part_of_narrative_threshold() {
        let mut set_a = HashSet::new();
        set_a.insert("1".to_string());
        set_a.insert("2".to_string());

        let mut set_b = HashSet::new();
        set_b.insert("1".to_string());
        set_b.insert("3".to_string());

        let j = entity_jaccard(&set_a, &set_b);
        assert!(j >= PART_OF_NARRATIVE_THRESHOLD);
        assert!(j < SAME_EVENT_AS_THRESHOLD);
    }
}