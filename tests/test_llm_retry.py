"""Tests for LLM retry mechanism - 429 should be retried 3 times."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import HTTPStatusError, Response, Request
from src.shared.llm import LLMClient, LLMError


class TestLLMRetry:
    """Tests for LLM retry on transient errors (429, 5xx, timeout)."""

    @pytest.mark.asyncio
    async def test_groq_429_retried_3_times(self):
        """Groq 429 should be retried 3 times before giving up."""
        client = LLMClient()

        # Mock Groq client
        mock_groq = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = None  # No fallback

        # All 3 attempts return 429
        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(429, request=request, json={"error": {"message": "Rate limit exceeded"}})
        error = HTTPStatusError("429", request=request, response=response)
        mock_groq.chat.completions.create.side_effect = [error, error, error]

        # Should retry 3 times then raise LLMError
        with pytest.raises(LLMError, match="No LLM provider available"):
            await client.chat_completion([{"role": "user", "content": "test"}])

        # Should have been called 3 times
        assert mock_groq.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_groq_500_retried_3_times(self):
        """Groq 500 should be retried 3 times before giving up."""
        client = LLMClient()

        mock_groq = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = None

        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(500, request=request, json={"error": {"message": "Internal server error"}})
        error = HTTPStatusError("500", request=request, response=response)
        mock_groq.chat.completions.create.side_effect = [error, error, error]

        with pytest.raises(LLMError, match="No LLM provider available"):
            await client.chat_completion([{"role": "user", "content": "test"}])

        assert mock_groq.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_groq_timeout_retried_3_times(self):
        """Groq timeout should be retried 3 times before giving up."""
        from httpx import TimeoutException

        client = LLMClient()

        mock_groq = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = None

        error = TimeoutException("Request timed out")
        mock_groq.chat.completions.create.side_effect = [error, error, error]

        with pytest.raises(LLMError, match="No LLM provider available"):
            await client.chat_completion([{"role": "user", "content": "test"}])

        assert mock_groq.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_groq_429_succeeds_on_second_attempt(self):
        """Groq 429 on first attempt, success on second should return result."""
        client = LLMClient()

        mock_groq = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = None

        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response_429 = Response(429, request=request, json={"error": {"message": "Rate limit exceeded"}})
        error = HTTPStatusError("429", request=request, response=response_429)

        mock_groq.chat.completions.create.side_effect = [
            error,
            MagicMock(choices=[MagicMock(message=MagicMock(content="Success on retry"))])
        ]

        result = await client.chat_completion([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "Success on retry"
        assert mock_groq.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_cerebras_429_retried_3_times(self):
        """Cerebras 429 should be retried 3 times."""
        client = LLMClient()

        client.groq_client = None
        mock_cerebras = AsyncMock()
        client.cerebras_client = mock_cerebras

        request = Request("POST", "https://api.cerebras.ai/v1/chat/completions")
        response = Response(429, request=request, json={"error": {"message": "Rate limit exceeded"}})
        error = HTTPStatusError("429", request=request, response=response)
        mock_cerebras.chat.completions.create.side_effect = [error, error, error]

        with pytest.raises(LLMError, match="No LLM provider available"):
            await client.chat_completion([{"role": "user", "content": "test"}])

        assert mock_cerebras.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_classify_relevance_groq_429_retried(self):
        """classify_relevance should retry on 429."""
        client = LLMClient()

        mock_groq = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = None

        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(429, request=request, json={"error": {"message": "Rate limit exceeded"}})
        error = HTTPStatusError("429", request=request, response=response)
        mock_groq.chat.completions.create.side_effect = [error, error, error]

        with pytest.raises(LLMError):
            await client.classify_relevance("title", "body")

        assert mock_groq.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_generate_caption_groq_429_retried(self):
        """generate_caption should retry on 429."""

        client = LLMClient()

        mock_groq = AsyncMock()
        client.groq_client = mock_groq
        client.cerebras_client = None

        request = Request("POST", "https://api.groq.com/v1/chat/completions")
        response = Response(429, request=request, json={"error": {"message": "Rate limit exceeded"}})
        error = HTTPStatusError("429", request=request, response=response)
        mock_groq.chat.completions.create.side_effect = [error, error, error]

        with patch("src.shared.llm.validate_caption", return_value=(True, "")):
            with pytest.raises(LLMError):
                await client.generate_caption("story", ["fact"], ["http://example.com"])

        assert mock_groq.chat.completions.create.call_count == 3