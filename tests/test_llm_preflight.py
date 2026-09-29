"""Tests for LLM preflight check."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.shared.llm_preflight import run_llm_preflight, run_llm_preflight_or_fail


@pytest.mark.asyncio
async def test_preflight_groq_200_cerebras_200():
    """Both providers return 200."""
    with patch("src.shared.llm_preflight.get_llm_client", new_callable=AsyncMock) as mock_client_factory:
        mock_client = AsyncMock()
        mock_client.chat_completion.return_value = {"choices": [{"message": {"content": "pong"}}]}
        mock_client_factory.return_value = mock_client

        with patch("src.shared.llm_preflight.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                groq_api_key="gsk_test",
                cerebras_api_key="csk_test"
            )

            results = await run_llm_preflight()

            assert results["groq"]["status"] == 200
            assert results["groq"]["ok"] is True
            assert results["cerebras"]["status"] == 200
            assert results["cerebras"]["ok"] is True


@pytest.mark.asyncio
async def test_preflight_groq_401():
    """Groq returns 401, Cerebras 200."""
    with patch("src.shared.llm_preflight.get_llm_client", new_callable=AsyncMock) as mock_client_factory:
        mock_client = AsyncMock()
        
        # First call (Groq) raises 401
        async def mock_chat_completion(messages, max_tokens, temperature):
            if not hasattr(mock_chat_completion, "call_count"):
                mock_chat_completion.call_count = 0
            mock_chat_completion.call_count += 1
            if mock_chat_completion.call_count == 1:
                # Groq call
                error = Exception("401 Unauthorized")
                error.status_code = 401
                raise error
            # Cerebras call
            return {"choices": [{"message": {"content": "pong"}}]}
        
        mock_client.chat_completion.side_effect = mock_chat_completion
        mock_client_factory.return_value = mock_client

        with patch("src.shared.llm_preflight.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                groq_api_key="gsk_test",
                cerebras_api_key="csk_test"
            )

            results = await run_llm_preflight()

            assert results["groq"]["status"] == 401
            assert results["groq"]["ok"] is False
            assert results["cerebras"]["status"] == 200
            assert results["cerebras"]["ok"] is True


@pytest.mark.asyncio
async def test_preflight_no_keys():
    """Neither key configured."""
    with patch("src.shared.llm_preflight.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            groq_api_key="",
            cerebras_api_key=""
        )

        results = await run_llm_preflight()

        assert results["groq"]["ok"] is False
        assert results["groq"]["error"] == "No API key configured"
        assert results["cerebras"]["ok"] is False
        assert results["cerebras"]["error"] == "No API key configured"


@pytest.mark.asyncio
async def test_preflight_or_fail_success():
    """run_llm_preflight_or_fail exits 0 when all providers OK."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 200, "ok": True},
            "cerebras": {"status": 200, "ok": True}
        }
        
        # Should not raise SystemExit
        await run_llm_preflight_or_fail()


@pytest.mark.asyncio
async def test_preflight_or_fail_failure():
    """run_llm_preflight_or_fail exits 1 when any provider fails."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 401, "ok": False, "error": "Unauthorized"},
            "cerebras": {"status": 200, "ok": True}
        }
        
        with pytest.raises(SystemExit) as exc_info:
            await run_llm_preflight_or_fail()
        
        assert exc_info.value.code == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
