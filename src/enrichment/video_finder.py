"""Video finder using YouTube/Vimeo APIs for related content."""

import os
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from src.utils.ingest_stats import STATS
import logging

logger = logging.getLogger(__name__)


class YouTubeFinder:
    """Find related YouTube videos using YouTube Data API v3."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("YOUTUBE_API_KEY")
        self.base_url = "https://www.googleapis.com/youtube/v3"

    async def search_videos(
        self,
        query: str,
        max_results: int = 10,
        published_after: Optional[datetime] = None,
        order: str = "relevance",
    ) -> List[Dict[str, Any]]:
        """
        Search for YouTube videos.

        Args:
            query: Search query
            max_results: Maximum results to return
            published_after: Only return videos published after this date
            order: relevance, date, rating, viewCount, title
        """
        if not self.api_key:
            logger.warning("YouTube API key not configured")
            return []

        import httpx

        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": min(max_results, 50),
            "order": order,
            "key": self.api_key,
        }

        if published_after:
            params["publishedAfter"] = published_after.isoformat() + "Z"

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(f"{self.base_url}/search", params=params)
                response.raise_for_status()
                data = response.json()

                videos = []
                for item in data.get("items", []):
                    video_id = item["id"]["videoId"]
                    snippet = item["snippet"]
                    videos.append({
                        "video_id": video_id,
                        "title": snippet["title"],
                        "description": snippet["description"][:500],
                        "thumbnail_url": snippet["thumbnails"]["high"]["url"],
                        "channel_title": snippet["channelTitle"],
                        "channel_id": snippet["channelId"],
                        "published_at": snippet["publishedAt"],
                        "embed_url": f"https://www.youtube.com/embed/{video_id}",
                        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
                        "source": "youtube",
                    })

                STATS.record("youtube", "search_ok")
                return videos

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    logger.error("YouTube API quota exceeded or key invalid")
                else:
                    logger.error(f"YouTube search failed: {e}")
                STATS.record("youtube", f"search_failed:http_{e.response.status_code}")
                return []
            except Exception as e:
                logger.error(f"YouTube search error: {e}")
                STATS.record("youtube", f"search_failed:error_{type(e).__name__}")
                return []

    async def get_video_details(self, video_ids: List[str]) -> List[Dict[str, Any]]:
        """Get detailed info for specific video IDs."""
        if not self.api_key or not video_ids:
            return []

        import httpx

        params = {
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(video_ids[:50]),
            "key": self.api_key,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(f"{self.base_url}/videos", params=params)
                response.raise_for_status()
                data = response.json()

                videos = []
                for item in data.get("items", []):
                    snippet = item["snippet"]
                    content_details = item["contentDetails"]
                    statistics = item.get("statistics", {})

                    # Parse duration (ISO 8601)
                    duration = self._parse_duration(content_details["duration"])

                    videos.append({
                        "video_id": item["id"],
                        "title": snippet["title"],
                        "description": snippet["description"][:500],
                        "thumbnail_url": snippet["thumbnails"]["high"]["url"],
                        "channel_title": snippet["channelTitle"],
                        "channel_id": snippet["channelId"],
                        "published_at": snippet["publishedAt"],
                        "duration_seconds": duration,
                        "view_count": int(statistics.get("viewCount", 0)),
                        "like_count": int(statistics.get("likeCount", 0)),
                        "comment_count": int(statistics.get("commentCount", 0)),
                        "embed_url": f"https://www.youtube.com/embed/{item['id']}",
                        "watch_url": f"https://www.youtube.com/watch?v={item['id']}",
                        "source": "youtube",
                    })

                return videos

            except Exception as e:
                logger.error(f"YouTube video details failed: {e}")
                return []

    def _parse_duration(self, iso_duration: str) -> int:
        """Parse ISO 8601 duration to seconds."""
        import re
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
        match = re.match(pattern, iso_duration)
        if not match:
            return 0
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        return hours * 3600 + minutes * 60 + seconds


class VimeoFinder:
    """Find related Vimeo videos using Vimeo API."""

    def __init__(self, access_token: Optional[str] = None):
        self.access_token = access_token or os.getenv("VIMEO_ACCESS_TOKEN")
        self.base_url = "https://api.vimeo.com"

    async def search_videos(
        self,
        query: str,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search for Vimeo videos."""
        if not self.access_token:
            logger.warning("Vimeo access token not configured")
            return []

        import httpx

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/vnd.vimeo.*+json;version=3.4",
        }

        params = {
            "query": query,
            "per_page": min(max_results, 50),
            "fields": "uri,name,description,link,pictures.sizes,created_time,duration,stats.plays",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/videos",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()

                videos = []
                for item in data.get("data", []):
                    # Extract video ID from URI
                    video_id = item["uri"].split("/")[-1]

                    # Get thumbnail
                    thumbnail = ""
                    for size in item.get("pictures", {}).get("sizes", []):
                        if size.get("width", 0) >= 640:
                            thumbnail = size["link"]
                            break

                    videos.append({
                        "video_id": video_id,
                        "title": item["name"],
                        "description": item["description"][:500],
                        "thumbnail_url": thumbnail,
                        "published_at": item["created_time"],
                        "duration_seconds": item.get("duration", 0),
                        "view_count": item.get("stats", {}).get("plays", 0),
                        "embed_url": f"https://player.vimeo.com/video/{video_id}",
                        "watch_url": item["link"],
                        "source": "vimeo",
                    })

                STATS.record("vimeo", "search_ok")
                return videos

            except Exception as e:
                logger.error(f"Vimeo search failed: {e}")
                STATS.record("vimeo", f"search_failed:error_{type(e).__name__}")
                return []


async def find_related_videos(
    query: str,
    max_results: int = 10,
    youtube_key: Optional[str] = None,
    vimeo_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Find related videos from multiple sources.

    Args:
        query: Search query (e.g., article title, key entities)
        max_results: Total max results across all sources
        youtube_key: YouTube API key (optional, uses env var)
        vimeo_token: Vimeo access token (optional, uses env var)

    Returns:
        Combined list of video results
    """
    youtube = YouTubeFinder(youtube_key)
    vimeo = VimeoFinder(vimeo_token)

    # Run searches in parallel
    youtube_results, vimeo_results = await asyncio.gather(
        youtube.search_videos(query, max_results=max_results // 2),
        vimeo.search_videos(query, max_results=max_results // 2),
        return_exceptions=True,
    )

    # Handle exceptions
    if isinstance(youtube_results, Exception):
        logger.error(f"YouTube search exception: {youtube_results}")
        youtube_results = []
    if isinstance(vimeo_results, Exception):
        logger.error(f"Vimeo search exception: {vimeo_results}")
        vimeo_results = []

    # Combine and deduplicate
    all_videos = youtube_results + vimeo_results

    # Sort by relevance (YouTube already sorted by relevance, Vimeo by relevance)
    # Could add more sophisticated ranking here
    return all_videos[:max_results]


async def find_videos_for_story(
    story_title: str,
    key_entities: List[str],
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """
    Find videos relevant to a story.

    Combines story title and key entities for search query.
    """
    # Build search query from title and entities
    query_parts = [story_title]
    query_parts.extend(key_entities[:5])  # Top 5 entities
    query = " ".join(query_parts)

    return await find_related_videos(query, max_results=max_results)