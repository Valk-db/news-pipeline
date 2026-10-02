"""Tests for the public inclusion-proof permalinks (/proof/{article_id}).

Covers the four states the page must render honestly: a valid proof that
verifies end to end, the pending state for an unstamped article, the pending
state for a stamped-but-uncheckpointed article, and tamper detection when the
stored payload no longer matches its leaf hash. Plus a 404 for unknown ids
and a unit check that the displayed proof steps fold to the same root the
verifier checks.
"""

import hashlib
import html
import re
import subprocess
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared import database as database_module
from src.schema.models import (
    Base,
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.transparency.checkpoint import HmacDevSigner, build_checkpoint, sign_checkpoint
from src.transparency.log import (
    SqlAlchemyMerkleLog,
    TransparencyBase,
    leaf_hash,
)
from src.transparency.proofs import inclusion_proof, proof_root, proof_steps
from src.transparency.store import latest_checkpoint_covering, save_checkpoint


SECRET = b"proof-permalink-test-secret"


@pytest.fixture
def test_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
async def db_engine(test_settings):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(TransparencyBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture
def app_with_db(test_settings, db_engine):
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    return app


def _make_article(db_session, **overrides):
    url = overrides.get("url", "https://example.test/article-1")
    article = RawArticle(
        id=uuid.uuid4(),
        url=url,
        url_hash=hashlib.sha256(url.encode()).hexdigest(),
        title=overrides.get("title", "Test headline"),
        body_text="Body text.",
        source_domain="example.test",
        source_tier=SourceTier.TIER1,
        published_at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 10, 1, 12, 5, tzinfo=timezone.utc),
        content_hash="ab" * 32,
        log_index=overrides.get("log_index"),
    )
    db_session.add(article)
    return article


async def _stamp_article(db_session, article, log):
    """Stamp like rss_evidence.stamp_observations does, minus the network."""
    payload = {
        "type": "rss_evidence",
        "url": article.url,
        "source_domain": article.source_domain,
        "fetched_at": article.fetched_at.isoformat(),
        "body_sha256": article.content_hash,
        "title": article.title,
    }
    entry = await log.append(payload)
    article.log_index = entry.index
    await db_session.flush()
    return entry


async def _checkpoint_all(db_session, log, *, key_id="test-key"):
    signed = sign_checkpoint(
        await build_checkpoint(log, await log.size()),
        HmacDevSigner(SECRET, key_id=key_id),
    )
    await save_checkpoint(db_session, signed)
    await db_session.flush()
    return signed


class TestProofSteps:
    def test_steps_fold_to_the_same_root_as_proof_root(self):
        leaves = [leaf_hash({"n": i}) for i in range(7)]
        from src.transparency.checkpoint import merkle_levels

        levels = merkle_levels(leaves)
        index = 5
        # Build the sibling path the same way inclusion_proof does.
        siblings = []
        position = index
        for level in levels[:-1]:
            sib = position ^ 1
            if sib >= len(level):
                sib = position
            siblings.append(level[sib])
            position //= 2

        steps = proof_steps(leaves[index], index, tuple(siblings))
        assert len(steps) == len(siblings)
        assert steps[-1].digest == proof_root(leaves[index], index, tuple(siblings))
        # Even/odd ordering is what the page tells readers to reproduce.
        for step in steps:
            if step.position % 2 == 0:
                assert step.left == (leaves[index] if step.level == 0 else steps[step.level - 1].digest)
            else:
                assert step.right == (leaves[index] if step.level == 0 else steps[step.level - 1].digest)

    def test_single_entry_tree_has_no_steps(self):
        leaf = leaf_hash({"only": "one"})
        assert proof_steps(leaf, 0, ()) == []
        assert proof_root(leaf, 0, ()) == leaf


class TestProofPage:
    async def test_valid_proof_renders_verified(self, app_with_db, db_session):
        log = SqlAlchemyMerkleLog(db_session)
        article = _make_article(db_session)
        entry = await _stamp_article(db_session, article, log)
        await _stamp_article(db_session, _make_article(db_session, url="https://example.test/article-2"), log)
        signed = await _checkpoint_all(db_session, log)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/proof/{article.id}")
        assert response.status_code == 200
        body = response.text
        assert "Inclusion verified" in body
        assert entry.leaf_hash_hex in body
        assert signed.checkpoint.merkle_root.hex() in body
        assert "Recompute it yourself" in body
        assert "sha256(0x01 || " in body
        # The sibling hashes are on the page for hand verification.
        proof = await inclusion_proof(log, entry.index, signed.checkpoint.tree_size)
        for sibling in proof.siblings:
            assert sibling.hex() in body
        # Canonical payload shown so the leaf is recomputable.
        assert "rss_evidence" in body
        # Server-rendered: no JS needed for the proof content.
        assert "canonical payload" in body.lower()

    async def test_printed_verify_recipe_actually_recomputes_a_level(
        self, app_with_db, db_session
    ):
        """The command the page prints must run and land on the digest it prints.

        A verify affordance nobody can run is not an affordance, so this shells
        out to the recipe rendered in the page rather than re-deriving it here.
        """
        log = SqlAlchemyMerkleLog(db_session)
        article = _make_article(db_session)
        entry = await _stamp_article(db_session, article, log)
        await _stamp_article(db_session, _make_article(db_session, url="https://example.test/a2"), log)
        await _stamp_article(db_session, _make_article(db_session, url="https://example.test/a3"), log)
        signed = await _checkpoint_all(db_session, log)
        await db_session.commit()

        body = TestClient(app_with_db).get(f"/proof/{article.id}").text
        recipe = html.unescape(
            re.search(r'<pre class="proof-pre" id="verify-recipe">(.*?)</pre>', body, re.S).group(1)
        )
        levels = re.findall(
            r'<td class="mono">\d+</td>\s*<td class="mono">\d+</td>\s*'
            r'<td class="mono">([0-9a-f]{64})</td>\s*<td class="mono">([0-9a-f]{64})</td>\s*'
            r'<td class="mono">([0-9a-f]{64})</td>',
            body,
        )
        assert levels, "the page rendered no proof steps"
        left, right, digest = levels[-1]
        result = subprocess.run(
            ["bash", "-c", recipe.replace("$LEFT_HEX", left).replace("$RIGHT_HEX", right)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == digest
        # ... and the last level is the root the checkpoint signs.
        assert digest == signed.checkpoint.merkle_root.hex()
        assert entry.index == 0

    async def test_unstamped_article_renders_pending(self, app_with_db, db_session):
        article = _make_article(db_session, log_index=None)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/proof/{article.id}")
        assert response.status_code == 200
        body = response.text
        assert "Proof pending" in body
        assert "not been stamped" in body
        assert "Inclusion verified" not in body

    async def test_stamped_but_uncheckpointed_renders_pending(self, app_with_db, db_session):
        log = SqlAlchemyMerkleLog(db_session)
        article = _make_article(db_session)
        await _stamp_article(db_session, article, log)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/proof/{article.id}")
        assert response.status_code == 200
        body = response.text
        assert "Proof pending" in body
        assert "not yet checkpointed" in body
        # The stamped entry is still shown honestly.
        assert "log entry #0" in body

    async def test_tampered_payload_is_detected_not_hidden(self, app_with_db, db_session):
        from src.transparency.log import MerkleLogEntry

        log = SqlAlchemyMerkleLog(db_session)
        article = _make_article(db_session)
        entry = await _stamp_article(db_session, article, log)
        await _checkpoint_all(db_session, log)
        await db_session.commit()

        # Tamper with the stored payload behind the log's back.
        tampered = dict(entry.payload)
        tampered["title"] = "A completely different headline"
        await db_session.execute(
            update(MerkleLogEntry)
            .where(MerkleLogEntry.index == entry.index)
            .values(payload=tampered)
        )
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/proof/{article.id}")
        assert response.status_code == 200
        body = response.text
        assert "Verification failed" in body
        assert "Inclusion verified" not in body
        assert "fail" in body  # the checklist shows the failed check

    async def test_unknown_article_is_404(self, app_with_db, db_session):
        client = TestClient(app_with_db)
        response = client.get(f"/proof/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_page_is_public_no_auth_required(self, app_with_db, db_session):
        article = _make_article(db_session, log_index=None)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/proof/{article.id}")
        # Anonymous: no 401, the pending page renders.
        assert response.status_code == 200

    async def test_public_story_page_links_a_stamped_article_to_its_permalink(
        self, app_with_db, db_session
    ):
        """The entry point: a reader on the story page can reach the proof."""
        article = _make_article(db_session, log_index=3)
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime(2026, 10, 1, tzinfo=timezone.utc),
            representative_article_id=article.id,
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={"AP": 1},
            tier1_owner_groups={"AP": 1},
        )
        story = Story(
            id=uuid.uuid4(),
            day=datetime(2026, 10, 1, tzinfo=timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.QUEUED,
            tier1_unit_count=1,
            tier2_unit_count=0,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=1,
        )
        db_session.add_all([unit, story, StoryUnitLink(story_id=story.id, unit_id=unit.id)])
        await db_session.commit()

        client = TestClient(app_with_db)
        story_page = client.get(f"/stories/{story.id}")
        assert story_page.status_code == 200
        link = re.search(r'href="(/proof/[0-9a-f-]{36})"', story_page.text)
        assert link is not None, "the public story page offers no inclusion-proof link"
        assert link.group(1) == f"/proof/{article.id}"
        # And the link it advertises actually resolves.
        assert client.get(link.group(1)).status_code == 200

    async def test_public_story_page_omits_the_link_for_an_unstamped_article(
        self, app_with_db, db_session
    ):
        article = _make_article(db_session, log_index=None)
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime(2026, 10, 1, tzinfo=timezone.utc),
            representative_article_id=article.id,
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={"AP": 1},
            tier1_owner_groups={"AP": 1},
        )
        story = Story(
            id=uuid.uuid4(),
            day=datetime(2026, 10, 1, tzinfo=timezone.utc),
            primary_entities=["test-entity"],
            status=Story.Status.QUEUED,
            tier1_unit_count=1,
            tier2_unit_count=0,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=1,
        )
        db_session.add_all([unit, story, StoryUnitLink(story_id=story.id, unit_id=unit.id)])
        await db_session.commit()

        body = TestClient(app_with_db).get(f"/stories/{story.id}").text
        # No proof exists for this article, so no dead-end link is offered.
        assert "/proof/" not in body


class TestCheckpointStore:
    async def test_latest_checkpoint_covering_picks_newest(self, db_session):
        log = SqlAlchemyMerkleLog(db_session)
        for i in range(4):
            await log.append({"n": i})
        await _checkpoint_all(db_session, log, key_id="k1")
        await log.append({"n": 4})
        await _checkpoint_all(db_session, log, key_id="k2")
        await db_session.commit()

        covering = await latest_checkpoint_covering(db_session, 1)
        assert covering is not None
        assert covering.key_id == "k2"  # newest checkpoint covering index 1
        assert covering.checkpoint.tree_size == 5

        covering_old = await latest_checkpoint_covering(db_session, 3)
        assert covering_old.key_id == "k2"

        assert await latest_checkpoint_covering(db_session, 9) is None

    async def test_stamp_writeback_links_article_to_entry(self, db_session):
        """stamp_observations must set article.log_index (the permalink's join)."""
        from src.ingestion.rss_evidence import stamp_observations
        from src.transparency.log import InMemoryMerkleLog

        article = _make_article(db_session)
        await db_session.flush()
        assert article.log_index is None

        mem_log = InMemoryMerkleLog()
        result = await stamp_observations(db_session, [article], merkle_log=mem_log)
        assert result["stamped"] == 1
        assert article.log_index == 0

        rows = await db_session.execute(select(RawArticle.log_index).where(RawArticle.id == article.id))
        assert rows.scalar_one() == 0
