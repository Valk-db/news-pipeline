use crate::models::SourceTier;
use std::collections::HashMap;

/// Categories for organizing sources
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SourceCategory {
    WireService,
    Broadcaster,
    Newspaper,
    DigitalNative,
    Government,
    ThinkTank,
    Social,
    Newsletter,
    Blog,
    Forum,
    Video,
    Academic,
    Other,
}

/// Configuration for a single news source
#[derive(Debug, Clone)]
pub struct SourceConfig {
    pub domain: String,
    pub name: String,
    pub tier: SourceTier,
    pub category: SourceCategory,
    pub rss_urls: Vec<String>,
    pub geographic_focus: Option<String>,
    pub language: String,
    pub reliability_score: f64,
    pub bias_rating: Option<String>,
    pub owner_group: Option<String>,
    pub enabled: bool,
    pub fetch_priority: u32,
    pub max_articles_per_fetch: usize,
    pub custom_headers: Option<HashMap<String, String>>,
    pub notes: String,
}

impl SourceConfig {
    pub fn new(
        domain: &str,
        name: &str,
        tier: SourceTier,
        category: SourceCategory,
        rss_urls: Vec<&str>,
    ) -> Self {
        Self {
            domain: domain.to_string(),
            name: name.to_string(),
            tier,
            category,
            rss_urls: rss_urls.into_iter().map(|s| s.to_string()).collect(),
            geographic_focus: None,
            language: "en".to_string(),
            reliability_score: 0.5,
            bias_rating: None,
            owner_group: None,
            enabled: true,
            fetch_priority: 1,
            max_articles_per_fetch: 50,
            custom_headers: None,
            notes: String::new(),
        }
    }

    pub fn with_geographic_focus(mut self, focus: &str) -> Self {
        self.geographic_focus = Some(focus.to_string());
        self
    }

    pub fn with_reliability(mut self, score: f64) -> Self {
        self.reliability_score = score;
        self
    }

    pub fn with_bias(mut self, bias: &str) -> Self {
        self.bias_rating = Some(bias.to_string());
        self
    }

    pub fn with_owner_group(mut self, owner: &str) -> Self {
        self.owner_group = Some(owner.to_string());
        self
    }

    pub fn with_priority(mut self, priority: u32) -> Self {
        self.fetch_priority = priority;
        self
    }

    pub fn with_disabled(mut self) -> Self {
        self.enabled = false;
        self
    }
}

