"""URL-scheme allowlist shared by ingestion and rendering.

Jinja autoescaping does not validate URL schemes: `{{ article.url }}` with a
`javascript:` URL renders a working script-execution link, and the URLs come
from scraped feeds stored verbatim. This module is the single choke point:

    is_safe_url(url)  -> bool   strict check, http/https only
    safe_url(url)     -> str    the URL when safe, "#" otherwise (for href/src)

Both ingestion (src/ingestion/rss_evidence.py refuses to persist unsafe URLs)
and every template that puts a stored URL into href/src (via the `is_safe_url`
Jinja filter registered in curation_ui/app_state.py) go through here, so the
rule cannot drift between the two.

Parsing notes: browsers strip leading/trailing C0 controls and spaces, and
remove tab/CR/LF characters anywhere, before parsing a URL. The check mirrors
that so `  javascript:...`, `java\\tscript:...`, and `JAVASCRIPT:...` are all
rejected. Character-entity tricks (`&#106;avascript:`) are harmless here
because template autoescaping turns the `&` into `&amp;` before the browser
sees it.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_SAFE_SCHEMES = frozenset({"http", "https"})

# WHATWG URL parsing removes ASCII tab and newline characters anywhere in the
# input, and strips leading/trailing C0 controls and spaces, before parsing.
_TAB_NEWLINE_RE = re.compile(r"[\t\n\r]")


def _browser_cleaned(url: str) -> str:
    cleaned = _TAB_NEWLINE_RE.sub("", url)
    start = 0
    end = len(cleaned)
    while start < end and (ord(cleaned[start]) <= 0x20):
        start += 1
    while end > start and (ord(cleaned[end - 1]) <= 0x20):
        end -= 1
    return cleaned[start:end]


def is_safe_url(url: object) -> bool:
    """True only for absolute http/https URLs.

    Non-strings, empty strings, relative URLs, and every other scheme are
    unsafe: the only URLs this project stores are feed article/asset links,
    which are absolute http(s).
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        scheme = urlsplit(_browser_cleaned(url)).scheme.lower()
    except ValueError:
        return False
    return scheme in _SAFE_SCHEMES


def safe_url(url: object) -> str:
    """Render-safe form of a stored URL: the URL itself when safe, else "#".

    Used as the `is_safe_url` Jinja filter. Returning "#" (not the empty
    string) keeps the anchor focusable and avoids a confusing dead control;
    the link text still shows where the link claimed to go.
    """
    return url if is_safe_url(url) else "#"
