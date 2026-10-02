"""Tests for the translation enrichment step.

No network: all backend calls go through a stub. Live MyMemory quality was
verified manually on 2026-10-01 (FR/ES/DE/ZH/FA/UK/SQ news headlines).
"""

import asyncio

import pytest

from scripts import backfill_translations
from scripts.backfill_translations import NEEDS_TRANSLATION
from src.shared.budget import MYMEMORY_CHARS, spend, used
from src.enrichment.translation import (
    MyMemoryBackend,
    TranslationUnavailable,
    detect_language,
    select_backend,
    refresh_entities_after_translation,
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


class TestBackfillSelection:
    """The backfill must select rows with work left, not rows nobody has touched.

    The filter it used to carry was detected_language IS NULL, which reached only
    the never-detected rows. A row that was detected as foreign and then failed to
    translate -- budget spent, transient network, headline translated and body not --
    keeps its detected_language and was therefore invisible to it and never
    retried. Live dev census of the two filters: 1 row reached, 8 needed it.
    """

    def test_filter_reaches_never_detected_and_half_translated_rows(self):
        assert "detected_language.is.null" in NEEDS_TRANSLATION
        assert "detected_language.neq.en" in NEEDS_TRANSLATION
        assert "title_en.is.null" in NEEDS_TRANSLATION
        assert "body_text.not.is.null" in NEEDS_TRANSLATION
        assert "body_text_en.is.null" in NEEDS_TRANSLATION

    def test_filter_never_selects_finished_rows(self):
        """English rows are finished, not pending: translating them spends quota to
        duplicate text every reader already coalesces from `title`. And the old
        null-only selection must not come back: it is what left the
        half-translated rows unreachable in the first place.
        """
        assert "detected_language.eq.en" not in NEEDS_TRANSLATION
        assert "detected_language=is.null" not in NEEDS_TRANSLATION

    def test_main_requests_only_rows_needing_work(self, monkeypatch):
        """The filter is wired into the request, not just defined."""
        paths: list[str] = []

        class FakeRest:
            def __init__(self, url, key):
                pass

            def get(self, path):
                paths.append(path)
                if "detected_language&limit" in path:  # the column probe
                    return []
                if f"&{NEEDS_TRANSLATION}" in path:
                    return [
                        {
                            "id": "row-1",
                            "url": "https://example.com/a",
                            "title": "Le président a annoncé de nouvelles mesures ce matin",
                            "body_text": "Le gouvernement a publié un communiqué officiel.",
                        }
                    ]
                return []

            def patch(self, path, body):
                raise AssertionError("dry run must not write")

        monkeypatch.setattr(backfill_translations, "Rest", FakeRest)
        monkeypatch.setattr(
            backfill_translations,
            "load_env",
            lambda: {"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"},
        )
        monkeypatch.setattr(
            backfill_translations, "select_backend", lambda: StubBackend()
        )

        assert backfill_translations.main(["--dry-run"]) == 0
        assert any(f"&{NEEDS_TRANSLATION}" in path for path in paths)


class TestRefreshEntitiesAfterTranslation:
    """Non-English articles used to arrive with an empty entity map.

    Ingestion extracts entities from the ORIGINAL body with en_core_web_sm,
    an English model, so a French or Turkish article was ingested and then
    inert: no canonical entity, no reporting-unit grouping, no corroboration.
    These tests pin the fix -- re-extract from body_text_en -- and the two
    things it must not do (touch English articles, or raise).
    """

    def test_translated_article_gets_entities_from_english_body(self, monkeypatch):
        seen = {}

        def fake_extract(text, top_n=None):
            seen["text"] = text
            seen["top_n"] = top_n
            return {"PERSON": ["Volodymyr Zelenskyy"], "GPE": ["Kyiv"]}

        monkeypatch.setattr("src.utils.ner.extract_entities_top_n", fake_extract)
        art = _article(title="Le président à Kyiv", body="Corps en français.")
        art.detected_language = "fr"
        art.body_text_en = "The president arrived in Kyiv on Tuesday to meet ministers."

        summary = refresh_entities_after_translation([art], top_n=3)

        assert summary == {
            "eligible": 1, "refreshed": 1, "entities_found": 2, "errors": 0,
        }
        assert art.entities == {"PERSON": ["Volodymyr Zelenskyy"], "GPE": ["Kyiv"]}
        # The English translation is the input, not the French original, and
        # the entity cap is the one the settings carry.
        assert seen["text"] == "The president arrived in Kyiv on Tuesday to meet ministers."
        assert seen["top_n"] == 3

    def test_english_article_is_left_untouched(self, monkeypatch):
        monkeypatch.setattr(
            "src.utils.ner.extract_entities_top_n",
            lambda text, top_n=None: pytest.fail("English articles must not be re-extracted"),
        )
        art = _article()
        art.detected_language = "en"
        art.entities = {"GPE": ["London"]}

        summary = refresh_entities_after_translation([art])

        assert summary["eligible"] == 0
        assert summary["refreshed"] == 0
        assert art.entities == {"GPE": ["London"]}

    def test_untranslated_article_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            "src.utils.ner.extract_entities_top_n",
            lambda text, top_n=None: pytest.fail("nothing translated to extract from"),
        )
        art = _article(title="Le président a annoncé des mesures")
        art.detected_language = "fr"
        art.body_text_en = None

        summary = refresh_entities_after_translation([art])

        assert summary["eligible"] == 0

    def test_extraction_failure_keeps_existing_entities(self, monkeypatch):
        def boom(text, top_n=None):
            raise RuntimeError("spaCy model missing")

        monkeypatch.setattr("src.utils.ner.extract_entities_top_n", boom)
        art = _article()
        art.detected_language = "tr"
        art.body_text_en = "The minister met the delegation in Ankara."
        art.entities = {"GPE": ["Ankara"]}

        summary = refresh_entities_after_translation([art])

        assert summary["eligible"] == 1
        assert summary["refreshed"] == 0
        assert summary["errors"] == 1
        assert art.entities == {"GPE": ["Ankara"]}

    def test_empty_extraction_is_not_written(self, monkeypatch):
        monkeypatch.setattr(
            "src.utils.ner.extract_entities_top_n", lambda text, top_n=None: {}
        )
        art = _article()
        art.detected_language = "es"
        art.body_text_en = "Short translated body with nothing to extract."

        summary = refresh_entities_after_translation([art])

        assert summary["refreshed"] == 0
        assert summary["eligible"] == 1
        assert art.entities is None
