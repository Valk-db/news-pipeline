"""Regression test: story card must not render internal canonical entity IDs."""

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import jinja2

TEMPLATES = Path(__file__).resolve().parent.parent / "curation_ui" / "templates"


def test_story_card_hides_primary_entity_ids():
    entity_id = "c847e979-d6a7-4ac1-87f7-bd312e966733"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))
    story = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        status=SimpleNamespace(value="pending"),
        day=date(2026, 9, 24),
        gate_reason="Passed gate: 2 tier-1 units, 2 distinct owners",
        primary_entities=[entity_id],
        tier1_unit_count=2,
        distinct_owners=2,
        viewpoint_cluster_id=None,
    )
    article = SimpleNamespace(
        url="https://example.com/a",
        title="Example headline",
        source_domain="example.com",
        published_at=datetime(2026, 9, 24, 15, 38),
        source_tier=SimpleNamespace(value="tier1"),
    )
    html = env.get_template("story_card.html").render(
        item=SimpleNamespace(
            story=story,
            articles=[article],
            units=[object(), object()],
            reliability_by_domain={},
            media=[],
            snippets=[],
        )
    )
    assert entity_id not in html
    assert "entity-tag" not in html
    assert "Example headline" in html