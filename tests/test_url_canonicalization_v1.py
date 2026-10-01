"""Test vectors for scheme u1, the versioned URL canonical form.

Every vector here is a URL shape that shows up in the GDELT and RSS feeds this
pipeline reads, paired with the canonical form scheme u1 has to produce. The
rules the vectors encode are the numbered list in the docstring of
``src.utils.trafilatura_extract.canonicalize_url_v1``. If a rule changes, the
vectors change with it, and a change that is not deliberate shows up here.
"""

import hashlib

import pytest

from src.utils.trafilatura_extract import (
    canonicalize_url,
    canonicalize_url_v1,
    compute_url_hash,
)


# (raw input, expected u1 canonical form, why the vector is here)
CANONICAL_VECTORS = [
    # Baseline shape, nothing to fold.
    ("https://apnews.com/article/biden-ukraine-9f2c1a",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "already canonical"),
    ("https://apnews.com",
     "https://apnews.com/", "empty path becomes the root"),
    ("https://www.apnews.com/article/biden-ukraine-9f2c1a",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "www label stripped"),
    ("https://APNEWS.COM/article/biden-ukraine-9f2c1a",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "host lowercased"),
    ("HTTPS://APNEWS.COM/article/biden-ukraine-9f2c1a",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "scheme lowercased"),
    ("https://apnews.com/article/biden-ukraine-9f2c1a/",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "trailing slash dropped"),
    ("https://apnews.com/article/biden-ukraine-9f2c1a#lead",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "fragment dropped"),
    ("  https://apnews.com/article/biden-ukraine-9f2c1a  ",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "surrounding space trimmed"),
    ("https://apnews.com./article/biden-ukraine-9f2c1a",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "root dot dropped"),
    ("//apnews.com/article/biden-ukraine-9f2c1a",
     "https://apnews.com/article/biden-ukraine-9f2c1a", "protocol relative form"),

    # Tier one outlets as they actually appear in the feeds.
    ("http://www.reuters.com/world/europe/german-election-2024-05-01/",
     "https://reuters.com/world/europe/german-election-2024-05-01",
     "reuters with scheme, www, and slash"),
    ("https://www.reuters.com/world/europe/german-election-2024-05-01/?rpc=401&",
     "https://reuters.com/world/europe/german-election-2024-05-01?rpc=401",
     "rpc is content, empty tail dropped"),
    ("https://www.theguardian.com/world/2024/may/01/ukraine-war-live",
     "https://theguardian.com/world/2024/may/01/ukraine-war-live", "guardian"),
    ("https://m.theguardian.com/world/2024/may/01/ukraine-war-live",
     "https://theguardian.com/world/2024/may/01/ukraine-war-live", "mobile label"),
    ("https://www.bbc.co.uk/news/world-12345678",
     "https://bbc.co.uk/news/world-12345678", "co.uk keeps its second level"),
    ("https://m.bbc.co.uk/news/world-12345678",
     "https://bbc.co.uk/news/world-12345678", "mobile label on a co.uk host"),
    ("https://www.npr.org/2024/05/01/nx-s1-5089012/ukraine-standoff",
     "https://npr.org/2024/05/01/nx-s1-5089012/ukraine-standoff", "npr"),
    ("https://text.npr.org/nx-s1-5089012",
     "https://text.npr.org/nx-s1-5089012", "text label is not a mobile label"),
    ("https://www.nytimes.com/2024/05/01/world/europe/german-election.html",
     "https://nytimes.com/2024/05/01/world/europe/german-election.html", "nytimes"),
    ("https://www.nytimes.com/2024/05/01/world/europe/german-election.html?smid=url-share",
     "https://nytimes.com/2024/05/01/world/europe/german-election.html?smid=url-share",
     "smid is not in the tracking list, so it stays"),
    ("https://news.ycombinator.com/item?id=40551234",
     "https://news.ycombinator.com/item?id=40551234", "id is identity"),
    ("https://news.ycombinator.com/item?id=40551234&utm_source=hn",
     "https://news.ycombinator.com/item?id=40551234", "tracking dropped, id kept"),
    ("https://www.aljazeera.com/news/2024/5/1/german-election",
     "https://aljazeera.com/news/2024/5/1/german-election", "al jazeera"),
    ("https://www.aljazeera.com/amp/news/2024/5/1/german-election",
     "https://aljazeera.com/news/2024/5/1/german-election", "amp path segment"),
    ("https://aljazeera.com/news/2024/5/1/german-election?output=1",
     "https://aljazeera.com/news/2024/5/1/german-election", "output marker dropped"),
    ("https://edition.cnn.com/2024/05/01/politics/biden/story",
     "https://edition.cnn.com/2024/05/01/politics/biden/story",
     "edition is an editorial label, not www"),
    ("https://www.ft.com/content/9f0c1b2a-3d4e-5f60-7182-93a4b5c6d7e8",
     "https://ft.com/content/9f0c1b2a-3d4e-5f60-7182-93a4b5c6d7e8", "ft uuid path"),
    ("https://www.politico.com/news/2024/05/01/german-election-00123456",
     "https://politico.com/news/2024/05/01/german-election-00123456", "politico"),
    ("https://www.defense.gov/News/News-Stories/Article/Article/3812345/",
     "https://defense.gov/News/News-Stories/Article/Article/3812345",
     "path case is preserved for a government site"),
    ("https://www.whitehouse.gov/briefings-statements/2024/05/01/remarks-president/",
     "https://whitehouse.gov/briefings-statements/2024/05/01/remarks-president",
     "white house"),
    ("https://www.state.gov/briefings/2024/05/01/press-release-12345",
     "https://state.gov/briefings/2024/05/01/press-release-12345", "state dept"),
    ("https://www.gov.uk/government/news/council-agrees-12345",
     "https://gov.uk/government/news/council-agrees-12345", "gov uk"),
    ("https://www.gov.uk/government/news/council-agrees-12345?utm_source=twitter&utm_medium=social",
     "https://gov.uk/government/news/council-agrees-12345", "gov uk tracking"),
    ("https://www.who.int/news/item/01-05-2024-statement-on-the-outbreak",
     "https://who.int/news/item/01-05-2024-statement-on-the-outbreak",
     "dots inside a path are data"),
    ("https://www.amnesty.org/en/latest/news/2024/05/report-12345/",
     "https://amnesty.org/en/latest/news/2024/05/report-12345", "amnesty"),
    ("https://www.spiegel.de/politik/deutschland/artikel-a-123456.html",
     "https://spiegel.de/politik/deutschland/artikel-a-123456.html", "spiegel"),
    ("https://m.spiegel.de/politik/deutschland/artikel-a-123456.html",
     "https://spiegel.de/politik/deutschland/artikel-a-123456.html", "spiegel mobile"),
    ("https://amp.spiegel.de/politik/deutschland/artikel-a-123456.html",
     "https://spiegel.de/politik/deutschland/artikel-a-123456.html", "spiegel amp"),
    ("https://www.semafor.com/article/05/01/2024/german-election",
     "https://semafor.com/article/05/01/2024/german-election",
     "a date inside the path keeps its slashes"),
    ("https://www.axios.com/2024/05/01/german-election?utm_source=twitter",
     "https://axios.com/2024/05/01/german-election", "axios tracking"),
    ("https://www.vox.com/2024/5/1/1234567/german-election",
     "https://vox.com/2024/5/1/1234567/german-election", "vox"),
    ("https://www.timesofindia.indiatimes.com/city/delhi/story-1234567",
     "https://timesofindia.indiatimes.com/city/delhi/story-1234567", "toi"),

    # AMP shapes.
    ("https://theatlantic.com/technology/archive/2024/05/story-123456/amp/",
     "https://theatlantic.com/technology/archive/2024/05/story-123456", "trailing amp"),
    ("https://theatlantic.com/technology/archive/2024/05/story-123456/AMP",
     "https://theatlantic.com/technology/archive/2024/05/story-123456", "uppercase amp"),
    ("https://example.amp.org/technology/story-123456",
     "https://example.org/technology/story-123456", "amp label before the tld"),
    ("https://www.washingtonpost.com/technology/2024/05/01/story/?output=amp",
     "https://washingtonpost.com/technology/2024/05/01/story", "output amp"),
    ("https://www.washingtonpost.com/technology/2024/05/01/story?amp=1",
     "https://washingtonpost.com/technology/2024/05/01/story", "amp marker"),

    # Tracking parameters, the families the feeds actually carry.
    ("https://example.org/page?utm_source=n&fbclid=IwAR1&gclid=abc&id=42",
     "https://example.org/page?id=42", "utm and click ids, id kept"),
    ("https://example.org/page?gclid=abc&dclid=xyz&wbraid=1&gbraid=2&msclkid=3",
     "https://example.org/page", "google and microsoft click ids"),
    ("https://example.org/page?mc_cid=1&mc_eid=2&igshid=3&_ga=GA1.2&_gl=US",
     "https://example.org/page", "mailchimp and analytics"),
    ("https://example.org/page?yclid=1&twclid=2&ttclid=3&igshid=4",
     "https://example.org/page", "social click ids"),
    ("https://example.org/page?pk_campaign=news&pk_kwd=abc&pk_source=mail",
     "https://example.org/page", "mailchimp prefixes"),
    ("https://example.org/page?hsa_acc=xyz&hsa_cam=news&utm_medium=cpc",
     "https://example.org/page", "hubspot prefix"),
    ("https://example.org/page?vero_id=1&vero_conv=2&piwik_campaign=3&matomo_id=4",
     "https://example.org/page", "matomo and piwik"),
    ("https://example.org/page?ns_campaign=bi&ns_mchannel=email&ns_source=site",
     "https://example.org/page", "business insider prefix"),
    ("https://example.org/page?ref=twitter&taid=1&cmpid=2&ocid=3&oc=5",
     "https://example.org/page", "the original tracking list, plus oc"),
    ("https://example.org/page?referrer=newsletter&ref_src=tw&ref_url=https%3A%2F%2Fx.com",
     "https://example.org/page", "referrer family"),
    ("https://example.org/page?trk=pub&trkCampaign=news&mkt_tok=abc&originalSubdomain=www",
     "https://example.org/page", "linkedin and microsoft"),
    ("https://example.org/page?guccounter=1&guce_referrer=a&guce_referrer_sig=b",
     "https://example.org/page", "yahoo counter"),
    ("https://example.org/page?fb_action_ids=1&fb_action_types=2&fb_source=3&fb_ref=4",
     "https://example.org/page", "facebook action family"),
    ("https://example.org/page?AT_medium=x&UTM_SOURCE=y&Utm_Campaign=z",
     "https://example.org/page", "prefix match ignores case"),
    ("https://example.org/page?sc_channel=social&sc_campaign=spring&share_id=99&spm=a.1",
     "https://example.org/page", "telegram and shopify"),
    ("https://example.org/page?id=42&utm_source=x",
     "https://example.org/page?id=42", "tracking mixed with content"),

    # Query handling that must not over fold.
    ("https://example.org/search?q=protest&page=2",
     "https://example.org/search?page=2&q=protest", "pairs sorted by name"),
    ("https://example.org/search?page=2&q=protest",
     "https://example.org/search?page=2&q=protest", "already sorted"),
    ("https://example.org/tag?tag=Flood%20Rescue",
     "https://example.org/tag?tag=Flood+Rescue", "space reencoded as plus"),
    ("https://example.org/tag?tag=Flood+Rescue",
     "https://example.org/tag?tag=Flood+Rescue", "plus already present"),
    ("https://example.org/a?b=",
     "https://example.org/a?b=", "blank value kept"),
    ("https://example.org/a?ID=7&Page=2",
     "https://example.org/a?id=7&page=2", "names lowercased"),
    ("https://example.org/a?id=7&id=8",
     "https://example.org/a?id=7&id=8", "repeated name sorted by value"),
    ("https://example.org/a?z=1&a=2&m=3",
     "https://example.org/a?a=2&m=3&z=1", "sorting is total"),

    # Percent encoding and dot segments.
    ("https://example.org/caf%C3%A9/men%C3%BC",
     "https://example.org/caf%C3%A9/men%C3%BC", "reserved escapes kept"),
    ("https://example.org/%61%62%63",
     "https://example.org/abc", "unreserved escapes decoded"),
    ("https://example.org/a%2fb",
     "https://example.org/a%2Fb", "escape case normalized, still reserved"),
    ("https://example.org/~user/%7Ebackup",
     "https://example.org/~user/~backup", "tilde is unreserved"),
    ("https://example.org/a/b/../c",
     "https://example.org/a/c", "dot dot resolved"),
    ("https://example.org/a/./b",
     "https://example.org/a/b", "single dot resolved"),
    ("https://example.org/a/b/..",
     "https://example.org/a", "trailing dot dot"),
    ("https://example.org/a/b/../../c",
     "https://example.org/c", "dot dot past the root"),

    # Ports, userinfo, other schemes.
    ("https://example.org:443/a", "https://example.org/a", "default https port"),
    ("http://example.org:80/a", "https://example.org/a", "default http port"),
    ("https://example.org:8443/a", "https://example.org:8443/a", "real port kept"),
    ("https://user:secret@example.org/a", "https://example.org/a", "userinfo dropped"),
    ("mailto:editor@example.org", "mailto:editor@example.org", "other scheme untouched"),
    ("", "", "empty input"),

    # Redirectors that carry their destination.
    ("https://www.google.com/url?q=https://www.reuters.com/world/story-1&sa=U&ved=0ahUKEwi",
     "https://reuters.com/world/story-1", "google url redirector"),
    ("https://www.google.com/url?url=http://m.bbc.co.uk/news/world-1&usg=AFQjCN",
     "https://bbc.co.uk/news/world-1", "redirector target folded again"),
    ("https://www.ampproject.org/c/s/apnews.com/article/story-1",
     "https://apnews.com/article/story-1", "amp cache c path"),
    ("https://www.google.com/amp/s/nytimes.com/2024/05/01/story.html",
     "https://nytimes.com/2024/05/01/story.html", "amp cache amp path"),
    ("https://news.google.com/rss/articles/CBMiOiFib2lzX?oc=5",
     "https://news.google.com/rss/articles/CBMiOiFib2lzX",
     "opaque google news id is not resolvable, oc is dropped"),

    # International hosts and paths.
    ("https://www.bbc.com/\u65e5\u672c\u8a9e/news-1",
     "https://bbc.com/\u65e5\u672c\u8a9e/news-1", "unicode path left alone"),
    ("https://\u4f8b\u3048.\u30c6\u30b9\u30c8/\u8a18\u4e8b",
     "https://xn--r8jz45g.xn--zckzah/\u8a18\u4e8b", "idna host punycoded"),
    ("https://xn--caf-dma.com/caf%C3%A9",
     "https://xn--caf-dma.com/caf%C3%A9", "already punycode"),
    ("https://www.bbc.com/news/articles/c4g2kq7z5nwo?utm_campaign=brand&at_medium=custom1",
     "https://bbc.com/news/articles/c4g2kq7z5nwo", "at prefix from apple news"),

    # Cases where a fold would be wrong, so u1 leaves them alone.
    ("https://amp.co.uk/news/story-1", "https://amp.co.uk/news/story-1",
     "a public suffix is not stripped"),
    ("https://amp.com.au/news/story-1", "https://amp.com.au/news/story-1",
     "another two label suffix"),
    ("https://www2.example.org/a", "https://www2.example.org/a", "www2 is not www"),
    ("https://example.org//a", "https://example.org//a", "double slash kept"),
    ("https://example.org/A/B/", "https://example.org/A/B", "path case preserved"),
]


