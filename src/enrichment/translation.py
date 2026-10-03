"""Article translation to English: language detection plus a pluggable backend.

Every article in the pipeline gets language-detected; non-English articles get
their headline and body translated to English. The originals are never touched:
``detected_language``/``title_en``/``body_text_en`` on RawArticle carry the
result, and the UI reads ``COALESCE(title_en, title)``.

Backend selection, best free option that actually works on this box:
  1. Groq (free tier, LLM quality) when GROQ_API_KEY is set. Drop-in upgrade,
     no code change: select_backend() prefers it automatically. An LLM is the
     only one of the two that keeps proper nouns intact: MyMemory rendered
     "Pezeshkian" as "doctors" on 2026-10-01, and a news translation that
     rewrites a president's name is worse than no translation.
  2. MyMemory (keyless HTTP, no account, no data-sharing opt-in, 50k
     chars/day anonymous). Verified working through the egress proxy on
     2026-10-01 with good quality on FR/ES/DE/ZH/FA/UK/SQ news headlines.

Deliberately NOT used, with evidence:
  - Argos Translate: needs spacy + stanza (hundreds of MB, a forbidden
    install class on this VM) and pip cannot install large packages here
    (ENOSPC on anything heavy). Revisit only on a bigger box.
  - LibreTranslate public instances: translate.argosopentech.com drops the
    connection through our proxy; libretranslate.com returns 403. Unreliable.

MyMemory politeness: 1s between requests, 480-char chunks (the anonymous tier
rejects anything over 500 characters, measured 2026-10-02 -- see
MYMEMORY_MAX_CHUNK), and a daily char budget of 45000 counted in
budget_counters. That counter was a JSON file under var/, a
gitignored directory on an ephemeral runner, so the cap was really per run and
two runs a day could send twice the anonymous limit. Translation never breaks
ingest: failures are caught, reported, and the article persists untranslated.

Groq politeness: 3s between requests, a daily request budget of 300 counted in the
same table under its own name (groq_translation_requests), and one retry on a 429
that waits exactly as long as the response's retry-after says. Sharing the LLM
client's row would let a long translation batch spend the budget that caption and
classification work -- which runs first -- was counted against.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from src.shared.budget import GROQ_TRANSLATION_REQUESTS, MYMEMORY_CHARS, spend_sync as spend

logger = logging.getLogger(__name__)

MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_DAILY_CHAR_BUDGET = 45000
# Measured 2026-10-02, not assumed: MyMemory's anonymous tier rejects any query
# over 500 CHARACTERS with HTTP 200 + responseStatus 403 and
# responseData.translatedText = "QUERY LENGTH LIMIT EXCEEDED. MAX ALLOWED QUERY
# : 500 CHARS". 499 and 500 chars pass, 501 fails, every length above that fails
# identically. This used to be 4500 ("under the 5000 per-request cap"), which is
# wrong by 9x, and the consequence was silent: the error string is a perfectly
# well-formed non-empty translation, so it was stored in body_text_en and NER
# then dutifully extracted ORG "CHARS" and PERSON "MAX" from it. Chunks are now
# under the real limit, and a response that looks like a rejection is refused
# rather than stored (see _looks_like_rejection).
MYMEMORY_MAX_CHUNK = 480
MYMEMORY_POLITENESS_SECONDS = 1.0
# MyMemory reports failures inside a 200 response, so the error text has to be
# recognised rather than inferred from a status code.
MYMEMORY_REJECTION_MARKERS = (
    "QUERY LENGTH LIMIT EXCEEDED",
    "MYMEMORY WARNING",
    "YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY",
    "INVALID LANGUAGE PAIR",
    "PLEASE SELECT TWO DISTINCT LANGUAGES",
)
# Bodies are front-loaded in news copy; translating the whole archive would
# blow the free daily budget on one long article. The cap is reported, not hidden.
BODY_TRANSLATE_CHAR_CAP = 4000

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Live-checked 2026-10-02 against the free-tier key: openai/gpt-oss-20b answers, and
# it is the only chat model that key may use besides its 120b sibling (not on the free
# tier) and the prompt guards. Pinned as a constant, like the MyMemory URL above,
# rather than read from GROQ_MODEL: reasoning_effort below is a gpt-oss parameter, so
# a model swap made in one place and not the other would 400 into silence.
GROQ_MODEL = "openai/gpt-oss-20b"
# gpt-oss reasons before it answers and the reasoning tokens come out of the same
# max_tokens budget: asked for 10 tokens it returned an empty completion with 8 of
# them spent on reasoning. 4096 covers a 4000-char body's translation (worst seen:
# ~2500 tokens) plus its reasoning, and costs nothing when unused, because
# generation stops at the stop token.
GROQ_MAX_TOKENS = 4096
GROQ_TIMEOUT = 30
# The free tier is 30 requests/minute and 8000 tokens/minute (x-ratelimit headers,
# 2026-10-02), so the gap only has to respect the request ceiling: 3s is 20 requests
# a minute, under the 30/min. The token window cannot be spaced out for -- a
# 4000-char body is ~3000 tokens, so two of them fill the minute -- and guessing a
# gap wide enough would add minutes to every batch. It is left to the one
# rate-limit retry below, which is what the provider's own wait time is for.
GROQ_POLITENESS_SECONDS = 3.0
# Sized under the free tier's measured allowance, not guessed: the response headers on
# 2026-10-02 carried x-ratelimit-limit-requests: 1000. 300 requests is ~150 articles
# (a title and a body each) and leaves the LLM client's own 900/day row alone.
GROQ_DAILY_REQUEST_BUDGET = 300
# A rate-limited request is not a failed one, and Groq says how long to wait: the 429
# on 2026-10-02 carried `retry-after: 20` and a body reading "Limit 8000, Used 7600,
# Requested 3061 ... Please try again in 19.9575s". One wait and one retry is the
# difference between an article translated now and one that waits for the next run.
# A second 429 means the window is still full, which is a genuine "not now".
GROQ_RETRY_AFTER_FALLBACK_SECONDS = 20.0
GROQ_RETRY_AFTER_CEILING_SECONDS = 60.0

# One system message, one rule set. The proper-noun rule is the whole reason Groq is
# worth the round trip: MyMemory turned "Pezeshkian" into "doctors" on 2026-10-01.
_TRANSLATION_SYSTEM = (
    "You are a professional news translator. You translate the whole text, you never "
    "summarize it, and you never comment on it."
)
_TRANSLATION_RULES = (
    "Rules:\n"
    "- Output only the translation: no preamble, no notes, no wrapping quotes.\n"
    "- Translate every sentence, including repeated ones. Never summarize or shorten.\n"
    "- Proper nouns (people, places, organizations) must stay recognizable: keep the\n"
    "  original spelling or its standard English transliteration (for example\n"
    "  \"Pezeshkian\"), and never translate what a name means.\n"
    "- A word that could be a person's name is a name, even when it also spells an\n"
    "  ordinary word and even when the language adds a case ending to it.\n"
    "  «مسعود پزشکیان» is \"Masoud Pezeshkian\", not \"Masoud the physician\".\n"
    "- Keep numbers, dates, units, months and titles as written.\n"
)

class TranslationUnavailable(Exception):
    """Raised when no translation backend can serve a request right now."""


class TranslationBackend(Protocol):
    name: str

    def translate(self, text: str, source_lang: str, target_lang: str = "en") -> str:
        ...


def detect_language(text: str | None) -> str | None:
    """Detect the ISO 639-1 language of text, offline via langdetect.

    Returns None for empty/short text or when detection fails, so callers
    treat it as unknown rather than guessing. Give it as much text as you have:
    see the probe in translate_article() for why a bare headline is not enough.
    """
    if not text or len(text.strip()) < 12:
        return None
    try:
        from langdetect import detect, DetectorFactory

        DetectorFactory.seed = 0
        return detect(text)
    except Exception:
        return None


def _looks_like_rejection(translated: str, status: object) -> bool:
    """True when MyMemory's 200 response is actually a refusal.

    Two independent signals, because either alone is insufficient: the quota
    rejection comes back with responseStatus 200 and only the marker text, and
    the length rejection comes back with status 403 whose translatedText is the
    complaint. Trusting a status code alone is how the length bug hid.
    """
    if status not in (None, 200, "200"):
        return True
    upper = translated.upper()
    return any(marker in upper for marker in MYMEMORY_REJECTION_MARKERS)


class MyMemoryBackend:
    """Keyless MyMemory translation. Free, no account, no opt-ins.

    Polite by construction: per-request chunking, a sleep between calls, and a
    hard daily character budget in budget_counters, reserved chunk by chunk, so
    no number of runs can burn the quota.
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
        # A rejection arrives as HTTP 200 with the complaint in the body, so
        # responseStatus has to be checked and so does the text itself: storing
        # the complaint as the translation is worse than having none, because
        # downstream stages then treat an error string as an article.
        status = payload.get("responseStatus")
        if _looks_like_rejection(translated, status):
            raise TranslationUnavailable(
                f"MyMemory rejected the request (status={status}): {translated[:120]}"
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
            # Reserved before the call, so two runners cannot both find room for the
            # same chunk. A refused reservation means the daily cap is spent or the
            # counter is unreachable, and the safe reading of both is: stop.
            if spend(MYMEMORY_CHARS, len(chunk), self.daily_budget) is None:
                raise TranslationUnavailable(
                    f"daily translation budget exhausted ({self.daily_budget} chars)"
                )
            self._polite_wait()
            out.append(self._translate_chunk(chunk, source_lang, target_lang))
        return "".join(out)


class GroqBackend:
    """LLM translation via Groq's free tier, the upgrade MyMemory cannot make.

    An LLM keeps proper nouns intact, which is the failure MyMemory had: on the
    2026-10-01 check it rendered "Pezeshkian" as "doctors". One request per call --
    Groq's context holds a whole article, so there is no MyMemory-style chunking --
    three seconds between calls, and one retry when Groq says how long to wait.

    Polite by construction the way MyMemoryBackend is: every request is reserved
    against the day's budget in budget_counters before it is sent, so no number of
    runs can burn the free tier. Translation is best-effort enrichment, so every
    failure mode -- no key, 401/402/429/5xx, a timeout, a truncated, empty or
    malformed completion -- surfaces as TranslationUnavailable and leaves the
    article untranslated instead of breaking ingest.
    """

    name = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        politeness_seconds: float = GROQ_POLITENESS_SECONDS,
        daily_budget: int = GROQ_DAILY_REQUEST_BUDGET,
    ) -> None:
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not self.api_key:
            raise TranslationUnavailable(
                "GROQ_API_KEY is not set; Groq translation is unavailable"
            )
        self.politeness_seconds = politeness_seconds
        self.daily_budget = daily_budget
        self._last_call = 0.0

    def _polite_wait(self) -> None:
        wait = self.politeness_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _retry_after_seconds(exc: urllib.error.HTTPError) -> float:
        """How long Groq asked us to wait, from the header it puts on a 429.

        Clamped so a missing, unparseable or absurd value cannot turn a rate limit
        into either a busy loop or an hour of sleep; the ceiling is above the
        longest wait ever seen (57s of x-ratelimit-reset-tokens, 2026-10-02).
        """
        header = exc.headers.get("retry-after") if exc.headers else None
        if not header:
            return GROQ_RETRY_AFTER_FALLBACK_SECONDS
        try:
            return min(max(float(header), 0.0), GROQ_RETRY_AFTER_CEILING_SECONDS)
        except ValueError:
            return GROQ_RETRY_AFTER_FALLBACK_SECONDS

    def _complete(self, text: str, source_lang: str, target_lang: str) -> str:
        """One chat completion, or TranslationUnavailable. Never any other exception."""
        body = json.dumps(
            {
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": _TRANSLATION_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"Translate the text below from {source_lang} to {target_lang}.\n\n"
                            f"{_TRANSLATION_RULES}\nTEXT:\n{text}"
                        ),
                    },
                ],
                "max_tokens": GROQ_MAX_TOKENS,
                "temperature": 0,
                # gpt-oss reasons before it answers; "low" keeps that to a handful of
                # tokens. The API rejects "none" (400, checked 2026-10-02), so the
                # only lever is how much, not whether.
                "reasoning_effort": "low",
            }
        ).encode("utf-8")
        raw = ""
        for attempt in (0, 1):
            # A new Request every attempt. Reusing one does not survive this egress
            # proxy: measured 2026-10-02, a second urlopen on the same object comes
            # back RemoteDisconnected instead of a response, which would turn every
            # rate-limit retry into a "connection failed".
            req = urllib.request.Request(
                GROQ_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "news-pipeline/1.0 (translation enrichment)",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=GROQ_TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as exc:
                # 429 is the one failure worth waiting out: it costs no tokens and
                # Groq's own retry-after is the wait it wants, so one retry is the
                # difference between a translated row and one that waits for the
                # next run. The reservation already spent covers it -- an attempt
                # the provider refused is not work done. 401/402/5xx are not worth
                # a retry on a free tier; the article keeps its originals and the
                # backfill retries it.
                if exc.code == 429 and not attempt:
                    wait = self._retry_after_seconds(exc)
                    logger.info("Groq rate limited; waiting %ss before one retry", wait)
                    time.sleep(wait)
                    continue
                detail = exc.read()[:160].decode("utf-8", "replace")
                raise TranslationUnavailable(f"Groq HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                # urlopen surfaces a timeout as the socket's TimeoutError and a refused
                # connection as URLError, which is an OSError. All of it is "not now".
                raise TranslationUnavailable(f"Groq request failed: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslationUnavailable(f"Groq returned non-JSON: {raw[:160]}") from exc
        try:
            choice = (data.get("choices") or [{}])[0]
            finish_reason = choice.get("finish_reason")
            content = ((choice.get("message") or {}).get("content") or "").strip()
        except (AttributeError, IndexError, KeyError, TypeError) as exc:
            raise TranslationUnavailable(
                f"Groq response was malformed: {raw[:160]}"
            ) from exc
        if finish_reason == "length":
            # Half a body stored as body_text_en would be silently wrong, and the
            # row stays eligible for the backfill, so refuse instead of truncating.
            raise TranslationUnavailable(
                f"Groq hit max_tokens ({GROQ_MAX_TOKENS}); refusing a truncated translation"
            )
        if not content:
            raise TranslationUnavailable(f"Groq returned no translation: {raw[:160]}")
        return content

    def translate(self, text: str, source_lang: str, target_lang: str = "en") -> str:
        if not text or not text.strip():
            return text
        if source_lang == target_lang:
            return text
        # Reserved before the call, for the same reason as MyMemory: a refused
        # reservation means the cap is spent or the counter is unreachable, and the
        # safe reading of both is: do not send.
        if spend(GROQ_TRANSLATION_REQUESTS, 1, self.daily_budget) is None:
            raise TranslationUnavailable(
                f"daily Groq translation budget exhausted ({self.daily_budget} requests)"
            )
        self._polite_wait()
        return self._complete(text, source_lang, target_lang)


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

    # Detect on title AND body together. langdetect needs enough text to work and
    # has no confidence floor worth trusting here: measured on 2026-10-02, a bare
    # headline from the English-tier feeds (BBC/Guardian/NPR/France24) came back
    # as no/da/fr/nl at p=0.46-1.00 for 5 of 109 items, and each miss spent
    # MyMemory quota "translating" English copy. Title + body is right for 109/109.
    probe = "\n".join(part for part in (article.title, article.body_text) if part)
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


def refresh_entities_after_translation(
    articles: list, top_n: int | None = None
) -> dict:
    """Re-run entity extraction on the English text a translation just produced.

    Why this exists, measured rather than argued: ingestion extracts entities
    from the ORIGINAL body (src/ingestion/rss.py, in process_feed_entry), and
    extraction runs en_core_web_sm, which is an English model. A French,
    Turkish or Chinese article therefore arrives with an empty entity map and
    stays inert -- no canonical entity, no story merge, no corroboration, no
    topic label -- no matter how good the translation is.

    On the dev database (2026-10-02): 28 non-English articles, every one of
    them with a translated body, and only 5 carrying usable entities -- 3 of
    those from the GDELT static join, 2 from the English-NER path. 23 had
    nothing. Those 23 are the ones this function is about, and the cost of
    having no entities is specific rather than cosmetic: build_stories()
    builds a story's canonical entities from the unit's representative
    article, and a unit with no entities creates a story with empty
    primary_entities, which build_stories() then skips when matching later
    units. Such a story is one unit forever, so the corroboration gate
    (>=2 units, >=2 owners) can never be evaluated for it.
    Reporting units themselves are not affected -- those cluster by shingle
    containment on body_text, not by entities -- so the loss is at the story
    layer and above, which is where the gate lives.

    After this step, on those same 28 rows: 0 with no canonical entity (was
    23), all 28 sharing at least one canonical entity with an existing story
    and with an English article, and 2 already past the 0.4 Jaccard attach
    threshold against today's stories.

    So translation without this step bought a translated string and nothing
    else. This runs the same extractor over body_text_en, in the same thread-
    offloaded way, and only for articles that actually have translated text --
    an English article's entities were already extracted from English text and
    are left exactly as they are.

    Returns a summary, never raises: an article that fails extraction keeps the
    entities it had.
    """
    from src.utils.ner import extract_entities_top_n

    summary = {"eligible": 0, "refreshed": 0, "entities_found": 0, "errors": 0}
    for article in articles:
        lang = getattr(article, "detected_language", None)
        body_en = getattr(article, "body_text_en", None)
        if not lang or lang == "en" or not body_en:
            continue
        summary["eligible"] += 1
        try:
            entities = extract_entities_top_n(body_en, top_n=top_n)
        except Exception as exc:  # never break ingest on NER
            summary["errors"] += 1
            logger.warning(
                "entity refresh failed for %s: %s", getattr(article, "url", "?"), exc
            )
            continue
        if not entities:
            continue
        article.entities = entities
        summary["refreshed"] += 1
        summary["entities_found"] += sum(len(v) for v in entities.values())
    return summary
