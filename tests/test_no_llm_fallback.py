"""The deterministic no-LLM caption builder.

This file used to cover the whole curation approve/edit/save caption path: that the
unconfigured path never touched an LLM, and that a configured provider got a chance
to draft a caption. Those flows are gone (the curation queue is read-only now, see
tests/test_route_table.py), so their tests are gone with them.

`build_deterministic_caption` itself is not part of any flow. It is a pure library
function in `src/shared/llm.py` and is deliberately kept, so its tests are kept:
same inputs must give the same caption, it must use the first key fact and the first
source url, it must fall back to the story title, it must trim to the platform limit
and it must still pass caption validation.
"""

from src.shared.llm import PLATFORM_LIMITS, build_deterministic_caption


class TestBuildDeterministicCaption:
    """The no-LLM caption builder must be pure and fit the platform limit."""

    def test_same_inputs_give_same_output(self):
        args = {
            "story_title": "Test Article About Politics",
            "key_facts": ["Test Article About Politics", "Another fact"],
            "source_urls": ["https://apnews.com/article/test-1"],
            "platform": "twitter",
        }
        assert build_deterministic_caption(**args) == build_deterministic_caption(**args)

    def test_uses_first_key_fact_and_source_url(self):
        caption = build_deterministic_caption(
            story_title="Story title",
            key_facts=["First fact about the event", "Second fact"],
            source_urls=["https://apnews.com/article/test-1", "https://reuters.com/article/test-2"],
            platform="twitter",
        )
        assert caption == "First fact about the event https://apnews.com/article/test-1"

    def test_falls_back_to_story_title(self):
        caption = build_deterministic_caption(
            story_title="Only the story title",
            key_facts=[],
            source_urls=[],
            platform="twitter",
        )
        assert caption == "Only the story title"

    def test_trims_to_platform_limit_with_long_text_and_url(self):
        url = "https://example.com/a-very-long-source-url-for-a-news-article"
        caption = build_deterministic_caption(
            story_title="x" * 500,
            key_facts=["y" * 500],
            source_urls=[url],
            platform="twitter",
        )
        assert len(caption) <= PLATFORM_LIMITS["twitter"]
        assert caption.endswith(url)

    def test_result_passes_caption_validation(self):
        from src.shared.llm import validate_caption

        caption = build_deterministic_caption(
            story_title="Test Article About Politics",
            key_facts=["Test Article About Politics"],
            source_urls=["https://apnews.com/article/test-1"],
            platform="twitter",
        )
        is_valid, error = validate_caption(caption, "twitter", [])
        assert is_valid, error

