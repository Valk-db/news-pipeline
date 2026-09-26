//! Consensus analysis for source reliability - compare source claims against tier-1 baseline.

use crate::database::PgPool;
use crate::models::RawArticle;
use crate::utils::ner::{canonical_jaccard, EntityCanonicalizer};
use crate::llm::LLMClient;
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use sqlx::Row;
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

/// Result of consensus analysis
#[derive(Debug, Serialize, Deserialize)]
pub struct ConsensusResult {
    pub consensus_alignment: i32,
    pub articles_compared: usize,
    pub min_alignment: Option<i32>,
    pub max_alignment: Option<i32>,
    pub std_alignment: Option<f64>,
    pub details: String,
}

/// Consensus analyzer for source reliability
pub struct ConsensusAnalyzer {
    pool: PgPool,
    llm: LLMClient,
    canonicalizer: EntityCanonicalizer,
}

impl ConsensusAnalyzer {
    pub fn new(pool: PgPool, llm: LLMClient, canonicalizer: EntityCanonicalizer) -> Self {
        Self { pool, llm, canonicalizer }
    }

    /// Analyze a source's alignment with tier-1 consensus over a period
    pub async fn analyze_source_consensus(
        &self,
        source_domain: &str,
        days_back: i64,
    ) -> ConsensusResult {
        let cutoff = Utc::now() - Duration::days(days_back);

        // Get tier-1 articles for the same stories as this source
        let tier1_articles = match self.get_tier1_articles_for_source(source_domain, cutoff).await {
            Ok(articles) => articles,
            Err(e) => {
                return ConsensusResult {
                    consensus_alignment: 50,
                    articles_compared: 0,
                    min_alignment: None,
                    max_alignment: None,
                    std_alignment: None,
                    details: format!("Error fetching tier-1 articles: {}", e),
                };
            }
        };

        if tier1_articles.is_empty() {
            return ConsensusResult {
                consensus_alignment: 50,
                articles_compared: 0,
                min_alignment: None,
                max_alignment: None,
                std_alignment: None,
                details: "No tier-1 articles found for comparison".to_string(),
            };
        }

        // Get this source's articles
        let source_articles = match self.get_source_articles(source_domain, cutoff).await {
            Ok(articles) => articles,
            Err(e) => {
                return ConsensusResult {
                    consensus_alignment: 50,
                    articles_compared: 0,
                    min_alignment: None,
                    max_alignment: None,
                    std_alignment: None,
                    details: format!("Error fetching source articles: {}", e),
                };
            }
        };

        if source_articles.is_empty() {
            return ConsensusResult {
                consensus_alignment: 50,
                articles_compared: 0,
                min_alignment: None,
                max_alignment: None,
                std_alignment: None,
                details: "No source articles found".to_string(),
            };
        }

        // Compute consensus embeddings per story from tier-1
        let consensus_embeddings = match self.compute_story_consensus_embeddings(&tier1_articles).await {
            Ok(emb) => emb,
            Err(e) => {
                return ConsensusResult {
                    consensus_alignment: 50,
                    articles_compared: 0,
                    min_alignment: None,
                    max_alignment: None,
                    std_alignment: None,
                    details: format!("Error computing consensus embeddings: {}", e),
                };
            }
        };

        // Compare source articles to consensus using entity overlap as proxy for embeddings
        let mut alignments = Vec::new();

        for article in source_articles {
            if let Some(story_id) = self.get_story_for_article(&article.id).await {
                if let Some(consensus_entities) = consensus_embeddings.get(&story_id) {
                    let source_entities = if let Some(ref body) = article.body_text {
                self.extract_entities_from_text(body).await
            } else {
                Vec::new()
            };
                    let source_entity_set: HashSet<String> = source_entities.into_iter().collect();

                    let jaccard = canonical_jaccard(&source_entity_set, consensus_entities);
                    alignments.push(jaccard);
                }
            }
        }

        if alignments.is_empty() {
            return ConsensusResult {
                consensus_alignment: 50,
                articles_compared: 0,
                min_alignment: None,
                max_alignment: None,
                std_alignment: None,
                details: "No alignments computed".to_string(),
            };
        }

        // Average alignment (0-1 -> 0-100)
        let avg_alignment = (alignments.iter().sum::<f64>() / alignments.len() as f64) * 100.0;
        let min_align = alignments.iter().cloned().fold(f64::INFINITY, f64::min) * 100.0;
        let max_align = alignments.iter().cloned().fold(f64::NEG_INFINITY, f64::max) * 100.0;

        // Standard deviation
        let mean = alignments.iter().sum::<f64>() / alignments.len() as f64;
        let variance = alignments.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / alignments.len() as f64;
        let std_dev = variance.sqrt() * 100.0;

        ConsensusResult {
            consensus_alignment: avg_alignment.round() as i32,
            articles_compared: alignments.len(),
            min_alignment: Some(min_align.round() as i32),
            max_alignment: Some(max_align.round() as i32),
            std_alignment: Some(std_dev),
            details: format!("Compared {} articles", alignments.len()),
        }
    }

