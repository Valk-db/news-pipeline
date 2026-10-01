#!/usr/bin/env python3
"""Daily recurring GDELT ingest for the ProcMon DEV database.

Keeps the dev map fresh with REAL articles on a REAL schedule: every run
queries GDELT for recent events, extracts article text, and writes
idempotently to the dev Supabase (news-pipeline-dev). Re-runs skip URLs
already present via url_hash, so this is safe to run on a cron.

Two ingest paths, in order:
  1. DOC API (v2 doc/doc, artlist) with ADAPTIVE RECURSIVE TIME-SLICING:
     the API returns max 250 records per call with no pagination, so a full
     250 window is split in half and recursed until every slice is under 250.
     DOC articles carry no coordinates, so they feed raw_articles (curation
     UI) but not map events. Client-side relevance filter drops titles that
     do not contain the query terms.
  2. v1 GKG GeoJSON fallback (geocoded): the query loop mirrors
     src/ingestion/gdelt.py::fetch_gkg_geojson_articles (same endpoint,
     params, feature parsing, entities["GEO"] shape). GKG features carry
     lat/lon, so these become map events. This is what keeps the map fresh.

Article extraction follows ~/workspace/procmon-seed/seed_gdelt.py:
trafilatura primary, readability-lxml fallback, raw HTML retained, and the
same provenance fields in entities (extractor, extractor_version,
fetch_status, fetch_ts, scraper_version, canonical_url, feed_url, guid).

Worker pattern: GDELT queries run SEQUENTIALLY seconds apart (never parallel
bursts); article fetch+extract runs under asyncio.Semaphore(5) with
per-domain rate limits (>=1.5s between hits to the same domain), bounded
retries (4xx is never retried, it goes straight to the dead-letter file),
JSON checkpointing for resume, idempotent writes, and per-run cost
accounting (everything here is free; COST_GUARD aborts if a paid endpoint
ever gets configured).

Transport note: this VM's egress goes through an HTTP proxy. httpx is
unreliable here (it chokes parsing this sandbox's no_proxy entry and the
proxy truncates long bodies), so ALL HTTP in this script uses urllib, which
honors the proxy env vars. asyncio.to_thread keeps the event loop free.

Database: dev Supabase ONLY, via postgREST (raw Postgres TCP is blocked
from this VM). Credentials come from ~/.config/procmon/supabase-dev.env
(mode 600) and are never copied into code, logs, or chat. Production is
never touched.

Idempotency: url_hash uses the SAME scheme as the seed script
(sha256 of the seed-style canonical URL), so re-runs and overlapping
windows never duplicate rows already seeded.

Cost: $0. GDELT is free, Supabase dev tier is free, no LLM calls.

CRON INSTALL (runs daily 10:15 UTC == 06:15 EDT):
    15 10 * * * /home/hatch/.venvs/nptest/bin/python \\
        /home/hatch/workspace/repos/news-pipeline/scripts/ingest_gdelt_daily.py \\
        --hours-back 24 \\
        >> /home/hatch/workspace/procmon-dev/hidden_files/gdelt_ingest.log 2>&1

Do NOT run this on Vercel (hobby cron limits, ~10s timeouts); it belongs on
this VM's cron. See ingest_gdelt_daily.DEPLOY.md for the full setup note.

Usage:
    python scripts/ingest_gdelt_daily.py [--hours-back 24] [--limit 200]
        [--dry-run] [--fresh] [--gkg-only] [--doc-only]
        [--checkpoint PATH]
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Proxy env sanitization. httpx (used by src.ingestion imports below) cannot
# parse this sandbox's no_proxy entry ("Invalid port: ':1]'"), so normalize
# it before anything else imports httpx. urllib, which does the real work
# here, honors the proxy vars either way.
# ---------------------------------------------------------------------------
os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

# Reuse the fallback-path constants from the pipeline's GDELT module rather
# than redefining the endpoint/queries. (The query LOOP below is implemented
# with urllib because httpx is unreliable through this VM's egress proxy;
# the logic mirrors fetch_gkg_geojson_articles: same params, same feature
# parsing, same entities["GEO"] shape.)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.ingestion.gdelt import (  # noqa: E402
    GDELT_GKG_GEOJSON_API,
    GDELT_API,
    TIER1_DOMAINS,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_PATH = os.path.expanduser("~/.config/procmon/supabase-dev.env")
CHECKPOINT_DEFAULT = os.path.expanduser(
    "~/workspace/procmon-dev/hidden_files/gdelt_ingest_checkpoint.json"
)
DEADLETTER_DEFAULT = os.path.expanduser(
    "~/workspace/procmon-dev/hidden_files/gdelt_ingest_deadletters.jsonl"
)

# (event_type, gdelt topic query, relevance terms). Same proven set the seed
# used for map-freshness, plus the terms the DOC relevance filter requires
# in the title.
QUERIES = [
    ("conflict", "conflict", ["conflict", "war", "clash", "fighting"]),
    ("protest", "protest", ["protest", "demonstration", "rally", "march"]),
    ("disaster", "earthquake", ["earthquake", "quake", "tremor", "seismic"]),
    ("disaster", "flood", ["flood", "flooding", "inundation"]),
    ("election", "election", ["election", "vote", "ballot", "poll"]),
    ("political", "government", ["government", "minister", "parliament", "senate"]),
    ("economic", "economy", ["economy", "economic", "inflation", "market"]),
    ("health", "disease", ["disease", "outbreak", "virus", "epidemic"]),
    ("crime", "crime", ["crime", "criminal", "arrest", "police"]),
    ("sports", "sports", ["sports", "match", "tournament", "championship"]),
    ("accident", "accident", ["accident", "crash", "collision"]),
    ("environmental", "climate", ["climate", "environment", "pollution"]),
]

DOC_MAXRECORDS = 250          # GDELT DOC API hard cap per call, no pagination
DOC_PACE_SECONDS = 6.0        # GDELT asks for >=5s between DOC requests
GKG_PACE_SECONDS = 2.0        # politeness between GKG queries
DOC_MAX_DEPTH = 6             # recursion cap for adaptive time-slicing
DOC_MIN_WINDOW = timedelta(minutes=15)
DOC_MAX_THROTTLE_RETRIES = 3

EXTRACT_SEMAPHORE = 5         # bounded concurrency for article fetch+extract
PER_DOMAIN_GAP = 1.5          # seconds between hits to the same domain
FETCH_TIMEOUT = 30
FETCH_RETRIES = 3             # bounded; 4xx is NEVER retried
RAW_HTML_LIMIT = 400_000      # retained fetched HTML cap (same as seed)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
SCRAPER_VERSION = "ingest_gdelt_daily v1"

TRACKING_EXACT = {
    "fbclid", "gclid", "gclsrc", "dclid", "wbraid", "gbraid", "msclkid",
    "mc_cid", "mc_eid", "igshid", "_ga", "pk_campaign", "pk_kwd",
}
TRACKING_PREFIXES = ("utm_", "piwik_", "matomo_")
DEFAULT_PORTS = {"http": "80", "https": "443"}

# Cost guard: every endpoint this script touches must be free. If a paid
# endpoint is ever added to PAID_ENDPOINTS, the run aborts before spending.
PAID_ENDPOINTS = set()


# ---------------------------------------------------------------------------
# URL canonicalization + hashing: IDENTICAL to ~/workspace/procmon-seed/seed_gdelt.py
# so url_hash dedupe matches rows the seed already wrote. (Note: this differs
# slightly from src/utils/trafilatura_extract.canonicalize_url, which strips
# www and forces https; the seed's scheme is what the dev DB already uses.)
# ---------------------------------------------------------------------------

def is_tracking_param(name):
    low = name.lower()
    if low in TRACKING_EXACT:
        return True
    return any(low.startswith(p) for p in TRACKING_PREFIXES)


def canonicalize_url(u):
    if not u:
        return u
    try:
        p = urllib.parse.urlsplit(u.strip())
    except ValueError:
        return u
    scheme = (p.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return u
    host = (p.hostname or "").lower()
    if not host:
        return u
    netloc = host
    try:
        port = p.port
    except ValueError:
        port = None
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    pairs = [
        (k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if not is_tracking_param(k)
    ]
    query = urllib.parse.urlencode(sorted(pairs))
    path = p.path or "/"
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def url_hash_of(canonical):
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def domain_of(url):
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host[:255]


# ---------------------------------------------------------------------------
# HTTP helpers (urllib: honors the egress proxy; httpx does not work here)
# ---------------------------------------------------------------------------

def http_get_text(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def http_get_json(url, headers, timeout=40):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


class RestError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:600]}")


def http_post(url, payload, headers, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise RestError(e.code, e.read().decode("utf-8", errors="replace")) from e


def http_patch(url, payload, headers, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise RestError(e.code, e.read().decode("utf-8", errors="replace")) from e


def batches(rows, n=100):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ---------------------------------------------------------------------------
# Checkpointing (resume) + dead letters + cost accounting
# ---------------------------------------------------------------------------

class RunState:
    def __init__(self, path, fresh=False):
        self.path = path
        self.data = {
            "run_id": str(uuid.uuid4()),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_queries": [],
            "processed_url_hashes": [],
            "dead_letters": 0,
            "stats": {},
        }
        if not fresh and os.path.exists(path):
            try:
                with open(path) as f:
                    old = json.load(f)
                # Keep the old run's completed work so we resume, not redo.
                self.data["completed_queries"] = old.get("completed_queries", [])
                self.data["processed_url_hashes"] = old.get("processed_url_hashes", [])
                self.data["dead_letters"] = old.get("dead_letters", 0)
                print(f"resuming: {len(self.data['completed_queries'])} queries already done, "
                      f"{len(self.data['processed_url_hashes'])} urls processed")
            except Exception as e:
                print(f"checkpoint unreadable ({e}), starting fresh")

    def mark_query(self, key):
        if key not in self.data["completed_queries"]:
            self.data["completed_queries"].append(key)
            self.save()

    def mark_url(self, uh):
        if uh not in self.data["processed_url_hashes"]:
            self.data["processed_url_hashes"].append(uh)

    def save_candidates(self, items):
        """Persist phase-1 candidates (JSON-serializable) so a resume does
        not need to re-query GDELT."""
        serializable = []
        for it in items:
            row = dict(it)
            for k in ("published_at", "start"):
                v = row.get(k)
                if isinstance(v, datetime):
                    row[k] = v.isoformat()
            serializable.append(row)
        self.data["candidates"] = serializable
        self.save()

    def load_candidates(self):
        cands = self.data.get("candidates") or []
        items = []
        for row in cands:
            it = dict(row)
            for k in ("published_at", "start"):
                v = it.get(k)
                if isinstance(v, str):
                    try:
                        it[k] = datetime.fromisoformat(v)
                    except ValueError:
                        it[k] = None
            items.append(it)
        return items

    def save(self):
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)


def dead_letter(path, url, reason):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "url": url[:500], "reason": reason}
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


class CostLedger:
    """Per-run cost accounting. Everything here is free; the guard aborts
    if a paid endpoint ever gets configured."""
    def __init__(self):
        self.counts = {"gdelt_calls": 0, "article_fetches": 0,
                       "supabase_calls": 0}

    def check(self):
        if PAID_ENDPOINTS:
            raise SystemExit(
                f"COST GUARD: paid endpoints configured {PAID_ENDPOINTS}; "
                "refusing to run without Tyler's explicit approval.")

    def usd(self):
        return 0.0


# ---------------------------------------------------------------------------
# GDELT DOC API with adaptive recursive time-slicing (urllib transport)
# ---------------------------------------------------------------------------

class DocThrottled(Exception):
    pass


def _doc_params(query, start, end):
    return {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(DOC_MAXRECORDS),
        "sort": "datedesc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }


def doc_fetch_window(query, start, end):
    """One DOC API call for [start, end). Returns list of article dicts.
    Raises DocThrottled when GDELT rate-limits us."""
    q = urllib.parse.urlencode(_doc_params(query, start, end))
    url = f"{GDELT_API}?{q}"
    try:
        status, body = http_get_text(url, timeout=45)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise DocThrottled(f"HTTP 429 for {query}")
        raise
    text = body.decode("utf-8", errors="replace")
    if "Please limit requests" in text:
        raise DocThrottled(f"throttle text for {query}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise DocThrottled(f"non-JSON response for {query}: {text[:120]}")
    return data.get("articles") or []


def doc_relevant(article, terms):
    """Client-side relevance filter: title (or excerpt) must contain a query term."""
    hay = ((article.get("title") or "") + " " + (article.get("excerpt") or "")).lower()
    return any(t in hay for t in terms)


async def doc_sliced(state, ledger, query, terms, start, end, depth=0):
    """Adaptive recursive time-slicing: a full 250 window is split in half
    and recursed until every slice is under 250 (or we hit depth/window floors)."""
    key = f"doc:{query}:{start.isoformat()}:{end.isoformat()}"
    if key in state.data["completed_queries"]:
        return []

    articles = await asyncio.to_thread(doc_fetch_window, query, start, end)
    ledger.counts["gdelt_calls"] += 1

    if (len(articles) >= DOC_MAXRECORDS and depth < DOC_MAX_DEPTH
            and (end - start) > DOC_MIN_WINDOW):
        mid = start + (end - start) / 2
        await asyncio.sleep(DOC_PACE_SECONDS)
        left = await doc_sliced(state, ledger, query, terms, start, mid, depth + 1)
        await asyncio.sleep(DOC_PACE_SECONDS)
        right = await doc_sliced(state, ledger, query, terms, mid, end, depth + 1)
        state.mark_query(key)
        return left + right

    kept = [a for a in articles if a.get("url") and doc_relevant(a, terms)]
    state.mark_query(key)
    return kept


async def doc_sweep(state, ledger, hours_back, limit):
    """Sequential DOC sweep over topic queries (never parallel bursts).
    If one topic exhausts its throttle retries, GDELT is throttling this IP
    and the rest of the DOC sweep is skipped outright (the GKG fallback
    covers the run) rather than burning backoff sleeps per topic."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    found = []
    seen = set()
    for event_type, topic, terms in QUERIES:
        query = f"{topic} language:english"
        backoff = 10.0
        articles = None
        for attempt in range(DOC_MAX_THROTTLE_RETRIES + 1):
            try:
                await asyncio.sleep(DOC_PACE_SECONDS)
                articles = await doc_sliced(state, ledger, query, terms, start, now)
                break
            except DocThrottled as e:
                print(f"  [doc:{topic}] throttled ({e}), backoff {backoff:.0f}s "
                      f"attempt {attempt + 1}/{DOC_MAX_THROTTLE_RETRIES + 1}")
                if attempt >= DOC_MAX_THROTTLE_RETRIES:
                    articles = None
                    break
                await asyncio.sleep(backoff)
                backoff *= 3
        if articles is None:
            print(f"  [doc:{topic}] DOC throttled for this IP; "
                  f"skipping remaining DOC topics (GKG fallback covers the run)")
            break
        new = 0
        for a in articles:
            u = (a.get("url") or "").strip()
            if not u or u in seen:
                continue
            seen.add(u)
            a["_event_type"] = event_type
            a["_topic"] = topic
            found.append(a)
            new += 1
        print(f"  [doc:{topic:>13}] kept {new:>3} relevant articles")
        if limit and len(found) >= limit:
            break
    return found


