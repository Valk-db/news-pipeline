# Decisions

This is the repo's own decision log. The company's working decisions live in
`~/workspace/procmon-dev/DECISIONS.md`; where the two disagree, this file is the
one that describes the code in *this* repository, and it is the one the code and
its tests point at.

Entries are dated, attributed, and written in the past tense once they are acted
on. A decision that is still open says so in its own heading.

---

## Public exposure is human-gated

**Locked 2026-10-02 by Tyler. Not reopened since.**

Nothing in this repository publishes a story automatically. A story becomes
visible on the public surfaces (`/map`, `/stories/{id}`, `/proof/{id}`) only by
passing the corroboration gate *and* a human deciding to expose it.

The mechanism, in code, is the status filter in `curation_ui/discovery.py`:

```python
PUBLIC_STORY_STATUSES = (Story.Status.QUEUED, Story.Status.POSTED)
```

`QUEUED` means "approved, ready to post". `PENDING` (gate-passed, not yet
reviewed), `BLOCKED` (gate-failed), `EXPIRED` (stale) and `REJECTED` are all
excluded. There is no code path that sets `QUEUED` automatically. The approve /
reject / edit routes that used to do it were removed in the 2026-10-02 curation
redesign and are not to be restored; `tests/test_curation_read_only_ui.py` pins
that the UI has no state-changing route and that its markup names no
approve/reject/edit/save control.

### Why this is the safe default and not a missing feature

The gate is a machine judgement about *corroboration*: how many distinct
reporting owners stand behind a story. It is not a judgement about *harm*. A
story can clear the corroboration bar and still be something a person would not
want published — wrong grouping of a multi-actor event, thin sourcing dressed up
as agreement, or an allegation resting on a single outlet's framing of a named
person. Those are the errors that a machine is measurably bad at, which is why
the last one is exactly what the spot audit below is designed to catch.

There was a superseded decision in the other direction (auto-queue on gate
pass). It was reversed before any code was written. The worktree and branch for
it were abandoned at zero commits; nothing in this repo was built on it.

### What "human-gated" does *not* mean

It does not mean the map is unpopulated, and it does not mean a human presses a
button for every story. It means **no code decides exposure**. Whatever routes a
story into `QUEUED` must be a deliberate act by a person, or a human-approved
export. Everything else about the pipeline is automatic and stays that way.

### Immediate capability: unpublishing

Because there is no in-app publish button, there was for a time no in-app
withdraw button either — a story that had been exposed could only be pulled by
hand-editing the database. That gap is closed by:

    python -m scripts.unpublish_story <story_id>

which sets the story to `REJECTED` (so every public filter drops it on the next
request, with no route changes) and writes the action to the `status_log` so the
withdrawal is auditable. This is the manual version of exit criterion 4 below;
the automatic per-request flag is still owed.

---

## Exit criteria for ever auto-publishing

**Pre-registered 2026-10-02, before any measurement. All four must hold.**
These numbers are fixed in advance on purpose: thresholds chosen after seeing
the results are not thresholds, they are rationalizations. The point of writing
them down now is that nobody gets to move them later because the data is
inconvenient.

If any one of them fails, the correct response is to fix the thing that failed
and re-measure. The correct response is *not* to adjust the number.

### 1. Phase 2 must run before the publish decision

Harm detection depends on claims, and claims come out of Phase 2 (claim
extraction, narrative arcs, topic groups). A harm check that runs weekly against
a pipeline that decides exposure first is checking yesterday's decisions with
this week's tools.

Status: **not met.** Phase 2 runs on gate-passed `PENDING` stories, and the
publish decision is a separate, human act, so the ordering is correct today.
It becomes load-bearing the moment the ordering is automated, and it must not
be inverted then.

### 2. The dynamic gate shadows the static gate for 14 days

The dynamic gate is currently shadow mode, default off
(`dynamic_gate_enabled: bool = False`, `src/shared/config.py:135`). It runs,
records what it would have decided, and changes nothing.

