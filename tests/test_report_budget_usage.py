"""Tests for scripts/report_budget_usage.py.

The script's one job is to make the pathological state visible: a counter whose TOKENS are
spent while its REQUESTS read as almost untouched. A report that cannot print that state
would leave the pipeline in exactly the condition this batch exists to fix, so these tests
drive the formatting and flagging functions against the measured numbers rather than
asserting only that the script imports.

No database required: `collect()` is the only part that reads, and it is exercised by the
report's own failure-path test through an unreachable engine.
"""

from datetime import date

import pytest

from scripts.report_budget_usage import (
    REQUESTS_UNSHARE,
    _row_for,
    collect,
    flag_token_exhausted,
    format_table,
    main,
)
from src.shared.budget import (
    COUNTERS_BY_NAME,
    GROQ_PHASE2_TOKENS,
    GROQ_REQUEST_TOKENS,
    GROQ_REQUESTS,
    GROQ_TRANSLATION_TOKENS,
    counter_cap,
    spend,
)

DAY = date(2026, 10, 3)


def _rows(**overrides):
    """One row per known counter, from the real caps, with `overrides` applied by name.

    Built through the report's own _row_for so these fixtures cannot drift from the shape
    collect() produces. The first version hand-wrote the dict and left `exhausted` False
    for every row, which made two tests fail for a reason that had nothing to do with the
    report: the row said "ok" while its numbers said otherwise. Building the fixture from
    the product's own constructor is what stops the harness lying about the code.
    """
    return [
        _row_for(name, overrides.get(name, 0), counter_cap(spec), spec)
        for name, spec in COUNTERS_BY_NAME.items()
    ]


class TestFlagsTheProductionFailure:
    def test_tokens_spent_requests_far_under_is_flagged(self):
        """The measured production state: 28 requests of 900, tokens gone."""
        rows = _rows(**{GROQ_REQUESTS: 28, GROQ_REQUEST_TOKENS: 200_000})
        flagged = flag_token_exhausted(rows)

        assert len(flagged) == 1
        assert flagged[0]["name"] == GROQ_REQUEST_TOKENS
        assert flagged[0]["request_counter"]["name"] == GROQ_REQUESTS
        assert flagged[0]["request_counter"]["used"] == 28

    def test_the_flag_survives_into_the_printed_report(self):
        rows = _rows(**{GROQ_REQUESTS: 28, GROQ_REQUEST_TOKENS: 200_000})
        text = format_table(rows, flag_token_exhausted(rows))

        assert "TOKEN cap" in text
        assert "Raising the request cap will not help" in text
        assert GROQ_REQUEST_TOKENS in text
        # The operator has to be able to see the request number too, or they cannot tell
        # how far under it is.
        assert "28" in text

    def test_a_healthy_day_is_not_flagged(self):
        rows = _rows(**{GROQ_REQUESTS: 28, GROQ_REQUEST_TOKENS: 30_000})
        assert flag_token_exhausted(rows) == []

    def test_requests_exhausted_with_tokens_under_is_not_the_flagged_shape(self):
        """The converse. Requests spent, tokens fine, is the old failure's mirror image and
        the report must not claim the token diagnosis for it."""
        rows = _rows(**{GROQ_REQUESTS: 900, GROQ_REQUEST_TOKENS: 1_000})
        assert flag_token_exhausted(rows) == []

    def test_tokens_spent_with_requests_also_substantial_is_not_flagged(self):
        """The threshold exists so the flag means something. Tokens spent AND requests
        nearly spent is an ordinary exhausted day, not a unit-denomination mismatch."""
        rows = _rows(**{GROQ_REQUESTS: 880, GROQ_REQUEST_TOKENS: 200_000})
        assert flag_token_exhausted(rows) == []

    def test_the_threshold_is_a_share_of_the_request_cap(self):
        """Asserted as a relationship, not a constant: the flag must not depend on the
        token figure being comparable to the request figure, which it is not."""
        rows = _rows(**{GROQ_REQUESTS: 899, GROQ_REQUEST_TOKENS: 200_000})
        assert flag_token_exhausted(rows) == []
        rows = _rows(**{GROQ_REQUESTS: 10, GROQ_REQUEST_TOKENS: 200_000})
        assert len(flag_token_exhausted(rows)) == 1
        assert 0 < REQUESTS_UNSHARE < 1

    def test_phase2_is_reported_without_a_request_partner(self):
        """Phase 2 has never had a request counter. It must still be reported when its
        tokens are spent, and must not be silently dropped for lacking a pair."""
        rows = _rows(**{GROQ_PHASE2_TOKENS: 45_000})
        text = format_table(rows, flag_token_exhausted(rows))
        assert GROQ_PHASE2_TOKENS in text
        assert "EXHAUSTED (tokens)" in text
        # No pair, so no token-exhausted-while-requests-under claim about it.
        assert flag_token_exhausted(rows) == []

    def test_translation_tokens_are_paired_with_their_own_requests(self):
        rows = _rows(
            **{GROQ_TRANSLATION_TOKENS: 25_000, "groq_translation_requests": 4}
        )
        flagged = flag_token_exhausted(rows)
        assert len(flagged) == 1
        assert flagged[0]["name"] == GROQ_TRANSLATION_TOKENS
        assert flagged[0]["request_counter"]["name"] == "groq_translation_requests"


