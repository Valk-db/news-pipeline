"""Test that _token_budget_for wires each rung to its own Settings cap."""
from types import SimpleNamespace

from src.shared.llm import LLMClient
from src.shared.llm_roster import ROSTER


def test_token_budget_wiring_uses_rung_specific_caps():
    """Each rung's TokenBudget must get the cap from its own Settings attribute.
    
    Uses distinct sentinel values per rung so a swapped wire or defaulted value
    fails. The sentinels are explicit (not generated) so the fixture-fields guard
    can see every roster cap is configured. This proves the wiring against the
    actual roster attributes.
    
    The client fixture sets every field LLMClient.__init__ sets (see
    test_llm_client_fixture_fields.py). A missing field or cap silently breaks
    other tests, so the guard requires completeness here.
    """
    # Distinct sentinels per rung's token cap attribute. Explicit, not generated,
    # so the guard sees them. If ROSTER gains a rung, add its cap here.
    client = LLMClient.__new__(LLMClient)
    client.settings = SimpleNamespace(
        groq_daily_token_cap=1111,
        groq_daily_request_budget=900,
        openrouter_gemma_daily_token_cap=2222,
        openrouter_gemma_daily_request_budget=900,
        openrouter_nemotron_daily_token_cap=3333,
        openrouter_nemotron_daily_request_budget=900,
        cerebras_daily_token_cap=4444,
        cerebras_daily_request_budget=900,
    )
    client.groq_client = None
    client.cerebras_client = None
    client._budgets = {}
    client._minute_limiters = {}
    client._token_budgets = {}
    client._pinned = None
    client._last_walk_error = None
    client._demoted = set()
    client._auth_warned = set()
    client._openrouter_key = None
    client._openrouter_http = None
    
    expected = {
        "groq_daily_token_cap": 1111,
        "openrouter_gemma_daily_token_cap": 2222,
        "openrouter_nemotron_daily_token_cap": 3333,
        "cerebras_daily_token_cap": 4444,
    }
    
    # Verify the roster hasn't gained a rung we didn't cover
    roster_attrs = {r.daily_token_cap_attr for r in ROSTER if r.daily_token_cap_attr}
    assert roster_attrs == set(expected), (
        f"ROSTER changed: {roster_attrs} vs {set(expected)}. "
        f"Update the sentinels above."
    )
    
    for rung in ROSTER:
        if not rung.daily_token_cap_attr:
            continue
        budget = client._token_budget_for(rung)
        want = expected[rung.daily_token_cap_attr]
        assert budget.daily_limit == want, (
            f"rung {rung.name}: got {budget.daily_limit}, "
            f"expected {want} from {rung.daily_token_cap_attr}"
        )
