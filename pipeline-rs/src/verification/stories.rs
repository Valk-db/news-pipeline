use crate::database::PgPool;
use crate::models::{ReportingUnit, Story, StoryStatus, SourceTier};
use crate::utils::ner::{get_primary_entity_set, resolve_entities_to_canonical, canonical_jaccard, EntityCanonicalizer};
use std::sync::Arc;
use chrono::{DateTime, Utc};
use sqlx::Row;
use std::collections::{HashMap, HashSet};
use tracing::{info, warn};
use uuid::Uuid;

/// Build stories by grouping reporting units via top-N entity set overlap (Jaccard) on canonical IDs.
pub async fn build_stories(pool: &PgPool) -> Result<Vec<Uuid>, Box<dyn std::error::Error + Send + Sync>> {
    // Initialize canonicalizer
    let canonicalizer = EntityCanonicalizer::new(std::sync::Arc::new(pool.clone()));

    // Get unassigned reporting units
    let rows = sqlx::query(
        r#"
        SELECT ru.id, ru.representative_article_id, ru.day, ru.source_tiers
        FROM reporting_units ru
        LEFT JOIN story_unit_links sul ON sul.unit_id = ru.id
        WHERE sul.id IS NULL
        "#
    )
    .fetch_all(pool)
    .await?;

    if rows.is_empty() {
        return Ok(Vec::new());
    }

    // Fetch representative articles for entity extraction
    let article_ids: Vec<Uuid> = rows.iter().map(|r| r.get("representative_article_id")).collect();
    let placeholders = article_ids.iter().enumerate().map(|(i, _)| format!("${}", i + 1)).collect::<Vec<_>>().join(",");
    let query_str = format!(
        "SELECT id, entities FROM raw_articles WHERE id IN ({})",
        placeholders
    );
    let query = sqlx::query(sqlx::AssertSqlSafe(query_str));
    let article_rows = article_ids.iter().fold(query, |q, id| q.bind(id)).fetch_all(pool).await?;
    let articles: HashMap<Uuid, serde_json::Value> = article_rows
        .into_iter()
        .map(|row| (row.get("id"), row.get("entities")))
        .collect();

    // Build canonical entity sets for each new unit
    let mut unit_canonical_entities: HashMap<Uuid, HashSet<String>> = HashMap::new();
    for row in &rows {
        let unit_id: Uuid = row.get("id");
        let article_id: Uuid = row.get("representative_article_id");

        if let Some(entities_json) = articles.get(&article_id) {
            if let Some(entities_obj) = entities_json.as_object() {
                // Convert JSON to HashMap<String, Vec<String>>
                let mut entities_dict: HashMap<String, Vec<String>> = HashMap::new();
                for (label, value) in entities_obj {
                    if let Some(arr) = value.as_array() {
                        entities_dict.insert(label.clone(), arr.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect());
                    }
                }

                // Get primary entity set (PERSON, ORG, GPE)
                let primary_entities = get_primary_entity_set(&entities_dict);

                // Resolve to canonical IDs
                let (canonical_ids, _) = resolve_entities_to_canonical(&entities_dict, &EntityCanonicalizer::new(std::sync::Arc::new(pool.clone()))).await.unwrap_or_default();
                unit_canonical_entities.insert(unit_id, canonical_ids);
            } else {
                unit_canonical_entities.insert(unit_id, HashSet::new());
            }
        } else {
            unit_canonical_entities.insert(unit_id, HashSet::new());
        }
    }

    // Get recent stories (PENDING + BLOCKED) from last 48h with their canonical entities
    let cutoff = Utc::now() - chrono::Duration::hours(48);
    let story_rows = sqlx::query(
        r#"
        SELECT id, primary_entities FROM stories
        WHERE status IN ('pending', 'blocked')
        AND updated_at >= $1
        "#
    )
    .bind(cutoff)
    .fetch_all(pool)
    .await?;

    let mut recent_stories: HashMap<Uuid, HashSet<String>> = HashMap::new();
    for row in story_rows {
        let story_id: Uuid = row.get("id");
        let primary_entities: Option<serde_json::Value> = row.get("primary_entities");
        let mut entities = HashSet::new();
        if let Some(val) = primary_entities {
            if let Some(arr) = val.as_array() {
                for v in arr {
                    if let Some(s) = v.as_str() {
                        entities.insert(s.to_string());
                    }
                }
            }
        }
        recent_stories.insert(story_id, entities);
    }

    let mut modified_story_ids = Vec::new();
    let similarity_threshold = 0.4; // Jaccard threshold for attaching to existing story

    for row in rows {
        let unit_id: Uuid = row.get("id");
        let unit_day: chrono::DateTime<Utc> = row.get("day");
        let unit_canonical_set = unit_canonical_entities.get(&unit_id).cloned().unwrap_or_default();

        if unit_canonical_set.is_empty() {
            // No entities - create new story
            let story = create_story_from_unit(pool, unit_day, &row, &unit_canonical_entities).await?;
            recent_stories.insert(story.id, HashSet::new());
            modified_story_ids.push(story.id);
            continue;
        }

        // Find best matching story
        let mut best_story_id = None;
        let mut best_jaccard = 0.0;

        for (story_id, story_entities) in &recent_stories {
            if story_entities.is_empty() {
                continue;
            }
            let jaccard = canonical_jaccard(&unit_canonical_set, story_entities);
            if jaccard > best_jaccard && jaccard >= similarity_threshold {
                best_jaccard = jaccard;
                best_story_id = Some(*story_id);
            }
        }

        if let Some(best_story_id) = best_story_id {
            // Attach to existing story
            attach_unit_to_story(pool, unit_id, best_story_id).await?;
            // Update story's aggregate canonical entities
            update_story_entities(pool, best_story_id, &unit_canonical_set).await?;
            recent_stories.get_mut(&best_story_id).unwrap().extend(unit_canonical_set);
            modified_story_ids.push(best_story_id);
        } else {
            // Create new story
            let story = create_story_from_unit(pool, row.get("day"), &row, &unit_canonical_entities).await?;
            recent_stories.insert(story.id, story_entities_from_row(&row, &unit_canonical_entities));
            modified_story_ids.push(story.id);
        }
    }

    // Recompute counters for all modified stories
    if !modified_story_ids.is_empty() {
        recompute_story_counters(pool, &modified_story_ids).await?;
    }

    // Phase 2: Viewpoint sub-clustering within stories
    cluster_viewpoints(pool, &modified_story_ids).await?;

    Ok(modified_story_ids)
}

