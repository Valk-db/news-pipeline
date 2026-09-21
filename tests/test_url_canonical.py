"""Tests for canonicalize_url and compute_url_hash."""

from src.utils.trafilatura_extract import canonicalize_url, compute_url_hash


class TestCanonicalizeUrl:
    """Test URL canonicalization."""

    def test_tracking_params_removed(self):
        """Tracking params don't change the canonical URL."""
        url = "https://example.com/article?utm_source=test&utm_medium=email&fbclid=123&at_campaign=foo&at_medium=bar&id=keep"
        canon = canonicalize_url(url)
        # tracking params removed, non-tracking params kept
        assert "utm_source" not in canon
        assert "utm_medium" not in canon
        assert "fbclid" not in canon
        assert "at_campaign" not in canon
        assert "at_medium" not in canon
        assert "id=keep" in canon

    def test_http_vs_https_same(self):
        """http vs https gives same canonical URL."""
        canon1 = canonicalize_url("http://example.com/article")
        canon2 = canonicalize_url("https://example.com/article")
        assert canon1 == canon2

    def test_www_vs_bare_host_same(self):
        """www vs bare host gives same canonical URL."""
        canon1 = canonicalize_url("https://www.example.com/article")
        canon2 = canonicalize_url("https://example.com/article")
        assert canon1 == canon2

    def test_trailing_slash_normalized(self):
        """Trailing slash is stripped."""
        canon1 = canonicalize_url("https://example.com/article/")
        canon2 = canonicalize_url("https://example.com/article")
        assert canon1 == canon2

    def test_fragment_removed(self):
        """Fragment is removed."""
        canon1 = canonicalize_url("https://example.com/article#section")
        canon2 = canonicalize_url("https://example.com/article")
        assert canon1 == canon2

    def test_host_case_insensitive(self):
        """Host case doesn't matter."""
        canon1 = canonicalize_url("https://EXAMPLE.COM/article")
        canon2 = canonicalize_url("https://example.com/article")
        assert canon1 == canon2

    def test_path_case_insensitive(self):
        """Path case doesn't matter (lowercased in compute_url_hash)."""
        # canonicalize_url preserves path case, but compute_url_hash lowercases everything
        h1 = compute_url_hash("https://example.com/Article/Path")
        h2 = compute_url_hash("https://example.com/article/path")
        assert h1 == h2

    def test_different_paths_different(self):
        """Different paths give different hashes."""
        h1 = compute_url_hash("https://example.com/article1")
        h2 = compute_url_hash("https://example.com/article2")
        assert h1 != h2

    def test_non_tracking_params_differ(self):
        """Non-tracking params with different values give different canonical URLs."""
        canon1 = canonicalize_url("https://example.com/article?id=1")
        canon2 = canonicalize_url("https://example.com/article?id=2")
        assert canon1 != canon2

    def test_param_order_doesnt_matter(self):
        """Param order doesn't matter (sorted)."""
        canon1 = canonicalize_url("https://example.com/article?a=1&b=2")
        canon2 = canonicalize_url("https://example.com/article?b=2&a=1")
        assert canon1 == canon2


class TestComputeUrlHash:
    """Test URL hash computation."""

    def test_same_canonical_same_hash(self):
        """Same canonical URL gives same hash."""
        url1 = "https://example.com/article?utm_source=test&id=1"
        url2 = "https://example.com/article?id=1"
        assert compute_url_hash(url1) == compute_url_hash(url2)

    def test_different_urls_different_hashes(self):
        """Different canonical URLs give different hashes."""
        hash1 = compute_url_hash("https://example.com/article1")
        hash2 = compute_url_hash("https://example.com/article2")
        assert hash1 != hash2

    def test_hash_is_deterministic(self):
        """Hash is deterministic."""
        url = "https://example.com/article?utm_foo=bar&id=123"
        h1 = compute_url_hash(url)
        h2 = compute_url_hash(url)
        assert h1 == h2

    def test_case_insensitive_full(self):
        """Full URL case insensitivity: http vs https, www, host case, path case."""
        urls = [
            "HTTP://WWW.EXAMPLE.COM/ARTICLE?UTM_SOURCE=TEST&ID=1",
            "https://example.com/article?id=1",
            "https://Example.com/Article?id=1",
        ]
        hashes = [compute_url_hash(u) for u in urls]
        assert len(set(hashes)) == 1