    /// Get tier-1 articles that cover the same stories as the given source
    async fn get_tier1_articles_for_source(
        &self,
        source_domain: &str,
        cutoff: DateTime<Utc>,
    ) -> Result<Vec<RawArticle>, sqlx::Error> {
        // Get stories covered by this source
        let story_ids: Vec<Uuid> = sqlx::query_scalar(
            r#"
            SELECT DISTINCT s.id
            FROM stories s
            JOIN story_unit_links sul ON sul.story_id = s.id
            JOIN reporting_units ru ON ru.id = sul.unit_id
            JOIN raw_articles ra ON ra.id = ru.representative_article_id
            WHERE ra.source_domain = $1 AND ra.fetched_at >= $2
            "#
        )
        .bind(source_domain)
        .bind(cutoff)
        .fetch_all(&self.pool)
        .await?;

        if story_ids.is_empty() {
            return Ok(Vec::new());
        }

        // Get tier-1 articles for these stories
        let rows = sqlx::query(
            r#"
            SELECT ra.* FROM raw_articles ra
            JOIN reporting_units ru ON ra.id = ru.representative_article_id
            JOIN story_unit_links sul ON sul.unit_id = ru.id
            WHERE sul.story_id = ANY($1)
            AND ra.source_tier = 'tier1'
            AND ra.fetched_at >= $2
            "#
        )
        .bind(&story_ids)
        .bind(cutoff)
        .fetch_all(&self.pool)
        .await?;

        let mut articles = Vec::new();
        for row in rows {
            articles.push(RawArticle {
                id: row.get("id"),
                url: row.get("url"),
                url_hash: row.get("url_hash"),
                title: row.get("title"),
                body_text: row.get("body_text"),
                summary: row.get("summary"),
                source_domain: row.get("source_domain"),
                source_tier: row.get("source_tier"),
                published_at: row.get("published_at"),
                fetched_at: row.get("fetched_at"),
                entities: row.get("entities"),
                minhash_signature: row.get("minhash_signature"),
                content_hash: row.get("content_hash"),
                reporting_unit_id: row.get("reporting_unit_id"),
            });
        }

        Ok(articles)
    }

