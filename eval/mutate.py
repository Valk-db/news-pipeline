"""Deliberate breakage of the model's output, to prove the harness can see it.

An eval that cannot detect a regression is not a gate. This module applies named,
documented corruptions to the JSON the model returned, at the transport seam, and
nothing else. The prompt, the truncation, the 300-char cap, the 20-char minimum,
the fence fallback and the field defaults all stay exactly as production has them,
so a score drop is attributable to the corruption and to nothing else.

Two rules make the check honest:

1. A mutation runs AFTER the cache lookup and is never written back to the cache.
   So a mutated run costs zero provider requests and cannot poison later runs.
2. Every mutation states which field it targets and what production does with a
   field it cannot find, because "the field vanished" and "the field fell back to a
   default" are different failures with different scores. The default is not a
   clean -- it is the worst case for that field and the best case for a lazy test,
   which is why a mutation that removes a field is the interesting one.

Run ``python -m eval.mutate`` to list the catalog.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

Mutator = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]

# The five type values the prompt enumerates at snippet_extractor.py:55. Shuffling
# within this set is the honest way to break `type`: a model that invented a value
# would be caught by the gold loader's validation, not by the scorer.
TYPES = ("quote", "stat", "fact", "summary", "claim")


def _parse(content: str) -> list[dict[str, Any]] | None:
    """Parse the same way production does, fence fallback included.

    snippet_extractor.py:74-82 tries bare ``json.loads`` and then a
    ```` ```json ```` fence. A mutator that only understood bare JSON would report
    "mutation did not apply" on a fenced response and quietly prove nothing, so it
    reuses production's two-step parse.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"```json\n(.*?)\n```", content, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def _render(data: list[dict[str, Any]]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _drop_field(field: str) -> Mutator:
    def apply(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.items() if k != field} if isinstance(s, dict) else s
                for s in data]
    return apply


def _collapse_type() -> Mutator:
    def apply(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for s in data:
            if isinstance(s, dict):
                s = dict(s)
                s["type"] = "summary"
            out.append(s)
        return out
    return apply


def _cycle_type() -> Mutator:
    """Rotate every type one slot around the enum.

    A stronger break than collapsing to one value: every snippet still gets a
    plausible type and the count is unchanged, so a harness that only checked
    "did it return the right number of well-formed snippets" would pass this.
    """
    def apply(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for i, s in enumerate(data):
            if isinstance(s, dict) and isinstance(s.get("type"), str) and s["type"] in TYPES:
                s = dict(s)
                s["type"] = TYPES[(TYPES.index(s["type"]) + 1) % len(TYPES)]
            elif isinstance(s, dict):
                s = dict(s)
                s["type"] = TYPES[i % len(TYPES)]
            out.append(s)
        return out
    return apply


def _truncate_text() -> Mutator:
    def apply(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for s in data:
            if isinstance(s, dict):
                s = dict(s)
                s["text"] = str(s.get("text", ""))[:19]
            out.append(s)
        return out
    return apply


def _first_only() -> Mutator:
    def apply(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return data[:1]
    return apply


def _shout_entities() -> Mutator:
    """Keep the entity STRINGS, destroy the field's usefulness.

    Entities are scored as a set (scoring.py tokenizes and compares), so
    lowercasing every entity must not change the entity score at all. If it does,
    the scorer is comparing surfaces rather than entities, which is a bug in the
    scorer. This mutation is therefore also a test of the scorer.
    """
    def apply(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for s in data:
            if isinstance(s, dict):
                s = dict(s)
                s["entities"] = [str(e).upper() for e in (s.get("entities") or [])]
            out.append(s)
        return out
    return apply


# name -> (what it breaks, what production does with the damage, the mutator)
MUTATIONS: dict[str, tuple[str, str, Mutator]] = {
    "drop_entities": (
        "entities",
        "production leaves s['entities'] absent -> Snippet.entities is NULL in the DB",
        _drop_field("entities"),
    ),
    "drop_position": (
        "position",
        "production falls back to s.get('position_estimate', 0.5) -> every snippet "
        "lands mid-article, so a harness that never checked position would not notice",
        _drop_field("position_estimate"),
    ),
    "drop_confidence": (
        "confidence",
        "production falls back to s.get('confidence', 80) -> every snippet lands in "
        "one confidence band",
        _drop_field("confidence"),
    ),
    "collapse_type": (
        "type",
        "every snippet is labelled 'summary'",
        _collapse_type(),
    ),
    "cycle_type": (
        "type",
        "every snippet keeps a plausible type, rotated one slot, so only a real "
        "field comparison can tell",
        _cycle_type(),
    ),
    "truncate_text": (
        "snippet_found",
        "text is cut to 19 chars, below production's 20-char minimum at line 91, so "
        "production drops every snippet and the stage returns []",
        _truncate_text(),
    ),
    "first_only": (
        "snippet_found",
        "only the first snippet survives: precision holds, recall collapses",
        _first_only(),
    ),
    "shout_entities": (
        "none (scorer self-test)",
        "entity strings are uppercased and nothing else changes; the entity score "
        "must be IDENTICAL to baseline or the scorer is comparing surfaces",
        _shout_entities(),
    ),
}

# Mutations expected to leave every field score unchanged. A harness that cannot
# pass these is measuring the wrong thing.
SCORER_SELF_TESTS = ("shout_entities",)


def apply_mutation(name: str, content: str) -> tuple[str, str | None]:
    """Return (content_to_score, note). ``note`` is None if nothing was applied.

    A mutation that could not parse the response is reported as not applied rather
    than silently passing through the baseline unchanged, because "the mutation did
    nothing" and "the mutation broke nothing" are different sentences and only one
    of them is evidence.
    """
    if name not in MUTATIONS:
        raise KeyError(f"unknown mutation {name!r}; known: {', '.join(sorted(MUTATIONS))}")
    field, consequence, fn = MUTATIONS[name]
    data = _parse(content)
    if data is None:
        return content, None
    mutated = fn(data)
    if mutated == data:
        return content, f"{name}: no snippet carried {field!r}, so nothing changed"
    return _render(mutated), f"{name}: {len(data)} -> {len(mutated)} snippets, {consequence}"


def main(argv: list[str] | None = None) -> int:
    print("mutation catalog (applied after the cache lookup, never cached back):\n")
    for name, (field, consequence, _) in sorted(MUTATIONS.items()):
        tag = "  [scorer self-test: score MUST NOT change]" if name in SCORER_SELF_TESTS else ""
        print(f"  {name:<18} field={field:<14} {consequence}{tag}")
    print("\nuse: python -m eval.run --mutate <name>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
