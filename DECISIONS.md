# Decisions

Durable decisions and their evidence. A decision moves from here to a migration or a
code comment when it stops being a judgement call. Dates are the day the decision was
made, not the day it was implemented.

---

## 2026-10-02 — Public exposure stays human-gated; nothing auto-publishes

**Decision.** Phase 2 enrichment (claims, claim evidence, entity edges, topic groups)
runs on gate-passed `PENDING` stories, but it never changes a story's status. A story
becomes publicly visible only when a human approves it in the curation UI, exactly as
before. The public story filters stay `(QUEUED, POSTED)`
(`PUBLIC_STORY_STATUSES` in `curation_ui/discovery.py`).

**Evidence (measured on the dev database, 2026-10-03).** 2,278 stories: 643 `PENDING`,
1,635 `BLOCKED`, 0 `QUEUED`, 0 `POSTED`, and `curated_posts` count(\*) = **0**. With
`/api/map/stories?hours=8760&limit=1000` returning 0 stories and
`/stories/{pending_id}` returning 404 before Phase 2 ran, and the same 0 and 404
after a Phase 2 run created 4 claims, 16 claim-evidence rows, 28 entity edges and 3
topic groups on a real story. The load-bearing proof is
`tests/test_phase2_isolation.py::test_phase2_derived_rows_do_not_make_a_pending_story_visible`,
which attaches real derived rows to a `PENDING` story and asserts it is still 404 and
that its claim text appears nowhere on the public surface.

**What would reopen this.** Not a code change and not a passing test. It requires new
evidence against all four pre-registered exit criteria below, audited and written down
first. "It looks fine in the UI" is not evidence.

### Pre-registered exit criteria for automatic publication

These were fixed **before** the implementation and are not to be renegotiated on the
basis of how the implementation turned out.

1. **Phase 2 runs before the publish decision, not weekly.** Phase 2 is a daily job
   (`.github/workflows/daily-phase2.yml`, `23 20 * * *`, after both ingests at
   `23 6,18 * * *`). A weekly Phase 2 would mean a week's evidence arriving after the
   decision it is supposed to inform, which makes the gate's inputs stale by
   construction.
2. **The dynamic gate runs in shadow for 14 days**, alongside the current static
   ≥2 tier-1 owners rule, with no effect on any public surface. Disagreement ceiling:
   **5% of the stories both gates evaluated**, where the denominator is the
   intersection of the two gates' evaluated sets (stated explicitly because "5% of
   stories" is ambiguous and an ambiguous denominator is a denominator that gets
   quietly redefined until it passes).
3. **Spot audit of 30 gate-passed stories per week**, drawn by a **seeded** script so
   the sample is reproducible and cannot be cherry-picked, plus **100% of
   harm-flagged** stories. A *false pass* is a story Tyler would reject on review for
   wrong grouping, thin corroboration, or an allegation about a named person. **Any**
   false pass resets the clean count to zero and requires a written root cause. The
   threshold for considering the dynamic gate proven is **300 consecutive clean
   samples**. (300, not 120: at 30/week that is ten weeks, which is the shortest
   window in which a rare failure mode shows up at all.)
4. **A one-switch unpublish flag, checked on every public request.** Not a nightly
   reconciliation job and not a dashboard toggle. A retraction has to be live by the
   next request, because the failure being protected against is a harmful story being
   visible, and a flag that takes a job cycle to take effect is a flag that is late
   exactly when it is needed.

---

## 2026-10-03 — Groq's daily *token* cap, not its daily request cap, is the binding constraint

**Decision.** Phase 2 is budgeted in **tokens** on its own counter
(`groq_phase2_tokens`, `phase2_daily_token_cap = 40_000`), not in requests.

