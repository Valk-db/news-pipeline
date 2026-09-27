use crate::database::PgPool;
use crate::models::{ReportingUnit, SourceTier};
use crate::config::Settings;
use chrono::Utc;
use sqlx::Row;
use std::collections::HashMap;
use uuid::Uuid;

/// Apply the dynamic gate to stories based on source tier counts and owner diversity.
/// This implements the single-source and single-tier blocking logic.
pub async fn apply_dynamic_gate(
    pool: &crate::database::PgPool,
    story_ids: Option<Vec<Uuid>>,
) -> Result<serde_json::Value, Box<dyn std::error::Error + Send + Sync>> {
    let settings = Settings::from_env().unwrap();
    let dynamic_gate_enabled = settings.dynamic_gate_enabled;

    // Get stories to evaluate
    let stories = if let Some(ids) = story_ids {
        if ids.is_empty() {
            Vec::new()
        } else {
            let placeholders = ids.iter().enumerate().map(|(i, _)| format!("${}", i + 1)).collect::<Vec<_>>().join(",");
            let query_str = format!("SELECT * FROM stories WHERE id IN ({})", placeholders);
            let query = sqlx::query(sqlx::AssertSqlSafe(query_str));
            let stories = ids.iter().fold(query, |q, id| q.bind(id)).fetch_all(pool).await?;
            stories
        }
    } else {
        // Evaluate all PENDING stories
        sqlx::query("SELECT * FROM stories WHERE status = 'pending'")
            .fetch_all(pool)
            .await?
    };

    let mut queued = 0;
    let mut blocked = 0;
    let mut block_reasons = HashMap::new();

    for row in stories {
        let story_id: Uuid = row.get("id");
        let tier1_count: i32 = row.get("tier1_unit_count");
        let tier2_count: i32 = row.get("tier2_unit_count");
        let tier3_count: i32 = row.get("tier3_unit_count");
        let tier4_count: i32 = row.get("tier4_unit_count");
        let distinct_owners: i32 = row.get("distinct_owners");

        let story_id_for_bind: Uuid = story_id;

        let total_units = tier1_count + tier2_count + tier3_count + tier4_count;

        if total_units == 0 {
            continue;
        }

        let mut gate_reasons = Vec::new();

        // Check single-source blocking (only one owner)
        if distinct_owners == 1 {
            gate_reasons.push("single_source".to_string());
        }

        // Check single-tier blocking (all units from same tier)
        let tier_counts = [tier1_count, tier2_count, tier3_count, tier4_count];
        let non_zero_tiers = tier_counts.iter().filter(|&&c| c > 0).count();
        if non_zero_tiers == 1 {
            gate_reasons.push("single_tier".to_string());
        }

        // Check minimum reporting units per story
        let min_units = settings.min_reporting_units_per_story as i32;
        if total_units < min_units {
            gate_reasons.push("below_min_units".to_string());
        }

        // Check tier-1 source requirement (at least 1 tier-1 unit for high-confidence stories)
        if tier1_count == 0 && total_units >= 2 {
            // This is a soft signal, not a hard block
        }

        if gate_reasons.is_empty() || !dynamic_gate_enabled {
            // Pass gate - queue for curation
            sqlx::query(
                "UPDATE stories SET status = 'queued', gate_reason = NULL, updated_at = $1 WHERE id = $2"
            )
            .bind(Utc::now())
            .bind(story_id_for_bind)
            .execute(pool)
            .await?;
            queued += 1;
        } else {
            // Block
            let reason = gate_reasons.join(", ");
            sqlx::query(
                "UPDATE stories SET status = 'blocked', gate_reason = $1, updated_at = $2 WHERE id = $3"
            )
            .bind(&reason)
            .bind(Utc::now())
            .bind(story_id_for_bind)
            .execute(pool)
            .await?;
            blocked += 1;
            block_reasons.insert(story_id_for_bind.to_string(), reason);
        }
    }

    Ok(serde_json::json!({
        "queued": queued,
        "blocked": blocked,
        "block_reasons": block_reasons
    }))
}

/// Classify source tier based on domain
pub fn classify_source_tier(domain: &str) -> SourceTier {
    let tier1_domains = [
        "apnews.com", "reuters.com", "bbc.com", "theguardian.com", "npr.org",
        "dw.com", "france24.com", "aljazeera.com", "euronews.com", "pbs.org",
    ];

    let tier2_domains = [
        "nytimes.com", "washingtonpost.com", "wsj.com", "ft.com", "economist.com",
        "foreignpolicy.com", "foreignaffairs.com", "csis.org", "who.int",
        "latimes.com", "chicagotribune.com", "bostonglobe.com",
    ];

    if tier1_domains.iter().any(|d| *d == domain) {
        SourceTier::Tier1
    } else if tier2_domains.iter().any(|d| *d == domain) {
        SourceTier::Tier2
    } else if domain == "reddit.com" || domain == "bsky.social" {
        SourceTier::Tier3
    } else {
        SourceTier::Tier4
    }
}

/// Compute story counters from linked units
pub async fn recompute_story_counters(pool: &crate::database::PgPool, story_ids: &[Uuid]) -> Result<(), sqlx::Error> {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_classify_source_tier() {
        assert_eq!(classify_source_tier("bbc.com"), SourceTier::Tier1);
        assert_eq!(classify_source_tier("nytimes.com"), SourceTier::Tier2);
        assert_eq!(classify_source_tier("reddit.com"), SourceTier::Tier3);
        assert_eq!(classify_source_tier("unknown.com"), SourceTier::Tier4);
    }
}