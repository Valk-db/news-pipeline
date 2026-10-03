# `eval/` — the gold set and the extraction eval

A frozen, hand-labeled gold set and a field-level scoring harness for the LLM
extraction chain. **Read this before changing any prompt, model id, or extraction
code path.** The point of this directory is that the number exists before the
change, so the change can be judged instead of guessed at.

## What is measured

`extract_snippets_from_article` (`src/enrichment/snippet_extractor.py:16`) — the
per-article LLM snippet extraction that runs as stage 4 of weekly enrichment
(`src/enrichment/pipeline.py:116`, `.github/workflows/weekly-enrichment.yml:60`).

Five fields are scored, and only these five:

| field | what it is | how it is scored |
| --- | --- | --- |
| `snippet_found` | did the model return the sentence at all | P/R/F1 over gold snippets, token-F1 >= 0.50 one-to-one match |
| `type` | one of quote/stat/fact/summary/claim | exact equality, one assertion per matched snippet |
| `entities` | array of entity names | set intersection, plus exact-set-equality rate |
| `confidence` | 0-100 self-report | banded (0-24/25-49/50-74/75-100) |
| `position` | 0-1 estimate | quintile bucket |

`confidence` is banded and `position` is bucketed on purpose. Scoring a 0-100
self-report as an exact integer reports near-zero agreement for a field that moved
by two points, which is noise wearing a number.

**Micro is the headline; macro is printed next to it so one dead field cannot hide
behind four healthy ones.** `macro_f1` is the plain mean of the five field F1s.

## The rule that makes this an eval and not a demo

`src/enrichment/snippet_extractor.py` is called **verbatim**. The harness supplies
exactly one thing, and it is the transport: `get_llm_client` is redirected to
return a `TransportShim` that implements the `LLMClient` surface production
actually calls — `.chat_completion(...)` (awaited) and `._parse_json_response(...)`,
plus `.model` — and funnels into the one seam where the request is really made, so
the record-and-replay cache and the cost accounting still see every call.

There used to be a second substitution, a `STATS` stub, and the shim faked an
`llm.chat` attribute that does not exist. Both existed because production had two
real defects (an un-awaited `get_llm_client`, and `record(..., count=)` against a
signature of `n=`) that made extraction return `[]` for every article. Those are
fixed in the product, so the stubs are gone: stubbing them now would hide
regressions instead of measuring them.

Neither the prompt, the 8000-char truncation, the 20-char minimum, the 300-char
cap, the fence fallback, the field defaults, nor the `max_snippets` slice is
supplied by the harness. Those are all production's, and the harness reads them
from the production module rather than retyping them (`BODY_CHARS` in
`eval/adapter.py` is the one constant, and it names its source line).

## Files

```
eval/
  corpus.py     20 fixed strata, freezes id+url+title+lang+body sha256. Bodies are
                NOT committed; load() re-reads dev and refuses if a body changed.
  gold.json     30 hand-labeled articles, 132 snippets, every one annotated with
                why it was chosen. This is the instrument.
  scoring.py    match_snippets / score_article / aggregate. No I/O, no provider.
  adapter.py    TransportShim: cache lookup, live call, cache write, usage capture.
  mutate.py     8 named corruptions of the model's output. `python -m eval.mutate`.
  run.py        the one command. `python -m eval.run`
  cache.py      record-and-replay, sharded, write-then-rename, corrupt entry = miss
```

## Running it

```bash
set -a; . ~/.config/procmon/supabase-dev.env; . ~/.config/procmon/groq.env; set +a
python -m eval.run                          # warm cache: free, deterministic
python -m eval.run --no-cache               # force live provider calls
python -m eval.run --limit 5                # first 5 labeled articles, smoke test
python -m eval.run --mutate cycle_type      # prove the harness can see a break
python -m eval.run --json out.json          # machine-readable scoreboard
```

