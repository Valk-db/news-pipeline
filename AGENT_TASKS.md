# AGENT_TASKS.md v15

Supersedes v14. v14's P0-C and P0-D are applied and verified (11/11 curation UI tests pass,
full suite 316 passed / 5 skipped). This revision adds P0-E: the `/globe` page failing to
initialize.

Status: **all three fixes (P0-C, P0-D, P0-E) are implemented in this checkout** (see diffs
below) and verified. This doc is the record of what was wrong and why, for the PR
description / commit message, and as a template if any of these classes of bug recur.

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

## P0-E — `/globe` page fails to initialize

### Symptom

Browser console, in order:

```
Uncaught ReferenceError: hideEventDetail is not defined
    at globe-core.js:520:5
globe-init.js:13 Initializing Globe...
globe-init.js:43 Globe initialization failed: TypeError: Cannot read properties of undefined (reading 'initGlobe')
    at HTMLDocument.<anonymous> (globe-init.js:17:32)
```

### Diagnosis

`curation_ui/static/globe/globe-core.js` ends with:

```js
window.GlobeCore = {
    ...
    showEventDetail,
    hideEventDetail,   // <- line 520
    ...
};
```

`hideEventDetail` was never defined in `globe-core.js`. A function with that exact name
exists, but only in `globe-interaction.js` (loaded via a separate `<script>` tag, after
`globe-core.js`, per `curation_ui/templates/globe.html`) — it's local to that file and only
exposed as `window.GlobeInteractions.hideEventDetail`, not anything `globe-core.js` can see.

Because these are classic (non-module) scripts, evaluating the `window.GlobeCore = {...}`
object literal throws a `ReferenceError` on the undefined shorthand property the instant
`globe-core.js` runs — before the assignment completes. So `window.GlobeCore` is never set
at all. That's the first console line. It cascades directly into the second and third:
`globe-init.js` calls `window.GlobeCore.initGlobe()` a few lines into its own
`DOMContentLoaded` handler, and since `window.GlobeCore` is `undefined`, that throws
"Cannot read properties of undefined (reading 'initGlobe')" and the whole globe never
initializes.

`globe-core.js` already fully owns open/close of `#event-detail-panel` — its own
`showEventDetail()` (used by its own `onLeftClick` handler) ends with
`panel.classList.add('open')`. It was just missing the matching close half; it was never
meant to depend on `globe-interaction.js`'s same-named function.

Verified: rebuilt `window.GlobeCore` in a minimal Node/Cesium-stub harness — it threw the
exact same `ReferenceError` on `hideEventDetail` before the fix, and after the fix built
cleanly with a working `initGlobe` function on it.

### Fix

`curation_ui/static/globe/globe-core.js` — add a local `hideEventDetail`, right after
`showEventDetail` (which already sets the panel's `open` class, so this mirrors it exactly):

```js
// Hide the event detail panel (mirrors showEventDetail above; this module
// owns #event-detail-panel, so it must not depend on globe-interaction.js's
// same-named function, which runs in a separate scope and loads after this
// script — referencing it here threw a ReferenceError while building the
// window.GlobeCore export object, which in turn left window.GlobeCore
// undefined and broke globe-init.js's window.GlobeCore.initGlobe() call).
function hideEventDetail() {
    const panel = document.getElementById('event-detail-panel');
    if (panel) panel.classList.remove('open');
}
```

Nothing else changes — the existing `hideEventDetail,` line in the `window.GlobeCore = {...}`
export object at the bottom of the file now resolves correctly, and
`globe-interaction.js`'s own separate `hideEventDetail` (exposed as
`window.GlobeInteractions.hideEventDetail`, bound to its own `#panel-close` click listener)
is untouched and still works exactly as before — the two are independent, same-named
functions in different scopes, and both doing the same harmless thing (removing the `open`
class) is not itself a bug.

### Verification

```
node --check curation_ui/static/globe/globe-core.js   # syntax OK
```

Plus a minimal Node harness stubbing `document`/`Cesium` and `require()`-ing the file directly:
before the fix, building `window.GlobeCore` throws `ReferenceError: hideEventDetail is not
defined`; after the fix, `window.GlobeCore` builds with all expected keys including a
function-typed `initGlobe` and `hideEventDetail`.

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
4. P0-E has no automated coverage — there's no JS test harness in this repo at all yet. If
   one gets added later, a smoke test that `require()`s `globe-core.js` against a stubbed
   `document`/`Cesium` and asserts `window.GlobeCore.initGlobe` is a function (the same check
   used to verify this fix) would have caught this before it shipped. Also worth a quick scan
   of `globe-layers.js` / `globe-timeline.js` / `globe-interaction.js` for the same
   pattern — a name referenced in one file's public export object but only ever defined in
   another — since this was the second time (after P0-D) that a bug shipped from two files
   silently assuming they shared more than they did.