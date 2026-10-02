"""Tests for the curation workbench discovery controls.

The queue's tier filter, date window, sort, topic search, and the corroboration
badge on each triage card. Search runs the ILIKE fallback here (SQLite has no
pg_trgm); the trigram path is exercised live against dev Postgres.

The fixtures are PENDING, not QUEUED like the public map's: this is the triage
queue, which serves Story.Status.PENDING and nothing else, so a queued fixture
would make every assertion here silently degrade to "no results".
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from curation_ui.curation import _filter_query
from src.schema.models import (
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.shared import database as database_module
from src.shared.config import get_settings

AUTH = ("testuser", "testpass")


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
def app_with_db(test_settings, db_engine):
    """App backed by the shared in-memory test database."""
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    # SQLite never has pg_trgm, and the probe caches per process.
    import curation_ui.discovery as discovery_module
    discovery_module._trigram_available_cache = False

    return app


def _make_pending_story(
    db_session,
    *,
    headline,
    tier_counts=(0, 0, 0, 0),
    owners=1,
    created_at=None,
    domain="example.com",
):
    """A PENDING triage-queue story with an explicit tier mix and headline."""
    created_at = created_at or datetime.now(timezone.utc)
    t1, t2, t3, t4 = tier_counts
    story = Story(
        id=uuid.uuid4(),
        day=created_at.replace(hour=0, minute=0, second=0, microsecond=0),
        created_at=created_at,
        status=Story.Status.PENDING,
        primary_entities=["test-entity"],
        tier1_unit_count=t1,
        tier2_unit_count=t2,
        tier3_unit_count=t3,
        tier4_unit_count=t4,
        distinct_owners=owners,
    )
    db_session.add(story)
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=story.day,
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={},
        owner_groups={},
        tier1_owner_groups={},
    )
    db_session.add(unit)
    article = RawArticle(
        id=unit.representative_article_id,
        url=f"https://{domain}/{unit.id}",
        url_hash=str(unit.id).replace("-", ""),
        title=headline,
        source_domain=domain,
        source_tier=SourceTier.TIER1,
        reporting_unit_id=unit.id,
    )
    db_session.add(article)
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    return story


def _card_ids(html):
    """Story ids in rendered order."""
    return re.findall(r'data-story-id="([0-9a-f-]+)"', html)


def _cards(html):
    """One entry per rendered card, in order."""
    return re.findall(r'<article class="story-card".*?</article>', html, re.S)


class TestToolbar:
    @pytest.mark.asyncio
    async def test_toolbar_renders_with_query_state(self, app_with_db, db_session):
        """The queue ships search, tier chips, window and sort, server-rendered
        in their initial state from the query string."""
        await db_session.commit()
        client = TestClient(app_with_db)

        response = client.get("/?tiers=1,2&sort=top&hours=168&q=earthquake", auth=AUTH)
        assert response.status_code == 200
        html = response.text
        assert 'id="discovery-q"' in html
        assert 'value="earthquake"' in html
        assert 'id="discovery-hours"' in html
        assert 'id="discovery-sort"' in html
        assert 'value="1,2"' in html
        chips = re.findall(r'class="tier-chip[^"]*"[^>]*aria-pressed="(\w+)"', html)
        assert chips == ["true", "true", "false", "false"]
        # Window and sort reflect the request, not the defaults.
        assert re.search(r'value="168"[^>]*selected', html)
        assert re.search(r'value="top"[^>]*selected', html)

    @pytest.mark.asyncio
    async def test_toolbar_survives_outside_the_swapped_fragment(self, app_with_db, db_session):
        """The toolbar must sit outside #stories, or an approve/reject swap
        destroys the controls the curator is holding."""
        _make_pending_story(db_session, headline="Placement of the toolbar",
                            tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()
        html = TestClient(app_with_db).get("/", auth=AUTH).text
        toolbar = html.index('id="discovery-bar"')
        grid = html.index('id="stories"')
        assert toolbar < grid

    @pytest.mark.asyncio
    async def test_bad_tiers_rejected(self, app_with_db, db_session):
        response = TestClient(app_with_db).get("/?tiers=bogus", auth=AUTH)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_sort_falls_back_to_newest(self, app_with_db, db_session):
        """A mistyped sort must still show the queue, not a 500 in front of a
        curator mid-triage."""
        response = TestClient(app_with_db).get("/?sort=chaos", auth=AUTH)
        assert response.status_code == 200
        assert re.search(r'value="newest"[^>]*selected', response.text)


class TestTierFilter:
    @pytest.mark.asyncio
    async def test_tier_filter_drops_tier3_only_story(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        kept = _make_pending_story(db_session, headline="Summit talks resume in Geneva",
                                   tier_counts=(2, 0, 0, 0), owners=2, created_at=now)
        dropped = _make_pending_story(db_session, headline="Viral rumor spreads on forums",
                                      tier_counts=(0, 0, 3, 0), owners=3, created_at=now)
        await db_session.commit()
        client = TestClient(app_with_db)

        unfiltered = _card_ids(client.get("/", auth=AUTH).text)
        assert set(unfiltered) == {str(kept.id), str(dropped.id)}

        filtered = _card_ids(client.get("/?tiers=1,2", auth=AUTH).text)
        assert filtered == [str(kept.id)]

    @pytest.mark.asyncio
    async def test_tier_filter_keeps_mixed_story(self, app_with_db, db_session):
        """A tier-1 story that also has tier-3 units survives tiers=1,2."""
        mixed = _make_pending_story(db_session, headline="Mixed coverage of the summit",
                                    tier_counts=(2, 0, 5, 0), owners=4)
        await db_session.commit()

        filtered = _card_ids(TestClient(app_with_db).get("/?tiers=1,2", auth=AUTH).text)
        assert filtered == [str(mixed.id)]

    @pytest.mark.asyncio
    async def test_all_tiers_selected_is_the_unfiltered_queue(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_pending_story(db_session, headline="Only tier three here",
                            tier_counts=(0, 0, 2, 0), owners=2, created_at=now)
        await db_session.commit()

        html = TestClient(app_with_db).get("/?tiers=1,2,3,4", auth=AUTH).text
        assert len(_card_ids(html)) == 1


class TestWindowFilter:
    @pytest.mark.asyncio
    async def test_window_narrows_the_queue(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        fresh = _make_pending_story(db_session, headline="Fresh overnight report",
                                    tier_counts=(2, 0, 0, 0), owners=2,
                                    created_at=now - timedelta(hours=2))
        stale = _make_pending_story(db_session, headline="Older harbor bridge report",
                                    tier_counts=(2, 0, 0, 0), owners=2,
                                    created_at=now - timedelta(days=4))
        await db_session.commit()
        client = TestClient(app_with_db)

        assert len(_card_ids(client.get("/?hours=0", auth=AUTH).text)) == 2
        assert _card_ids(client.get("/?hours=24", auth=AUTH).text) == [str(fresh.id)]
        # Four days old is outside 24 hours but inside 7 days.
        assert set(_card_ids(client.get("/?hours=168", auth=AUTH).text)) == {
            str(fresh.id), str(stale.id)
        }


class TestSort:
    @pytest.mark.asyncio
    async def test_newest_and_oldest_flip_the_order(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        older = _make_pending_story(db_session, headline="Older story about the bridge",
                                    tier_counts=(2, 0, 0, 0), owners=2,
                                    created_at=now - timedelta(days=3))
        newer = _make_pending_story(db_session, headline="Newer story about the summit",
                                    tier_counts=(2, 0, 0, 0), owners=2,
                                    created_at=now - timedelta(hours=1))
        await db_session.commit()
        client = TestClient(app_with_db)

        assert _card_ids(client.get("/?sort=newest", auth=AUTH).text) == [
            str(newer.id), str(older.id)
        ]
        assert _card_ids(client.get("/?sort=oldest", auth=AUTH).text) == [
            str(older.id), str(newer.id)
        ]

    @pytest.mark.asyncio
    async def test_top_ranks_by_corroboration(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        # Newer but uncorroborated, so recency and corroboration disagree.
        weak = _make_pending_story(db_session, headline="Thin coverage, newest of the two",
                                   tier_counts=(0, 0, 1, 0), owners=1,
                                   created_at=now - timedelta(minutes=5))
        strong = _make_pending_story(db_session, headline="Well sourced but older",
                                     tier_counts=(4, 2, 0, 0), owners=5,
                                     created_at=now - timedelta(days=2))
        await db_session.commit()
        client = TestClient(app_with_db)

        assert _card_ids(client.get("/?sort=newest", auth=AUTH).text) == [
            str(weak.id), str(strong.id)
        ]
        assert _card_ids(client.get("/?sort=top", auth=AUTH).text) == [
            str(strong.id), str(weak.id)
        ]


class TestTopicSearch:
    @pytest.mark.asyncio
    async def test_search_returns_matching_stories(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        hit = _make_pending_story(db_session, headline="Earthquake relief efforts in the valley",
                                  tier_counts=(2, 0, 0, 0), owners=2, created_at=now)
        miss = _make_pending_story(db_session, headline="Harbor bridge reopens to traffic",
                                   tier_counts=(2, 0, 0, 0), owners=2, created_at=now)
        await db_session.commit()
        client = TestClient(app_with_db)

        assert _card_ids(client.get("/?q=earthquake", auth=AUTH).text) == [str(hit.id)]
        assert _card_ids(client.get("/?q=zzzznothing", auth=AUTH).text) == []
        assert len(_card_ids(client.get("/", auth=AUTH).text)) == 2
        assert miss.id  # the miss is a real story, it simply does not match

    @pytest.mark.asyncio
    async def test_search_keeps_the_chosen_sort_order(self, app_with_db, db_session):
        """Search narrows the queue; the sort control still decides the order."""
        now = datetime.now(timezone.utc)
        older = _make_pending_story(db_session, headline="Earthquake in the northern valley",
                                    tier_counts=(2, 0, 0, 0), owners=2,
                                    created_at=now - timedelta(days=2))
        newer = _make_pending_story(db_session, headline="Earthquake aftershocks continue",
                                    tier_counts=(2, 0, 0, 0), owners=2,
                                    created_at=now - timedelta(hours=2))
        await db_session.commit()
        client = TestClient(app_with_db)

        assert _card_ids(client.get("/?q=earthquake&sort=newest", auth=AUTH).text) == [
            str(newer.id), str(older.id)
        ]
        assert _card_ids(client.get("/?q=earthquake&sort=oldest", auth=AUTH).text) == [
            str(older.id), str(newer.id)
        ]


class TestVerificationBadge:
    @pytest.mark.asyncio
    async def test_card_states_corroboration(self, app_with_db, db_session):
        """The card says in words whether the story is corroborated, and by how many."""
        now = datetime.now(timezone.utc)
        corroborated = _make_pending_story(db_session, headline="Corroborated summit report",
                                           tier_counts=(3, 1, 0, 0), owners=4, created_at=now)
        singleton = _make_pending_story(db_session, headline="Unconfirmed forum sighting",
                                        tier_counts=(0, 0, 1, 0), owners=1, created_at=now)
        await db_session.commit()

        html = TestClient(app_with_db).get("/", auth=AUTH).text
        cards = _cards(html)
        assert len(cards) == 2

        good_card = next(c for c in cards if str(corroborated.id) in c)
        assert "story-corroboration is-corroborated" in good_card
        assert "Corroborated" in good_card
        # Named tiers with counts, not a tooltip string of bare digits.
        assert "Tier 1" in good_card
        assert "3 outlets" in good_card
        assert "Tier 2" in good_card
        assert "1 outlet" in good_card
        assert "T1:3" not in good_card

        thin_card = next(c for c in cards if str(singleton.id) in c)
        assert "is-corroborated" not in thin_card
        assert "story-corroboration" in thin_card
        assert "unconfirmed" in thin_card.lower()

    @pytest.mark.asyncio
    async def test_card_reads_top_down_headline_first(self, app_with_db, db_session):
        """Dominance is a placement requirement, not only a colour one: the headline
        comes first, then the tier chips that say what makes it trustworthy, then
        the source list, then the small print."""
        _make_pending_story(db_session, headline="Placement check",
                            tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        html = TestClient(app_with_db).get("/", auth=AUTH).text
        card = _cards(html)[0]
        assert (card.index("story-headline")
                < card.index("story-tiers")
                < card.index("story-corroboration")
                < card.index("story-sources")
                < card.index("story-card-footer"))


class TestFiltersSurviveNavigation:
    """The card carries the active filters onto the detail view, and the detail
    view carries them back, so a filtered queue survives the round trip. The
    mutations that used to be tested here are gone with the approve and reject
    flows."""

    @pytest.mark.asyncio
    async def test_card_detail_link_carries_the_active_filters(self, app_with_db, db_session):
        story = _make_pending_story(db_session, headline="Harbor bridge reopens to traffic",
                                    tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        html = TestClient(app_with_db).get("/?tiers=1,2&sort=oldest&hours=168&q=bridge",
                                           auth=AUTH).text
        # The ampersands are HTML-escaped in the attribute, which the browser
        # unescapes before it issues the request.
        expected = "?tiers=1%2C2&amp;sort=oldest&amp;hours=168&amp;q=bridge"
        assert f'href="/story/{story.id}{expected}"' in html

    @pytest.mark.asyncio
    async def test_unfiltered_cards_carry_no_query_string(self, app_with_db, db_session):
        story = _make_pending_story(db_session, headline="No filters here",
                                    tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        html = TestClient(app_with_db).get("/", auth=AUTH).text
        assert f'href="/story/{story.id}"' in html

    @pytest.mark.asyncio
    async def test_detail_view_links_back_into_the_filters(self, app_with_db, db_session):
        story = _make_pending_story(db_session, headline="Keep the queue filtered",
                                    tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        html = TestClient(app_with_db).get(
            f"/story/{story.id}?tiers=1%2C2&sort=oldest", auth=AUTH
        ).text
        back = "?tiers=1%2C2&amp;sort=oldest"
        assert f'href="/{back}"' in html

    @pytest.mark.asyncio
    async def test_detail_view_of_a_hidden_story_is_still_reachable_directly(self, app_with_db, db_session):
        """Direct navigation is not filtered; only the queue listing is. A curator
        following a link to a story must not get a 404 because a chip is off."""
        now = datetime.now(timezone.utc)
        target = _make_pending_story(db_session, headline="Tier three only",
                                     tier_counts=(0, 0, 3, 0), owners=3, created_at=now)
        await db_session.commit()

        client = TestClient(app_with_db)
        assert _card_ids(client.get("/?tiers=1,2", auth=AUTH).text) == []
        response = client.get(f"/story/{target.id}?tiers=1%2C2", auth=AUTH)
        assert response.status_code == 200
        assert "Tier three only" in response.text


class TestEmptyState:
    @pytest.mark.asyncio
    async def test_empty_queue_says_the_queue_is_empty(self, app_with_db, db_session):
        """No stories at all reads differently from no stories matching a filter."""
        await db_session.commit()
        client = TestClient(app_with_db)

        unfiltered = client.get("/", auth=AUTH).text
        assert "No stories are waiting." in unfiltered
        assert "Run the ingestion pipeline" in unfiltered
        assert "match these filters" not in unfiltered

    @pytest.mark.asyncio
    async def test_filtered_empty_queue_says_the_filters_are(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        _make_pending_story(db_session, headline="Only tier three here",
                            tier_counts=(0, 0, 2, 0), owners=2, created_at=now)
        await db_session.commit()

        html = TestClient(app_with_db).get("/?tiers=1,2", auth=AUTH).text
        assert "match these filters" in html
        assert "Run the ingestion pipeline" not in html


class TestFilterQueryHelper:
    def test_defaults_are_omitted(self):
        assert _filter_query() == ""

    def test_non_defaults_are_carried_and_escaped(self):
        query = _filter_query(tiers=[2, 1], sort="top", hours=168, q="earthquake quake")
        assert query.startswith("?")
        assert "tiers=1%2C2" in query
        assert "sort=top" in query
        assert "hours=168" in query
        assert "q=earthquake+quake" in query

    def test_all_four_tiers_is_no_filter(self):
        assert _filter_query(tiers=[4, 3, 2, 1]) == ""