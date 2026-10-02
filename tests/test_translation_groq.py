"""Tests for GroqBackend, the preferred backend whenever GROQ_API_KEY is set.

No network: urlopen is replaced. The prompt itself is asserted, because the thing
this backend buys over MyMemory is proper nouns that survive the round trip -- a
real miss on 2026-10-01 was MyMemory rendering "Pezeshkian" as "doctors" -- and a
prompt that stops asking for that is a silent quality regression nothing else
would catch. Live Groq quality was verified manually on 2026-10-02.
"""

import asyncio
import io
import json
import urllib.error

import pytest

from src.enrichment import translation
from src.enrichment.translation import (
    GROQ_MODEL,
    GroqBackend,
    TranslationUnavailable,
    translate_article,
)
from src.schema.models import RawArticle, SourceTier
from src.shared.budget import GROQ_TRANSLATION_REQUESTS, spend, used

# A real headline from the dev corpus, name and all: the word MyMemory mangled.
HEADLINE = "مسعود پزشکیان در نشست خبری درباره وضعیت اقتصادی کشور صحبت کرد"
TRANSLATION = "President Pezeshkian spoke at a news conference about the economy."


def _completion(content: str, finish_reason: str = "stop") -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"completion_tokens": 20},
        }
    ).encode()


class _Response:
    """Just enough of the urlopen context manager for _complete()."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._payload


@pytest.fixture
def groq(monkeypatch):
    """Record what would have been sent, and answer with `reply`."""

    def install(reply=None):
        sent: list[dict] = []
        if reply is None:
            reply = _completion(TRANSLATION)
        if not isinstance(reply, bytes):  # dicts in the matrix below, on the wire as bytes
            reply = json.dumps(reply).encode()

        def fake_urlopen(req, timeout=None):
            sent.append(
                {
                    "url": req.full_url,
                    "headers": dict(req.headers),
                    "body": json.loads(req.data.decode()),
                    "timeout": timeout,
                }
            )
            return _Response(reply() if callable(reply) else reply)

        monkeypatch.setattr(translation.urllib.request, "urlopen", fake_urlopen)
        return sent

    return install


@pytest.fixture
def groq_fails(monkeypatch):
    """Make the call fail the way a provider or the proxy would."""

    def install(error):
        def fake_urlopen(req, timeout=None):
            raise error(req) if callable(error) else error

        monkeypatch.setattr(translation.urllib.request, "urlopen", fake_urlopen)

    return install


def _http_error(code: int, detail: bytes = b'{"error":"denied"}'):
    return lambda req: urllib.error.HTTPError(req.full_url, code, "nope", {}, io.BytesIO(detail))


def _backend(**kwargs) -> GroqBackend:
    kwargs.setdefault("api_key", "gsk_test")
    kwargs.setdefault("politeness_seconds", 0)
    return GroqBackend(**kwargs)


def _article(**overrides) -> RawArticle:
    fields = {
        "url": "https://example.com/fa",
        "url_hash": "hfa",
        "title": HEADLINE,
        "body_text": HEADLINE + " اقتصاد کشور در وضعیت دشواری قرار دارد.",
        "source_domain": "example.com",
        "source_tier": SourceTier.TIER1,
    }
    return RawArticle(**{**fields, **overrides})


class TestGroqSuccess:
    def test_translates_and_posts_an_openai_shaped_request(self, groq):
        sent = groq()

        assert _backend().translate(HEADLINE, "fa", "en") == TRANSLATION

        assert len(sent) == 1
        assert sent[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
        # The key travels in the Authorization header, never in the body.
        assert sent[0]["headers"]["Authorization"] == "Bearer gsk_test"
        assert "gsk_test" not in json.dumps(sent[0]["body"])
        assert sent[0]["body"]["model"] == GROQ_MODEL
        assert sent[0]["body"]["temperature"] == 0
        # gpt-oss spends its max_tokens on reasoning before it answers: too small a
        # budget comes back as an empty completion, which we would refuse.
        assert sent[0]["body"]["max_tokens"] >= 1024
        assert sent[0]["body"]["reasoning_effort"] == "low"
        assert sent[0]["body"]["messages"][-1]["content"].endswith(HEADLINE)

    def test_asks_for_the_translation_of_the_named_languages(self, groq):
        sent = groq()

        _backend().translate(HEADLINE, "fa", "en")

        assert "from fa to en" in sent[0]["body"]["messages"][-1]["content"]

    def test_prompt_preserves_proper_nouns(self, groq):
        """The rule MyMemory needed and did not have: names stay recognizable."""
        sent = groq()

        _backend().translate(HEADLINE, "fa", "en")

        prompt = sent[0]["body"]["messages"][-1]["content"].lower()
        assert "proper nouns" in prompt
        assert "transliteration" in prompt
        assert "never translate what a name means" in prompt
        assert "never summarize" in prompt
        assert "only the translation" in prompt

    def test_spends_one_request_of_the_daily_budget(self, groq):
        """Counted in the pipeline's table, not in an int in a process."""
        groq()

        _backend().translate(HEADLINE, "fa", "en")

        assert asyncio.run(used(GROQ_TRANSLATION_REQUESTS)) == 1

    def test_sleeps_between_calls_and_not_before_the_first(self, monkeypatch):
        """1s between calls: the free tier is rate limited, and a first call has
        nothing to be polite to."""
        slept: list[float] = []
        monkeypatch.setattr(translation.time, "sleep", slept.append)
        monkeypatch.setattr(
            translation.urllib.request,
            "urlopen",
            lambda req, timeout=None: _Response(_completion(TRANSLATION)),
        )
        backend = _backend(politeness_seconds=1.0)

        backend.translate(HEADLINE, "fa", "en")
        assert slept == []
        backend.translate(HEADLINE, "fa", "en")
        assert len(slept) == 1

    def test_timeout_is_bounded(self, groq):
        sent = groq()

        _backend().translate(HEADLINE, "fa", "en")

        assert sent[0]["timeout"] == translation.GROQ_TIMEOUT


