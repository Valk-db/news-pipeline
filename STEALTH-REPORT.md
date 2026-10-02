# STEALTH REPORT — GDELT DOC throttle detection + caching

Branch `procmon/batch-gdelt-throttle` (from `procmon/phase-0-foundations` @ 85c528b).
Commit `e6e4704`. Not pushed, not merged. Worktree clean. Adapter interface untouched.

## What changed

- `src/ingestion/gdelt.py` — `is_throttle_response()` matches the apology text
  (case-insensitive) before any JSON parse; bodies starting with `{` are never
  scanned, so an article headline about rate limits cannot trip it.
  `fetch_with_retry()` backs off on 429 **or** body throttle, with the long base
  (60s → 120s + jitter) for body throttles, no sleep after the final attempt, and
  it stops retrying the instant the breaker opens.
  `ThrottleCircuitBreaker` — 3 consecutive throttles ⇒ 15 min cold, one success
  clears the streak. `DocResponseCache` — 15 min TTL keyed on normalized
  domain + hours_back + max_records, successes only.
  `GlobalRateLimiter` — one lock spacing every DOC request.
  Response parsing moved to `_articles_from_doc_response()` so the throttle
  check and the cache around it stay readable.
- `tests/test_gdelt_throttle.py` — new, 35 tests.
- `tests/conftest.py` — autouse fixture resets the three process-global DOC
  singletons (without it one test's cache entry answered the next test).

## The limiter was not global (requirement 3)

It was a `sleep()` after each domain's successful call: skipped on every early
return (empty response, non-JSON, 429, exception) and never applied to retries,
so retries hammered unspaced. `throttle_seconds` was also dead — the function
read `settings.gdelt_throttle_seconds` instead, and its default (5.0) would have
overridden the 30s prod value for every caller that omitted it. Now one
`GlobalRateLimiter` gates every attempt; the parameter is `None` = use settings.

## Live evidence

**The real API, 2026-10-02** (`curl` through the egress proxy):
`HTTP/1.1 429 Too Many Requests`, no `content-type`, body
`"Please limit requests to one every 5 seconds or contact ..."`. That byte string
from GDELT itself is fed to `is_throttle_response()` → `True`, and through
`fetch_with_retry` → `GDELT throttling in the response body (HTTP 429)`.
So GDELT sends the apology with a 429 *and* (historically) with a 200; matching
the body covers both. The shared egress IP is throttled right now (45s idle did
not clear it), so a clean live 200 JSON fetch was not obtainable — the local
server below covers the success path.

**Real httpx over real TCP** against a local server reproducing that exact shape
(`200`/`429` + `text/plain` apology), sleeps recorded not served:

| Scenario | Result |
|---|---|
| A. 3 body throttles then JSON | detected; 3 requests; waits `[60.7s, 121.6s]`; breaker `open (15 min left)` |
| B. 429 without the marker | waits `[10.2s, 20.2s]` — short base preserved |
| C. breaker | closed after 2, open after 3; `fetch_gdelt_articles` → `ok=False error='circuit_open'` with **0 HTTP requests** |
| D. cache | two identical calls → `doc/extract=(1,1)` for both; `max_records=99` → `(2,2)`; after TTL → `(3,3)` |
| E. global spacing | 3 different domains, 1.0s limit → request gaps `[1.0, 1.0]`, total 2.32s |

## Tests

- New: `tests/test_gdelt_throttle.py` **35 passed** (detection, long vs short
  backoff, no-sleep-after-final, breaker-stop-mid-fetch, limiter serialization,
  cache hit/miss/expiry/no-failure-caching, health classification).
- Full suite: **1164 passed, 8 failed, 18 skipped**. The 8 failures are the
  pre-existing DB-dependent ones (`RuntimeError: Database not configured`,
  5 in test_content_hash, 2 in test_gdelt_toggle, 1 in test_gdelt); identical
  before my change on the same 6 files (baseline 91 passed / 8 failed).
- `ruff check` clean on all three changed files.
- Ran with `NO_PROXY` unset (the VM's literal `[::1]` in `NO_PROXY` kills
  `httpx.AsyncClient`; known cause of pre-existing failures).

## Decisions / notes for review

1. **A 429 that carries the apology takes the long backoff** (GDELT sends both
   signals together, as the live capture shows). A bare 429 keeps the 10s base.
2. **The breaker skips DOC calls only**, not the v1 GKG GeoJSON sweep — it is a
   different endpoint. If a DOC block should also stop the sweep, say so.
3. **Cache holds `RawArticle` objects for 15 min.** Same process, same objects
   returned; `ingest_gdelt` dedupes by `url_hash` and the DB dedupes the rest.
4. `is_open()` self-clears an expired cooldown, so no timer thread and no
   cross-test leakage.
5. Pre-existing bug left alone (out of scope, would change query semantics):
   `hours_back` is accepted by `fetch_gdelt_articles` but never sent as a
   `timespan` param to the DOC API. It is part of the cache key regardless.