- Window: **14 consecutive days** of shadowed runs.
- Every boolean-vs-dynamic **disagreement** is reviewed by hand. Not sampled,
  not estimated — all of them. A disagreement is a case where the static gate
  said one thing and the dynamic gate said another; each one is either a bug in
  the static gate, a bug in the dynamic gate, or a case where the two are
  measuring different things, and all three are worth knowing about.
- **Ceiling: 5%** of all stories evaluated by *both* gates during the window.
- The denominator is every story both gates evaluated inside the window, counted
  in the data, not estimated. A run that crashes, skips the dynamic gate, or
  fails to record its decision does not shrink the denominator.

Status: **not met.** Not started; the dynamic gate has never been switched on in
shadow against production-shaped traffic.

### 3. A seeded spot audit, 30 per week, plus every harm-flagged story

Two separate obligations. The second is not a sample and does not get scaled
down.

- **30 gate-passed stories per week**, drawn by a seeded script so the draw is
  reproducible and not quietly cherry-picked. Before writing a new audit tool,
  check whether `scripts/story_audit.py` can be extended.
- **100% of harm-flagged stories.** Every one, reviewed, every week. A flagged
  story that nobody looked at is the single most expensive failure mode here,
  because a flag is a system asserting "this one might hurt someone" and then
  nothing acting on it.

**A false pass** is a story a human would reject on review for any of:

- wrong grouping (the story is really about something else, or has merged
  unrelated events),
- thin corroboration (it presents as confirmed when the sourcing is not),
- an allegation about a named person (a real person's name is attached to a
  claim on insufficient evidence, or the framing implies more than the sources
  say).

Any false pass **resets the clean count to zero** and requires a written root
cause before sampling resumes. It does not count as 1 of 30 and get averaged
away.

**Threshold: 300 consecutive clean samples.** At 30 per week that is roughly ten
weeks. 300 is the number Tyler chose, and it is the right one: the rule of
three says zero failures in *n* trials bounds the true failure rate at about
`3/n` with 95% confidence, so 300 clean samples bounds it under ~1%. 120 was
considered and rejected — it bounds at ~2.5%, which is not the bar being asked
for.

Status: **not met.** The audit is not running and there are no clean samples
yet.

### 4. A one-switch unpublish that the public routes check on every request

Instant, cheap, and evaluated per request.

Explicitly **not** a pipeline-wide switch. A pipeline-wide flag is the wrong
shape: an import failure kills the process before any switch is read, which is
the same class of bug as a broken Merkle-log import taking down every ingest
run. Ingest can already be stopped by disabling its GitHub workflow, which is
the right tool for stopping ingest.

What is owed is a flag the public read path consults on each request, so that
flip one thing and every public surface stops serving that story without a
redeploy.

The interim version exists: `scripts/unpublish_story.py <id>` sets a story to
`REJECTED` and logs the action (see above). That is per-story and manual, not a
switch. It is better than raw SQL and it is not this criterion.

Status: **not met.**

### Summary

| # | Criterion | Number | Status |
|---|---|---|---|
| 1 | Phase 2 before the publish decision | ordering | not met |
| 2 | Dynamic gate in shadow | 14 days, ≤5% disagreement | not met |
| 3 | Seeded spot audit | 30/week + 100% of harm-flagged, 300 consecutive clean | not met |
| 4 | Per-request unpublish switch | one flag, every public request | manual script only |

**Reopening any of this requires new evidence.** Not a new argument, not a
better intuition about the gate, not a run where the disagreement rate looked
fine. Evidence: measurements against the pre-registered numbers above, over a
window long enough for the numbers to mean something. Until then the decision
stands as written.

---

## `curated_posts`: measured, kept, not dropped

Measured on **dev** (Supabase project behind the local tunnel), 2026-10-03:

```sql
SELECT count(*) FROM curated_posts;
```

```
 count
-------
     0
```

