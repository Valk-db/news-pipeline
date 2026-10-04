"""Test that _token_budget_for wires each rung to its own Settings cap."""
import pytest
from src.shared.llm import LLMClient
from src.shared.llm_roster import LLMRung


def test_token_budget_wiring_uses_rung_specific_caps():
    """Each rung's TokenBudget must get the cap from its own Settings attribute.
    
    Uses distinct sentinel values per rung so a swapped wire or defaulted value
    fails. This proves the wiring, not just that a value is stored.
    """
    # Stub Settings with distinct sentinel values
    class StubSettings:
        groq_token_cap = 1111
        cerebras_token_cap = 2222
        openrouter_token_cap = 3333
    
    client = LLMClient.__new__(LLMClient)
    client.settings = StubSettings()
    client._token_budgets = {}
    
    # Create rungs with different cap attributes
    rung1 = LLMRung(
        name="groq", method="groq", key_attr="x", client_attr="y",
        budget_name="groq_tokens", daily_token_cap_attr="groq_token_cap",
    )
    rung2 = LLMRung(
        name="cerebras", method="cerebras", key_attr="x", client_attr="y",
        budget_name="cerebras_tokens", daily_token_cap_attr="cerebras_token_cap",
    )
    rung3 = LLMRung(
        name="openrouter", method="openrouter", key_attr="x", client_attr="y",
        budget_name="openrouter_tokens", daily_token_cap_attr="openrouter_token_cap",
    )
    
    b1 = client._token_budget_for(rung1)
    b2 = client._token_budget_for(rung2)
    b3 = client._token_budget_for(rung3)
    
    # Each must get its own sentinel, proving the wire is correct
    assert b1.daily_limit == 1111, f"groq got {b1.daily_limit}, expected 1111"
    assert b2.daily_limit == 2222, f"cerebras got {b2.daily_limit}, expected 2222"
    assert b3.daily_limit == 3333, f"openrouter got {b3.daily_limit}, expected 3333"