@pytest.mark.parametrize("raw,expected,reason", CANONICAL_VECTORS,
                         ids=[v[2] for v in CANONICAL_VECTORS])
def test_canonicalize_url_v1_vector(raw, expected, reason):
    assert canonicalize_url_v1(raw) == expected, reason


def test_vector_count_is_in_the_documented_band():
    assert 80 <= len(CANONICAL_VECTORS) <= 100


# Inputs that must all land on one canonical form, so one article cannot be
# ingested twice because a feed spelled it four ways.
MUST_COLLIDE = [
    [
        "http://apnews.com/article/story-1",
        "https://apnews.com/article/story-1",
        "https://www.apnews.com/article/story-1",
        "https://APNEWS.com/article/story-1/",
        "https://apnews.com/article/story-1#lead",
        "https://apnews.com/article/story-1?utm_source=twitter&utm_medium=social",
        "https://apnews.com/article/story-1?fbclid=IwAR9",
        "https://m.apnews.com/article/story-1",
        "https://amp.apnews.com/article/story-1",
        "https://apnews.com/article/story-1/amp",
        "https://apnews.com/article/story-1?output=1",
        "https://www.google.com/url?q=https://apnews.com/article/story-1",
        "https://www.ampproject.org/c/s/apnews.com/article/story-1",
        "https://www.apnews.com/article/story-1#lead?utm_source=twitter",
    ],
    [
        "https://theguardian.com/world/story-2",
        "https://www.theguardian.com/world/story-2/",
        "https://m.theguardian.com/world/story-2",
        "https://mobile.theguardian.com/world/story-2",
        "https://amp.theguardian.com/world/story-2",
        "https://theguardian.com/world/story-2/amp/",
        "https://theguardian.com/world/story-2?cmp=share&at_medium=custom4",
    ],
    [
        "https://example.org/search?q=flood&page=2",
        "https://example.org/search?page=2&q=flood",
        "https://example.org/search?Q=flood&PAGE=2&utm_source=hn",
    ],
]


