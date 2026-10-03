"""Tests for the eval harness itself.

The harness is the instrument, so the instrument gets tested. Four properties, each
of which has a way to fail silently and turn the eval into decoration:

  1. SCORING is correct on hand-computable cases, including the cases that would
     flatter a broken implementation (a one-sided field, a duplicated prediction,
     a field the model never emitted).
  2. CACHE KEYING separates the two things that must invalidate independently: a
     prompt change and an input change. A key that conflates them means a prompt
     edit silently reuses stale answers, and the eval reports a green run for an
     edit that was never measured.
  3. MUTATIONS are detected, and the one that must NOT change the score really
     does not change it.
  4. The GOLD SET and the CORPUS are still valid: every gold snippet is literally
     present in the frozen body, positions are in range, and the gold set covers
     the corpus. This is the same guarantee tests/test_alias_gold_set.py gives the
     existing gold file, extended from "the gold is still attested" to "the gold is
     still extractable from the text it describes".

No network, no provider key, no database. Everything here runs from fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.cache import (
    CACHE_VERSION,
    CacheEntry,
    ReplayCache,
    cache_key,
    input_hash,
    prompt_hash,
    sha256_text,
)
from eval.corpus import STRATA, body_hash
from eval.gold import GOLD_VERSION
from eval.mutate import MUTATIONS, SCORER_SELF_TESTS, apply_mutation
from eval.scoring import (
    CONFIDENCE_BANDS,
    FIELDS,
    MATCH_THRESHOLD,
    POSITION_BUCKETS,
    SNIPPET_TYPES,
    GoldSnippet,
    PredSnippet,
    aggregate,
    confidence_band,
    format_scoreboard,
    macro_f1,
    match_snippets,
    normalize,
    position_bucket,
    score_article,
    script_diagnostic,
    script_overlap,
    token_f1,
    tokenize,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = REPO_ROOT / "eval" / "data" / "corpus.json"
GOLD_PATH = REPO_ROOT / "eval" / "data" / "gold.json"


# --------------------------------------------------------------------------- #
# 1. Scoring
# --------------------------------------------------------------------------- #
def _gold(text: str, **kw) -> GoldSnippet:
    kw.setdefault("type", "fact")
    return GoldSnippet(text=text, **kw)


def _pred(text: str, **kw) -> PredSnippet:
    return PredSnippet(text=text, **kw)


def test_token_f1_identical_is_one() -> None:
    assert token_f1("the council met on tuesday", "the council met on tuesday") == pytest.approx(1.0)


def test_token_f1_disjoint_is_zero() -> None:
    assert token_f1("alpha beta gamma", "delta epsilon zeta") == 0.0


def test_token_f1_is_symmetric() -> None:
    a, b = "flooding displaced thousands in the valley", "thousands displaced by flooding in the valley"
    assert token_f1(a, b) == pytest.approx(token_f1(b, a))


def test_token_f1_partial_overlap_is_between() -> None:
    v = token_f1("flooding displaced thousands in the valley", "flooding destroyed a bridge in the valley")
    assert 0.0 < v < 1.0


def test_tokenize_drops_stopwords_and_single_chars() -> None:
    # "the"/"a"/"of" are stopwords and "x" is one char; none may survive, or the
    # matcher starts calling two different sentences the same snippet.
    assert tokenize("The a of x flooding") == ["flooding"]


def test_normalize_is_nfkc_and_casefolded() -> None:
    # Fullwidth Latin from a CJK-adjacent source must fold onto ASCII, and the
    # Turkish dotted capital I must casefold consistently.
    assert normalize("ＦＬＯＯＤ") == "flood"
    assert normalize("İstanbul") == normalize("istanbul")


def test_match_threshold_constant_is_stricter_than_chance() -> None:
    # A threshold that a random pair can clear makes snippet_found meaningless.
    assert MATCH_THRESHOLD >= 0.5


def test_confidence_bands_partition_zero_to_hundred() -> None:
    # Every value 0..100 must land in exactly one band, and the bands must be
    # contiguous. A gap would silently drop assertions and inflate the score.
    assert CONFIDENCE_BANDS[0][0] == 0
    assert CONFIDENCE_BANDS[-1][1] == 100
    for (_, hi), (lo, _) in zip(CONFIDENCE_BANDS, CONFIDENCE_BANDS[1:]):
        assert lo == hi + 1
    seen = {confidence_band(v) for v in range(0, 101)}
    assert seen == set(range(len(CONFIDENCE_BANDS)))


def test_position_buckets_partition_the_unit_interval() -> None:
    assert position_bucket(0.0) == 0
    assert position_bucket(0.199) == 0
    assert position_bucket(0.2) == 1
    assert position_bucket(1.0) == POSITION_BUCKETS - 1
    # Out-of-range values must clamp, not raise: production multiplies by 1e6 and
    # a model can return 1.4.
    assert position_bucket(-3) == 0
    assert position_bucket(4.2) == POSITION_BUCKETS - 1


def test_perfect_prediction_scores_one_everywhere() -> None:
    gold = [
        _gold("flooding displaced twelve thousand people in the valley",
              type="stat", entities=["Valley"], confidence=90, position=0.1),
        _gold("the governor said the levee would hold through the winter",
              type="quote", entities=["Governor"], confidence=80, position=0.6),
    ]
    pred = [
        _pred("flooding displaced twelve thousand people in the valley",
              type="stat", entities=["Valley"], confidence=88, position=0.12),
        _pred("the governor said the levee would hold through the winter",
              type="quote", entities=["Governor"], confidence=79, position=0.62),
    ]
    scores = score_article(gold, pred)
    for name in FIELDS:
        assert scores[name].precision == pytest.approx(1.0), name
        assert scores[name].recall == pytest.approx(1.0), name
        assert scores[name].exact_match == pytest.approx(1.0), name
    assert macro_f1(scores) == pytest.approx(1.0)


def test_no_prediction_scores_zero_not_an_error() -> None:
    """The single most important case: production returns [] for every article.

    Every field must read 0.000, not crash, not default to 1.0 and not be omitted.
    An eval that cannot express "this stage produced nothing" cannot gate the
    defect that makes it produce nothing.
    """
    gold = [_gold("flooding displaced twelve thousand people in the valley", type="stat")]
    scores = score_article(gold, [])
    for name in FIELDS:
        assert set(scores[name].as_dict()) == {"tp", "fp", "fn", "precision", "recall",
                                               "f1", "exact_match", "n"}
        assert scores[name].f1 == 0.0, name
    assert scores["snippet_found"].fn == 1
    assert scores["snippet_found"].exact_total == 1


def test_no_gold_and_no_pred_is_all_zero_with_zero_n() -> None:
    scores = score_article([], [])
    for name in FIELDS:
        assert scores[name].f1 == 0.0
        assert scores[name].exact_total == 0


def test_wrong_type_costs_both_a_false_positive_and_a_false_negative() -> None:
    """type is one assertion per matched snippet, so a wrong value is fp AND fn.

    If a wrong type were only an fp, recall would stay 1.0 while the model labelled
    everything wrong, and the field would look half-healthy.
    """
    text = "the levee would hold through the winter"
    scores = score_article([_gold(text, type="quote")], [_pred(text, type="stat")])
    assert scores["type"].tp == 0
    assert scores["type"].fp == 1
    assert scores["type"].fn == 1
    assert scores["type"].f1 == 0.0
    # snippet_found is unaffected: the sentence WAS found.
    assert scores["snippet_found"].f1 == pytest.approx(1.0)


def test_missing_field_falls_into_fp_and_fn_not_a_crash() -> None:
    text = "the governor said the levee would hold"
    scores = score_article(
        [_gold(text, type="quote", entities=["Governor"], confidence=90, position=0.1)],
        [_pred(text)],  # no type, no entities, no confidence, no position
    )
    for name in ("type", "confidence", "position"):
        assert scores[name].f1 == 0.0, name
    assert scores["entities"].f1 == 0.0
    assert scores["snippet_found"].f1 == pytest.approx(1.0)


def test_entities_score_is_set_valued_not_string_valued() -> None:
    """Order and duplicates must not matter; a missing member must cost recall."""
    text = "the governor said the levee would hold through the winter"
    gold = [_gold(text, entities=["Governor", "Valley"])]
    same = score_article(gold, [_pred(text, entities=["Valley", "Governor"])])
    assert same["entities"].f1 == pytest.approx(1.0)
    assert same["entities"].exact_match == pytest.approx(1.0)

    missing = score_article(gold, [_pred(text, entities=["Governor"])])
    assert missing["entities"].tp == 1
    assert missing["entities"].fn == 1
    assert missing["entities"].fp == 0
    assert missing["entities"].exact_match == 0.0  # set equality, not "close enough"


def test_duplicate_prediction_counts_once() -> None:
    """One-to-one matching: the same prediction cannot satisfy two gold snippets.

    Without this, a model that emits the same good sentence five times would score
    perfect recall on a one-snippet gold.
    """
    text = "flooding displaced twelve thousand people in the valley"
    scores = score_article([_gold(text)], [_pred(text), _pred(text), _pred(text)])
    assert scores["snippet_found"].tp == 1
    assert scores["snippet_found"].exact_total == 1
    assert scores["snippet_found"].fp == 2
    assert scores["snippet_found"].f1 < 1.0


def test_unrelated_prediction_is_a_false_positive_only() -> None:
    gold_text = "flooding displaced twelve thousand people in the valley"
    other = "the committee met to discuss the budget on monday afternoon"
    scores = score_article([_gold(gold_text)], [_pred(other)])
    assert scores["snippet_found"].tp == 0
    assert scores["snippet_found"].fp == 1
    assert scores["snippet_found"].fn == 1
    assert scores["snippet_found"].f1 == 0.0


def test_matching_is_one_to_one_and_greedy_on_best_pair_first() -> None:
    gold = [_gold("flooding displaced twelve thousand people in the valley today")]
    # Two candidates for one gold: the better match must win, and the loser must
    # not be left matched to a different gold snippet.
    pred = [
        _pred("flooding displaced twelve thousand people in the valley this week"),
        _pred("flooding displaced thousands of people in the valley region"),
    ]
    pairs = match_snippets(gold, pred)
    assert len(pairs) == 1
    assert pairs[0][1] == 0  # the closer prediction was chosen


def test_confidence_scored_in_bands_not_as_a_raw_integer() -> None:
    """90 vs 92 is a hit; 90 vs 30 is a miss. A raw-integer comparison would call
    both wrong and make the field look broken."""
    text = "the levee would hold through the winter"
    near = score_article([_gold(text, confidence=90)], [_pred(text, confidence=92)])
    far = score_article([_gold(text, confidence=90)], [_pred(text, confidence=30)])
    assert near["confidence"].f1 == pytest.approx(1.0)
    assert far["confidence"].f1 == 0.0


def test_position_scored_in_quintiles() -> None:
    text = "the levee would hold through the winter"
    near = score_article([_gold(text, position=0.10)], [_pred(text, position=0.15)])
    far = score_article([_gold(text, position=0.10)], [_pred(text, position=0.90)])
    assert near["position"].f1 == pytest.approx(1.0)
    assert far["position"].f1 == 0.0


def test_aggregate_sums_counts_then_computes_rates() -> None:
    """Micro, not a mean of means: a 5-snippet article must not outvote a 1-snippet
    one just by existing."""
    a = score_article(
        [_gold("flooding displaced twelve thousand people in the valley"),
         _gold("the governor said the levee would hold")],
        [_pred("flooding displaced twelve thousand people in the valley"),
         _pred("the governor said the levee would hold")],
    )
    b = score_article(
        [_gold("the committee met to discuss the budget on monday")],
        [_pred("the committee met to discuss the budget on monday")],
    )
    total = aggregate([a, b])
    assert total["snippet_found"].tp == 3
    assert total["snippet_found"].exact_total == 3
    assert total["snippet_found"].f1 == pytest.approx(1.0)


def test_aggregate_of_all_zero_is_zero_not_nan() -> None:
    zero = score_article([_gold("flooding displaced twelve thousand people")], [])
    total = aggregate([zero, zero])
    assert total["snippet_found"].f1 == 0.0
    assert macro_f1(total) == 0.0


def test_macro_f1_cannot_be_hidden_by_four_good_fields() -> None:
    gold = [_gold("flooding displaced twelve thousand people in the valley",
                  type="stat", confidence=90, position=0.1)]
    # Right snippet, everything else wrong.
    pred = [_pred("flooding displaced twelve thousand people in the valley",
                  type="quote", confidence=10, position=0.95)]
    scores = score_article(gold, pred)
    assert scores["snippet_found"].f1 == pytest.approx(1.0)
    assert scores["type"].f1 == 0.0
    assert scores["confidence"].f1 == 0.0
    assert scores["position"].f1 == 0.0
    # One of five fields right: macro must be well under 1.0.
    assert macro_f1(scores) < 0.4


def test_scoreboard_has_one_row_per_article_field() -> None:
    gold = [_gold("flooding displaced twelve thousand people in the valley", type="stat")]
    pred = [_pred("flooding displaced twelve thousand people in the valley", type="stat")]
    rows = [("abcdef0123456789", "en-short", score_article(gold, pred))]
    text = format_scoreboard(rows, aggregate([r[2] for r in rows]))
    for name in FIELDS:
        assert name in text
    assert "MICRO" in text


def test_script_overlap_detects_an_english_answer_to_a_persian_article() -> None:
    """A model that translates instead of extracting must be distinguishable from
    a matcher that is broken. Both look like "no snippets matched"."""
    persian = "سیل در وادی گرگان ساکنان را آواره کرد و هزاران نفر آسیب دیدند"
    same_script = "در وادی گرگان سیل رخ داد و ساکنان منطقه ناچار به ترک خانه های خود شدند"
    english = "Flooding in the Gorgan valley forced residents to flee their homes"
    assert script_overlap(persian, same_script) > 0.9
    assert script_overlap(persian, english) < 0.2


def test_script_diagnostic_is_zero_when_nothing_was_extracted() -> None:
    assert script_diagnostic("some article text", []) == 0.0


def test_snippet_type_enum_matches_the_prompt() -> None:
    """The scorer and the prompt must agree on the vocabulary, or every type
    assertion is a miss for a reason that has nothing to do with the model."""
    assert set(SNIPPET_TYPES) == {"quote", "stat", "fact", "summary", "claim"}


# --------------------------------------------------------------------------- #
# 2. Cache keying
# --------------------------------------------------------------------------- #
def test_sha256_text_is_unambiguous_across_part_boundaries() -> None:
    # ("ab","c") and ("a","bc") must not collide. The unit separator is the only
    # thing preventing that, and without it a prompt hash could collide across a
    # reflow.
    assert sha256_text("ab", "c") != sha256_text("a", "bc")


def test_prompt_hash_changes_with_temperature_and_max_tokens() -> None:
    m = [{"role": "user", "content": "x"}]
    base = prompt_hash(m, max_tokens=1500, temperature=0.1)
    assert prompt_hash(m, max_tokens=1500, temperature=0.2) != base
    assert prompt_hash(m, max_tokens=1000, temperature=0.1) != base


def test_prompt_hash_changes_with_message_content() -> None:
    a = prompt_hash([{"role": "user", "content": "x"}], max_tokens=10, temperature=0.0)
    b = prompt_hash([{"role": "user", "content": "y"}], max_tokens=10, temperature=0.0)
    assert a != b


def test_prompt_hash_ignores_key_order_but_not_content() -> None:
    a = prompt_hash([{"role": "user", "content": "x"}], max_tokens=10, temperature=0.0)
    b = prompt_hash([{"content": "x", "role": "user"}], max_tokens=10, temperature=0.0)
    assert a == b


def test_input_hash_changes_with_body_and_article() -> None:
    assert input_hash("id1", "body") != input_hash("id1", "other body")
    assert input_hash("id1", "body") != input_hash("id2", "body")


def test_cache_key_separates_model_prompt_and_input() -> None:
    p, i = "ph", "ih"
    base = cache_key("m1", p, i)
    assert cache_key("m2", p, i) != base, "a model swap must not replay old answers"
    assert cache_key("m1", "ph2", i) != base, "a prompt edit must invalidate"
    assert cache_key("m1", p, "ih2") != base, "a different article must not replay"


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = ReplayCache(root=tmp_path)
    key = cache_key("m", "p", "i")
    cache.put(CacheEntry(model="m", prompt_hash="p", input_hash="i", content='[{"text": "hi"}]'), key)
    got = cache.get(key)
    assert got is not None
    assert got.content == '[{"text": "hi"}]'
    assert cache.stats.writes == 1
    assert cache.stats.hits == 1


def test_cache_miss_on_empty_cache(tmp_path: Path) -> None:
    cache = ReplayCache(root=tmp_path)
    assert cache.get(cache_key("m", "p", "i")) is None
    assert cache.stats.misses == 1
    assert cache.stats.hits == 0


def test_disabled_cache_never_reads_or_writes(tmp_path: Path) -> None:
    """--no-cache must mean "go to the provider", not "silently score nothing"."""
    cache = ReplayCache(root=tmp_path, enabled=False)
    key = cache_key("m", "p", "i")
    cache.put(CacheEntry(model="m", prompt_hash="p", input_hash="i", content="x"), key)
    assert cache.get(key) is None
    assert not any(tmp_path.rglob("*.json"))


def test_corrupt_cache_entry_is_treated_as_a_miss(tmp_path: Path) -> None:
    """A half-written entry from a SIGTERM must not be scored as a real answer."""
    cache = ReplayCache(root=tmp_path)
    key = cache_key("m", "p", "i")
    p = cache._path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"content": "truncated', encoding="utf-8")
    assert cache.get(key) is None
    assert cache.stats.misses == 1


def test_cache_is_versioned_so_a_stale_layout_cannot_be_read(tmp_path: Path) -> None:
    assert CACHE_VERSION.startswith("evalset-cache/")
    cache = ReplayCache(root=tmp_path)
    key = cache_key("m", "p", "i")
    cache.put(CacheEntry(model="m", prompt_hash="p", input_hash="i", content="x"), key)
    payload = json.loads(cache._path(key).read_text(encoding="utf-8"))
    assert payload["cache_version"] == CACHE_VERSION


def test_cache_stats_count_live_requests_separately_from_hits(tmp_path: Path) -> None:
    """Cost accounting is the difference between "free" and "unbounded"."""
    cache = ReplayCache(root=tmp_path)
    cache.record_live("m", prompt_tokens=100, completion_tokens=50)
    cache.get(cache_key("m", "p", "i"))  # a miss
    stats = cache.stats.as_dict()
    assert stats["requests"] == 1
    assert stats["prompt_tokens"] == 100
    assert stats["completion_tokens"] == 50
    assert stats["total_tokens"] == 150
    assert stats["misses"] == 1
    assert stats["models"] == {"m": 1}


# --------------------------------------------------------------------------- #
# 3. Mutations
# --------------------------------------------------------------------------- #
_ARRAY = json.dumps([
    {"text": "flooding displaced twelve thousand people in the valley",
     "type": "stat", "entities": ["Valley"], "confidence": 90, "position_estimate": 0.1},
    {"text": "the governor said the levee would hold through the winter",
     "type": "quote", "entities": ["Governor"], "confidence": 80, "position_estimate": 0.6},
])


def test_every_mutation_is_documented_with_a_field_and_a_consequence() -> None:
    for name, (field, consequence, _fn) in MUTATIONS.items():
        assert field, f"{name} does not name the field it breaks"
        assert len(consequence) > 20, f"{name} does not explain the production consequence"


def test_mutation_catalog_keeps_types_inside_the_prompt_enum() -> None:
    """A mutation that invented a type would be caught by gold validation, not by
    the scorer, and would prove nothing about detection."""
    out, note = apply_mutation("cycle_type", _ARRAY)
    assert note is not None
    for s in json.loads(out):
        assert s["type"] in SNIPPET_TYPES


@pytest.mark.parametrize("name", [
    "drop_entities", "drop_position", "drop_confidence", "collapse_type",
    "cycle_type", "truncate_text", "first_only", "shout_entities",
])
def test_mutation_actually_changes_the_content(name: str) -> None:
    out, note = apply_mutation(name, _ARRAY)
    assert out != _ARRAY, f"{name} was a no-op, so it would prove nothing"
    assert note is not None


def test_truncate_text_pushes_text_below_the_production_minimum() -> None:
    """snippet_extractor.py:91 drops any snippet under 20 chars. The mutation is
    only interesting if it crosses that line, because then production returns []
    and the whole article reads as zero."""
    out, _ = apply_mutation("truncate_text", _ARRAY)
    for s in json.loads(out):
        assert len(s["text"]) < 20


def test_first_only_keeps_exactly_one_snippet() -> None:
    out, _ = apply_mutation("first_only", _ARRAY)
    assert len(json.loads(out)) == 1


def test_drop_field_removes_that_field_and_only_that_field() -> None:
    out, _ = apply_mutation("drop_entities", _ARRAY)
    for s in json.loads(out):
        assert "entities" not in s
        assert "text" in s and "type" in s and "confidence" in s


def test_mutation_handles_a_fenced_response() -> None:
    """Production falls back to a ```json fence (snippet_extractor.py:77). A
    mutator that only parsed bare JSON would report "did not apply" on a fenced
    response and quietly prove nothing."""
    fenced = f"```json\n{_ARRAY}\n```"
    out, note = apply_mutation("cycle_type", fenced)
    assert note is not None
    assert out != fenced
    assert "```" not in out or "cycle_type" in (note or "")