# ---------------------------------------------------------------------------
# GKG GeoJSON sweep (fallback path logic, urllib transport).
# Mirrors src/ingestion/gdelt.py::fetch_gkg_geojson_articles: same endpoint,
# same params, same feature parsing, same entities["GEO"] shape.
# ---------------------------------------------------------------------------

def gkg_fetch_query(query, timespan_min, per_query_cap):
    q = urllib.parse.urlencode({
        "QUERY": query,
        "TIMESPAN": str(timespan_min),
        "OUTPUTFIELDS": "url,name,tone,lang",
    })
    url = f"{GDELT_GKG_GEOJSON_API}?{q}"
    last = None
    for attempt in range(3):
        try:
            _, body = http_get_text(url, timeout=45)
            data = json.loads(body.decode("utf-8", errors="replace"))
            feats = data.get("features") or []
            if feats or attempt == 2:
                return feats[:per_query_cap]
            time.sleep(3)  # GDELT sometimes answers valid-but-empty when loaded
        except Exception as e:
            last = e
            time.sleep(2.0 * (attempt + 1))
    print(f"  [gkg:{query}] failed after 3 attempts: {last}")
    return []


def parse_gkg_feature(f, event_type):
    """Same parsing as fetch_gkg_geojson_articles: coordinates + GEO entities."""
    props = f.get("properties") or {}
    coords = (f.get("geometry") or {}).get("coordinates") or []
    url = (props.get("url") or "").strip()
    if len(coords) != 2 or not url:
        return None
    lon, lat = coords
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    try:
        tone = float(props.get("urltone") or 0)
    except (TypeError, ValueError):
        tone = 0.0
    name = (props.get("name") or "Unknown location").strip() or "Unknown location"
    pub = props.get("urlpubtimedate") or ""
    try:
        start = datetime.fromisoformat(pub.replace("Z", "+00:00"))
    except ValueError:
        start = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return {
        "url": url,
        "lat": lat,
        "lon": lon,
        "location_name": name,
        "tone": tone,
        "lang": props.get("urllangcode"),
        "start": start,
        "_event_type": event_type,
        "_geo": True,
    }