@pytest.mark.parametrize("group", MUST_COLLIDE, ids=["apnews", "guardian", "query"])
def test_equivalent_spellings_collapse(group):
    canonical = {canonicalize_url_v1(url) for url in group}
    assert len(canonical) == 1, canonical


# Distinct articles that look alike. Every pair here must stay distinct, since
# a false merge destroys evidence rather than tidying it. Every pair also has to
# produce a different hash, because the hash is the dedup key.
MUST_NOT_COLLIDE = [
    ("https://example.org/a?id=1", "https://example.org/a?id=2",
     "different content parameter values"),
    ("https://example.org/a?page=1", "https://example.org/a?page=2",
     "paging is not tracking"),
    ("https://edition.cnn.com/2024/05/01/story", "https://cnn.com/2024/05/01/story",
     "edition is a different site section"),
    ("https://text.npr.org/nx-s1-1", "https://npr.org/nx-s1-1",
     "text is not a stripped label"),
    ("https://amp.co.uk/news/story-1", "https://example.co.uk/news/story-1",
     "a suffix guard does not merge two hosts"),
    ("https://www2.example.org/a", "https://example.org/a",
     "www2 is not www"),
    ("https://sub.example.org/a", "https://example.org/a",
     "an ordinary subdomain is untouched"),
    ("https://example.org//a", "https://example.org/a",
     "double slash is a different path"),
    ("https://example.org/a%2Fb", "https://example.org/a/b",
     "an escaped slash is not a separator"),
    ("https://example.org:8443/a", "https://example.org/a",
     "a real port is a different origin"),
    ("https://example.org:8443/a", "https://example.org:9443/a",
     "two different ports"),
    ("https://example.org/a?output=2", "https://example.org/a?output=1",
     "only the amp marker value is dropped"),
    ("https://example.org/a?amp=2", "https://example.org/a?amp=1",
     "only the amp marker value is dropped"),
    ("https://example.org/a?ampmode=web", "https://example.org/a",
     "ampmode with a real value stays"),
    ("https://example.org/a?smid=url-share", "https://example.org/a",
     "smid is kept, so it is its own identity"),
    ("https://example.org/a?src=homepage", "https://example.org/a",
     "a bare src is kept, which is the conservative choice"),
    ("https://news.google.com/rss/articles/CBMiOiFib2lzX",
     "https://news.google.com/rss/articles/CBMiOiFib2lzY",
     "opaque ids stay distinct rather than collapsing to one bucket"),
    ("https://feeds.feedburner.com/Guardian", "https://feeds.feedburner.com/BBC",
     "feed labels stay distinct"),
    ("https://example.org/a/b", "https://example.org/a/c",
     "different last segment"),
    ("https://example.org/2024/05/01/story", "https://example.org/2024/05/02/story",
     "a date in the path is identity"),
    ("https://example.org/a/story", "https://example.org/b/story",
     "different middle segment"),
]


