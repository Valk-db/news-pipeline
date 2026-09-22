"""Tests for caption generation validation."""

from src.shared.llm import validate_caption, PLATFORM_LIMITS


class TestValidateCaption:
    def test_empty_caption(self):
        is_valid, error = validate_caption("", "twitter", ["source text"])
        assert not is_valid
        assert "empty" in error.lower()

    def test_whitespace_only_caption(self):
        is_valid, error = validate_caption("   ", "twitter", ["source text"])
        assert not is_valid
        assert "empty" in error.lower()

    def test_valid_caption_twitter(self):
        caption = "Breaking: New policy announced in Washington today. https://apnews.com/article"
        is_valid, error = validate_caption(caption, "twitter", ["Different source text here"])
        assert is_valid
        assert error == ""

    def test_exceeds_twitter_limit(self):
        caption = "a" * 281
        is_valid, error = validate_caption(caption, "twitter", ["source"])
        assert not is_valid
        assert "280" in error

    def test_exceeds_bluesky_limit(self):
        caption = "a" * 301
        is_valid, error = validate_caption(caption, "bluesky", ["source"])
        assert not is_valid
        assert "300" in error

    def test_exceeds_threads_limit(self):
        caption = "a" * 501
        is_valid, error = validate_caption(caption, "threads", ["source"])
        assert not is_valid
        assert "500" in error

    def test_exceeds_instagram_limit(self):
        caption = "a" * 2201
        is_valid, error = validate_caption(caption, "instagram", ["source"])
        assert not is_valid
        assert "2200" in error

    def test_case_insensitive_platform(self):
        caption = "Valid caption here https://example.com"
        is_valid, _ = validate_caption(caption, "TWITTER", ["source"])
        assert is_valid
        is_valid, _ = validate_caption(caption, "Twitter", ["source"])
        assert is_valid

    def test_paraphrase_violation_ngram_overlap(self):
        source = "The president announced a new policy today in Washington regarding the economy"
        caption = "The president announced a new policy today in Washington https://example.com"
        is_valid, error = validate_caption(caption, "twitter", [source], min_ngram_overlap=6)
        assert not is_valid
        assert "n-gram" in error.lower()

    def test_paraphrase_ok_no_ngram_overlap(self):
        source = "The president announced a new policy today in Washington regarding economic reforms"
        caption = "Biden unveils economic reform plan in Washington https://example.com"
        is_valid, error = validate_caption(caption, "twitter", [source], min_ngram_overlap=6)
        assert is_valid
        assert error == ""

    def test_multiple_source_texts(self):
        source1 = "President Biden signed the bill into law on Monday"
        source2 = "The legislation passed with bipartisan support"
        caption = "New law enacted after presidential signature https://example.com"
        is_valid, error = validate_caption(caption, "twitter", [source1, source2], min_ngram_overlap=6)
        assert is_valid
        assert error == ""

    def test_override_validation_allows_save(self):
        source = "The president announced a new policy today in Washington regarding the economy"
        caption = "The president announced a new policy today in Washington https://example.com"
        is_valid, error = validate_caption(caption, "twitter", [source], min_ngram_overlap=6, allow_override=True)
        assert is_valid  # With override, validation passes but logs warning

    def test_unknown_platform_defaults_to_280(self):
        caption = "a" * 281
        is_valid, error = validate_caption(caption, "unknown_platform", ["source"])
        assert not is_valid
        assert "280" in error


class TestPlatformLimits:
    def test_twitter_limit(self):
        assert PLATFORM_LIMITS["twitter"] == 280

    def test_x_limit(self):
        assert PLATFORM_LIMITS["x"] == 280

    def test_bluesky_limit(self):
        assert PLATFORM_LIMITS["bluesky"] == 300

    def test_threads_limit(self):
        assert PLATFORM_LIMITS["threads"] == 500

    def test_instagram_limit(self):
        assert PLATFORM_LIMITS["instagram"] == 2200

    def test_linkedin_limit(self):
        assert PLATFORM_LIMITS["linkedin"] == 3000

    def test_facebook_limit(self):
        assert PLATFORM_LIMITS["facebook"] == 63206