    /// Get articles from a specific source
    async fn get_source_articles(
        &self,
        source_domain: &str,
        cutoff: DateTime<Utc>,
    ) -> Result<Vec<RawArticle>, sqlx::Error> {
        let rows = sqlx::query(
            r#"
            SELECT * FROM raw_articles
            WHERE source_domain = $1
            AND fetched_at >= $2
            AND body_text IS NOT NULL
            AND body_text != ''
            "#
        )
        .bind(source_domain)
        .bind(cutoff)
        .fetch_all(&self.pool)
        .await?;

        let mut articles = Vec::new();
        for row in rows {
            articles.push(RawArticle {
                id: row.get("id"),
                url: row.get("url"),
                url_hash: row.get("url_hash"),
                title: row.get("title"),
                body_text: row.get("body_text"),
                summary: row.get("summary"),
                source_domain: row.get("source_domain"),
                source_tier: row.get("source_tier"),
                published_at: row.get("published_at"),
                fetched_at: row.get("fetched_at"),
                entities: row.get("entities"),
                minhash_signature: row.get("minhash_signature"),
                content_hash: row.get("content_hash"),
                reporting_unit_id: row.get("reporting_unit_id"),
            });
        }

        Ok(articles)
    }

    /// Compute consensus entity sets for each story from tier-1 articles
    async fn compute_story_consensus_embeddings(
        &self,
        tier1_articles: &[RawArticle],
    ) -> Result<HashMap<Uuid, HashSet<String>>, sqlx::Error> {
        // Group articles by story
        let mut story_articles: HashMap<Uuid, Vec<RawArticle>> = HashMap::new();

        for article in tier1_articles {
            if let Some(story_id) = self.get_story_for_article(&article.id).await {
                story_articles.entry(story_id).or_default().push(article.clone());
            }
        }

        // Compute consensus entity sets per story
        let mut consensus = HashMap::new();
        for (story_id, articles) in story_articles {
            if articles.len() < 2 {
                continue; // Need at least 2 for meaningful consensus
            }

            // Extract entities from all tier-1 articles in this story
            let mut all_entities = HashSet::new();
            for article in articles {
                if let Some(body) = &article.body_text {
                    let entities = self.extract_entities_from_text(body).await;
                    all_entities.extend(entities);
                }
            }

            if !all_entities.is_empty() {
                consensus.insert(story_id, all_entities);
            }
        }

        Ok(consensus)
    }

    /// Get the story an article belongs to
    async fn get_story_for_article(&self, article_id: &Uuid) -> Option<Uuid> {
        sqlx::query_scalar(
            r#"
            SELECT s.id FROM stories s
            JOIN story_unit_links sul ON sul.story_id = s.id
            JOIN reporting_units ru ON ru.id = sul.unit_id
            WHERE ru.representative_article_id = $1
            "#
        )
        .bind(article_id)
        .fetch_optional(&self.pool)
        .await
        .ok()
        .flatten()
    }

    /// Extract entities from text using LLM (placeholder - will be replaced with T5 NER)
    async fn extract_entities_from_text(&self, text: &str) -> Vec<String> {
        // For now, use a simple keyword-based approach
        // In T5, this will use rust-bert NER
        let text_lower = text.to_lowercase();
        let mut entities = Vec::new();

        let known_entities = [
            "biden", "putin", "zelensky", "xi", "trump", "netanyahu", "hamas", "hezbollah",
            "un", "eu", "nato", "who", "us", "uk", "russia", "ukraine", "israel", "gaza",
            "china", "taiwan", "iran", "north korea", "south korea", "japan"
        ];

        for entity in known_entities {
            if text_lower.contains(entity) {
                entities.push(entity.to_uppercase());
            }
        }

        entities
    }
}

