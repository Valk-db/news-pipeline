"""Article body extraction using trafilatura."""

import trafilatura
import hashlib
import asyncio
import httpx
import re
import string
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

try:
    from src.utils.ingest_stats import STATS
except ImportError:
    STATS = None


# Query parameter names dropped by scheme u1 as tracking. Names are matched
# case insensitively. Anything that is not obviously a campaign or click id stays.
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "wbraid", "gbraid", "msclkid",
    "mc_cid", "mc_eid", "igshid", "yclid", "twclid", "ttclid",
    "_ga", "_gl", "_gat",
    "ref", "ref_src", "ref_url", "referrer", "taid", "cmpid", "cmp",
    "ocid", "oc", "s_cid", "mkt_tok", "trk", "trkcampaign",
    "originalsubdomain", "share_id", "spm", "sc_channel", "sc_campaign",
    "fb_action_ids", "fb_action_types", "fb_source", "fb_ref",
    "guccounter", "guce_referrer", "guce_referrer_sig", "_hsci",
    "__twitter_impression",
})

# Query parameter name prefixes dropped by scheme u1 as tracking.
_TRACKING_PREFIXES = (
    "utm_", "at_", "piwik_", "matomo_", "mtm_", "pk_", "hsa_",
    "vero_", "oly_", "ns_", "_ga_",
)

# AMP query markers. The name is in this set and the value is one of these values.
_AMP_QUERY_NAMES = frozenset({"amp", "ampmode", "amp_js_v", "output"})
_AMP_QUERY_VALUES = frozenset({"", "1", "amp", "true"})

# Host labels that name a presentation of the same article and are stripped.
_STRIPPABLE_HOST_LABELS = frozenset({"www", "m", "mobile", "amp"})

# Two label public suffixes seen in news domains. Used only to keep a strip from
# producing one, so that a host such as amp.co.uk keeps its amp label instead of
# collapsing to co.uk. A registry domain that is itself a site, such as gov.uk or
# bbc.co.uk, is not in this set and is stripped normally.
_PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "co.jp", "or.jp", "ne.jp", "ac.jp",
    "go.jp", "co.za", "org.za", "com.br", "net.br", "org.br", "com.mx", "com.ar",
    "com.co", "com.pe", "com.ve", "com.uy", "com.ec", "com.co.uk",
    "co.in", "net.in", "org.in", "gov.in", "ac.in", "com.sg", "com.my",
    "com.ph", "com.vn", "co.th", "in.th", "go.th", "co.id", "co.kr", "or.kr",
    "com.hk", "com.tw", "com.cn", "net.cn", "org.cn", "gov.cn", "com.tr",
    "com.ua", "com.pl", "com.es", "com.pt", "com.gr", "com.il", "co.il",
    "com.sa", "com.eg", "com.ng", "com.pk", "com.kw", "com.qa", "com.bh",
    "co.ke", "or.ke", "co.tz", "com.ru", "net.ru", "org.ru",
})

# Hosts that carry their destination in a query parameter, plus the parameter
# names used for that across the known redirectors.
_REDIRECTOR_TARGET_PARAMS = ("url", "u", "q", "target", "redirect", "redirect_url",
                             "dest", "url_to")
_REDIRECTOR_HOST_SUFFIXES = ("feedburner.com", "feedproxy.google.com")

# Guards against a redirector chain that points at itself.
_MAX_UNWRAP_DEPTH = 3

_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_PERCENT_ESCAPE_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_ABSOLUTE_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)

