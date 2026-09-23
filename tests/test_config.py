"""Tests for Settings config validation."""

import pytest
from pydantic import ValidationError

from src.shared.config import Settings


class TestSettingsValidation:
    """Tests for Settings model validation."""

    def test_default_top_n_entities_is_3(self):
        """Default top_n_entities is 3."""
        settings = Settings()
        assert settings.top_n_entities == 3

    def test_top_n_entities_zero_raises(self):
        """top_n_entities=0 raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(top_n_entities=0)
        assert "top_n_entities" in str(exc_info.value)
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_top_n_entities_negative_raises(self):
        """top_n_entities=-1 raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(top_n_entities=-1)
        assert "top_n_entities" in str(exc_info.value)
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_top_n_entities_valid_values(self):
        """Valid top_n_entities values work."""
        for val in [1, 2, 3, 5, 10, 100]:
            settings = Settings(top_n_entities=val)
            assert settings.top_n_entities == val

    def test_extra_ignored(self):
        """Extra fields are ignored (no error)."""
        # This should not raise - construct via env or dict
        import os
        os.environ["UNKNOWN_FIELD"] = "value"
        try:
            settings = Settings(top_n_entities=5)
            assert settings.top_n_entities == 5
        finally:
            del os.environ["UNKNOWN_FIELD"]