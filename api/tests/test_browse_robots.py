"""robots.txt honoring for deep_research (cavekit-browse-web-access.md — the
crawler-etiquette item deferred in the treasuremap).

deep_research follows links across a site, so it respects robots.txt: a URL an
origin disallows for our agent is skipped and counted, and a declared
Crawl-delay is honored (capped). web_fetch — one page a user named — is
deliberately NOT gated. robots.txt is fetched through the same tunnelled
connection as pages (browse_fetch raw_text=True).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.connection import BrowseFetchResult, html_to_plaintext
from selfai_ui.browse.reference_profiles import register_reference_profiles
from selfai_ui.browse.robots import RobotsCache
from selfai_ui.utils.middleware import run_deep_research_tool_call, run_web_fetch_tool_call


@pytest.fixture(autouse=True)
def _profiles_registered():
    profiles_module._PROFILE_REGISTRY.clear()
    register_reference_profiles()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


def _fake_request(respect_robots=True, ua="self.ai-research", max_crawl_delay=10.0, **over):
    cfg = dict(
        RAG_WEB_SEARCH_ENGINE="searxng",
        BROWSE_PLAYWRIGHT_SERVICE_URL="http://fake:3000",
        BROWSE_PLAYWRIGHT_API_KEY="k",
        BROWSE_MAX_LINKS_PER_PAGE=50,
        BROWSE_FETCH_MAX_CHARS=50000,
        BROWSE_USER_AGENT=ua,
        DEEP_RESEARCH_MAX_DEPTH=2,
        DEEP_RESEARCH_MAX_PAGES=12,
        DEEP_RESEARCH_MAX_SECONDS=90,
        DEEP_RESEARCH_CONCURRENCY=5,
        DEEP_RESEARCH_MAX_CHARS_PER_PAGE=6000,
        DEEP_RESEARCH_RESPECT_ROBOTS=respect_robots,
        DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS=max_crawl_delay,
        USER_PERMISSIONS={"features": {"web_browsing": True}},
    )
    cfg.update(over)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(**cfg))))


def _admin():
    return SimpleNamespace(id="u1", role="admin")


def _result(link):
    return SimpleNamespace(link=link, title="R", snippet="")


def _html(body, title="P"):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _pre(text):
    """robots.txt as Chromium renders a text/plain resource — wrapped in <pre>."""
    return f"<html><head></head><body><pre>{text}</pre></body></html>"


def _serve(pages: dict, robots: dict):
    """A browse_fetch stand-in. `robots` maps netloc -> robots.txt body served
    (as <pre>) for that host's /robots.txt; `pages` maps url -> html."""

    async def _fetch(request, url, profile, raw_text=False):
        from urllib.parse import urlparse

        p = urlparse(url)
        if url.endswith("/robots.txt"):
            body = robots.get(p.netloc)
            if body is None:
                return BrowseFetchResult(success=False, error="404")
            # robots.py fetches with raw_text=True, so the real browse_fetch
            # would already have de-tagged the <pre>-wrapped text/plain body.
            # This mock stands in for that call, so it returns the de-tagged
            # text directly.
            return BrowseFetchResult(success=True, content=body)
        if url in pages:
            return BrowseFetchResult(success=True, content=pages[url])
        return BrowseFetchResult(success=False, error="not found")

    return _fetch


# --- html_to_plaintext ----------------------------------------------------


@pytest.mark.tier0
def test_plaintext_recovers_robots_from_pre_wrapping():
    body = "User-agent: *\nDisallow: /private\nCrawl-delay: 2\nDisallow: /q?a=1&amp;b=2\n"
    out = html_to_plaintext(_pre(body))
    assert "Disallow: /private" in out
    assert "Crawl-delay: 2" in out
    assert "a=1&b=2" in out  # entity unescaped
    assert "<pre>" not in out


@pytest.mark.tier0
def test_plaintext_drops_script_contents():
    out = html_to_plaintext("<pre>ok</pre><script>var x='leak'</script>")
    assert "ok" in out
    assert "leak" not in out


# --- RobotsCache ----------------------------------------------------------


def _cache_request(robots: dict):
    req = _fake_request()
    return req


@pytest.mark.tier0
def test_absent_robots_is_allow_all():
    req = _fake_request()
    cache = RobotsCache("self.ai-research")
    profile = profiles_module.resolve_profile("link-follow")
    with patch("selfai_ui.browse.robots.browse_fetch", new=_serve({}, {})):
        allowed = asyncio.run(cache.is_allowed(req, "https://nowhere.example/x", profile))
    assert allowed is True


@pytest.mark.tier0
def test_disallowed_path_is_refused():
    req = _fake_request()
    cache = RobotsCache("self.ai-research")
    profile = profiles_module.resolve_profile("link-follow")
    robots = {"site.example": "User-agent: *\nDisallow: /private\n"}
    with patch("selfai_ui.browse.robots.browse_fetch", new=_serve({}, robots)):
        allowed_priv = asyncio.run(cache.is_allowed(req, "https://site.example/private/x", profile))
        allowed_pub = asyncio.run(cache.is_allowed(req, "https://site.example/public", profile))
    assert allowed_priv is False
    assert allowed_pub is True


