# AGENT_TASKS.md: news-pipeline task file (v8, updated 2026-09-22)

This file is the only task file in force. Ignore any older task files and anything you remember
from earlier rounds, including earlier reports that work was "complete". Read it fully before
doing anything.

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
1. Work on a NEW branch created from the latest `origin/main` (names are given per task). Never
   push to main, never force-push, never merge a pull request yourself. main receives bot commits
   ("chore: heartbeat"), so always `git fetch origin` first.
2. Modify ONLY the files a task names. No features, endpoints, settings, DB schema changes or
   dependency changes beyond what a task explicitly says. Do NOT touch `src/ingestion`,
   `src/verification` (grouping/gate/tiers/cleanup logic), GDELT, or the heartbeat mechanism this
   round. `curation_ui/` IS in scope this round, but only for the two things Task 2 and Task 3 name
   — do not touch triage/business logic (approve/reject/edit/mark-posted) or the dark-theme CSS/
   templates merged in #9.
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
7. Before using `ruff --fix` or `--unsafe-fixes` anywhere, read Task 2's warning about
   `curation_ui/main.py` in full. A blind autofix in that file will delete a name that is still in
   use and break the app at runtime — this is not hypothetical, see Verified state below.
8. Do NOT claim a pull request exists unless you created it. A `github.com/.../pull/new/...` link is
   only the compare page.
9. EVIDENCE RULE. A step is "done" only when its VERIFY commands were run and passed. In every
   report paste the last lines of each VERIFY output. If a VERIFY fails, say so and stop. Never
   write "complete" from memory. GitHub Actions (Ubuntu) is the source of truth for tests: passing
   on this machine does not prove CI is green. If `gh` is not installed, write "CI status unknown,
   Tyler must check the Actions tab".
10. Do the steps in order without asking me between steps, but stop at every CHECKPOINT.

## Verified state (main = 628ccd9, checked from a fresh clone on Linux, 2026-09-22)
- **CI is fixed.** All three workflows (`ci.yml`, `daily-ingest.yml`, `cleanup.yml`) now use
  `astral-sh/setup-uv@v4` with `enable-cache: true` and no longer pass `cache: 'uv'` to
  `actions/setup-python@v5`. The v7 blocker is resolved. I cannot confirm the scheduled Actions runs
  are actually green (no `gh` CLI, unauthenticated GitHub API is rate-limited from this sandbox) —
  Tyler must check the Actions tab.
- **Tests: 220 passed, 5 skipped, 0 failed** (`uv run pytest tests/ -q`, DATABASE_URL unset, fresh
  `uv sync --extra dev --extra pipeline` + `spacy download en_core_web_sm`). The v7 flaky
  `test_outside_48h_excluded` failure is gone.
- **`scripts/story_audit.py` rewrite (v7 Task 2, steps C/D/E) is complete.** All 7 functions exist
  (`bucket_for`, `compute_story_metrics`, `summarize_story_metrics`, `format_crosstab`,
  `format_per_day`, `build_report`, `fetch_audit_data`), no "similar to above" placeholder remains,
  `--help` exits 0 both as a script and as `-m scripts.story_audit`. I have not run it against
  production (correctly off-limits) so I can't confirm the numbers it prints are right — that's
  Tyler's job per v7's Task 3 gate, which is still untouched (correctly: no grouping/gate code has
  changed).
- **The DB-password-in-skip-message leak from v7 is fixed** — `tests/test_verification.py` now
  shows only the parsed hostname, per the `#10` commit body.
- **New finding — two dead, misleading config files.** `curation_ui/pyproject.toml` and
  `curation_ui/uv.lock` duplicate the root project's dependencies (and disagree with it:
  `requires-python = ">=3.11"` there vs `"==3.12.*"` at the root). Nothing installs from them:
  `pyproject.toml`'s own comment says "Vercel installs ONLY this list" referring to the root
  `[project.dependencies]`, `VERCEL_DEPLOY.md` confirms deps live in the root `pyproject.toml`, and
  no workflow or doc `cd`s into `curation_ui/` or references its pyproject. They are pure clutter
  that could mislead someone into editing the wrong file when adding a dependency. Safe to delete.
