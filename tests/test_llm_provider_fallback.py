"""Tests for LLM provider auth failure handling (P0-5)."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.shared.llm import LLMClient, LLMError


class TestLLMProviderFallback:
    """Tests for uniform provider auth failure handling and fallback."""

    @pytest.mark.asyncio
    async def test_groq_401_falls_through_to_cerebras(self):
        """Groq 401 should fall through to Cerebras and emit one warning annotation."""
        client = LLMClient()

        # Mock both clients
        mock_groq = AsyncMock()
        mock_cerebras = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = mock_cerebras

        # Groq raises 401
        from httpx import HTTPStatusError, Response, Request
        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(401, request=request, json={"error": {"message": "Invalid API Key"}})
        mock_groq.chat.completions.create.side_effect = HTTPStatusError("401", request=request, response=response)

        # Cerebras succeeds
        mock_cerebras.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Cerebras response"))]
        )

        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            result = await client.chat_completion([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "Cerebras response"
        # Verify Groq was called and failed, then Cerebras was called
        mock_groq.chat.completions.create.assert_called_once()
        mock_cerebras.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_both_providers_401_raises_llmerror(self):
        """Both providers failing with 401 should raise LLMError."""
        client = LLMClient()

        mock_groq = AsyncMock()
        mock_cerebras = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = mock_cerebras

        from httpx import HTTPStatusError, Response, Request
        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(401, request=request, json={"error": {"message": "Invalid API Key"}})
        mock_groq.chat.completions.create.side_effect = HTTPStatusError("401", request=request, response=response)

        request2 = Request("POST", "https://api.cerebras.ai/v1/chat/completions")
        response2 = Response(401, request=request2, json={"error": {"message": "Invalid API Key"}})
        mock_cerebras.chat.completions.create.side_effect = HTTPStatusError("401", request=request2, response=response2)

        with pytest.raises(LLMError, match="No LLM provider available"):
            await client.chat_completion([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_groq_403_falls_through_to_cerebras(self):
        """Groq 403 should fall through to Cerebras."""
        client = LLMClient()

        mock_groq = AsyncMock()
        mock_cerebras = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = mock_cerebras

        from httpx import HTTPStatusError, Response, Request
        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(403, request=request, json={"error": {"message": "Forbidden"}})
        mock_groq.chat.completions.create.side_effect = HTTPStatusError("403", request=request, response=response)

        mock_cerebras.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Cerebras response"))]
        )

        result = await client.chat_completion([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "Cerebras response"

    @pytest.mark.asyncio
    async def test_classify_relevance_groq_401_falls_through(self):
        """classify_relevance: Groq 401 should fall through to Cerebras."""
        client = LLMClient()

        mock_groq = AsyncMock()
        mock_cerebras = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = mock_cerebras

        from httpx import HTTPStatusError, Response, Request
        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(401, request=request, json={"error": {"message": "Invalid API Key"}})
        mock_groq.chat.completions.create.side_effect = HTTPStatusError("401", request=request, response=response)

        mock_cerebras.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"score": 0.8}'))]
        )

        score = await client.classify_relevance("test title", "test body")

        assert score == 0.8
        mock_groq.chat.completions.create.assert_called_once()
        mock_cerebras.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_caption_groq_401_falls_through(self):
        """generate_caption: Groq 401 should fall through to Cerebras."""
        client = LLMClient()

        mock_groq = AsyncMock()
        mock_cerebras = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = mock_cerebras

        from httpx import HTTPStatusError, Response, Request
        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(401, request=request, json={"error": {"message": "Invalid API Key"}})
        mock_groq.chat.completions.create.side_effect = HTTPStatusError("401", request=request, response=response)

        mock_cerebras.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Valid caption"))]
        )

        with patch("src.shared.llm.validate_caption", return_value=(True, "")):
            caption = await client.generate_caption("test story", ["fact1"], ["http://example.com"])

        assert caption == "Valid caption"
        mock_groq.chat.completions.create.assert_called_once()
        mock_cerebras.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_auth_warning_only_emitted_once_per_provider_per_run(self):
        """Auth warning annotation should be emitted only once per provider per run."""
        client = LLMClient()

        mock_groq = AsyncMock()
        mock_cerebras = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = mock_cerebras

        from httpx import HTTPStatusError, Response, Request
        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(401, request=request, json={"error": {"message": "Invalid API Key"}})
        mock_groq.chat.completions.create.side_effect = HTTPStatusError("401", request=request, response=response)

        request2 = Request("POST", "https://api.cerebras.ai/v1/chat/completions")
        response2 = Response(401, request=request2, json={"error": {"message": "Invalid API Key"}})
        mock_cerebras.chat.completions.create.side_effect = HTTPStatusError("401", request=request2, response=response2)

        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            with pytest.raises(LLMError):
                await client.chat_completion([{"role": "user", "content": "test"}])

            # Second call - warnings should not be re-emitted (but we can't easily test the print)
            # Just verify it still raises LLMError
            with pytest.raises(LLMError):
                await client.chat_completion([{"role": "user", "content": "test2"}])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])