use crate::config::Settings;
use crate::ingestion::adapter::{SourceAdapter, SourceHealth};
use crate::models::{RawArticle, SourceTier};
use crate::utils::ner::extract_entities_top_n;
use crate::utils::trafilatura_extract::{compute_content_hash, compute_url_hash, extract_article};
use async_trait::async_trait;
use feed_rs::parser;
use futures::stream::{self, StreamExt};
use reqwest::Client;
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Semaphore;
use tracing::{error, info, warn};
use uuid::Uuid;

/// Source configuration for RSS feeds
#[derive(Debug, Clone)]
pub struct SourceConfig {
    pub domain: String,
    pub name: String,
    pub tier: SourceTier,
    pub rss_urls: Vec<String>,
}

/// Feed entry from RSS
#[derive(Debug, Clone)]
struct FeedEntry {
    pub title: String,
    pub link: String,
    pub summary: Option<String>,
    pub published: Option<chrono::DateTime<chrono::Utc>>,
}

/// Fetch and parse a single RSS feed with retry logic
async fn fetch_feed(
    client: &Client,
    feed_url: &str,
    timeout: Duration,
    _source_key: &str,
    settings: &Settings,
) -> Option<feed_rs::model::Feed> {
    let max_retries = settings.rss_max_retries;
    let retry_delay = Duration::from_secs_f64(settings.rss_retry_delay);

    for attempt in 0..max_retries {
        eprintln!("[DEBUG] Attempting to fetch feed: {} (attempt {}/{})", feed_url, attempt + 1, max_retries);
        warn!("Attempting to fetch feed: {} (attempt {}/{})", feed_url, attempt + 1, max_retries);
        match client.get(feed_url).timeout(timeout).send().await {
            Ok(response) => {
                let status = response.status();
                let status_str = format!("{}", status);
                eprintln!("[DEBUG] Feed {} returned HTTP {}", feed_url, status_str);
                warn!("Feed {} returned HTTP {}", feed_url, status_str);
                if status.is_success() {
                    let text = match response.text().await {
                        Ok(t) => t,
                        Err(e) => {
                            warn!("Failed to read response body for {}: {}", feed_url, e);
                            if attempt < max_retries - 1 {
                                tokio::time::sleep(retry_delay).await;
                                continue;
                            }
                            return None;
                        }
                    };
                    eprintln!("[DEBUG] Successfully fetched feed: {} (status: {})", feed_url, status_str);
                    eprintln!("[DEBUG] Feed response preview (first 500 chars): {}", &text[..text.len().min(500)]);
                    let feed = parser::parse(text.as_bytes());
                    match feed {
                        Ok(f) => {
                            eprintln!("[DEBUG] Parsed feed: {} entries", f.entries.len());
                            if f.entries.is_empty() {
                                eprintln!("[DEBUG] WARNING: Feed has 0 entries!");
                            }
                            return Some(f);
                        }
                        Err(e) => {
                            eprintln!("[DEBUG] Failed to parse feed {}: {}", feed_url, e);
                            error!("Failed to parse feed {}: {}", feed_url, e);
                            return None;
                        }
                    }
                }

                let status = response.status().as_u16();
                let should_retry = status >= 500 || status == 429;

                if should_retry && attempt < max_retries - 1 {
                    warn!(
                        "Failed to fetch {} (attempt {}/{}): HTTP {}, retrying in {:?}...",
                        feed_url, attempt + 1, max_retries, status, retry_delay
                    );
                    tokio::time::sleep(retry_delay).await;
                } else {
                    error!("Failed to fetch {} after {} attempt(s): HTTP {}", feed_url, attempt + 1, status);
                    return None;
                }
            }
            Err(e) => {
                // Network errors (connection refused, DNS, timeout, etc.) - retry
                warn!("Network error fetching {} (attempt {}/{}): {}", feed_url, attempt + 1, max_retries, e);
                if attempt < max_retries - 1 {
                    warn!("Retrying in {:?}...", retry_delay);
                    tokio::time::sleep(retry_delay).await;
                } else {
                    error!("Failed to fetch {} after {} attempts: {}", feed_url, max_retries, e);
                    return None;
                }
            }
        }
    }
    None
}