@pytest.mark.tier0
def test_crawl_delay_is_read_for_our_agent():
    req = _fake_request()
    cache = RobotsCache("otherbot")  # falls to * group, which has the delay
    profile = profiles_module.resolve_profile("link-follow")
    robots = {"site.example": "User-agent: *\nCrawl-delay: 3\n"}
    with patch("selfai_ui.browse.robots.browse_fetch", new=_serve({}, robots)):
        delay = asyncio.run(cache.crawl_delay(req, "https://site.example/x", profile))
    assert delay == 3.0


@pytest.mark.tier0
def test_robots_fetched_once_per_origin():
    req = _fake_request()
    cache = RobotsCache("self.ai-research")
    profile = profiles_module.resolve_profile("link-follow")
    calls = []

    async def _fetch(request, url, profile, raw_text=False):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return BrowseFetchResult(success=True, content="User-agent: *\nDisallow: /no\n")
        return BrowseFetchResult(success=False, error="x")

    with patch("selfai_ui.browse.robots.browse_fetch", new=_fetch):

        async def _many():
            return await asyncio.gather(
                *(cache.is_allowed(req, f"https://site.example/p{i}", profile) for i in range(6))
            )

        asyncio.run(_many())

    robots_fetches = [c for c in calls if c.endswith("/robots.txt")]
    assert len(robots_fetches) == 1, f"robots.txt fetched {len(robots_fetches)} times, expected 1"


# --- deep_research integration --------------------------------------------


def _run_dr(request, query="q"):
    return asyncio.run(run_deep_research_tool_call(request, query, {"__event_emitter__": AsyncMock()}, _admin()))


@pytest.mark.tier0
def test_deep_research_skips_disallowed_seed_and_counts_it():
    request = _fake_request()
    robots = {"site.example": "User-agent: *\nDisallow: /blocked\n"}
    served_pages = {
        "https://site.example/ok": _html("<p>Allowed body</p>"),
    }
    seeds = [_result("https://site.example/blocked/x"), _result("https://site.example/ok")]
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=seeds),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_serve(served_pages, robots)),
        patch("selfai_ui.browse.robots.browse_fetch", new=_serve(served_pages, robots)),
    ):
        out = _run_dr(request)

    assert "Allowed body" in out
    assert "respect robots.txt" in out
    assert "read 1 page" in out


@pytest.mark.tier0
def test_respect_robots_false_fetches_disallowed_urls():
    request = _fake_request(respect_robots=False)
    robots = {"site.example": "User-agent: *\nDisallow: /blocked\n"}
    served = {"https://site.example/blocked/x": _html("<p>Fetched anyway</p>")}
    seeds = [_result("https://site.example/blocked/x")]
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=seeds),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_serve(served, robots)),
        patch("selfai_ui.browse.robots.browse_fetch", new=_serve(served, robots)),
    ):
        out = _run_dr(request)

    assert "Fetched anyway" in out
    assert "respect robots.txt" not in out


@pytest.mark.tier0
def test_a_crawl_delay_over_the_cap_skips_the_origin():
    request = _fake_request(max_crawl_delay=5.0)
    robots = {"slow.example": "User-agent: *\nCrawl-delay: 3600\n"}
    served = {"https://slow.example/a": _html("<p>Should be skipped</p>")}
    seeds = [_result("https://slow.example/a")]
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=seeds),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_serve(served, robots)),
        patch("selfai_ui.browse.robots.browse_fetch", new=_serve(served, robots)),
    ):
        out = _run_dr(request)

    assert "No pages could be read" in out


@pytest.mark.tier0
def test_a_small_crawl_delay_is_honored_and_the_page_is_read():
    request = _fake_request(max_crawl_delay=10.0)
    robots = {"ok.example": "User-agent: *\nCrawl-delay: 0\n"}
    served = {"https://ok.example/a": _html("<p>Read with delay</p>")}
    seeds = [_result("https://ok.example/a")]
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=seeds),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_serve(served, robots)),
        patch("selfai_ui.browse.robots.browse_fetch", new=_serve(served, robots)),
    ):
        out = _run_dr(request)

    assert "Read with delay" in out


# --- web_fetch is NOT gated -----------------------------------------------


@pytest.mark.tier0
def test_web_fetch_ignores_robots_txt():
    """A single page a user named is a user action, not crawling. web_fetch
    reads it regardless of robots.txt and never fetches robots.txt at all."""
    request = _fake_request()
    served = {"https://site.example/blocked/x": _html("<p>User asked for this</p>")}
    robots = {"site.example": "User-agent: *\nDisallow: /blocked\n"}
    calls = []

    async def _fetch(request, url, profile, raw_text=False):
        calls.append(url)
        f = _serve(served, robots)
        return await f(request, url, profile, raw_text)

    extra = {"__event_emitter__": AsyncMock()}
    with patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch):
        out = asyncio.run(
            run_web_fetch_tool_call(request, "https://site.example/blocked/x", extra, _admin())
        )

    assert "User asked for this" in out
    assert not any(c.endswith("/robots.txt") for c in calls), "web_fetch must not consult robots.txt"
