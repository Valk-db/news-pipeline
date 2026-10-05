"""Test that _token_budget_for wires each rung to its own Settings cap."""
import pytest
from src.shared.llm import LLMClient
from src.shared.llm_roster import ROSTER


def test_token_budget_wiring_uses_rung_specific_caps():
    """Each rung's TokenBudget must get the cap from its own Settings attribute.
    
    Loops over the real ROSTER, giving each rung's attribute a distinct sentinel
    value. A swapped wire (two rungs sharing an attribute after copy-paste) or
    a defaulted value fails. This proves the wiring against the actual roster,
    not hand-made rungs.
    """
    # Build a stub Settings with one distinct sentinel per rung's attribute
    attrs = [r.daily_token_cap_attr for r in ROSTER if r.daily_token_cap_attr]
    assert len(attrs) == len(set(attrs)), "roster has duplicate cap attributes"
    
    StubSettings = type("StubSettings", (), {
        attr: 1000 + i * 111 for i, attr in enumerate(attrs)
    })
    expected = {attr: 1000 + i * 111 for i, attr in enumerate(attrs)}
    
    client = LLMClient.__new__(LLMClient)
    client.settings = StubSettings()
    client._token_budgets = {}
    
    for rung in ROSTER:
        if not rung.daily_token_cap_attr:
            continue
        budget = client._token_budget_for(rung)
        want = expected[rung.daily_token_cap_attr]
        assert budget.daily_limit == want, (
            f"rung {rung.name}: got {budget.daily_limit}, "
            f"expected {want} from {rung.daily_token_cap_attr}"
        )
