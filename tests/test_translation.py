"""Tests for the translation enrichment step.

No network: all backend calls go through a stub. Live MyMemory quality was
verified manually on 2026-10-01 (FR/ES/DE/ZH/FA/UK/SQ news headlines).
"""

import pytest

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

    def test_short_title_detected_from_body(self):
        """A headline too short to detect on is read together with the body.

        langdetect on a bare headline misfires into low-resource languages at high
        confidence (measured 2026-10-02: 5/109 English-tier headlines became
        no/da/fr/nl at p=0.46-1.00), and every miss then spent MyMemory quota
        translating English copy. The body settles it.
        """
        art = _article(
            title="No",
            body="Le président a annoncé de nouvelles mesures économiques après la réunion du cabinet.",
        )
        stats = translate_article(art, backend=StubBackend())
        assert stats["detected"] == "fr"
        assert stats["translated"] is True

    def test_english_title_with_nonenglish_looking_body_is_english(self):
        """Real misfire: an English headline over an English body stays English."""
        art = _article(
            title="France demands belt-tightening in 2027 budget as investors sour on its debt",
            body=(
                "Paris - The French government on Tuesday presented a budget for 2027 that "
                "holds spending flat while debt costs keep rising, drawing criticism from "
                "investors who warned the deficit would widen for a third year."
            ),
        )
        stats = translate_article(art, backend=StubBackend())
        assert stats["detected"] == "en"
        assert stats["translated"] is False
        assert art.title_en is None

    def test_body_only_article_is_detected(self):
        """No title at all: the body alone is still a usable probe."""
        art = _article(
            title=None,
            body="El gobierno anunció nuevas medidas económicas después de la reunión del gabinete.",
        )
        stats = translate_article(art, backend=StubBackend())
        assert stats["detected"] == "es"

    def test_no_text_at_all_stays_unknown(self):
        art = _article(title=None, body=None)
        stats = translate_article(art, backend=StubBackend())
        assert stats["detected"] is None
        assert art.detected_language is None

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
        monkeypatch.setattr(
            "src.enrichment.translation.budget_remaining", lambda: 10**9
        )
        monkeypatch.setattr(
            "src.enrichment.translation._write_budget", lambda chars: None
        )
        assert backend.translate("Bonjour le monde", "fr") == "EN:Bonjour le monde"

    def test_mymemory_same_language_passthrough(self):
        backend = MyMemoryBackend()
        assert backend.translate("Hello", "en", "en") == "Hello"

    def test_mymemory_budget_exhaustion_raises(self, monkeypatch):
        backend = MyMemoryBackend(politeness_seconds=0)
        monkeypatch.setattr("src.enrichment.translation.budget_remaining", lambda: 0)
        with pytest.raises(TranslationUnavailable):
            backend.translate("Bonjour le monde entier", "fr")


class TestTranslateArticles:
    def test_batch_summary_counts(self):
        arts = [
            _article(title="The president announced new economic measures after the cabinet meeting today."),
            _article(title="Le président a annoncé de nouvelles mesures économiques ce matin à Paris"),
            _article(title="x", body=None),  # nothing to detect on
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
