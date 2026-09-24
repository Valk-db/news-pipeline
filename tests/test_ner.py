"""Tests for NER entity extraction and Jaccard similarity."""

from unittest.mock import MagicMock, patch

import pytest

from src.utils.ner import (
    ENTITY_LABELS,
    extract_entities,
    extract_entities_top_n,
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

    def test_top_n_none_keeps_all(self):
        """top_n=None returns every entity per label, no cap."""
        names = ["John", "Mary", "Robert", "Patricia", "James", "Jennifer",
                 "Michael", "Linda", "William", "Elizabeth"]
        # Period separators so spaCy treats each name as a distinct PERSON
        # entity (a space-separated run gets merged into one entity).
        text = ". ".join(names) + "."
        entities = extract_entities_top_n(text, top_n=None)
        assert len(entities.get("PERSON", [])) == 10

    def test_top_n_caps_per_label(self):
        """extract_entities_top_n honors the per-label cap from settings.top_n_entities.

        Regression: the per-label cap used to be hardcoded to 3 at each call
        site, so settings.top_n_entities was dead config. This test reads the
        same Settings object the ingestion modules read and asserts the cap
        it exposes actually bounds the extraction per label.
        """
        from src.shared.config import get_settings

        # Real first names so spaCy actually classifies them as PERSON; period
        # separators so each is a distinct entity rather than one merged span.
        names = ["John", "Mary", "Robert", "Patricia", "James", "Jennifer",
                 "Michael", "Linda", "William", "Elizabeth"]
        text = ". ".join(names) + "."

        cap = get_settings().top_n_entities
        assert isinstance(cap, int) and cap > 0
        entities = extract_entities_top_n(text, top_n=cap)
        assert len(entities.get("PERSON", [])) == min(10, cap)

    def test_org_entities(self):
        text = "Microsoft and Google announced partnerships with OpenAI."
        entities = extract_entities(text)
        assert "ORG" in entities
        orgs = entities["ORG"]
        assert any("Microsoft" in o for o in orgs)
        assert any("Google" in o for o in orgs)


class TestExtractEntitiesTopNValidation:
    """Deterministic tests for extract_entities_top_n using a fake NLP.

    These tests patch get_nlp to return a callable whose result has .ents
    with .label_ and .text, so no spaCy model is needed.
    """

    def _make_fake_nlp(self, ents_data):
        """Create a fake NLP callable.

        Args:
            ents_data: List of (label, text) tuples representing entities.
        """
        mock_doc = MagicMock()
        mock_ents = []
        for label, text in ents_data:
            mock_ent = MagicMock()
            mock_ent.label_ = label
            mock_ent.text = text
            mock_ents.append(mock_ent)
        mock_doc.ents = mock_ents
        fake_nlp = MagicMock(return_value=mock_doc)
        return fake_nlp

    def test_top_n_negative_raises_value_error(self):
        """top_n=-1 raises ValueError."""
        fake_nlp = self._make_fake_nlp([("PERSON", "Biden")])
        with patch("src.utils.ner.get_nlp", return_value=fake_nlp):
            with pytest.raises(ValueError, match="top_n must be >= 0"):
                extract_entities_top_n("text", top_n=-1)

    def test_top_n_negative_five_raises_value_error(self):
        """top_n=-5 raises ValueError."""
        fake_nlp = self._make_fake_nlp([("PERSON", "Biden")])
        with patch("src.utils.ner.get_nlp", return_value=fake_nlp):
            with pytest.raises(ValueError, match="top_n must be >= 0"):
                extract_entities_top_n("text", top_n=-5)

    def test_top_n_zero_returns_empty_for_all_labels(self):
        """top_n=0 returns an empty list for every label in ENTITY_LABELS."""
        fake_nlp = self._make_fake_nlp([
            ("PERSON", "Biden"),
            ("ORG", "White House"),
            ("GPE", "Washington"),
        ])
        with patch("src.utils.ner.get_nlp", return_value=fake_nlp):
            result = extract_entities_top_n("text", top_n=0)
            for label in ENTITY_LABELS:
                assert result[label] == [], f"Label {label} should be empty list"

    def test_top_n_none_keeps_all_entities(self):
        """top_n=None keeps all entities (no cap)."""
        fake_nlp = self._make_fake_nlp([
            ("PERSON", "Biden"),
            ("PERSON", "Trump"),
            ("PERSON", "Obama"),
            ("PERSON", "Clinton"),
            ("PERSON", "Bush"),
        ])
        with patch("src.utils.ner.get_nlp", return_value=fake_nlp):
            result = extract_entities_top_n("text", top_n=None)
            assert len(result["PERSON"]) == 5

    def test_top_n_two_returns_two_most_frequent(self):
        """top_n=2 with 3 distinct entities in one label returns the 2 most frequent."""
        # Biden appears 3 times, Trump 2 times, Obama 1 time
        fake_nlp = self._make_fake_nlp([
            ("PERSON", "Biden"),
            ("PERSON", "Biden"),
            ("PERSON", "Biden"),
            ("PERSON", "Trump"),
            ("PERSON", "Trump"),
            ("PERSON", "Obama"),
        ])
        with patch("src.utils.ner.get_nlp", return_value=fake_nlp):
            result = extract_entities_top_n("text", top_n=2)
            assert result["PERSON"] == ["Biden", "Trump"]

    def test_extract_entities_default_caps_at_three(self):
        """extract_entities(text) with no top_n caps at 3 per label."""
        # 5 distinct PERSON entities
        fake_nlp = self._make_fake_nlp([
            ("PERSON", "Biden"),
            ("PERSON", "Trump"),
            ("PERSON", "Obama"),
            ("PERSON", "Clinton"),
            ("PERSON", "Bush"),
        ])
        with patch("src.utils.ner.get_nlp", return_value=fake_nlp):
            result = extract_entities("text")
            assert len(result["PERSON"]) == 3


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