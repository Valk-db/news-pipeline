"""The evidence locker's articles must be usable by the gate and grouping.

Measured 2026-10-02 on dev, after the lazy-import change in run.py: the locker
is now the only scheduled thing that stamps, and it runs on its own cron
(.github/workflows/transparency-stamp.yml, --sources rss_evidence) rather than
inside a tiered run. That is deliberate, so the articles it writes have to stand
on their own: whatever build_reporting_units, build_stories and the corroboration
counter read off a RawArticle, the locker has to have filled in.

This is a static coupling guard, not a behavioural one, and it is two-sided on
purpose. It asserts the locker SETS each required field, and it also asserts
each field is still actually READ by a named consumer. Without the second
half the list below rots silently: a field nothing reads any more would keep
passing, and the next person to trust the list would be trusting fiction.

Keep the list honest, do not grow it. A field is only in it if a named consumer
reads it; if you remove the consumer, remove the row in the same commit.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import src.ingestion.rss_evidence as rss_evidence
from src.verification import corroboration, stories, units

REPO_ROOT = Path(__file__).resolve().parents[1]

# field -> (the module that reads it, why it matters)
REQUIRED = {
    "body_text": (units, "shingle_text() and the longest-body representative pick"),
    "source_domain": (units, "get_owner_group() -> owner_groups / tier1_owner_groups"),
    "source_tier": (units, "tier_counts, and the tier1_owner_groups split"),
    "published_at": (units, "the UTC day bucket, alongside fetched_at"),
    "entities": (stories, "primary_entities for PERSON/ORG/GPE"),
    "url": (corroboration, "ArticleEvidence.as_json, stored in gate_decisions"),
    "content_hash": (corroboration, "ArticleEvidence.as_json, stored in gate_decisions"),
}


def _locker_kwargs() -> set[str]:
    """The keyword names on the RawArticle(...) call the locker builds."""
    # getsource hands back an indented function body; ast.parse needs a module.
    source = textwrap.dedent(inspect.getsource(rss_evidence.build_articles))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RawArticle"
        ):
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError("build_articles no longer constructs a RawArticle(...)")


def test_locker_sets_every_field_the_gate_and_grouping_read():
    missing = sorted(set(REQUIRED) - _locker_kwargs())
    assert not missing, (
        "the evidence locker no longer sets these fields, so articles it writes "
        f"cannot be grouped or gated: {missing}. Either the locker has to fill "
        "them in, or a consumer has stopped needing them -- in which case fix "
        "REQUIRED in this file, do not leave the drift."
    )


@pytest.mark.parametrize("field", sorted(REQUIRED))
def test_every_required_field_is_still_read_by_its_named_consumer(field):
    """The anti-rot half. A field nothing reads any more is not a requirement."""
    module, why = REQUIRED[field]
    # corroboration reads it off a frozen dataclass, not a RawArticle, so its
    # as_json body is the place to look rather than the whole module.
    scope = (
        inspect.getsource(module.ArticleEvidence.as_json)
        if module is corroboration
        else inspect.getsource(module)
    )
    assert field in scope, (
        f"REQUIRED lists {field!r} as needed by {module.__name__} ({why}), but "
        f"nothing in {module.__name__} reads it any more. Drop the row from "
        "REQUIRED in this file in the same commit that stopped reading it."
    )


def test_the_consumers_named_here_are_the_real_ones():
    """If these modules move, this file is guarding nothing. Fail loudly instead."""
    for module in (units, stories, corroboration):
        path = Path(inspect.getfile(module)).resolve()
        assert path.is_relative_to(REPO_ROOT), f"{module.__name__} moved out of the repo: {path}"
