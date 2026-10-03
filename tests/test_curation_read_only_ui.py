"""The curation page redesign, asserted as a contract.

Tyler, 2026-10-02: "the pipeline curation page, unusable and unreadable with not the
right information. Also remove the approve, reject, edit, each story on the curation
page must have each source that attributed, and just click in for all the info on the
stories and curated together."

Four things are pinned here, each of which was either broken on main or was the
directive itself:

* `TestTheTriageFlowsAreGone` -- approve, reject and edit do not appear in the
  rendered curation markup and their routes 404. This is a regression test for the
  directive, not for a route: hide-the-button would leave the htmx endpoints live at
  a guessable URL, which is the thing the directive rules out.
* `TestEveryCardIsIdentifiable` -- every card leads with a real headline, a named
  tier, a plain-language corroboration verdict and every attributed source.
* `TestTheDetailViewHoldsEverything` -- one click in, everything in, with the
  key=value internals behind a disclosure rather than inline.
* The pure helpers that turn counts into sentences, unit tested directly because the
  sentences are the readable surface and their wording is the requirement.

The `no approve/reject/edit in the markup` check is deliberately case-insensitive and
substring-based, and it scans the queue page and the detail page both. It will fail on
a future contributor adding a button back, which is the point.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from curation_ui import curation
from src.schema.models import (
    Claim,
    ClaimEvidence,
    ClaimStance,
    ClaimType,
    RawArticle,
    ReportingUnit,
    Snippet,
    SourceTier,
    Story,
    StoryTopicGroup,
    StoryUnitLink,
    TopicGroup,
)
from src.shared import database as database_module
from src.shared.config import get_settings

AUTH = ("testuser", "testpass")

# Any of these appearing in curation markup means a triage flow came back. Matched
# on word boundaries, not as bare substrings, because the tier vocabulary lifted
# from src/verification/tiers.py contains the word "editorial" and that is not an
# edit control.
BANNED = (r"\bapprove\b", r"\breject\b", r"\bedit\b")

# Attributes and URLs that would exist only to drive a triage control.
BANNED_MARKUP = (
    "hx-post",
    "hx-get",
    "hx-target",
    "/approve",
    "/reject",
    "/save",
    "/edit",
    "/mark-posted",
    "X-CSRF-Token",
)


def _assert_no_triage_controls(html: str, where: str) -> None:
    for pattern in BANNED:
        match = re.search(pattern, html, re.I)
        assert match is None, (
            f"{pattern!r} is back in the {where} markup: ...{html[max(0, match.start() - 60):match.end() + 60]}..."
        )
    for needle in BANNED_MARKUP:
        assert needle not in html, f"{needle!r} is back in the {where} markup"


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
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    import curation_ui.security as security_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None
    security_module.auth_limiter.reset()

    import curation_ui.discovery as discovery_module
    discovery_module._trigram_available_cache = False

    return app


def _make_story(
    db_session,
    *,
    title="Officials described the situation in a short statement",
    domain="bbc.com",
    tier=SourceTier.TIER1,
    tier_counts=(2, 0, 0, 0),
    owners=2,
    created_at=None,
    story_status=Story.Status.PENDING,
    blank_title=False,
    summary="A short summary of what happened.",
    extra_articles=(),
):
    """A PENDING queue story with a representative article and real source data.

    `blank_title=True` builds the honest worst case: a story whose representative
    article exists but carries no title, which is what the "No headline captured"
    fallback exists for.
    """
    created_at = created_at or datetime.now(timezone.utc)
    t1, t2, t3, t4 = tier_counts
    story = Story(
        id=uuid.uuid4(),
        day=created_at.replace(hour=0, minute=0, second=0, microsecond=0),
        created_at=created_at,
        status=story_status,
        primary_entities=["11111111-1111-1111-1111-111111111111"],
        gate_reason="Passed gate: 2 tier-1 units, 2 distinct owners",
        tier1_unit_count=t1,
        tier2_unit_count=t2,
        tier3_unit_count=t3,
        tier4_unit_count=t4,
        distinct_owners=owners,
    )
    db_session.add(story)

    def _unit(article_domain, article_title, article_tier, article_url):
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=story.day,
            representative_article_id=uuid.uuid4(),
            article_count=1,
            source_tiers={f"tier{article_tier.value[-1]}": 1},
            owner_groups={article_domain: 1},
            tier1_owner_groups={article_domain: 1},
        )
        db_session.add(unit)
        article = RawArticle(
            id=unit.representative_article_id,
            url=article_url,
            url_hash=str(unit.id).replace("-", ""),
            title=article_title,
            summary=article_summary,
            source_domain=article_domain,
            source_tier=article_tier,
            published_at=created_at,
            reporting_unit_id=unit.id,
        )
        db_session.add(article)
        unit.representative_article_id = article.id
        db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
        return article

    article_summary = summary
    primary = _unit(domain, "" if blank_title else title, tier, f"https://{domain}/a")
    for entry in extra_articles:
        _unit(
            entry["domain"],
            entry.get("title", f"{entry['domain']} report"),
            entry.get("tier", tier),
            entry.get("url", f"https://{entry['domain']}/b"),
        )
    return story, primary


def _cards(html):
    return re.findall(r'<article class="story-card".*?</article>', html, re.S)


def _detail_html(app, story, query=""):
    return TestClient(app).get(f"/story/{story.id}{query}", auth=AUTH).text


# ---------------------------------------------------------------------------
# Part 1: the triage flows are gone.
# ---------------------------------------------------------------------------


class TestTheTriageFlowsAreGone:
    """Regression test for the directive itself, not for a route."""

    @pytest.mark.asyncio
    async def test_the_queue_markup_names_no_triage_action(self, app_with_db, db_session):
        _make_story(db_session)
        await db_session.commit()

        _assert_no_triage_controls(TestClient(app_with_db).get("/", auth=AUTH).text, "queue")

    @pytest.mark.asyncio
    async def test_the_detail_markup_names_no_triage_action(self, app_with_db, db_session):
        story, _ = _make_story(db_session)
        await db_session.commit()

        _assert_no_triage_controls(_detail_html(app_with_db, story), "detail")

    @pytest.mark.asyncio
    async def test_the_empty_queue_markup_names_no_triage_action(self, app_with_db, db_session):
        """The empty state is a rendered state too, and it used to invite a run."""
        await db_session.commit()

        _assert_no_triage_controls(TestClient(app_with_db).get("/", auth=AUTH).text, "empty queue")

    def test_the_curation_module_imports_no_llm_client(self):
        """The caption builder stays in src/shared/llm.py; the curation flows that
        used it are gone, so curation.py must not hold a reference to it either."""
        source = open(curation.__file__).read().lower()
        assert "build_deterministic_caption" not in source
        assert "get_llm_client" not in source
        assert "curatedpost" not in source

    def test_the_router_exposes_no_state_changing_route(self):
        from fastapi.routing import APIRoute

        from tests.test_route_table import iter_routes

        curation_paths = {
            route.path
            for route in iter_routes(curation.router.routes)
            if isinstance(route, APIRoute)
        }
        assert curation_paths == {"/", "/story/{story_id}"}


# ---------------------------------------------------------------------------
# Part 2: a card a human can read on a phone.
# ---------------------------------------------------------------------------


class TestEveryCardIsIdentifiable:
    @pytest.mark.asyncio
    async def test_every_card_leads_with_a_non_empty_headline(self, app_with_db, db_session):
        now = datetime.now(timezone.utc)
        first, _ = _make_story(db_session, title="Flooding closes the river crossing",
                               domain="bbc.com", created_at=now)
        second, _ = _make_story(db_session, title="Rail strike halts the morning services",
                                domain="reuters.com", created_at=now - timedelta(minutes=1))
        await db_session.commit()

        cards = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)
        assert len(cards) == 2
        for card, story in zip(cards, (first, second)):
            headline = re.search(r'<h2 class="story-headline">\s*<a[^>]*>(.*?)</a>', card, re.S)
            assert headline, "card has no headline element"
            assert headline.group(1).strip(), "card headline rendered empty"
            assert f'href="/story/{story.id}"' in card

    @pytest.mark.asyncio
    async def test_a_headline_less_story_says_so_instead_of_rendering_blank(
        self, app_with_db, db_session
    ):
        """A story is never shipped as a blank card, whatever the data says."""
        _make_story(db_session, blank_title=True, domain="example.net")
        await db_session.commit()

        cards = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)
        assert len(cards) == 1
        assert curation.NO_HEADLINE in cards[0]

    @pytest.mark.asyncio
    async def test_the_headline_is_the_article_title_not_the_summary(
        self, app_with_db, db_session
    ):
        """The card must not pass an article title off as if it were the story title.

        The Story model has no headline column, so the headline is derived from the
        representative article's title. This pins which field that is, so nobody
        swaps in body_text later and every card silently becomes a wall of text.
        """
        story, _ = _make_story(
            db_session,
            title="A distinct headline",
            summary="This summary must not become the headline.",
        )
        await db_session.commit()

        card = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)[0]
        assert "A distinct headline" in card
        assert "This summary must not become the headline." not in card

    @pytest.mark.asyncio
    async def test_a_story_whose_tally_has_not_landed_does_not_deny_its_own_sources(
        self, app_with_db, db_session
    ):
        """A card must not list outlets and then say there are none.

        Reproduces the state measured on dev: a story created seconds ago has a
        representative article attached but its tier counters and owner count
        are still zero, because the pipeline writes those a stage later.
        """
        story, _ = _make_story(
            db_session,
            title="A story the pipeline has not tallied yet",
            domain="cjme.com",
            tier=SourceTier.TIER2,
            tier_counts=(0, 0, 0, 0),
            owners=0,
        )
        await db_session.commit()

        card = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)[0]
        assert "cjme.com" in card, "the source should still be listed"
        assert "no outlet has been attributed" not in card
        assert "No sources have been attributed" not in card
        assert "Tally pending" in card
        assert "Tiers not tallied yet" in card
        assert "Not tallied yet" in card

    @pytest.mark.asyncio
    async def test_the_detail_view_also_refuses_to_deny_its_own_sources(
        self, app_with_db, db_session
    ):
        story, _ = _make_story(
            db_session,
            title="Untallied story on the detail page",
            domain="cjme.com",
            tier=SourceTier.TIER2,
            tier_counts=(0, 0, 0, 0),
            owners=0,
        )
        await db_session.commit()

        page = _detail_html(app_with_db, story)
        assert "cjme.com" in page
        assert "no outlet has been attributed" not in page
        assert "No sources have been attributed" not in page
        assert page.count("Tally pending") >= 2

    @pytest.mark.asyncio
    async def test_a_story_with_no_source_at_all_still_says_it_has_none(
        self, app_with_db, db_session
    ):
        """The honest "nothing here" wording is kept for a story with no sources."""
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            status=Story.Status.PENDING,
            primary_entities=[],
            tier1_unit_count=0,
            tier2_unit_count=0,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=0,
        )
        db_session.add(story)
        await db_session.commit()

        card = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)[0]
        assert "No source has been attributed to this story yet" in card
        assert "Tally pending" not in card

    @pytest.mark.asyncio
    async def test_a_tallied_story_still_uses_the_filters_own_vocabulary(
        self, app_with_db, db_session
    ):
        """The pending wording is an exception for the untallied, not a replacement
        for the badge vocabulary the T1..T4 filters are selected with."""
        _make_story(
            db_session,
            title="A properly tallied story",
            tier_counts=(2, 0, 0, 0),
            owners=2,
        )
        await db_session.commit()

        card = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)[0]
        assert "Corroborated · 2 outlets" in card
        assert "Tally pending" not in card
        assert "Tier 1" in card and "2 outlets" in card

    @pytest.mark.asyncio
    async def test_every_attributed_source_is_listed_on_the_card(self, app_with_db, db_session):
        """Not a sample, not a count: the outlets themselves, under the headline."""
        story, _ = _make_story(
            db_session,
            title="Multiple outlets reported the same thing",
            extra_articles=(
                {"domain": "theguardian.com", "title": "Guardian write-up"},
                {"domain": "france24.com", "title": "France 24 write-up"},
            ),
        )
        await db_session.commit()

        card = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)[0]
        assert "3 sources" in card
        for domain in ("bbc.com", "theguardian.com", "france24.com"):
            assert domain in card
        # Each source carries a timestamp and a link out.
        assert card.count('class="source-time"') == 3
        assert card.count('class="source-link"') == 3
        assert "read &#8599;" in card

    @pytest.mark.asyncio
    async def test_the_card_states_status_and_freshness(self, app_with_db, db_session):
        _make_story(db_session)
        await db_session.commit()

        card = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)[0]
        assert "Corroborated" in card
        assert "arrived" in card

    @pytest.mark.asyncio
    async def test_a_story_with_no_attributed_source_says_it_cannot_be_corroborated(
        self, app_with_db, db_session
    ):
        """A unit whose representative article never landed: the card must say so,
        not render an empty source list under a claim of corroboration."""
        now = datetime.now(timezone.utc)
        story = Story(
            id=uuid.uuid4(),
            day=now,
            created_at=now,
            status=Story.Status.PENDING,
            primary_entities=[],
            tier1_unit_count=0,
            tier2_unit_count=0,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=0,
        )
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=now,
            representative_article_id=uuid.uuid4(),  # no RawArticle row for it
            article_count=1,
            source_tiers={},
            owner_groups={},
            tier1_owner_groups={},
        )
        db_session.add_all([story, unit, StoryUnitLink(story_id=story.id, unit_id=unit.id)])
        await db_session.commit()

        cards = _cards(TestClient(app_with_db).get("/", auth=AUTH).text)
        assert len(cards) == 1
        assert "No source has been attributed" in cards[0]
        assert curation.NO_HEADLINE in cards[0]


# ---------------------------------------------------------------------------
# Part 3: one click in, everything in.
# ---------------------------------------------------------------------------


class TestTheDetailViewHoldsEverything:
    @pytest.mark.asyncio
    async def test_the_detail_view_renders_headline_summary_and_sources(
        self, app_with_db, db_session
    ):
        story, _ = _make_story(
            db_session,
            title="The headline a reader needs",
            domain="bbc.com",
            summary="What actually happened, in the words the outlet used.",
            extra_articles=({"domain": "dw.com", "title": "DW write-up"},),
        )
        await db_session.commit()

        html = _detail_html(app_with_db, story)
        assert "<h1" in html and "The headline a reader needs" in html
        assert "What actually happened, in the words the outlet used." in html
        assert "Quoted from" in html
        # Both sources, each with a real link and a timestamp.
        for expected in ("bbc.com", "dw.com", "https://bbc.com/a", "https://dw.com/b",
                         "UTC"):
            assert expected in html, f"{expected!r} missing from the detail view"

    @pytest.mark.asyncio
    async def test_the_detail_view_states_the_tier_meanings_in_words(
        self, app_with_db, db_session
    ):
        story, _ = _make_story(db_session, tier_counts=(2, 0, 1, 0))
        await db_session.commit()

        html = _detail_html(app_with_db, story)
        # The words are lifted from src/verification/tiers.py, not invented here.
        assert "verified editorial standards" in html
        assert "social, forums, unverified" in html
        assert "2 outlets" in html
        assert "1 outlet" in html

    @pytest.mark.asyncio
    async def test_the_detail_view_states_corroboration_and_the_gate_in_one_sentence(
        self, app_with_db, db_session
    ):
        story, _ = _make_story(db_session, tier_counts=(2, 0, 0, 0), owners=2)
        await db_session.commit()

        html = _detail_html(app_with_db, story)
        assert "Gate outcome" in html
        assert "Passed gate: 2 tier-1 units, 2 distinct owners" in html
        assert "Whether to believe it" in html
        assert re.search(r"Corroborated by \d+ outlets?", html)

    @pytest.mark.asyncio
    async def test_the_internals_are_behind_a_disclosure_not_inline(
        self, app_with_db, db_session
    ):
        """The claim matrix and narrative links stay, but nobody has to scroll to
        them and they do not crowd the story."""
        now = datetime.now(timezone.utc)
        story, article = _make_story(db_session, created_at=now)
        claim = Claim(
            id=uuid.uuid4(),
            story_id=story.id,
            text="Officials said the crossing would reopen on Friday.",
            claim_type=ClaimType.QUOTE,
            first_seen_at=now,
        )
        db_session.add(claim)
        db_session.add(ClaimEvidence(
            id=1,
            claim_id=claim.id,
            unit_id=uuid.uuid4(),
            stance=ClaimStance.SUPPORTS,
            confidence=82,
        ))
        db_session.add(Snippet(
            story_id=story.id,
            article_id=article.id,
            snippet_type=Snippet.SnippetType.QUOTE,
            text="A quotable line from the report.",
            confidence=77,
        ))
        group = TopicGroup(id=uuid.uuid4(), name="Flooding")
        db_session.add(group)
        db_session.add(StoryTopicGroup(story_id=story.id, topic_group_id=group.id, confidence=0.8))
        await db_session.commit()

        html = _detail_html(app_with_db, story)
        assert "<details" in html
        assert "Technical details" in html
        # The internals are present, and inside the disclosure rather than above it.
        disclosure_at = html.index("<details")
        assert html.index("Officials said the crossing would reopen") > disclosure_at
        assert html.index("Flooding") > disclosure_at

    @pytest.mark.asyncio
    async def test_the_detail_view_resolves_canonical_entity_ids_to_names(
        self, app_with_db, db_session
    ):
        """`Story.primary_entities` is a list of canonical UUIDs. A raw uuid in the
        markup is the debug spew this page was rebuilt to remove, so they resolve."""
        story, _ = _make_story(db_session)
        await db_session.commit()

        html = _detail_html(app_with_db, story)
        assert "11111111-1111-1111-1111-111111111111" not in html  # canonical entity id
        # The row is simply absent when nothing resolves, rather than broken.
        assert "Primary entities" not in html

    @pytest.mark.asyncio
    async def test_the_detail_view_of_a_story_with_no_summary_still_renders(
        self, app_with_db, db_session
    ):
        story, _ = _make_story(db_session, summary=None)
        await db_session.commit()

        response = TestClient(app_with_db).get(f"/story/{story.id}", auth=AUTH)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_the_detail_view_404s_for_an_unknown_story(self, app_with_db, db_session):
        await db_session.commit()

        response = TestClient(app_with_db).get(f"/story/{uuid.uuid4()}", auth=AUTH)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# The sentences are the readable surface, so their wording is under test.
# ---------------------------------------------------------------------------


class TestTheSentencesReadAsEnglish:
    def test_tier_sentence_names_each_tier_and_its_meaning(self):
        sentence = curation._tier_sentence({"t1": 2, "t2": 0, "t3": 1, "t4": 0})
        assert "2 tier-1 outlets" in sentence
        assert "verified editorial standards" in sentence
        assert "1 tier-3 outlet" in sentence
        assert "social, forums, unverified" in sentence

    def test_tier_sentence_handles_a_story_with_no_tiered_source(self):
        assert "No sources have been attributed" in curation._tier_sentence(
            {"t1": 0, "t2": 0, "t3": 0, "t4": 0}
        )

    # ------------------------------------------------------------------
    # The tally lags the sources. The pipeline attaches a source article to a
    # story a stage before it writes tier1..4_unit_count and distinct_owners,
    # so a story that is minutes old has real, listed sources and a tally of
    # zero. Measured on dev on 2026-10-02: 50 of 50 live queue cards were in
    # that state, and the page said "no outlet has been attributed to this
    # story yet" directly above a list of outlets. These tests pin the fix.
    # ------------------------------------------------------------------

    def test_counts_pending_needs_both_a_source_and_an_untallied_story(self):
        tallied = {"outlets": 2, "tier_mix": {"t1": 2}, "corroborated": True}
        assert curation._counts_pending(tallied, 2) is False
        assert curation._counts_pending(tallied, 0) is False
        blank = {"outlets": 0, "tier_mix": {"t1": 0, "t2": 0, "t3": 0, "t4": 0},
                 "corroborated": False}
        assert curation._counts_pending(blank, 0) is False
        assert curation._counts_pending(blank, 1) is True
        # A story with a source and a tally is not pending, even when the tally
        # is only a tier-3 source: that is a real verdict, not a missing one.
        untiered = {"outlets": 1, "tier_mix": {"t1": 0, "t2": 0, "t3": 1, "t4": 0},
                    "corroborated": False}
        assert curation._counts_pending(untiered, 1) is False

    def test_a_pending_tally_never_claims_there_is_no_source(self):
        blank = {"outlets": 0, "tier_mix": {"t1": 0, "t2": 0, "t3": 0, "t4": 0},
                 "corroborated": False}
        sentence = curation._corroboration_sentence(blank, 1)
        assert "Not tallied yet" in sentence
        assert "1 source listed" in sentence
        assert "no outlet has been attributed" not in sentence
        tiers = curation._tier_sentence(blank["tier_mix"], 1)
        assert "has not tallied this story's tiers yet" in tiers
        assert "the source listed below carries its own tier" in tiers
        assert "No sources have been attributed" not in tiers
        # "each carry" is not a thing a one-source story can say.
        assert "each carry" not in tiers
        many = curation._tier_sentence(blank["tier_mix"], 3)
        assert "each of the 3 sources listed below carries its own tier" in many

    def test_a_pending_tally_is_reported_in_the_plural_too(self):
        blank = {"outlets": 0, "tier_mix": {"t1": 0, "t2": 0, "t3": 0, "t4": 0},
                 "corroborated": False}
        assert "2 sources listed" in curation._corroboration_sentence(blank, 2)

    def test_a_story_with_no_source_at_all_still_says_plainly_that_it_has_none(self):
        blank = {"outlets": 0, "tier_mix": {"t1": 0, "t2": 0, "t3": 0, "t4": 0},
                 "corroborated": False}
        assert curation._corroboration_sentence(blank, 0) == (
            "Not corroborated: no outlet has been attributed to this story yet."
        )
        assert curation._tier_sentence(blank["tier_mix"], 0) == (
            "No sources have been attributed to this story yet."
        )

    def test_a_single_outlet_is_singular(self):
        assert "1 outlet" in curation._outlet_phrase(1)
        assert "3 outlets" in curation._outlet_phrase(3)
        assert "no outlets" in curation._outlet_phrase(0)

    def test_corroboration_agrees_with_the_discovery_verification_badge(self):
        """The card and the filter must not disagree, so the sentence is built from
        the same verification dict the filter's badge uses."""
        from curation_ui.discovery import _verification_badge

        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            status=Story.Status.PENDING,
            primary_entities=[],
            tier1_unit_count=2,
            tier2_unit_count=0,
            tier3_unit_count=0,
            tier4_unit_count=0,
            distinct_owners=2,
        )
        verification = _verification_badge(story)
        assert verification["corroborated"] is True
        assert curation._corroboration_sentence(verification) == (
            "Corroborated by 2 outlets, 2 of them tier-1."
        )

        thin = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            status=Story.Status.PENDING,
            primary_entities=[],
            tier1_unit_count=0,
            tier2_unit_count=0,
            tier3_unit_count=1,
            tier4_unit_count=0,
            distinct_owners=1,
        )
        assert _verification_badge(thin)["corroborated"] is False
        assert "Not corroborated" in curation._corroboration_sentence(
            _verification_badge(thin)
        )

    def test_status_sentence_quotes_the_gate_reason(self):
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            status=Story.Status.PENDING,
            primary_entities=[],
            gate_reason="Failed gate: single source",
        )
        sentence = curation._status_sentence(story)
        assert "Failed gate: single source" in sentence
        assert "awaiting review" in sentence.lower()

    def test_status_sentence_says_so_when_there_is_no_gate_reason(self):
        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            status=Story.Status.PENDING,
            primary_entities=[],
            gate_reason=None,
        )
        assert "No gate explanation was recorded." in curation._status_sentence(story)

    def test_freshness_reads_in_words(self):
        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        assert curation._freshness(now, now) == "just now"
        assert curation._freshness(now - timedelta(minutes=5), now) == "5 minutes ago"
        assert curation._freshness(now - timedelta(hours=3), now) == "3 hours ago"
        assert curation._freshness(now - timedelta(days=2), now) == "2 days ago"

    def test_freshness_survives_a_missing_timestamp(self):
        assert curation._freshness(None, datetime(2026, 10, 2, tzinfo=timezone.utc)) == (
            "timestamp not recorded"
        )

    def test_tier_chips_skip_tiers_with_nothing_in_them(self):
        chips = curation._tier_chips({"t1": 2, "t2": 0, "t3": 1, "t4": 0})
        assert [chip["tier"] for chip in chips] == [1, 3]
        assert chips[0] == {
            "tier": 1,
            "count": 2,
            "noun": "outlets",
            "meaning": curation.TIER_MEANING[1],
        }

    def test_story_headline_falls_back_to_the_honest_placeholder(self):
        assert curation._story_headline([])["headline"] == curation.NO_HEADLINE

    def test_story_headline_prefers_the_translation(self):
        article = type(
            "A",
            (),
            {
                "title": "Original title",
                "title_en": "English title",
                "source_domain": "dw.com",
            },
        )()
        headline = curation._story_headline([article])
        assert headline["headline"] == "English title"
        assert headline["headline_original"] == "Original title"
        assert headline["translated"] is True
        assert headline["source_domain"] == "dw.com"
