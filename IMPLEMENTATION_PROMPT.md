# Implementation Prompt: Cross-Run Story Grouping & Tier-1 Gate Fixes

## Context
The current pipeline only groups articles into stories within a single ingestion run. If outlet A publishes at 06:00 and outlet B at 18:00, they become separate stories — the tier-1 gate (requires ≥2 tier-1 units from ≥2 distinct owners) never passes because both units are never in the same story simultaneously.

Also: `apply_tier1_gate` only checks `PENDING` stories, so a `BLOCKED` story that later gets a second unit is never re-evaluated.

## Required Changes

### 1. Extract Pure Gate Decision Function
**File:** `src/verification/tiers.py`

Add a pure, testable function:
```python
def evaluate_tier1_gate(units: List[ReportingUnit]) -> tuple[bool, str]:
    """
    Returns (should_queue, reason).
    
    Gate passes iff:
    - At least 2 units with source_tier == TIER1
    - Those units have ≥2 distinct ownership groups
    """
    tier1_units = [u for u in units if u.source_tier == SourceTier.TIER1]
    if len(tier1_units) < 2:
        return False, f"Only {len(tier1_units)} tier-1 units (need ≥2)"
    
    owners = {get_owner_group(u.source_domain) for u in tier1_units}
    if len(owners) < 2:
        return False, f"Tier-1 units from only {len(owners)} owner(s) (need ≥2 distinct)"
    
    return True, "Gate passed"
```

Add unit tests in `tests/test_tiers.py` covering all branches.

### 2. Modify `build_stories` to Attach to Existing Stories
**File:** `src/verification/stories.py`

Current logic: only creates new stories from unlinked units.

New logic:
1. Get all `PENDING` and `BLOCKED` stories from last 48h
2. For each new unit, compute entity Jaccard against each story's aggregate entity set
3. If best match ≥ threshold (0.4), attach unit to that story (create `StoryUnitLink`)
4. Track which stories received new units
5. After all units processed, return list of modified story IDs

```python
async def build_stories(session: AsyncSession) -> List[uuid.UUID]:
    """
    Returns list of story IDs that were created or modified.
    """
    # Get recent stories (PENDING + BLOCKED) with their aggregate entities
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent_stories = await _get_recent_stories_with_entities(session, cutoff)
    
    # Get unlinked units
    units = await _get_unlinked_units(session)
    
    modified_story_ids = set()
    
    for unit in units:
        best_story_id = None
        best_jaccard = 0.0
        
        for story_id, story_entities in recent_stories:
            jaccard = entity_set_jaccard(unit.primary_entities, story_entities)
            if jaccard > best_jaccard and jaccard >= 0.4:
                best_jaccard = jaccard
                best_story_id = story_id
        
        if best_story_id:
            # Attach to existing story
            await _attach_unit_to_story(session, unit.id, best_story_id)
            modified_story_ids.add(best_story_id)
        else:
            # Create new story (existing logic)
            story_id = await _create_story_from_unit(session, unit)
            modified_story_ids.add(story_id)
    
    await session.commit()
    return list(modified_story_ids)
```

### 3. Modify `apply_tier1_gate` to Re-evaluate Modified Stories
**File:** `src/verification/tiers.py`

Change signature to accept optional story IDs:
```python
async def apply_tier1_gate(
    session: AsyncSession,
    story_ids: Optional[List[uuid.UUID]] = None
) -> dict:
    """
    If story_ids provided, only evaluate those stories.
    Otherwise evaluate all PENDING + BLOCKED stories.
    """
    if story_ids:
        # Also include BLOCKED stories that got new units
        stmt = select(Story).where(Story.id.in_(story_ids))
    else:
        stmt = select(Story).where(
            Story.status.in_([Story.Status.PENDING, Story.Status.BLOCKED])
        )
    
    # For each story, load its units and call evaluate_tier1_gate
    # Update status to QUEUED if passes, BLOCKED if fails
```

### 4. Wire It Together in `run.py`
**File:** `src/ingestion/run.py`

After `build_reporting_units`, call `build_stories` and pass modified IDs to `apply_tier1_gate`:
```python
unit_count = await build_reporting_units(session)
modified_story_ids = await build_stories(session)
gate_result = await apply_tier1_gate(session, story_ids=modified_story_ids)
```

### 5. Add PostgreSQL Service Container for CI
**File:** `.github/workflows/ci.yml` (new file)

```yaml
name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: test_news
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --extra dev
      - run: uv run pytest tests/ -v
        env:
          DATABASE_URL: postgresql+asyncpg://postgres:postgres@localhost:5432/test_news
```

### 6. Enable DB Integration Tests
**File:** `tests/test_verification.py`

Remove `@pytest.mark.skip` and update fixtures to use the CI Postgres:
```python
@pytest.fixture
async def db_engine():
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    # ... rest unchanged
```

## Acceptance Criteria
- [ ] Pure `evaluate_tier1_gate` function with 100% branch coverage
- [ ] `build_stories` attaches units to existing stories within 48h window
- [ ] `apply_tier1_gate` re-evaluates `BLOCKED` stories that received new units
- [ ] `run.py` wires the three steps in correct order
- [ ] CI runs full test suite (including DB tests) on every push/PR
- [ ] Manual test: simulate two runs 12h apart with tier-1 articles from AP and Reuters → both end up in same `QUEUED` story

## Files to Modify
- `src/verification/tiers.py` — add `evaluate_tier1_gate`, modify `apply_tier1_gate`
- `src/verification/stories.py` — rewrite `build_stories` with attachment logic
- `src/ingestion/run.py` — wire modified story IDs to gate
- `tests/test_tiers.py` — add gate function tests
- `tests/test_verification.py` — enable integration tests
- `.github/workflows/ci.yml` — new CI workflow with pgvector service

## Out of Scope
- `.env.production` removal (separate task)
- Workflow bug fixes (separate task)
- Caption validation improvements (separate task)