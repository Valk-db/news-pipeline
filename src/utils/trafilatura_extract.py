"""Article body extraction using trafilatura."""

import trafilatura
from typing import Optional, Tuple
import hashlib
import asyncio


def _extract_article_sync(url: str, html: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Synchronous article extraction (blocking).
    Internal function - use extract_article() for async version.
    """
    try:
        if html:
            downloaded = html
        else:
            downloaded = trafilatura.fetch_url(url, no_ssl=False)
            if not downloaded:
                return None, None

        # Extract with metadata
        result = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            include_images=False,
            output_format="json",
            with_metadata=True,
        )

        if not result:
            return None, None

        import json
        data = json.loads(result)
        body = data.get("text", "").strip()
        title = data.get("title", "").strip()

        return body if body else None, title if title else None

    except Exception:
        return None, None


async def extract_article(url: str, html: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract article body text and title from URL or HTML (async).
    Runs blocking trafilatura call in a thread pool to avoid blocking event loop.
    Returns (body_text, title) or (None, None) on failure.
    """
    return await asyncio.to_thread(_extract_article_sync, url, html)


def compute_content_hash(text: str) -> str:
    """SHA256 hash of normalized text for exact dedup."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def compute_url_hash(url: str) -> str:
    """SHA256 hash of URL for dedup."""
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()