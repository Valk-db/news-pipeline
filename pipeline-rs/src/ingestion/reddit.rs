use crate::config::Settings;
use crate::models::{RawArticle, SourceTier};
use crate::utils::trafilatura_extract::{compute_content_hash, compute_url_hash, extract_article};
use feed_rs::parser;
use reqwest::Client;
use std::collections::HashSet;
use std::time::Duration;
use tokio::time;
use tracing::{info, warn};

/// Target subreddits for geopolitics/news
const TARGET_SUBREDDITS: &[&str] = &[
    "worldnews",
    "geopolitics",
    "news",
    "politics",
    "europe",
    "middleeast",
    "china",
    "russia",
    "ukraine",
    "credibledefense",
    "lesscredibledefense",
];

/// Reddit throttles anonymous RSS traffic; space subreddit fetches out.
const REDDIT_FETCH_DELAY_SECONDS: f64 = 3.0;

const NON_ARTICLE_EXTENSIONS: &[&str] = &[".jpg", ".jpeg", ".png", ".gif", ".gifv", ".mp4", ".webm"];

/// Feed entry from RSS
#[derive(Debug, Clone)]
struct FeedEntry {
    pub title: String,
    pub link: String,
    pub summary: Option<String>,
    pub published: Option<chrono::DateTime<chrono::Utc>>,
}

/// Fetch and parse a single Reddit RSS feed
async fn fetch_reddit_feed(
    client: &Client,
    feed_url: &str,
    timeout: Duration,
) -> Option<feed_rs::model::Feed> {
    match client.get(feed_url).timeout(timeout).send().await {
        Ok(response) => {
            if response.status().is_success() {
                match response.text().await {
                    Ok(text) => parser::parse(text.as_bytes()).ok(),
                    Err(e) => {
                        warn!("Failed to read response body for {}: {}", feed_url, e);
                        None
                    }
                }
            } else {
                warn!("HTTP {} fetching {}", response.status(), feed_url);
                None
            }
        }
        Err(e) => {
            warn!("Failed to fetch {}: {}", feed_url, e);
            None
        }
    }
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

/// Extract the outbound URL from Reddit RSS entry summary HTML
fn extract_outbound_url(summary: &str, comments_url: &str) -> Option<String> {
    if summary.is_empty() {
        return None;
    }

    // Simple HTML parsing to find "[link]" anchor
    use regex::Regex;
    let link_re = Regex::new(r#"<a[^>]*href="([^"]*)"[^>]*>\[link\]</a>"#).ok()?;
    let caps = link_re.captures(summary)?;
    let href = caps.get(1)?.as_str();

    if href.is_empty() || href == comments_url {
        return None;
    }

    Some(href.to_string())
}

/// Process a single Reddit RSS entry into a RawArticle
async fn process_entry(
    entry: &FeedEntry,
    source_key: &str,
    settings: &Settings,
    seen_hashes: &mut HashSet<String>,
) -> Option<RawArticle> {
    let comments_url = &entry.link;
    let url = extract_outbound_url(entry.summary.as_deref().unwrap_or(""), comments_url)?;

    if NON_ARTICLE_EXTENSIONS.iter().any(|ext| url.to_lowercase().ends_with(ext)) {
        return None;
    }

    let url_hash = compute_url_hash(&url);
    if seen_hashes.contains(&url_hash) {
        return None;
    }

    // Extract article body
    let (body_text, extracted_title) = extract_article(&url, Some(source_key)).await;
    let body_text = body_text?;

    if body_text.len() < 200 {
        return None;
    }

    let title = extracted_title.unwrap_or_else(|| entry.title.clone());
    if title.is_empty() {
        return None;
    }

    // Parse date
    let published_at = entry.published;

    // Content hash
    let content_hash = compute_content_hash(&body_text);

    // Add to seen hashes
    seen_hashes.insert(url_hash.clone());

    // Build RawArticle (Tier 3 for Reddit - unverified social)
    Some(RawArticle {
        id: uuid::Uuid::new_v4(),
        url,
        url_hash,
        title,
        body_text: Some(body_text),
        summary: None,
        source_domain: "reddit.com".to_string(),
        source_tier: SourceTier::Tier3,
        published_at,
        fetched_at: chrono::Utc::now(),
        entities: None,
        minhash_signature: None,
        content_hash: Some(content_hash),
        reporting_unit_id: None,
    })
}

/// Ingest top submissions from target subreddits via public RSS (no auth)
pub async fn ingest_reddit(
    subreddits: Option<Vec<String>>,
    limit_per_sub: usize,
    _time_filter: &str, // "day", "week", "month", "year", "all"
    settings: &Settings,
) -> Vec<RawArticle> {
    let timeout = Duration::from_secs(settings.rss_fetch_timeout as u64);
    let client = Client::builder()
        .user_agent(&settings.reddit_user_agent)
        .timeout(timeout)
        .build()
        .expect("Failed to create HTTP client");

    let subreddits = subreddits.unwrap_or_else(|| TARGET_SUBREDDITS.iter().map(|s| s.to_string()).collect());
    let mut articles = Vec::new();
    let mut seen_hashes = HashSet::new();
    let delay = Duration::from_secs_f64(REDDIT_FETCH_DELAY_SECONDS);

    for (i, sub_name) in subreddits.iter().enumerate() {
        if i > 0 {
            time::sleep(delay).await;
        }

        // Reddit's top.rss feed is pre-sorted by score (descending)
        let feed_url = format!("https://www.reddit.com/r/{}/top.rss?t=day&limit={}", sub_name, limit_per_sub);
        info!("Fetching Reddit feed: {}", feed_url);

        let feed = fetch_reddit_feed(&client, &feed_url, timeout).await;
        if let Some(feed) = feed {
            for entry in feed.entries.iter().take(limit_per_sub).map(parse_feed_entry).flatten() {
                if let Some(article) = process_entry(&entry, "reddit", settings, &mut seen_hashes).await {
                    articles.push(article);
                }
            }
        }
    }

    articles
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_outbound_url() {
        let summary = r#"<p>Some text <a href="https://example.com/article">[link]</a> more text</p>"#;
        let comments_url = "https://www.reddit.com/r/worldnews/comments/abc123/";
        let url = extract_outbound_url(summary, comments_url);
        assert_eq!(url, Some("https://example.com/article".to_string()));
    }

    #[test]
    fn test_extract_outbound_url_self_post() {
        let summary = r#"<p>Some text <a href="https://www.reddit.com/r/worldnews/comments/abc123/">[link]</a> more text</p>"#;
        let comments_url = "https://www.reddit.com/r/worldnews/comments/abc123/";
        let url = extract_outbound_url(summary, comments_url);
        assert_eq!(url, None);
    }

    #[test]
    fn test_extract_outbound_url_no_link() {
        let summary = r#"<p>Some text without link</p>"#;
        let comments_url = "https://www.reddit.com/r/worldnews/comments/abc123/";
        let url = extract_outbound_url(summary, comments_url);
        assert_eq!(url, None);
    }
}