`curated_posts` was the table the removed approve/reject flow wrote curated
captions into. No code writes it any more. The approve / reject / edit routes
are gone and the LLM caption step went with them.

**It is still in the schema, and it is staying there for now.** Two reasons,
both about the measurement being narrower than it looks:

1. **Dropping a table is one-way.** The count above is a snapshot of *dev*.
   Nothing was measured against production. If prod holds approved captions,
   a `DROP TABLE` destroys the only copy of them, and there is no undrop.
   Export first, decide second, in a later migration.
2. **A table drop does not clean up after itself.** `curated_post_status` is a
   Postgres `ENUM` type owned by the column; dropping the table leaves the type
   behind. `tests/test_recomputable_derived_state.py` pins the
   `curated_posts.story_id → stories.id` foreign key as a
   `NO ACTION` blocker, and `tests/test_schema_check.py` pins the enum type.
   Both would need to change in the same migration that drops the table.

What was done instead is to make the table's status honest rather than
invisible: the `CuratedPost` docstring in `src/schema/models.py` marks it
retired and records these reasons, the dead `approved_posts` count is gone from
`/healthz/details` (it was watching a table nothing writes, through a code path
that could not fail), and `scripts/recompute_derived.py` still reports
`curated_posts` as a foreign-key blocker — because if a row ever *does* appear,
the right response is to stop and ask, not to have the tooling quietly forget
it.

If prod is measured at zero as well, the drop goes in its own migration, after
an export.

---

## Open decisions referenced by the code

These are referenced from the code but are **not** settled here, and this file
does not pretend otherwise.

### Independent checkpoint monitor — Tyler's call, not the app's

`curation_ui/cron.py` explains why the watchdog alone is not enough: on Vercel
Hobby a scheduled invocation that fails sends no notification, so the way to
page a human when *Vercel* is the thing that is broken is a third-party probe
(healthchecks.io, UptimeRobot) pointed at
`/api/cron/checkpoint/watchdog`. Creating that account, choosing the vendor and
paying or not paying for it is a deployment decision for Tyler, deliberately
left outside the app.

### Transparency log signing key (v2) — ceremony blocked

The production keypair has not been generated. The signer is hardened and the
refusal vocabulary is implemented (`src/transparency/signing.py`), but the
ceremony is blocked on a skeptic review's findings being fully closed, so no key
exists and no checkpoint is signed. The properties the signer is held to, as
exercised by `tests/test_transparency_signer_v2.py`:

1. **Note primitives** — the C2SP v2 wire format, checked against the spec's own
   example vector.
2. **Checkpoint v2** — note text, extension line, and byte-for-byte stability of
   v1 checkpoints, which must stay verifiable under the v2 reader.
3. **Key bounds** — validity windows are enforced at signing, and an unbounded
   key is refused rather than quietly accepted.
4. **Signer refusals** — append-only enforcement, idempotency, and no
   tree-size regression: the signer refuses to sign a smaller tree than it has
   already published, and refuses to advance a tree whose shape it cannot
   verify. It also refuses when the previous checkpoint's signature does not
   verify, when the key is not in the trusted set, and when the externally held
   head disagrees with the database.
5. **Cron surface** — bearer auth with a constant-time comparison, honest 503s
   with a body that says what is missing, and a watchdog that measures the
   newest published checkpoint rather than the scheduler.

The full skeptical review and the per-finding status live in the company
backlog, not here. The point of listing the properties here is that the test
module's docstring points at this file, and it should not be pointing at
nothing.

---

## Also referenced by the code

- Transparency/Merkle subsystem: **keep and freeze** (2026-10-02, Claude's
  design review). It is append-only at the database level, enforced by trigger
  and by role grants rather than by convention, and the entry point no longer
  imports it at module level so a transparency problem cannot take down ingest.
- `pgvector`: the `vector` extension is installed on the database
  (2026-10-02) but no column, index, or RPC uses it yet. Its presence is
  deliberately not a vector-search feature; planning against a vector index
  that does not exist is how that becomes a real accident later.