@pytest.mark.parametrize("left,right,reason", MUST_NOT_COLLIDE,
                         ids=[f"{i:02d}-{r}" for i, (_, _, r) in enumerate(MUST_NOT_COLLIDE)])
def test_distinct_articles_stay_distinct(left, right, reason):
    assert canonicalize_url_v1(left) != canonicalize_url_v1(right), reason
    assert compute_url_hash(left) != compute_url_hash(right), reason


# Pairs the canonical form keeps apart but the hash does not, because
# compute_url_hash lowercases the canonical form. This is the one known defect of
# u1 and it is pinned in both directions, so a later scheme that fixes it has to
# change these assertions on purpose.
CASE_ONLY_PAIRS = [
    ("https://example.org/Story", "https://example.org/story",
     "path case"),
    ("https://example.org/a/Story", "https://example.org/a/story",
     "one segment of case apart"),
    ("https://example.org/de/Story", "https://example.org/de/story",
     "a locale prefix does not make case matter less"),
    ("https://example.org/a?ID=AbC", "https://example.org/a?id=abc",
     "value case"),
]


@pytest.mark.parametrize("left,right,reason", CASE_ONLY_PAIRS,
                         ids=[r for _, _, r in CASE_ONLY_PAIRS])
def test_case_only_pairs_are_distinct_in_canonical_form(left, right, reason):
    assert canonicalize_url_v1(left) != canonicalize_url_v1(right), reason


