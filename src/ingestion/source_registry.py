"""Central registry of all news sources with metadata."""

from dataclasses import dataclass
from enum import Enum
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
    geographic_focus: str | None = None  # e.g., "US", "UK", "EU", "Global"
    language: str = "en"
    reliability_score: float = 0.5  # 0-1, will be updated by reliability system
    bias_rating: str | None = None  # e.g., "center", "left", "right", "mixed"
    owner_group: str | None = None  # e.g., "BBC", "Guardian Media Group"
    enabled: bool = True
    fetch_priority: int = 1  # Higher = more frequent
    max_articles_per_fetch: int = 50
    custom_headers: dict | None = None
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
            "https://feeds.npr.org/1001/rss.xml",  # News
            "https://feeds.npr.org/1003/rss.xml",  # National
            "https://feeds.npr.org/1004/rss.xml",  # World
            "https://feeds.npr.org/1014/rss.xml",  # Politics (verified 2026-09-28)
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
            # Was rss-en-europe, which is dead in the exact shape this batch
            # exists to catch: HTTP 200, 28-byte body, "Error: no feed by that
            # name." Verified 2026-10-02 by probing 9 DW variants: -europe, -eco,
            # -sci, -de, -allsects, -europa and -pol all return that identical
            # error body, and rss.dw.com/xml/rss-en-all is a byte-identical feed
            # to rss-en-all above, so this entry contributed one error and one
            # duplicate. rss-en-world is the same desk's live feed: HTTP 200, RDF
            # 1.0, 11 items, 11 dated, 11 within 48h, newest 2026-10-02T16:39Z.
            "https://rss.dw.com/rdf/rss-en-world",
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
            "https://www.euronews.com/rss",  # 2026-09-28: corrected from /rss?level=theme&name=world (404)
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
            "https://www.pbs.org/newshour/feeds/rss/headlines",  # 2026-09-28: corrected from /rss.xml (404)
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.92,
        bias_rating="center",
        owner_group="PBS",
        fetch_priority=2,
    ),
    # AP: the two URLs below are the ones this registry has always used, and a
    # live fetch (2026-10-01) returns HTTP 200 text/html. They are HTML hub
    # pages, not feeds, so feedparser finds no entries. Candidate real feed
    # paths all failed too: /apf-topnews?output=1 and /hub/ap-top-news.rss close
    # the connection, /index.rss is 403. No working AP feed from this network.
    # Adapter code is left intact; enabling this would just add two empty feeds.
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
        enabled=False,  # Verified 2026-10-01: HTML hub pages, no RSS, no working feed URL
    ),
    # Reuters: live fetch of both URLs below (2026-10-01) returns HTTP 401, so
    # they are bot blocked from this network. The Reuters arc news sitemap
    # index does answer 200 with application/xml, but it is a sitemap, not a
    # feed; the matching rss category path 404s. No working Reuters feed here.
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
        enabled=False,  # Verified 2026-10-01: HTTP 401 on both URLs, bot blocked
    ),
}

# Hazard sensor feeds. These are not publishers, so they are tier 3 machine
# sources with no RSS and no ownership. Both feed URLs were verified live
# (2026-10-01) and are served by src/ingestion/sensors.py, not by the RSS
# adapter, so rss_urls stays empty to keep them out of the RSS sweep.
SENSOR_SOURCES = {
    "earthquake.usgs.gov": SourceConfig(
        domain="earthquake.usgs.gov",
        name="USGS Earthquakes",
        tier=SourceTier.TIER3,
        category=SourceCategory.GOVERNMENT,
        rss_urls=[],
        geographic_focus="Global",
        language="en",
        reliability_score=0.99,
        bias_rating="center",
        owner_group="US Geological Survey",
        fetch_priority=1,
        notes="GeoJSON all_day feed, ~290 features/day, handled by sensors.usgs_earthquakes",
    ),
    "gdacs.org": SourceConfig(
        domain="gdacs.org",
        name="GDACS",
        tier=SourceTier.TIER3,
        category=SourceCategory.GOVERNMENT,
        rss_urls=[],
        geographic_focus="Global",
        language="en",
        reliability_score=0.90,
        bias_rating="center",
        owner_group="Global Disaster Alert and Coordination System",
        fetch_priority=1,
        notes="xml/rss.xml feed, ~223 items, handled by sensors.gdacs_alerts",
    ),
}

