# C1 — P1 cheap correctness (P1a / P1b / P1c)

Branch `procmon/batch-p1-correctness`, forked from `procmon/phase-0-foundations` @ `2fafdc7`.
Nothing pushed. Dev database only (`news-pipeline-dev`, ref `qzothzirwwpesafzlxtw`) via the
already-running tunnel on `127.0.0.1:15432`.

## Commits on this branch

| commit | subject |
| --- | --- |
| `e3811b3` | P1a: make story primary_entities truncation deterministic |
| `7777107` | P1b: detect language on title + body, not the headline alone |
| `59da989` | P1c: normalize English entity aliases at canonicalization time |
| `04818d1` | gitignore the dev credential file `.procmon-dev-env` |

`git diff --stat 2fafdc7..HEAD` = 8 files, +399/-22. No other agent's files are in these commits.

## Files added / changed

- `src/verification/stories.py` — sort before the `[:10]` truncate of `Story.primary_entities`
  (both `create_story_from_units` and `_update_story_entities`); extracted `MAX_PRIMARY_ENTITIES`.
- `src/enrichment/translation.py` — `translate_article()` probes title **and** body instead of
  `title or body`; docstring on `detect_language()` records why a bare headline is not enough.
- `src/utils/ner.py` — `ALIAS_SEED` (34 rows) + `_build_alias_indexes()` + `canonical_surface()`,
  applied in `EntityCanonicalizer.resolve()` and `get_or_create()`; the hardcoded GPE/LOC alias
  block in `_generate_aliases()` is replaced by a lookup into the same table's reverse index.
- `supabase/migrations/20261002000000_entity_aliases_id_default.sql` *(new)* — restores the
  `entity_aliases.id` sequence default; idempotent.
- `tests/test_story_entity_determinism.py` *(new, 4 tests)* — story entity truncation is stable.
- `tests/test_translation.py` — 4 new detection tests; one existing batch fixture input changed
  (`_article(title="x")` → `_article(title="x", body=None)`) because the body is no longer ignored.
- `tests/test_canonicalization.py` — `TestEnglishAliasNormalization`, 8 new tests.
- `.gitignore` — `.procmon-dev-env` (credentials must never be committed; the file stayed untracked).

## Tests

Run in `/home/hatch/.venvs/nptest`.

| file | result |
| --- | --- |
| `tests/test_story_entity_determinism.py` (new) | 4 passed |
| `tests/test_translation.py` | 18 passed (14 before → 4 added) |
| `tests/test_canonicalization.py` | 32 passed (24 before → 8 added) |
| `tests/test_ner.py` | 18 passed |
| `tests/test_verification.py` | 10 passed, **5 skipped** |
| `tests/test_story_audit.py`, `tests/test_viewpoint_clustering*.py`, `tests/test_tiers.py` | 73 passed |
| **targeted total** | **145 passed, 5 skipped** |

Full suite: **935 passed, 31 failed, 8 skipped**. The 31 failures are the *identical set* the fork
point produces with my changes stashed (verified by diffing the two failure lists: empty diff), and
all 31 are `httpx.InvalidURL` — the sandbox proxy env var is malformed
(`ValueError: invalid literal for int() with base 10: ':1]'` while httpx parses it), which also
breaks `src/utils/trafilatura_extract.py`'s client in this box. **No regression from this work.**

The 5 skips in `test_verification.py` are the postgres-only story integration tests
(`build_stories`, tier gates, cross-run attachment). They refuse to run because `DATABASE_URL` is
unset: *"Refusing to run integration tests against non-localhost DB (host: unknown)"*. There is no
postgres binary, no docker and no podman in this sandbox, so those 5 could not be executed here.
The new P1a tests were written against the sqlite fixture instead so the determinism property is
actually covered rather than skipped.

`ruff check` on all 6 changed/added Python files: **All checks passed!**

## Live evidence (dev)

### P1a — deterministic story identity

Real path: 25 canonical UUIDs through `create_story_from_units()` + `_update_story_entities()`
against a live DB, printed as JSON, run in separate processes.

```
BEFORE (2fafdc7) — md5 of each of 5 processes:
0885e0bcf3e79bb7e1b74f8dfcdb6e6b
87bff374f1c952bff7497f1f01f6d430
12f645e2e60e84d0abb8195991a03866
38d4fd2ebc3946b0247098b9e5e0e7e7
13cf1872ff35997c283b1f4b7cf660b7      -> 5/5 different 10-element lists

AFTER  (committed) — 3 processes:
de3325027ac14cbc7a524ed6849779f5
de3325027ac14cbc7a524ed6849779f5
de3325027ac14cbc7a524ed6849779f5      -> byte-identical
```

The stored set is now always `0000...0001 .. 0000...000a`, and a second merge pass is a no-op.

### P1b — langdetect

**Call path.** `src/ingestion/run.py:381` → `translate_articles()` → `translate_article()`, which
did `probe = article.title or article.body_text or ""`. With a title present (33/33 dev articles)
detection therefore ran on ~50-80 characters of headline. `detect_language()` had a 12-character
minimum and `DetectorFactory.seed = 0`, so the seed was already pinned — only the probe was wrong.

