"""robots.txt Crawl-delay honoring for the Knowledge-Base domain crawl
(Firecrawl), layered on top of the user's static delay.

A KB crawl is crawler behavior, so before it starts, the target's robots.txt
Crawl-delay is read (through the browse/Playwright tunnel) and Firecrawl's
inter-request delay is set to max(static delay, robots Crawl-delay). The static
delay keeps its existing meaning (the per-page render wait); this only adds the
inter-request politeness spacing.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.connection import BrowseFetchResult
from selfai_ui.browse.reference_profiles import register_reference_profiles
from selfai_ui.browse.robots import RobotsCache
from selfai_ui.retrieval.web.firecrawl import SafeFirecrawlLoader


@pytest.fixture(autouse=True)
def _profiles_registered():
    profiles_module._PROFILE_REGISTRY.clear()
    register_reference_profiles()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


def _serve_robots(robots: dict):
    async def _fetch(request, url, profile, raw_text=False):
        from urllib.parse import urlparse

        netloc = urlparse(url).netloc
        body = robots.get(netloc)
        if body is None:
            return BrowseFetchResult(success=False, error="404")
        return BrowseFetchResult(success=True, content=body)

    return _fetch


# --- RobotsCache.max_crawl_delay ------------------------------------------


@pytest.mark.tier0
def test_max_crawl_delay_takes_the_largest_across_targets():
    cache = RobotsCache("self.ai-research")
    profile = profiles_module.resolve_profile("direct-fetch")
    robots = {
        "a.example": "User-agent: *\nCrawl-delay: 2\n",
        "b.example": "User-agent: *\nCrawl-delay: 7\n",
        "c.example": "User-agent: *\n",  # no delay
    }
    urls = ["https://a.example/x", "https://b.example/y", "https://c.example/z"]
    with patch("selfai_ui.browse.robots.browse_fetch", new=_serve_robots(robots)):
        delay = asyncio.run(cache.max_crawl_delay(SimpleNamespace(), urls, profile))
    assert delay == 7.0


@pytest.mark.tier0
def test_max_crawl_delay_is_none_when_no_target_declares_one():
    cache = RobotsCache("self.ai-research")
    profile = profiles_module.resolve_profile("direct-fetch")
    robots = {"a.example": "User-agent: *\nDisallow: /x\n"}  # rules but no delay
    with patch("selfai_ui.browse.robots.browse_fetch", new=_serve_robots(robots)):
        delay = asyncio.run(cache.max_crawl_delay(SimpleNamespace(), ["https://a.example/x"], profile))
    assert delay is None


# --- crawl_with_progress delay passthrough --------------------------------


def _completed_status(url):
    return {
        "status": "completed",
        "completed": 1,
        "total": 1,
        "data": [{"markdown": "hello", "metadata": {"sourceURL": url, "title": "T"}}],
        "next": None,
    }


class _FakeCrawlApp:
    """Captures the kwargs start_crawl is called with."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def start_crawl(self, url, **kwargs):
        _FakeCrawlApp.captured = dict(kwargs)
        return SimpleNamespace(id="crawl-1")

    async def cancel_crawl(self, crawl_id):
        return None


class _FakeHttpResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def json(self):
        return self._p


class _FakeHttpClient:
    def __init__(self, url):
        self._url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _FakeHttpResp(_completed_status(self._url))


def _run_crawl(delay=None, robots_crawl_delay=None):
    _FakeCrawlApp.captured = {}
    loader = SafeFirecrawlLoader(urls=["https://site.example"], api_key="k", api_base_url=None)
    job_state = {"pages": [], "job_id": "j1"}

    def _client_factory(*a, **k):
        return _FakeHttpClient("https://site.example/a")

    with (
        patch("selfai_ui.retrieval.web.firecrawl.AsyncFirecrawlApp", _FakeCrawlApp),
        patch("selfai_ui.retrieval.web.firecrawl.httpx.AsyncClient", _client_factory),
    ):
        asyncio.run(
            loader.crawl_with_progress(
                job_state, limit=5, delay=delay, robots_crawl_delay=robots_crawl_delay
            )
        )
    return _FakeCrawlApp.captured


@pytest.mark.tier0
def test_crawl_delay_is_the_max_of_static_and_robots():
    captured = _run_crawl(delay=2, robots_crawl_delay=7.0)
    assert captured.get("delay") == 7.0


@pytest.mark.tier0
def test_static_delay_wins_when_larger_than_robots():
    captured = _run_crawl(delay=9, robots_crawl_delay=3.0)
    assert captured.get("delay") == 9


@pytest.mark.tier0
def test_robots_delay_alone_sets_the_crawl_delay():
    captured = _run_crawl(delay=None, robots_crawl_delay=4.0)
    assert captured.get("delay") == 4.0


@pytest.mark.tier0
def test_no_delay_kwarg_when_neither_is_set():
    captured = _run_crawl(delay=None, robots_crawl_delay=None)
    assert "delay" not in captured, "Firecrawl's default is left untouched when nothing to honor"
