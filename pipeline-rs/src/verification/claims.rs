//! Claim extraction: populate claims + claim_evidence for gated stories
//!
//! One LLM call per story (not per unit) -- see AGENT_TASKS.md P2-B for why.

use crate::database::PgPool;
use crate::llm::LLMClient;
use crate::models::{Claim, ClaimStance, ClaimType, Story};
use serde::{Deserialize, Serialize};
use sqlx::Row;
use std::collections::HashMap;
use uuid::Uuid;

/// Claim extraction prompt
const CLAIM_EXTRACTION_PROMPT: &str = r#"Analyze these news article excerpts about the same event and extract the atomic factual claims being made.

For each distinct claim, identify:
- The claim text (one sentence, self-contained)
- claim_type: one of "fact", "allegation", "prediction", "quote"
- Which sources support it, dispute it, or are neutral/don't mention it

Articles:
{texts_for_prompt}

Return ONLY a JSON object of this exact shape, no explanation, no markdown fences:
{
  "claims": [
    {
      "text": "...",
      "claim_type": "fact",
      "evidence": [
        {"unit_id": "...", "stance": "supports", "confidence": 80}
      ]
    }
  ]
}"#;

/// LLM response structure for claim extraction
#[derive(Debug, Deserialize)]
struct ClaimExtractionResponse {
    claims: Vec<ExtractedClaim>,
}

#[derive(Debug, Deserialize)]
struct ExtractedClaim {
    text: String,
    claim_type: String,
    evidence: Vec<ExtractedEvidence>,
}

#[derive(Debug, Deserialize)]
struct ExtractedEvidence {
    unit_id: String,
    stance: String,
    confidence: u32,
}

/// Result of claim extraction for a story
#[derive(Debug, Default, Serialize)]
pub struct ClaimExtractionResult {
    pub story_id: String,
    pub claims_created: usize,
    pub evidence_created: usize,
    pub errors: Vec<String>,
}

/// Extract and persist the claim matrix for one story
pub async fn extract_claims_for_story(
    pool: &PgPool,
    llm: &LLMClient,
    story_id: Uuid,
) -> ClaimExtractionResult {
    let mut results = ClaimExtractionResult {
        story_id: story_id.to_string(),
        ..Default::default()
    };

    // Get story
    let story = match sqlx::query("SELECT * FROM stories WHERE id = $1")
        .bind(story_id)
        .fetch_optional(pool)
        .await {
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
            results.errors.push("Story not found".to_string());
            return results;
        }
        Err(e) => {
            results.errors.push(format!("Database error: {}", e));
            return results;
        }
    };

    // Get unit texts for this story
    let unit_texts = get_unit_texts_for_story(pool, &story, 2000).await;
    if unit_texts.len() < 2 {
        // Not enough units for a meaningful claim matrix
        return results;
    }

    // Build prompt
    let texts_for_prompt = unit_texts
        .iter()
        .map(|u| format!("unit_id: {}\nSource: {}\n{}", u.unit_id, u.source_tier, u.text))
        .collect::<Vec<_>>()
        .join("\n\n---\n\n");

    let prompt = CLAIM_EXTRACTION_PROMPT.replace("{texts_for_prompt}", &texts_for_prompt);

    // Call LLM
    match llm.complete_json::<ClaimExtractionResponse>(
        "You are a claim extraction assistant. Extract atomic factual claims from news articles.",
        &prompt,
        Some(1500),
        Some(0.1),
    ).await {
        Ok(data) => {
            for claim_data in data.claims {
                // Create claim
                let claim_type = match claim_data.claim_type.as_str() {
                    "allegation" => ClaimType::Allegation,
                    "prediction" => ClaimType::Prediction,
                    "quote" => ClaimType::Quote,
                    _ => ClaimType::Fact,
                };

                let claim = Claim {
                    id: Uuid::new_v4(),
                    story_id,
                    text: claim_data.text,
                    claim_type,
                    first_seen_at: chrono::Utc::now(),
                    created_at: chrono::Utc::now(),
                };

                // Insert claim
                if let Err(e) = sqlx::query(
                    r#"
                    INSERT INTO claims (id, story_id, text, claim_type, first_seen_at, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    "#
                )
                .bind(claim.id)
                .bind(claim.story_id)
                .bind(&claim.text)
                .bind(claim.claim_type as ClaimType)
                .bind(claim.first_seen_at)
                .bind(claim.created_at)
                .execute(pool)
                .await {
                    tracing::warn!("Failed to insert claim: {}", e);
                    results.errors.push(format!("Failed to insert claim: {}", e));
                    continue;
                }
                results.claims_created += 1;

                // Insert evidence
                for ev in claim_data.evidence {
                    let Ok(unit_id) = Uuid::parse_str(&ev.unit_id) else {
                        tracing::warn!("Invalid unit_id: {}", ev.unit_id);
                        continue;
                    };

                    let stance = match ev.stance.as_str() {
                        "supports" => ClaimStance::Supports,
                        "disputes" => ClaimStance::Disputes,
                        _ => ClaimStance::Neutral,
                    };

                    let confidence = ev.confidence.clamp(0, 100) as i32;

                    if let Err(e) = sqlx::query(
                        r#"
                        INSERT INTO claim_evidence (claim_id, unit_id, stance, confidence, created_at)
                        VALUES ($1, $2, $3, $4, $5)
                        "#
                    )
                    .bind(claim.id)
                    .bind(unit_id)
                    .bind(stance as ClaimStance)
                    .bind(confidence)
                    .bind(chrono::Utc::now())
                    .execute(pool)
                    .await {
                        tracing::warn!("Failed to insert claim evidence: {}", e);
                        continue;
                    }
                    results.evidence_created += 1;
                }
            }
        }
        Err(e) => {
            tracing::warn!("Claim extraction failed for story {}: {}", story_id, e);
            results.errors.push(e.to_string());
        }
    }

    results
}

