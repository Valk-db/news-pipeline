# AGENT_TASKS.md: news-pipeline task file (v9, updated 2026-09-22)

This file is the only task file in force. Ignore any older task files and anything you remember
from earlier rounds, including earlier reports that work was "complete" — a previous round's report
claimed a "Task 3" was done when it wasn't (see Verified state). Read this file fully before doing
anything.

Repo: Valk-db/news-pipeline. Python 3.12, uv, async SQLAlchemy, Supabase Postgres, GitHub Actions,
FastAPI curation UI on Vercel.

## Non-interactive shell rules (read first)
- Before anything else set these for your shell session.
  PowerShell: `$env:GIT_PAGER="cat"; $env:GIT_EDITOR="true"; $env:GIT_TERMINAL_PROMPT="0"`
  bash: `export GIT_PAGER=cat GIT_EDITOR=true GIT_TERMINAL_PROMPT=0`
- Use `git --no-pager` for log/diff/show/grep/stash show. Use `git merge --no-edit`. Commit only
  with `git commit -m "subject" -m "body"`. Never run `git commit` without -m, `git rebase -i`,
  `git add -p`, `less`, `vim`, `nano`, or anything that waits for keyboard input.
- Never use commands that block until finished (for example `gh run watch`). Use one-shot commands
  such as `gh pr checks` and report "pending" if it is not done.
- Make targeted edits only. Never rewrite a whole script in one tool call, never print whole files
  to the terminal, read only the function you are editing.
- If a command shows no output for about 2 minutes, cancel it and report which command. Do not
  retry the same command more than twice. If you are stuck, stop and say which step you are on and
  what you tried.