@pytest.mark.parametrize("left,right,reason", CASE_ONLY_PAIRS,
                         ids=[r for _, _, r in CASE_ONLY_PAIRS])
def test_case_only_pairs_still_collide_in_the_hash(left, right, reason):
    assert compute_url_hash(left) == compute_url_hash(right), reason


# The hash is an identity key that the Merkle log and every dedup set depend on,
# so its output is pinned byte for byte. Changing a line here is a scheme change,
# not a refactor.
HASH_PINS = [
    ("https://apnews.com/article/story-1",
     "68c62e00b613e7ae37eba0de824910b02033033ce9d16f1ee6b4ae5b99cf1dc8"),
    ("https://www.reuters.com/world/story-2",
     "df67a9d6bc979ba3ec1ffc5c9898b38be2cf1f7fc1a6f2f9ffe6a98018aaec2c"),
    ("https://apnews.com/article/story-1?utm_source=twitter",
     "68c62e00b613e7ae37eba0de824910b02033033ce9d16f1ee6b4ae5b99cf1dc8"),
    ("https://apnews.com/article/story-3",
     "d714b36e8777cc7629b4841673bc900478f732f0a14feb7f8f78325b1714a0b4"),
    ("https://example.org/search?page=2&q=flood",
     "299453ade6a8a8391ea40c2243afe4967b1aabd18cddc00590a2e47385e78e70"),
    ("https://www.theguardian.com/world/2024/may/01/ukraine-war-live",
     "84b7ae851b705decbcdd1eac547760bfc8acc162cfbeaad5558795eb0b63ce44"),
    ("http://apnews.com/article/story-4/",
     "891a1c33b9bfe4db59d1dca71f9f64445abab9e674518747e2c363604cc627ef"),
]


