"""Loader and validator for the hand labels.

The labels live in ``eval/data/gold.json`` and are keyed by the frozen corpus
article id, never by url or title, because those change. A label that fails
validation is a hard error rather than a warning: a silently-dropped label turns
into a silently-inflated score, which is the one failure mode an eval cannot
have.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.scoring import SNIPPET_TYPES, GoldSnippet

GOLD_PATH = Path(__file__).resolve().parent / "data" / "gold.json"
GOLD_VERSION = "evalset-gold/v1"


@dataclass
class GoldLabel:
    article_id: str
    snippets: list[dict[str, Any]]
    labeler_notes: str = ""
    labeled_from: str = "article text as stored in dev raw_articles, first 8000 chars (the model's window)"

    def as_gold_snippets(self) -> list[GoldSnippet]:
        return [GoldSnippet(**s) for s in self.snippets]


def _validate(item_id: str, raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError(f"{item_id}: gold snippets must be a list, got {type(raw).__name__}")
    out: list[dict[str, Any]] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ValueError(f"{item_id}[{i}]: snippet must be an object")
        missing = [k for k in ("text", "type") if k not in s]
        if missing:
            raise ValueError(f"{item_id}[{i}]: missing {missing}")
        stype = str(s["type"]).strip().lower()
        if stype not in SNIPPET_TYPES:
            raise ValueError(
                f"{item_id}[{i}]: type {stype!r} is not one of {SNIPPET_TYPES}. The gold set "
                "may not invent a label the production prompt does not ask for."
            )
        text = str(s["text"]).strip()
        if len(text) < 20:
            raise ValueError(
                f"{item_id}[{i}]: text is {len(text)} chars. The production extractor drops "
                "snippets under 20 chars (snippet_extractor.py:91), so a gold snippet that "
                "short could never be returned and would make recall unreachable."
            )
        conf = int(s.get("confidence", 80))
        if not 0 <= conf <= 100:
            raise ValueError(f"{item_id}[{i}]: confidence {conf} outside 0-100")
        pos = float(s.get("position", 0.5))
        if not 0.0 <= pos <= 1.0:
            raise ValueError(f"{item_id}[{i}]: position {pos} outside 0-1")
        ents = s.get("entities", [])
        if not isinstance(ents, list):
            raise ValueError(f"{item_id}[{i}]: entities must be a list")
        out.append(
            {
                "text": text,
                "type": stype,
                "entities": [str(e) for e in ents],
                "confidence": conf,
                "position": pos,
                "note": str(s.get("note", "")),
            }
        )
    return out


def load(path: Path | str = GOLD_PATH) -> dict[str, GoldLabel]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    version = data.get("gold_version")
    if version != GOLD_VERSION:
        raise ValueError(f"gold_version {version!r} != {GOLD_VERSION!r}")
    labels: dict[str, GoldLabel] = {}
    for item_id, raw in data["items"].items():
        labels[item_id] = GoldLabel(
            article_id=item_id,
            snippets=_validate(item_id, raw.get("snippets", [])),
            labeler_notes=raw.get("labeler_notes", ""),
            labeled_from=raw.get("labeled_from", ""),
        )
    return labels


def coverage(gold: dict[str, GoldLabel], corpus_ids: list[str]) -> dict[str, Any]:
    labeled = [i for i in corpus_ids if i in gold]
    return {
        "corpus": len(corpus_ids),
        "labeled": len(labeled),
        "unlabeled": len(corpus_ids) - len(labeled),
        "gold_snippets": sum(len(gold[i].snippets) for i in labeled),
    }
