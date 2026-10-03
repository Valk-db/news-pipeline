"""Field-level scoring.

The unit of scoring is (article, field), never the document. A regression has to
be attributable to one field of one article, so:

  * predicted snippets are MATCHED to gold snippets first, by token-F1, with a
    greedy maximum-weight assignment. Matching is what lets a field be scored at
    all -- without it, "the model returned three snippets and the gold has three
    snippets" is a coincidence, not a measurement.
  * every field is then scored as a bag of assertions. A gold assertion is
    (gold_snippet, value); a predicted assertion is (matched_gold_snippet,
    predicted_value). Intersections are true positives, predicted-only are false
    positives, gold-only are false negatives. That gives precision, recall and
    F1 per field with the same code path, so a field that is simply always wrong
    reads as 0.000 precision rather than being averaged away.
  * `exact_match` is reported per field too, and it means what it says: for a
    categorical field it is the rate at which a matched snippet's value equals
    the gold value outright; for a set field it is the rate at which the
    predicted set equals the gold set exactly (not approximately).

Fields scored, and why these and not others: they are the four the production
prompt at src/enrichment/snippet_extractor.py:44-60 actually asks for
(``type``, ``entities``, ``confidence``, ``position_estimate``) plus snippet
presence, which is the retrieval question underneath all four -- if the model
does not find the sentence, the four labels cannot be right.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

FIELDS = ("snippet_found", "type", "entities", "confidence", "position")

SNIPPET_TYPES = ("quote", "stat", "fact", "summary", "claim")

# Token F1 at or above this counts as the same snippet. Below it, two snippets
# are different facts, and calling them the same would hide a recall failure.
MATCH_THRESHOLD = 0.50

# Confidence is a 0-100 self-report, so it is scored in bands. Scoring the raw
# integer as an exact value would report near-zero agreement for a field that
# differs by two points, which is noise, not a regression.
CONFIDENCE_BANDS = ((0, 24), (25, 49), (50, 74), (75, 100))

# position_estimate is a 0-1 guess. Quintiles, not exact values.
POSITION_BUCKETS = 5

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "been", "at", "by", "with", "as", "that", "this", "it",
    "its", "from", "has", "have", "had", "but", "not", "no", "de", "la", "el",
    "les", "des", "du", "und", "der", "die", "das", "ein", "eine", "que", "y",
    "il", "che", "di", "il", "per", "non", "son", "con", "una", "un", "por",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return text.casefold().strip()


def tokenize(text: str) -> list[str]:
    return [t for t in _WORD.findall(normalize(text)) if t not in _STOP and len(t) > 1]


def token_f1(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 1.0 if normalize(a) == normalize(b) else 0.0
    ca, cb = Counter(ta), Counter(tb)
    overlap = sum((ca & cb).values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(ta)
    recall = overlap / len(tb)
    return 2 * precision * recall / (precision + recall)


def confidence_band(value: int) -> int:
    for i, (lo, hi) in enumerate(CONFIDENCE_BANDS):
        if lo <= value <= hi:
            return i
    return len(CONFIDENCE_BANDS) - 1


def position_bucket(value: float) -> int:
    return max(0, min(POSITION_BUCKETS - 1, int(float(value) * POSITION_BUCKETS)))


def _norm_set(values: Iterable[Any]) -> set[str]:
    return {normalize(str(v)) for v in (values or []) if str(v).strip()}


@dataclass
class GoldSnippet:
    text: str
    type: str
    entities: list[str] = field(default_factory=list)
    confidence: int = 80
    position: float = 0.5
    note: str = ""


@dataclass
class PredSnippet:
    text: str
    type: str | None = None
    entities: list[str] = field(default_factory=list)
    confidence: int | None = None
    position: float | None = None


def script_profile(text: str) -> Counter:
    """Count characters by Unicode script block, ignoring shared marks.

    Used for one diagnostic only. The prompt asks for "the exact snippet text",
    so a compliant model returns the article's own script; a model that answers a
    Persian or Tamil article in English has not failed the *scoring* of a field,
    it has produced a snippet the `snippets.text` column cannot be trusted with.
    Without this diagnostic that failure is indistinguishable from a matching
    bug, and a broken matcher reads as a broken model.
    """
    counts: Counter = Counter()
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        block = name.split(" ")[0]
        counts[block] += 1
    return counts


def script_overlap(a: str, b: str) -> float:
    """Fraction of `a`'s alphabetic characters whose script also appears in `b`.

    Deliberately one-sided and generous: it answers "did the model answer in the
    source article's script", not "is this a translation of the same sentence".
    """
    ca, cb = script_profile(a), script_profile(b)
    total = sum(ca.values())
    if not total:
        return 1.0
    shared = sum(n for blk, n in ca.items() if cb.get(blk))
    return shared / total


@dataclass
class FieldScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    exact_hit: int = 0
    exact_total: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def exact_match(self) -> float:
        return self.exact_hit / self.exact_total if self.exact_total else 0.0

    def merge(self, other: "FieldScore") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.exact_hit += other.exact_hit
        self.exact_total += other.exact_total

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "exact_match": round(self.exact_match, 4),
            "n": self.exact_total,
        }


def match_snippets(gold: list[GoldSnippet], pred: list[PredSnippet],
                   threshold: float = MATCH_THRESHOLD) -> list[tuple[int, int, float]]:
    """Greedy maximum-weight one-to-one assignment, best pair first.

    One-to-one because a snippet repeated twice is a duplication defect, and a
    scoring rule that let one prediction satisfy two gold snippets would score it
    as two hits.
    """
    pairs: list[tuple[float, int, int]] = []
    for gi, g in enumerate(gold):
        for pi, p in enumerate(pred):
            f1 = token_f1(g.text, p.text)
            if f1 >= threshold:
                pairs.append((f1, gi, pi))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_g: set[int] = set()
    used_p: set[int] = set()
    out: list[tuple[int, int, float]] = []
    for f1, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        out.append((gi, pi, f1))
    out.sort()
    return out


def score_article(gold: list[GoldSnippet], pred: list[PredSnippet]) -> dict[str, FieldScore]:
    """Score one article. Keys are FIELDS; every key is always present."""
    scores = {name: FieldScore() for name in FIELDS}
    pairs = match_snippets(gold, pred)

    # snippet_found: a gold snippet is "found" if a prediction matched it.
    sf = scores["snippet_found"]
    sf.tp = len(pairs)
    sf.fn = len(gold) - len(pairs)
    sf.fp = len(pred) - len(pairs)
    sf.exact_total = len(gold)
    sf.exact_hit = len(pairs)

    for gi, pi, _f1 in pairs:
        g, p = gold[gi], pred[pi]

        # type: categorical, one assertion per matched snippet.
        t = scores["type"]
        g_type = normalize(g.type)
        p_type = normalize(p.type) if p.type else ""
        t.exact_total += 1
        if p_type == g_type:
            t.tp += 1
            t.exact_hit += 1
        else:
            t.fp += 1
            t.fn += 1

        # entities: set-valued, one assertion per distinct entity.
        e = scores["entities"]
        g_ents, p_ents = _norm_set(g.entities), _norm_set(p.entities)
        e.exact_total += 1
        if g_ents == p_ents and (g_ents or not p_ents):
            e.exact_hit += 1
        e.tp += len(g_ents & p_ents)
        e.fp += len(p_ents - g_ents)
        e.fn += len(g_ents - p_ents)

        # confidence: banded integer.
        c = scores["confidence"]
        c.exact_total += 1
        g_band = confidence_band(int(g.confidence))
        p_band = confidence_band(int(p.confidence)) if p.confidence is not None else None
        if p_band == g_band:
            c.tp += 1
            c.exact_hit += 1
        else:
            c.fp += 1
            c.fn += 1

        # position: quintile bucket of a 0-1 estimate.
        po = scores["position"]
        po.exact_total += 1
        g_pos = position_bucket(g.position)
        p_pos = position_bucket(p.position) if p.position is not None else None
        if p_pos == g_pos:
            po.tp += 1
            po.exact_hit += 1
        else:
            po.fp += 1
            po.fn += 1

    return scores


def script_diagnostic(article_text: str, pred: list[PredSnippet]) -> float:
    """Mean fraction of each predicted snippet written in the source article's
    script. 1.0 = every snippet came back in the source language."""
    if not pred:
        return 0.0
    scores = [script_overlap(article_text, p.text) for p in pred]
    return sum(scores) / len(scores)


def aggregate(rows: Iterable[dict[str, FieldScore]]) -> dict[str, FieldScore]:
    total = {name: FieldScore() for name in FIELDS}
    for row in rows:
        for name, s in row.items():
            total[name].merge(s)
    return total


def format_scoreboard(
    per_article: list[tuple[str, str, dict[str, FieldScore]]],
    overall: dict[str, FieldScore],
) -> str:
    """The single printed artifact. One row per (article, field)."""
    lines: list[str] = []
    header = f"{'article':<14} {'stratum':<14} {'field':<14} {'P':>6} {'R':>6} {'F1':>6} {'exact':>6} {'n':>4}"
    lines.append(header)
    lines.append("-" * len(header))
    for article_id, stratum, scores in per_article:
        first = True
        for name in FIELDS:
            s = scores[name]
            label = article_id[:8] if first else ""
            strat = stratum if first else ""
            first = False
            lines.append(
                f"{label:<14} {strat:<14} {name:<14} {s.precision:>6.3f} {s.recall:>6.3f} "
                f"{s.f1:>6.3f} {s.exact_match:>6.3f} {s.exact_total:>4}"
            )
    lines.append("-" * len(header))
    lines.append(f"{'MICRO (all)':<14} {'':<14} {'':<14}")
    for name in FIELDS:
        s = overall[name]
        lines.append(
            f"{'':<14} {'':<14} {name:<14} {s.precision:>6.3f} {s.recall:>6.3f} "
            f"{s.f1:>6.3f} {s.exact_match:>6.3f} {s.exact_total:>4}"
        )
    return "\n".join(lines)


def macro_f1(overall: dict[str, FieldScore]) -> float:
    """Mean F1 across fields. Reported next to micro so one dead field cannot be
    hidden by five good ones (and vice versa)."""
    vals = [overall[f].f1 for f in FIELDS]
    return sum(vals) / len(vals) if vals else 0.0
