"""Central registry of all news sources with metadata."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
from src.schema.models import SourceTier


class SourceCategory(str, Enum):
    """Categories for organizing sources."""
    WIRE_SERVICE = "wire_service"
    BROADCASTER = "broadcaster"
    NEWSPAPER = "newspaper"
    DIGITAL_NATIVE = "digital_native"
    GOVERNMENT = "government"
    THINK_TANK = "think_tank"
    SOCIAL = "social"
    NEWSLETTER = "newsletter"
    BLOG = "blog"
    FORUM = "forum"
    VIDEO = "video"
    ACADEMIC = "academic"
    OTHER = "other"


@dataclass
class SourceConfig:
    """Configuration for a single news source."""
    domain: str
    name: str
    tier: SourceTier
    category: SourceCategory
    rss_urls: list[str]
    geographic_focus: Optional[str] = None  # e.g., "US", "UK", "EU", "Global"
    language: str = "en"
    reliability_score: float = 0.5  # 0-1, will be updated by reliability system
    bias_rating: Optional[str] = None  # e.g., "center", "left", "right", "mixed"
    owner_group: Optional[str] = None  # e.g., "BBC", "Guardian Media Group"
    enabled: bool = True
    fetch_priority: int = 1  # Higher = more frequent
    max_articles_per_fetch: int = 50
    custom_headers: Optional[dict] = None
    notes: str = ""


# Tier-1 Sources (Verified editorial standards)
TIER1_SOURCES = {
    "bbc.com": SourceConfig(
        domain="bbc.com",
        name="BBC News",
        tier=SourceTier.TIER1,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.bbci.co.uk/news/uk/rss.xml",
            "https://feeds.bbci.co.uk/news/politics/rss.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.95,
        bias_rating="center",
        owner_group="BBC",
        fetch_priority=3,
    ),
    "theguardian.com": SourceConfig(
        domain="theguardian.com",
        name="The Guardian",
        tier=SourceTier.TIER1,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.theguardian.com/world/rss",
            "https://www.theguardian.com/uk-news/rss",
            "https://www.theguardian.com/politics/rss",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.92,
        bias_rating="center-left",
        owner_group="Guardian Media Group",
        fetch_priority=3,
    ),
    "npr.org": SourceConfig(
        domain="npr.org",
        name="NPR",
        tier=SourceTier.TIER1,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://feeds.npr.org/1001/rss.xml",
            "https://feeds.npr.org/1003/rss.xml",
            "https://feeds.npr.org/1004/rss.xml",
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.93,
        bias_rating="center",
        owner_group="NPR",
        fetch_priority=3,
    ),
    "dw.com": SourceConfig(
        domain="dw.com",
        name="Deutsche Welle",
        tier=SourceTier.TIER1,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://rss.dw.com/rdf/rss-en-all",
            "https://rss.dw.com/rdf/rss-en-europe",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.90,
        bias_rating="center",
        owner_group="Deutsche Welle",
        fetch_priority=2,
    ),
    "france24.com": SourceConfig(
        domain="france24.com",
        name="France 24",
        tier=SourceTier.TIER1,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://www.france24.com/en/rss",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.88,
        bias_rating="center",
        owner_group="France Médias Monde",
        fetch_priority=2,
    ),
    "aljazeera.com": SourceConfig(
        domain="aljazeera.com",
        name="Al Jazeera English",
        tier=SourceTier.TIER1,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.85,
        bias_rating="center",
        owner_group="Al Jazeera Media Network",
        fetch_priority=2,
    ),
    "euronews.com": SourceConfig(
        domain="euronews.com",
        name="Euronews",
        tier=SourceTier.TIER1,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://www.euronews.com/rss?level=theme&name=world",
        ],
        geographic_focus="EU",
        language="en",
        reliability_score=0.87,
        bias_rating="center",
        owner_group="Euronews",
        fetch_priority=2,
    ),
    "pbs.org": SourceConfig(
        domain="pbs.org",
        name="PBS NewsHour",
        tier=SourceTier.TIER1,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://www.pbs.org/newshour/feeds/rss.xml",
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.92,
        bias_rating="center",
        owner_group="PBS",
        fetch_priority=2,
    ),
    "apnews.com": SourceConfig(
        domain="apnews.com",
        name="Associated Press",
        tier=SourceTier.TIER1,
        category=SourceCategory.WIRE_SERVICE,
        rss_urls=[
            "https://apnews.com/hub/world-news",
            "https://apnews.com/hub/politics",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.98,
        bias_rating="center",
        owner_group="Associated Press",
        fetch_priority=3,
        enabled=False,  # Currently no working RSS
    ),
    "reuters.com": SourceConfig(
        domain="reuters.com",
        name="Reuters",
        tier=SourceTier.TIER1,
        category=SourceCategory.WIRE_SERVICE,
        rss_urls=[
            "https://www.reuters.com/world/",
            "https://www.reuters.com/politics/",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.98,
        bias_rating="center",
        owner_group="Reuters",
        fetch_priority=3,
        enabled=False,  # Currently no working RSS
    ),
}

# Tier-2 Sources (National/Regional reputable outlets)
TIER2_SOURCES = {
    "nytimes.com": SourceConfig(
        domain="nytimes.com",
        name="The New York Times",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.90,
        bias_rating="center-left",
        owner_group="New York Times Company",
        fetch_priority=2,
    ),
    "washingtonpost.com": SourceConfig(
        domain="washingtonpost.com",
        name="The Washington Post",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://feeds.washingtonpost.com/rss/world",
            "https://feeds.washingtonpost.com/rss/politics",
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.88,
        bias_rating="center-left",
        owner_group="Nash Holdings",
        fetch_priority=2,
    ),
    "wsj.com": SourceConfig(
        domain="wsj.com",
        name="The Wall Street Journal",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.89,
        bias_rating="center-right",
        owner_group="News Corp",
        fetch_priority=2,
    ),
    "ft.com": SourceConfig(
        domain="ft.com",
        name="Financial Times",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.ft.com/rss/home/uk",
            "https://www.ft.com/rss/home/world",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.91,
        bias_rating="center",
        owner_group="Nikkei",
        fetch_priority=2,
    ),
    "economist.com": SourceConfig(
        domain="economist.com",
        name="The Economist",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.economist.com/international/rss.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.92,
        bias_rating="center",
        owner_group="Economist Group",
        fetch_priority=2,
    ),
    "foreignpolicy.com": SourceConfig(
        domain="foreignpolicy.com",
        name="Foreign Policy",
        tier=SourceTier.TIER2,
        category=SourceCategory.THINK_TANK,
        rss_urls=[
            "https://foreignpolicy.com/feed/",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.85,
        bias_rating="center",
        owner_group="Graham Holdings",
        fetch_priority=1,
    ),
    "foreignaffairs.com": SourceConfig(
        domain="foreignaffairs.com",
        name="Foreign Affairs",
        tier=SourceTier.TIER2,
        category=SourceCategory.THINK_TANK,
        rss_urls=[
            "https://www.foreignaffairs.com/rss.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.90,
        bias_rating="center",
        owner_group="Council on Foreign Relations",
        fetch_priority=1,
    ),
    "csis.org": SourceConfig(
        domain="csis.org",
        name="CSIS",
        tier=SourceTier.TIER2,
        category=SourceCategory.THINK_TANK,
        rss_urls=[
            "https://www.csis.org/rss.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.88,
        bias_rating="center",
        owner_group="CSIS",
        fetch_priority=1,
    ),
    # "brookings.edu": SourceConfig(
#         domain="brookings.edu",
#         name="Brookings Institution",
#         tier=SourceTier.TIER2,
#         category=SourceCategory.THINK_TANK,
#         rss_urls=[
#             "https://www.brookings.edu/feed/",  # Returns HTML, not RSS (verified 2026-09-24)
#         ],
#         geographic_focus="US",
#         language="en",
#         reliability_score=0.89,
#         bias_rating="center-left",
#         owner_group="Brookings",
#         fetch_priority=1,
#         enabled=False,  # Disabled: no working RSS feed
#     ),
    # "chathamhouse.org": SourceConfig(
#         domain="chathamhouse.org",
#         name="Chatham House",
#         tier=SourceTier.TIER2,
#         category=SourceCategory.THINK_TANK,
#         rss_urls=[
#             "https://www.chathamhouse.org/rss.xml",  # Cloudflare 403 (verified 2026-09-24)
#         ],
#         geographic_focus="Global",
#         language="en",
#         reliability_score=0.88,
#         bias_rating="center",
#         owner_group="Chatham House",
#         fetch_priority=1,
#         enabled=False,  # Disabled: Cloudflare blocks RSS
#     ),
    # "un.org": SourceConfig(
#         domain="un.org",
#         name="United Nations News",
#         tier=SourceTier.TIER2,
#         category=SourceCategory.GOVERNMENT,
#         rss_urls=[
#             "https://news.un.org/feed/subscribe/en/news/all/rss.xml",  # CloudFront 403 (verified 2026-09-24)
#         ],
#         geographic_focus="Global",
#         language="en",
#         reliability_score=0.92,
#         bias_rating="center",
#         owner_group="United Nations",
#         fetch_priority=1,
#         enabled=False,  # Disabled: CloudFront blocks RSS
#     ),
    "who.int": SourceConfig(
        domain="who.int",
        name="World Health Organization",
        tier=SourceTier.TIER2,
        category=SourceCategory.GOVERNMENT,
        rss_urls=[
            "https://www.who.int/rss-feeds/news-english.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.94,
        bias_rating="center",
        owner_group="WHO",
        fetch_priority=1,
    ),
    # Major US regional papers
    "latimes.com": SourceConfig(
        domain="latimes.com",
        name="Los Angeles Times",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.latimes.com/world-nation/rss2.0.xml",
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.85,
        bias_rating="center-left",
        owner_group="Patrick Soon-Shiong",
        fetch_priority=1,
    ),
    "chicagotribune.com": SourceConfig(
        domain="chicagotribune.com",
        name="Chicago Tribune",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.chicagotribune.com/arc/outboundfeeds/rss/category/news/nation-world/",
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.84,
        bias_rating="center",
        owner_group="Tribune Publishing",
        fetch_priority=1,
    ),
    "bostonglobe.com": SourceConfig(
        domain="bostonglobe.com",
        name="The Boston Globe",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.bostonglobe.com/arc/outboundfeeds/rss/category/news/nation/",
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.86,
        bias_rating="center-left",
        owner_group="Boston Globe Media Partners",
        fetch_priority=1,
    ),
}

# Tier-3 Sources (Social, forums, unverified)
TIER3_SOURCES = {
    "reddit.com": SourceConfig(
        domain="reddit.com",
        name="Reddit",
        tier=SourceTier.TIER3,
        category=SourceCategory.FORUM,
        rss_urls=[],  # Handled by dedicated reddit.py ingestion
        geographic_focus="Global",
        language="en",
        reliability_score=0.30,
        bias_rating="mixed",
        owner_group="Reddit Inc",
        fetch_priority=1,
    ),
    "bsky.social": SourceConfig(
        domain="bsky.social",
        name="Bluesky",
        tier=SourceTier.TIER3,
        category=SourceCategory.SOCIAL,
        rss_urls=[],
        geographic_focus="Global",
        language="en",
        reliability_score=0.25,
        bias_rating="mixed",
        owner_group="Bluesky Social",
        fetch_priority=1,
    ),
}

# Tier-4 Sources (Niche, hyperlocal, experimental, newsletters)
TIER4_SOURCES = {
    "substack.com": SourceConfig(
        domain="substack.com",
        name="Substack Newsletters",
        tier=SourceTier.TIER4,
        category=SourceCategory.NEWSLETTER,
        rss_urls=[],  # Individual newsletter RSS feeds
        geographic_focus="Global",
        language="en",
        reliability_score=0.40,
        bias_rating="mixed",
        owner_group="Substack Inc",
        fetch_priority=1,
    ),
}

# Combined registry
ALL_SOURCES = {}
ALL_SOURCES.update(TIER1_SOURCES)
ALL_SOURCES.update(TIER2_SOURCES)
ALL_SOURCES.update(TIER3_SOURCES)
ALL_SOURCES.update(TIER4_SOURCES)


def get_sources_by_tier(tier: SourceTier) -> dict[str, SourceConfig]:
    """Get all sources for a specific tier."""
    return {k: v for k, v in ALL_SOURCES.items() if v.tier == tier}


def get_enabled_sources_by_tier(tier: SourceTier) -> dict[str, SourceConfig]:
    """Get enabled sources for a specific tier."""
    return {k: v for k, v in ALL_SOURCES.items() if v.tier == tier and v.enabled}


def get_source_config(domain: str) -> Optional[SourceConfig]:
    """Get source configuration by domain."""
    return ALL_SOURCES.get(domain)


def get_all_rss_feeds() -> dict[str, list[str]]:
    """Get all RSS feed URLs grouped by domain."""
    feeds = {}
    for domain, config in ALL_SOURCES.items():
        if config.enabled and config.rss_urls:
            feeds[domain] = config.rss_urls
    return feeds