"""Tests for story audit pure helper functions (no database)."""

import sys
import os
# Add project root to path (repo root, not scripts/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.story_audit import (
    build_unit_entity_sets,
    jaccard_similarity,
    compute_jaccard_histogram,
    find_duplicate_titles,
    format_histogram,
    format_near_misses,
)
from datetime import datetime, timezone, timedelta


class TestBuildUnitEntitySets:
    """Test build_unit_entity_sets function."""

    def test_builds_entity_sets_from_articles(self):
        """Builds entity sets for units from their representative articles."""
        units = [
            {"id": "unit1", "representative_article_id": "art1", "created_at": datetime.now(timezone.utc)},
            {"id": "unit2", "representative_article_id": "art2", "created_at": datetime.now(timezone.utc)},
        ]
        articles = {
            "art1": {
                "entities": {
                    "PERSON": ["Joe Biden", "Donald Trump"],
                    "ORG": ["White House"],
                    "GPE": ["United States"],
                    "LOC": ["Washington"],  # Should be ignored
                }
            },
            "art2": {
                "entities": {
                    "PERSON": ["Vladimir Putin"],
                    "ORG": ["Kremlin"],
                    "GPE": ["Russia"],
                }
            },
        }

        result = build_unit_entity_sets(units, articles)

        assert "unit1" in result
        assert "unit2" in result
        # PERSON entities
        assert "PERSON:joe biden" in result["unit1"]
        assert "PERSON:donald trump" in result["unit1"]
        assert "PERSON:vladimir putin" in result["unit2"]
        # ORG entities
        assert "ORG:white house" in result["unit1"]
        assert "ORG:kremlin" in result["unit2"]
        # GPE entities
        assert "GPE:united states" in result["unit1"]
        assert "GPE:russia" in result["unit2"]
        # LOC should not be included
        assert not any("LOC" in e for e in result["unit1"])

    def test_handles_missing_article(self):
        """Handles units with missing representative articles."""
        units = [
            {"id": "unit1", "representative_article_id": "art1", "created_at": datetime.now(timezone.utc)},
            {"id": "unit2", "representative_article_id": "missing", "created_at": datetime.now(timezone.utc)},
        ]
        articles = {
            "art1": {"entities": {"PERSON": ["Test Person"]}}
        }

        result = build_unit_entity_sets(units, articles)

        assert result["unit1"] == {"PERSON:test person"}
        assert result["unit2"] == set()

    def test_handles_missing_entities(self):
        """Handles articles with no entities field."""
        units = [
            {"id": "unit1", "representative_article_id": "art1", "created_at": datetime.now(timezone.utc)},
        ]
        articles = {
            "art1": {}  # No entities
        }

        result = build_unit_entity_sets(units, articles)
        assert result["unit1"] == set()


class TestJaccardSimilarity:
    """Test jaccard_similarity function."""

    def test_identical_sets(self):
        """Identical sets return 1.0."""
        set_a = {"a", "b", "c"}
        set_b = {"a", "b", "c"}
        assert jaccard_similarity(set_a, set_b) == 1.0

    def test_disjoint_sets(self):
        """Disjoint sets return 0.0."""
        set_a = {"a", "b"}
        set_b = {"c", "d"}
        assert jaccard_similarity(set_a, set_b) == 0.0

    def test_partial_overlap(self):
        """Partial overlap returns correct ratio."""
        set_a = {"a", "b", "c"}
        set_b = {"c", "d", "e"}
        # intersection = 1, union = 5
        assert jaccard_similarity(set_a, set_b) == 1.0 / 5.0

    def test_empty_sets(self):
        """Both empty returns 1.0."""
        assert jaccard_similarity(set(), set()) == 1.0

    def test_one_empty(self):
        """One empty returns 0.0."""
        assert jaccard_similarity({"a"}, set()) == 0.0
        assert jaccard_similarity(set(), {"a"}) == 0.0