async def gkg_sweep(state, ledger, hours_back, limit, per_query_cap=50):
    """Sequential GKG sweep (never parallel bursts). Returns geocoded items."""
    timespan_min = int(hours_back * 60)
    items = []
    seen = set()
    for event_type, topic, _terms in QUERIES:
        key = f"gkg:{topic}:{timespan_min}"
        if key in state.data["completed_queries"]:
            continue
        await asyncio.sleep(GKG_PACE_SECONDS)
        feats = await asyncio.to_thread(gkg_fetch_query, topic, timespan_min, per_query_cap)
        ledger.counts["gdelt_calls"] += 1
        new = 0
        for f in feats:
            it = parse_gkg_feature(f, event_type)
            if not it or it["url"] in seen:
                continue
            seen.add(it["url"])
            items.append(it)
            new += 1
        state.mark_query(key)
        print(f"  [gkg:{topic:>13}] api {len(feats):>3}, kept {new:>3}")
        if limit and len(items) >= limit:
            break
    return items

# ---------------------------------------------------------------------------
# Article fetch + extract worker (seed provenance pattern).
# Bounded: asyncio.Semaphore(5), per-domain gap, bounded retries.
# 4xx is NEVER retried: it goes straight to the dead-letter file.
# ---------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(fragment):
    txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h[1-6]>|</li>", "\n", fragment or "")
    txt = TAG_RE.sub("", txt)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'")):
        txt = txt.replace(a, b)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r" *\n *", "\n", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def fetch_html(url, timeout=FETCH_TIMEOUT):
    """Single polite GET. Returns (status_or_error_name, html_bytes).
    Response body is capped at 5MB so one huge page cannot eat memory."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(5_000_000)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(1_000_000)
        except Exception:
            return e.code, b""
    except Exception as e:
        return type(e).__name__, b""


def _retryable_fetch_error(status):
    """4xx (and only 4xx) is final. Everything else may be retried, bounded."""
    return not (isinstance(status, int) and 400 <= status < 500)


def extract_text(html):
    """trafilatura primary, readability fallback. Returns
    (text, extractor, extractor_version) or (None, None, None)."""
    try:
        import trafilatura
    except ImportError:
        trafilatura = None
    text = extractor = version = None
    if trafilatura is not None:
        try:
            cand = trafilatura.extract(html, include_comments=False, include_tables=False)
        except Exception:
            cand = None
        if cand and len(cand.strip()) >= 300:
            text, extractor = cand.strip(), "trafilatura"
            version = getattr(trafilatura, "__version__", None)
    if text is None:
        try:
            from readability import Document
            cand = html_to_text(Document(html).summary())
        except Exception:
            cand = None
        if cand and len(cand) >= 200:
            text, extractor, version = cand, "readability", "readability-lxml"
    return text, extractor, version


def extracted_title(html, fallback_title, url):
    """Prefer the article's own headline when plausible, else the feed title."""
    try:
        import trafilatura, json as _json
        md = trafilatura.extract(html, include_comments=False, include_tables=False,
                                 with_metadata=True, output_format="json")
        headline = (_json.loads(md).get("title") or "").strip() if md else ""
    except Exception:
        headline = ""
    dom = domain_of(url)
    if len(headline) > 15 and dom.lower() not in headline.lower():
        return headline[:2000]
    return (fallback_title or "")[:2000]


