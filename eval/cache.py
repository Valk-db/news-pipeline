"""Record-and-replay cache for LLM responses.

Every LLM response is written to disk keyed by (model, prompt_hash, input_hash).
A re-run with a warm cache costs nothing and is byte-identical, which is what makes
the eval a usable gate: a score change between two runs is a real change in
extraction behaviour, not sampling noise.

Cache files live under ``eval/.cache/`` and are gitignored. They contain LLM
completions only -- never a credential, never a request header.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Where the cache lives. Relative to the repo root so it travels with the checkout.
CACHE_DIR = Path(__file__).resolve().parent / ".cache"

CACHE_VERSION = "evalset-cache/v1"


def sha256_text(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")  # unit separator: keeps ("ab","c") != ("a","bc")
    return h.hexdigest()


def prompt_hash(messages: list[dict[str, str]], *, max_tokens: int, temperature: float) -> str:
    """Hash of everything about the request that is a *choice*, not an input.

    The article text is deliberately excluded here and lives in ``input_hash``
    instead, so that editing a prompt invalidates every entry while editing one
    article invalidates only that article's entry.
    """
    rendered = json.dumps(
        {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return sha256_text(CACHE_VERSION, "prompt", rendered)


def input_hash(article_id: str, body: str) -> str:
    """Hash of the specific input. Changing the article changes this only."""
    return sha256_text(CACHE_VERSION, "input", article_id, body)


def cache_key(model: str, p_hash: str, i_hash: str) -> str:
    return sha256_text(CACHE_VERSION, "key", model, p_hash, i_hash)


@dataclass
class CacheEntry:
    model: str
    prompt_hash: str
    input_hash: str
    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
    error: str | None = None


@dataclass
class CacheStats:
    """Cost accounting. `requests` counts live HTTP calls only."""

    requests: int = 0
    hits: int = 0
    misses: int = 0
    writes: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    errors: int = 0
    models: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "errors": self.errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "models": dict(sorted(self.models.items())),
        }


class ReplayCache:
    """Filesystem cache. Sharded by the first two hex chars of the key."""

    def __init__(self, root: Path | str | None = None, *, enabled: bool = True):
        self.root = Path(root) if root is not None else CACHE_DIR
        self.enabled = enabled
        self.stats = CacheStats()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            self.stats.misses += 1
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt entry must not be trusted; treat it as a miss so the
            # caller re-fetches rather than scoring garbage.
            self.stats.misses += 1
            return None
        if raw.get("error") is not None:
            # A RECORDED FAILURE IS NOT A HIT. Provider errors get written to the
            # cache like successes so the run can report them, but replaying one
            # is worse than missing: the entry replays as empty content, the
            # extractor sees an empty completion, returns [], and the article is
            # scored 0 on every future run with no provider call to explain it.
            # One 429 from a daily token cap then silently becomes a permanent
            # zero for that article. A miss costs one request and is honest.
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return CacheEntry(
            model=raw["model"],
            prompt_hash=raw["prompt_hash"],
            input_hash=raw["input_hash"],
            content=raw["content"],
            prompt_tokens=raw.get("prompt_tokens"),
            completion_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
            finish_reason=raw.get("finish_reason"),
            error=raw.get("error"),
        )

    def put(self, entry: CacheEntry, key: str) -> None:
        if not self.enabled:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": CACHE_VERSION,
            "key": key,
            "model": entry.model,
            "prompt_hash": entry.prompt_hash,
            "input_hash": entry.input_hash,
            "content": entry.content,
            "prompt_tokens": entry.prompt_tokens,
            "completion_tokens": entry.completion_tokens,
            "total_tokens": entry.total_tokens,
            "finish_reason": entry.finish_reason,
            "error": entry.error,
        }
        # Write-then-rename so a SIGTERM mid-write cannot leave a half entry that
        # a later run would read as a valid replay.
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        self.stats.writes += 1

    def record_live(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.stats.requests += 1
        self.stats.models[model] = self.stats.models.get(model, 0) + 1
        self.stats.prompt_tokens += prompt_tokens
        self.stats.completion_tokens += completion_tokens
        self.stats.total_tokens += prompt_tokens + completion_tokens

    def record_error(self) -> None:
        self.stats.errors += 1