fn story_entities_from_row(row: &sqlx::postgres::PgRow, unit_canonical_entities: &std::collections::HashMap<Uuid, std::collections::HashSet<String>>) -> std::collections::HashSet<String> {
    let unit_id: Uuid = row.get("id");
    unit_canonical_entities.get(&unit_id).cloned().unwrap_or_default()
}

async fn attach_unit_to_story(pool: &crate::database::PgPool, unit_id: Uuid, story_id: Uuid) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO story_unit_links (story_id, unit_id) VALUES ($1, $2) ON CONFLICT DO NOTHING"
    )
    .bind(story_id)
    .bind(unit_id)
    .execute(pool)
    .await?;
    Ok(())
}

async fn update_story_entities(pool: &crate::database::PgPool, story_id: Uuid, new_entities: &std::collections::HashSet<String>) -> Result<(), sqlx::Error> {
    let mut current: HashSet<String> = sqlx::query_scalar(
        "SELECT primary_entities FROM stories WHERE id = $1"
    )
    .bind(story_id)
    .fetch_optional(pool)
    .await?
    .and_then(|v: Option<serde_json::Value>| v?.as_array().map(|arr| arr.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect()))
    .unwrap_or_default();

    current.extend(new_entities.iter().cloned());
    let entities_json = serde_json::json!(current.iter().take(10).cloned().collect::<Vec<_>>());

    sqlx::query(
        "UPDATE stories SET primary_entities = $1, updated_at = $2 WHERE id = $3"
    )
    .bind(entities_json)
    .bind(Utc::now())
    .bind(story_id)
    .execute(pool)
    .await?;

    Ok(())
}