async def process_one(item, sem, domain_locks, domain_last, dead_path, ledger):
    """Fetch + extract one article under the semaphore and per-domain gap."""
    url = item["url"]
    async with sem:
        domain = domain_of(url)
        lock = domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            gap = PER_DOMAIN_GAP - (now - domain_last.get(domain, 0))
            if gap > 0:
                await asyncio.sleep(gap)
            status, raw = None, b""
            for attempt in range(FETCH_RETRIES):
                status, raw = await asyncio.to_thread(fetch_html, url)
                ledger.counts["article_fetches"] += 1
                if isinstance(status, int) and status == 200:
                    break
                if not _retryable_fetch_error(status):
                    break
                if attempt < FETCH_RETRIES - 1:
                    await asyncio.sleep(2.0 * (attempt + 1))
            domain_last[domain] = time.monotonic()

    ent = {
        "canonical_url": item["canonical"],
        "feed_url": item.get("feed_url") or ("gdelt-doc" if not item.get("_geo") else "gdelt-gkg"),
        "guid": url,
        "scraper_version": SCRAPER_VERSION,
        "fetch_status": status,
        "fetch_ts": datetime.now(timezone.utc).isoformat(),
    }
    if not isinstance(status, int) or status != 200:
        ent["extractor"] = "failed"
        dead_letter(dead_path, url, f"fetch {status}")
        return None, ent, "fetch-fail"

    html = raw.decode("utf-8", errors="replace")
    ent["raw_html"] = html[:RAW_HTML_LIMIT]
    text, extractor, version = await asyncio.to_thread(extract_text, html)
    if text is None:
        ent["extractor"] = "failed"
        dead_letter(dead_path, url, "no text extracted")
        return None, ent, "extract-fail"

    ent["extractor"] = extractor
    ent["extractor_version"] = version
    title = extracted_title(html, item.get("title") or "", url)
    return {
        "text": text,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "title": title or item.get("title") or "Untitled",
        "summary": (item.get("excerpt") or text[:500] or "")[:500] or None,
        "published_at": item.get("published_at"),
    }, ent, "ok"


