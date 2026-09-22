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
    compute_story_metrics,
    summarize_story_metrics,
    format_crosstab,
    format_per_day,
    build_report,
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


class TestComputeStoryMetrics:
    """Test compute_story_metrics function."""

    def test_story_with_no_links(self):
        """A story with no linked units returns (0, 0)."""
        stories = [{"id": "story1", "status": "OPEN", "tier1_unit_count": 0, "distinct_owners": 0}]
        story_units = {"story1": []}
        unit_info = {}

        result = compute_story_metrics(stories, story_units, unit_info)

        assert len(result) == 1
        assert result[0]["linked_units"] == 0
        assert result[0]["computed_tier1_count"] == 0
        assert result[0]["computed_distinct_owners"] == 0
        assert result[0]["mismatch"] is False

    def test_tier1_count_is_sum_not_linked_count(self):
        """Tier-1 count is sum of source_tiers['tier1'], not number of linked units."""
        stories = [{"id": "story1", "status": "OPEN", "tier1_unit_count": 2, "distinct_owners": 1}]
        story_units = {"story1": ["unit1", "unit2"]}
        unit_info = {
            "unit1": {"source_tiers": {"tier1": 2}, "tier1_owner_groups": {"BBC": 1}},
            "unit2": {"source_tiers": {"tier1": 3}, "tier1_owner_groups": {"BBC": 1}},
        }

        result = compute_story_metrics(stories, story_units, unit_info)

        assert result[0]["linked_units"] == 2
        assert result[0]["computed_tier1_count"] == 5  # sum, not count

    def test_stale_stored_value_flagged(self):
        """A stale stored value is flagged as mismatch."""
        stories = [{"id": "story1", "status": "OPEN", "tier1_unit_count": 5, "distinct_owners": 2}]
        story_units = {"story1": ["unit1"]}
        unit_info = {
            "unit1": {"source_tiers": {"tier1": 3}, "tier1_owner_groups": {"BBC": 1}},
        }

        result = compute_story_metrics(stories, story_units, unit_info)

        assert result[0]["stored_tier1_count"] == 5
        assert result[0]["computed_tier1_count"] == 3
        assert result[0]["mismatch"] is True

    def test_distinct_owners_union(self):
        """Distinct owners is union of tier1_owner_groups keys."""
        stories = [{"id": "story1", "status": "OPEN", "tier1_unit_count": 1, "distinct_owners": 1}]
        story_units = {"story1": ["unit1", "unit2"]}
        unit_info = {
            "unit1": {"source_tiers": {"tier1": 1}, "tier1_owner_groups": {"BBC": 1}},
            "unit2": {"source_tiers": {"tier1": 1}, "tier1_owner_groups": {"Guardian": 1}},
        }

        result = compute_story_metrics(stories, story_units, unit_info)

        assert result[0]["computed_distinct_owners"] == 2


class TestSummarizeStoryMetrics:
    """Test summarize_story_metrics function."""

    def test_crosstab_counts_by_status(self):
        """Crosstab counts stories by (linked_units, computed_distinct_owners) and status."""
        metrics = [
            {"id": "s1", "status": "OPEN", "linked_units": 2, "computed_tier1_count": 3, "computed_distinct_owners": 1,
             "stored_tier1_count": 3, "stored_distinct_owners": 1, "mismatch": False},
            {"id": "s2", "status": "OPEN", "linked_units": 2, "computed_tier1_count": 3, "computed_distinct_owners": 1,
             "stored_tier1_count": 3, "stored_distinct_owners": 1, "mismatch": False},
            {"id": "s3", "status": "CLOSED", "linked_units": 2, "computed_tier1_count": 3, "computed_distinct_owners": 1,
             "stored_tier1_count": 3, "stored_distinct_owners": 1, "mismatch": False},
            {"id": "s4", "status": "OPEN", "linked_units": 3, "computed_tier1_count": 4, "computed_distinct_owners": 2,
             "stored_tier1_count": 4, "stored_distinct_owners": 2, "mismatch": False},
        ]

        result = summarize_story_metrics(metrics)

        assert result["crosstab"][(2, 1)]["OPEN"] == 2
        assert result["crosstab"][(2, 1)]["CLOSED"] == 1
        assert result["crosstab"][(3, 2)]["OPEN"] == 1
        assert "OPEN" in result["statuses"]
        assert "CLOSED" in result["statuses"]
        assert result["mismatch_count"] == 0

    def test_mismatch_examples_limited(self):
        """Mismatch examples limited to max_examples."""
        metrics = [
            {"id": f"s{i}", "status": "OPEN", "linked_units": 1, "computed_tier1_count": 1, "computed_distinct_owners": 1,
             "stored_tier1_count": 2, "stored_distinct_owners": 1, "mismatch": True}
            for i in range(15)
        ]

        result = summarize_story_metrics(metrics, max_examples=5)

        assert result["mismatch_count"] == 15
        assert len(result["examples"]) == 5


