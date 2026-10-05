"""Guard against the date.today() vs today() bug class in budget tests.

The budget counter's day is defined exactly once, in src/shared/budget.py as
the UTC day. Tests that use `date.today()` (the *local* day) read a different
row than the one `record()` wrote to, on any runner whose timezone is not UTC.

This happened twice:
- tests/test_llm_budget.py (fixed in c58c6f6)
- tests/test_budget_tokens.py (fixed in 97c00b4)

This test scans budget-touching test files for `date.today()` outside of
comments and fails if found. Use `today()` from src.shared.budget instead.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUDGET_TEST_FILES = [
    "tests/test_budget_tokens.py",
    "tests/test_llm_budget.py",
    "tests/test_cost_accounting.py",
    "tests/test_token_refusal_walk.py",
]


def test_no_date_today_in_budget_tests():
    """Budget tests must use today() from src.shared.budget, not date.today()."""
    violations = []
    for rel in BUDGET_TEST_FILES:
        path = REPO / rel
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            # Strip comments; only flag actual code
            code = line.split("#")[0]
            if "date.today()" in code:
                violations.append(f"{rel}:{i}: {line.strip()}")
    assert not violations, (
        "Found date.today() in budget-touching tests (use today() from "
        "src.shared.budget instead):\n" + "\n".join(violations)
    )