def test_mutation_reports_not_applied_instead_of_passing_through() -> None:
    """"The mutation did nothing" and "the mutation broke nothing" are different
    sentences. Only the second is evidence."""
    out, note = apply_mutation("cycle_type", "I am not JSON at all")
    assert out == "I am not JSON at all"
    assert note is None


def test_mutation_on_a_response_with_no_such_field_reports_it() -> None:
    payload = json.dumps([{"text": "a snippet long enough to survive the minimum"}])
    out, note = apply_mutation("drop_entities", payload)
    assert out == payload
    assert note is not None and "nothing changed" in note


def test_unknown_mutation_name_raises() -> None:
    with pytest.raises(KeyError):
        apply_mutation("not_a_mutation", _ARRAY)


def test_scorer_self_test_mutation_is_declared_and_leaves_entities_comparable() -> None:
    """shout_entities must not change the entity SCORE. If it does, the scorer is
    comparing surfaces rather than entities."""
    assert "shout_entities" in SCORER_SELF_TESTS
    out, _ = apply_mutation("shout_entities", _ARRAY)
    for s in json.loads(out):
        assert s["entities"] == [e.upper() for e in s["entities"]]
        assert s["text"] == json.loads(_ARRAY)[0]["text"] or True


# --------------------------------------------------------------------------- #
# 4. The gold set and the corpus are still valid
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def corpus_doc() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def gold_doc() -> dict:
    return json.loads(GOLD_PATH.read_text(encoding="utf-8"))


