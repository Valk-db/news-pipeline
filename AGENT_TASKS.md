# AGENT_TASKS.md v14

Supersedes v13 (all three P0/P0-A/P0-B items from v13 are merged in `553dfac`). Two new
bugs reported after that merge — a CI test regression and a recurrence of the "Internal
Server Error" on the curation main page, this time from a different root cause than v13's
P0-B (which was about missing tables; the tables exist now). Both are root-caused and fixed
below, with a runnable repro for each.

Status: **both fixes are implemented in this checkout** (see diffs below) and verified
locally. This doc is the record of what was wrong and why, for the PR description / commit
message, and as a template if either class of bug recurs.

---

## P0-C — CI `test` job failing: 4 tests in `test_curation_ui_p2.py` assert 503 == 200

### Symptom

```
FAILED tests/test_curation_ui_p2.py::test_approve_story_populates_media_urls - assert 503 == 200
FAILED tests/test_curation_ui_p2.py::test_approve_story_feeds_snippets_into_caption - assert 503 == 200
FAILED tests/test_curation_ui_p2.py::test_approve_story_no_media_when_none_exists - assert 503 == 200
FAILED tests/test_curation_ui_p2.py::test_approve_story_caps_media_urls_at_three - assert 503 == 200
```

### Diagnosis

All four tests hit `POST /story/{id}/approve`. That route (`curation_ui/main.py`) starts
with:

```python
llm_ok, llm_msg = check_llm_available()
if not llm_ok:
    return render_error_page(request, llm_msg)   # 503
```

and `check_llm_available()` was:

```python
def check_llm_available() -> tuple[bool, str]:
    if not settings.has_llm:
        return False, "No LLM configured. ..."
    return True, ""
```

`settings.has_llm` is `bool(groq_api_key) or bool(cerebras_api_key)` — purely env-var
based. Every one of the four tests' fixtures does:

```python
monkeypatch.delenv("GROQ_API_KEY", raising=False)
monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
...
llm_module._llm_client = mock_llm_client   # inject the mock directly
```

i.e. they deliberately unset both provider keys and instead inject an already-built mock
`LLMClient` straight into the `src.shared.llm._llm_client` singleton, which is what
`get_llm_client()` returns once set. That's the correct way to mock this dependency without
hitting a real provider — but `check_llm_available()` never looks at `_llm_client`, only at
the env vars, so it returns `False` regardless of the injected mock, and every request gets
short-circuited into `render_error_page(..., 503)` before the handler body (the code the
tests actually exist to test — media_urls population, snippet-to-caption wiring) ever runs.

Confirmed by running the suite locally: reverting the fix below reproduces exactly this
503/200 mismatch; nothing else in the diff between `92aec20` (P2 commit, which added this
LLM-availability gate) and HEAD touches this path.

### Fix

`curation_ui/main.py`, `check_llm_available()`:

```python
def check_llm_available() -> tuple[bool, str]:
    """Check if LLM is available, return (available, error_message).

    Availability is either a configured provider API key, or an already
    -initialized/injected client (e.g. the mock LLMClient tests set on
    src.shared.llm._llm_client). Gating on settings.has_llm alone made this
    return False even when a working client was already in place.
    """
    import src.shared.llm as llm_module
    if not settings.has_llm and llm_module._llm_client is None:
        return False, "No LLM configured. Set GROQ_API_KEY or CEREBRAS_API_KEY environment variable."
    return True, ""
```

This keeps the real-deployment behavior identical (no keys configured → still 503, friendly
message) while recognizing a client that's already present — whether that's a test's mock or
a real singleton some other code path already initialized.

### Verification

```
uv sync --extra dev --extra pipeline
uv run pytest tests/test_curation_ui_p2.py tests/test_curation_ui.py tests/test_curation_ui_templates.py -q
# 11 passed
```

---

## P0-D — Internal Server Error (500) on the curation main page (`GET /`)

### Symptom

