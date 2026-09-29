# AGENT_TASKS v38

Adds to v37; it does NOT supersede v37 §2 (what-if sim, waits on "go 7") or §7 (claims/media counts). For every v37 item already done, say so and skip it.

Rust stays PAUSED. Python only, on `main`. Same working rules as v37: no branches, no push until every task here is done, run EXACTLY what CI runs (`uv run ruff check .`, `uv run mypy src/ curation_ui/`, `uv run pytest tests/`) and paste all three outputs, never print secrets, never delete/weaken/skip tests, no grouping/threshold/gate-logic changes without "go 7". If something fails or times out, say so.

Baseline measured on a fresh clone of 2e3c910 with no Postgres available: 415 passed / 11 failed / 7 skipped. 8 of those 11 failures (test_content_hash x5, test_gdelt x1, test_gdelt_toggle x2) also fail there without a real DB; report whether they pass in CI (`gh run list --workflow ci.yml --branch main --limit 5`).

## 0. Drop in 6 files (already tested, drop in as-is)

Files are in `news-pipeline-changes/` at their repo paths.

| File | What changed | Why |
|---|---|---|
| `src/verification/stories.py` | viewpoint prompt now includes each `unit_id`; robust JSON parse (fences/prose); labels normalized; no children when only 1 distinct label; `_get_recent_stories_with_entities` excludes viewpoint children | The old prompt never showed the model any unit id, so every unit fell back to "neutral" and each run created one child duplicating its parent. Children also copied the parent's entities, so new units could attach to a child instead of the parent. |
| `src/shared/llm_preflight.py` | probes Groq via `_chat_completion_groq` and Cerebras via `_chat_completion_cerebras`; unwraps tenacity `RetryError`; an unset optional key is "not configured", not "failed"; fails only if a configured provider is rejected or none works | Old probes both went through `chat_completion()` (Groq first, silent Cerebras fallback): a dead Groq key showed healthy when Cerebras answered, and the "cerebras" probe re-tested Groq. Also an unset Cerebras key failed every run despite README calling it optional. |
| `tests/test_llm_preflight.py` | rewritten (5 -> 10 tests) | the old 401 test encoded the bug: call #1 = Groq, call #2 = Cerebras through the same method |
| `tests/test_viewpoint_prompt_and_parsing.py` | new, 12 tests | old viewpoint tests mock the LLM keyed by real ids regardless of the prompt, so they could not catch the missing ids |
| `src/utils/minhash_utils.py` | `tokens_to_minhash` uses `update_batch` | ~8x faster MinHash build, hashvalues verified identical (numpy array_equal) |
| `.github/workflows/ci.yml` | `uv lock --check`, `uv sync --frozen`, concurrency cancel, timeouts | prod workflows install `--frozen`, so a stale `uv.lock` must fail CI, not prod |

Evidence to paste: ruff/mypy/pytest outputs; test count must be >= 450 collected (433 before).

## 1. approve_story ordering (`curation_ui/main.py`, `approve_story`)

Fixes the 3 failing tests in `tests/test_approve_save_story.py` (they get 503 instead of 409).

1. Delete the early `check_llm_available()` block near the top of `approve_story` (the 3 lines after the `check_database_available()` block).
2. Add that same 3-line block (`llm_ok, llm_msg = check_llm_available()` / `if not llm_ok:` / `return render_error_page(request, llm_msg)`) inside `async with get_session()`, immediately after the `existing_post` 409 check and before "Get source URLs and key facts for caption generation". Add a comment saying why: the state checks (404/409) must win over a missing-provider 503.
3. Do not touch `edit_story` here (see §5).
4. Verified locally: all 3 tests pass after this change, 24/24 in the curation UI test files.

## 2. `_is_transient_error` never matches real SDK errors (`src/shared/llm.py`)

