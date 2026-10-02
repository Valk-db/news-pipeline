"""Tests for the translation enrichment step.

No network: all backend calls go through a stub. Live MyMemory quality was
verified manually on 2026-10-01 (FR/ES/DE/ZH/FA/UK/SQ news headlines).
"""

import asyncio

import pytest

from src.shared.budget import MYMEMORY_CHARS, spend, used
from src.enrichment.translation import (
    MyMemoryBackend,
    TranslationUnavailable,
    detect_language,
    select_backend,
    translate_article,
    translate_articles,
)
from src.schema.models import RawArticle, SourceTier


class StubBackend:
    name = "stub"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def translate(self, text, source_lang, target_lang="en"):
        self.calls.append((text, source_lang, target_lang))
        if self.fail:
            raise TranslationUnavailable("stub failure")
        return f"[en] {text}"


def _article(title="Hello world, this is a news headline here", body="Body text here for testing."):
    return RawArticle(
        url="https://example.com/a",
        url_hash="abc123",
        title=title,
        body_text=body,
        source_domain="example.com",
        source_tier=SourceTier.TIER1,
    )


class TestDetectLanguage:
    def test_french_detected(self):
        assert detect_language(
            "Le président a annoncé de nouvelles mesures économiques après la réunion du cabinet."
        ) == "fr"

    def test_english_detected(self):
        assert detect_language(
            "The president announced new economic measures after the cabinet meeting today."
        ) == "en"

    def test_empty_returns_none(self):
        assert detect_language("") is None
        assert detect_language(None) is None

    def test_short_text_returns_none(self):
        assert detect_language("Hi there") is None


class TestTranslateArticle:
    def test_english_passthrough_leaves_en_fields_null(self):
        art = _article()
        stats = translate_article(art, backend=StubBackend())
        assert stats["detected"] == "en"
        assert stats["translated"] is False
        assert art.title_en is None
        assert art.body_text_en is None
        assert art.detected_language == "en"

    def test_non_english_gets_translated(self):
        art = _article(
            title="Le président a annoncé de nouvelles mesures économiques ce matin à Paris",
            body="Le gouvernement a publié un communiqué officiel concernant les nouvelles mesures économiques.",
        )
        stats = translate_article(art, backend=StubBackend())
        assert stats["detected"] == "fr"
        assert stats["translated"] is True
        assert art.title_en == "[en] " + art.title
        assert art.body_text_en.startswith("[en] ")
        # Originals untouched
        assert art.title.startswith("Le président")

    def test_backend_failure_never_raises_and_keeps_originals(self):
        art = _article(
            title="Le président a annoncé de nouvelles mesures économiques ce matin à Paris",
        )
        stats = translate_article(art, backend=StubBackend(fail=True))
        assert stats["translated"] is False
        assert stats["error"] is not None
        assert art.title_en is None
        assert art.detected_language == "fr"

    def test_mymemory_backend_translates_without_network(self, monkeypatch):
        backend = MyMemoryBackend(politeness_seconds=0)
        monkeypatch.setattr(
            backend, "_translate_chunk", lambda text, s, t: f"EN:{text}"
        )
        assert backend.translate("Bonjour le monde", "fr") == "EN:Bonjour le monde"

    def test_mymemory_same_language_passthrough(self):
        backend = MyMemoryBackend()
        assert backend.translate("Hello", "en", "en") == "Hello"

    def test_mymemory_spends_the_daily_character_budget(self, monkeypatch):
        """Every chunk sent to MyMemory is reserved against the day's character budget.

        The counter is the budget_counters row from conftest's budget_counter fixture,
        not the JSON file under var/ that this used to keep on an ephemeral runner.
        """
        backend = MyMemoryBackend(politeness_seconds=0)
        monkeypatch.setattr(backend, "_translate_chunk", lambda text, s, t: f"EN:{text}")

        backend.translate("x" * 100, "fr")

        assert asyncio.run(used(MYMEMORY_CHARS)) == 100

    def test_mymemory_stops_at_the_cap_including_other_runs(self):
        """A cap of 10 chars with 8 already spent: a 2-char chunk fits, the next is refused.

        Those 8 were spent by another run, which is the whole reason the counter is a table
        and not a file on a runner that no longer exists.
        """
        asyncio.run(spend(MYMEMORY_CHARS, 8, 10))
        backend = MyMemoryBackend(politeness_seconds=0, daily_budget=10)
        sent = []

        def fake_chunk(text, source, target):
            sent.append(text)
            return f"EN:{text}"

        backend._translate_chunk = fake_chunk

        assert backend.translate("ab", "fr") == "EN:ab"
        assert asyncio.run(used(MYMEMORY_CHARS)) == 10

        # Nothing left, so nothing is sent at all.
        with pytest.raises(TranslationUnavailable):
            backend.translate("c", "fr")
        assert sent == ["ab"]

    def test_mymemory_refuses_to_translate_without_a_counter(self, monkeypatch):
        """No reachable counter means no translation: the anonymous quota is the scarce thing."""
        from src.shared import budget as budget_module

        monkeypatch.setattr(budget_module, "_get_engine", lambda: None)
        backend = MyMemoryBackend(politeness_seconds=0)
        backend._translate_chunk = lambda text, s, t: pytest.fail("must not call MyMemory")

        with pytest.raises(TranslationUnavailable):
            backend.translate("Bonjour le monde", "fr")


class TestTranslateArticles:
    def test_batch_summary_counts(self):
        arts = [
            _article(title="The president announced new economic measures after the cabinet meeting today."),
            _article(title="Le président a annoncé de nouvelles mesures économiques ce matin à Paris"),
            _article(title="x"),  # too short to detect
        ]
        summary = translate_articles(arts, backend=StubBackend())
        assert summary["total"] == 3
        assert summary["translated"] == 1
        assert summary["english"] == 1
        assert summary["unknown"] == 1
        assert summary["failed"] == 0

    def test_batch_never_raises_on_backend_failure(self):
        arts = [_article(title="Le président a annoncé de nouvelles mesures économiques ce matin")]
        summary = translate_articles(arts, backend=StubBackend(fail=True))
        assert summary["failed"] == 1
        assert summary["translated"] == 0


class TestSelectBackend:
    def test_defaults_to_mymemory_without_key(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert select_backend().name == "mymemory"

    def test_groq_preferred_when_keyed(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
        assert select_backend().name == "groq"
