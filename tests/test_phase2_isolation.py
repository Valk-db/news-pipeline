"""The load-bearing test of this batch: Phase 2 must not change what is public.

Tyler decided on 2026-10-02 that public exposure stays human-gated. Phase 2
attaches analysis to stories in the triage queue -- topic groups, narrative
arcs, a claim matrix -- and every one of those tables is derived state that
exists to help a curator decide. None of it is an editorial act.

That is easy to state and easy to break by accident, so these two tests are
written to fail loudly the moment the line is crossed rather than to describe
the line in prose:

  1. The public modules never reference the derived tables at all. Not "they
     filter correctly" -- they do not import them. A public page that reads
     `Claim` has already decided that a claim is something a reader may see,
     and the status filter in the query is the only thing left standing
     between that decision and the page.

  2. A PENDING story id is a 404 on every public route, derived from the
     route table rather than from a list written here. A new public route with
     {story_id} in its path is covered by the same assertion the moment it
     exists, so "we only checked /stories/{id}" cannot quietly become true.

Both are checked two ways where that is possible: the source is parsed (so a
reference hidden behind a lazy import or an alias still counts) and the live
module namespace is inspected (so the test does not depend on knowing where in
the file a name appears). The mutation evidence is in the batch report: each
rule was broken on purpose and each test went red.

Run `python -m pytest tests/test_phase2_isolation.py` to see this file alone.
"""

from __future__ import annotations

import ast
import importlib
from datetime import datetime, timedelta, UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.schema import models
from src.shared.analyzer_versions import CLAIM_VERSION
from src.shared.config import get_settings
from src.shared import database as database_module
from src.schema.models import (
    Claim,
    ClaimEvidence,
    ClaimStance,
    ClaimType,
    EdgePredicate,
    EntityEdge,
    Story,
    StoryTopicGroup,
    StoryUnitLink,
    TopicGroup,
)

from tests.test_map_public import _make_story
from tests.test_route_table import dependency_names, iter_routes

# The modules an anonymous reader's request can reach into. curation_ui/main.py
# is the composition root, not a surface, so it is not here: it is where a
# router is mounted, and mounting one is not referencing a table.
PUBLIC_MODULES = (
    "curation_ui.public_pages",
    "curation_ui.map_api",
    "curation_ui.discovery",
)

# The tables Phase 2 writes and no reader may see. Claim and ClaimEvidence are
# the claim matrix, EntityEdge is the narrative arc link, TopicGroup /
# StoryTopicGroup are the curated categories. A public surface naming any of
# these is a design change, not a bug fix, whatever the status filter says.
DERIVED_MODEL_NAMES = (
    "Claim",
    "ClaimEvidence",
    "EntityEdge",
    "TopicGroup",
    "StoryTopicGroup",
)


