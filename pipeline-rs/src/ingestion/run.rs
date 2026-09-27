use crate::config::Settings;
use crate::database::PgPool;
use crate::ingestion::adapter::{SourceAdapter, SourceHealth};
use crate::ingestion::rss::{RssAdapter, SourceConfig as RssSourceConfig};
use crate::ingestion::reddit::RedditAdapter;
use crate::ingestion::source_registry::{get_enabled_sources_by_tier, SourceConfig as RegistrySourceConfig};
use crate::models::{RawArticle, SourceTier};
use crate::utils::ingest_stats::STATS;
use crate::verification::units::build_reporting_units;
use crate::verification::stories::build_stories;
use crate::verification::tiers::apply_dynamic_gate;
use chrono::{DateTime, Utc};
use sqlx::Row;
use std::collections::{HashMap, HashSet};
use tracing::{info, warn, error};
use uuid::Uuid;

/// Run the full ingestion → verification → grouping pipeline.
pub async fn run_ingestion(
    pool: &PgPool,
    settings: &Settings,
    dry_run: bool,
    tiers: Option<Vec<SourceTier>>,
) -> Result<serde_json::Value, Box<dyn std::error::Error + Send + Sync>> {
    STATS.reset();
    let started_at = Utc::now();
    let mut results = serde_json::json!({
        "started_at": started_at.to_rfc3339(),
        "phases": {},
    });

    // Default to all tiers if not specified
    let tiers = tiers.unwrap_or_else(|| vec![
        SourceTier::Tier1,
        SourceTier::Tier2,
        SourceTier::Tier3,
        SourceTier::Tier4,
    ]);

    info!("Phase 1: Ingesting articles...");
    let mut all_articles = Vec::new();
    let mut adapter_health: HashMap<String, SourceHealth> = HashMap::new();
    let mut rss_tier1_count = 0;
    let mut rss_tier2_count = 0;
    let mut gdelt_count = 0;
    let mut reddit_count = 0;

    // Build list of adapters for requested tiers
    let mut adapters: Vec<Box<dyn SourceAdapter>> = Vec::new();

    // RSS adapters per tier
    if tiers.contains(&SourceTier::Tier1) {
        let tier1_sources = get_enabled_sources_by_tier(SourceTier::Tier1);
        let rss_configs: Vec<RssSourceConfig> = tier1_sources.into_iter().map(|s| RssSourceConfig {
            domain: s.domain,
            name: s.name,
            tier: s.tier,
            rss_urls: s.rss_urls,
        }).collect();
        if !rss_configs.is_empty() {
            adapters.push(Box::new(RssAdapter::new(rss_configs, SourceTier::Tier1, settings.rss_fetch_timeout as u64, settings.rss_max_retries as u32, settings.rss_retry_delay)?));
        }
    }

    if tiers.contains(&SourceTier::Tier2) {
        let tier2_sources = get_enabled_sources_by_tier(SourceTier::Tier2);
        let rss_configs: Vec<RssSourceConfig> = tier2_sources.into_iter().map(|s| RssSourceConfig {
            domain: s.domain,
            name: s.name,
            tier: s.tier,
            rss_urls: s.rss_urls,
        }).collect();
        if !rss_configs.is_empty() {
            adapters.push(Box::new(RssAdapter::new(rss_configs, SourceTier::Tier2, settings.rss_fetch_timeout as u64, settings.rss_max_retries as u32, settings.rss_retry_delay)?));
        }
    }

    // Reddit (tier-3)
    if tiers.contains(&SourceTier::Tier3) {
        adapters.push(Box::new(RedditAdapter::new(
            settings.reddit_user_agent.clone(),
            settings.rss_fetch_timeout as u64,
        )?));
    }

    // GDELT - disabled for now as per original code
    // if settings.gdelt_enabled && tiers.contains(&SourceTier::Tier1) {
    //     adapters.push(Box::new(GDELTAdapter::new()));
    // }

    // Fetch from each adapter
    for adapter in &mut adapters {
        info!("Fetching from adapter: {}", adapter.name());
        match adapter.fetch().await {
            Ok(articles) => {
                let count = articles.len();
                info!("  {} fetched {} articles", adapter.name(), count);
                all_articles.extend(articles);

                // Track counts per source type
                match adapter.name() {
                    "rss_tier1" => rss_tier1_count = count,
                    "rss_tier2" => rss_tier2_count = count,
                    "reddit_tier3" => reddit_count = count,
                    _ => {}
                }
            }
            Err(e) => {
                error!("Adapter {} failed: {}", adapter.name(), e);
            }
        }
    }

    // Health checks
    for adapter in &adapters {
        let health = adapter.health_check().await;
        adapter_health.insert(adapter.name().to_string(), health);
    }

    info!("  Total fetched articles: {}", all_articles.len());

        // Filter out articles that already exist in DB (by URL hash)
        let mut existing_url_hashes = HashSet::new();
        let mut existing_content_hashes: HashMap<String, HashSet<String>> = HashMap::new();

        if !all_articles.is_empty() {
            let url_hashes: Vec<String> = all_articles.iter().map(|a| a.url_hash.clone()).collect();

            // Check existing URL hashes
            let placeholders = url_hashes.iter().enumerate().map(|(i, _)| format!("${}", i + 1)).collect::<Vec<_>>().join(",");
            let query_str = format!("SELECT url_hash FROM raw_articles WHERE url_hash IN ({})", placeholders);
            let query = sqlx::query(sqlx::AssertSqlSafe(query_str));
            let rows = url_hashes.iter().fold(query, |q, hash| q.bind(hash)).fetch_all(pool).await?;
            for row in rows {
                let hash: String = row.get("url_hash");
                existing_url_hashes.insert(hash);
            }

            // Check content hashes
            let content_hashes: Vec<String> = all_articles.iter()
                .filter_map(|a| a.content_hash.clone())
                .collect();
            if !content_hashes.is_empty() {
                let placeholders = content_hashes.iter().enumerate().map(|(i, _)| format!("${}", i + 1)).collect::<Vec<_>>().join(",");
                let query_str = format!("SELECT content_hash, source_domain FROM raw_articles WHERE content_hash IN ({})", placeholders);
                let query = sqlx::query(sqlx::AssertSqlSafe(query_str));
                let rows = content_hashes.iter().fold(query, |q, hash| q.bind(hash)).fetch_all(pool).await?;
                for row in rows {
                    let content_hash: String = row.get("content_hash");
                    let source_domain: String = row.get("source_domain");
                    existing_content_hashes.entry(content_hash).or_default().insert(source_domain);
                }
            }
        }

        // Deduplicate
        let (new_articles, url_dup, content_dup) = dedupe_articles(
            all_articles.clone(),
            &existing_url_hashes,
            &existing_content_hashes,
        );

        info!("  New articles (after dedup): {}", new_articles.len());
        info!("  URL duplicates skipped: {}", url_dup);
        info!("  Content duplicates skipped: {}", content_dup);

        // Collect extraction stats
        let stats_snapshot = STATS.snapshot();

        // Get GDELT health for tier-1 critical check
        let gdelt_health = adapter_health.get("gdelt").cloned().unwrap_or_else(|| SourceHealth {
            status: "down".to_string(),
            detail: "GDELT adapter not run".to_string(),
            succeeded: Vec::new(),
            failed: Vec::new(),
            skipped: Vec::new(),
        });

        // Check for tier-1 critical GDELT domains down (if GDELT was run)
        let tier1_critical_down: Vec<String> = Vec::new(); // Would check against GDELT_TIER1_CRITICAL_DOMAINS
        let ingest_status = if tier1_critical_down.is_empty() { "ok" } else { "degraded" };

        results["phases"]["ingestion"] = serde_json::json!({
            "rss": rss_tier1_count + rss_tier2_count,
            "gdelt": gdelt_count,
            "reddit": reddit_count,
            "total_fetched": all_articles.len(),
            "total_new": new_articles.len(),
            "url_duplicates_skipped": url_dup,
            "content_duplicates_skipped": content_dup,
            "gdelt_health": {
                "succeeded": gdelt_health.succeeded,
                "failed": gdelt_health.failed,
                "skipped": gdelt_health.skipped,
            },
            "tier1_critical_down": tier1_critical_down,
            "extraction_stats": stats_snapshot,
            "adapter_health": adapter_health,
        });

        if dry_run {
            info!("Dry run complete.");
            return Ok(results);
        }

        // Persist new raw articles
        for art in &new_articles {
            sqlx::query(
                r#"
                INSERT INTO raw_articles (id, url, url_hash, title, body_text, summary, source_domain, source_tier, published_at, fetched_at, entities, minhash_signature, content_hash, reporting_unit_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                "#
            )
            .bind(art.id)
            .bind(&art.url)
            .bind(&art.url_hash)
            .bind(&art.title)
            .bind(&art.body_text)
            .bind(&art.summary)
            .bind(&art.source_domain)
            .bind(art.source_tier)
            .bind(art.published_at)
            .bind(art.fetched_at)
            .bind(&art.entities)
            .bind(&art.minhash_signature)
            .bind(&art.content_hash)
            .bind(art.reporting_unit_id)
            .execute(pool)
            .await?;
        }

        // Phase 2: Build reporting units (near-dup clustering)
        info!("Phase 2: Building reporting units...");
        let units_created = build_reporting_units(pool).await?;
        info!("  Reporting units created: {}", units_created);
        results["phases"]["reporting_units"] = serde_json::json!({"created": units_created});

        // Phase 3: Build stories (semantic grouping)
        info!("Phase 3: Building stories...");
        let modified_story_ids = build_stories(pool).await?;
        let stories_created = modified_story_ids.len();
        info!("  Stories created/modified: {}", stories_created);
        results["phases"]["stories"] = serde_json::json!({
            "created_or_modified": stories_created,
            "modified_story_ids": modified_story_ids.iter().map(|s| s.to_string()).collect::<Vec<_>>()
        });

        // Phase 4: Apply dynamic gate
        info!("Phase 4: Applying dynamic gate...");
        let gated = apply_dynamic_gate(pool, Some(modified_story_ids)).await?;
        info!("  Stories queued: {}, blocked: {}", gated.get("queued").unwrap_or(&serde_json::json!(0)), gated.get("blocked").unwrap_or(&serde_json::json!(0)));
        results["phases"]["gate"] = gated;

        // Log final status
        let completed_at = Utc::now();
        results["completed_at"] = serde_json::json!(completed_at.to_rfc3339());

        // Exit non-zero if tier-1 critical GDELT domains are down (not dry run)
        if !dry_run && !tier1_critical_down.is_empty() {
            warn!("tier-1 critical GDELT domains down: {:?}", tier1_critical_down);
            // In a real scenario, would exit non-zero
        }

        // Exit guard: zero fetched articles is a real problem
        let total_fetched = all_articles.len();
        if total_fetched == 0 {
            error!("total_fetched == 0 (no articles fetched from any source)");
            return Err("No articles fetched".into());
        }

        Ok(results)
    }

/// Deduplicate articles
fn dedupe_articles(
    all_articles: Vec<RawArticle>,
    existing_url_hashes: &HashSet<String>,
    existing_content_hashes: &HashMap<String, HashSet<String>>,
) -> (Vec<RawArticle>, usize, usize) {
    let mut new_articles = Vec::new();
    let mut url_dup = 0;
    let mut content_dup = 0;
    let mut batch_url_hashes = HashSet::new();
    let mut batch_content_keys = HashSet::new();

    for a in all_articles {
        if existing_url_hashes.contains(&a.url_hash) || batch_url_hashes.contains(&a.url_hash) {
            url_dup += 1;
            continue;
        }
        if let Some(content_hash) = &a.content_hash {
            let key = (content_hash.clone(), a.source_domain.clone());
            let existing_domains = existing_content_hashes.get(content_hash).cloned().unwrap_or_default();
            if existing_domains.contains(&a.source_domain) || batch_content_keys.contains(&key) {
                content_dup += 1;
                continue;
            }
            batch_content_keys.insert(key);
        }
        batch_url_hashes.insert(a.url_hash.clone());
        new_articles.push(a);
    }

    (new_articles, url_dup, content_dup)
}

/// Log pipeline status to database
async fn log_status(
    pool: &PgPool,
    phase: &str,
    status: &str,
    details: Option<serde_json::Value>,
) -> Result<(), sqlx::Error> {
    let commit_sha = std::fs::read_to_string(".git/HEAD")
        .ok()
        .and_then(|head| {
            if head.starts_with("ref: ") {
                let ref_path = head[5..].trim();
                std::fs::read_to_string(format!(".git/{}", ref_path)).ok()
            } else {
                Some(head.trim().to_string())
            }
        });

    sqlx::query(
        r#"
        INSERT INTO status_log (phase, status, details, commit_sha)
        VALUES ($1, $2, $3, $4)
        "#
    )
    .bind(phase)
    .bind(status)
    .bind(details)
    .bind(commit_sha)
    .execute(pool)
    .await?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_dedupe_articles() {
        let existing_urls: HashSet<String> = HashSet::new();
        let existing_content: HashMap<String, HashSet<String>> = HashMap::new();

        let article = RawArticle {
            id: Uuid::new_v4(),
            url: "https://example.com/1".to_string(),
            url_hash: "abc123".to_string(),
            title: "Test".to_string(),
            body_text: Some("Body text".to_string()),
            summary: None,
            source_domain: "example.com".to_string(),
            source_tier: SourceTier::Tier1,
            published_at: None,
            fetched_at: Utc::now(),
            entities: None,
            minhash_signature: None,
            content_hash: Some("content123".to_string()),
            reporting_unit_id: None,
        };

        let articles = vec![article.clone()];
        let (new, url_dup, content_dup) = dedupe_articles(articles, &existing_urls, &existing_content);
        assert_eq!(new.len(), 1);
        assert_eq!(url_dup, 0);
        assert_eq!(content_dup, 0);

        // Test duplicate URL
        let articles = vec![article.clone(), article];
        let (new, url_dup, _) = dedupe_articles(articles, &existing_urls, &existing_content);
        assert_eq!(new.len(), 1);
        assert_eq!(url_dup, 1);
    }
}