class TestReportsEveryCounterInItsOwnUnit:
    def test_every_known_counter_appears(self):
        text = format_table(_rows(), [])
        for name in COUNTERS_BY_NAME:
            assert name in text, f"{name} missing from the report"

    def test_chars_are_not_compared_against_a_token_budget(self):
        """MyMemory is keyless and capped in characters. Rendering its 45,000 against a
        token cap would be a category error, so the unit is printed and the number is
        simply its own."""
        rows = _rows(**{"mymemory_chars": 45_000})
        text = format_table(rows, flag_token_exhausted(rows))
        assert "EXHAUSTED (chars)" in text
        assert flag_token_exhausted(rows) == []

    def test_an_undeclared_counter_is_surfaced_not_omitted(self):
        """A counter in the table that this module has never heard of is still spend. A
        report that omitted it would read as "nothing else happened today"."""
        rows = _rows()
        rows.append(
            {
                "name": "mystery_counter",
                "unit": "unknown",
                "used": 7,
                "cap": None,
                "percent": None,
                "spent_by": "NOT IN src/shared/budget.py COUNTERS -- undeclared counter",
            }
        )
        text = format_table(rows, flag_token_exhausted(rows))
        assert "mystery_counter" in text
        assert "not declared" in text


class TestReadsTheRealTable:
    async def test_collect_reads_what_was_actually_spent(self, budget_counter):
        """The real table, through the real spend() path, not a hand-built row dict."""
        await spend(GROQ_REQUEST_TOKENS, 12_345, 60_000, day=DAY)
        await spend(GROQ_REQUESTS, 7, 900, day=DAY)


        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{budget_counter.url.database}")
        try:
            rows = await collect(str(engine.url), DAY)
        finally:
            await engine.dispose()

        by_name = {r["name"]: r for r in rows}
        assert by_name[GROQ_REQUEST_TOKENS]["used"] == 12_345
        assert by_name[GROQ_REQUESTS]["used"] == 7
        assert by_name[GROQ_PHASE2_TOKENS]["used"] == 0  # present, never spent
        assert all(r["cap"] is not None for r in rows if r["unit"] != "unknown")

    async def test_collect_reports_another_days_spend_as_zero(self, budget_counter):
        await spend(GROQ_REQUEST_TOKENS, 500, 60_000, day=DAY)
        from datetime import timedelta

        rows = await collect(f"sqlite+aiosqlite:///{budget_counter.url.database}", DAY + timedelta(days=1))
        by_name = {r["name"]: r for r in rows}
        assert by_name[GROQ_REQUEST_TOKENS]["used"] == 0


class TestDoesNotReportConfidentZeros:
    def test_an_unreachable_database_exits_non_zero(self, capsys):
        """The failure mode this script exists to prevent: a confident "0 used" for a
        database it could not read. It must exit non-zero and print nothing that looks
        like a measurement."""
        code = main(["--day", str(DAY), "--db-url", "sqlite+aiosqlite:////nonexistent/dir/x.db"])
        assert code == 1
        err = capsys.readouterr().err
        assert "could not read budget_counters" in err
        assert "EXHAUSTED" not in err

    def test_no_database_url_exits_non_zero_without_printing_a_table(self, capsys, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        code = main(["--day", str(DAY)])
        assert code == 1
        assert "DATABASE_URL" in capsys.readouterr().err

    @pytest.mark.parametrize("day", ["not-a-date", "2026-13-01"])
    def test_a_bad_day_is_rejected_rather_than_defaulted(self, day):
        """Silently reporting today when the caller asked for another day would answer a
        different question than the one that was asked."""
        with pytest.raises(ValueError):
            main(["--day", day])