class TestFormatCrosstab:
    """Test format_crosstab function."""

    def test_formats_as_markdown_table(self):
        """Formats crosstab as markdown with statuses as columns."""
        summary = {
            "crosstab": {
                (2, 1): {"OPEN": 3, "CLOSED": 1},
                (3, 2): {"OPEN": 2},
            },
            "statuses": ["CLOSED", "OPEN"],
            "mismatch_count": 0,
            "examples": [],
        }

        result = format_crosstab(summary)

        assert "| Linked Units | Tier-1 Owners |" in result
        assert "CLOSED" in result
        assert "OPEN" in result
        assert "| 2 | 1 | 1 | 3 | 4 |" in result  # sorted statuses: CLOSED first, then OPEN
        assert "| 3 | 2 | 0 | 2 | 2 |" in result

    def test_empty_crosstab(self):
        """Empty crosstab returns 'No stories.'"""
        summary = {"crosstab": {}, "statuses": [], "mismatch_count": 0, "examples": []}
        result = format_crosstab(summary)
        assert result == "No stories."


class TestFormatPerDay:
    """Test format_per_day function."""

    def test_formats_per_day_table(self):
        """Formats per-day rows as markdown table."""
        rows = [
            {"day": "2026-09-20", "status": "OPEN", "count": 2},
            {"day": "2026-09-20", "status": "CLOSED", "count": 1},
            {"day": "2026-09-19", "status": "OPEN", "count": 3},
        ]

        result = format_per_day(rows, "Test Title")

        assert "### Test Title" in result
        assert "| Day | CLOSED | OPEN | Total |" in result
        assert "| 2026-09-20 | 1 | 2 | 3 |" in result
        assert "| 2026-09-19 | 0 | 3 | 3 |" in result

    def test_empty_rows(self):
        """Empty rows returns 'No data.'"""
        result = format_per_day([], "Empty")
        assert "### Empty" in result
        assert "No data." in result


class TestBuildReport:
    """Test build_report function."""

    def test_all_headings_appear_once(self):
        """Each heading appears exactly once in the report."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        data = {
            "counts": {
                "raw_articles": 10, "reporting_units": 5, "story_unit_links": 4,
                "stories": 3, "stories_no_units": 0, "units_missing_rep": 0, "articles_no_unit": 0,
            },
            "stories_per_day": [],
            "articles_per_day": [],
            "units_per_day": [],
            "orphans_per_day": [],
            "story_summary": {"crosstab": {}, "statuses": [], "mismatch_count": 0, "examples": []},
            "entity_sizes": {"min": 1, "median": 3, "max": 5, "empty_count": 0},
            "jaccard": {"histogram": {0.0: 3}, "near_misses": [], "stats": {
                "total_recent_units": 3, "units_with_entities": 3, "units_with_no_entities": 0,
                "units_with_no_candidates": 0, "zero_overlap_pairs": 0, "would_attach": 0
            }},
            "duplicates": [],
        }

        result = build_report(data, days=3, host="db.example.com:5432", now=now)

        # Check each heading appears exactly once
        assert result.count("# Story Audit Report") == 1
        assert result.count("## 1. Counts") == 1
        assert result.count("## 2. Per-day activity") == 1
        assert result.count("## 3. Story shape") == 1
        assert result.count("## 4. Entity-set size") == 1
        assert result.count("## 5. Cross-owner Jaccard") == 1
        assert result.count("## 6. Duplicate") == 1
        # Check host and note
        assert "db.example.com:5432" in result
        assert "approximate canonical IDs" in result

    def test_empty_data_renders(self):
        """All-empty data renders without crashing."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        data = {
            "counts": {},
            "stories_per_day": [],
            "articles_per_day": [],
            "units_per_day": [],
            "orphans_per_day": [],
            "story_summary": {"crosstab": {}, "statuses": [], "mismatch_count": 0, "examples": []},
            "entity_sizes": {},
            "jaccard": {"histogram": {}, "near_misses": [], "stats": {}},
            "duplicates": [],
        }

        result = build_report(data, days=3, host="db.example.com:5432", now=now)

        # Should not crash and should have structure
        assert "# Story Audit Report" in result
        assert "No stories." in result
        assert "No duplicates found." in result


class TestNoPrintInDataLayer:
    """Test that data layer functions don't contain print statements."""

    def test_no_print_in_data_layer(self):
        """No print() calls inside run_audit or fetch_audit_data."""
        import ast

        with open("scripts/story_audit.py", "r") as f:
            source = f.read()

        tree = ast.parse(source)

        class PrintVisitor(ast.NodeVisitor):
            def __init__(self):
                self.prints_in_data_layer = []

            def visit_FunctionDef(self, node):
                if node.name in ("run_audit", "fetch_audit_data"):
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "print":
                            self.prints_in_data_layer.append((node.name, child.lineno))
                self.generic_visit(node)

        visitor = PrintVisitor()
        visitor.visit(tree)

        assert not visitor.prints_in_data_layer, f"Found print() in data layer: {visitor.prints_in_data_layer}"


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