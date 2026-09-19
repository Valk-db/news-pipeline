"""LLM client with Groq primary, Cerebras fallback, and token accounting."""

import asyncio
from typing import Optional, List, Dict, Any, Sequence
from src.shared.config import get_settings
from tenacity import retry, stop_after_attempt, wait_exponential
from httpx import HTTPStatusError, TimeoutException, ConnectError
import json

try:
    from groq import AsyncGroq
except ImportError:
    AsyncGroq = None

try:
    from cerebras.cloud.sdk import AsyncCerebras
except ImportError:
    AsyncCerebras = None


class LLMError(Exception):
    pass


class LLMClient:
    """Unified LLM client with provider fallback."""

    def __init__(self):
        self.settings = get_settings()
        self.groq_client: Optional[AsyncGroq] = None
        self.cerebras_client: Optional[AsyncCerebras] = None
        self._init_clients()

    def _init_clients(self):
        if self.settings.groq_api_key and AsyncGroq:
            self.groq_client = AsyncGroq(api_key=self.settings.groq_api_key)
        if self.settings.cerebras_api_key and AsyncCerebras:
            self.cerebras_client = AsyncCerebras(api_key=self.settings.cerebras_api_key)

    @staticmethod
    def _is_transient_error(exception: BaseException) -> bool:
        """Check if error is transient (rate limit, 5xx, timeout, connection)."""
        if isinstance(exception, (TimeoutException, ConnectError)):
            return True
        if isinstance(exception, HTTPStatusError):
            # HTTPStatusError has response attribute with status_code
            status_code = getattr(exception.response, "status_code", 0)
            return status_code >= 500 or status_code == 429
        return False

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=lambda e: LLMClient._is_transient_error(e),
    )
    async def _chat_completion_groq(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> Dict[str, Any]:
        """Make a chat completion request to Groq."""
        if not self.groq_client:
            raise LLMError("Groq client not initialized")
        response = await self.groq_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return {
            "choices": [{"message": {"content": response.choices[0].message.content}}]
        }

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=lambda e: LLMClient._is_transient_error(e),
    )
    async def _chat_completion_cerebras(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> Dict[str, Any]:
        """Make a chat completion request to Cerebras."""
        if not self.cerebras_client:
            raise LLMError("Cerebras client not initialized")
        response = await self.cerebras_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return {
            "choices": [{"message": {"content": response.choices[0].message.content}}]
        }

    async def generate_caption(
        self,
        story_title: str,
        key_facts: List[str],
        source_urls: List[str],
        platform: str = "twitter",
    ) -> Optional[str]:
        """
        Generate a caption for a curated post.
        CRITICAL: Paraphrase only. One link to source. Never reproduce
        more than a short fragment of the scraped article body.
        """
        # Platform-specific constraints
        constraints = {
            "twitter": "Max 280 characters including link.",
            "instagram": "Max 2200 characters, hashtags encouraged, link in bio.",
            "linkedin": "Max 3000 characters, professional tone.",
            "threads": "Max 500 characters, conversational.",
            "bluesky": "Max 300 characters.",
        }

        platform_constraint = constraints.get(platform, "Max 280 characters.")

        # Build prompt with explicit copyright constraint
        prompt = f"""Write a {platform} post about this news story.

STORY: {story_title}
KEY FACTS: {'; '.join(key_facts)}
SOURCE URLS: {source_urls[0] if source_urls else 'N/A'}

CONSTRAINTS:
- {platform_constraint}
- PARAPHRASE ONLY — never reproduce more than a short fragment (≤15 words) of the original article text
- Include exactly ONE link to the primary source at the end
- No hallucination — only use the key facts provided
- Neutral, journalistic tone
- No hashtags unless platform-specific (Instagram/Threads)

OUTPUT: Just the post text, nothing else."""

        messages = [
            {"role": "system", "content": "You are a news summarization assistant. You paraphrase — you never copy."},
            {"role": "user", "content": prompt},
        ]

        # Try Groq first
        if self.groq_client:
            try:
                result = await self._chat_completion_groq(
                    self.settings.groq_model,
                    messages,
                    max_tokens=300,
                    temperature=0.2,
                )
                return result["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"Groq failed: {e}")

        # Fallback to Cerebras
        if self.cerebras_client:
            try:
                result = await self._chat_completion_cerebras(
                    self.settings.cerebras_model,
                    messages,
                    max_tokens=300,
                    temperature=0.2,
                )
                return result["choices"][0]["message"]["content"].strip()
            except Exception as e:
                print(f"Cerebras failed: {e}")

        return None

    async def classify_relevance(
        self,
        title: str,
        body: str,
        topics: Optional[List[str]] = None,
    ) -> float:
        """Classify article relevance to target topics (0-1)."""
        if topics is None:
            topics = ["geopolitics", "international relations", "conflict", "diplomacy", "sanctions"]

        prompt = f"""Rate the relevance of this article to the topics: {', '.join(topics)}

TITLE: {title}
BODY: {body[:1000]}...

Return a JSON object: {{"score": 0.0-1.0, "reason": "brief explanation"}}"""

        messages = [
            {"role": "user", "content": prompt},
        ]

        if self.groq_client:
            try:
                result = await self._chat_completion_groq(
                    self.settings.groq_model,
                    messages,
                    max_tokens=100,
                    temperature=0.1,
                )
                content = result["choices"][0]["message"]["content"]
                data = json.loads(content)
                return float(data.get("score", 0))
            except Exception:
                pass

        if self.cerebras_client:
            try:
                result = await self._chat_completion_cerebras(
                    self.settings.cerebras_model,
                    messages,
                    max_tokens=100,
                    temperature=0.1,
                )
                content = result["choices"][0]["message"]["content"]
                data = json.loads(content)
                return float(data.get("score", 0))
            except Exception:
                pass

        return 0.5  # Default neutral

    async def close(self):
        """Close HTTP clients."""
        if self.groq_client:
            await self.groq_client.close()
        if self.cerebras_client:
            await self.cerebras_client.close()


# Singleton instance
_llm_client = None


async def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client