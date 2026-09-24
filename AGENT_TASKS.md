# AGENT_TASKS.md: news-pipeline task file (v10, updated 2026-09-24)

This file is the only task file in force. Ignore any older task files (including v9's "Task 3",
which is finished and merged) and anything you remember from earlier rounds. Read this file fully
before doing anything.

Repo: Valk-db/news-pipeline. Python 3.12, uv, async SQLAlchemy, Supabase Postgres, GitHub Actions,
FastAPI curation UI on Vercel.

## Problem
In the curation UI, every story card shows a wall of UUID "pills" (for example
`c847e979-d6a7-4ac1-87f7-bd312e966733`) between the header row (PENDING / date / gate reason) and
the source list. Those are canonical entity IDs. `Story.primary_entities` stores them on purpose
(`src/verification/stories.py` uses them to match new articles to existing stories), and
`story_card.html` renders them raw as `.entity-tag` spans. They mean nothing to a reviewer.
Goal: stop rendering them. Do NOT change the data.

## Non-interactive shell rules (read first)
- Before anything else set these for your shell session.
  PowerShell: `$env:GIT_PAGER="cat"; $env:GIT_EDITOR="true"; $env:GIT_TERMINAL_PROMPT="0"`
  bash: `export GIT_PAGER=cat GIT_EDITOR=true GIT_TERMINAL_PROMPT=0`
- Use `git --no-pager` for log/diff/show/grep/stash show. Use `git merge --no-edit`. Commit only
  with `git commit -m "subject" -m "body"`. Never run `git commit` without -m, `git rebase -i`,
  `git add -p`, `less`, `vim`, `nano`, or anything that waits for keyboard input.
- Never use commands that block until finished (for example `gh run watch`). Use one-shot commands
  such as `gh pr checks` and report "pending" if it is not done.
- Make targeted edits only. Never rewrite a whole file in one tool call, never print whole files to
  the terminal, read only the section you are editing.
- If a command shows no output for about 2 minutes, cancel it and report which command. Do not
  retry the same command more than twice. If you are stuck, stop and say which step you are on and
  what you tried.

## Working agreement
1. Start from a fresh `git fetch origin` and branch from `origin/main`:
   `git switch -c fix/hide-entity-ids-in-curation-ui origin/main`. main receives bot commits
   ("chore: heartbeat"), so do not assume its SHA; record the SHA you branched from. Never push to
   main, never force-push, never merge a pull request yourself.
2. Modify ONLY these files:
   - `curation_ui/templates/story_card.html` (remove one block, see 1.1)
   - `curation_ui/static/style.css` (remove three rules, see 1.2)
   - `tests/test_curation_ui_templates.py` (new, see 1.3)
   - `AGENT_TASKS.md` (this file, commit it as-is, see step 0)
   Do NOT touch `src/**`, `scripts/**`, `Story.primary_entities` or any model/column, GDELT, the
   heartbeat, gate/grouping/tier logic, `curation_ui/main.py` triage logic, `tests/conftest.py`, or
   `tests/test_curation_ui.py`. The dark theme from #9 stays; this task authorizes removing only
   the entity-tag markup and its three CSS rules, nothing else in those two files.
3. Each commit message = a short subject plus a body paragraph explaining WHY, describing only what
   the diff contains.
4. Tests run with DATABASE_URL UNSET: `uv run pytest tests/ -q`. Never set DATABASE_URL to the
   Supabase URL. Never print it, and never repeat anything that looks like a connection string or
   API key in your report.
5. Nothing runs against the production database from your machine. Do not run
   `scripts/story_audit.py` (except `--help`), `scripts/init_db.py`, `cleanup_stale.py`,
   `enable_rls.py`, or `python -m src.ingestion.run` without `--dry-run`.
6. Do not weaken, delete or skip existing tests. Never commit `.env`.
7. EVIDENCE RULE: a step is "done" only when (a) its VERIFY commands were run and passed, AND
   (b) `git --no-pager diff --stat` after committing shows a change to the exact file(s) that step
   named. Before writing your final report, re-read this file's Task 1 side by side with
   `git --no-pager diff origin/main...HEAD --stat` and confirm every named deliverable is there.
   Never write "complete" from memory.
8. GitHub Actions (Ubuntu) is the source of truth for tests, not this machine. If `gh` is not
   installed, write "CI status unknown, Tyler must check the Actions tab". Do not guess pass/fail.
9. Do NOT claim a pull request exists unless you created it or confirmed one exists with
   `gh pr view fix/hide-entity-ids-in-curation-ui`. A `github.com/.../pull/new/...` link is only the
   compare page.
10. Do the steps in order without asking between steps, but stop at the CHECKPOINT.

## Verified state (checked from a fresh clone, 2026-09-24; main was `be60821`)
- `curation_ui/templates/story_card.html` lines 10-14 render `item.story.primary_entities` as
  `<span class="entity-tag">{{ entity }}</span>`. This is the only place in the repo that renders
  them. `primary_entities` is otherwise only read/written in `src/verification/stories.py` and
  declared in `src/schema/models.py`.
- `.story-entities`, `.entity-tag`, `.entity-tag:hover` exist only in
  `curation_ui/static/style.css` (about lines 420-441) and are referenced nowhere else (not in
  `edit.html`, `main.py`, tests, or README).