**Dev corpus, real `translate_articles()` with a stub backend (no MyMemory quota spent):**

```
dev corpus: 33 articles
summary: {'backend': 'stub', 'total': 33, 'translated': 23, 'english': 10, 'unknown': 0, 'failed': 0}
rows whose detection changes vs the old title-only probe: 1/33
  ntn24.com  None -> 'es'   'Untitled'      (body is Spanish; verified: '"Exterminio hacia el adulto mayor": jubilados en Venezuela protestan...')
```

Every one of the 23 non-English rows is genuinely non-English and every one of the 10 English rows
stays `en`. **Zero regressions on the 8 English-tier dev articles.**

**The misfire class, reproduced on live English-tier feeds** (BBC world, Guardian world, NPR,
France24 — the four `EVIDENCE_FEEDS` in `src/ingestion/rss_evidence.py`; fetched with `urllib`
through the egress proxy, 109 headlines):

```
headline-only probe wrong: 5/109 (4.6%)
  BBC      'no' p=0.462   "Plummeting Israel flight like 'rollercoaster', says passenger"
  Guardian 'da' p=0.714   "Brazil attorney general says meddling 'cannot be tolerated' after Trump funding plans reveal"
  France24 'fr' p=0.571   "UK, France end 'on in, one out' Channel migrant exchange deal"
  France24 'nl' p=0.857   "France demands belt-tightening in 2027 budget as investors sour on its debt"
  France24 'fr' p=1.000   "France's 2027 budget plan puts pressure on local services"
title+description probe wrong: 0/109
```

Those confidences are why the fix is "more text", not a threshold: `nl` at 0.857 and `fr` at 1.000
would pass any confidence floor, and `detect_langs` is the only probablistic API `langdetect` 1.0.9
exposes. Of the 5, 2 had fetchable full bodies and the **production probe** (title + extracted
body) got both right (`no`→`en`, `da`→`en`, bodies of 801 and 4447 chars). The other 3 are
france24.com, which returns an HTTP error to a direct fetch from this box, so only the RSS
description stand-in was testable — and that is correct for all 3.

Detection cost on the longest dev body (12,345 chars): 20 ms, so no probe cap was added.

**Correction to the brief.** The census as described does not reproduce on today's dev data. The
current distribution is `en 10, fr 6, sq 3, de 2, zh-cn 2, ml 2, it 2, ro 1, tr 1, es 1, fa 1,
uk 1, NULL 1` = 32/33 set. The three `sq` rows are Albanian-language articles
(balkanweb.com, gazeta-shqip.com, kosovarja-ks.com) and the two `ml` rows are Malayalam articles
(evartha.in, thejasnews.com) — both **correct** detections, and none of them is on an English-tier
feed. The dev corpus has been re-ingested since that census. The underlying defect is real and is
what I reproduced above on live feed data.

### P1c — English alias normalization

`canonical_entities` and `entity_aliases` are both **empty in dev** (0 rows), so story grouping on
canonical IDs has never completed there. The real dev entity surfaces come from
`raw_articles.entities` (7 of 33 articles carry PERSON/ORG/GPE), and they contain exactly the
fragmentation the brief describes: `US` (x4) and `United States`, `Netanyahu` and
`Benjamin Netanyahu`, `UK`, `Trump`.

**Whole dev corpus, real `resolve_entities_to_canonical()` against dev** (inside a transaction that
is always rolled back; dev row counts re-checked afterwards and unchanged):

```
                      BEFORE(2fafdc7)   AFTER
canonical_entities             53        51
entity_aliases                112       117
surfaces whose canonical name changes: 2
  'Trump'  'Trump' -> 'Donald Trump'
  'UK'     'UK'    -> 'United Kingdom'
pairwise entity-Jaccard (story attach threshold 0.4):
  bbc.co.uk vs npr.org           0.000 -> 0.062
  bbc.co.uk vs theguardian.com   0.062 -> 0.133
  bbc.co.uk vs france24.com      0.000 -> 0.067
```

**Order-independence, the actual bug.** Built from real dev surfaces, both arrival orders:

```
BEFORE | long form first   Jaccard=0.500 attach=YES | GPE rows ['United States']
BEFORE | short form first  Jaccard=0.000 attach=no  | GPE rows ['United States', 'US']   PERSON rows ['Benjamin Netanyahu', 'Netanyahu']
AFTER  | long form first   Jaccard=0.500 attach=YES | GPE rows ['United States']
AFTER  | short form first  Jaccard=0.500 attach=YES | GPE rows ['United States']
```

At `2fafdc7` aliases were only generated when the long form happened to arrive first, so `US`
arriving first created a second canonical entity and the Jaccard of two units about the same event
fell to 0.000 — two stories instead of one. With the table applied in `get_or_create()`, whichever
spelling lands first becomes the one entity.

### Dev-side schema blocker found during P1c verification

