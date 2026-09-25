"""Build reporting units by clustering near-duplicate articles."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import RawArticle, ReportingUnit, SourceTier
from src.utils.minhash_utils import (
    shingle_text,
    cluster_articles_by_containment,
)
from src.shared.config import get_settings
from datetime import datetime, timezone, timedelta
from collections import defaultdict


OWNERSHIP_GROUPS = {
    # Wire services
    "apnews.com": "AP",
    "reuters.com": "Reuters",
    "afp.com": "AFP",
    "pa.media": "PA Media",

    # US TV rollups
    "sinclair.com": "Sinclair",
    "nexstar.tv": "Nexstar",
    "gray.tv": "Gray Television",
    "tegna.com": "Tegna",

    # Newspaper chains
    "gannett.com": "Gannett",
    "hearst.com": "Hearst",
    "advance.net": "Advance Publications",
    "mclatchy.com": "McClatchy",
    "tribpub.com": "Tribune Publishing",

    # Entertainment (critical for celebrity vertical)
    "variety.com": "Penske",
    "deadline.com": "Penske",
    "hollywoodreporter.com": "Penske",
    "rollingstone.com": "Penske",
    "billboard.com": "Penske",

    # International broadcasters (tier-1)
    "dw.com": "DW",
    "france24.com": "France24",
    "aljazeera.com": "Al Jazeera",
    "euronews.com": "Euronews",
    "pbs.org": "PBS",

    # Tier-2 major papers (enabled in source_registry)
    "nytimes.com": "NYT",
    "washingtonpost.com": "WaPo",
    "wsj.com": "WSJ",
    "ft.com": "Financial Times",
    "economist.com": "Economist",
    "foreignpolicy.com": "Foreign Policy",
    "foreignaffairs.com": "Foreign Affairs",
    "csis.org": "CSIS",
    "who.int": "WHO",
    "latimes.com": "LA Times",
    "chicagotribune.com": "Chicago Tribune",
    "bostonglobe.com": "Boston Globe",
    "sfgate.com": "SFGate",
    "seattletimes.com": "Seattle Times",
    "denverpost.com": "Denver Post",
    "miamiherald.com": "Miami Herald",
    "ajc.com": "AJC",
    "houstonchronicle.com": "Houston Chronicle",
    "dallasnews.com": "Dallas News",
    "phillyinquirer.com": "Philly Inquirer",
    "startribune.com": "Star Tribune",
    "oregonlive.com": "OregonLive",
    "dispatch.com": "Dispatch",
    "tennessean.com": "Tennessean",
    "courier-journal.com": "Courier Journal",
    "cincinnati.com": "Cincinnati Enquirer",
    "jsonline.com": "Journal Sentinel",
    "freep.com": "Free Press",
    "azcentral.com": "AZCentral",
    "reviewjournal.com": "Review Journal",
    "rgj.com": "Reno Gazette",
    "cjonline.com": "Topeka Capital-Journal",
    "statesman.com": "Statesman",
    "pressherald.com": "Portland Press Herald",
    "burlingtonfreepress.com": "Burlington Free Press",
    "dailycamera.com": "Daily Camera",
    "coloradoan.com": "Coloradoan",
    "journalnow.com": "Journal Now",
    "greensboro.com": "Greensboro",
    "fayobserver.com": "Fayetteville Observer",
    "citizen-times.com": "Citizen Times",
    "postandcourier.com": "Post and Courier",
    "thestate.com": "The State",
    "tallahassee.com": "Tallahassee",
    "news-press.com": "News Press",
    "naplesnews.com": "Naples News",
    "pnj.com": "Pensacola News Journal",
    "tcpalm.com": "TCPalm",
    "floridatoday.com": "Florida Today",
    "tampabay.com": "Tampa Bay Times",
    "orlandosentinel.com": "Orlando Sentinel",
    "sun-sentinel.com": "Sun Sentinel",
    "palmbeachpost.com": "Palm Beach Post",
    "tcpanews.com": "TCP News",
    "kansascity.com": "Kansas City Star",
    "stltoday.com": "STL Today",
    "columbiatribune.com": "Columbia Tribune",
    "springfieldnewssun.com": "Springfield News Sun",
    "daytondailynews.com": "Dayton Daily News",
    "wichitaeagle.com": "Wichita Eagle",
    "kansas.com": "Kansas.com",
    "omaha.com": "Omaha World-Herald",
    "journalstar.com": "Journal Star",
    "rapidcityjournal.com": "Rapid City Journal",
    "argusleader.com": "Argus Leader",
    "siouxcityjournal.com": "Sioux City Journal",
    "thegazette.com": "The Gazette",
    "qctimes.com": "Quad-City Times",
    "desmoinesregister.com": "Des Moines Register",
    "waterloocedarfallscourier.com": "Waterloo Cedar Falls Courier",
    "globegazette.com": "Globe Gazette",
    "messengernews.net": "Messenger News",
    "carrollspaper.com": "Carroll Daily Times Herald",
    "dailyjournal.net": "Daily Journal",
    "timesdaily.com": "Times Daily",
    "decaturdaily.com": "Decatur Daily",
    "annistonstar.com": "Anniston Star",
    "gadsdentimes.com": "Gadsden Times",
    "dothaneagle.com": "Dothan Eagle",
    "opelikaauburnnews.com": "Opelika-Auburn News",
    "tuscaloosanews.com": "Tuscaloosa News",
    "montgomeryadvertiser.com": "Montgomery Advertiser",
    "timesrecordnews.com": "Times Record News",
    "wacotrib.com": "Waco Tribune",
    "tylerpaper.com": "Tyler Paper",
    "longviewnewsjournal.com": "Longview News Journal",

    # Tier-1 international
    "bbc.com": "BBC",
    "theguardian.com": "Guardian",
    "npr.org": "NPR",

    # Tier-3 social
    "reddit.com": "Reddit",
    "twitter.com": "X/Twitter",
    "x.com": "X/Twitter",
    "bsky.social": "Bluesky",
    "threads.net": "Threads",
    "mastodon.social": "Mastodon",
    "facebook.com": "Meta",
    "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",

    # Disabled tier-2 sources (no working RSS) - commented out:
    # "brookings.edu": "Brookings",
    # "chathamhouse.org": "Chatham House",
    # "un.org": "UN",
}


def get_owner_group(domain: str) -> str:
    """Map domain to ownership group."""
    # Check exact match first
    if domain in OWNERSHIP_GROUPS:
        return OWNERSHIP_GROUPS[domain]

    # Check subdomain matches
    for known_domain, group in OWNERSHIP_GROUPS.items():
        if domain.endswith("." + known_domain) or domain == known_domain:
            return group

    return "Independent"


async def build_reporting_units(session: AsyncSession) -> int:
    """
    Cluster articles from the last 24h into reporting units.
    Each unit = one reporting event (original + syndications).
    """
    settings = get_settings()
    threshold = settings.containment_threshold

    # Get articles from last 24h that aren't yet clustered
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = select(RawArticle).where(
        RawArticle.fetched_at >= cutoff,
        RawArticle.reporting_unit_id.is_(None),  # Not yet assigned
    )
    result = await session.execute(stmt)
    articles = result.scalars().all()

    if not articles:
        return 0

    # Group by day bucket (UTC date)
    day_buckets = defaultdict(list)
    for art in articles:
        day = art.published_at or art.fetched_at
        day = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        day_buckets[day].append(art)

    total_units = 0

    for day, day_articles in day_buckets.items():
        # Prepare for clustering: [(article_id, tokens)]
        article_tokens = []
        for art in day_articles:
            tokens = shingle_text(art.body_text or "", k=5)
            article_tokens.append((str(art.id), tokens))

        # Cluster by containment
        clusters = cluster_articles_by_containment(article_tokens, threshold=threshold)

        for cluster in clusters:
            # Find representative (longest body text)
            cluster_articles = [a for a in day_articles if str(a.id) in cluster]
            representative = max(cluster_articles, key=lambda a: len(a.body_text or ""))

            # Count source tiers and owner groups
            tier_counts = defaultdict(int)
            owner_counts = defaultdict(int)
            tier1_owner_counts = defaultdict(int)
            for art in cluster_articles:
                tier_counts[art.source_tier.value] += 1
                owner = get_owner_group(art.source_domain)
                owner_counts[owner] += 1
                if art.source_tier == SourceTier.TIER1:
                    tier1_owner_counts[owner] += 1

            # Create reporting unit
            unit = ReportingUnit(
                day=day,
                representative_article_id=representative.id,
                article_count=len(cluster_articles),
                source_tiers=dict(tier_counts),
                owner_groups=dict(owner_counts),
                tier1_owner_groups=dict(tier1_owner_counts),
            )
            session.add(unit)
            await session.flush()

            # Link articles to unit
            for art in cluster_articles:
                art.reporting_unit_id = unit.id

            total_units += 1

    await session.commit()
    return total_units