"""Test that every roster rung's cap attributes name real Settings fields.

If a rung's daily_cap_attr (e.g. "groq_daily_request_budget") does not match a field
on Settings, _cap() falls through to its default (0), silently refusing every call
for that rung. This test fails in CI before such a mismatch can deploy.

_cap() only accepts int or float values, so the field must also be numeric. A cap
attribute pointing to a str field (e.g. "groq_api_key") would also fall through to 0.
"""

import pytest

from src.shared.config import Settings
from src.shared.llm_roster import ROSTER


def test_every_rung_cap_attr_is_a_real_settings_field():
    """Each rung's cap attributes must name existing numeric Settings fields."""
    errors = []

    for rung in ROSTER:
        # daily_token_cap_attr must be non-empty: empty skips the token check
        # entirely (llm.py:308), contradicting the documented requirement that
        # a missing token cap must not let calls through.
        if not rung.daily_token_cap_attr:
            errors.append(
                f"rung {rung.name!r}: daily_token_cap_attr is empty; "
                f"empty skips the token check, which is not allowed"
            )
        for attr_name in (
            rung.daily_cap_attr,
            rung.daily_token_cap_attr,
            rung.minute_cap_attr,
        ):
            if not attr_name:
                continue
            field = Settings.model_fields.get(attr_name)
            if field is None:
                errors.append(
                    f"rung {rung.name!r}: {attr_name!r} is not a field on Settings"
                )
            elif field.annotation not in (int, float):
                errors.append(
                    f"rung {rung.name!r}: {attr_name!r} is a {field.annotation}, "
                    f"not int or float; _cap() would fall through to 0"
                )

    assert not errors, (
        "Roster cap attributes must name real numeric Settings fields:\n"
        + "\n".join(errors)
    )