## Working agreement
1. Continue on the EXISTING branch `chore/cleanup-and-coverage` (already pushed, 3 commits ahead of
   main). Do not create a new branch for Task 3. Never push to main, never force-push, never merge a
   pull request yourself. main receives bot commits ("chore: heartbeat"), so always `git fetch
   origin` first and rebase-free (`git merge --no-edit origin/main` only if main has moved and you
   need it — check first, don't assume).
2. Modify ONLY the files this task names: `tests/test_curation_ui.py` (new) and, if genuinely
   required to make it work, `tests/conftest.py`. Do NOT touch `src/ingestion`, `src/verification`
   (grouping/gate/tiers/cleanup logic), GDELT, the heartbeat mechanism, or curation_ui's
   triage/business logic (approve/reject/edit/mark-posted) or its dark-theme CSS/templates from #9.
3. Each commit message = a short subject plus a body paragraph explaining WHY, describing only what
   the diff contains. Never mention work that is not in the diff.
4. Tests run with DATABASE_URL UNSET: `uv run pytest tests/ -q`. Never set DATABASE_URL to the
   Supabase URL. Never print it, and never repeat anything that looks like a connection string or
   API key in your report.
5. Nothing runs against the production database from your machine. Do not run
   `scripts/story_audit.py` (except `--help`), `scripts/init_db.py`, `cleanup_stale.py`,
   `enable_rls.py`, `seed_*.py`/`print_*_reference.py` writes, or `python -m src.ingestion.run`
   without `--dry-run`.
6. Do not weaken, delete or skip existing tests except where a task says so. Never commit `.env`.
7. EVIDENCE RULE, stricter than before: a step is "done" only when (a) its VERIFY commands were run
   and passed, AND (b) `git --no-pager diff --stat` after committing shows a change to the exact
   file(s) that step named. Before writing your final report, re-read this task file's Task 3
   section side by side with `git --no-pager diff main...HEAD --stat` and confirm every named
   deliverable (`tests/test_curation_ui.py` existing, containing the three cases below) is actually
   there. The previous round's report described "Task 3" work that didn't match any task in that
   round's file — don't let that happen again. Never write "complete" from memory.
8. GitHub Actions (Ubuntu) is the source of truth for tests, not this machine. If `gh` is not
   installed, write "CI status unknown, Tyler must check the Actions tab" — do not guess a pass/fail.
9. Do NOT claim a pull request exists unless you created it or confirmed one already exists for this
   branch with `gh pr view chore/cleanup-and-coverage` (or equivalent). A `github.com/.../pull/new/...`
   link is only the compare page.
10. Do the steps in order without asking me between steps, but stop at the CHECKPOINT.

## Verified state (checked independently from a fresh clone, 2026-09-22)
- main = `628ccd9` (includes #9 dark theme, #10 audit-round-2 fixes). `cache: 'uv'` is confirmed
  gone from all three workflow files — the v7 CI blocker is resolved on main.
- Branch `chore/cleanup-and-coverage` = `16aecbe`, 3 commits ahead of main:
  `d62765b` (docs: AGENT_TASKS.md v8), `3d229cd` (Task 1: remove dead curation_ui project files),
  `16aecbe` (Task 2: pin ruff ruleset + fix findings, including mypy overrides for
  feedparser/datasketch and `__init__.py` for curation_ui/ and scripts/).
- **Independently re-ran and confirmed**, fresh `uv sync --extra dev --extra pipeline` +
  `spacy download en_core_web_sm`: `uv run pytest tests/ -q` → 220 passed, 5 skipped, 0 failed.
  `uv run ruff check .` → 0 findings.
- **Task 1 and Task 2 are genuinely complete and correct.** In particular, the
  `curation_ui/main.py` fix removed only the dead `Story as StoryModel` alias and kept the real
  `Story` import that `approve_story`/`reject_story`/`edit_story`/`save_story`/`mark_posted`
  actually use — the non-obvious, behavior-preserving fix v8 asked for, not the naive autofix that
  would have broken those routes.
- **Task 3 from v8 (add `tests/test_curation_ui.py` smoke coverage) was NOT done**, despite a round
  report describing a "Task 3" as complete. There is no `tests/test_curation_ui.py` file on this
  branch or anywhere in the repo. What that report actually described (unused test imports/vars
  cleaned up, `__init__.py` added for mypy module resolution, mypy overrides added, spaCy model
  downloaded) is real, verified work — but it's part of Task 2's diff and R0 setup, not a separate
  completed task. Task 3 below is that same, still-outstanding work, reissued.
- **CI/PR status is unverified by me.** GitHub's REST API is rate-limiting unauthenticated requests
  from the sandbox I checked this in, and I don't have `gh` there either, so I could not confirm (a)
  that GitHub Actions is actually green for `16aecbe`, or (b) that a PR for this branch exists. If
  one exists, do not merge it — Task 3 isn't in it yet. Check the Actions tab and the PR yourself.
- Unchanged from v8, still true, still Tyler's calls to make: `/healthz` unauthenticated, whether to
  widen the ruff ruleset beyond `E4/E7/E9/F`, the Supabase password rotation reminder, and the
  off-limits list below.

## Task 3: add minimal test coverage for curation_ui (branch chore/cleanup-and-coverage, same branch)
The goal is a smoke-test safety net, not full coverage of the triage workflow — that's a bigger task
for another round. New file: `tests/test_curation_ui.py`.
3.1 A test that imports `curation_ui.main` and `curation_ui.health` and asserts the import succeeds.
    This alone would have caught the Story/StoryModel bug from Task 2 had it existed before the fix
    — write it so it would fail on that specific bug (e.g. also exercise the route that constructs
    `select(Story)` outside `_render_stories_grid`, such as by calling `approve_story`'s handler
    through a `TestClient` request, not just importing the module).
3.2 Reuse the existing `test_settings`/`db_engine`/`db_session` fixture pattern from
    `tests/conftest.py` (sqlite+aiosqlite in-memory). Because `get_settings()` and the module-level
    `_engine` in `src/shared/database.py` are cached (`lru_cache`, module globals), use
    `monkeypatch.setenv` plus `get_settings.cache_clear()` before constructing the `TestClient`, and
    add a comment explaining why that ordering matters — a future reader needs to know the fixture
    depends on cache-clearing order.
3.3 Cover, at minimum:
    - `GET /healthz` with `DATABASE_URL` unset returns `env_set.DATABASE_URL: false` and a verdict
      string, without raising.
    - `GET /` without HTTP Basic credentials returns 401 (via `require_auth`'s dependency), and with
      `CURATION_USER`/`CURATION_PASSWORD` unset it returns the "not configured" 401/403 detail
      rather than a 500.
    - `GET /` with valid credentials against an empty in-memory DB (no stories) returns 200.
3.4 Do not add tests for `approve_story`/`reject_story`/`edit_story`/`mark_posted` full flows this
    round — that needs LLM-client mocking that's out of scope here. Note that gap explicitly in your
    report as a follow-up.
3.5 VERIFY, paste all output: `uv run pytest tests/test_curation_ui.py -v` all pass. Full suite
    `uv run pytest tests/ -q` shows the new tests plus the existing 220 (so ~223+ passed, 5 skipped,
    0 failed — exact new count depends on how many you write). `uv run ruff check .` still 0
    findings on the new file.
3.6 Commit subject: `test(curation-ui): add smoke tests for health and auth boundary` — body
    explains the cache-clearing requirement and lists what is and isn't covered.
3.7 Push.

## CHECKPOINT: STOP
Push and report: `git --no-pager log --oneline -6`, full test counts, `uv run ruff check .` output,
files changed (`git --no-pager diff main...HEAD --stat`), and — per the stricter evidence rule above
— confirm explicitly that `tests/test_curation_ui.py` exists and covers the 3 cases in 3.3 before
calling this done. State the compare URL or PR URL if one exists; never claim a PR exists unless you
verified it. Tyler will review and merge himself.

## Decisions for Tyler (agent: do nothing about these)
- Confirm the Actions tab actually shows green runs for `ci.yml`, `daily-ingest.yml`, and
  `cleanup.yml`, and check whether PR #11 (or whatever number this branch's PR is) already exists
  before telling the agent to open a new one.
- `/healthz` is still unauthenticated and returns story-status counts and scrubbed exception text to
  anyone. Options: put it behind the same `CURATION_USER`/`CURATION_PASSWORD` basic auth (loses the
  ability for an external uptime monitor to hit it without credentials), or leave as-is since it
  never returns secrets. Not touched this round either way.
- Whether to expand the pinned ruff rule set beyond `E4/E7/E9/F` (e.g. `UP` for typing/datetime
  modernizations, `I` for import sorting, `BLE`/`S` for exception-handling hygiene) is a separate,
  bigger cleanup — deliberately left out of Task 2/3 so those stay small and reviewable.
- Rotate the Supabase database password if a pre-#10 pytest run with `-v` or `-rs` ever printed the
  skip message while `.env` pointed at Supabase (carried over from v7/v8 — unverifiable by me).
- Off-limits until Tyler instructs: GDELT changes, embeddings/pgvector, the heartbeat mechanism,
  gate logic, grouping code, the curation UI's triage/business logic or its dark theme, expiring or
  deleting orphan stories, editing production data, and v7's Task 3 (grouping-change proposal),
  which still requires Tyler to run `story_audit.py` against production and paste the output first.

## Test-compatibility notes
- `tests/test_story_audit.py` imports the script as `from story_audit import ...` after inserting
  the `scripts/` directory into `sys.path`; keep that working. `python -m scripts.story_audit` must
  keep working too.
- Existing tests elsewhere patch by name `src.ingestion.run.ingest_rss_feeds / ingest_gdelt /
  ingest_reddit` and `src.ingestion.{rss,gdelt,reddit}.extract_article`; do not touch those modules
  this round regardless.
- After `uv sync`, spaCy's `en_core_web_sm` may be missing locally:
  `uv run python -m spacy download en_core_web_sm`.

## Report format after the checkpoint
Files changed; tests run with pass/fail counts; the pasted VERIFY output; ruff output; CI result or
"unknown"; whether `tests/test_curation_ui.py` exists and what it covers (explicitly); anything
surprising; what you need from me.