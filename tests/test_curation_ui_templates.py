"""Regression test: story card must not render internal canonical entity IDs.

Rendered standalone against the same Jinja environment the app registers, so a
card can be checked without a database. `build_item` mirrors the dict shape
`_render_stories_grid` in curation_ui/curation.py hands to the template; keep
the two in step when the card changes.
"""

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import jinja2

from src.shared.safe_url import safe_url

TEMPLATES = Path(__file__).resolve().parent.parent / "curation_ui" / "templates"


def render(template: str, **context) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))
    # The app registers this filter on its Jinja env (curation_ui/app_state.py);
    # standalone renders must do the same.
    env.filters["is_safe_url"] = safe_url
    return env.get_template(template).render(**context)


def build_item(
    *,
    headline: str = "Example headline",
    primary_entities: list[str] | None = None,
    tier_chips: list[dict] | None = None,
    sources: list[dict] | None = None,
) -> SimpleNamespace:
    """The subset of the card's context this test cares about."""
    story = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        status=SimpleNamespace(value="pending"),
        day=date(2026, 9, 24),
        gate_reason="Passed gate: 2 tier-1 units, 2 distinct owners",
        primary_entities=primary_entities if primary_entities is not None else [],
        tier1_unit_count=2,
        distinct_owners=2,
        viewpoint_cluster_id=None,
    )
    if sources is None:
        sources = [
            {
                "domain": "example.com",
                "tier_num": 1,
                "published_at": datetime(2026, 9, 24, 15, 38),
                "url": "https://example.com/a",
            }
        ]
    if tier_chips is None:
        tier_chips = [{"tier": 1, "count": 2, "noun": "outlets", "meaning": "verified editorial standards"}]
    return SimpleNamespace(
        story=story,
        units=[object(), object()],
        articles=[],
        headline={
            "headline": headline,
            "headline_original": headline,
            "source_domain": "example.com",
            "translated": False,
        },
        lead_image=None,
        sources=sources,
        tier_chips=tier_chips,
        corroboration_sentence="Corroborated by 2 outlets, including tier-1 reporting.",
        verification={
            "outlets": 2,
            "tier_mix": {"t1": 2, "t2": 0, "t3": 0, "t4": 0},
            "best_tier": 1,
            "corroborated": True,
            "label": "Corroborated · 2 outlets",
        },
        freshness="3 hours ago",
        filter_query="",
    )


def test_story_card_hides_primary_entity_ids():
    entity_id = "c847e979-d6a7-4ac1-87f7-bd312e966733"
    html = render("story_card.html", item=build_item(primary_entities=[entity_id]))
    assert entity_id not in html
    assert "entity-tag" not in html
    # The headline is what identifies the card, so it must be there.
    assert "Example headline" in html


def test_story_card_leads_with_the_headline():
    """The card's first content is the headline, not a bare tier count."""
    html = render("story_card.html", item=build_item())
    headline_at = html.index("story-headline")
    first_badge_at = html.index("story-tiers")
    assert headline_at < first_badge_at
    assert "<h2 class=\"story-headline\">" in html


def test_story_card_states_a_missing_headline_honestly():
    """A story with no captured title says so instead of rendering a blank card."""
    html = render("story_card.html", item=build_item(headline="No headline captured"))
    assert "No headline captured" in html
    assert 'class="story-headline">\n      <a' in html


def test_story_card_names_every_tier_and_its_meaning():
    html = render(
        "story_card.html",
        item=build_item(
            tier_chips=[
                {"tier": 1, "count": 2, "noun": "outlets", "meaning": "verified editorial standards"},
                {"tier": 3, "count": 1, "noun": "outlet", "meaning": "social, forums, unverified"},
            ]
        ),
    )
    # No bare digits: the tier and the count are both named in words.
    assert "Tier 1" in html
    assert "2 outlets" in html
    assert "Tier 3" in html
    assert "1 outlet" in html
    # The meaning is real text, not a hover tooltip.
    assert "verified editorial standards" in html
    assert "social, forums, unverified" in html
    assert "visually-hidden" in html


def test_story_card_lists_every_attributed_source_with_domain_time_and_tier():
    html = render(
        "story_card.html",
        item=build_item(
            sources=[
                {"domain": "bbc.com", "tier_num": 1,
                 "published_at": datetime(2026, 9, 24, 15, 38), "url": "https://bbc.com/a"},
                {"domain": "example.org", "tier_num": 3,
                 "published_at": None, "url": None},
            ]
        ),
    )
    assert "2 sources" in html
    for expected in ("bbc.com", "example.org", "Tier 1", "Tier 3",
                     "24 Sep 15:38 UTC", "no timestamp", "no link",
                     "https://bbc.com/a"):
        assert expected in html, f"{expected!r} missing from the card"


def test_story_card_drops_the_debug_spew():
    """Claim, evidence, unit-id and edge internals live on the detail page now."""
    html = render("story_card.html", item=build_item())
    for gone in ("story-claims", "story-narrative", "story-topics", "story-actions",
                 "story-snippets", "story-units", "story-viewpoints", "viewpoint-badge"):
        assert gone not in html, f"{gone} is still on the card"
    assert "Confidence:" not in html