**Evidence.** Groq's free tier for `openai/gpt-oss-20b` is 30 RPM, 1,000 RPD,
8,000 TPM, 200,000 TPD (provider's own rate-limit docs, read 2026-10-03; the TPM
figure independently confirmed by a live 429 body: "Limit 8000, Used 5011, Requested
3954"). Measured cost of one claim-extraction call: 1,742 tokens at 3 units, 2,437 at
5 units, 4,962 at 10 units. On 2026-10-03 the dev database held 16 `groq_requests`
against roughly 200,000 tokens — i.e. **4% of the request quota was spent while
100% of the token quota was spent**, and the run that pushed the day over the line
was a 429 reading `TPD: Limit 200000, Used 199337, Requested 4388`. A request-denominated
budget would have read as 96% unspent while the day was over. 40,000/day is 20% of the
allowance, leaving room for the existing caption and classification work.

**Consequence, stated plainly.** The existing caption/classification path is
token-denominated but **records only requests** (`groq_requests`), so its daily token
cost is not readable from the database at all. That is a real gap in the cost
accounting, and it is the reason Phase 2 has its own row rather than sharing one.

---

## 2026-10-03 — Claim extraction was silently returning zero claims

**Decision.** Claim extraction runs with `max_tokens=4096` and
`reasoning_effort="low"`, and treats an empty completion as an error rather than as
"no claims found".

**Evidence.** `openai/gpt-oss-20b` is a reasoning model. With the previous
`max_tokens=1500` and no `reasoning_effort`, a live call on story
`311c846b-f13a-40cc-b18e-407e4f2e7d29` returned `finish_reason="length"`,
`usage.total_tokens=4962`, and **content length 0** — so the caller saw an empty
JSON string, parsed nothing, and inserted nothing. A repeat call with identical
parameters returned 1,054 characters of valid JSON, so the failure is intermittent:
it would have looked like a working feature that produced no results on some days.
With `max_tokens=4096` and `reasoning_effort="low"` the same prompt returned
`finish_reason="stop"` and 3,400 characters of complete JSON every time. This is why
`claims` and `claim_evidence` were both 0 across 2,278 dev stories.

## 2026-10-03 — Rate-limit classification has to look through the retry wrapper

**Decision.** `LLMClient`'s status-code helpers unwrap the exception chain
(`tenacity.RetryError` → last attempt → `__cause__`/`__context__`, bounded to 6
levels), and `chat_completion` always converts a final provider failure to
`LLMError(...) from e` rather than re-raising the raw SDK or tenacity exception.

**Evidence.** `groq.RateLimitError` is not an `httpx.HTTPStatusError`, so the
previous `isinstance` checks classified every real 429 as "not transient" and "not
auth", skipped the retry, fell through to Cerebras (unconfigured) and surfaced
`LLMError("No LLM provider available")` with no status code and no cause. Retry-after
therefore could not be honoured, because nothing could tell that the failure was a
429. The tests that lock this in are
`tests/test_phase2.py::test_a_429_survives_the_client_and_is_still_classifiable` and
`::test_a_402_survives_the_client_and_is_never_routed_around`.

## 2026-10-03 — A story is only analysed if it has two text-bearing units

**Decision.** The runner selects stories with at least 2 units that have non-empty
article body text (`min_units=2`, `claims.MIN_CLAIM_UNITS`).

**Evidence.** All 20 newest `PENDING` dev stories have exactly one linked unit
(`links=1 texts=1`). A one-unit story is the same article counted twice, so a claim
matrix over it is a restatement, not corroboration, and the LLM call would cost
~1,700 tokens to say so. On the first real run, 20 of 20 stories were skipped for
this reason and **0 tokens were spent**; before the skip existed, the same run
reported "20 processed, 0 claims, 0 errors", which reads like success.

## 2026-10-03 — Viewpoint children are excluded from Phase 2

**Decision.** `select_phase2_stories` excludes stories with a non-null
`viewpoint_cluster_id`.

**Evidence.** Measured on dev: **0 of 643** `PENDING` stories are viewpoint children,
so the guard matches nothing today. It is retained because the spend would otherwise
multiply on a single body of evidence — a viewpoint child is a stance-labelled slice
of a story that is itself in the pool. It is tested against seeded rows, not against
the dev data, precisely because the dev data cannot exercise it.

## 2026-10-03 — A dead connection costs a re-parse, not a re-buy

**Decision.** Claim parsing and claim writing are separate steps, and only the write
is retried, on a fresh session, exactly once, and only for a genuine disconnect
(`sqlalchemy.InterfaceError`/`OperationalError`, or text containing "connection is
closed" / "closed the connection").

**Evidence.** A live run spent a real Groq call, received 4 claims, and then lost the
entire insert to `InterfaceError: connection is closed` at flush — the tokens were
already charged and the work was already done. Retrying the write costs nothing;
retrying the call would have doubled the spend and, at the daily token cap, could not
be afforded. The predicate is deliberately narrow: a duplicate key or a bad column
must not be retried, because that would loop on a real defect.

---

## 2026-10-03 — Every rung is capped in tokens, and the cap is an upper bound

**Decision.** Each rung in `llm_roster.ROSTER` gets a daily **token** cap alongside its
request cap (`groq_daily_token_cap` 120,000; `cerebras_daily_token_cap` 500,000;
`openrouter_{gemma,nemotron}_daily_token_cap` 200,000 each), enforced **both** before the
call and after it: `ensure_headroom(upper_bound_tokens(messages, max_tokens))` refuses the
call, and `record(usage.total_tokens)` charges what the provider actually reported. The
token counter row name is *derived* from the rung's existing request counter
(`token_counter_name()`), so two rungs cannot collide and one rung cannot grow a second
name for the same spend. A call whose `usage` cannot be read is charged a one-token floor
and counted on a separate `<rung>_unpriced_calls` row — never zero.

**Evidence.** The gap was recorded in the entry above and measured live: on 2026-10-03
dev's `groq_requests` stood at 28 while the day's 200,000 tokens were gone. The gate's
estimate is an **upper bound**, not a guess, because `max_tokens` is the provider's own
ceiling on the completion and the prompt side is estimated at characters/2 against
English's ~4 chars/token; both terms over-estimate, so refusing when the bound does not
fit is correct rather than pessimistic. Phase 2 keeps its own row (`groq_phase2_tokens`,
40,000) because it calls Groq through a different path, and 120,000 + 40,000 = 160,000 of
200,000 leaves 20% shared margin. Live on dev: a real call reported
`total_tokens=115` and the row moved 0 → 115; with the day's row inflated to
119,905/120,000 the next real call was refused **before the provider was contacted**
(dispatch count unchanged, request row unchanged); restoring the row re-opened the gate
for a fresh client.

**Two things decided against the obvious alternative.** No reserve-and-refund: the
statement that reserves a request has no refund primitive, so an estimate would be either
the final charge (starving the job at half its granted budget) or a new shared decrement.
And **recording never refuses**, which looks inconsistent with refusing on an unreadable
counter until you follow it: refusing to record a call that already happened and was
already billed would understate the day and make the *next* gate believe there is
allowance left. Unreadable is "spent"; already-spent is "still counted".

**Consequence for the coordinator.** If `phase2_daily_token_cap` is raised,
`groq_daily_token_cap` has to come down, or the two rows sum past the provider's 200,000.
That coupling is arithmetic, not a policy, so it is written down rather than enforced.

## 2026-10-03 — Bind parameters that Postgres has to type, or the cap does not exist

**Decision.** `budget.py`'s single spend statement declares `:amount` and `:cap` as
`bindparam(..., type_=BigInteger)`.

**Evidence.** Commit `bcff1e8` (04:08 UTC today) moved the insert path's cap predicate
into `INSERT ... SELECT :name, :day, :amount WHERE :amount <= :cap` so that a day whose
first spend exceeded the cap would be refused. On SQLite that is fine; on Postgres the
statement does not parse at all — `ProgrammingError: inconsistent types deduced for
parameter $3`, because `:amount` is typed as the target column (bigint) in the SELECT list
and as integer in the comparison, and Postgres refuses a parameter whose two uses disagree.
Measured live on dev through the pg tunnel against `postgresql+asyncpg`. `spend()` catches
the error and returns `None`, which the module reads as "cap spent" and `RequestBudget`
reads as "do not spend" — so for those two hours every daily budget on Postgres read as
exhausted and every LLM call was refused, with the whole suite green, because the 2,000+
budget tests all run against SQLite, which never asks the server to deduce a type. Nothing
in the codebase is allowed to depend on a statement that only the test dialect can parse.