# Tier-2 Sources (National/Regional reputable outlets)
TIER2_SOURCES = {
    # FAILING (paywall/403): 0 ok across healthy runs 36434221800, 36452503330, 36514993348
    # 36434221800: feed_ok=2, entries_in_feed=69, fetch_failed:http_403=9, circuit_open=61
    # 36452503330: feed_ok=2, entries_in_feed=70, fetch_failed:http_403=9, circuit_open=60
    # 36514993348: disabled (not attempted)
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
        enabled=False,
    ),
    # FAILING (paywall/timeout): 0 ok across healthy runs 36434221800, 36452503330, 36514993348
    # 36434221800: feed_ok=2, entries_in_feed=15, fetch_failed:timeout=9, circuit_open=8
    # 36452503330: feed_ok=2, entries_in_feed=18, fetch_failed:timeout=9, circuit_open=9
    # 36514993348: disabled (not attempted)
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
        enabled=False,
    ),
    # FAILING (paywall/401): 0 ok across healthy runs 36434221800, 36452503330, 36514993348
    # 36434221800: feed_ok=2, entries_in_feed=40, fetch_failed:http_401=9, circuit_open=33
    # 36452503330: feed_ok=2, entries_in_feed=40, fetch_failed:http_401=9, circuit_open=31
    # 36514993348: disabled (not attempted)
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
        enabled=False,
    ),
    # FAILING (paywall/403): 0 ok across healthy runs 36434221800, 36452503330, 36514993348
    # 36434221800: feed_ok=1, feed_failed:http_403=1, entries_in_feed=10, fetch_failed:http_403=8, circuit_open=3
    # 36452503330: feed_ok=1, feed_failed:http_403=1, entries_in_feed=10, fetch_failed:http_403=8, circuit_open=2
    # 36514993348: disabled (not attempted)
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
        enabled=False,
    ),
    # FAILING (paywall/403): 0 ok across healthy runs 36434221800, 36452503330, 36514993348
    # 36434221800: feed_ok=1, entries_in_feed=50, fetch_failed:http_403=8, circuit_open=43
    # 36452503330: feed_ok=1, entries_in_feed=50, fetch_failed:http_403=8, circuit_open=42
    # 36514993348: disabled (not attempted)
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
        enabled=False,
    ),
    # WORKING: ok>0 in all 3 healthy runs
    # 36434221800: feed_ok=1, entries_in_feed=25, entries_seen=1, ok=1, already_known=24
    # 36452503330: feed_ok=1, entries_in_feed=25, entries_seen=1, ok=1, already_known=24
    # 36514993348: feed_ok=1, entries_in_feed=25, already_known=25
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
    # WORKING: ok>0 in all 3 healthy runs
    # 36434221800: feed_ok=1, entries_in_feed=20, entries_seen=5, already_known=15, ok=5
    # 36452503330: feed_ok=1, entries_in_feed=20, entries_seen=5, already_known=15, ok=5
    # 36514993348: feed_ok=1, entries_in_feed=20, entries_seen=4, already_known=16, ok=4
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
    # WORKING-BUT-DEDUPED: entries_in_feed>0, already_known>0, ok=0 in all 3 healthy runs (dedup, not failure)
    # 36434221800: entries_in_feed=10, already_known=10
    # 36452503330: entries_in_feed=10, already_known=10
    # 36514993348: feed_ok=1 (entries_in_feed not shown, likely 10)
    # Keep enabled; if newest entry >30 days old, disable as stale. Current feed active.
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
    # UN News: the 2026-09-24 "CloudFront 403" note is obsolete. Re-verified
    # 2026-10-02: HTTP 200, 30 dated items, 22 of them inside 48h, newest
    # 2026-10-02T12:00Z. The reason this read as dead before was the body
    # arrived content-encoding: gzip and the fetcher did not decompress it, so
    # parse_feed_xml choked on the magic bytes and reported 0 items (the same
    # 200-that-is-not-a-feed trap). rss_evidence.fetch_feed_polite now gunzips
    # on the magic bytes, so this feed is live.
    "un.org": SourceConfig(
        domain="un.org",
        name="United Nations News",
        tier=SourceTier.TIER2,
        category=SourceCategory.GOVERNMENT,
        rss_urls=[
            "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
            # French edition, added 2026-10-02. Part C (NER on the translated
            # body) is proven, so a non-English feed is no longer inert: these
            # rows are translated in Phase 1.5 and their entities are extracted
            # in Phase 1.6, which is the whole point of that fix. 30 items,
            # 8 within 48h at the time of adding. Free, no key, no account.
            "https://news.un.org/feed/subscribe/fr/news/all/rss.xml",
        ],
        geographic_focus="Global",
        language="en,fr",
        reliability_score=0.92,
        bias_rating="center",
        owner_group="United Nations",
        fetch_priority=1,
        notes=("UN News English + French feeds. English re-enabled 2026-10-02 "
               "(see comment above). French added 2026-10-02: 30 items, 8 within "
               "48h; the pipeline now translates then extracts entities, so these "
               "rows are not inert. Deliberately only ONE non-English feed: the "
               "free translation budget is 45,000 chars/day, about 10 article "
               "bodies, so a second one would compete with the first for it."),
    ),
    # WORKING-BUT-DEDUPED: entries_in_feed>0, already_known>0, ok=0 in all 3 healthy runs (dedup, not failure)
    # 36434221800: entries_in_feed=25, already_known=25
    # 36452503330: entries_in_feed=25, already_known=25
    # 36514993348: feed_ok=1 (entries_in_feed not shown, likely 25)
    # Keep enabled; if newest entry >30 days old, disable as stale. Current feed active.
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
    # FAILING (403): 0 ok across healthy runs 36434221800, 36452503330, 36514993348
    # 36434221800: feed_failed:http_403=1
    # 36452503330: feed_failed:http_403=1
    # 36514993348: disabled (not attempted)
    "latimes.com": SourceConfig(
        domain="latimes.com",
        name="Los Angeles Times",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.latimes.com/rss2.0.xml",  # 2026-09-28: corrected from /world-nation/rss2.0.xml (404)
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.85,
        bias_rating="center-left",
        owner_group="Patrick Soon-Shiong",
        fetch_priority=1,
        enabled=False,
    ),
    # EMPTY FEED: entries_in_feed=0 in all 3 healthy runs
    # 36434221800: entries_in_feed=0 (feed_ok=1 but no entries)
    # 36452503330: entries_in_feed=0 (feed_ok=1 but no entries)
    # 36514993348: feed_ok=1 (entries_in_feed not shown, likely 0)
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
        enabled=False,
    ),
    "bostonglobe.com": SourceConfig(
        domain="bostonglobe.com",
        name="The Boston Globe",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.bostonglobe.com/rss/",  # 2026-09-28: corrected from /arc/outboundfeeds/... (404); desktop 200
        ],
        geographic_focus="US",
        language="en",
        reliability_score=0.86,
        bias_rating="center-left",
        owner_group="Boston Globe Media Partners",
        fetch_priority=1,
        enabled=False,  # 2026-09-28: desktop 200 but runner may differ; disable until verified in CI
    ),
    # ------------------------------------------------------------------
    # Regional blind-spot sources added 2026-10-02 (batch-coverage).
    #
    # Every feed below was re-verified live on 2026-10-02 through the repo's
    # own polite fetcher (rss_evidence.fetch_feed_polite) and its own parser
    # (rss_evidence.parse_feed_xml), not through curl or a hand-rolled reader.
    # Each entry comment carries the measured item count, the measured share of
    # items inside 48h, and the first real headline returned at verification
    # time. All nine are English-language, free, no key, no account, no
    # data-sharing opt-in.
    #
    # Tier: TIER2. In this repo's own terms (tiers.py:143-147) tier 2 is
    # "national/regional reputable outlets", and TIER2_DOMAINS is documented as
    # "keep ONLY the sources that are actually enabled in source_registry.py and
    # have verified working RSS feeds". Each of these is a national or regional
    # publisher with an established editorial operation and a working feed
    # verified today, which is exactly the tier-2 definition; none of them are
    # in TIER1_DOMAINS (verified editorial standards), and none of them are
    # social/newsletter platforms, so none are tier 3 or 4. New domains are
    # mirrored into TIER2_DOMAINS in the same commit so the two sets stay in
    # sync.
    # ------------------------------------------------------------------

    # Africa wire. Aggregates African publishers (The Africa Report, Africanews,
    # Africa Intelligence, national dailies) into one English feed. This is the
    # single highest-value door for the African blind spots in the GDELT census
    # (Nigeria, Kenya, Ghana were all at zero pipeline articles).
    # Verified 2026-10-02: HTTP 200, RDF 1.0, 31 items, 31 dated, 30 within
    # 48h, newest 2026-10-02T17:50Z.
    # First headline: "Nigeria: Boko Haram Fighters Gun Down Farmers in Borno".
    "allafrica.com": SourceConfig(
        domain="allafrica.com",
        name="allAfrica",
        tier=SourceTier.TIER2,
        category=SourceCategory.WIRE_SERVICE,
        rss_urls=[
            "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf",
        ],
        geographic_focus="Africa",
        language="en",
        reliability_score=0.72,
        bias_rating="mixed",
        owner_group="allAfrica Media Group",
        fetch_priority=2,
        notes="Pan-African aggregation wire. 31 items, 30 within 48h on 2026-10-02. Aggregator: item quality varies by upstream publisher.",
    ),

    # China / Hong Kong. SCMP is the largest English-language China-desk
    # operation available without a subscription; its China feed is where the
    # US-China trade and Taiwan Strait reporting the GDELT census found at zero
    # pipeline articles originates.
    # Verified 2026-10-02: HTTP 200, 50 items, 50 dated, 50 within 48h,
    # newest 2026-10-02T22:00Z.
    # First headline: "'No retreat': Key Trump ally urges continued US-China
    # engagement on rare earths". Feed items carry a utm_source=rss_feed query
    # parameter, which the url_hash treats as part of the URL, so dedup by
    # url_hash still works but a re-published item can appear once per variant.
    "scmp.com": SourceConfig(
        domain="scmp.com",
        name="South China Morning Post",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.scmp.com/rss/91/feed",  # China
        ],
        geographic_focus="China",
        language="en",
        reliability_score=0.78,
        bias_rating="center",
        owner_group="Alibaba Media Group",
        fetch_priority=2,
        # CUT 2026-10-02 with evidence. The feed is fine: HTTP 200, 50 dated
        # items, 50 within 48h on 2026-10-02. Every ARTICLE page fetch is
        # refused, so the pipeline produced zero articles from it: one real
        # dev run (tier-2 sweep) recorded entries_in_feed=50,
        # entries_seen=8, fetch_failed:http_403=8, circuit_open=42, ok=0, and a
        # direct single-URL fetch through the repo's own extract_article()
        # returned 403 with our honest User-Agent too. SCMP serves bodies only
        # to a full browser fingerprint. A headline-only feed is not evidence
        # (see rss_evidence MIN_BODY_CHARS), so this stays disabled until that
        # changes. Re-verify before ever re-enabling.
        enabled=False,
        notes="Disabled 2026-10-02: feed alive (50 items) but every article page returns 403, so 0 articles. Widely read; editorial line is state-adjacent.",
    ),

    # Ireland. RTÉ is the Irish national public broadcaster; Ireland was a
    # zero-coverage country in the GDELT census.
    # Verified 2026-10-02: HTTP 200, 20 items, 20 dated, 20 within 48h,
    # newest 2026-10-02T21:46Z.
    # First headline: "Two killed in workplace incident in Co Down".
    # Mixed desk (Ireland domestic plus international), so expect a majority of
    # items with no external-relations content.
    "rte.ie": SourceConfig(
        domain="rte.ie",
        name="RTÉ News",
        tier=SourceTier.TIER2,
        category=SourceCategory.BROADCASTER,
        rss_urls=[
            "https://www.rte.ie/news/rss/news-headlines.xml",
        ],
        geographic_focus="Ireland",
        language="en",
        reliability_score=0.88,
        bias_rating="center",
        owner_group="RTÉ (Ireland)",
        fetch_priority=2,
        notes="Irish national broadcaster headlines. 20 items, all within 48h on 2026-10-02. Headline feed only; bodies come from the article fetch.",
    ),

    # Philippines / South China Sea. The Philippine Daily Inquirer feed
    # 403s from this network (verified 2026-10-02), and philstar.com/rss and
    # philstar.com/rss/national return 404 or 200-with-0-items; /rss/world is
    # the only working Philippines feed found.
    # Verified 2026-10-02: HTTP 200, 10 items, 10 dated, 0 within 48h,
    # newest 2026-09-28T09:53Z. Low cadence and it carries dated items, so this
    # is measured as a slow feed, not a dead one: it returned 10 real items
    # with real links. Watch it against the dead-feed streak counter added in
    # this same batch.
    "philstar.com": SourceConfig(
        domain="philstar.com",
        name="The Philippine Star",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.philstar.com/rss/world",
        ],
        geographic_focus="Philippines",
        language="en",
        reliability_score=0.75,
        bias_rating="center-right",
        owner_group="The Philippine Star",
        fetch_priority=1,
        notes="Only working free Philippines feed from this network. 10 items, newest 2026-09-28 on 2026-10-02, so low cadence; wire-thin but the only door to the Philippines and South China Sea cells.",
    ),

    # Middle East. Middle East Eye is a London-based Arabic-speaking-region
    # outlet with an English site. Note the URL: the 2026-10-02 GDELT report
    # recommended https://www.middleeasteye.net/rss.xml, which is the FEATURES
    # feed and whose newest item is dated 2019-01-21. /rss is the news feed.
    # Verified 2026-10-02: /rss.xml -> 10 items, newest 2019-01-21 (stale, do
    # not use). /rss -> HTTP 200, 20 items, 20 dated, 20 within 48h, newest
    # 2026-10-02T21:35Z. /news/rss and /rss2.xml both 404.
    # First headline: "Trump says US not 'doing the export ban'".
    "middleeasteye.net": SourceConfig(
        domain="middleeasteye.net",
        name="Middle East Eye",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.middleeasteye.net/rss",  # news feed; /rss.xml is the 2016-2019 features feed
        ],
        geographic_focus="Middle East",
        language="en",
        reliability_score=0.72,
        bias_rating="mixed",
        owner_group="Middle East Eye",
        fetch_priority=2,
        notes="20 items, all within 48h on 2026-10-02. Served gzip-encoded; needs the gunzip path in fetch_feed_polite. Outlet is editorially aligned with an Arab regional perspective, so reliability is set conservatively.",
    ),

    # Azerbaijan / South Caucasus. Trend.az is the largest English-language
    # news operation in Azerbaijan, and the GDELT census found Azerbaijan at
    # zero pipeline articles despite it being an active negotiation venue.
    # Verified 2026-10-02: HTTP 200, 25 items, 25 dated, 25 within 48h,
    # newest 2026-10-02T22:09Z.
    # First headline: "Georgia's transport costs drive nearly 40% of Inflation
    # in September". Feed carries some evergreen filler rows (e.g. "Chronicles
    # of Victory: October 3, 2020"), so max_articles_per_fetch is trimmed.
    "trend.az": SourceConfig(
        domain="trend.az",
        name="Trend Azerbaijan (English)",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://en.trend.az/rss/",
        ],
        geographic_focus="Azerbaijan",
        language="en",
        reliability_score=0.70,
        bias_rating="center-right",
        owner_group="Trend Information Agency",
        fetch_priority=1,
        max_articles_per_fetch=25,
        notes="English-language Azerbaijan/South Caucasus. 25 items, all within 48h on 2026-10-02. Includes evergreen filler rows, so the per-fetch cap is 25.",
    ),

    # Nigeria. Premium Times is one of Nigeria's larger independents with a
    # documented editorial operation; allAfrica above also carries Nigeria
    # stories but only at aggregation density.
    # Verified 2026-10-02: HTTP 200, 15 items, 15 dated, 15 within 48h,
    # newest 2026-10-02T21:17Z.
    # First headline: "Jigawa recruits additional 2,000 secondary school
    # teachers". Mixed desk, so expect domestic Nigerian coverage
    # (education, politics, security) alongside foreign-relations items.
    "premiumtimesng.com": SourceConfig(
        domain="premiumtimesng.com",
        name="Premium Times Nigeria",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://www.premiumtimesng.com/feed/",
        ],
        geographic_focus="Nigeria",
        language="en",
        reliability_score=0.73,
        bias_rating="center",
        owner_group="Premium Times Publishing",
        fetch_priority=1,
        notes="Nigeria national desk. 15 items, all within 48h on 2026-10-02.",
    ),

    # Kenya. Nation Media Group's Kenya feed.
    # Verified 2026-10-02: HTTP 200, 25 items, 25 dated, 25 within 48h,
    # newest 2026-10-02T16:01Z (the /kenya/rss.xml desk feed; the bare
    # /kenya/rss.xml variant returns opinion and blog rows mixed into the same
    # 25, and /category/news/feed/ timed out).
    # First headline: "Faith rewarded: Kisumu Catholic church wins ownership of land".
    "nation.africa": SourceConfig(
        domain="nation.africa",
        name="Nation (Kenya)",
        tier=SourceTier.TIER2,
        category=SourceCategory.NEWSPAPER,
        rss_urls=[
            "https://nation.africa/kenya/rss.xml",
        ],
        geographic_focus="Kenya",
        language="en",
        reliability_score=0.72,
        bias_rating="center",
        owner_group="Nation Media Group",
        fetch_priority=1,
        notes="Kenya national desk. 25 items, all within 48h on 2026-10-02. The feed mixes the /news/ desk with /blogs-opinion/ and the opinion items extract 0 chars of body (checked 3 live on 2026-10-02: 2 opinion items empty, the /news/ item 3920 chars), so those get skipped by MIN_BODY_CHARS rather than stored empty. Not a reason to disable: the news desk ingests.",
    ),

    # ReliefWeb (UN OCHA). The humanitarian wire the AID topic group is about,
    # and the only door to per-crisis situation reports: every item is an OCHA
    # or partner update on a named emergency, which is exactly the shape the
    # corroboration gate wants (two owners, same crisis) and exactly what the
    # general-interest wires do not produce.
    # Verified 2026-10-02: HTTP 200, RSS 2.0, 20 items, 20 dated, 20 within
    # 48h, newest 2026-10-02T20:09Z (6h old at the time of checking). Served
    # with RFC-822 dates, not ISO, so the freshness math needs parsedate.
    # /updates/rss?view=headlines is 404 and the api.reliefweb.int RSS route is
    # 410 Gone; /updates/rss.xml is the live one.
    # First three headlines: "DR Congo: Humanitarian Dashboard (July 2026)",
    # "Mexico: Latin America & The Caribbean Weekly Situation Update as of
    # 2 October 2026", plus a Nigeria item.
    "reliefweb.int": SourceConfig(
        domain="reliefweb.int",
        name="ReliefWeb (UN OCHA)",
        tier=SourceTier.TIER2,
        # OTHER, not WIRE_SERVICE: WIRE_SERVICE is load-bearing in
        # src/verification/corroboration.py (wire items collapse to one owner so
        # one agency cannot corroborate itself), and every item here comes from
        # OCHA or a named partner, so marking it a wire would make the whole feed
        # one owner. Keeping it OTHER leaves that judgement to the evidence.
        category=SourceCategory.OTHER,
        geographic_focus="Global",
        language="en",
        rss_urls=[
            "https://reliefweb.int/updates/rss.xml",
        ],
        reliability_score=0.93,
        bias_rating="center",
        owner_group="United Nations OCHA",
        fetch_priority=1,
        notes="UN OCHA humanitarian updates wire. 20 items, all within 48h on 2026-10-02. Situation reports rather than news, so expect documents as well as articles.",
    ),

    # The New Humanitarian. Left in the registry, disabled, because the only feed
    # it publishes is stale and the alternative URLs are refused. Recorded rather
    # than deleted so the next person does not re-run the search.
    # Verified 2026-10-02: /rss.xml answers HTTP 200, RSS 2.0, 10 items, 10
    # dated - but the NEWEST item is 2026-07-01T14:50Z, 2243h (93 days) old, and
    # 0 of 10 are within 48h. /rss, /news/rss, /news/rss.xml and /atom.xml all
    # 403, /rss/latest 404s, and ?page=1 / ?pagesize=20 return the identical
    # stale 12438-byte body, so there is no fresher feed behind it. Enabling it
    # would inject quarter-old articles into a pipeline that treats feed items
    # as news, so it stays off until the publisher revives it. Re-verify before
    # ever re-enabling: the test is the newest item's age, not the item count.
    "thenewhumanitarian.org": SourceConfig(
        domain="thenewhumanitarian.org",
        name="The New Humanitarian",
        tier=SourceTier.TIER2,
        category=SourceCategory.OTHER,
        rss_urls=[
            "https://www.thenewhumanitarian.org/rss.xml",
        ],
        geographic_focus="Global",
        language="en",
        reliability_score=0.88,
        bias_rating="center-left",
        owner_group="The New Humanitarian",
        fetch_priority=2,
        enabled=False,
        notes="Disabled 2026-10-02: the only published feed is stale (200, 10 items, newest 2026-07-01, 93 days old, 0 within 48h); /rss, /news/rss, /atom.xml 403 and /rss/latest 404s. Article bodies are unreachable too: 3 of 3 sampled items extracted 0 chars.",
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
        # Disabled 2026-10-02: no ingestion adapter exists for this domain and
        # the only code that talks to it (enrichment/social_snippets.py
        # BlueskyFinder) needs BLUESKY_HANDLE + BLUESKY_PASSWORD, i.e. a real
        # account. Listing it enabled made the registry claim coverage the
        # pipeline does not have; being disabled is the honest state until an
        # account-free adapter exists.
        enabled=False,
        notes=(
            "No adapter. The only Bluesky code path requires an account "
            "(BLUESKY_HANDLE/BLUESKY_PASSWORD), so it is enrichment-only and "
            "was never an article source."
        ),
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
        # Disabled 2026-10-02: no adapter, no feed list, and no account-free
        # index. Substack has no cross-publisher feed directory, so a feed can
        # only be added one newsletter at a time, and the only public way to
        # enumerate them is a logged-in session. Enabled with an empty
        # rss_urls this entry was indistinguishable from a working source.
        enabled=False,
        notes=(
            "No adapter and no feed index: Substack publishes per-newsletter "
            "RSS only, with no public all-publisher feed and no account-free "
            "enumeration. Add specific newsletter feeds here to re-enable."
        ),
    ),
}

