"""Media extraction from article HTML using trafilatura and custom selectors."""

import re
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from src.utils.trafilatura_extract import extract_article
from src.schema.models import MediaAsset, MediaAsset as MediaType
import logging

logger = logging.getLogger(__name__)


# Common video embed patterns
VIDEO_PATTERNS = {
    "youtube": [
        r'(?:youtube\.com/(?:embed/|v/|watch\?v=)|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    ],
    "vimeo": [
        r'vimeo\.com/(\d+)',
        r'player\.vimeo\.com/video/(\d+)',
    ],
    "dailymotion": [
        r'dailymotion\.com/video/([a-zA-Z0-9]+)',
    ],
    "twitch": [
        r'twitch\.tv/videos/(\d+)',
        r'clips\.twitch\.tv/([a-zA-Z0-9]+)',
    ],
}


# Social media embed patterns
SOCIAL_PATTERNS = {
    "twitter": [
        r'twitter\.com/\w+/status/(\d+)',
        r'x\.com/\w+/status/(\d+)',
    ],
    "instagram": [
        r'instagram\.com/p/([a-zA-Z0-9_-]+)',
        r'instagram\.com/reel/([a-zA-Z0-9_-]+)',
    ],
    "tiktok": [
        r'tiktok\.com/@\w+/video/(\d+)',
    ],
    "bluesky": [
        r'bsky\.app/profile/[\w.]+/post/([a-zA-Z0-9]+)',
    ],
    "linkedin": [
        r'linkedin\.com/posts/([a-zA-Z0-9-]+)',
    ],
    "facebook": [
        r'facebook\.com/\w+/posts/(\d+)',
        r'fb\.watch/([a-zA-Z0-9]+)',
    ],
}


async def extract_media_from_html(html: str, base_url: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extract all media assets from article HTML.

    Returns dict with keys: images, videos, embeds, audio
    """
    soup = BeautifulSoup(html, 'html.parser')
    media = {
        "images": [],
        "videos": [],
        "embeds": [],
        "audio": [],
    }

    # Extract images
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
        if not src:
            continue

        # Make absolute URL
        src = urljoin(base_url, src)

        # Skip tiny images (likely tracking pixels, icons)
        width = img.get('width')
        height = img.get('height')
        if width and height:
            try:
                if int(width) < 50 or int(height) < 50:
                    continue
            except ValueError:
                pass

        media["images"].append({
            "url": src,
            "alt_text": img.get('alt', ''),
            "width": int(width) if width and width.isdigit() else None,
            "height": int(height) if height and height.isdigit() else None,
            "source": "article",
        })

    # Extract videos (HTML5 video tags)
    for video in soup.find_all('video'):
        src = video.get('src')
        if not src:
            # Check source children
            for source in video.find_all('source'):
                src = source.get('src')
                if src:
                    break

        if src:
            src = urljoin(base_url, src)
            media["videos"].append({
                "url": src,
                "thumbnail_url": video.get('poster'),
                "width": int(video.get('width')) if video.get('width', '').isdigit() else None,
                "height": int(video.get('height')) if video.get('height', '').isdigit() else None,
                "source": "article",
            })

    # Extract iframes (embeds)
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src')
        if not src:
            continue

        src = urljoin(base_url, src)

        # Classify embed type
        embed_type = classify_embed(src)
        if embed_type:
            media["embeds"].append({
                "url": src,
                "type": embed_type,
                "source": "iframe",
            })

    # Extract video embeds from text content (YouTube, Vimeo, etc.)
    text_content = soup.get_text()
    video_embeds = extract_video_embeds(text_content, base_url)
    media["videos"].extend(video_embeds)

    # Extract social media embeds
    social_embeds = extract_social_embeds(text_content, base_url)
    media["embeds"].extend(social_embeds)

    # Extract audio
    for audio in soup.find_all('audio'):
        src = audio.get('src')
        if not src:
            for source in audio.find_all('source'):
                src = source.get('src')
                if src:
                    break

        if src:
            src = urljoin(base_url, src)
            media["audio"].append({
                "url": src,
                "source": "article",
            })

    return media


def classify_embed(url: str) -> Optional[str]:
    """Classify an iframe embed URL by platform."""
    domain = urlparse(url).netloc.lower()

    if 'youtube' in domain or 'youtu.be' in domain:
        return 'youtube'
    elif 'vimeo' in domain:
        return 'vimeo'
    elif 'twitter' in domain or 'x.com' in domain:
        return 'twitter'
    elif 'instagram' in domain:
        return 'instagram'
    elif 'tiktok' in domain:
        return 'tiktok'
    elif 'bsky' in domain or 'bluesky' in domain:
        return 'bluesky'
    elif 'linkedin' in domain:
        return 'linkedin'
    elif 'facebook' in domain or 'fb.watch' in domain:
        return 'facebook'
    elif 'dailymotion' in domain:
        return 'dailymotion'
    elif 'twitch' in domain:
        return 'twitch'
    elif 'soundcloud' in domain:
        return 'soundcloud'
    elif 'spotify' in domain:
        return 'spotify'

    return 'embed'


def extract_video_embeds(text: str, base_url: str) -> List[Dict[str, Any]]:
    """Extract video IDs from text content and build embed URLs."""
    videos = []

    for platform, patterns in VIDEO_PATTERNS.items():
        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                embed_url = build_video_embed_url(platform, match)
                if embed_url:
                    videos.append({
                        "url": embed_url,
                        "source_id": match,
                        "source": platform,
                        "media_type": "video",
                    })

    return videos


def extract_social_embeds(text: str, base_url: str) -> List[Dict[str, Any]]:
    """Extract social media post IDs from text content."""
    embeds = []

    for platform, patterns in SOCIAL_PATTERNS.items():
        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                embed_url = build_social_embed_url(platform, match)
                if embed_url:
                    embeds.append({
                        "url": embed_url,
                        "type": platform,
                        "source_id": match,
                        "source": platform,
                    })

    return embeds


def build_video_embed_url(platform: str, video_id: str) -> Optional[str]:
    """Build embed URL for a video platform."""
    embed_urls = {
        "youtube": f"https://www.youtube.com/embed/{video_id}",
        "vimeo": f"https://player.vimeo.com/video/{video_id}",
        "dailymotion": f"https://www.dailymotion.com/embed/video/{video_id}",
        "twitch": f"https://clips.twitch.tv/embed?clip={video_id}",
    }
    return embed_urls.get(platform)


def build_social_embed_url(platform: str, post_id: str) -> Optional[str]:
    """Build embed URL for a social media post."""
    embed_urls = {
        "twitter": f"https://publish.twitter.com/oembed?url=https://twitter.com/user/status/{post_id}",
        "instagram": f"https://www.instagram.com/p/{post_id}/embed/",
        "tiktok": f"https://www.tiktok.com/embed/{post_id}",
        "bluesky": f"https://bsky.app/profile/user/post/{post_id}",
        "linkedin": f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}",
        "facebook": f"https://www.facebook.com/plugins/post.php?href=https://www.facebook.com/user/posts/{post_id}",
    }
    return embed_urls.get(platform)


async def extract_media_from_article(article_id: str, url: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extract media from a live article URL.

    Uses trafilatura for initial extraction, then parses HTML for media.
    """
    try:
        # Use trafilatura to get clean HTML
        body_text, extracted_title = await extract_article(url, source_key=article_id)

        # For media extraction, we need the raw HTML
        # We'll re-fetch the article to get HTML
        import httpx
        from src.shared.config import get_settings

        settings = get_settings()
        async with httpx.AsyncClient(timeout=settings.rss_fetch_timeout) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            html = response.text

        return extract_media_from_html(html, url)

    except Exception as e:
        logger.warning(f"Failed to extract media from {url}: {e}")
        return {"images": [], "videos": [], "embeds": [], "audio": []}


def deduplicate_media(media_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicate media by URL."""
    seen = set()
    unique = []
    for item in media_list:
        url = item.get('url')
        if url and url not in seen:
            seen.add(url)
            unique.append(item)
    return unique