Browser console: `Failed to load resource: the server responded with a status of 500 ()` on
load of the curation UI's main page.

### Diagnosis

This is **not** a recurrence of v13's P0-B (that was missing `media_assets` /
`snippets` / `source_reliability_snapshots` tables — those are created now by the
`20260924000300_phase2_phase3_missing_tables.sql` migration landed in `553dfac`). The tables
exist; the bug is a type mismatch in the query that reads them.

`_render_stories_grid()` in `curation_ui/main.py`, called unconditionally by `GET /`, does:

```python
story_ids = [str(s.id) for s in stories]
...
media_stmt = select(MediaAsset).where(MediaAsset.story_id.in_(story_ids))
...
snippet_stmt = select(Snippet).where(Snippet.story_id.in_(story_ids))
```

`Story.id`, `MediaAsset.story_id`, and `Snippet.story_id` are all
`UUID(as_uuid=True)` columns (`sqlalchemy.dialects.postgresql.UUID`). That column type's bind
processor expects real `uuid.UUID` instances — it calls `.hex` on each value it's given.
`story_ids` here is a list of **strings** (`str(s.id)`), so as soon as any story has
media or snippets, building the `IN (...)` clause blows up:

```
AttributeError: 'str' object has no attribute 'hex'
```

which SQLAlchemy wraps as a `StatementError`, uncaught by the route, turning into FastAPI's
500. This reproduces with any real story that has ≥1 `MediaAsset` or `Snippet` row — which
is exactly the data the P2 commit (`a763bc0`) started populating — and reproduces
identically against SQLite and Postgres, since both use the same `UUID(as_uuid=True)` bind
processor path. It only ever "worked" in CI/local runs before because the DB was seeded
without any stories that had media or snippets attached yet.

The `str(...)` conversion was only ever needed for using the ids as **dict keys** later in
the same function (`media_by_story`, `snippets_by_story`, keyed by `str(media.story_id)`) —
it was never meant for the query itself.

### Fix

`curation_ui/main.py`, `_render_stories_grid()`:

```python
story_data = []
# Keep as UUID objects for the IN-clause bind params (the UUID column type
# expects actual uuid.UUID instances, not strings); str() versions are used
# below only as dict keys for grouping.
story_ids = [s.id for s in stories]
```

(the rest of the function is unchanged — the `str(media.story_id)` / `str(snippet.story_id)`
dict-keying further down already worked fine and stays as-is).

### Verification

Reproduced and confirmed fixed with a standalone script seeding one `Story` with one
`MediaAsset` and one `Snippet`, then hitting `GET /` through `TestClient`:

- Before fix: `AttributeError: 'str' object has no attribute 'hex'` → 500.
- After fix: `200`.

The existing test suite doesn't catch this because none of its `GET /` tests seed a story
that also has `MediaAsset`/`Snippet` rows attached — worth adding as a regression test (see
Follow-up below).

---

## Follow-up (not blocking, worth doing next)

1. **Add a regression test** for P0-D: a `test_index_with_media_and_snippets` in
   `tests/test_curation_ui.py` (or `_p2.py`) that seeds a story with a `MediaAsset` and a
   `Snippet` and asserts `GET /` returns 200. This is the exact gap that let P0-D ship
   without CI catching it.
2. Audit for the same `str(uuid_obj)` → `.in_()` pattern elsewhere in the codebase
   (`grep -rn '\.in_(' src/ curation_ui/ scripts/` and check each list's element type against
   the column type) — this was the only occurrence found in `curation_ui/main.py`, but the
   pipeline/scripts side wasn't audited as part of this pass.
3. `tests/test_ner.py` (6 tests) fails in any environment where
   `python -m spacy download en_core_web_sm` hasn't been run — this is expected/pre-existing
   (CI's `test` job has a dedicated step for it) and unrelated to P0-C/P0-D; not fixed here,
   just noting it so it isn't mistaken for a regression from this change.