def test_hash_pins_match():
    for url, expected in HASH_PINS:
        assert compute_url_hash(url) == expected, url


def test_hash_is_lowercased_canonical_form_in_utf8():
    """The hashed bytes are exactly canonicalize_url_v1(url).lower() in utf8."""
    for url in [
        "https://example.org/Story?ID=1",
        "https://www.example.org:8443/café?utm_source=x",
        "https://example.org/中文?q=值",
    ]:
        expected = hashlib.sha256(
            canonicalize_url_v1(url).lower().encode("utf-8")
        ).hexdigest()
        assert compute_url_hash(url) == expected
        assert len(compute_url_hash(url)) == 64


def test_hash_is_case_insensitive_in_path_and_values():
    """The one known defect of u1, pinned so nobody mistakes it for a bug.

    compute_url_hash lowercases the whole canonical form, so two paths that
    differ only in case hash alike. A later scheme has to drop that.
    """
    assert compute_url_hash("https://example.org/Story") == compute_url_hash(
        "https://example.org/story"
    )
    assert compute_url_hash("https://example.org/a?ID=AbC") == compute_url_hash(
        "https://example.org/a?id=abc"
    )
    assert canonicalize_url_v1("https://example.org/Story") != canonicalize_url_v1(
        "https://example.org/story"
    )