class TestFindDuplicateTitles:
    """Test find_duplicate_titles function."""

    def test_finds_duplicates(self):
        """Finds (source_domain, title) pairs appearing more than once."""
        now = datetime.now(timezone.utc)
        articles = [
            {"source_domain": "bbc.com", "title": "Test Article", "fetched_at": now - timedelta(hours=2)},
            {"source_domain": "bbc.com", "title": "Test Article", "fetched_at": now - timedelta(hours=1)},
            {"source_domain": "guardian.com", "title": "Test Article", "fetched_at": now},
            {"source_domain": "bbc.com", "title": "Different Article", "fetched_at": now},
        ]

        result = find_duplicate_titles(articles)

        assert len(result) == 1
        assert result[0]["source_domain"] == "bbc.com"
        assert result[0]["title"] == "Test Article"
        assert result[0]["count"] == 2
        assert "to" in result[0]["fetched_at_range"]

    def test_no_duplicates_returns_empty(self):
        """Returns empty list when no duplicates."""
        now = datetime.now(timezone.utc)
        articles = [
            {"source_domain": "bbc.com", "title": "Article 1", "fetched_at": now},
            {"source_domain": "guardian.com", "title": "Article 2", "fetched_at": now},
        ]

        result = find_duplicate_titles(articles)
        assert result == []

    def test_multiple_duplicate_groups(self):
        """Handles multiple duplicate groups."""
        now = datetime.now(timezone.utc)
        articles = [
            {"source_domain": "bbc.com", "title": "Same", "fetched_at": now},
            {"source_domain": "bbc.com", "title": "Same", "fetched_at": now},
            {"source_domain": "guardian.com", "title": "Also Same", "fetched_at": now},
            {"source_domain": "guardian.com", "title": "Also Same", "fetched_at": now},
            {"source_domain": "npr.org", "title": "Unique", "fetched_at": now},
        ]

        result = find_duplicate_titles(articles)
        assert len(result) == 2
        titles = {r["title"] for r in result}
        assert titles == {"Same", "Also Same"}


class TestFormatHistogram:
    """Test format_histogram function."""

    def test_formats_histogram_as_markdown(self):
        """Formats histogram as markdown table."""
        histogram = {0.1: 5, 0.3: 2, 0.5: 1}
        result = format_histogram(histogram)

        assert "| Bucket | Count |" in result
        assert "| 0.1 | 5 |" in result
        assert "| 0.3 | 2 |" in result
        assert "| 0.5 | 1 |" in result

    def test_empty_histogram(self):
        """Empty histogram returns header only."""
        result = format_histogram({})
        assert "| Bucket | Count |" in result
        assert "|--------|-------|" in result


class TestFormatNearMisses:
    """Test format_near_misses function."""

    def test_formats_near_misses_as_markdown(self):
        """Formats near-misses as markdown table."""
        near_misses = [{
            "unit_a_id": "unit1",
            "unit_b_id": "unit2",
            "jaccard": 0.33,
            "shared_entities": ["PERSON:joe biden", "ORG:white house"],
            "unit_a_title": "Biden speaks at White House",
            "unit_b_title": "White House announces new policy",
            "unit_a_owners": ["BBC"],
            "unit_b_owners": ["Guardian"],
        }]

        result = format_near_misses(near_misses)

        assert "| Unit A | Unit B | Jaccard |" in result
        assert "unit1" in result
        assert "unit2" in result
        assert "0.330" in result
        assert "joe biden" in result
        assert "Biden speaks" in result
        assert "White House" in result

    def test_empty_near_misses(self):
        """Empty near-misses returns 'None found.'."""
        result = format_near_misses([])
        assert result == "None found."


