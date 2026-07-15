"""
_normalize_next_url — self-hosted Firecrawl echoes its own (internal,
plain-http) view of itself in the crawl-status `next` pagination link, even
when reached over a TLS-terminating ingress. Following that link literally
404s against the origin, which silently caps crawl ingestion at the first
page of results (~80-100 items) no matter how large the crawl actually is.
"""

import pytest

from selfai_ui.retrieval.web.firecrawl import _normalize_next_url


@pytest.mark.tier0
def test_rewrites_scheme_and_host_to_base_url():
    next_url = "http://scrape.example.com/v2/crawl/abc-123?skip=81"
    base_url = "https://scrape.example.com"
    assert (
        _normalize_next_url(next_url, base_url)
        == "https://scrape.example.com/v2/crawl/abc-123?skip=81"
    )


@pytest.mark.tier0
def test_leaves_already_matching_url_unchanged():
    next_url = "https://scrape.example.com/v2/crawl/abc-123?skip=81"
    base_url = "https://scrape.example.com"
    assert _normalize_next_url(next_url, base_url) == next_url


@pytest.mark.tier0
def test_none_passthrough():
    assert _normalize_next_url(None, "https://scrape.example.com") is None


@pytest.mark.tier0
def test_empty_string_passthrough():
    assert _normalize_next_url("", "https://scrape.example.com") == ""
