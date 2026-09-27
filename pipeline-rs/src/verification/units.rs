use crate::database::PgPool;
use crate::models::{RawArticle, ReportingUnit, SourceTier};
use crate::utils::minhash_utils::{cluster_articles_by_containment, shingle_text};
use crate::utils::ner::{get_primary_entity_set, resolve_entities_to_canonical, EntityCanonicalizer};
use crate::ingestion::source_registry::get_owner_group;
use chrono::{DateTime, Utc};
use sqlx::Row;
use std::collections::{HashMap, HashSet};
use tracing::{info, warn};
use uuid::Uuid;

/// Build reporting units by clustering near-duplicate articles.
/// Each unit = one reporting event (original + syndications).
pub async fn build_reporting_units(pool: &PgPool) -> Result<i32, Box<dyn std::error::Error + Send + Sync>> {
    // Get articles from last 24h that aren't yet clustered
    let cutoff = Utc::now() - chrono::Duration::hours(24);
    let rows = sqlx::query(
        r#"
        SELECT id, body_text, source_domain, source_tier, published_at, fetched_at, entities, content_hash
        FROM raw_articles
        WHERE fetched_at >= $1
        AND reporting_unit_id IS NULL
        "#
    )
    .bind(cutoff)
    .fetch_all(pool)
    .await?;

    if rows.is_empty() {
        return Ok(0);
    }

    // Group by day bucket (UTC date)
    let mut day_buckets: HashMap<DateTime<Utc>, Vec<RawArticle>> = HashMap::new();
    for row in rows {
        let article = RawArticle {
            id: row.get("id"),
            body_text: row.get("body_text"),
            source_domain: row.get("source_domain"),
            source_tier: row.get("source_tier"),
            published_at: row.get("published_at"),
            fetched_at: row.get("fetched_at"),
            entities: row.get("entities"),
            content_hash: row.get("content_hash"),
            ..Default::default()
        };

        let day = article.published_at.unwrap_or(article.fetched_at);
        let day = day.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
        day_buckets.entry(day).or_default().push(article);
    }

    let settings = crate::config::Settings::from_env().unwrap();
    let threshold = settings.containment_threshold;

    // Initialize canonicalizer
    let canonicalizer = EntityCanonicalizer::new(std::sync::Arc::new(pool.clone()));
    // Note: initialize() would be called here if needed

    let mut total_units = 0;

    for (day, day_articles) in day_buckets {
        // Prepare for clustering: [(article_id, tokens)]
        let mut article_tokens = Vec::new();
        for article in &day_articles {
            let body = article.body_text.clone().unwrap_or_default();
            let tokens = shingle_text(&body, 5);
            article_tokens.push((article.id.to_string(), tokens));
        }

        // Cluster by containment
        let clusters = cluster_articles_by_containment(article_tokens, threshold, 128);

        for cluster in clusters {
            // Find representative (longest body text)
            let cluster_articles: Vec<&RawArticle> = day_articles.iter()
                .filter(|a| cluster.contains(&a.id.to_string()))
                .collect();

            if cluster_articles.is_empty() {
                continue;
            }

            let representative = cluster_articles.iter()
                .max_by_key(|a| a.body_text.as_ref().map(|b| b.len()).unwrap_or(0))
                .unwrap();

            // Count source tiers and owner groups
            let mut tier_counts: HashMap<String, i32> = HashMap::new();
            let mut owner_counts: HashMap<String, i32> = HashMap::new();
            let mut tier1_owner_counts: HashMap<String, i32> = HashMap::new();

            for article in &cluster_articles {
                let tier_str = format!("{:?}", article.source_tier).to_lowercase();
                *tier_counts.entry(tier_str).or_default() += 1;

                let owner = get_owner_group(&article.source_domain);
                *owner_counts.entry(owner.clone()).or_default() += 1;

                if article.source_tier == SourceTier::Tier1 {
                    *tier1_owner_counts.entry(owner).or_default() += 1;
                }
            }

            // Create reporting unit
            let unit_id = Uuid::new_v4();
            sqlx::query(
                r#"
                INSERT INTO reporting_units (id, day, representative_article_id, article_count, source_tiers, owner_groups, tier1_owner_groups, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                "#
            )
            .bind(unit_id)
            .bind(day)
            .bind(representative.id)
            .bind(cluster_articles.len() as i32)
            .bind(serde_json::to_value(&tier_counts)?)
            .bind(serde_json::to_value(&owner_counts)?)
            .bind(serde_json::to_value(&tier1_owner_counts)?)
            .bind(Utc::now())
            .execute(pool)
            .await?;

            // Link articles to unit
            for article in cluster_articles {
                sqlx::query(
                    "UPDATE raw_articles SET reporting_unit_id = $1 WHERE id = $2"
                )
                .bind(unit_id)
                .bind(article.id)
                .execute(pool)
                .await?;
            }

            total_units += 1;
        }
    }

    Ok(total_units)
}

impl Default for RawArticle {
    fn default() -> Self {
        Self {
            id: Uuid::new_v4(),
            url: String::new(),
            url_hash: String::new(),
            title: String::new(),
            body_text: None,
            summary: None,
            source_domain: String::new(),
            source_tier: SourceTier::Tier3,
            published_at: None,
            fetched_at: Utc::now(),
            entities: None,
            minhash_signature: None,
            content_hash: None,
            reporting_unit_id: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_get_owner_group() {
        assert_eq!(get_owner_group("bbc.com"), "BBC");
        assert_eq!(get_owner_group("www.bbc.com"), "BBC");
        assert_eq!(get_owner_group("unknown.com"), "Independent");
    }
}