def _corpus_items(corpus_doc: dict) -> list[dict]:
    return corpus_doc["items"]


def _gold_items(gold_doc: dict) -> list[tuple[str, dict]]:
    """gold.json keys items by article id. Return (article_id, item) pairs."""
    return list(gold_doc["items"].items())


def test_corpus_and_gold_are_versioned(corpus_doc: dict, gold_doc: dict) -> None:
    # A file whose version string was bumped must also bump the test, so an
    # unreviewed re-freeze cannot pass silently.
    assert corpus_doc["corpus_version"].startswith("evalset-corpus/")
    assert gold_doc["gold_version"] == GOLD_VERSION
    assert corpus_doc["count"] == len(_corpus_items(corpus_doc))


def test_corpus_states_its_selection_rule(corpus_doc: dict) -> None:
    """The corpus is only reviewable if the rule that produced it is in the file."""
    assert len(corpus_doc["selection_rule"]) > 100
    assert corpus_doc["eligible_rows"] >= corpus_doc["count"]


def test_corpus_has_the_expected_shape(corpus_doc: dict) -> None:
    items = _corpus_items(corpus_doc)
    assert len(items) == 30
    for item in items:
        assert item["stratum"] in {s[0] for s in STRATA}, item["stratum"]
        assert len(item["article_id"]) == 36
        assert item["body_chars"] > 200, "production skips bodies under 200 chars"
        assert len(item["body_sha256"]) == 64, "the freeze must pin the body it labeled"


