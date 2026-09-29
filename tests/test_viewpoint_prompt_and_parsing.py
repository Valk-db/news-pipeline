"""Regression tests for viewpoint clustering internals.

The older viewpoint tests mock the LLM with a response keyed by the real unit ids no
matter what the prompt says, so they could not notice that the prompt never showed the
model any unit ids (every unit then fell back to the default label). These tests pin the
prompt contract and the response parsing directly.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.schema.models import Story
from src.verification.stories import (
    DEFAULT_VIEWPOINT_LABEL,
    _build_viewpoint_prompt,
    _get_recent_stories_with_entities,
    _normalize_viewpoint_label,
    _parse_viewpoint_response,
)


def _unit_texts(n: int = 3) -> list[dict]:
    return [
        {"unit_id": uuid.uuid4(), "text": f"excerpt number {i}", "source_tier": "tier1"}
        for i in range(n)
    ]


def test_prompt_contains_every_unit_id():
    units = _unit_texts(4)
    prompt = _build_viewpoint_prompt(units)
    for u in units:
        assert str(u["unit_id"]) in prompt, "model must be shown each unit_id to key its answer"
    assert "excerpt number 3" in prompt


def test_parse_plain_json():
    uid = str(uuid.uuid4())
    assert _parse_viewpoint_response(json.dumps({uid: "Pro_Govt"})) == {uid: "pro_govt"}


def test_parse_fenced_json():
    uid = str(uuid.uuid4())
    content = f"```json\n{json.dumps({uid: 'opposition'})}\n```"
    assert _parse_viewpoint_response(content) == {uid: "opposition"}


def test_parse_json_wrapped_in_prose():
    uid = str(uuid.uuid4())
    content = f"Sure! Here you go: {json.dumps({uid: 'local'})} Hope that helps."
    assert _parse_viewpoint_response(content) == {uid: "local"}


def test_parse_rejects_non_object_and_garbage():
    with pytest.raises(ValueError):
        _parse_viewpoint_response("[1, 2, 3]")
    with pytest.raises(json.JSONDecodeError):
        _parse_viewpoint_response("no json here at all")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Pro Govt  ", "pro_govt"),
        ("International/Foreign", "international_foreign"),
        ("", DEFAULT_VIEWPOINT_LABEL),
        (None, DEFAULT_VIEWPOINT_LABEL),
        (42, DEFAULT_VIEWPOINT_LABEL),
        ("x" * 100, "x" * 32),
    ],
)
def test_normalize_label(raw, expected):
    assert _normalize_viewpoint_label(raw) == expected


@pytest.mark.asyncio
async def test_recent_stories_excludes_viewpoint_children(db_session):
    """New units must attach to the parent story, never to a viewpoint slice of it."""
    now = datetime.now(timezone.utc)
    parent = Story(
        id=uuid.uuid4(), day=now, primary_entities=["e1", "e2"],
        status=Story.Status.PENDING, updated_at=now,
    )
    child = Story(
        id=uuid.uuid4(), day=now, primary_entities=["e1", "e2"],
        status=Story.Status.PENDING, viewpoint_cluster_id=parent.id, updated_at=now,
    )
    db_session.add_all([parent, child])
    await db_session.commit()

    rows = await _get_recent_stories_with_entities(db_session, now - timedelta(hours=48))

    assert [sid for sid, _ in rows] == [parent.id]