# Combined registry
ALL_SOURCES = {}
ALL_SOURCES.update(TIER1_SOURCES)
ALL_SOURCES.update(TIER2_SOURCES)
ALL_SOURCES.update(TIER3_SOURCES)
ALL_SOURCES.update(TIER4_SOURCES)
ALL_SOURCES.update(SENSOR_SOURCES)

# Sensors that have their own ingestion path, keyed by the sensors.py function.
SENSOR_FEEDS = {
    "earthquake.usgs.gov": "usgs_earthquakes",
    "gdacs.org": "gdacs_alerts",
}


def get_enabled_sensor_feeds() -> dict[str, str]:
    """Enabled sensor domains mapped to their sensors.py function name."""
    return {
        domain: SENSOR_FEEDS[domain]
        for domain, config in SENSOR_SOURCES.items()
        if config.enabled and domain in SENSOR_FEEDS
    }


def get_sources_by_tier(tier: SourceTier) -> dict[str, SourceConfig]:
    """Get all sources for a specific tier."""
    return {k: v for k, v in ALL_SOURCES.items() if v.tier == tier}


def get_enabled_sources_by_tier(tier: SourceTier) -> dict[str, SourceConfig]:
    """Get enabled sources for a specific tier."""
    return {k: v for k, v in ALL_SOURCES.items() if v.tier == tier and v.enabled}


def get_source_config(domain: str) -> SourceConfig | None:
    """Get source configuration by domain."""
    return ALL_SOURCES.get(domain)


def get_all_rss_feeds() -> dict[str, list[str]]:
    """Get all RSS feed URLs grouped by domain."""
    feeds = {}
    for domain, config in ALL_SOURCES.items():
        if config.enabled and config.rss_urls:
            feeds[domain] = config.rss_urls
    return feeds