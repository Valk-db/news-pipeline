"""Test that RequestBudget.status() does not raise when used() raises."""
import pytest
from src.shared.llm_budget import RequestBudget, BudgetExhausted

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
    
    class FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def execute(self, *args, **kwargs):
            raise ProgrammingError("fake", {}, Exception("bad statement"))
    
    class FakeEngine:
        def connect(self):
            raise ProgrammingError("fake", {}, Exception("connection failed"))
    
    monkeypatch.setattr(budget_module, "_get_engine", lambda: FakeEngine())
    
    with pytest.raises(ProgrammingError):
        await budget_module.used("test_counter")
