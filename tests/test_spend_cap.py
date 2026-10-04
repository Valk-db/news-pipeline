"""Tests for spend() cap enforcement on the INSERT path.

The _SPEND SQL has a hole: the INSERT path (first spend of the day) does not check
the cap. The WHERE clause only applies to the UPDATE path (subsequent spends).
This means a cap of 0 does not refuse the first call, and any cap can be exceeded
by exactly one request on the first spend of the day.

These tests verify the fix: an early return in spend() when amount > cap.
"""

import pytest
from src.shared.budget import spend, used


@pytest.mark.asyncio
async def test_spend_cap_zero_fresh_counter_refuses(budget_counter):
    """Fresh counter, cap 0, amount 1: must return None, no row created."""
    result = await spend("test_cap_zero", 1, 0)
    assert result is None, f"spend with cap 0 should refuse, got {result}"
    # No row should have been created
    assert await used("test_cap_zero") == 0


@pytest.mark.asyncio
async def test_spend_amount_exceeds_cap_fresh_counter_refuses(budget_counter):
    """Fresh counter, cap 3, amount 5: must return None, no row created."""
    result = await spend("test_cap_exceeded", 5, 3)
    assert result is None, f"spend with amount > cap should refuse, got {result}"
    assert await used("test_cap_exceeded") == 0


@pytest.mark.asyncio
async def test_spend_boundary_cap_one_amount_one_succeeds(budget_counter):
    """Fresh counter, cap 1, amount 1: succeeds, next spend refused."""
    result = await spend("test_boundary", 1, 1)
    assert result == 1, f"spend at exactly cap should succeed, got {result}"
    assert await used("test_boundary") == 1

    # Second spend should be refused
    result2 = await spend("test_boundary", 1, 1)
    assert result2 is None, f"second spend over cap should refuse, got {result2}"


@pytest.mark.asyncio
async def test_spend_normal_under_cap_unchanged(budget_counter):
    """Normal spend under cap works as before."""
    result = await spend("test_normal", 1, 900)
    assert result == 1
    result2 = await spend("test_normal", 1, 900)
    assert result2 == 2
    assert await used("test_normal") == 2


@pytest.mark.asyncio
async def test_spend_sql_directly_enforces_cap_on_insert(budget_counter):
    """Execute _SPEND directly, bypassing the Python guard.

    The Python guard in spend() catches amount > cap before the SQL runs. If someone
    reverts the SQL to the VALUES form (which has the hole), these tests would still
    pass because the guard hides the SQL. This test executes _SPEND directly to verify
    the SQL itself enforces the cap on INSERT.
    """
    from src.shared.budget import _SPEND, _get_engine, today
    engine = _get_engine()
    assert engine is not None, "budget_counter fixture must provide an engine"

    # Fresh counter, cap 0, amount 1: SQL must return no row
    async with engine.connect() as conn:
        result = await conn.execute(
            _SPEND, {"name": "test_sql_direct_1", "day": today(), "amount": 1, "cap": 0}
        )
        row = result.first()
        await conn.commit()
    assert row is None, f"_SPEND with cap 0 should return no row, got {row}"
    assert await used("test_sql_direct_1") == 0, "no row should have been created"

    # Fresh counter, cap 3, amount 5: SQL must return no row
    async with engine.connect() as conn:
        result = await conn.execute(
            _SPEND, {"name": "test_sql_direct_2", "day": today(), "amount": 5, "cap": 3}
        )
        row = result.first()
        await conn.commit()
    assert row is None, f"_SPEND with amount > cap should return no row, got {row}"
    assert await used("test_sql_direct_2") == 0, "no row should have been created"
