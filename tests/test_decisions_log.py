"""DECISIONS.md is a decision log, so its failure mode is silent.

`curation_ui/cron.py` and `tests/test_transparency_signer_v2.py` both tell a
reader to go read it, and both references existed while the file did not: a
docstring pointing at a missing document is a dead end that looks like a
citation. Worse for a log like this one, the numbers in it were pre-registered
*before* the measurements, deliberately, so that nobody could move them later
because the data was inconvenient. That property is worth a test, because the
alternative -- a threshold quietly edited after a bad run -- is exactly the
thing the pre-registration exists to prevent.

What this file pins:

  TestTheFileIsActuallyThere        the referrers point at something
  TestPreRegisteredNumbers          the four exit criteria keep their numbers
  TestTheCuratedPostsMeasurement   the count is pasted, not paraphrased
  TestReopening                     the decision says how it is reopened
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISIONS = REPO_ROOT / "DECISIONS.md"


def _text() -> str:
    assert DECISIONS.is_file(), (
        f"{DECISIONS.name} is referenced from curation_ui/cron.py and "
        "tests/test_transparency_signer_v2.py but does not exist"
    )
    return DECISIONS.read_text(encoding="utf-8")


class TestTheFileIsActuallyThere:
    def test_it_exists_at_the_repo_root(self) -> None:
        assert DECISIONS.is_file(), f"expected {DECISIONS}"
        assert DECISIONS.parent == REPO_ROOT

    def test_it_is_markdown_with_real_headings_not_a_stub(self) -> None:
        text = _text()
        assert text.startswith("# Decisions")
        headings = re.findall(r"^##+ .+$", text, re.MULTILINE)
        assert len(headings) >= 6, f"only {len(headings)} headings; this reads as a stub"

    def test_the_code_that_points_here_points_at_this_file(self) -> None:
        """Both referrers must name DECISIONS.md, and must resolve."""
        for relpath in ("curation_ui/cron.py", "tests/test_transparency_signer_v2.py"):
            source = (REPO_ROOT / relpath).read_text(encoding="utf-8")
            assert "DECISIONS.md" in source, f"{relpath} no longer references DECISIONS.md"
        assert DECISIONS.is_file()

    def test_the_open_items_the_code_points_here_for_are_actually_open(self) -> None:
        """cron.py sends the reader here for the monitor decision. Say it is open."""
        text = _text()
        assert "Independent checkpoint monitor" in text
        assert "healthchecks.io" in text, (
            "cron.py's docstring names the monitor vendors; the decision log "
            "should not quietly drop them"
        )
        # It is a decision Tyler has to make, not one this repo made.
        assert "Tyler" in text.split("Independent checkpoint monitor")[1][:600]


class TestPreRegisteredNumbers:
    """Each number is asserted as a literal, not as a concept.

    A test that only checked "there is a section about the shadow window" would
    pass just as happily after someone raised the disagreement ceiling from 5%
    to 15% in a bad afternoon.
    """

    def test_the_human_gated_decision_is_stated_and_attributed(self) -> None:
        text = _text()
        assert "Public exposure is human-gated" in text
        assert "2026-10-02" in text
        assert "Tyler" in text
        assert "QUEUED" in text and "POSTED" in text, (
            "the decision must name the statuses that actually reach the public"
        )

    def test_criterion_1_is_the_ordering_one(self) -> None:
        assert "Phase 2 must run before the publish decision" in _text()

    def test_criterion_2_keeps_14_days_and_a_5_percent_ceiling(self) -> None:
        section = self._section("The dynamic gate shadows the static gate for 14 days")
        assert "**14 consecutive days**" in section
        assert "**Ceiling: 5%**" in section
        # The denominator is the part people argue about later.
        assert "denominator" in section.lower()

    def test_criterion_3_keeps_30_per_week_100_percent_and_300(self) -> None:
        section = self._section("A seeded spot audit, 30 per week")
        assert "**30 gate-passed stories per week**" in section
        assert "**100% of harm-flagged stories.**" in section
        assert "**Threshold: 300 consecutive clean samples.**" in section
        assert "seeded" in section.lower()

    def test_the_false_pass_definition_names_the_three_harm_modes(self) -> None:
        section = self._section("A seeded spot audit, 30 per week")
        for phrase in ("wrong grouping", "thin corroboration", "named person"):
            assert phrase in section, f"false-pass definition lost {phrase!r}"
        assert "resets the clean count to zero" in section, (
            "a false pass must reset the count, not be averaged into it"
        )

    def test_criterion_4_is_a_per_request_switch_not_a_pipeline_switch(self) -> None:
        section = self._section("A one-switch unpublish")
        assert "per request" in section
        assert "not** a pipeline-wide switch" in section.replace("Explicitly **not", "not")
        assert "scripts/unpublish_story.py" in section

    def test_all_four_criteria_are_present_and_numbered(self) -> None:
        text = _text()
        for heading in (
            "Phase 2 must run before the publish decision",
            "The dynamic gate shadows the static gate for 14 days",
            "A seeded spot audit, 30 per week",
            "A one-switch unpublish",
        ):
            assert heading in text, f"missing exit criterion: {heading}"

    @staticmethod
    def _section(heading_fragment: str) -> str:
        text = _text()
        match = re.search(
            r"^### (.+)$", text[text.index(heading_fragment) - 400 :], re.MULTILINE
        )
        assert match is not None, f"no heading found for {heading_fragment!r}"
        start = text.index(heading_fragment) - 400 + match.start()
        rest = text[start + len(match.group(0)) :]
        end = re.search(r"^#{2,3} ", rest, re.MULTILINE)
        return rest[: end.start()] if end else rest


class TestTheCuratedPostsMeasurement:
    def test_the_count_is_pasted_as_a_sql_result(self) -> None:
        section = _text().split("`curated_posts`: measured, kept, not dropped")[1]
        assert "SELECT count(*) FROM curated_posts;" in section
        # A pasted result block, not a paraphrase in prose.
        assert re.search(r"```\s*\n count\n-+\s*\n\s+\d+\s*\n```", section), (
            "expected a pasted psql result block with the count in it"
        )

    def test_it_says_which_database_was_measured(self) -> None:
        section = _text().split("`curated_posts`: measured, kept, not dropped")[1][:900]
        assert "dev" in section, "the measurement must say it is dev, not prod"

    def test_it_says_why_the_table_is_not_dropped_yet(self) -> None:
        section = _text().split("`curated_posts`: measured, kept, not dropped")[1]
        assert "one-way" in section
        assert "Enum" in section or "ENUM" in section, (
            "dropping the table leaves the enum type behind; that is a reason too"
        )

    def test_the_model_docstring_and_the_log_agree_the_table_survives(self) -> None:
        model = (REPO_ROOT / "src/schema/models.py").read_text(encoding="utf-8")
        tree = ast.parse(model)
        curated = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CuratedPost"
        )
        doc = ast.get_docstring(curated) or ""
        assert "etired" in doc, "the CuratedPost docstring must mark the table retired"

    def test_the_health_endpoint_no_longer_reports_approved_posts(self) -> None:
        """Parsed, not grepped: the removal is explained in a comment, and a
        grep for the name would match that comment forever."""
        health = (REPO_ROOT / "curation_ui/health.py").read_text(encoding="utf-8")
        tree = ast.parse(health)
        offenders = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "approved_posts" in node.value
        ]
        assert not offenders, (
            "the dead metric is back; it watches a table nothing writes "
            f"(line {offenders[0].lineno})"
        )


class TestReopening:
    def test_reopening_requires_new_evidence_not_a_new_argument(self) -> None:
        text = _text()
        assert "Reopening any of this requires new evidence." in text
        section = text.split("Reopening any of this requires new evidence.")[1][:700]
        assert "measurements against the pre-registered numbers" in section

    def test_the_decision_log_does_not_claim_the_exit_criteria_are_met(self) -> None:
        """All four are unmet today. If this test fails, a real change happened.

        That is not a failure to be silenced -- it is the log telling you to
        re-measure and update the numbers honestly.
        """
        text = _text()
        table = text.split("### Summary")[1]
        rows = [line for line in table.splitlines() if line.startswith("| ") and "---" not in line]
        rows = [r for r in rows if not r.startswith("| # ")]
        assert len(rows) == 4, f"expected four criterion rows, found {len(rows)}"
        unmet = table.count("not met")
        assert unmet == 3, f"three criteria are not met and one has only the manual script: {unmet}"
        assert "manual script only" in table
