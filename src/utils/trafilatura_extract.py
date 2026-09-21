"""Article body extraction using trafilatura."""

import trafilatura
from typing import Optional, Tuple
import hashlib
import asyncio
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

try:
    from src.utils.ingest_stats import STATS
except ImportError:
    STATS = None


_TRACKING_PARAMS = {"fbclid", "gclid", "ocid", "cmpid", "ref", "taid", "mc_cid", "mc_eid"}
_TRACKING_PREFIXES = ("utm_", "at_")


def canonicalize_url(url: str) -> str:
    """
    Normalize URL for consistent hashing:
    - lowercase scheme and host
    - strip www.
    - remove tracking query parameters
    - remove fragment
    - normalize path (strip trailing slash)
    - path case preserved; compute_url_hash lowercases for case-insensitive matching
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def _extract_article_sync(url: str, html: Optional[str] = None, source_key: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Synchronous article extraction (blocking).
    Internal function - use extract_article() for async version.
    """
    try:
        if html:
            downloaded = html
        else:
            import httpx
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; news-pipeline/0.1; +https://github.com/Valk-db/news-pipeline)"
            }
            try:
                response = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
                response.raise_for_status()
                downloaded = response.text
            except httpx.HTTPStatusError as e:
                if source_key:
                    # STATS is available at module level
                    STATS.record(source_key, f"fetch_failed:http_{e.response.status_code}")
                return None, None
            except httpx.TimeoutException:
                if source_key:
                    # STATS is available at module level
                    STATS.record(source_key, "fetch_failed:timeout")
                return None, None
            except Exception as e:
                if source_key:
                    # STATS is available at module level
                    STATS.record(source_key, f"fetch_failed:error_{type(e).__name__}")
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
            if source_key:
                # STATS is available at module level
                STATS.record(source_key, "fetch_failed:empty_extract")
            return None, None

        import json
        data = json.loads(result)
        body = data.get("text", "").strip()
        title = data.get("title", "").strip()

        return body if body else None, title if title else None

    except Exception:
        if source_key:
            # STATS is available at module level
            STATS.record(source_key, f"fetch_failed:error_Exception")
        return None, None


async def extract_article(url: str, html: Optional[str] = None, source_key: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract article body text and title from URL or HTML (async).
    Runs blocking trafilatura call in a thread pool to avoid blocking event loop.
    Returns (body_text, title) or (None, None) on failure.
    """
    return await asyncio.to_thread(_extract_article_sync, url, html, source_key)


def compute_content_hash(text: str) -> str:
    """SHA256 hash of normalized text for exact dedup."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def compute_url_hash(url: str) -> str:
    """SHA256 hash of canonicalized URL for dedup."""
    return hashlib.sha256(canonicalize_url(url).lower().encode()).hexdigest()