class TestComputeJaccardHistogram:
    """Test compute_jaccard_histogram function."""

    def test_basic_histogram_and_near_misses(self):
        """Computes histogram and finds near-misses for disjoint owner groups."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        units = [
            {"id": "unit1", "created_at": now, "representative_article_id": "art1"},
            {"id": "unit2", "created_at": now, "representative_article_id": "art2"},
            {"id": "unit3", "created_at": now - timedelta(hours=24), "representative_article_id": "art3"},
        ]
        unit_entities = {
            "unit1": {"PERSON:joe biden", "ORG:white house"},
            "unit2": {"PERSON:joe biden", "ORG:congress"},
            "unit3": {"PERSON:vladimir putin", "ORG:kremlin"},
        }
        unit_owner_groups = {
            "unit1": {"BBC": 1},
            "unit2": {"Guardian": 1},  # Disjoint from BBC
            "unit3": {"BBC": 1},  # Same owner as unit1, should be excluded
        }
        articles = {
            "art1": {"title": "Biden at White House"},
            "art2": {"title": "Biden and Congress"},
            "art3": {"title": "Putin at Kremlin"},
        }

        result = compute_jaccard_histogram(
            units, unit_entities, unit_owner_groups, articles, now=now
        )
        histogram = result["histogram"]
        near_misses = result["near_misses"]
        stats = result["stats"]

        # unit1 and unit2 share "PERSON:joe biden" -> intersection=1, union=3 -> Jaccard=0.33
        # unit1 and unit3 have disjoint owners? No, both BBC -> excluded
        # unit2 and unit3 have disjoint owners (Guardian vs BBC) but no shared entities -> Jaccard=0
        # Jaccard 0.33 rounds to bucket 0.3 and is in [0.2, 0.4) near-miss range
        # Near-misses are deduped by frozenset pair, so unit1<->unit2 produces ONE entry

        assert 0.3 in histogram  # 0.33 floors to 0.3
        assert histogram[0.3] == 2  # unit1 and unit2 both have best_jaccard=0.33
        assert histogram.get(0.0, 0) == 1  # unit3 has best_jaccard=0.0 (only candidate unit2, jaccard=0)
        assert len(near_misses) == 1  # deduped: unit1<->unit2 produces one entry
        assert near_misses[0]["unit_a_id"] == "unit1"
        assert near_misses[0]["unit_b_id"] == "unit2"
        assert near_misses[0]["jaccard"] == 1/3
        # Stats checks
        assert stats["total_recent_units"] == 3
        assert stats["units_with_entities"] == 3
        assert stats["units_with_no_candidates"] == 0  # unit3 HAS candidate unit2 (jaccard=0.0, owners disjoint)

    def test_same_owner_excluded(self):
        """Units with same owner are excluded from comparison."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        units = [
            {"id": "unit1", "created_at": now, "representative_article_id": "art1"},
            {"id": "unit2", "created_at": now, "representative_article_id": "art2"},
        ]
        unit_entities = {
            "unit1": {"PERSON:joe biden"},
            "unit2": {"PERSON:joe biden"},
        }
        unit_owner_groups = {
            "unit1": {"BBC": 1},
            "unit2": {"BBC": 1},  # Same owner
        }
        articles = {
            "art1": {"title": "Article 1"},
            "art2": {"title": "Article 2"},
        }

        result = compute_jaccard_histogram(
            units, unit_entities, unit_owner_groups, articles, now=now
        )
        histogram = result["histogram"]
        near_misses = result["near_misses"]
        stats = result["stats"]

        # Both units have same owner (BBC), so no valid candidates
        # Both get bucketed at 0.0
        assert histogram == {0.0: 2}
        assert near_misses == []
        assert stats["units_with_no_candidates"] == 2

    def test_outside_48h_excluded(self):
        """Units outside 48h window are excluded."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        units = [
            {"id": "unit1", "created_at": now, "representative_article_id": "art1"},
            {"id": "unit2", "created_at": now - timedelta(hours=60), "representative_article_id": "art2"},  # 60h ago (inside 3-day window, outside 48h comparison window)
        ]
        unit_entities = {
            "unit1": {"PERSON:joe biden"},
            "unit2": {"PERSON:joe biden"},
        }
        unit_owner_groups = {
            "unit1": {"BBC": 1},
            "unit2": {"Guardian": 1},
        }
        articles = {
            "art1": {"title": "Article 1"},
            "art2": {"title": "Article 2"},
        }

        result = compute_jaccard_histogram(
            units, unit_entities, unit_owner_groups, articles, now=now
        )
        histogram = result["histogram"]
        near_misses = result["near_misses"]
        stats = result["stats"]

        # unit2 is 60h old, within 3-day window (days=3) but outside 48h comparison window
        # Both units are recent (within 3 days), but excluded from each other's comparison due to 48h
        # Both get no valid candidates -> bucketed at 0.0
        assert histogram == {0.0: 2}
        assert near_misses == []
        assert stats["units_with_no_candidates"] == 2

    def test_units_without_entities_excluded(self):
        """Units with empty entity sets are skipped."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        units = [
            {"id": "unit1", "created_at": now, "representative_article_id": "art1"},
            {"id": "unit2", "created_at": now, "representative_article_id": "art2"},
        ]
        unit_entities = {
            "unit1": {"PERSON:joe biden"},
            "unit2": set(),  # Empty
        }
        unit_owner_groups = {
            "unit1": {"BBC": 1},
            "unit2": {"Guardian": 1},
        }
        articles = {
            "art1": {"title": "Article 1"},
            "art2": {"title": "Article 2"},
        }

        result = compute_jaccard_histogram(
            units, unit_entities, unit_owner_groups, articles, now=now
        )
        histogram = result["histogram"]
        near_misses = result["near_misses"]
        stats = result["stats"]

        # unit2 has no entities -> skipped (units_with_no_entities += 1)
        # unit1 has no valid candidates (unit2 has no entities) -> bucketed at 0.0
        assert histogram == {0.0: 1}
        assert near_misses == []
        assert stats["units_with_entities"] == 1
        assert stats["units_with_no_entities"] == 1
        assert stats["units_with_no_candidates"] == 1
        assert stats["would_attach"] == 0

    def test_hours_window_honored(self):
        """hours_window parameter is honored."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        units = [
            {"id": "unit1", "created_at": now, "representative_article_id": "art1"},
            {"id": "unit2", "created_at": now - timedelta(hours=30), "representative_article_id": "art2"},  # 30h ago
        ]
        unit_entities = {
            "unit1": {"A", "B"},
            "unit2": {"A", "B"},
        }
        unit_owner_groups = {
            "unit1": {"BBC": 1},
            "unit2": {"Guardian": 1},
        }
        articles = {f"art{i}": {"title": f"Article {i}"} for i in range(1, 3)}

        # With hours_window=24, they are 30h apart -> excluded
        result = compute_jaccard_histogram(
            units, unit_entities, unit_owner_groups, articles, days=3, hours_window=24, now=now
        )
        stats = result["stats"]
        assert stats["units_with_no_candidates"] == 2  # Neither has a candidate within 24h

        # With hours_window=48, they are 30h apart -> included
        result = compute_jaccard_histogram(
            units, unit_entities, unit_owner_groups, articles, days=3, hours_window=48, now=now
        )
        stats = result["stats"]
        assert stats["units_with_entities"] == 2
        assert stats["would_attach"] == 2  # Jaccard = 1.0 >= 0.4

    def test_would_attach_threshold(self):
        """would_attach counts best Jaccard >= 0.4, not 0.39."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        # Jaccard = 0.4 exactly (2 shared out of 5 total = 0.4)
        units = [
            {"id": "unit1", "created_at": now, "representative_article_id": "art1"},
            {"id": "unit2", "created_at": now, "representative_article_id": "art2"},
            {"id": "unit3", "created_at": now, "representative_article_id": "art3"},
            {"id": "unit4", "created_at": now, "representative_article_id": "art4"},
        ]
        unit_entities = {
            "unit1": {"A", "B", "C", "D", "E"},  # 5 entities
            "unit2": {"A", "B"},  # 2 shared -> 2/5 = 0.4
            "unit3": {"A", "B", "C", "D", "E", "F", "G", "H", "I"},  # 9 entities
            "unit4": {"X", "Y", "Z"},  # No shared with unit3 -> Jaccard = 0
        }
        unit_owner_groups = {
            "unit1": {"BBC": 1},
            "unit2": {"Guardian": 1},
            "unit3": {"BBC": 1},
            "unit4": {"Guardian": 1},
        }
        articles = {f"art{i}": {"title": f"Article {i}"} for i in range(1, 5)}

        result = compute_jaccard_histogram(
            units, unit_entities, unit_owner_groups, articles, days=3, hours_window=48, now=now
        )
        stats = result["stats"]
        # unit1's best is unit2 (0.4) -> would_attach
        # unit2's best is unit1 (0.4) -> would_attach
        # unit3's best is unit4 (0.0) -> NOT would_attach
        # unit4's best is unit3 (0.0) -> NOT would_attach
        assert stats["would_attach"] == 2