"""Tests for the URL-scheme allowlist (S-P1-4).

Jinja autoescaping does not validate URL schemes, so every template that puts
a feed-origin URL into href/src pipes it through the `is_safe_url` filter
(src/shared/safe_url.py, registered in curation_ui/app_state.py). These tests
pin the allowlist: http/https pass, everything browsers would execute as
script (including whitespace/case/tab evasions) is rejected.
"""
import pytest

from src.shared.safe_url import is_safe_url, safe_url


@pytest.mark.parametrize("url", [
    "https://example.com/article",
    "http://example.com/article",
    "HTTPS://EXAMPLE.COM/UPPER",
    "https://example.com:8080/path?q=1#frag",
])
def test_safe_urls_pass(url):
    assert is_safe_url(url)


@pytest.mark.parametrize("url", [
    "javascript:alert(document.cookie)",
    "JAVASCRIPT:alert(1)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)",
    "\x00javascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "/proof/some-id",
    "proof/some-id",
    "",
    "//example.com/protocol-relative",
])
def test_unsafe_urls_rejected(url):
    assert not is_safe_url(url)


@pytest.mark.parametrize("url", [None, 123, b"https://example.com", ["https://example.com"]])
def test_non_strings_rejected(url):
    assert not is_safe_url(url)


def test_safe_url_filter_returns_hash_for_unsafe():
    assert safe_url("https://example.com/a") == "https://example.com/a"
    assert safe_url("javascript:alert(1)") == "#"
    assert safe_url(None) == "#"


def test_filter_is_registered_on_the_jinja_env():
    from curation_ui.app_state import templates

    assert "is_safe_url" in templates.env.filters
    assert templates.env.filters["is_safe_url"]("javascript:alert(1)") == "#"
    assert templates.env.filters["is_safe_url"]("https://example.com/") == "https://example.com/"