/// Convert feed-rs entry to our FeedEntry
fn parse_feed_entry(entry: &feed_rs::model::Entry) -> Option<FeedEntry> {
    let link = entry.links.iter().find(|l| {
        l.rel.as_deref() == Some("alternate") || l.rel.as_deref() == Some("")
    }).map(|l| l.href.clone())?;
    let title = entry.title.as_ref()?.content.trim().to_string();
    let summary = entry.summary.as_ref().map(|s| s.content.clone());
    let published = entry.published.or(entry.updated).map(|dt| chrono::DateTime::<chrono::Utc>::from(dt));

    Some(FeedEntry { title, link, summary, published })
}

/// Process a single feed entry into a RawArticle
async fn process_feed_entry(
    entry: &FeedEntry,
    source_info: &SourceConfig,
    seen_urls: &Arc<tokio::sync::Mutex<HashSet<String>>>,
    source_key: &str,
    settings: &Settings,
) -> Option<RawArticle> {
    let url = &entry.link;
    if url.is_empty() {
        return None;
    }

    let url_hash = compute_url_hash(url);

    // Reserve URL in seen_urls before extraction (dedup lock)
    {
        let mut seen = seen_urls.lock().await;
        if seen.contains(&url_hash) {
            return None;
        }
        seen.insert(url_hash.clone());
    }

    // Extract article body
    eprintln!("[DEBUG] process_feed_entry: Calling extract_article for URL: {}", url);
    let (body_text, extracted_title) = extract_article(url, Some(source_key)).await;
    eprintln!("[DEBUG] process_feed_entry: extract_article returned body={:?}, title={:?}", body_text.as_ref().map(|b| b.len()), extracted_title);
    let body_text = body_text?;
    eprintln!("[DEBUG] process_feed_entry: body_text len = {}", body_text.len());

    if body_text.len() < 200 {
        eprintln!("[DEBUG] process_feed_entry: Body too short ({}) for URL: {}", body_text.len(), url);
        return None;
    }

    let title = extracted_title.unwrap_or_else(|| entry.title.clone());

    // Extract entities using rust-bert NER
    let top_n = settings.top_n_entities.try_into().unwrap_or(10);
    let entities_dict = extract_entities_top_n(&body_text, Some(top_n)).await;
    let entities = Some(serde_json::to_value(&entities_dict).unwrap_or(serde_json::json!({})));

    // Compute content hash for exact dedup
    let content_hash = compute_content_hash(&body_text);

    // Build RawArticle
    Some(RawArticle {
        id: uuid::Uuid::new_v4(),
        url: url.clone(),
        url_hash,
        title,
        body_text: Some(body_text),
        summary: entry.summary.clone(),
        source_domain: source_info.domain.clone(),
        source_tier: source_info.tier,
        published_at: entry.published,
        fetched_at: chrono::Utc::now(),
        entities,
        minhash_signature: None,
        content_hash: Some(content_hash),
        reporting_unit_id: None,
    })
}