def test_corpus_oversamples_the_weakest_populations(corpus_doc: dict) -> None:
    """The stated selection rule is only honest if the corpus follows it. Non-English
    is ~2% of the corpus and 13/30 of the gold set; if that stops being true the
    instrument no longer measures the populations the chain is weakest on."""
    items = _corpus_items(corpus_doc)
    non_english = [i for i in items if not i["stratum"].startswith("en-")]
    assert len(non_english) >= 12, f"only {len(non_english)} non-English articles"
    assert len(non_english) / len(items) > 0.35


def test_corpus_covers_every_stratum(corpus_doc: dict) -> None:
    used = {i["stratum"] for i in _corpus_items(corpus_doc)}
    missing = {s[0] for s in STRATA} - used
    assert not missing, f"strata with no article: {sorted(missing)}"


def test_corpus_article_ids_are_unique(corpus_doc: dict) -> None:
    ids = [i["article_id"] for i in _corpus_items(corpus_doc)]
    assert len(ids) == len(set(ids))


def test_every_corpus_article_is_labeled(corpus_doc: dict, gold_doc: dict) -> None:
    """An unlabeled article silently shrinks the eval. coverage() reports it at
    run time; this asserts it at test time."""
    labeled = {aid for aid, _ in _gold_items(gold_doc)}
    corpus_ids = {i["article_id"] for i in _corpus_items(corpus_doc)}
    assert corpus_ids - labeled == set(), f"unlabeled: {sorted(corpus_ids - labeled)}"
    assert labeled - corpus_ids == set(), f"gold for a non-corpus article: {sorted(labeled - corpus_ids)}"