async def extract_all(items, dead_path, ledger, limit=None):
    sem = asyncio.Semaphore(EXTRACT_SEMAPHORE)
    domain_locks, domain_last = {}, {}
    results = []
    todo = items if not limit else items[:limit]
    for coro in asyncio.as_completed(
            [process_one(it, sem, domain_locks, domain_last, dead_path, ledger)
             for it in todo]):
        content, ent, note = await coro
        results.append((content, ent, note))
    ok = sum(1 for c, _, _ in results if c)
    print(f"  extraction: {ok}/{len(results)} ok "
          f"({sum(1 for _,e,_ in results if e.get('extractor')=='trafilatura')} trafilatura, "
          f"{sum(1 for _,e,_ in results if e.get('extractor')=='readability')} readability)")
    return [(c, e) for c, e, _ in results if c]


# ---------------------------------------------------------------------------
# Supabase REST (dev only, postgREST over HTTPS)
# ---------------------------------------------------------------------------

class DevDB:
    def __init__(self, base, key, ledger, dry_run=False):
        self.base = base.rstrip("/")
        self.get_headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.rest_headers = {**self.get_headers,
                             "Content-Type": "application/json",
                             "Prefer": "return=minimal"}
        self.ledger = ledger
        self.dry_run = dry_run

    def get(self, path):
        self.ledger.counts["supabase_calls"] += 1
        return http_get_json(f"{self.base}/rest/v1/{path}", self.get_headers)

    def post(self, table, rows):
        self.ledger.counts["supabase_calls"] += 1
        if self.dry_run:
            print(f"    [dry-run] POST {table} x{len(rows)}")
            return
        for b in batches(rows, 50 if table == "raw_articles" else 100):
            http_post(f"{self.base}/rest/v1/{table}", b, self.rest_headers)

    def patch(self, table, filt, payload):
        self.ledger.counts["supabase_calls"] += 1
        if self.dry_run:
            print(f"    [dry-run] PATCH {table}?{filt}")
            return
        http_patch(f"{self.base}/rest/v1/{table}?{filt}", payload, self.rest_headers)

    def existing_hashes(self, hashes):
        have = set()
        for chunk in batches(list(hashes), 50):
            q = urllib.parse.urlencode(
                {"url_hash": f"in.({','.join(chunk)})", "select": "url_hash"})
            for r in self.get(f"raw_articles?{q}"):
                have.add(r["url_hash"])
        return have

    def default_layer(self):
        rows = self.get("event_layers?is_default=eq.true&select=id&limit=1")
        return rows[0]["id"] if rows else None

    def next_link_id(self):
        rows = self.get("story_unit_links?select=id&order=id.desc&limit=1")
        return (int(rows[0]["id"]) + 1) if rows else 1


