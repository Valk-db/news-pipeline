"""Test that RequestBudget.status() does not raise when used() raises."""
import pytest
from src.shared.llm_budget import RequestBudget

async def test_status_does_not_raise_when_used_raises(monkeypatch):
    """If used() raises, status() must not propagate the exception.
    
    The caller (RequestBudget.run) does `raise BudgetExhausted(await self.status())`
    after spend() returns None. If status() raises, the walk gets a database
    exception instead of BudgetExhausted, and records the wrong stat key.
    """
    import src.shared.llm_budget as module
    
    async def raising_used(name, *, day=None):
        raise ConnectionError("database is down")
    
    monkeypatch.setattr(module, "used", raising_used)
    budget = RequestBudget(daily_limit=900)
    
    # Must not raise ConnectionError
    status = await budget.status()
    assert status.used_today == -1  # -1 means "unreadable", not a spend figure

async def test_status_does_not_claim_exhausted_when_unreadable(monkeypatch):
    """An unreadable counter must not be reported as exhausted.
    
    `spent_today` returns -1 for unreadable. status() must follow the same
    model: report unknown, not a measured exhaustion.
    """
    import src.shared.llm_budget as module
    
    async def unreadable(name, *, day=None):
        return None
    
    monkeypatch.setattr(module, "used", unreadable)
    budget = RequestBudget(daily_limit=900)
    
    status = await budget.status()
    assert status.used_today == -1
    assert status.exhausted is False


async def test_used_propagates_programming_error(monkeypatch):
    """used() must let ProgrammingError propagate, not swallow it as unreadable.
    
    A broken SQL statement is a defect, not an exhausted budget. This test uses
    a fake engine whose connect() raises ProgrammingError, which proves the
    except clauses work. It does NOT prove that Postgres raises ProgrammingError
    for a bad _USED statement; that is a database behavior, not a code behavior.
    """
    from sqlalchemy.exc import ProgrammingError
    import src.shared.budget as budget_module
    
    class FakeEngine:
        def connect(self):
            raise ProgrammingError("fake", {}, Exception("connection failed"))
    
    monkeypatch.setattr(budget_module, "_get_engine", lambda: FakeEngine())
    
    with pytest.raises(ProgrammingError):
        await budget_module.used("test_counter")


async def test_status_swallows_programming_error_reports_unreadable(monkeypatch):
    """status() must swallow ProgrammingError from used() and report unreadable.
    
    The caller does `raise BudgetExhausted(await self.status())` after spend()
    returns None. If status() propagated ProgrammingError, the walk would get
    a database exception instead of BudgetExhausted. The defect is logged at
    ERROR by used() before raising, so it is not silent. The refusal itself
    is tested separately in test_llm_budget.py.
    """
    from sqlalchemy.exc import ProgrammingError
    import src.shared.budget as budget_module
    
    class FakeEngine:
        def connect(self):
            raise ProgrammingError("fake", {}, Exception("connection failed"))
    
    monkeypatch.setattr(budget_module, "_get_engine", lambda: FakeEngine())
    
    budget = RequestBudget(daily_limit=900)
    # Must not raise ProgrammingError; must return unreadable status
    status = await budget.status()
    assert status.used_today == -1
    assert status.exhausted is False


async def test_unreadable_refusal_message_says_unreadable_not_exhausted():
    """The BudgetExhausted message for unreadable must say 'unreadable'.
    
    This is the operator-visible distinction between 'cannot verify spend'
    and 'measured exhaustion'. The whole branch exists to remove this conflation.
    """
    from src.shared.llm_budget import BudgetExhausted, BudgetStatus
    
    status = BudgetStatus(used_today=-1, limit=900, remaining=0, exhausted=False)
    exc = BudgetExhausted(status, "groq_requests")
    msg = str(exc).lower()
    assert "unreadable" in msg, f"message should say unreadable, got: {exc}"
    assert "exhausted" not in msg, f"message conflates unreadable with exhausted: {exc}"


# NOTE: _walk ProgrammingError behavior is not yet pinned by a test.
# When ensure_headroom raises ProgrammingError (broken _USED SQL), _walk catches
# it in `except Exception`, demotes the rung, and moves on without recording
# budget_skipped/token_budget_skipped stats. This is correct (defect, not
# exhaustion) but the behavior has no test. test_token_refusal_walk.py provides
# a real walk harness that could be adapted for this. Until then, this gap
# is recorded here.


async def test_status_unreadable_true_when_used_returns_none(monkeypatch):
    """status().unreadable must be True when used() returns None.
    
    Distinct from exhausted=False: "cannot determine" vs "measured not exhausted".
    A refusal with unreadable=True is fail-safe, not healthy.
    """
    import src.shared.llm_budget as module
    
    async def unreadable_used(name, *, day=None):
        return None
    
    monkeypatch.setattr(module, "used", unreadable_used)
    budget = RequestBudget(daily_limit=900)
    
    status = await budget.status()
    assert status.unreadable is True
    assert status.used_today == -1
    # exhausted stays False; unreadable is the signal, not exhausted
    assert status.exhausted is False


async def test_status_unreadable_true_when_used_raises(monkeypatch):
    """status().unreadable must be True when used() raises ProgrammingError."""
    from sqlalchemy.exc import ProgrammingError
    import src.shared.budget as budget_module
    
    class FakeEngine:
        def connect(self):
            raise ProgrammingError("fake", {}, Exception("connection failed"))
    
    monkeypatch.setattr(budget_module, "_get_engine", lambda: FakeEngine())
    budget = RequestBudget(daily_limit=900)
    
    status = await budget.status()
    assert status.unreadable is True
    assert status.exhausted is False


async def test_status_unreadable_false_on_normal_read(monkeypatch):
    """status().unreadable must be False when the counter reads normally."""
    import src.shared.llm_budget as module
    
    async def normal_used(name, *, day=None):
        return 42
    
    monkeypatch.setattr(module, "used", normal_used)
    budget = RequestBudget(daily_limit=900)
    
    status = await budget.status()
    assert status.unreadable is False
    assert status.used_today == 42
    assert status.exhausted is False
