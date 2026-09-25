"""Social media snippet extraction from various platforms."""

import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


class SocialSnippetFinder:
    """Find relevant social media posts for a story."""

    def __init__(self):
        # API keys from environment
        self.twitter_bearer = os.getenv("TWITTER_BEARER_TOKEN")
        self.bluesky_handle = os.getenv("BLUESKY_HANDLE")
        self.bluesky_password = os.getenv("BLUESKY_PASSWORD")
        self.reddit_client_id = os.getenv("REDDIT_CLIENT_ID")
        self.reddit_client_secret = os.getenv("REDDIT_CLIENT_SECRET")

    async def find_twitter_posts(
        self,
        query: str,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find relevant tweets using Twitter API v2."""
        if not self.twitter_bearer:
            logger.warning("Twitter bearer token not configured")
            return []

        import httpx

        headers = {
            "Authorization": f"Bearer {self.twitter_bearer}",
        }

        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,author_id,public_metrics,conversation_id,lang",
            "expansions": "author_id",
            "user.fields": "username,verified",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    "https://api.twitter.com/2/tweets/search/recent",
                    headers=headers,
                    params=params,
                )

                if response.status_code == 429:
                    logger.warning("Twitter API rate limited")
                    return []

                response.raise_for_status()
                data = response.json()

                tweets = []
                users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

                for tweet in data.get("data", []):
                    author = users.get(tweet.get("author_id"), {})
                    metrics = tweet.get("public_metrics", {})

                    tweets.append({
                        "post_id": tweet["id"],
                        "text": tweet["text"],
                        "author_username": author.get("username", "unknown"),
                        "author_verified": author.get("verified", False),
                        "created_at": tweet["created_at"],
                        "retweet_count": metrics.get("retweet_count", 0),
                        "like_count": metrics.get("like_count", 0),
                        "reply_count": metrics.get("reply_count", 0),
                        "quote_count": metrics.get("quote_count", 0),
                        "url": f"https://twitter.com/{author.get('username', 'user')}/status/{tweet['id']}",
                        "source": "twitter",
                    })

                return tweets

            except Exception as e:
                logger.error(f"Twitter search failed: {e}")
                return []


class BlueskyFinder:
    """Find relevant Bluesky posts using AT Protocol."""

    def __init__(self, handle: Optional[str] = None, password: Optional[str] = None):
        self.handle = handle or os.getenv("BLUESKY_HANDLE")
        self.password = password or os.getenv("BLUESKY_PASSWORD")
        self.access_token = None
        self.did = None

    async def authenticate(self) -> bool:
        """Authenticate with Bluesky."""
        if not self.handle or not self.password:
            return False

        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    "https://bsky.social/xrpc/com.atproto.server.createSession",
                    json={"identifier": self.handle, "password": self.password},
                )
                response.raise_for_status()
                data = response.json()
                self.access_token = data["accessJwt"]
                self.did = data["did"]
                return True
            except Exception as e:
                logger.error(f"Bluesky auth failed: {e}")
                return False

    async def search_posts(
        self,
        query: str,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search Bluesky posts."""
        if not self.access_token and not await self.authenticate():
            return []

        import httpx

        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        params = {
            "q": query,
            "limit": min(max_results, 100),
            "sort": "top",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    "https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()

                posts = []
                for post in data.get("posts", []):
                    record = post.get("record", {})
                    author = post.get("author", {})

                    posts.append({
                        "post_id": post["uri"].split("/")[-1],
                        "text": record.get("text", ""),
                        "author_handle": author.get("handle", "unknown"),
                        "author_did": author.get("did", ""),
                        "author_avatar": author.get("avatar", ""),
                        "created_at": record.get("createdAt", ""),
                        "like_count": post.get("likeCount", 0),
                        "repost_count": post.get("repostCount", 0),
                        "reply_count": post.get("replyCount", 0),
                        "url": f"https://bsky.app/profile/{author.get('handle', 'user')}/post/{post['uri'].split('/')[-1]}",
                        "source": "bluesky",
                    })

                return posts

            except Exception as e:
                logger.error(f"Bluesky search failed: {e}")
                return []


class RedditFinder:
    """Find relevant Reddit posts/comments using Reddit API."""

    def __init__(self):
        self.client_id = os.getenv("REDDIT_CLIENT_ID")
        self.client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        self.access_token = None
        self.token_expires = 0

    async def _get_access_token(self) -> bool:
        """Get Reddit OAuth token."""
        if not self.client_id or not self.client_secret:
            return False

        import httpx
        import time

        if self.access_token and time.time() < self.token_expires:
            return True

        auth = (self.client_id, self.client_secret)
        headers = {"User-Agent": "news-pipeline/0.1"}

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    "https://www.reddit.com/api/v1/access_token",
                    auth=auth,
                    headers=headers,
                    data={"grant_type": "client_credentials"},
                )
                response.raise_for_status()
                data = response.json()
                self.access_token = data["access_token"]
                self.token_expires = time.time() + data["expires_in"] - 60
                return True
            except Exception as e:
                logger.error(f"Reddit auth failed: {e}")
                return False

    async def search_posts(
        self,
        query: str,
        subreddits: Optional[List[str]] = None,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search Reddit posts."""
        if not await self._get_access_token():
            logger.warning("Reddit credentials not configured")
            return []

        import httpx

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "news-pipeline/0.1",
        }

        # Build subreddit filter
        sub_filter = ""
        if subreddits:
            sub_filter = " OR ".join([f"subreddit:{s}" for s in subreddits])

        search_query = f"{query} {sub_filter}".strip()

        params = {
            "q": search_query,
            "limit": min(max_results, 100),
            "sort": "relevance",
            "t": "week",  # Past week
            "restrict_sr": "false",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    "https://oauth.reddit.com/search",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                data = response.json()

                posts = []
                for post in data.get("data", {}).get("children", []):
                    p = post["data"]
                    posts.append({
                        "post_id": p["id"],
                        "title": p["title"],
                        "text": p.get("selftext", "")[:1000],
                        "author": p["author"],
                        "subreddit": p["subreddit"],
                        "score": p["score"],
                        "upvote_ratio": p["upvote_ratio"],
                        "num_comments": p["num_comments"],
                        "created_at": datetime.fromtimestamp(p["created_utc"], tz=timezone.utc).isoformat(),
                        "url": f"https://reddit.com{p['permalink']}",
                        "source": "reddit",
                    })

                return posts

            except Exception as e:
                logger.error(f"Reddit search failed: {e}")
                return []


async def find_social_snippets(
    query: str,
    key_entities: List[str],
    max_results: int = 10,
    platforms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Find relevant social media posts across platforms.

    Args:
        query: Search query
        key_entities: Key entities from the story
        max_results: Max results per platform
        platforms: Which platforms to search (default: all available)
    """
    if platforms is None:
        platforms = ["twitter", "bluesky", "reddit"]

    # Enhance query with entities
    enhanced_query = query
    if key_entities:
        enhanced_query += " " + " ".join(key_entities[:5])

    # Run searches in parallel
    tasks = []

    if "twitter" in platforms:
        twitter = SocialSnippetFinder()
        tasks.append(("twitter", twitter.find_twitter_posts(enhanced_query, max_results)))

    if "bluesky" in platforms:
        bluesky = BlueskyFinder()
        tasks.append(("bluesky", bluesky.search_posts(enhanced_query, max_results)))

    if "reddit" in platforms:
        reddit = RedditFinder()
        tasks.append(("reddit", reddit.search_posts(enhanced_query, max_results=max_results)))

    import asyncio
    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

    # Combine results
    all_snippets = []
    for i, result in enumerate(results):
        platform = tasks[i][0]
        if isinstance(result, Exception):
            logger.error(f"{platform} search exception: {result}")
            continue
        if isinstance(result, list):
            for item in result:
                item["platform"] = platform
            all_snippets.extend(result)

    # Sort by engagement (likes + retweets + replies etc.)
    def engagement_score(item):
        return (
            item.get("like_count", 0) +
            item.get("retweet_count", 0) +
            item.get("repost_count", 0) +
            item.get("reply_count", 0) +
            item.get("score", 0)  # Reddit score
        )

    all_snippets.sort(key=engagement_score, reverse=True)

    return all_snippets[:max_results * len(platforms)]


async def find_snippets_for_story(
    story_title: str,
    key_entities: List[str],
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """Find social snippets relevant to a story."""
    return await find_social_snippets(
        query=story_title,
        key_entities=key_entities,
        max_results=max_results,
    )