class TestFunctionContract:
    def test_alias_matches_v1(self):
        for raw, expected, _ in CANONICAL_VECTORS:
            assert canonicalize_url(raw) == canonicalize_url_v1(raw)

    def test_idempotent(self):
        for raw, _, _ in CANONICAL_VECTORS:
            once = canonicalize_url_v1(raw)
            assert canonicalize_url_v1(once) == once, raw

    def test_deterministic(self):
        for raw, _, _ in CANONICAL_VECTORS:
            assert canonicalize_url_v1(raw) == canonicalize_url_v1(raw)

    def test_pure(self):
        """No global state is touched, so the result depends only on the input."""
        before = dict(globals())
        canonicalize_url_v1("https://example.org/a?utm_source=x")
        assert set(globals()) == set(before)

    @pytest.mark.parametrize("raw", [
        "", "   ", "not a url", "https://", "http:///a",
        "javascript:alert(1)", "data:text/html,<h1>x</h1>",
        "https://example.org:99999/a", "https://[2001:db8::1]:443/a",
        "https://exa mple.org/a", "https://example.org/a?%ZZ=1",
        "https://example.org/a?" + "x=1&" * 50,
        "https://xn--/a", "https://..:80/a", "https://example.org/a#" + "f" * 100,
    ])
    def test_never_raises(self, raw):
        assert isinstance(canonicalize_url_v1(raw), str)

    def test_none_is_tolerated_as_empty(self):
        assert canonicalize_url_v1(None) == ""

    def test_ipv6_literal_keeps_brackets(self):
        assert canonicalize_url_v1("https://[2001:db8::1]/a") == "https://[2001:db8::1]/a"

    def test_bare_host_gets_a_path(self):
        assert canonicalize_url_v1("https://example.org") == "https://example.org/"

    def test_output_is_always_absolute_https_for_http_input(self):
        for raw in ["http://example.org", "http://example.org:80/a/",
                    "HTTP://WWW.EXAMPLE.ORG"]:
            out = canonicalize_url_v1(raw)
            assert out.startswith("https://")
            assert "#" not in out
