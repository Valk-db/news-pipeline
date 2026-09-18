"""Named entity extraction for story grouping."""

import spacy
from typing import List, Dict, Set
from functools import lru_cache

# Load spaCy model (download with: python -m spacy download en_core_web_sm)
@lru_cache
def get_nlp():
    return spacy.load("en_core_web_sm")


ENTITY_LABELS = {"PERSON", "ORG", "GPE", "LOC", "EVENT", "PRODUCT"}


def extract_entities(text: str, top_n: int = 3) -> Dict[str, List[str]]:
    """
    Extract named entities from text.
    Returns dict of label -> list of entity texts (top N by frequency).
    """
    nlp = get_nlp()
    doc = nlp(text)

    entities_by_label: Dict[str, Dict[str, int]] = {label: {} for label in ENTITY_LABELS}

    for ent in doc.ents:
        if ent.label_ in ENTITY_LABELS:
            entities_by_label[ent.label_][ent.text] = entities_by_label[ent.label_].get(ent.text, 0) + 1

    # Sort by frequency, take top N per label
    result = {}
    for label, counts in entities_by_label.items():
        sorted_entities = sorted(counts.items(), key=lambda x: -x[1])
        result[label] = [ent for ent, _ in sorted_entities[:top_n]]

    return result


def get_primary_entity_set(entities: Dict[str, List[str]]) -> Set[str]:
    """
    Get a flat set of top entities across PERSON, ORG, GPE for story grouping.
    """
    primary_labels = {"PERSON", "ORG", "GPE"}
    entity_set = set()
    for label in primary_labels:
        entity_set.update(entities.get(label, []))
    return entity_set


def entity_set_jaccard(set_a: Set[str], set_b: Set[str]) -> float:
    """Jaccard similarity between two entity sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)