def build_rows(item, content, entities, layer_id, tier):
    now = datetime.now(timezone.utc).isoformat()
    sid = str(uuid.uuid4())
    aid = str(uuid.uuid4())
    start = item.get("start") or item.get("published_at") or datetime.now(timezone.utc)
    if isinstance(start, str):
        try:
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            start = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    geo = item.get("_geo")
    loc = (item.get("location_name") or "").strip() or "unknown location"
    etype = (item.get("_event_type") or "other").upper()

    story = {
        "id": sid,
        "day": start.isoformat(),
        "primary_entities": {
            "ingestion": "gdelt-daily",
            "location": loc if geo else None,
            "url": item["url"],
            "lang": item.get("lang"),
            "geocoded": bool(geo),
        },
        "tier1_unit_count": 0, "tier2_unit_count": 0,
        "tier3_unit_count": 0, "tier4_unit_count": 0,
        "distinct_owners": 0,
        "status": "PENDING",
        "created_at": now, "updated_at": now,
    }
    event = None
    if geo:
        tone = item.get("tone") or 0
        event = {
            "id": str(uuid.uuid4()),
            "story_id": sid,
            "latitude": item["lat"],
            "longitude": item["lon"],
            "location_name": loc[:255],
            "location_type": "place",
            "start_time": start.isoformat(),
            "event_type": etype,
            "confidence": round(min(0.95, max(0.35, 0.5 + abs(tone) / 25.0)), 3),
            "source_count": 1,
            "tier1_source_count": 1 if tier == "TIER1" else 0,
            "entities": {"url": item["url"], "tone": tone,
                         "ingestion": "gdelt-daily",
                         "geo": {"lat": item["lat"], "lon": item["lon"], "name": loc}},
            "layer_id": layer_id,
            "created_at": now,
        }
    article = {
        "id": aid,
        "url": item["canonical"],
        "url_hash": item["url_hash"],
        "entities": entities,
        "title": content["title"][:2000],
        "summary": content["summary"],
        "body_text": content["text"],
        "content_hash": content["content_hash"],
        "source_domain": domain_of(item["url"]),
        "source_tier": tier,
        "published_at": start.isoformat(),
        "fetched_at": now,
        "reporting_unit_id": None,
    }
    unit = {
        "id": str(uuid.uuid4()),
        "day": day,
        "representative_article_id": aid,
        "article_count": 1,
        "source_tiers": {tier: 1},
        "owner_groups": {domain_of(item["url"]): 1},
        "tier1_owner_groups": {},
        "created_at": now,
    }
    return story, event, article, unit