- **New finding — a real bug hiding in the current lint output.** `curation_ui/main.py` line 27
  imports `Story` twice: `from src.schema.models import Story, ..., Story as StoryModel`. The
  `StoryModel` alias is never used anywhere in the file (verified: `grep -n StoryModel
  curation_ui/main.py` matches only the import line itself). Bare `Story` IS used throughout the
  file (`select(Story)`, `Story.Status.PENDING`, etc., in `approve_story`, `reject_story`,
  `edit_story`, `save_story`, `mark_posted`). `ruff --fix` on this line proposes deleting the FIRST
  `Story` (the one actually used) and keeping `Story as StoryModel` (the one that's dead) — because
  ruff can't see that `_render_stories_grid` shadows `Story` with its own local import three lines
  later while every other function relies on the module-level name. Applying that autofix verbatim
  would delete a name still in use elsewhere in the file and break every route except the story
  grid at runtime, with no test to catch it (see next finding). The correct fix is to remove only
  `, Story as StoryModel`, not `Story`.
- **New finding — zero test coverage for `curation_ui/`.** No `tests/test_curation_ui*.py` exists.
  Nothing imports `curation_ui.main` or `curation_ui.health` in the test suite, so a bug like the
  one above — or anything else that breaks import time or a route — would not be caught by
  `pytest` or by CI, only by opening the live site. This got riskier after `#9`'s 1,033-line
  template/CSS rewrite shipped with no corresponding tests.
- **No `[tool.ruff.lint] select` is pinned in `pyproject.toml`.** Whatever ruleset ruff defaults to
  is whatever that installed ruff version defaults to, which can silently grow or shrink across
  ruff upgrades — this is likely why the v7 "~220 pre-existing findings" figure doesn't match what
  I see now. With ruff 0.16.8's actual default families (`E4`, `E7`, `E9`, `F` — the ones ruff
  documents as "always on" without a `select`), current count is **62 findings**: 37 `F401` (unused
  import — mostly genuine, one is the bug above), 16 `E402` (module import not at top of file — 15
  of these are in `curation_ui/main.py` and are NOT a real problem, see below), 7 `F841` (unused
  variable), 2 `F541` (f-string with no placeholders).