class TestGroqErrorContract:
    """Every failure is TranslationUnavailable. Nothing else reaches the caller."""

    def test_no_key_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(TranslationUnavailable, match="GROQ_API_KEY"):
            GroqBackend()

    @pytest.mark.parametrize("code", [400, 401, 402, 403, 429, 500, 503])
    def test_http_status_is_unavailable(self, groq_fails, code):
        groq_fails(_http_error(code))

        with pytest.raises(TranslationUnavailable, match=f"Groq HTTP {code}"):
            _backend().translate(HEADLINE, "fa", "en")

    def test_the_key_is_never_in_the_error(self, groq_fails):
        groq_fails(_http_error(401, b'{"error":{"message":"invalid api key"}}'))

        with pytest.raises(TranslationUnavailable) as exc:
            _backend(api_key="gsk_super_secret").translate(HEADLINE, "fa", "en")
        assert "gsk_super_secret" not in str(exc.value)

    @pytest.mark.parametrize(
        "reply",
        [
            _completion(""),                # reasoning ate the whole max_tokens budget
            _completion("   \n "),          # whitespace is not a translation
            {"choices": []},                # no choice
            {"error": {"message": "quota exceeded"}},
            {"choices": [{"message": {}}]},  # content missing
            {"choices": "not-a-list"},       # malformed shape
            b"not json at all",              # proxy error page
        ],
    )
    def test_empty_or_malformed_response_is_unavailable(self, groq, reply):
        groq(reply)

        with pytest.raises(TranslationUnavailable):
            _backend().translate(HEADLINE, "fa", "en")

    def test_truncated_completion_is_refused(self, groq):
        """A body cut mid-sentence, stored as body_text_en, would be silently wrong."""
        groq(_completion(TRANSLATION[:40], "length"))

        with pytest.raises(TranslationUnavailable, match="truncated"):
            _backend().translate(HEADLINE, "fa", "en")

    @pytest.mark.parametrize(
        "error",
        [
            urllib.error.URLError("proxy refused"),
            TimeoutError("timed out"),
            ConnectionResetError("reset by peer"),
        ],
    )
    def test_network_failure_is_unavailable(self, groq_fails, error):
        groq_fails(error)

        with pytest.raises(TranslationUnavailable, match="Groq request failed"):
            _backend().translate(HEADLINE, "fa", "en")

    def test_401_leaves_the_article_untranslated(self, groq_fails):
        """The pipeline's promise: ingest never breaks, the originals are kept."""
        groq_fails(_http_error(401))

        stats = translate_article(_article(), backend=_backend())

        assert stats["translated"] is False
        assert "Groq HTTP 401" in stats["error"]
        assert stats["detected"] == "fa"


class TestGroqBudget:
    def test_refuses_to_send_once_the_day_is_spent(self, groq):
        """A spent cap means no request at all, not a smaller one."""
        asyncio.run(spend(GROQ_TRANSLATION_REQUESTS, 2, 2))
        sent = groq()

        with pytest.raises(TranslationUnavailable, match="budget exhausted"):
            _backend(daily_budget=2).translate(HEADLINE, "fa", "en")
        assert sent == []

    def test_refuses_to_translate_without_a_counter(self, monkeypatch):
        """Unreadable is not unspent: the free tier's quota is the scarce thing."""
        from src.shared import budget as budget_module

        monkeypatch.setattr(budget_module, "_get_engine", lambda: None)
        monkeypatch.setattr(
            translation.urllib.request,
            "urlopen",
            lambda req, timeout=None: pytest.fail("must not call Groq"),
        )

        with pytest.raises(TranslationUnavailable, match="budget exhausted"):
            _backend().translate(HEADLINE, "fa", "en")

    def test_spending_stops_at_the_cap_including_other_runs(self, groq):
        asyncio.run(spend(GROQ_TRANSLATION_REQUESTS, 1, 2))  # spent by another run
        sent = groq()
        backend = _backend(daily_budget=2)

        assert backend.translate(HEADLINE, "fa", "en") == TRANSLATION
        with pytest.raises(TranslationUnavailable):
            backend.translate(HEADLINE, "fa", "en")
        assert len(sent) == 1


class TestGroqPassthrough:
    def test_same_language_is_not_a_request(self, groq):
        sent = groq()

        assert _backend().translate("Hello", "en", "en") == "Hello"
        assert sent == []

    def test_empty_text_is_not_a_request(self, groq):
        sent = groq()

        assert _backend().translate("   ", "fa", "en") == "   "
        assert sent == []


class TestGroqThroughThePipeline:
    def test_translate_article_fills_the_english_columns(self, groq):
        """The bug this batch fixes: the preferred backend now writes the columns."""
        groq()

        article = _article()
        stats = translate_article(article, backend=_backend())

        assert stats["backend"] == "groq"
        assert stats["detected"] == "fa"
        assert stats["translated"] is True
        assert article.title_en == TRANSLATION
        assert article.body_text_en == TRANSLATION
        # Originals untouched, always.
        assert article.title == HEADLINE

    def test_select_backend_still_prefers_groq_when_keyed(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
        assert translation.select_backend().name == "groq"