# Module-level HTTP client for connection pooling
# Created lazily and lives for process lifetime (short-lived GitHub Actions jobs)
_http_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    """Get or create the shared HTTP client with connection pooling."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            headers={"User-Agent": "Mozilla/5.0 (compatible; news-pipeline/0.1; +https://github.com/Valk-db/news-pipeline)"},
            timeout=20,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
        )
    return _http_client


def _normalize_percent_encoding(text: str) -> str:
    """Decode escapes of unreserved characters, uppercase the rest."""
    def repl(match: re.Match) -> str:
        digits = match.group(1)
        char = chr(int(digits, 16))
        if char in _UNRESERVED:
            return char
        return "%" + digits.upper()

    return _PERCENT_ESCAPE_RE.sub(repl, text)


def _remove_dot_segments(path: str) -> str:
    """Resolve . and .. segments the way RFC 3986 section 5.2.4 specifies."""
    if "." not in path:
        return path
    remaining = path
    out: list = []
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith("./"):
            remaining = remaining[2:]
        elif remaining.startswith("/./"):
            remaining = "/" + remaining[3:]
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = "/" + remaining[4:]
            if out:
                out.pop()
        elif remaining == "/..":
            remaining = "/"
            if out:
                out.pop()
        elif remaining in (".", ".."):
            remaining = ""
        else:
            start = 1 if remaining.startswith("/") else 0
            index = remaining.find("/", start)
            if index == -1:
                segment, remaining = remaining, ""
            else:
                segment, remaining = remaining[:index], remaining[index:]
            out.append(segment)
    return "".join(out)


def _ascii_lower(text: str) -> str:
    """Lowercase ASCII letters only, leaving any other codepoint alone."""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)


def _to_punycode(host: str) -> str:
    """Convert a non ASCII host to IDNA punycode, label by label, best effort."""
    if host.isascii():
        return host
    labels = []
    for label in host.split("."):
        if label.isascii():
            labels.append(label)
            continue
        try:
            labels.append(label.encode("idna").decode("ascii"))
        except (UnicodeError, UnicodeDecodeError):
            return host
    return ".".join(labels)


def _strip_host_label(host: str) -> str:
    """Strip one leading www., m., mobile., or amp. label when that is safe."""
    labels = host.split(".")
    if len(labels) < 3 or labels[0] not in _STRIPPABLE_HOST_LABELS:
        return host
    rest = ".".join(labels[1:])
    # Never strip down to a bare public suffix, so that amp.co.uk keeps its amp
    # label and does not collapse to co.uk.
    if len(labels[1:]) < 2 or rest in _PUBLIC_SUFFIXES:
        return host
    return rest


def _normalize_host(parts) -> str | None:
    """Fold the netloc into the canonical host, or None when there is no host."""
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not hostname:
        return None
    host = _ascii_lower(_to_punycode(hostname))
    host = host.rstrip(".") or host
    while True:
        stripped = _strip_host_label(host)
        if stripped == host:
            break
        host = stripped
    # An amp label in a non final position, as in example.amp.com.
    labels = host.split(".")
    if len(labels) > 2 and labels[-2] in _STRIPPABLE_HOST_LABELS:
        labels.pop(-2)
        host = ".".join(labels)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and port not in (80, 443):
        host = f"{host}:{port}"
    return host


def _is_amp_query_pair(name: str, value: str) -> bool:
    return name.lower() in _AMP_QUERY_NAMES and value.lower() in _AMP_QUERY_VALUES


def _canonical_query(query: str) -> str:
    """Drop tracking and AMP pairs, lowercase names, sort, re encode."""
    pairs = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        lowered = name.lower()
        if lowered in _TRACKING_PARAMS:
            continue
        if any(lowered.startswith(prefix) for prefix in _TRACKING_PREFIXES):
            continue
        if _is_amp_query_pair(name, value):
            continue
        pairs.append((lowered, value))
    if not pairs:
        return ""
    return urlencode(sorted(pairs))


def _unwrap_redirector(parts, depth: int) -> str | None:
    """Return the destination URL a known redirector carries, or None."""
    host = _ascii_lower(parts.hostname or "")
    path = parts.path or ""
    is_google = host == "google.com" or host.endswith(".google.com")
    is_amp_cache = host == "ampproject.org" or host.endswith(".ampproject.org")
    # Google AMP cache, which serves both /c/s/host/path and /amp/s/host/path.
    if is_google or is_amp_cache:
        for prefix in ("/c/s/", "/amp/s/"):
            if path.startswith(prefix):
                origin = path[len(prefix):]
                return f"https://{origin}" if origin else None
    is_feed = any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _REDIRECTOR_HOST_SUFFIXES
    )
    if not is_google and not is_feed:
        return None
    # Google only redirects from /url, a feed host may carry the target anywhere.
    if is_google and not is_amp_cache and path.rstrip("/") != "/url":
        return None
    parsed = parse_qsl(parts.query, keep_blank_values=True)
    wanted = {p.lower() for p in _REDIRECTOR_TARGET_PARAMS}
    for name, value in parsed:
        if name.lower() in wanted and _ABSOLUTE_HTTP_RE.match(value.strip()):
            return value.strip()
    return None


def canonicalize_url_v1(url: str, _depth: int = 0) -> str:
    """
    Scheme u1, the versioned URL canonical form. A pure function of its input
    string, with no network, no database, and no clock. The numbered rules below
    are the whole specification, in the order they are applied.

    Scheme rules.
    1. Leading and trailing whitespace is trimmed from the input.
    2. A scheme of http or https, in any case, becomes https.
    3. A missing scheme with a host present, as in protocol relative //host/path,
       becomes https.
    4. Any other scheme, including mailto and ftp, returns the trimmed input
       unchanged, so canonicalization never invents an address.

    Redirector rules, applied before the host and path rules.
    5. A Google AMP cache URL on ampproject.org whose path begins /c/s/ is
       rewritten to the origin URL that follows, then canonicalized again.
    6. A redirect URL on a google.com host whose path is /url is rewritten to the
       absolute URL carried in its url, u, q, target, redirect, redirect_url, dest,
       or url_to parameter, then canonicalized again.
    7. A URL on a feedburner.com or feedproxy.google.com host carrying one of those
       same absolute URL parameters is rewritten the same way.
    8. Unwrapping happens at most three times, after which the URL is canonicalized
       as it stands.
    9. Opaque redirectors are not resolved, because resolving them needs a network
       fetch. Bitly style short paths, FeedBurner feed labels, and the base64
       article ids under news.google.com remain the URLs they are. The fetch layer
       follows the redirect and feeds the final URL back through this function.

    Host rules.
    10. Userinfo before an at sign is dropped, password and all.
    11. The host is lowercased using ASCII rules, so unicode case folding can never
        merge two distinct hosts.
    12. A non ASCII host is converted to IDNA punycode label by label, and is left
        lowercased and unchanged when any label fails to encode.
    13. A leading www., m., mobile., or amp. label is stripped, repeatedly.
    14. No leading label is stripped when what remains has fewer than two labels, or
        when what remains is exactly a two label public suffix such as co.uk or
        com.au, so a host such as amp.co.uk keeps its amp label.
    15. An amp label in any position other than the last is removed, which folds
        example.amp.com into example.com.
    16. A trailing root dot is dropped, so a fully qualified example.com. agrees
        with example.com.
    17. A port of 80 or 443 is dropped, and any other port is kept.
    18. A colon separated IPv6 literal keeps its brackets.
    19. An empty or unparseable host is a malformed input and returns the trimmed
        input unchanged.

    Path rules.
    19. Percent escapes of unreserved characters, the letters, the digits, and the
        marks minus, dot, underscore, and tilde, are decoded to that character.
    20. Every other percent escape is kept with its hexadecimal digits uppercased,
        so %2f and %2F agree while a reserved escape never turns into data.
    21. Dot segments are resolved per RFC 3986, so /a/b/../c becomes /a/c.
    22. Trailing slashes are removed, then an amp segment is removed when it is
        the last segment, in any case, or the first segment below the root. So
        /a/amp, /a/amp/, and /a all become /a, and /amp/news/story becomes
        /news/story.
    23. An amp segment in the middle of a path is kept, because there it is
        content rather than a presentation marker.
    24. Duplicate slashes inside the path are preserved, because collapsing them can
        merge two paths that a router treats differently.
    25. An empty path becomes a single slash.

    Query rules.
    25. The query is parsed into name and value pairs, keeping blank values.
    26. A pair whose name is one of these tracking names, compared case
        insensitively, is dropped: fbclid, gclid, gclsrc, dclid, wbraid, gbraid,
        msclkid, mc_cid, mc_eid, igshid, yclid, twclid, ttclid, _ga, _gl, _gat,
        ref, ref_src, ref_url, referrer, taid, cmpid, cmp, ocid, oc, s_cid,
        mkt_tok, trk, trkcampaign, originalsubdomain, share_id, spm, sc_channel,
        sc_campaign, fb_action_ids, fb_action_types, fb_source, fb_ref, guccounter,
        guce_referrer, guce_referrer_sig, _hsci, __twitter_impression.
    27. A pair whose name begins with one of these tracking prefixes, compared case
        insensitively, is dropped: utm_, at_, piwik_, matomo_, mtm_, pk_, hsa_,
        vero_, oly_, ns_, _ga_.
    28. A pair is dropped when it is an AMP marker, which is a name of amp, ampmode,
        amp_js_v, or output with a value of empty, 1, amp, or true.
    29. Names are lowercased, because a name is a key and keys behave case
        insensitively in practice, while values keep their case.
    30. Surviving pairs are sorted by name and then by value, so the same pairs in
        another order agree.
    31. The query is re encoded with plus for spaces, and is dropped when empty.

    Assembly rules.
    32. The fragment is dropped, because a fragment never reaches a server.
    33. The result is https, the folded host with any non default port, the folded
        path, and the folded query when there is one.

    Deliberately out of scope.
    34. A rel canonical link is a fetch layer concern. This function never fetches a
        page and never reads a link element.
    35. A javascript or data URL is returned trimmed and unchanged, since neither
        identifies a fetchable article.
    36. Non ASCII bytes in a path are left exactly as they arrived, because encoding
        them here would change paths that are already correct.

    Identity caveat that callers must know.
    37. Path case and query value case are preserved here, but compute_url_hash
        lowercases the canonical form before hashing, so /Story and /story hash
        alike under u1. That is the existing pipeline behavior, kept so the scheme
        stays a drop in replacement. A later scheme has to drop the global
        lowercasing before the hash can be case sensitive.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    scheme = _ascii_lower(parts.scheme)
    if scheme in ("http", "https"):
        pass
    elif scheme == "" and parts.netloc:
        pass
    else:
        return raw

    if _depth < _MAX_UNWRAP_DEPTH:
        destination = _unwrap_redirector(parts, _depth)
        if destination:
            return canonicalize_url_v1(destination, _depth + 1)

    host = _normalize_host(parts)
    if host is None:
        return raw

    path = _normalize_percent_encoding(parts.path)
    path = _remove_dot_segments(path)
    path = path.rstrip("/")
    segments = path.split("/")
    # A leading amp segment directly below the root, as in /amp/news/story.
    if len(segments) > 2 and _ascii_lower(segments[1]) == "amp":
        del segments[1]
    # A trailing amp segment, as in /news/story/amp.
    if len(segments) > 1 and _ascii_lower(segments[-1]) == "amp":
        segments.pop()
    path = "/".join(segments)
    path = path.rstrip("/") or "/"
    if not path.startswith("/"):
        path = "/" + path

    return urlunsplit(("https", host, path, _canonical_query(parts.query), ""))