def write_batch(db, built):
    stories = [s for s, _, _, _ in built]
    events = [e for _, e, _, _ in built if e]
    articles = [a for _, _, a, _ in built]
    units = [u for _, _, _, u in built]

    db.post("stories", stories)
    print(f"    stories: {len(stories)}")
    if events:
        db.post("events", events)
        print(f"    events: {len(events)}")
    db.post("raw_articles", articles)
    print(f"    raw_articles: {len(articles)}")
    db.post("reporting_units", units)
    print(f"    reporting_units: {len(units)}")

    link_id = db.next_link_id()
    links = []
    for (_, _, a, u), s in zip(built, stories):
        links.append({"id": link_id, "story_id": s["id"], "unit_id": u["id"]})
        link_id += 1
    db.post("story_unit_links", links)
    print(f"    story_unit_links: {len(links)}")

    for (_, _, a, u) in built:
        db.patch("raw_articles", f"id=eq.{a['id']}", {"reporting_unit_id": u["id"]})
    for s, _, a, _ in built:
        db.patch("stories", f"id=eq.{s['id']}",
                 {"tier3_unit_count": 1, "distinct_owners": 1,
                  "status": "PENDING",
                  "updated_at": datetime.now(timezone.utc).isoformat()})
    return len(stories), len(events), len(articles)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def normalize_items(raw_items, state):
    """Canonicalize, hash, drop dupes within the run. Returns item dicts."""
    items = []
    seen = set()
    for r in raw_items:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        canonical = canonicalize_url(url)
        uh = url_hash_of(canonical)
        if uh in seen or uh in state.data["processed_url_hashes"]:
            continue
        seen.add(uh)
        pub = None
        seendate = r.get("seendate", "")
        if seendate:
            try:
                pub = datetime.strptime(seendate, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        domain = domain_of(url)
        tier = "TIER1" if domain in TIER1_DOMAINS else "TIER3"
        items.append({
            "url": url, "canonical": canonical, "url_hash": uh,
            "title": (r.get("title") or "").strip(),
            "excerpt": r.get("excerpt") or "",
            "published_at": pub or r.get("start"),
            "start": r.get("start"), "lat": r.get("lat"), "lon": r.get("lon"),
            "location_name": r.get("location_name"), "tone": r.get("tone"),
            "lang": r.get("lang") or r.get("urllangcode"),
            "_event_type": r.get("_event_type", "other"),
            "_geo": r.get("_geo", False),
            "_tier": tier,
            "feed_url": "gdelt-gkg" if r.get("_geo") else "gdelt-doc",
        })
    return items


async def amain(args):
    ledger = CostLedger()
    ledger.check()  # aborts if a paid endpoint is ever configured
    t0 = time.time()

    env = load_env(ENV_PATH)
    base = env["SUPABASE_URL"]
    key = env["SUPABASE_SERVICE_ROLE_KEY"]
    # Sanity: this script must NEVER point at production. The dev project ref
    # is qzothzirwwpesafzlxtw; refuse anything else.
    if "qzothzirwwpesafzlxtw" not in base:
        raise SystemExit(f"REFUSING: SUPABASE_URL does not look like the dev project: {base[:40]}...")
    db = DevDB(base, key, ledger, dry_run=args.dry_run)
    state = RunState(args.checkpoint, fresh=args.fresh)

    print(f"gdelt daily ingest: hours_back={args.hours_back} dry_run={args.dry_run} "
          f"run={state.data['run_id'][:8]}")

    # ---- Phase 1: GDELT queries (strictly sequential) ----
    # Resume: if a previous run finished phase 1, reuse its candidates.
    raw = []
    items = []
    if not args.fresh:
        items = state.load_candidates()
        if items:
            print(f"phase 1: resumed {len(items)} candidates from checkpoint, "
                  "skipping GDELT queries")
    if not items:
        if not args.gkg_only:
            print("phase 1a: DOC API sweep (adaptive time-slicing, sequential)")
            try:
                raw.extend(await doc_sweep(state, ledger, args.hours_back, args.limit))
            except Exception as e:
                print(f"  DOC sweep failed ({e}); continuing with GKG fallback")
        if not args.doc_only:
            print("phase 1b: GKG GeoJSON sweep (geocoded, sequential)")
            raw.extend(await gkg_sweep(state, ledger, args.hours_back, args.limit))
        items = normalize_items(raw, state)
        print(f"phase 1: {len(raw)} raw candidates -> {len(items)} unique urls")
        state.save_candidates(items)

    # ---- Phase 2: idempotent pre-check against dev DB ----
    have = db.existing_hashes({i["url_hash"] for i in items}) if items else set()
    fresh_items = [i for i in items if i["url_hash"] not in have]
    print(f"phase 2: {len(have)} already in dev db, {len(fresh_items)} new")
    for i in items:
        if i["url_hash"] in have:
            state.mark_url(i["url_hash"])

    # ---- Phase 3: fetch + extract (bounded, per-domain paced) ----
    if args.limit:
        fresh_items = fresh_items[:args.limit]
    dead_before = _count_dead(args)
    extracted = await extract_all(fresh_items, args.deadletters, ledger) if fresh_items else []
    dead_this_run = _count_dead(args) - dead_before
    print(f"phase 3: {len(extracted)} articles with body text")

    # ---- Phase 4: write ----
    if extracted and not args.dry_run:
        layer_id = db.default_layer()
        print(f"phase 4: writing (default layer {str(layer_id)[:8] if layer_id else None})")
        # extract_all does not preserve input order; re-attach items by guid.
        built = []
        for (content, ent), item in _remap(fresh_items, extracted):
            story, event, article, unit = build_rows(
                item, content, ent, layer_id, item["_tier"])
            built.append((story, event, article, unit))
        n_s, n_e, n_a = write_batch(db, built)
        for s, e, a, u in built:
            state.mark_url(a["url_hash"])
        state.data["stats"] = {"stories": n_s, "events": n_e, "articles": n_a}
    elif args.dry_run:
        print(f"phase 4: dry-run, would write {len(extracted)} article chains")
        state.data["stats"] = {"dry_run_chains": len(extracted)}

    state.data["candidates"] = []  # run complete; next run re-queries fresh
    state.save()
    dt = time.time() - t0
    print(f"\ndone in {dt:.1f}s | cost ${ledger.usd():.2f} "
          f"(gdelt_calls={ledger.counts['gdelt_calls']} "
          f"article_fetches={ledger.counts['article_fetches']} "
          f"supabase_calls={ledger.counts['supabase_calls']}) "
          f"dead_letters_this_run={dead_this_run}")
    return 0


def _remap(items, extracted):
    """extract_all returns (content, entities) without the item; re-attach by
    matching on the canonical url stored in entities['guid']."""
    by_guid = {it["url"]: it for it in items}
    out = []
    for content, ent in extracted:
        guid = (ent.get("guid") or "")
        item = by_guid.get(guid)
        if item is None:
            continue
        out.append(((content, ent), item))
    return out


def _count_dead(args):
    # Dead letters append during the run; total file lines is the ledger.
    try:
        with open(args.deadletters) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def main():
    ap = argparse.ArgumentParser(description="Daily GDELT ingest -> dev Supabase")
    ap.add_argument("--hours-back", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0,
                    help="cap total new articles (0 = no cap); useful for testing")
    ap.add_argument("--dry-run", action="store_true",
                    help="query + extract but write nothing")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the checkpoint and start over")
    ap.add_argument("--gkg-only", action="store_true")
    ap.add_argument("--doc-only", action="store_true")
    ap.add_argument("--checkpoint", default=CHECKPOINT_DEFAULT)
    ap.add_argument("--deadletters", default=DEADLETTER_DEFAULT)
    args = ap.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
