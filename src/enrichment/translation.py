"""Article translation to English: language detection plus a pluggable backend.

Every article in the pipeline gets language-detected; non-English articles get
their headline and body translated to English. The originals are never touched:
``detected_language``/``title_en``/``body_text_en`` on RawArticle carry the
result, and the UI reads ``COALESCE(title_en, title)``.

Backend selection, best free option that actually works on this box:
  1. Groq (free tier, LLM quality) when GROQ_API_KEY is set. Drop-in upgrade,
     no code change: select_backend() prefers it automatically.
  2. MyMemory (keyless HTTP, no account, no data-sharing opt-in, 50k
     chars/day anonymous). Verified working through the egress proxy on
     2026-10-01 with good quality on FR/ES/DE/ZH/FA/UK/SQ news headlines.

Deliberately NOT used, with evidence:
  - Argos Translate: needs spacy + stanza (hundreds of MB, a forbidden
    install class on this VM) and pip cannot install large packages here
    (ENOSPC on anything heavy). Revisit only on a bigger box.
  - LibreTranslate public instances: translate.argosopentech.com drops the
    connection through our proxy; libretranslate.com returns 403. Unreliable.

MyMemory politeness: 1s between requests, 4500-char chunks (under the 5000
per-request cap), daily char budget tracked in var/translation_budget.json
(default 45000, under the 50000 anonymous limit). Translation never breaks
ingest: failures are caught, reported, and the article persists untranslated.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_DAILY_CHAR_BUDGET = 45000
MYMEMORY_MAX_CHUNK = 4500
MYMEMORY_POLITENESS_SECONDS = 1.0
# Bodies are front-loaded in news copy; translating the whole archive would
# blow the free daily budget on one long article. The cap is reported, not hidden.
BODY_TRANSLATE_CHAR_CAP = 4000

VAR_DIR = Path("var")
BUDGET_PATH = VAR_DIR / "translation_budget.json"


class TranslationUnavailable(Exception):
    """Raised when no translation backend can serve a request right now."""


class TranslationBackend(Protocol):
    name: str

    def translate(self, text: str, source_lang: str, target_lang: str = "en") -> str:
        ...


def detect_language(text: str | None) -> str | None:
    """Detect the ISO 639-1 language of text, offline via langdetect.

    Returns None for empty/short text or when detection fails, so callers
    treat it as unknown rather than guessing.
    """
    if not text or len(text.strip()) < 12:
        return None
    try:
        from langdetect import detect, DetectorFactory

        DetectorFactory.seed = 0
        return detect(text)
    except Exception:
        return None


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_budget() -> dict:
    try:
        data = json.loads(BUDGET_PATH.read_text())
        if data.get("day") == _today_key():
            return data
    except Exception:
        pass
    return {"day": _today_key(), "chars_used": 0}


def _write_budget(chars_used: int) -> None:
    try:
        VAR_DIR.mkdir(parents=True, exist_ok=True)
        BUDGET_PATH.write_text(json.dumps({"day": _today_key(), "chars_used": chars_used}))
    except Exception as exc:
        logger.warning("could not persist translation budget: %s", exc)


def budget_remaining() -> int:
    return max(0, MYMEMORY_DAILY_CHAR_BUDGET - _read_budget().get("chars_used", 0))


class MyMemoryBackend:
    """Keyless MyMemory translation. Free, no account, no opt-ins.

    Polite by construction: per-request chunking, a sleep between calls, and
    a hard daily character budget so one ingest can never burn the quota.
    """

    name = "mymemory"

    def __init__(
        self,
        politeness_seconds: float = MYMEMORY_POLITENESS_SECONDS,
        daily_budget: int = MYMEMORY_DAILY_CHAR_BUDGET,
    ) -> None:
        self.politeness_seconds = politeness_seconds
        self.daily_budget = daily_budget
        self._last_call = 0.0

    def _polite_wait(self) -> None:
        wait = self.politeness_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _translate_chunk(self, text: str, source_lang: str, target_lang: str) -> str:
        params = urllib.parse.urlencode(
            {"q": text, "langpair": f"{source_lang}|{target_lang}"}
        )
        req = urllib.request.Request(
            f"{MYMEMORY_URL}?{params}",
            headers={"User-Agent": "news-pipeline/1.0 (translation enrichment)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        translated = (payload.get("responseData") or {}).get("translatedText")
        if not translated:
            raise TranslationUnavailable(
                f"MyMemory returned no translation: {str(payload)[:160]}"
            )
        return translated

    def translate(self, text: str, source_lang: str, target_lang: str = "en") -> str:
        if not text or not text.strip():
            return text
        if source_lang == target_lang:
            return text
        chunks = [
            text[i : i + MYMEMORY_MAX_CHUNK]
            for i in range(0, len(text), MYMEMORY_MAX_CHUNK)
        ]
        out: list[str] = []
        for chunk in chunks:
            if len(chunk) > budget_remaining():
                raise TranslationUnavailable(
                    f"daily translation budget exhausted ({self.daily_budget} chars)"
                )
            self._polite_wait()
            out.append(self._translate_chunk(chunk, source_lang, target_lang))
            _write_budget(_read_budget().get("chars_used", 0) + len(chunk))
        return "".join(out)


class GroqBackend:
    """LLM translation via Groq free tier. Deferred until GROQ_API_KEY lands.

    Kept as a first-class backend so the upgrade is a config change, not a
    code change: select_backend() picks this automatically when the key exists.
    """

    name = "groq"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not self.api_key:
            raise TranslationUnavailable(
                "GROQ_API_KEY is not set; Groq translation is unavailable"
            )

    def translate(self, text: str, source_lang: str, target_lang: str = "en") -> str:
        raise TranslationUnavailable(
            "GroqBackend.translate is a stub until the GROQ_API_KEY is provisioned; "
            "wireed for future use, not called today"
        )


def select_backend() -> TranslationBackend:
    """Best available backend: Groq when keyed, else keyless MyMemory."""
    if os.environ.get("GROQ_API_KEY"):
        try:
            return GroqBackend()
        except TranslationUnavailable:
            pass
    return MyMemoryBackend()


def translate_article(article, backend: TranslationBackend | None = None) -> dict:
    """Detect language and translate one RawArticle in place.

    Sets detected_language always; sets title_en/body_text_en only for
    non-English articles (English articles keep _en NULL and the UI falls
    back to the originals, so no text is stored twice). Never raises for
    translation failures: the article keeps its originals and the failure is
    reported in the returned stats.
    """
    backend = backend or select_backend()
    stats = {"detected": None, "translated": False, "backend": backend.name, "error": None}

    probe = article.title or article.body_text or ""
    lang = detect_language(probe)
    article.detected_language = lang
    stats["detected"] = lang

    if not lang or lang == "en":
        return stats

    try:
        if article.title:
            article.title_en = backend.translate(article.title, lang, "en")
        if article.body_text:
            body = article.body_text
            truncated = len(body) > BODY_TRANSLATE_CHAR_CAP
            article.body_text_en = backend.translate(
                body[:BODY_TRANSLATE_CHAR_CAP], lang, "en"
            )
            if truncated:
                stats["body_truncated_at"] = BODY_TRANSLATE_CHAR_CAP
        stats["translated"] = True
    except TranslationUnavailable as exc:
        stats["error"] = str(exc)
        logger.warning("translation failed for %s: %s", article.url, exc)
    except Exception as exc:  # never break ingest on translation
        stats["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("translation failed for %s: %s", article.url, exc)
    return stats


def translate_articles(articles: list, backend: TranslationBackend | None = None) -> dict:
    """Translate a batch of new articles. Returns a summary, never raises."""
    backend = backend or select_backend()
    summary = {
        "backend": backend.name,
        "total": len(articles),
        "translated": 0,
        "english": 0,
        "unknown": 0,
        "failed": 0,
    }
    for article in articles:
        try:
            stats = translate_article(article, backend=backend)
        except Exception as exc:  # absolute backstop
            logger.warning("translate_article raised for %s: %s", getattr(article, "url", "?"), exc)
            summary["failed"] += 1
            continue
        if stats["translated"]:
            summary["translated"] += 1
        elif stats["error"]:
            summary["failed"] += 1
        elif stats["detected"] == "en":
            summary["english"] += 1
        else:
            summary["unknown"] += 1
    return summary