`eval/.cache/` holds the recorded responses and is gitignored. **Never commit it.**
It contains LLM completions only, never a credential or a request header.

A cold run costs **30 Groq requests** (~70k tokens at the baseline). The replay
cache makes every subsequent run cost **0**. If you are about to spend 30 requests,
run `--limit 3` first.

## The procedure for every future prompt or model change

This is the part that matters. In order:

1. **Before touching anything**, run `python -m eval.run --json before.json` and
   commit `before.json`. That is the number the change is judged against.
2. Make the change. Do not touch `eval/`. The harness must not be edited to make a
   change look good; if you find yourself wanting to, that is the finding.
3. `python -m eval.run --json after.json`. A prompt or model change invalidates every
   cache entry (the prompt template and the model id are both in the key), so this
   is a genuinely cold run and will spend 30 requests. Budget for it.
4. Compare. The gate is: **no field's macro F1 may fall.** A field that was already
   near 0 may rise; a field above 0.5 may not fall.
5. `python -m eval.run --mutate <name>` still has to show the break you expect. A
   change that makes the harness blind is a regression even if the score rose.

Free-tier ceiling is 20 requests/minute and 1000/day. `eval/run.py` paces live calls
at 20/min for you. The eval uses a throwaway SQLite `budget_counters` so it never
spends the pipeline's own 900/day row.

## What this instrument cannot tell you

Stated up front, and repeated in `gold.json`'s `known_limitations`:

* **Single annotator. No inter-annotator agreement measured.** These are
  single-annotator numbers, not a ceiling. Before any of them is used as a target,
  a second labeller should label 10 of the 30 and a per-field kappa should be
  reported. Until then the honest reading of a low score is "the model and one person
  disagreed", not "the model is wrong".
* **The gold is a judgement.** "The snippets a careful editor would keep" admits
  other defensible answers, so `snippet_found` recall is bounded above by annotator
  agreement rather than by 1.0.
* **Non-English entities are labeled in the source language.** If the model answers
  an article in English, `snippet_found` collapses to 0 for that article. That is a
  real finding about the field, not a matching artifact, and the `script_ovl`
  diagnostic column exists so the two can be told apart. Read it before concluding
  anything about a non-English row.
* **One model, one temperature, one date.** `openai/gpt-oss-20b` at temperature 0.1
  via Groq. These numbers are not a statement about models in general.

## Adding to the gold set

Do not add a label to make a score look better. The procedure that worked:

1. `eval/data/corpus.json` pins the article by id and body sha256. Add the article
   there by re-freezing deliberately, never by hand.
2. Read the **first 8000 characters** of `raw_articles.body_text` — that is the
   model's window, and a label from outside it scores something the model was never
   shown.
3. Write the snippet bytes from the source text, never retyped. For non-Latin
   scripts, selecting by position in a rendered worksheet is the only reliable way;
   retyping Arabic or Malayalam from memory introduces silent character errors that
   look exactly like extraction failures.
4. `position` should be **computed**, not estimated: `window.find(text) / len(window)`.
   A label not literally present in the window is an error, not a low score.
5. `eval/gold.py` hard-fails on: type outside the five the prompt enumerates, text
   under 20 chars (production drops those, so recall would be unreachable), text
   over 300, confidence outside 0-100, position outside 0-1, more than 5 snippets.
6. Every snippet needs a `note` saying why it was chosen. An unannotated choice is
   not a label, and `tests/test_eval_harness.py` enforces it.
7. `tests/test_eval_harness.py` asserts all five types are represented. A gold set
   that cannot express a value the prompt asks for scores that value as always-wrong.

## Tests

```bash
unset NO_PROXY no_proxy      # this VM's NO_PROXY has a literal [::1] that httpx cannot parse
python -m pytest tests/test_eval_harness.py -q
```

70 tests, no network, no key, no database. They cover scoring (including the cases
that would flatter a broken implementation), cache key separation, mutation
application, and gold/corpus validity.
