"""Thin adapter over the real extraction code.

Design rule: the harness patches the TRANSPORT, never the prompt and never the
post-processing. ``src.enrichment.snippet_extractor.extract_snippets_from_article``
is called verbatim, so the prompt the model sees, the truncation to 8000 chars,
the 300-char cap, the 20-char minimum, the JSON fence fallback and the field
defaults are all the production ones. The only thing replaced is the object the
production function reaches for, and that object does not exist: line 42 calls
``get_llm_client()`` without awaiting it, and line 63 uses ``llm.chat`` and
``llm.model``, neither of which ``LLMClient`` has. The production function
therefore returns ``[]`` for every article, every time, and the ``except
Exception`` at line 115 logs the AttributeError and swallows it. (Reported, not
fixed -- this batch measures.)

``TransportShim`` supplies the two attributes the production code reaches for
(``.model`` from settings, ``.chat.completions.create``) and puts the
record-and-replay cache and the cost accounting at the single seam where the
request is actually made. Nothing under ``src/`` is edited.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from eval.cache import CacheEntry, ReplayCache, cache_key, input_hash, prompt_hash, sha256_text

# Sentinels used to split the rendered prompt into template and input. Control
# characters, so they cannot occur in article text.
_TITLE = "\x00<title>\x00"
_TEXT = "\x00<text>\x00"

# Production constants, read from the production module rather than retyped, so a
# change there is a change here and not a silent drift.
BODY_CHARS = 8000  # src/enrichment/snippet_extractor.py:40


def build_template(messages: list[dict[str, str]], *, body: str, knobs: dict[str, Any]) -> str:
    """The prompt with every article-specific value replaced by a sentinel.

    Reconstructing the template by substitution, rather than re-typing the
    prompt, is what keeps the harness from drifting away from the string
    production actually sends. The failure mode that silently turns an eval into
    a fiction is a prompt copied into the harness and then edited in one place
    only.
    """
    body_for_llm = body[:BODY_CHARS]
    out = []
    for m in messages:
        content = m["content"].replace(body_for_llm, _TEXT)
        for name, value in knobs.items():
            if value is not None:
                content = content.replace(str(value), f"{_TEXT}{name}{_TEXT}")
        out.append({"role": m["role"], "content": content})
    return json.dumps(out, sort_keys=True, ensure_ascii=False)


def build_input(article_id: str, body: str, knobs: dict[str, Any]) -> str:
    payload = {
        "article_id": article_id,
        "body": body[:BODY_CHARS],
        "knobs": {k: str(v) for k, v in knobs.items() if v is not None},
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Duck-types for the SDK response shape the production code reads:
#   response.choices[0].message.content.strip()   (snippet_extractor.py:70)
# --------------------------------------------------------------------------- #
@dataclass
class _Message:
    content: str


@dataclass
class _Choice:
    message: _Message
    finish_reason: str | None = None


@dataclass
class _CompletionResponse:
    choices: list[_Choice]


class _Completions:
    def __init__(self, shim: "TransportShim"):
        self._shim = shim

    async def create(self, *, model: str, messages: list[dict[str, str]], temperature: float, max_tokens: int, **_: Any):
        return await self._shim.complete(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
        )


class _Chat:
    def __init__(self, shim: "TransportShim"):
        self.completions = _Completions(shim)


class UsageRecorder:
    """Captures token counts off the raw SDK response.

    ``LLMClient._chat_completion_groq`` normalises the Groq response down to
    ``{"choices": [...]}`` and drops ``.usage`` (src/shared/llm.py:196-199), and
    nothing in the repo reads ``usage`` at all. So the only way to get real token
    numbers without editing production code is to wrap the SDK call the client
    itself makes and read what it returns.
    """

    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def install(self, llm_client: Any):
        groq = getattr(llm_client, "groq_client", None)
        completions = getattr(getattr(groq, "chat", None), "completions", None)
        if completions is None:
            return False
        original = completions.create
        recorder = self

        async def create(**kwargs):
            response = await original(**kwargs)
            usage = getattr(response, "usage", None)
            if usage is not None:
                recorder.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                recorder.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            return response

        completions.create = create
        self._restore = lambda: setattr(completions, "create", original)
        return True

    def uninstall(self) -> None:
        restore = getattr(self, "_restore", None)
        if restore is not None:
            restore()
            self._restore = None


class TransportShim:
    """What ``extract_snippets_from_article`` believes it got from
    ``get_llm_client()``. Records, replays, or forwards to the real provider."""

    def __init__(
        self,
        llm_client: Any,
        cache: ReplayCache,
        *,
        article_id: str,
        body: str,
        knobs: dict[str, Any] | None = None,
        model_override: str | None = None,
        usage: UsageRecorder | None = None,
        on_response: Callable[..., None] | None = None,
        mutate: str | None = None,
    ):
        self._llm = llm_client
        self._cache = cache
        self._article_id = article_id
        self._body = body
        self._knobs = knobs or {}
        self._model_override = model_override
        self._usage = usage or UsageRecorder()
        self._on_response = on_response
        # Set only for a mutation run. Applied to the content AFTER the cache
        # lookup and never written back, so a mutated run is free and cannot
        # contaminate the cache the baseline replays from.
        self._mutate = mutate
        self.mutation_notes: list[str] = []
        self.chat = _Chat(self)
        # Production passes model=llm.model; LLMClient has no .model, so the shim
        # reports the configured production model id (settings.groq_model).
        self.model = model_override or llm_client.settings.groq_model

    async def complete(self, *, model: str, messages: list[dict[str, str]], temperature: float, max_tokens: int) -> Any:
        template = build_template(messages, body=self._body, knobs=self._knobs)
        effective_model = self._model_override or model
        p_hash = prompt_hash(
            [{"role": m["role"], "content": template} for m in messages],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        i_hash = input_hash(self._article_id, build_input(self._article_id, self._body, self._knobs))
        key = cache_key(effective_model, p_hash, i_hash)

        cached = self._cache.get(key)
        if cached is not None:
            if self._on_response is not None:
                self._on_response(key, effective_model, p_hash, i_hash, cached.content, replayed=True)
            return _CompletionResponse(
                choices=[
                    _Choice(message=_Message(self._maybe_mutate(cached.content)),
                            finish_reason=cached.finish_reason)
                ]
            )

        before = (self._usage.prompt_tokens, self._usage.completion_tokens)
        error: str | None = None
        try:
            result = await self._llm.chat_completion(
                messages=messages, max_tokens=max_tokens, temperature=temperature
            )
        except Exception as exc:  # noqa: BLE001 - an error is a recorded outcome, not a crash
            self._cache.record_error()
            error = f"{type(exc).__name__}: {exc}"
            self._cache.put(
                CacheEntry(model=effective_model, prompt_hash=p_hash, input_hash=i_hash,
                           content="", error=error),
                key,
            )
            if self._on_response is not None:
                self._on_response(key, effective_model, p_hash, i_hash, "", replayed=False, error=error)
            raise

        content = result["choices"][0]["message"]["content"]
        used_prompt = self._usage.prompt_tokens - before[0]
        used_completion = self._usage.completion_tokens - before[1]
        self._cache.record_live(effective_model, used_prompt, used_completion)
        self._cache.put(
            CacheEntry(
                model=effective_model, prompt_hash=p_hash, input_hash=i_hash, content=content,
                prompt_tokens=used_prompt or None, completion_tokens=used_completion or None,
                total_tokens=(used_prompt + used_completion) or None,
            ),
            key,
        )
        if self._on_response is not None:
            self._on_response(key, effective_model, p_hash, i_hash, content, replayed=False)
        return _CompletionResponse(choices=[_Choice(message=_Message(self._maybe_mutate(content)))])

    def _maybe_mutate(self, content: str) -> str:
        """Corrupt the content on the way to production, if a mutation is armed.

        Runs after the cache lookup and its output is never written back, so the
        cache the baseline replays from stays clean and the mutated run costs
        zero provider requests.
        """
        if self._mutate is None:
            return content
        from eval.mutate import apply_mutation

        mutated, note = apply_mutation(self._mutate, content)
        if note:
            self.mutation_notes.append(note)
        return mutated


def call_signature(content: str) -> str:
    """Stable id for a response, for comparing two runs' outputs cheaply."""
    return sha256_text("sig", content)[:16]