/// Ingest all configured RSS feeds
pub async fn ingest_rss_feeds(
    max_per_feed: usize,
    sources: Vec<SourceConfig>,
    settings: &Settings,
) -> Vec<RawArticle> {
    let timeout = Duration::from_secs(settings.rss_fetch_timeout as u64);
    let client = Client::new();
    let seen_urls = Arc::new(tokio::sync::Mutex::new(HashSet::new()));
    let mut articles = Vec::new();

    // Build list of (source_key, feed_url, source_info) tuples
    let mut feed_tasks = Vec::new();
    for source in &sources {
        for feed_url in &source.rss_urls {
            feed_tasks.push((source.domain.clone(), feed_url.clone(), source.clone()));
        }
    }

    // Fetch all feeds concurrently with bounded semaphore
    let fetch_sem = Arc::new(Semaphore::new(10));

    let fetch_futures = feed_tasks.into_iter().map(|(source_key, feed_url, source_info)| {
        let client = client.clone();
        let sem = fetch_sem.clone();
        let settings = settings.clone();
        let timeout = timeout;

        async move {
            let _permit = sem.acquire().await.unwrap();
            let feed = fetch_feed(&client, &feed_url, timeout, &source_key, &settings).await;
            (source_key, feed_url, source_info, feed)
        }
    });

    let fetched = stream::iter(fetch_futures).buffer_unordered(10).collect::<Vec<_>>().await;

    // Process entries with bounded concurrency
    let extract_sem = Arc::new(Semaphore::new(15));
    let mut process_tasks = Vec::new();

    for (source_key, feed_url, source_info, feed) in fetched {
        if let Some(feed) = feed {
            warn!("Processing {} entries from {} ({})", feed.entries.len(), source_info.name, feed_url);
            for (i, entry) in feed.entries.iter().enumerate() {
                if i >= max_per_feed {
                    break;
                }
                let title = entry.title.as_ref().map(|t| t.content.as_str()).unwrap_or("");
                let link = entry.links.iter().find(|l| l.rel.as_deref() == Some("alternate") || l.rel.as_deref() == Some("")).map(|l| l.href.as_str()).unwrap_or("");
                warn!("  Entry {}: title='{}', link='{}'", i, title, link);
                if let Some(parsed) = parse_feed_entry(entry) {
                    warn!("    Parsed: title='{}', link='{}'", parsed.title, parsed.link);
                } else {
                    warn!("    Failed to parse entry");
                }
            }
            for entry in feed.entries.iter().take(max_per_feed).map(parse_feed_entry).flatten() {
                let seen_urls = seen_urls.clone();
                let source_info = source_info.clone();
                let source_key = source_key.clone();
                let settings = settings.clone();
                let sem = extract_sem.clone();

                process_tasks.push(async move {
                    let _permit = sem.acquire().await.unwrap();
                    process_feed_entry(&entry, &source_info, &seen_urls, &source_key, &settings).await
                });
            }
        }
    }

    // Run extraction concurrently
    let results = stream::iter(process_tasks).buffer_unordered(15).collect::<Vec<_>>().await;

    // Flatten results
    for article in results.into_iter().flatten() {
        articles.push(article);
    }

    articles
}

/// Get enabled tier-1 sources from registry
pub fn get_tier1_sources() -> Vec<SourceConfig> {
    vec![
        SourceConfig {
            domain: "bbc.com".to_string(),
            name: "BBC News".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://feeds.bbci.co.uk/news/world/rss.xml".to_string(),
                "https://feeds.bbci.co.uk/news/uk/rss.xml".to_string(),
                "https://feeds.bbci.co.uk/news/politics/rss.xml".to_string(),
            ],
        },
        SourceConfig {
            domain: "theguardian.com".to_string(),
            name: "The Guardian".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://www.theguardian.com/world/rss".to_string(),
                "https://www.theguardian.com/uk-news/rss".to_string(),
                "https://www.theguardian.com/politics/rss".to_string(),
            ],
        },
        SourceConfig {
            domain: "npr.org".to_string(),
            name: "NPR".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://feeds.npr.org/1001/rss.xml".to_string(),
                "https://feeds.npr.org/1003/rss.xml".to_string(),
                "https://feeds.npr.org/1004/rss.xml".to_string(),
            ],
        },
        SourceConfig {
            domain: "dw.com".to_string(),
            name: "Deutsche Welle".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://rss.dw.com/rdf/rss-en-all".to_string(),
            ],
        },
        SourceConfig {
            domain: "france24.com".to_string(),
            name: "France 24".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://www.france24.com/en/rss".to_string(),
            ],
        },
        SourceConfig {
            domain: "aljazeera.com".to_string(),
            name: "Al Jazeera English".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://www.aljazeera.com/xml/rss/all.xml".to_string(),
            ],
        },
        SourceConfig {
            domain: "euronews.com".to_string(),
            name: "Euronews".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://www.euronews.com/rss?level=theme&name=world".to_string(),
            ],
        },
        SourceConfig {
            domain: "pbs.org".to_string(),
            name: "PBS NewsHour".to_string(),
            tier: SourceTier::Tier1,
            rss_urls: vec![
                "https://www.pbs.org/newshour/feeds/rss.xml".to_string(),
            ],
        },
    ]
}