Verified against groq 1.7.0: `RateLimitError`, `InternalServerError`, `APIConnectionError`, `APITimeoutError` are NOT httpx exceptions, and `LLMClient._is_transient_error` returns False for all four. So the tenacity `@retry` on both provider methods never fires for real failures (only the SDK's own built-in retries help), and `tests/test_llm_retry.py` only exercises httpx error types.

Change: treat as transient any exception with an int `status_code` that is >=500 or 429 (covers groq/cerebras `APIStatusError` subclasses), keep the existing httpx checks, and match SDK connection/timeout errors by class name in the MRO (`APIConnectionError`, `APITimeoutError`) so there is no hard SDK import. `AuthenticationError` (401) must stay non-transient.

Tests: parametrize with real SDK exceptions built like `groq.RateLimitError("x", response=httpx.Response(429, request=req), body=None)`. If real retries change timings in existing tests, patch the tenacity wait, do not loosen assertions.

## 3. Dead dependencies

- `slowapi` has zero imports anywhere (`grep -rn slowapi` hits only `pyproject.toml` and `requirements.txt`). Remove from `pyproject.toml` `dependencies` and from `requirements.txt`.
- `requirements.txt` still lists `scikit-learn==1.6.1` and `pgvector`; commit 2e3c910 says scikit-learn was removed from runtime, and it is only used by `scripts/` under the `analysis` extra. `VERCEL_DEPLOY.md` says the project uses uv, not requirements.txt. Find out whether anything consumes `requirements.txt` (Vercel config, workflows, docs). If nothing does, delete it and say so; if something does, sync it to `pyproject.toml`.
- Then `uv lock`, `uv lock --check`, `uv sync --frozen --extra dev --extra pipeline`, commit `uv.lock`.
- `SECURITY_AUDIT_OBSERVATIONS.md` Finding #2 still describes slowapi. Add a dated note that it was replaced by the in-memory failed-attempt limiter in `curation_ui/main.py`.

## 4. Failed-auth limiter trusts a client-controlled header (`curation_ui/main.py`, `_get_client_ip`)

It keys on the FIRST `X-Forwarded-For` entry. A client can rotate that value to dodge the 10/min limit unless the host overwrites the header (Vercel's edge does; the Tailscale and cloudflared setups in README §5 do not). Fix: use `x-vercel-forwarded-for` only when the `VERCEL` env var is set; otherwise use `request.client.host`. Add tests to `tests/test_auth_rate_limiter.py` (spoofed XFF must not reset the counter when not on Vercel).
Also note in the docstring that the dict is per-process, so on serverless each instance has its own counter. A Vercel WAF rate-limit rule is the real fix (already listed in the audit doc).

## 5. `GET /story/{id}/edit` calls the LLM on every page load

`edit_story` runs `generate_caption` each render. `RequestBudget` is per-process, so on Vercel the 900/day cap resets on every cold start and does not protect Groq's shared 1K RPD. BLOCKED on Tyler's answer (see below): render the page with an empty draft and add a "Draft caption" HTMX button, or persist the first draft.

## 6. Speed, behavior-preserving (existing tests must pass UNCHANGED; if a mock call-sequence test breaks, rewrite it onto the SQLite `db_session` fixture, do not delete it)

a. `src/ingestion/rss.py` `_bounded_process`: entries inside one feed are extracted one at a time (`await process_feed_entry` inside a per-feed `for`), so real concurrency = number of feeds, not `extract_sem` (15). Restructure: do the dedup/known/circuit checks for all entries under `seen_lock`, then `asyncio.gather` the extractions bounded by `extract_sem`. Keep circuit semantics (>=8 attempts and ok/attempts < 0.10 trips; later entries record `circuit_open`; entries already in flight when it trips may finish). `tests/test_circuit_breaker.py` must pass unchanged. Paste wall-clock duration from a before and an after `workflow_dispatch` run.
b. `rss.py` feed client: `httpx.AsyncClient(timeout=timeout)` sends the default python-httpx UA, while article fetches send the bot UA. Use the same UA for feed fetches. Benefit is unmeasured: report `feed_failed:http_403` counts before/after.
c. `src/verification/stories.py`: `create_story_from_units` commits once per story, `_update_story_entities` does one SELECT per unit, `_gather_unit_texts_for_story` runs one query per unit, `cluster_viewpoints` recomputes counters per story. Batch these (flush in loops, single commit at the end of `build_stories`, `IN` queries). Grouping results must be identical: snapshot stories/links before and after on the same fixture.
d. `src/verification/tiers.py` `apply_dynamic_gate`: the "fetch stories + units" block is copy-pasted three times (shadow path, tier-1 gate, dynamic path), and shadow mode runs `compute_admission_score` per story with several extra queries each. Extract one `_load_stories_and_units` helper and batch the virality/harm lookups. Gate output must be identical: add a test that compares statuses and `gate_reason` strings old vs new on one fixture set. Do NOT change any threshold or scoring.

## 7. mypy gates nothing

309 errors and CI has `continue-on-error: true`. Worst files: `schema/models.py` 61, `shared/llm.py` 37, `verification/tiers.py` 25, `enrichment/media_extractor.py` 22, `curation_ui/main.py` 21, `enrichment/event_locator.py` 19, `verification/stories.py` 17. Plan: add `[[tool.mypy.overrides]] ignore_errors = true` for every module that has errors today, then remove `continue-on-error` so new errors and new modules are enforced. Burn the override list down one module per round. Do not mass-add `# type: ignore`.

## 8. Small

- `src/ingestion/run.py` `log_status`: read `os.getenv("GITHUB_SHA")` first, fall back to the `.git/HEAD` read (it returns None whenever HEAD points at a ref stored in packed-refs).
- README and `AGENT_TASKS.md` say the daily workflow runs `spacy download`; the current workflows do not. Fix the docs after checking `ci.yml`, `daily-ingest.yml` and `weekly-enrichment.yml`.

## Blocked on Tyler (do not code these)

1. Gate counts ARTICLES, not units: `apply_tier1_gate` expands `tier1_owner_groups` per article, so ONE reporting unit containing BBC + Guardian copies passes "2 tier-1 owners" on its own, which contradicts README ("2 tier-1 reporting units"). Is syndicated-copy corroboration intended? (needs "go 7")
2. Viewpoint children are PENDING and show in the curation queue next to their parent. Keep as slices, or hide from the queue and use only for the claim matrix?
3. §5 choice for `edit_story`.
4. Should preflight fail the run when the primary (Groq) is rejected but the backup works, as it does today, or only warn?

## Final report

Per-task evidence, then a TYLER ACTIONS block containing only the decisions above.