struct UnitText {
    unit_id: String,
    text: String,
    source_tier: String,
}

/// Get unit texts for a story (shared with narrative module)
async fn get_unit_texts_for_story(
    pool: &PgPool,
    story: &Story,
    max_chars: usize,
) -> Vec<UnitText> {
    // Get units linked to this story
    let rows = sqlx::query(
        r#"
        SELECT ru.id, ru.representative_article_id, ru.source_tiers
        FROM reporting_units ru
        JOIN story_unit_links sul ON sul.unit_id = ru.id
        WHERE sul.story_id = $1
        LIMIT 10
        "#
    )
    .bind(story.id)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let mut unit_texts = Vec::new();

    for row in rows {
        let unit_id: Uuid = row.get("id");
        let rep_article_id: Uuid = row.get("representative_article_id");
        let source_tiers: Option<serde_json::Value> = row.get("source_tiers");

        // Get representative article
        if let Some(article_row) = sqlx::query("SELECT body_text FROM raw_articles WHERE id = $1")
            .bind(rep_article_id)
            .fetch_optional(pool)
            .await
            .ok()
            .flatten()
        {
            if let Some(body) = article_row.get::<Option<String>, _>("body_text") {
                let text = if body.len() > max_chars {
                    body[..max_chars].to_string()
                } else {
                    body
                };

                // Get first source tier
                let source_tier = source_tiers
                    .as_ref()
                    .and_then(|obj| obj.as_object())
                    .and_then(|obj| obj.keys().next())
                    .map(|k| k.clone())
                    .unwrap_or_else(|| "unknown".to_string());

                unit_texts.push(UnitText {
                    unit_id: unit_id.to_string(),
                    text,
                    source_tier,
                });
            }
        }
    }

    unit_texts
}

/// Batch extract claims for recent QUEUED stories
pub async fn extract_claims_for_recent_stories(
    pool: &PgPool,
    llm: &LLMClient,
    hours_back: i64,
    max_stories: usize,
) -> Vec<ClaimExtractionResult> {
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
        tracing::info!("No QUEUED stories to extract claims for");
        return Vec::new();
    }

    tracing::info!("Extracting claims for {} QUEUED stories", rows.len());

    let mut results = Vec::new();
    for row in rows {
        let story_id: Uuid = row.get("id");
        let result = extract_claims_for_story(pool, llm, story_id).await;
        results.push(result);
    }

    results
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::verification::utils::canonical_jaccard;
    use std::collections::HashSet;

    #[test]
    fn test_canonical_jaccard() {
        let mut set_a = HashSet::new();
        set_a.insert("1".to_string());
        set_a.insert("2".to_string());

        let mut set_b = HashSet::new();
        set_b.insert("2".to_string());
        set_b.insert("3".to_string());

        let j = canonical_jaccard(&set_a, &set_b);
        assert!((j - 1.0 / 3.0).abs() < 0.001);
    }
}