- **`curation_ui/main.py`'s E402 findings are a false positive, don't touch them.** Lines 3–17
  monkey-patch `socket.getaddrinfo` to force IPv4-only DNS *before* importing FastAPI/SQLAlchemy/
  asyncpg, because "Vercel's lack of outbound IPv6 routes" (per the file's own comment) breaks
  asyncpg's connection unless the patch is applied first. Reordering these imports to satisfy E402
  would silently reintroduce that production bug. Any lint task must either leave `E402` off the
  selected rule set for this file or add a scoped `# noqa: E402` / per-file-ignore, never reorder.
- **`/healthz` on the curation UI is still unauthenticated** (unchanged from v7): it returns story
  counts by status and scrubbed-but-present exception text with no auth. Still a decision for
  Tyler, not a task — see below.
- Unchanged from v7, still true, still Tyler's calls to make: GDELT/embeddings/celebrity-vertical/
  heartbeat-race items, and the reminder to rotate the Supabase password if a pre-fix pytest run
  with `-v`/`-rs` ever printed the skip message while `.env` pointed at Supabase.

## Task R0: preflight
R0.1 Set the environment variables from "Non-interactive shell rules".
R0.2 `git --no-pager status --short` and `git --no-pager stash list`. Report the output as plain
     text. Do not edit any file yet.
R0.3 `git fetch origin`, then `git checkout -b chore/cleanup-and-coverage origin/main`. If git
     refuses because AGENT_TASKS.md is dirty, `git stash push -m v8-tasks`, checkout, then
     `git stash pop`. First commit: `git add AGENT_TASKS.md` then
     `git commit -m "docs: AGENT_TASKS.md v8" -m "Task file for the dead-file cleanup, lint, and curation_ui coverage round."`
     and push with `git push -u origin chore/cleanup-and-coverage`.
R0.4 Baseline: `uv run pytest tests/ -q` with DATABASE_URL unset. Expect 220 passed, 5 skipped, 0
     failed. If you see something else, stop and report it before continuing.

## Task 1: remove dead curation_ui project files (branch chore/cleanup-and-coverage)
1.1 Confirm nothing references them before deleting:
    `git --no-pager grep -rn "curation_ui/pyproject\|curation_ui/uv.lock" -- . ':!curation_ui/pyproject.toml' ':!curation_ui/uv.lock'`
    must print nothing. Also check no workflow does `working-directory: curation_ui` or `cd
    curation_ui`: `git --no-pager grep -n "curation_ui" .github/workflows/*.yml` — if this shows
    anything beyond the `functions` path in `vercel.json`, STOP and report instead of deleting.
1.2 `git rm curation_ui/pyproject.toml curation_ui/uv.lock`.
1.3 VERIFY: `uv run pytest tests/ -q` still shows 220 passed, 5 skipped, 0 failed. `uv sync --extra
    dev --extra pipeline` still succeeds from the root (proves the root lockfile alone is
    sufficient). Paste both outputs.
1.4 Commit subject: `chore: remove unused curation_ui/pyproject.toml and uv.lock` — body explains
    they duplicated the root project file, disagreed with it on the Python version pin, and nothing
    installed from them (cite the root pyproject.toml comment and VERCEL_DEPLOY.md).
1.5 Push.

## Task 2: pin ruff's rule set and fix the resulting findings (same branch)
2.1 Add to `pyproject.toml`:
    ```
    [tool.ruff.lint]
    select = ["E4", "E7", "E9", "F"]
    ```
    directly under the existing `[tool.ruff]` table. This is a lock-in of ruff's current documented
    defaults, not a new stricter policy — it exists so the finding count can't silently drift again
    as ruff versions change. Do not add any other rule families (no `UP`, `I`, `BLE`, `S`, `SIM`,
    etc.) — those are a separate decision for Tyler, not this task.
2.2 Add a per-file ignore for the DNS-patch ordering:
    ```
    [tool.ruff.lint.per-file-ignores]
    "curation_ui/main.py" = ["E402"]
    ```
    with a one-line comment above it in the TOML referencing the IPv4-only monkeypatch and why
    reordering would break it.
2.3 Run `uv run ruff check .` and read every remaining finding yourself before touching anything —
    do not run `--fix` blind. For `curation_ui/main.py:27`, the fix is to remove only
    `, Story as StoryModel` from the import line (verify first with
    `grep -n "StoryModel" curation_ui/main.py` that it still shows zero uses besides the import
    line you're editing). For every other `F401` finding, confirm with `grep -n` that the name is
    genuinely unused in that file before removing it — do not trust the autofix diff without that
    check, per the Story/StoryModel example above. `F841` and `F541` findings are safe to autofix
    with `uv run ruff check --fix --select F841,F541 .` after eyeballing the diff.
2.4 VERIFY, paste all output: `uv run ruff check .` shows 0 findings. `uv run pytest tests/ -q`
    still shows 220 passed, 5 skipped, 0 failed.
2.5 Commit subject: `chore: pin ruff lint rules and fix unused-import/unused-variable findings` —
    body must explicitly call out the Story/StoryModel fix as a behavior-preserving bug fix, not
    just a lint fix, and must explain the `E402` per-file-ignore.
2.6 Push.

## Task 3: add minimal test coverage for curation_ui (same branch)
The goal is a smoke-test safety net, not full coverage of the triage workflow — that's a bigger
task for another round. New file: `tests/test_curation_ui.py`.
3.1 A test that imports `curation_ui.main` and `curation_ui.health` and asserts the import
    succeeds. This alone would have caught the Story/StoryModel bug in Task 2 had it existed before
    the fix — write it in a way that would fail on that specific bug (e.g. also exercise the route
    that constructs `select(Story)` outside `_render_stories_grid`, such as by calling
    `approve_story`'s handler through a `TestClient` request, not just importing the module).
3.2 Reuse the existing `test_settings`/`db_engine`/`db_session` fixture pattern from
    `tests/conftest.py` (sqlite+aiosqlite in-memory). Because `get_settings()` and the module-level
    `_engine` in `src/shared/database.py` are cached (`lru_cache`, module globals), use
    `monkeypatch.setenv` plus `get_settings.cache_clear()` before constructing the `TestClient`, and
    confirm in a comment why that's necessary — do not silently work around caching without
    explaining it, since a future reader needs to know the fixture depends on cache-clearing order.
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
    0 failed — exact new count depends on how many you write).
3.6 Commit subject: `test(curation-ui): add smoke tests for health and auth boundary` — body
    explains the cache-clearing requirement and lists what is and isn't covered.
3.7 Push.

## CHECKPOINT: STOP
Push and report: `git --no-pager log --oneline -6`, full test counts, `uv run ruff check .` output,
files changed, and the compare URL (never claim a PR exists unless you opened one). Tyler will
review and open the PR himself.

## Decisions for Tyler (agent: do nothing about these)
- Confirm the Actions tab actually shows green runs for `ci.yml`, `daily-ingest.yml`, and
  `cleanup.yml` since the cache fix landed — I could not check this from the sandbox.
- `/healthz` is still unauthenticated and returns story-status counts and scrubbed exception text
  to anyone. Options: put it behind the same `CURATION_USER`/`CURATION_PASSWORD` basic auth (loses
  the ability for an external uptime monitor to hit it without credentials), or leave as-is since it
  never returns secrets. Not touched this round either way.
- Whether to expand the pinned ruff rule set beyond `E4/E7/E9/F` (e.g. `UP` for the ~94+56 typing/
  datetime modernizations, `I` for import sorting, `BLE`/`S` for exception-handling hygiene) is a
  separate, bigger cleanup — deliberately left out of Task 2 so that task stays small and reviewable.
- Rotate the Supabase database password if a pre-`#10` pytest run with `-v` or `-rs` ever printed
  the skip message while `.env` pointed at Supabase (carried over from v7 — unverifiable by me).
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

## Report format after every checkpoint
Files changed; tests run with pass/fail counts; the pasted VERIFY output; ruff output; CI result or
"unknown"; anything surprising; what you need from me.