- Dry-run of this change in a scratch copy: removing the block and the CSS leaves the header row
  (margin-bottom 16px) directly above `.story-sources`, spacing looks correct, and a render-only
  regression test fails on the old template and passes on the new one. The full suite was NOT run
  in that scratch copy; you must run it.

## Task 1: hide entity IDs from the curation UI
1.0 Step 0: `git switch -c fix/hide-entity-ids-in-curation-ui origin/main`. Commit this file:
    `docs: AGENT_TASKS.md v10` with a body saying it scopes the entity-ID removal.
1.1 In `curation_ui/templates/story_card.html`, delete exactly this block (including the blank
    line after it), so `</div>` of `.story-header` is followed by one blank line and then
    `<div class="story-sources">`:

        <div class="story-entities">
            {% for entity in item.story.primary_entities %}
            <span class="entity-tag">{{ entity }}</span>
            {% endfor %}
        </div>

1.2 In `curation_ui/static/style.css`, delete the rules `.story-entities`, `.entity-tag`, and
    `.entity-tag:hover` (from the line `.story-entities {` up to, but not including, the
    `/* Story Sources */` comment). Keep that comment and everything after it intact.
    VERIFY: `git --no-pager grep -n "story-entities\|entity-tag" -- curation_ui tests` returns
    nothing except the new test's assertion string.
1.3 Create `tests/test_curation_ui_templates.py` (separate file on purpose: do not add to
    `tests/test_curation_ui.py`, whose fixtures depend on settings cache-clearing order). It must
    render `story_card.html` directly with a plain `jinja2.Environment` and
    `jinja2.FileSystemLoader`, no app import, no DB, no fixtures. Use this exact content:

        """Regression test: story card must not render internal canonical entity IDs."""

        from datetime import date, datetime
        from pathlib import Path
        from types import SimpleNamespace

        import jinja2

        TEMPLATES = Path(__file__).resolve().parent.parent / "curation_ui" / "templates"


        def test_story_card_hides_primary_entity_ids():
            entity_id = "c847e979-d6a7-4ac1-87f7-bd312e966733"
            env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))
            story = SimpleNamespace(
                id="00000000-0000-0000-0000-000000000001",
                status=SimpleNamespace(value="pending"),
                day=date(2026, 9, 24),
                gate_reason="Passed gate: 2 tier-1 units, 2 distinct owners",
                primary_entities=[entity_id],
                tier1_unit_count=2,
                distinct_owners=2,
            )
            article = SimpleNamespace(
                url="https://example.com/a",
                title="Example headline",
                source_domain="example.com",
                published_at=datetime(2026, 9, 24, 15, 38),
                source_tier=SimpleNamespace(value="tier1"),
            )
            html = env.get_template("story_card.html").render(
                item=SimpleNamespace(story=story, articles=[article], units=[object(), object()])
            )
            assert entity_id not in html
            assert "entity-tag" not in html
            assert "Example headline" in html

    If `jinja2` is not importable in the uv env, stop and report it (it should come in via
    fastapi/starlette templating). Do not add a dependency.
1.4 PROVE THE TEST BITES: run it once against the OLD template and confirm it FAILS, then confirm it
    PASSES on the new one. Do it with `git stash` around the two curation_ui files only, or
    equivalently `git --no-pager show origin/main:curation_ui/templates/story_card.html` into a temp
    copy. Paste both outcomes. Leave the working tree with the fix applied.
1.5 VERIFY, paste all output:
    - `uv run pytest tests/test_curation_ui_templates.py -v` passes.
    - `uv run pytest tests/ -q`: 0 failed. Report exact passed/skipped counts; the new file adds
      exactly 1 passing test.
    - `uv run ruff check .` shows 0 findings.
    - `git --no-pager diff origin/main...HEAD --stat` shows only the four files named in
      Working agreement 2.
1.6 Commit the fix: subject `fix(curation-ui): stop rendering raw entity IDs on story cards`; body
    explains that `primary_entities` holds canonical entity IDs used for story matching, that they
    are not human-readable, and that only the display markup and its CSS were removed while the
    stored data is unchanged. Test goes in the same commit or a second `test:` commit, your choice.
1.7 Push the branch.

## CHECKPOINT: STOP
Push and report: `git --no-pager log --oneline -6`, full test counts, `uv run ruff check .` output,
`git --no-pager diff origin/main...HEAD --stat`, the fail-before/pass-after output from 1.4, and
the branch's compare URL or PR URL if you verified one exists. Tyler will review, check the live
page after Vercel redeploys, and merge himself.

## Decisions for Tyler (agent: do nothing about these)
- If you ever want the entity context back in a readable form, the fix is to resolve the canonical
  IDs to names (join through the canonicalizer/entity table) and show the top 3-5 names. That is a
  separate feature; not done here.
- `/healthz` is still unauthenticated (carried over from v9). Untouched.
- Off-limits until Tyler instructs: GDELT changes, embeddings/pgvector, the heartbeat mechanism,
  gate logic, grouping code, the curation UI's triage/business logic, editing production data.

## Report format after the checkpoint
Files changed; tests run with pass/fail counts; pasted VERIFY output; fail-before/pass-after
evidence; ruff output; CI result or "unknown"; the SHA you branched from; anything surprising;
what you need from me.