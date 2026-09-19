"""Tests for NER entity extraction and Jaccard similarity."""

import pytest
from src.utils.ner import (
    extract_entities,
    get_primary_entity_set,
    entity_set_jaccard,
)


class TestExtractEntities:
    def test_extract_basic_entities(self):
        text = "President Biden met with Prime Minister Modi in Washington."
        entities = extract_entities(text)
        assert "PERSON" in entities
        assert "GPE" in entities
        assert any("Biden" in e for e in entities.get("PERSON", []))
        assert any("Modi" in e for e in entities.get("PERSON", []))
        assert any("Washington" in e for e in entities.get("GPE", []))

    def test_empty_text(self):
        entities = extract_entities("")
        for label in entities:
            assert entities[label] == []

    def test_top_n_limit(self):
        text = " ".join([f"Person{i}" for i in range(10)])
        entities = extract_entities(text, top_n=3)
        assert len(entities.get("PERSON", [])) <= 3

    def test_org_entities(self):
        text = "Microsoft and Google announced partnerships with OpenAI."
        entities = extract_entities(text)
        assert "ORG" in entities
        orgs = entities["ORG"]
        assert any("Microsoft" in o for o in orgs)
        assert any("Google" in o for o in orgs)


class TestGetPrimaryEntitySet:
    def test_combines_person_org_gpe(self):
        entities = {
            "PERSON": ["Biden"],
            "ORG": ["White House"],
            "GPE": ["Washington"],
            "LOC": ["Potomac River"],  # Should not be included
        }
        primary = get_primary_entity_set(entities)
        assert "Biden" in primary
        assert "White House" in primary
        assert "Washington" in primary
        assert "Potomac River" not in primary

    def test_missing_labels(self):
        entities = {"PERSON": ["Test"]}
        primary = get_primary_entity_set(entities)
        assert primary == {"Test"}


class TestEntitySetJaccard:
    def test_identical_sets(self):
        set_a = {"Biden", "White House", "Washington"}
        set_b = {"Biden", "White House", "Washington"}
        assert entity_set_jaccard(set_a, set_b) == 1.0

    def test_disjoint_sets(self):
        set_a = {"Biden", "White House"}
        set_b = {"Putin", "Kremlin", "Moscow"}
        assert entity_set_jaccard(set_a, set_b) == 0.0

    def test_partial_overlap(self):
        set_a = {"Biden", "White House", "Washington"}
        set_b = {"Biden", "Kremlin", "Moscow"}
        # Intersection: 1, Union: 5
        assert abs(entity_set_jaccard(set_a, set_b) - 0.2) < 0.01

    def test_empty_sets(self):
        assert entity_set_jaccard(set(), set()) == 1.0
        assert entity_set_jaccard({"a"}, set()) == 0.0
        assert entity_set_jaccard(set(), {"a"}) == 0.0