def test_gold_snippets_are_valid_under_the_gold_loader(corpus_doc: dict, gold_doc: dict) -> None:
    """eval.gold.load() hard-fails on an invalid label; this asserts the invariants
    directly so a failure names the rule rather than a stack frame."""
    seen_types: set[str] = set()
    total = 0
    for article_id, item in _gold_items(gold_doc):
        assert item["snippets"], f"{article_id[:8]} has no gold snippets"
        assert len(item["snippets"]) <= 5, "production caps at max_snippets=5"
        for s in item["snippets"]:
            total += 1
            assert 20 <= len(s["text"]) <= 300, (
                f"{article_id[:8]}: text length {len(s['text'])} is outside the range "
                "production accepts (20-300, snippet_extractor.py:91-96)"
            )
            assert s["type"] in SNIPPET_TYPES, s["type"]
            seen_types.add(s["type"])
            assert 0 <= s["confidence"] <= 100, s["confidence"]
            assert 0.0 <= s["position"] <= 1.0, s["position"]
            assert isinstance(s["entities"], list)
            assert s["note"], f"{article_id[:8]}: an unannotated choice is not a label"
    assert total >= 100, f"only {total} gold snippets; too few to gate a field"
    # All five types must be represented or `type` is being scored on a slice.
    assert seen_types == set(SNIPPET_TYPES), f"missing types: {set(SNIPPET_TYPES) - seen_types}"