/// Tier-1 Sources (Verified editorial standards)
pub fn tier1_sources() -> HashMap<String, SourceConfig> {
    let mut sources = HashMap::new();

    sources.insert(
        "bbc.com".to_string(),
        SourceConfig::new(
            "bbc.com",
            "BBC News",
            SourceTier::Tier1,
            SourceCategory::Broadcaster,
            vec![
                "https://feeds.bbci.co.uk/news/world/rss.xml",
                "https://feeds.bbci.co.uk/news/uk/rss.xml",
                "https://feeds.bbci.co.uk/news/politics/rss.xml",
            ],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.95)
        .with_bias("center")
        .with_owner_group("BBC")
        .with_priority(3),
    );

    sources.insert(
        "theguardian.com".to_string(),
        SourceConfig::new(
            "theguardian.com",
            "The Guardian",
            SourceTier::Tier1,
            SourceCategory::Newspaper,
            vec![
                "https://www.theguardian.com/world/rss",
                "https://www.theguardian.com/uk-news/rss",
                "https://www.theguardian.com/politics/rss",
            ],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.92)
        .with_bias("center-left")
        .with_owner_group("Guardian Media Group")
        .with_priority(3),
    );

    sources.insert(
        "npr.org".to_string(),
        SourceConfig::new(
            "npr.org",
            "NPR",
            SourceTier::Tier1,
            SourceCategory::Broadcaster,
            vec![
                "https://feeds.npr.org/1001/rss.xml",
                "https://feeds.npr.org/1003/rss.xml",
                "https://feeds.npr.org/1004/rss.xml",
            ],
        )
        .with_geographic_focus("US")
        .with_reliability(0.93)
        .with_bias("center")
        .with_owner_group("NPR")
        .with_priority(3),
    );

    sources.insert(
        "dw.com".to_string(),
        SourceConfig::new(
            "dw.com",
            "Deutsche Welle",
            SourceTier::Tier1,
            SourceCategory::Broadcaster,
            vec![
                "https://rss.dw.com/rdf/rss-en-all",
                "https://rss.dw.com/rdf/rss-en-europe",
            ],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.90)
        .with_bias("center")
        .with_owner_group("Deutsche Welle")
        .with_priority(2),
    );

    sources.insert(
        "france24.com".to_string(),
        SourceConfig::new(
            "france24.com",
            "France 24",
            SourceTier::Tier1,
            SourceCategory::Broadcaster,
            vec!["https://www.france24.com/en/rss"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.88)
        .with_bias("center")
        .with_owner_group("France Médias Monde")
        .with_priority(2),
    );

    sources.insert(
        "aljazeera.com".to_string(),
        SourceConfig::new(
            "aljazeera.com",
            "Al Jazeera English",
            SourceTier::Tier1,
            SourceCategory::Broadcaster,
            vec!["https://www.aljazeera.com/xml/rss/all.xml"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.85)
        .with_bias("center")
        .with_owner_group("Al Jazeera Media Network")
        .with_priority(2),
    );

    sources.insert(
        "euronews.com".to_string(),
        SourceConfig::new(
            "euronews.com",
            "Euronews",
            SourceTier::Tier1,
            SourceCategory::Broadcaster,
            vec!["https://www.euronews.com/rss?level=theme&name=world"],
        )
        .with_geographic_focus("EU")
        .with_reliability(0.87)
        .with_bias("center")
        .with_owner_group("Euronews")
        .with_priority(2),
    );

    sources.insert(
        "pbs.org".to_string(),
        SourceConfig::new(
            "pbs.org",
            "PBS NewsHour",
            SourceTier::Tier1,
            SourceCategory::Broadcaster,
            vec!["https://www.pbs.org/newshour/feeds/rss.xml"],
        )
        .with_geographic_focus("US")
        .with_reliability(0.92)
        .with_bias("center")
        .with_owner_group("PBS")
        .with_priority(2),
    );

    // AP and Reuters - disabled (no working RSS)
    sources.insert(
        "apnews.com".to_string(),
        SourceConfig::new(
            "apnews.com",
            "Associated Press",
            SourceTier::Tier1,
            SourceCategory::WireService,
            vec![
                "https://apnews.com/hub/world-news",
                "https://apnews.com/hub/politics",
            ],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.98)
        .with_bias("center")
        .with_owner_group("Associated Press")
        .with_priority(3)
        .with_disabled(),
    );

    sources.insert(
        "reuters.com".to_string(),
        SourceConfig::new(
            "reuters.com",
            "Reuters",
            SourceTier::Tier1,
            SourceCategory::WireService,
            vec!["https://www.reuters.com/world/", "https://www.reuters.com/politics/"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.98)
        .with_bias("center")
        .with_owner_group("Reuters")
        .with_priority(3)
        .with_disabled(),
    );

    sources
}

/// Tier-2 Sources (National/Regional reputable outlets)
pub fn tier2_sources() -> HashMap<String, SourceConfig> {
    let mut sources = HashMap::new();

    sources.insert(
        "nytimes.com".to_string(),
        SourceConfig::new(
            "nytimes.com",
            "The New York Times",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec![
                "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
                "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
            ],
        )
        .with_geographic_focus("US")
        .with_reliability(0.90)
        .with_bias("center-left")
        .with_owner_group("New York Times Company")
        .with_priority(2),
    );

    sources.insert(
        "washingtonpost.com".to_string(),
        SourceConfig::new(
            "washingtonpost.com",
            "The Washington Post",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec![
                "https://feeds.washingtonpost.com/rss/world",
                "https://feeds.washingtonpost.com/rss/politics",
            ],
        )
        .with_geographic_focus("US")
        .with_reliability(0.88)
        .with_bias("center-left")
        .with_owner_group("Nash Holdings")
        .with_priority(2),
    );

    sources.insert(
        "wsj.com".to_string(),
        SourceConfig::new(
            "wsj.com",
            "The Wall Street Journal",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec![
                "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
                "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            ],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.89)
        .with_bias("center-right")
        .with_owner_group("News Corp")
        .with_priority(2),
    );

    sources.insert(
        "ft.com".to_string(),
        SourceConfig::new(
            "ft.com",
            "Financial Times",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec![
                "https://www.ft.com/rss/home/uk",
                "https://www.ft.com/rss/home/world",
            ],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.91)
        .with_bias("center")
        .with_owner_group("Nikkei")
        .with_priority(2),
    );

    sources.insert(
        "economist.com".to_string(),
        SourceConfig::new(
            "economist.com",
            "The Economist",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec!["https://www.economist.com/international/rss.xml"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.92)
        .with_bias("center")
        .with_owner_group("Economist Group")
        .with_priority(2),
    );

    sources.insert(
        "foreignpolicy.com".to_string(),
        SourceConfig::new(
            "foreignpolicy.com",
            "Foreign Policy",
            SourceTier::Tier2,
            SourceCategory::ThinkTank,
            vec!["https://foreignpolicy.com/feed/"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.85)
        .with_bias("center")
        .with_owner_group("Graham Holdings")
        .with_priority(1),
    );

    sources.insert(
        "foreignaffairs.com".to_string(),
        SourceConfig::new(
            "foreignaffairs.com",
            "Foreign Affairs",
            SourceTier::Tier2,
            SourceCategory::ThinkTank,
            vec!["https://www.foreignaffairs.com/rss.xml"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.90)
        .with_bias("center")
        .with_owner_group("Council on Foreign Relations")
        .with_priority(1),
    );

    sources.insert(
        "csis.org".to_string(),
        SourceConfig::new(
            "csis.org",
            "CSIS",
            SourceTier::Tier2,
            SourceCategory::ThinkTank,
            vec!["https://www.csis.org/rss.xml"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.88)
        .with_bias("center")
        .with_owner_group("CSIS")
        .with_priority(1),
    );

    sources.insert(
        "who.int".to_string(),
        SourceConfig::new(
            "who.int",
            "World Health Organization",
            SourceTier::Tier2,
            SourceCategory::Government,
            vec!["https://www.who.int/rss-feeds/news-english.xml"],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.94)
        .with_bias("center")
        .with_owner_group("WHO")
        .with_priority(1),
    );

    sources.insert(
        "latimes.com".to_string(),
        SourceConfig::new(
            "latimes.com",
            "Los Angeles Times",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec!["https://www.latimes.com/world-nation/rss2.0.xml"],
        )
        .with_geographic_focus("US")
        .with_reliability(0.85)
        .with_bias("center-left")
        .with_owner_group("Patrick Soon-Shiong")
        .with_priority(1),
    );

    sources.insert(
        "chicagotribune.com".to_string(),
        SourceConfig::new(
            "chicagotribune.com",
            "Chicago Tribune",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec!["https://www.chicagotribune.com/arc/outboundfeeds/rss/category/news/nation-world/"],
        )
        .with_geographic_focus("US")
        .with_reliability(0.84)
        .with_bias("center")
        .with_owner_group("Tribune Publishing")
        .with_priority(1),
    );

    sources.insert(
        "bostonglobe.com".to_string(),
        SourceConfig::new(
            "bostonglobe.com",
            "The Boston Globe",
            SourceTier::Tier2,
            SourceCategory::Newspaper,
            vec!["https://www.bostonglobe.com/arc/outboundfeeds/rss/category/news/nation/"],
        )
        .with_geographic_focus("US")
        .with_reliability(0.86)
        .with_bias("center-left")
        .with_owner_group("Boston Globe Media Partners")
        .with_priority(1),
    );

    sources
}

/// Tier-3 Sources (Social, forums, unverified)
pub fn tier3_sources() -> HashMap<String, SourceConfig> {
    let mut sources = HashMap::new();

    sources.insert(
        "reddit.com".to_string(),
        SourceConfig::new(
            "reddit.com",
            "Reddit",
            SourceTier::Tier3,
            SourceCategory::Forum,
            vec![],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.30)
        .with_bias("mixed")
        .with_owner_group("Reddit Inc")
        .with_priority(1),
    );

    sources.insert(
        "bsky.social".to_string(),
        SourceConfig::new(
            "bsky.social",
            "Bluesky",
            SourceTier::Tier3,
            SourceCategory::Social,
            vec![],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.25)
        .with_bias("mixed")
        .with_owner_group("Bluesky Social")
        .with_priority(1),
    );

    sources
}

/// Tier-4 Sources (Niche, hyperlocal, experimental, newsletters)
pub fn tier4_sources() -> HashMap<String, SourceConfig> {
    let mut sources = HashMap::new();

    sources.insert(
        "substack.com".to_string(),
        SourceConfig::new(
            "substack.com",
            "Substack Newsletters",
            SourceTier::Tier4,
            SourceCategory::Newsletter,
            vec![],
        )
        .with_geographic_focus("Global")
        .with_reliability(0.40)
        .with_bias("mixed")
        .with_owner_group("Substack Inc")
        .with_priority(1),
    );

    sources
}

/// Get all sources combined
pub fn all_sources() -> HashMap<String, SourceConfig> {
    let mut all = HashMap::new();
    all.extend(tier1_sources());
    all.extend(tier2_sources());
    all.extend(tier3_sources());
    all.extend(tier4_sources());
    all
}

/// Get sources by tier
pub fn get_sources_by_tier(tier: SourceTier) -> Vec<SourceConfig> {
    all_sources()
        .into_values()
        .filter(|s| s.tier == tier)
        .collect()
}

/// Get enabled sources by tier
pub fn get_enabled_sources_by_tier(tier: SourceTier) -> Vec<SourceConfig> {
    all_sources()
        .into_values()
        .filter(|s| s.tier == tier && s.enabled)
        .collect()
}

/// Get source configuration by domain
pub fn get_source_config(domain: &str) -> Option<SourceConfig> {
    all_sources().get(domain).cloned()
}

/// Get all RSS feed URLs grouped by domain
pub fn get_all_rss_feeds() -> HashMap<String, Vec<String>> {
    let mut feeds = HashMap::new();
    for (domain, config) in all_sources() {
        if config.enabled && !config.rss_urls.is_empty() {
            feeds.insert(domain, config.rss_urls);
        }
    }
    feeds
}

/// Get tier-1 RSS sources as a vec for ingestion
pub fn get_tier1_rss_sources() -> Vec<crate::ingestion::rss::SourceConfig> {
    get_enabled_sources_by_tier(SourceTier::Tier1)
        .into_iter()
        .map(|s| crate::ingestion::rss::SourceConfig {
            domain: s.domain,
            name: s.name,
            tier: s.tier,
            rss_urls: s.rss_urls,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tier1_sources() {
        let sources = tier1_sources();
        assert!(!sources.is_empty());
        assert!(sources.contains_key("bbc.com"));
        assert!(sources.contains_key("theguardian.com"));
    }

    #[test]
    fn test_get_enabled_sources_by_tier() {
        let enabled = get_enabled_sources_by_tier(SourceTier::Tier1);
        assert!(!enabled.is_empty());
        for source in &enabled {
            assert!(source.enabled);
        }
    }

    #[test]
    fn test_get_tier1_rss_sources() {
        let sources = get_tier1_rss_sources();
        assert!(!sources.is_empty());
        for source in &sources {
            assert!(!source.rss_urls.is_empty());
        }
    }
}