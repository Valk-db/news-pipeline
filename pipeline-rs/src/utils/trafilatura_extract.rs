use once_cell::sync::Lazy;
use reqwest::Client;
use std::collections::HashSet;
use url::{Url, form_urlencoded};

static HTTP_CLIENT: Lazy<Client> = Lazy::new(|| {
    Client::builder()
        .user_agent("Mozilla/5.0 (compatible; news-pipeline/0.1; +https://github.com/Valk-db/news-pipeline)")
        .timeout(std::time::Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::default())
        .pool_max_idle_per_host(20)
        .pool_idle_timeout(std::time::Duration::from_secs(30))
        .build()
        .expect("Failed to create HTTP client")
});

/// Tracking parameters to strip from URLs
const TRACKING_PARAMS: &[&str] = &["fbclid", "gclid", "ocid", "cmpid", "ref", "taid", "mc_cid", "mc_eid"];
const TRACKING_PREFIXES: &[&str] = &["utm_", "at_"];

/// Normalize URL for consistent hashing:
/// - lowercase scheme and host
/// - strip www.
/// - remove tracking query parameters
/// - remove fragment
/// - normalize path (strip trailing slash)
/// - path case preserved; compute_url_hash lowercases for case-insensitive matching
pub fn canonicalize_url(url: &str) -> String {
    let Ok(mut url) = Url::parse(url.trim()) else {
        return url.to_string();
    };

    url.set_scheme("https").ok();
    if let Some(host) = url.host_str() {
        let host = host.to_lowercase();
        let host = host.strip_prefix("www.").unwrap_or(&host);
        let _ = url.set_host(Some(host));
    }
    url.set_fragment(None);

    // Remove tracking parameters
    let pairs: Vec<(String, String)> = url
        .query_pairs()
        .filter(|(k, _)| {
            let k_lower = k.to_lowercase();
            !TRACKING_PARAMS.contains(&k_lower.as_str())
                && !TRACKING_PREFIXES.iter().any(|p| k_lower.starts_with(p))
        })
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();

    if pairs.is_empty() {
        url.set_query(None);
    } else {
        url.query_pairs_mut().clear().extend_pairs(pairs);
    }

    // Normalize path - strip trailing slash
    let path: String = url.path().trim_end_matches('/').to_string();
    if path.is_empty() {
        url.set_path("/");
    } else {
        url.set_path(&path);
    }

    url.to_string()
}

/// SHA256 hash of normalized text for exact dedup
pub fn compute_content_hash(text: &str) -> String {
    use sha2::{Digest, Sha256};
    let normalized: String = text.to_lowercase().split_whitespace().collect::<Vec<_>>().join(" ");
    let mut hasher = Sha256::new();
    hasher.update(normalized.as_bytes());
    hex::encode(hasher.finalize())
}

/// SHA256 hash of canonicalized URL for dedup
pub fn compute_url_hash(url: &str) -> String {
    use sha2::{Digest, Sha256};
    let canonical = canonicalize_url(url).to_lowercase();
    let mut hasher = Sha256::new();
    hasher.update(canonical.as_bytes());
    hex::encode(hasher.finalize())
}

/// Extract article body text and title from URL (async)
/// Returns (body_text, title) or (None, None) on failure
pub async fn extract_article(url: &str, _source_key: Option<&str>) -> (Option<String>, Option<String>) {
    // Fetch HTML
    let response = match HTTP_CLIENT.get(url).send().await {
        Ok(resp) => resp,
        Err(e) => {
            tracing::warn!("Failed to fetch {}: {}", url, e);
            return (None, None);
        }
    };

    if !response.status().is_success() {
        tracing::warn!("HTTP {} fetching {}", response.status(), url);
        return (None, None);
    }

    let html = match response.text().await {
        Ok(text) => text,
        Err(e) => {
            tracing::warn!("Failed to read response body for {}: {}", url, e);
            return (None, None);
        }
    };

    // Extract using trafilatura via CLI or native Rust crate
    // For now, use a simple approach - we'll need to add trafilatura crate
    extract_from_html(&html)
}

/// Synchronous extraction from HTML - runs in thread pool
fn extract_from_html(html: &str) -> (Option<String>, Option<String>) {
    // Use trafilatura via command-line or native implementation
    // For now, a simple placeholder - we'll add proper extraction when trafilatura crate is available
    // This is a minimal implementation that extracts text between <p> tags as a fallback

    // Try to use a simple HTML parser
    use regex::Regex;

    let title_re = Regex::new(r"(?s)<title[^>]*>(.*?)</title>").ok();
    let title = title_re
        .and_then(|re| re.captures(html))
        .and_then(|cap| cap.get(1))
        .map(|m| m.as_str().trim().to_string());

    // Extract text content - basic approach
    let body = html
        .replace("<p>", "\n")
        .replace("</p>", "")
        .replace("<br>", "\n")
        .replace("<br/>", "\n")
        .replace("<br />", "\n");

    // Strip remaining HTML tags
    let tag_re = Regex::new(r"<[^>]*>").ok();
    let body = tag_re.map_or(body.clone(), |re| re.replace_all(&body, " ").to_string());

    // Clean up whitespace
    let body = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let body = if body.len() > 200 { Some(body) } else { None };

    (body, title)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_canonicalize_url() {
        let url = "https://www.example.com/path/?utm_source=test&fbclid=123#fragment";
        let canonical = canonicalize_url(url);
        // Function strips trailing slash from path, so expect "https://example.com/path"
        assert_eq!(canonical, "https://example.com/path");
        assert!(!canonical.contains("utm_source"));
        assert!(!canonical.contains("fbclid"));
        assert!(!canonical.contains("#fragment"));
    }

    #[test]
    fn test_canonicalize_url_www_strip() {
        let url = "https://www.bbc.com/news/world";
        let canonical = canonicalize_url(url);
        assert_eq!(canonical, "https://bbc.com/news/world");
    }

    #[test]
    fn test_compute_url_hash() {
        let url1 = "https://www.example.com/path?utm_source=test";
        let url2 = "https://example.com/path";
        assert_eq!(compute_url_hash(url1), compute_url_hash(url2));
    }

    #[test]
    fn test_compute_content_hash() {
        let text1 = "Hello   world";
        let text2 = "hello world";
        assert_eq!(compute_content_hash(text1), compute_content_hash(text2));
    }
}