/// Compute daily reliability snapshots for all sources
pub async fn compute_daily_reliability_snapshots(
    pool: &PgPool,
    llm: &LLMClient,
    canonicalizer: &EntityCanonicalizer,
    date: Option<DateTime<Utc>>,
) -> Result<i32, sqlx::Error> {
    let date = date.unwrap_or_else(|| {
        let now = Utc::now();
        now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc()
    });

    // Get all sources that have articles in the last 30 days
    let cutoff = date - Duration::days(30);

    let source_domains: Vec<String> = sqlx::query_scalar(
        r#"
        SELECT DISTINCT source_domain FROM raw_articles
        WHERE fetched_at >= $1
        "#
    )
    .bind(cutoff)
    .fetch_all(pool)
    .await?;

    // Pass references instead of cloning
    let analyzer = ConsensusAnalyzer::new(pool.clone(), (*llm).clone(), canonicalizer.clone());
    let mut snapshots_created = 0;

    for domain in source_domains {
        // Check if snapshot already exists
        let existing: Option<(i32,)> = sqlx::query_as(
            r#"
            SELECT id FROM source_reliability_snapshots
            WHERE source_domain = $1 AND snapshot_date = $2
            "#
        )
        .bind(&domain)
        .bind(date)
        .fetch_optional(pool)
        .await?;

        if existing.is_some() {
            continue;
        }

        // Get fact-check stats for this source
        let fact_check_stats = get_fact_check_stats(pool, &domain, date).await;

        // Get correction stats
        let correction_stats = get_correction_stats(pool, &domain, date).await;

        // Get consensus alignment
        let consensus = analyzer.analyze_source_consensus(&domain, 30).await;

        // Get tier at snapshot (simplified)
        let tier = get_source_tier(&domain);

        // Compute composite reliability score
        // Weights: factual=0.4, correction=0.2, consensus=0.3, transparency=0.1
        let factual = fact_check_stats.factual_accuracy;
        let correction = (100 - correction_stats.correction_rate).max(0); // Invert
        let consensus_score = consensus.consensus_alignment;
        let transparency = compute_transparency_score(&domain);

        let reliability_score = ((0.4 * factual as f64
            + 0.2 * correction as f64
            + 0.3 * consensus_score as f64
            + 0.1 * transparency as f64) as i32).clamp(0, 100);

        // Create snapshot
        sqlx::query(
            r#"
            INSERT INTO source_reliability_snapshots
            (source_domain, snapshot_date, factual_accuracy, correction_rate, consensus_alignment,
             transparency_score, reliability_score, total_claims_verified, claims_true, claims_false,
             claims_mixed, corrections_count, articles_sampled, tier_at_snapshot, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            "#
        )
        .bind(&domain)
        .bind(date)
        .bind(factual)
        .bind(correction_stats.correction_rate)
        .bind(consensus_score)
        .bind(transparency)
        .bind(reliability_score)
        .bind(fact_check_stats.total_verified)
        .bind(fact_check_stats.true_count)
        .bind(fact_check_stats.false_count)
        .bind(fact_check_stats.mixed_count)
        .bind(correction_stats.count)
        .bind(correction_stats.articles_sampled)
        .bind(tier)
        .bind(Utc::now())
        .execute(pool)
        .await?;

        snapshots_created += 1;
    }

    Ok(snapshots_created)
}

#[derive(Debug)]
struct FactCheckStats {
    factual_accuracy: i32,
    total_verified: i32,
    true_count: i32,
    false_count: i32,
    mixed_count: i32,
}

async fn get_fact_check_stats(
    pool: &PgPool,
    source_domain: &str,
    snapshot_date: DateTime<Utc>,
) -> FactCheckStats {
    let rows = sqlx::query(
        r#"
        SELECT * FROM fact_check_records
        WHERE source_domain = $1 AND checked_at <= $2
        "#
    )
    .bind(source_domain)
    .bind(snapshot_date)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let total = rows.len() as i32;
    if total == 0 {
        return FactCheckStats {
            factual_accuracy: 50,
            total_verified: 0,
            true_count: 0,
            false_count: 0,
            mixed_count: 0,
        };
    }

    let mut true_count = 0;
    let mut false_count = 0;
    let mut mixed_count = 0;

    for row in rows {
        let verdict: String = row.get("verdict");
        if matches!(verdict.as_str(), "true" | "mostly_true") {
            true_count += 1;
        } else if matches!(verdict.as_str(), "false" | "mostly_false") {
            false_count += 1;
        } else if verdict == "mixed" {
            mixed_count += 1;
        }
    }

    let verified = true_count + false_count + mixed_count;
    let factual_accuracy = if verified > 0 {
        ((true_count as f64 / verified as f64) * 100.0).round() as i32
    } else {
        50
    };

    FactCheckStats {
        factual_accuracy,
        total_verified: verified,
        true_count,
        false_count,
        mixed_count,
    }
}

#[derive(Debug)]
struct CorrectionStats {
    correction_rate: i32,
    count: i32,
    articles_sampled: i32,
}

async fn get_correction_stats(
    pool: &PgPool,
    source_domain: &str,
    snapshot_date: DateTime<Utc>,
) -> CorrectionStats {
    let cutoff = snapshot_date - Duration::days(30);

    let rows = sqlx::query(
        r#"
        SELECT * FROM correction_records
        WHERE source_domain = $1 AND correction_date <= $2
        "#
    )
    .bind(source_domain)
    .bind(snapshot_date)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let count = rows.len() as i32;
    let article_ids: HashSet<_> = rows.iter()
        .filter_map(|r| r.try_get::<Option<Uuid>, _>("article_id").ok().flatten())
        .collect();
    let articles_sampled = article_ids.len() as i32;

    // Get total articles from this source in period
    let total_articles: i64 = sqlx::query_scalar(
        r#"
        SELECT COUNT(*) FROM raw_articles
        WHERE source_domain = $1 AND fetched_at >= $2
        "#
    )
    .bind(source_domain)
    .bind(cutoff)
    .fetch_one(pool)
    .await
    .unwrap_or(1);

    let correction_rate = if total_articles > 0 {
        ((count as f64 / total_articles as f64) * 100.0).round() as i32
    } else {
        0
    };

    CorrectionStats {
        correction_rate,
        count,
        articles_sampled,
    }
}

fn compute_transparency_score(domain: &str) -> i32 {
    let transparency_domains = [
        ("apnews.com", 100), ("reuters.com", 100), ("bbc.com", 90), ("theguardian.com", 90),
        ("npr.org", 95), ("nytimes.com", 85), ("washingtonpost.com", 80), ("wsj.com", 80),
        ("economist.com", 85), ("ft.com", 80), ("latimes.com", 75), ("pbs.org", 85),
        ("france24.com", 70), ("dw.com", 70), ("aljazeera.com", 60), ("euronews.com", 65),
    ];

    transparency_domains.iter()
        .find(|(d, _)| *d == domain)
        .map(|(_, score)| *score)
        .unwrap_or(40)
}

fn get_source_tier(domain: &str) -> String {
    let tier1 = ["apnews.com", "reuters.com", "bbc.com", "theguardian.com", "npr.org",
                 "dw.com", "france24.com", "aljazeera.com", "euronews.com", "pbs.org"];
    let tier2 = ["nytimes.com", "washingtonpost.com", "wsj.com", "ft.com", "economist.com",
                 "foreignpolicy.com", "foreignaffairs.com", "csis.org", "who.int",
                 "latimes.com", "chicagotribune.com", "bostonglobe.com"];

    if tier1.contains(&domain) { "tier1" }
    else if tier2.contains(&domain) { "tier2" }
    else if domain == "reddit.com" || domain == "bsky.social" { "tier3" }
    else { "tier4" }.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compute_transparency_score() {
        assert_eq!(compute_transparency_score("apnews.com"), 100);
        assert_eq!(compute_transparency_score("bbc.com"), 90);
        assert_eq!(compute_transparency_score("unknown.com"), 40);
    }

    #[test]
    fn test_get_source_tier() {
        assert_eq!(get_source_tier("bbc.com"), "tier1");
        assert_eq!(get_source_tier("nytimes.com"), "tier2");
        assert_eq!(get_source_tier("reddit.com"), "tier3");
        assert_eq!(get_source_tier("unknown.com"), "tier4");
    }
}