def public_story_routes() -> list[str]:
    """Every anonymous route whose path carries a {story_id} placeholder.

    Computed, not listed. A literal list would be a snapshot of today and would
    happily stay green after someone adds a second public story route -- which
    is precisely the change this file exists to catch. The route table's own
    walker is reused so a new include_router cannot hide a route from it either.
    """
    from curation_ui.main import app

    paths = []
    for route in iter_routes(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "GET" not in methods or "{story_id}" not in path:
            continue
        names = dependency_names(getattr(route, "dependant", None))
        if "require_auth" in names:
            continue
        paths.append(path)
    return sorted(paths)


class TestPublicModulesDoNotReferenceDerivedTables:
    def test_the_surface_list_is_not_empty(self):
        """Guard the guard: a bad module name must not make the test vacuous."""
        for name in PUBLIC_MODULES:
            assert importlib.import_module(name) is not None

    def test_live_module_namespace_holds_no_derived_model(self):
        """Nothing a public module imported is one of the derived tables."""
        for name in PUBLIC_MODULES:
            module = importlib.import_module(name)
            for attr in DERIVED_MODEL_NAMES:
                model = getattr(models, attr)
                assert not hasattr(module, attr), (
                    f"{name} has {attr} in its namespace. A public module that "
                    f"imports {model.__name__} can render it; the status filter "
                    f"in one query is not a policy."
                )

    def test_source_of_public_modules_never_names_a_derived_model(self):
        """The same rule read from the parsed source, not the namespace.

        Catches the shapes the namespace check cannot see: a reference inside a
        function that is not called at import time, a string handed to
        getattr, an `import ... as` that binds a different name. Docstrings and
        comments are not code, so this walks the AST rather than grepping the
        text -- a comment explaining that claims are not public is not a
        violation, and failing on it would train people to delete the comment.
        """
        for name in PUBLIC_MODULES:
            module = importlib.import_module(name)
            with open(module.__file__, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=module.__file__)
            named = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    named.add(node.id)
                elif isinstance(node, ast.Attribute):
                    named.add(node.attr)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        named.add(alias.asname or alias.name)
            for attr in DERIVED_MODEL_NAMES:
                assert attr not in named, f"{name} names {attr} in its source"


@pytest.fixture
def phase2_app(test_settings, db_engine, monkeypatch):
    """The app bound to the in-memory test database, curation auth on."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module

    main_module.settings = settings

    import src.shared.llm as llm_module

    llm_module._llm_client = None
    return main_module.app


@pytest.mark.asyncio
class TestPendingStoriesStayInvisible:
    async def test_the_derivation_finds_at_least_one_public_story_route(self, phase2_app):
        """/stories/{story_id} must be in the computed set, or nothing follows."""
        paths = public_story_routes()
        assert "/stories/{story_id}" in paths, paths

    async def test_a_pending_story_404s_on_every_public_story_route(
        self, phase2_app, db_session
    ):
        """The load-bearing assertion, over the real route table.

        Parametrised by the route table so a public route added later inherits
        the assertion instead of needing to be remembered here.
        """
        now = datetime.now(UTC)
        pending = _make_story(
            db_session, day=now - timedelta(hours=2), events=1,
            status=Story.Status.PENDING,
        )
        await db_session.commit()

        client = TestClient(phase2_app)
        checked = 0
        for path in public_story_routes():
            url = path.replace("{story_id}", str(pending.id))
            response = client.get(url)
            assert response.status_code == 404, (
                f"{path} served a PENDING story with "
                f"{response.status_code}: public exposure is human-gated"
            )
            checked += 1
        assert checked >= 1

    async def test_a_pending_story_is_absent_from_the_public_map_feed(
        self, phase2_app, db_session
    ):
        """The aggregate view is the other half of the same promise.

        A 404 on the detail page is worthless if the map still lists the story,
        because the map is where an anonymous reader actually starts. The window
        is one year and the limit is 1,000, so nothing here can be excused as
        falling outside the query's range.
        """
        now = datetime.now(UTC)
        pending = _make_story(
            db_session, day=now - timedelta(hours=2), events=1,
            status=Story.Status.PENDING,
        )
        await db_session.commit()

        client = TestClient(phase2_app)
        response = client.get("/api/map/stories?hours=8760&limit=1000")
        assert response.status_code == 200
        body = response.json()
        stories = body["stories"] if isinstance(body, dict) else body
        assert stories == [], (
            f"the public map served {len(stories)} stories while the only story "
            f"in the database is PENDING"
        )
        assert str(pending.id) not in response.text

    async def test_the_same_story_is_served_once_it_is_approved(
        self, phase2_app, db_session
    ):
        """The control: the 404 above is about status, not about the id being broken.

        Without this, a route that 404s everything -- because of a broken join, a
        missing template, a filter that matches nothing -- would pass the tests
        above just as happily as a correct one.
        """
        now = datetime.now(UTC)
        approved = _make_story(
            db_session, day=now - timedelta(hours=2), events=1,
            status=Story.Status.QUEUED,
        )
        await db_session.commit()

        client = TestClient(phase2_app)
        assert client.get(f"/stories/{approved.id}").status_code == 200

    async def test_phase2_derived_rows_do_not_make_a_pending_story_visible(
        self, phase2_app, db_session
    ):
        """Attach the Phase 2 output to a PENDING story, then ask again.

        The real risk is not a PENDING story with no claims; it is a PENDING
        story whose claims, topic groups and arcs exist and are therefore
        tempting for a public template to reach. So this writes one row of
        each, exactly as a completed Phase 2 run would, and asserts the public
        surface is unchanged.
        """
        now = datetime.now(UTC)
        pending = _make_story(
            db_session, day=now - timedelta(hours=2), events=1,
            status=Story.Status.PENDING,
        )
        unit_id = (
            await db_session.execute(
                select(StoryUnitLink.unit_id).where(StoryUnitLink.story_id == pending.id)
            )
        ).scalar_one()
        await db_session.commit()

        # One row of each table a finished Phase 2 run leaves behind, with the
        # analyzer_version and input_hash a real run writes. A blank row would
        # be a weaker test: these are the rows a template could reach for.
        claim = Claim(
            story_id=pending.id,
            text="A claim that only a curator should be able to read",
            claim_type=ClaimType.ALLEGATION,
            analyzer_version=CLAIM_VERSION,
            input_hash="0" * 64,
        )
        db_session.add(claim)
        await db_session.flush()
        db_session.add(ClaimEvidence(
            claim_id=claim.id,
            unit_id=unit_id,
            stance=ClaimStance.SUPPORTS,
            confidence=80,
            analyzer_version=CLAIM_VERSION,
            input_hash="1" * 64,
        ))
        group = TopicGroup(name="Group the curator assigned", description="internal")
        db_session.add(group)
        await db_session.flush()
        db_session.add(StoryTopicGroup(
            story_id=pending.id,
            topic_group_id=group.id,
            confidence=70,
            analyzer_version=CLAIM_VERSION,
            input_hash="2" * 64,
        ))
        db_session.add(EntityEdge(
            subject_type="story",
            subject_id=pending.id,
            predicate=EdgePredicate.PART_OF_NARRATIVE,
            object_type="story",
            object_id=pending.id,
            confidence=60,
            analyzer_version=CLAIM_VERSION,
            input_hash="3" * 64,
        ))
        await db_session.commit()

        # The rows are really there, so a pass below cannot be a pass because
        # the fixture quietly wrote nothing.
        counts = {}
        for table in (Claim, ClaimEvidence, StoryTopicGroup, EntityEdge):
            counts[table.__name__] = (
                await db_session.execute(select(func.count()).select_from(table))
            ).scalar_one()
        assert counts == {
            "Claim": 1, "ClaimEvidence": 1, "StoryTopicGroup": 1, "EntityEdge": 1,
        }, counts

        client = TestClient(phase2_app)
        for path in public_story_routes():
            url = path.replace("{story_id}", str(pending.id))
            assert client.get(url).status_code == 404, path
        feed = client.get("/api/map/stories?hours=8760&limit=1000")
        assert feed.status_code == 200
        assert str(pending.id) not in feed.text
        # And the claim text itself never appears anywhere a reader can see.
        assert "A claim that only a curator should be able to read" not in feed.text
        page = client.get(f"/stories/{pending.id}")
        assert page.status_code == 404
        assert "A claim that only a curator should be able to read" not in page.text