The live run failed first with `NotNullViolationError: null value in column "id" of relation
"entity_aliases"`. `information_schema` on dev:

```
entity_aliases.id   integer   NOT NULL   column_default = None
```

`20260924000300_phase2_phase3_missing_tables.sql` declares it `SERIAL PRIMARY KEY`, but the table
that exists in dev never got the sequence. Consequence: **every** `EntityAlias` insert from
`EntityCanonicalizer.get_or_create()` fails, which is why `canonical_entities` and
`entity_aliases` are both empty and the canonicalization path has never run. Fixed by the
idempotent migration; applied to dev and run 3 times consecutively, same result each time:

```
run 1: column_default = nextval('entity_aliases_id_seq'::regclass)
run 2: column_default = nextval('entity_aliases_id_seq'::regclass)
run 3: column_default = nextval('entity_aliases_id_seq'::regclass)
```

Dev is otherwise untouched: `canonical_entities 0, entity_aliases 0, raw_articles 33, stories 26,
reporting_units 26, story_unit_links 26` — identical to before my runs. All canonicalization
verification ran inside a transaction that is always rolled back.

## Deferred

1. **Merging canonical entities that are already fragmented in a database that has rows.** The
   alias table prevents new fragmentation; it does not retroactively join two existing rows. Dev has
   0 rows and production is off limits, so a backfill migration could not be written *and
   verified* here — shipping an unverifiable migration is worse than not shipping it.
2. **Possessive stripping.** Dev surfaces include `Manchester City's` and `Manchester City's` ->
   `Manchester City`, plus `JD Vance's`. `_normalize_text()` removes punctuation but keeps the
   trailing `s`. That is a normalization rule, not alias mapping, and changing `_normalize_text`
   changes every cache key, so it is its own change.
3. **`UAE` labelled ORG by spaCy** in one dev article, so the `("GPE", "UAE", ...)` row does not
   fire for it. Deliberately not patched: adding an ORG row would hide a labelling error rather
   than fix it.
4. **Writing the corrected `detected_language` back to dev.** Verification was a read-only re-run
   over all 33 articles. The only path that writes (`translate_article`) also calls the backend, so
   writing it would spend MyMemory quota; a bespoke UPDATE outside the pipeline would be exactly
   the kind of throwaway code the quality bar forbids.
5. **A confidence floor on detection.** Deliberately not added — measured above, the real misfires
   sit at p=0.857 and p=1.000, so a threshold cannot fix this class and would only add a knob.

## Decisions needed

1. **`20261002000000_entity_aliases_id_default.sql` must be applied to production** before story
   grouping can write anything. Evidence: dev had `id integer NOT NULL` with no default, the
   canonicalizer raised `NotNullViolationError` on the first insert, and both canonical tables are
   empty. Whoever owns prod deployment has to run it; I did not touch prod.
2. **Prod may already hold fragmented canonical entities** ("US" and "United States" as separate
   rows). If it does, a one-off merge migration plus a rewrite of `stories.primary_entities` is
   needed, and it cannot be written safely without seeing prod. Evidence that the class is real:
   dev surfaces carried `US` x4 alongside `United States`, and `Netanyahu` alongside
   `Benjamin Netanyahu`.
3. **Does the alias seed grow?** 34 rows today (11 GPE / 10 ORG / 13 PERSON). The next-highest
   value pair visible in dev is `Man City` / `Manchester City`. The extension point is
   `ALIAS_SEED` in `src/utils/ner.py` and is documented there; nothing else needs to change.
4. **Worktree collision — please read.** Another agent (`.pg-tunnel-c3.py`, `.fails-after.txt`,
   `scripts/migrate.py`) is working in this same worktree concurrently. While taking a baseline I
   stashed my 5 files; by the time I popped them another stash had been pushed on top, so my pop
   applied *theirs* and dropped their stash entry. I then popped the remaining stash, which was
   mine, and committed only my 8 files. Net effect: their work is restored in the working tree as
   unstaged modifications (24 modified/deleted tracked files + 3 untracked), and their stash entry
   is gone — if they were relying on it, it is recoverable from `git fsck --unreachable` /
   `git stash` reflog. Worth separating the batches into separate worktrees.

## Reproduction commands

```bash
set -a; . ./.procmon-dev-env; set +a      # dev credentials, never echoed
psql "host=127.0.0.1 port=15432 user=postgres dbname=postgres" -c "select detected_language, count(*) from raw_articles group by 1"
/home/hatch/.venvs/nptest/bin/python -m pytest tests/test_story_entity_determinism.py tests/test_translation.py tests/test_canonicalization.py tests/test_ner.py tests/test_verification.py tests/test_story_audit.py tests/test_viewpoint_clustering.py tests/test_viewpoint_clustering_idempotency.py tests/test_tiers.py -q
/home/hatch/.venvs/nptest/bin/ruff check src/verification/stories.py src/enrichment/translation.py src/utils/ner.py tests/test_translation.py tests/test_canonicalization.py tests/test_story_entity_determinism.py
```