def canonicalize_url(url: str) -> str:
    """Backwards compatible alias for canonicalize_url_v1, kept for existing callers."""
    return canonicalize_url_v1(url)


def _extract_article_sync(url: str, html: str | None = None, source_key: str | None = None) -> tuple[str | None, str | None]:
    """
    Synchronous article extraction (blocking).
    Internal function - use extract_article() for async version.
    """
    try:
        if html:
            downloaded = html
        else:
            client = _get_http_client()
            try:
                response = client.get(url)
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
            STATS.record(source_key, "fetch_failed:error_Exception")
        return None, None


async def extract_article(url: str, html: str | None = None, source_key: str | None = None) -> tuple[str | None, str | None]:
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
    """SHA256 hex of the u1 canonical form, lowercased, encoded UTF8.

    The exact byte sequence hashed is canonicalize_url_v1(url).lower() encoded as
    UTF8, and nothing else, no prefix and no separator. The lowercasing is the
    existing pipeline behavior and is pinned here on purpose: it makes the path
    and the query values case insensitive, which is wrong for the few sites that
    serve case sensitive paths, and it is the one known defect of scheme u1. It
    stays until a later scheme drops it, because changing it now would change
    every hash the signed Merkle log already refers to.
    """
    return hashlib.sha256(canonicalize_url_v1(url).lower().encode("utf-8")).hexdigest()