/// RSS Adapter implementing SourceAdapter trait
#[derive(Debug)]
pub struct RssAdapter {
    configs: Vec<SourceConfig>,
    tier: SourceTier,
    fetch_timeout: u64,
    max_retries: u32,
    retry_delay: f64,
    last_articles: Vec<RawArticle>,
    last_stats: std::collections::HashMap<String, u64>,
    fetch_called: bool,
}

impl RssAdapter {
    pub fn new(
        configs: Vec<SourceConfig>,
        tier: SourceTier,
        fetch_timeout: u64,
        max_retries: u32,
        retry_delay: f64,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        Ok(Self {
            configs,
            tier,
            fetch_timeout,
            max_retries,
            retry_delay,
            last_articles: Vec::new(),
            last_stats: std::collections::HashMap::new(),
            fetch_called: false,
        })
    }
}

#[async_trait]
impl SourceAdapter for RssAdapter {
    fn name(&self) -> &str {
        match self.tier {
            SourceTier::Tier1 => "rss_tier1",
            SourceTier::Tier2 => "rss_tier2",
            _ => "rss",
        }
    }

    async fn fetch(&mut self) -> Result<Vec<RawArticle>, Box<dyn std::error::Error + Send + Sync>> {
        let settings = crate::config::Settings {
            rss_fetch_timeout: self.fetch_timeout as i64,
            rss_max_retries: self.max_retries as i64,
            rss_retry_delay: self.retry_delay,
            ..Default::default()
        };

        let articles = ingest_rss_feeds(50, self.configs.clone(), &settings).await;
        self.last_articles = articles.clone();
        self.last_stats = crate::utils::ingest_stats::STATS.snapshot();
        self.fetch_called = true;
        Ok(articles)
    }

    async fn health_check(&self) -> SourceHealth {
        if !self.fetch_called {
            return SourceHealth {
                status: "down".to_string(),
                detail: "No fetch performed yet".to_string(),
                failed: self.configs.iter().map(|c| c.domain.clone()).collect(),
                succeeded: Vec::new(),
                skipped: Vec::new(),
            };
        }

        // Check per-domain success for tier-1
        if self.tier == SourceTier::Tier1 {
            let mut failed_domains = Vec::new();
            for config in &self.configs {
                let ok_count = self.last_stats.get(&format!("{}.ok", config.domain)).copied().unwrap_or(0);
                if ok_count == 0 {
                    failed_domains.push(config.domain.clone());
                }
            }

            if !failed_domains.is_empty() {
                return SourceHealth {
                    status: "degraded".to_string(),
                    detail: format!("Tier-1 domains with zero articles: {}", failed_domains.join(", ")),
                    failed: failed_domains.clone(),
                    succeeded: self.configs.iter().map(|c| c.domain.clone()).filter(|d| !failed_domains.contains(d)).collect(),
                    skipped: Vec::new(),
                };
            }
        }

        // For tier-2 or if all tier-1 domains succeeded
        let succeeded_domains: Vec<String> = self.last_articles.iter().map(|a| a.source_domain.clone()).collect();
        SourceHealth {
            status: "ok".to_string(),
            detail: format!("Fetched {} articles", self.last_articles.len()),
            succeeded: succeeded_domains,
            failed: Vec::new(),
            skipped: Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_source_config() {
        let sources = get_tier1_sources();
        assert!(!sources.is_empty());
        for source in &sources {
            assert!(!source.domain.is_empty());
            assert!(!source.rss_urls.is_empty());
        }
    }
}