async fn create_story_from_unit(
    pool: &crate::database::PgPool,
    day: chrono::DateTime<Utc>,
    row: &sqlx::postgres::PgRow,
    unit_canonical_entities: &std::collections::HashMap<Uuid, std::collections::HashSet<String>>,
) -> Result<Story, sqlx::Error> {
    let unit_id: Uuid = row.get("id");
    let canonical_set = unit_canonical_entities.get(&unit_id).cloned().unwrap_or_default();

    let story = Story {
        id: Uuid::new_v4(),
        day,
        primary_entities: canonical_set.iter().cloned().collect::<Vec<_>>().into(),
        tier1_unit_count: 0,
        tier2_unit_count: 0,
        tier3_unit_count: 0,
        tier4_unit_count: 0,
        distinct_owners: 0,
        viewpoint_cluster_id: None,
        status: StoryStatus::Pending,
        gate_reason: None,
        created_at: Utc::now(),
        updated_at: Utc::now(),
    };

    sqlx::query(
        r#"
        INSERT INTO stories (id, day, primary_entities, tier1_unit_count, tier2_unit_count, tier3_unit_count, tier4_unit_count, distinct_owners, viewpoint_cluster_id, status, gate_reason, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        "#
    )
    .bind(story.id)
    .bind(story.day)
    .bind(&story.primary_entities)
    .bind(story.tier1_unit_count)
    .bind(story.tier2_unit_count)
    .bind(story.tier3_unit_count)
    .bind(story.tier4_unit_count)
    .bind(story.distinct_owners)
    .bind(story.viewpoint_cluster_id)
    .bind(story.status as StoryStatus)
    .bind(&story.gate_reason)
    .bind(story.created_at)
    .bind(story.updated_at)
    .execute(pool)
    .await?;

    // Link unit to story
    let unit_id: Uuid = row.get("id");
    sqlx::query("INSERT INTO story_unit_links (story_id, unit_id) VALUES ($1, $2)")
        .bind(story.id)
        .bind(unit_id)
        .execute(pool)
        .await?;

    Ok(story)
}

/// Recompute tier counts and owner counts for stories
async fn recompute_story_counters(pool: &crate::database::PgPool, story_ids: &[Uuid]) -> Result<(), sqlx::Error> {
    for story_id in story_ids {
        let units: Vec<ReportingUnit> = sqlx::query_as(
            r#"
            SELECT ru.* FROM reporting_units ru
            JOIN story_unit_links sul ON sul.unit_id = ru.id
            WHERE sul.story_id = $1
            "#
        )
        .bind(story_id)
        .fetch_all(pool)
        .await?;

        let mut tier1_count = 0;
        let mut tier2_count = 0;
        let mut tier3_count = 0;
        let mut tier4_count = 0;
        let mut owners = HashMap::new();

        for unit in units {
            if let Some(tiers) = unit.source_tiers.as_object() {
                for (tier, count) in tiers {
                    if let Some(count) = count.as_i64() {
                        match tier.as_str() {
                            "tier1" => tier1_count += count,
                            "tier2" => tier2_count += count,
                            "tier3" => tier3_count += count,
                            "tier4" => tier4_count += count,
                            _ => {}
                        }
                    }
                }
            }
            if let Some(owner_groups) = unit.owner_groups.as_object() {
                for (owner, count) in owner_groups {
                    if let Some(count) = count.as_i64() {
                        *owners.entry(owner.clone()).or_insert(0) += count;
                    }
                }
            }
        }

        let distinct_owners = owners.len() as i32;

        sqlx::query(
            r#"
            UPDATE stories SET
                tier1_unit_count = $1,
                tier2_unit_count = $2,
                tier3_unit_count = $3,
                tier4_unit_count = $4,
                distinct_owners = $5,
                updated_at = $6
            WHERE id = $7
            "#
        )
        .bind(tier1_count)
        .bind(tier2_count)
        .bind(tier3_count)
        .bind(tier4_count)
        .bind(distinct_owners)
        .bind(Utc::now())
        .bind(story_id)
        .execute(pool)
        .await?;
    }

    Ok(())
}

/// Viewpoint sub-clustering within stories
async fn cluster_viewpoints(pool: &crate::database::PgPool, story_ids: &[Uuid]) -> Result<HashMap<Uuid, Vec<Uuid>>, Box<dyn std::error::Error + Send + Sync>> {
    let mut viewpoint_clusters: HashMap<Uuid, Vec<Uuid>> = HashMap::new();

    for story_id in story_ids {
        // Get story with units
        let story = sqlx::query_as::<_, Story>("SELECT * FROM stories WHERE id = $1")
            .bind(story_id)
            .fetch_optional(pool)
            .await?;

        if let Some(story) = story {
            if story.tier1_unit_count + story.tier2_unit_count + story.tier3_unit_count + story.tier4_unit_count < 3 {
                continue;
            }

            // Get unit texts
            let unit_texts = get_unit_texts_for_story(pool, &story, 10, 2000).await;

            if unit_texts.len() < 3 {
                continue;
            }

            // Use LLM to classify stance/viewpoint
            let llm = crate::llm::LLMClient::new(crate::config::Settings::from_env().unwrap());

            let texts_for_prompt = unit_texts.iter()
                .map(|u| format!("Source: {}\n{}", u.source_tier, u.text))
                .collect::<Vec<_>>()
                .join("\n\n---\n\n");

            let prompt = format!(r#"Analyze these news article excerpts about the same event and identify distinct viewpoints/stances.
Group them into perspectives (e.g., pro-government, opposition, neutral/international, local, skeptical).

Articles:
{}

Return a JSON object mapping unit_id to viewpoint_label. Use concise labels like:
"pro_govt", "opposition", "neutral", "international", "local", "skeptical", "pro_business", "pro_labor", etc.

Only return the JSON object, no explanation."#, texts_for_prompt);

            if let Ok(response) = llm.chat_completion(
                vec![crate::llm::ChatMessage { role: "user".to_string(), content: prompt }],
                Some(500),
                Some(0.1),
                None,
            ).await {
                if let Some(content) = response.choices.first().map(|c| c.message.content.clone()) {
                    if let Ok(viewpoint_labels) = serde_json::from_str::<HashMap<String, String>>(&content) {
                        // Group units by viewpoint
                        let mut viewpoint_to_units: HashMap<String, Vec<Uuid>> = HashMap::new();
                        for unit_data in &unit_texts {
                            let unit_id_str = unit_data.unit_id.clone();
                            let label = viewpoint_labels.get(&unit_id_str).unwrap_or(&"neutral".to_string()).clone();
                            if let Ok(unit_id_uuid) = Uuid::parse_str(&unit_id_str) {
                                viewpoint_to_units.entry(label).or_default().push(unit_id_uuid);
                            }
                        }

                        // Create viewpoint sub-stories
                        for (label, unit_ids) in viewpoint_to_units {
                            if unit_ids.is_empty() {
                                continue;
                            }

                            // Create viewpoint story
                            let viewpoint_story = Story {
                                id: Uuid::new_v4(),
                                day: story.day,
                                primary_entities: story.primary_entities.clone(),
                                status: StoryStatus::Pending,
                                viewpoint_cluster_id: Some(story.id),
                                created_at: Utc::now(),
                                updated_at: Utc::now(),
                                ..Default::default()
                            };

                            sqlx::query(
                                r#"
                                INSERT INTO stories (id, day, primary_entities, status, viewpoint_cluster_id, created_at, updated_at)
                                VALUES ($1, $2, $3, $4, $5, $6, $7)
                                "#
                            )
                            .bind(viewpoint_story.id)
                            .bind(viewpoint_story.day)
                            .bind(&viewpoint_story.primary_entities)
                            .bind(viewpoint_story.status as StoryStatus)
                            .bind(viewpoint_story.viewpoint_cluster_id)
                            .bind(viewpoint_story.created_at)
                            .bind(viewpoint_story.updated_at)
                            .execute(pool)
                            .await?;

                            // Link units
                            for unit_id in &unit_ids {
                                sqlx::query("INSERT INTO story_unit_links (story_id, unit_id) VALUES ($1, $2) ON CONFLICT DO NOTHING")
                                    .bind(viewpoint_story.id)
                                    .bind(unit_id)
                                    .execute(pool)
                                    .await?;
                            }

                            viewpoint_clusters.entry(story.id).or_default().push(viewpoint_story.id);
                        }
                    }
                }
            }
        }
    }

    Ok(viewpoint_clusters)
}

async fn get_unit_texts_for_story(
    pool: &crate::database::PgPool,
    story: &Story,
    max_units: usize,
    max_chars: usize,
) -> Vec<UnitText> {
    let rows = sqlx::query(
        r#"
        SELECT ru.id, ru.representative_article_id, ru.source_tiers
        FROM reporting_units ru
        JOIN story_unit_links sul ON sul.unit_id = ru.id
        WHERE sul.story_id = $1
        LIMIT $2
        "#
    )
    .bind(story.id)
    .bind(max_units as i64)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let mut unit_texts = Vec::new();
    for row in rows {
        let unit_id: Uuid = row.get("id");
        let article_id: Uuid = row.get("representative_article_id");

        if let Some(article_row) = sqlx::query("SELECT body_text FROM raw_articles WHERE id = $1")
            .bind(article_id)
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

                let source_tiers: Option<serde_json::Value> = row.get("source_tiers");
                let source_tier = source_tiers
                    .as_ref()
                    .and_then(|v| v.as_object())
                    .and_then(|obj| obj.keys().next())
                    .map(|k| k.to_string())
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

#[derive(Debug, Clone)]
struct UnitText {
    unit_id: String,
    text: String,
    source_tier: String,
}

impl Default for Story {
    fn default() -> Self {
        Self {
            id: Uuid::new_v4(),
            day: Utc::now(),
            primary_entities: serde_json::json!([]),
            tier1_unit_count: 0,
            tier2_unit_count: 0,
            tier3_unit_count: 0,
            tier4_unit_count: 0,
            distinct_owners: 0,
            viewpoint_cluster_id: None,
            status: StoryStatus::Pending,
            gate_reason: None,
            created_at: Utc::now(),
            updated_at: Utc::now(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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