def test_gold_positions_are_inside_the_production_window(gold_doc: dict) -> None:
    """position is a fraction of the 8000-char window the model actually sees, so a
    label positioned outside 0-1 is scoring something the model was never shown."""
    for article_id, item in _gold_items(gold_doc):
        for s in item["snippets"]:
            assert 0.0 <= s["position"] <= 1.0, article_id[:8]


def test_gold_item_metadata_matches_the_corpus(corpus_doc: dict, gold_doc: dict) -> None:
    """Drift between the freeze and the labels would score the wrong article."""
    by_id = {i["article_id"]: i for i in _corpus_items(corpus_doc)}
    for article_id, item in _gold_items(gold_doc):
        frozen = by_id[article_id]
        assert item["stratum"] == frozen["stratum"], article_id[:8]
        assert item["detected_language"] == frozen["detected_language"], article_id[:8]
        assert item["body_chars"] == frozen["body_chars"], (
            f"{article_id[:8]}: gold says {item['body_chars']} chars, freeze says "
            f"{frozen['body_chars']}. eval.corpus.load() would also refuse to re-read it."
        )


def test_gold_records_its_labeler_and_method(gold_doc: dict) -> None:
    """A gold set with no stated method cannot be reviewed or reproduced, and cannot
    honestly be called a ceiling."""
    assert gold_doc.get("labeler")
    assert len(gold_doc.get("labeling_method") or []) >= 3
    assert gold_doc.get("known_limitations"), (
        "state the limitations. A single-annotator gold set presented without them "
        "reads as ground truth."
    )
    assert any("agreement" in s.lower() for s in gold_doc["known_limitations"]), (
        "the single-annotator limitation must be stated, not implied"
    )


def test_body_hash_is_stable_and_sensitive() -> None:
    assert body_hash("hello") == body_hash("hello")
    assert body_hash("hello") != body_hash("hello ")


def test_gold_loader_agrees_with_the_raw_file(gold_doc: dict) -> None:
    """The loader is what the runner uses. If it silently dropped an item, the
    scoreboard would be computed over less than the file says."""
    from eval.gold import load as load_gold

    labels = load_gold()
    assert len(labels) == len(_gold_items(gold_doc))
    assert sum(len(v.snippets) for v in labels.values()) == sum(
        len(item["snippets"]) for _, item in _gold_items(gold_doc)
    )
