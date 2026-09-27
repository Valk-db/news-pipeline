use crate::models::SourceTier;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

/// Health snapshot returned by a source adapter's health_check()
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceHealth {
    pub status: String, // "ok" | "degraded" | "down"
    pub detail: String,
    pub succeeded: Vec<String>,
    pub failed: Vec<String>,
    pub skipped: Vec<String>,
}

impl Default for SourceHealth {
    fn default() -> Self {
        Self {
            status: "down".to_string(),
            detail: String::new(),
            succeeded: Vec::new(),
            failed: Vec::new(),
            skipped: Vec::new(),
        }
    }
}

/// Common contract every ingestion source implements
#[async_trait::async_trait]
pub trait SourceAdapter: Send + Sync {
    /// Adapter name (e.g. "rss_tier1", "rss_tier2", "gdelt", "reddit_tier3")
    fn name(&self) -> &str;

    /// Fetch and return new articles. Must not raise on a single feed's
    /// failure inside the batch -- log/record it via IngestStats and
    /// continue; only raise for a total adapter failure.
    async fn fetch(&mut self) -> Result<Vec<crate::models::RawArticle>, Box<dyn std::error::Error + Send + Sync>>;

    /// Cheap status check. Reuse per-domain result from most recent fetch()
    /// rather than making a fresh network call.
    async fn health_check(&self) -> SourceHealth;
}

/// Get enabled sources for a tier as a simple domain list
pub async fn get_enabled_domains_by_tier(
    pool: &crate::database::PgPool,
    tier: SourceTier,
) -> Result<Vec<String>, sqlx::Error> {
    // This would query the database or use source_registry
    // For now, return empty - actual implementation uses source_registry
    Ok(Vec::new())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_source_health_default() {
        let health = SourceHealth::default();
        assert_eq!(health.status, "down");
        assert!(health.succeeded.is_empty());
    }
}