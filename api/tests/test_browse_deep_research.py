"""cavekit-browse-web-access.md R4 (link-following research tool), R5
(server-enforced traversal budget), and R6 (cross-origin hop policy).

deep_research does search -> read -> follow -> assemble inside one tool call.
The budget bounding that traversal is configuration, never a tool argument, and
links discovered in page content may only be followed on the same site — the
rule that stops an injected page from steering the traversal to a destination
of its choosing.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.connection import BrowseFetchResult
from selfai_ui.browse.hop_policy import host_of, is_same_site
from selfai_ui.browse.reference_profiles import register_reference_profiles
from selfai_ui.utils.middleware import (
    DEEP_RESEARCH_TOOL_SPEC,
    _dispatch_tool_call,
    run_deep_research_tool_call,
)


@pytest.fixture(autouse=True)
def _profiles_registered():
    profiles_module._PROFILE_REGISTRY.clear()
    register_reference_profiles()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


def _fake_request(depth=2, pages=12, seconds=90, concurrency=5, per_page=6000, links=50, browsing=True):
    config = SimpleNamespace(
        RAG_WEB_SEARCH_ENGINE="searxng",
        BROWSE_PLAYWRIGHT_SERVICE_URL="http://fake-playwright:3000",
        BROWSE_PLAYWRIGHT_API_KEY="test-key",
        BROWSE_MAX_LINKS_PER_PAGE=links,
        DEEP_RESEARCH_MAX_DEPTH=depth,
        DEEP_RESEARCH_MAX_PAGES=pages,
        DEEP_RESEARCH_MAX_SECONDS=seconds,
        DEEP_RESEARCH_CONCURRENCY=concurrency,
        DEEP_RESEARCH_MAX_CHARS_PER_PAGE=per_page,
        # robots.txt honoring is off in this file — these tests cover the
        # traversal, budget, and hop policy. robots behavior has its own suite
        # (test_browse_robots.py). Off means the new config reads still resolve
        # but the robots code path is skipped.
        DEEP_RESEARCH_RESPECT_ROBOTS=False,
        BROWSE_USER_AGENT="self.ai-research",
        DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS=10.0,
        USER_PERMISSIONS={"features": {"web_browsing": browsing}},
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def _extra_params():
    return {"__event_emitter__": AsyncMock()}


def _admin():
    return SimpleNamespace(id="u1", role="admin")


def _result(link):
    return SimpleNamespace(link=link, title="R", snippet="")


def _html(body, title="P"):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _page_with_links(*hrefs, text="Body text here"):
    anchors = "".join(f'<a href="{h}">link</a>' for h in hrefs)
    return _html(f"<p>{text}</p>{anchors}")


def _fetcher(pages: dict, default=None):
    """An async browse_fetch stand-in serving `pages` by URL."""

    async def _fetch(request, url, profile):
        if url in pages:
            return BrowseFetchResult(success=True, content=pages[url])
        if default is not None:
            return BrowseFetchResult(success=True, content=default)
        return BrowseFetchResult(success=False, error="not found")

    return _fetch


def _run(request, query="q", extra=None, user=None):
    return asyncio.run(run_deep_research_tool_call(request, query, extra or _extra_params(), user or _admin()))


def _fetched_urls(mock_fetch_calls):
    return [c.args[1] for c in mock_fetch_calls]


# --- R4: the traversal ----------------------------------------------------


@pytest.mark.tier0
def test_deep_research_follows_a_link_from_a_page_it_read():
    request = _fake_request()
    pages = {
        "https://site.example/a": _page_with_links("/b"),
        "https://site.example/b": _html("<p>Second page body</p>"),
    }
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return await _fetcher(pages)(req, url, profile)

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        result = _run(request)

    assert calls == ["https://site.example/a", "https://site.example/b"]
    assert "Second page body" in result


@pytest.mark.tier0
def test_deep_research_names_the_link_follow_profile():
    request = _fake_request()
    seen = []

    async def _fetch(req, url, profile):
        seen.append(profile.name)
        return BrowseFetchResult(success=True, content=_html("<p>Body</p>"))

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        _run(request)

    assert seen == ["link-follow"]


@pytest.mark.tier0
def test_a_failed_hop_is_skipped_not_fatal():
    request = _fake_request()
    pages = {"https://site.example/ok": _html("<p>Good page</p>")}

    with (
        patch(
            "selfai_ui.utils.middleware.search_web",
            return_value=[_result("https://site.example/bad"), _result("https://site.example/ok")],
        ),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetcher(pages)),
    ):
        result = _run(request)

    assert "Good page" in result
    assert "read 1 page" in result


@pytest.mark.tier0
def test_no_readable_page_is_stated_not_empty():
    request = _fake_request()
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetcher({})),
    ):
        result = _run(request)

    assert "No pages could be read" in result


@pytest.mark.tier0
def test_no_search_results_is_stated():
    request = _fake_request()
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()) as mock_fetch,
    ):
        result = _run(request)

    assert "No search results found" in result
    mock_fetch.assert_not_called()


@pytest.mark.tier0
def test_deep_research_is_gated_before_any_search_or_fetch():
    request = _fake_request(browsing=False)
    with (
        patch("selfai_ui.utils.middleware.search_web") as mock_search,
        patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()) as mock_fetch,
    ):
        result = _run(request, user=SimpleNamespace(id="u2", role="user"))

    assert "not permitted" in result
    mock_search.assert_not_called()
    mock_fetch.assert_not_called()


@pytest.mark.tier0
def test_a_url_is_never_fetched_twice():
    request = _fake_request()
    pages = {
        "https://site.example/a": _page_with_links("/b", "/a"),
        "https://site.example/b": _page_with_links("/a"),
    }
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return await _fetcher(pages)(req, url, profile)

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        _run(request)

    assert len(calls) == len(set(calls))


# --- gating: deep_research is its own toggle ------------------------------


@pytest.mark.tier0
def test_dispatch_refuses_deep_research_when_its_toggle_is_off():
    """Belt-and-suspenders: the tool is only offered when features.deep_research
    is set, but a model can emit a name it was never offered. The dispatcher
    refuses it without running any search or fetch."""
    request = _fake_request()
    extra = {"__event_emitter__": AsyncMock(), "__metadata__": {"features": {"web_search": True}}}
    tool_call = {"function": {"name": "deep_research", "arguments": '{"query": "x"}'}}

    messages = [{"role": "user", "content": "x"}]
    with (
        patch("selfai_ui.utils.middleware.search_web") as mock_search,
        patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()) as mock_fetch,
    ):
        result = asyncio.run(_dispatch_tool_call(request, tool_call, {}, extra, _admin(), messages))

    assert "not enabled" in result
    mock_search.assert_not_called()
    mock_fetch.assert_not_called()


@pytest.mark.tier0
def test_dispatch_runs_deep_research_when_its_toggle_is_on():
    request = _fake_request()
    extra = {"__event_emitter__": AsyncMock(), "__metadata__": {"features": {"deep_research": True}}}
    tool_call = {"function": {"name": "deep_research", "arguments": '{"query": "x"}'}}

    messages = [{"role": "user", "content": "x"}]
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=_fetcher({"https://site.example/a": _html("<p>Body</p>")}),
        ),
    ):
        result = asyncio.run(_dispatch_tool_call(request, tool_call, {}, extra, _admin(), messages))

    assert "Researched" in result


# --- R5: the budget -------------------------------------------------------


@pytest.mark.tier0
def test_budget_is_not_a_tool_parameter():
    """R5: the model is offered `query` and nothing else."""
    props = DEEP_RESEARCH_TOOL_SPEC.input_schema["properties"]
    assert list(props) == ["query"]
    for forbidden in ("depth", "max_depth", "max_pages", "pages", "timeout", "max_seconds"):
        assert forbidden not in props


@pytest.mark.tier0
def test_supplying_budget_arguments_has_no_effect():
    """Arguments beyond `query` are ignored — run_deep_research_tool_call has
    no parameter to receive them through in the first place."""
    import inspect

    params = set(inspect.signature(run_deep_research_tool_call).parameters)
    assert params == {"request", "query", "extra_params", "user"}


@pytest.mark.tier0
def test_depth_limit_stops_link_following():
    request = _fake_request(depth=0)
    pages = {
        "https://site.example/a": _page_with_links("/b"),
        "https://site.example/b": _html("<p>Should never be read</p>"),
    }
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return await _fetcher(pages)(req, url, profile)

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        result = _run(request)

    assert calls == ["https://site.example/a"]
    assert "Should never be read" not in result


@pytest.mark.tier0
def test_depth_limit_is_counted_per_hop():
    """depth=1: links on a search result are followed; links on the page that
    reached are not."""
    request = _fake_request(depth=1, concurrency=1)
    pages = {
        "https://site.example/a": _page_with_links("/b"),
        "https://site.example/b": _page_with_links("/c"),
        "https://site.example/c": _html("<p>Too deep</p>"),
    }
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return await _fetcher(pages)(req, url, profile)

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        _run(request)

    assert calls == ["https://site.example/a", "https://site.example/b"]


@pytest.mark.tier0
def test_page_limit_bounds_the_traversal_and_is_stated():
    request = _fake_request(pages=2, concurrency=5)
    seeds = [_result(f"https://site.example/{i}") for i in range(6)]
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return BrowseFetchResult(success=True, content=_html("<p>Body</p>"))

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=seeds),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        result = _run(request)

    assert len(calls) == 2, "the last batch must not overshoot the page budget"
    assert "read 2 page" in result
    assert "2-page limit" in result


@pytest.mark.tier0
def test_wall_clock_limit_returns_what_was_gathered_and_says_so():
    """R5: the bound covers the whole traversal, and reaching it returns the
    pages gathered so far rather than failing."""
    request = _fake_request(seconds=0.5, concurrency=1)
    pages = {
        "https://site.example/a": _page_with_links("/slow"),
        "https://site.example/slow": _html("<p>Never arrives</p>"),
    }

    async def _fetch(req, url, profile):
        if url.endswith("/slow"):
            await asyncio.sleep(5)
        return await _fetcher(pages)(req, url, profile)

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        result = _run(request)

    assert "Body text here" in result
    assert "Never arrives" not in result
    assert "time limit" in result
    assert "Stopped early" in result


@pytest.mark.tier0
def test_a_completed_traversal_does_not_claim_it_stopped_early():
    request = _fake_request()
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetcher({"https://site.example/a": _html("<p>B</p>")})),
    ):
        result = _run(request)

    assert "Stopped early" not in result


@pytest.mark.tier0
def test_long_pages_are_excerpted_per_page_and_say_so():
    request = _fake_request(per_page=100)
    long_page = _html("<p>" + ("y" * 5000) + "</p>")
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetcher({"https://site.example/a": long_page})),
    ):
        result = _run(request)

    assert "Excerpt" in result
    assert "100 characters" in result


# --- R6: the hop policy ---------------------------------------------------


@pytest.mark.tier0
def test_an_offsite_link_in_page_content_is_not_followed():
    """The injection-chain break: a page that says "go here" to somewhere it
    does not own must not be obeyed."""
    request = _fake_request()
    pages = {
        "https://site.example/a": _page_with_links("https://attacker.example/steal?q=secrets"),
    }
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return await _fetcher(pages)(req, url, profile)

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        result = _run(request)

    assert calls == ["https://site.example/a"]
    assert not any("attacker.example" in c for c in calls)
    # R6: recorded, not silently dropped.
    assert "not followed" in result


@pytest.mark.tier0
def test_a_subdomain_link_in_page_content_is_followed():
    request = _fake_request()
    pages = {
        "https://site.example/a": _page_with_links("https://docs.site.example/b"),
        "https://docs.site.example/b": _html("<p>Docs body</p>"),
    }
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return await _fetcher(pages)(req, url, profile)

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        result = _run(request)

    assert "https://docs.site.example/b" in calls
    assert "Docs body" in result


@pytest.mark.tier0
def test_search_provider_urls_may_be_any_origin():
    """R6: depth-0 URLs did not come from page content, so no page chose them."""
    request = _fake_request()
    seeds = [_result("https://one.example/x"), _result("https://two.example/y")]
    calls = []

    async def _fetch(req, url, profile):
        calls.append(url)
        return BrowseFetchResult(success=True, content=_html("<p>Body</p>"))

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=seeds),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetch),
    ):
        _run(request)

    assert set(calls) == {"https://one.example/x", "https://two.example/y"}


@pytest.mark.tier0
def test_the_hop_policy_is_applied_before_the_fetch_is_attempted():
    request = _fake_request()
    pages = {"https://site.example/a": _page_with_links("https://attacker.example/x")}
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetcher(pages)) ,
        patch("selfai_ui.utils.middleware.is_same_site", return_value=False) as mock_policy,
    ):
        _run(request)

    mock_policy.assert_called()


@pytest.mark.tier0
@pytest.mark.parametrize(
    "page_url,candidate,expected",
    [
        ("https://example.com/a", "https://example.com/b", True),
        ("https://example.com/a", "https://docs.example.com/b", True),
        ("https://www.example.com/a", "https://example.com/b", True),
        ("https://www.example.com/a", "https://docs.example.com/b", False),
        # No PSL here, and that is deliberate — this must refuse rather than
        # treat two unrelated .co.uk registrations as one site.
        ("https://foo.co.uk/a", "https://bar.co.uk/b", False),
        ("https://example.com/a", "https://attacker.example/steal", False),
        ("https://example.com/a", "https://notexample.com/b", False),
        # A suffix that merely *looks* like the page host must not match.
        ("https://example.com/a", "https://example.com.attacker.test/b", False),
        # Case and a trailing root dot are the same host written differently.
        ("https://example.com/a", "https://EXAMPLE.COM./b", True),
        ("https://example.com/a", "https://example.com:8443/b", True),
        # Unparseable authority: refuse rather than half-trust the hostname.
        ("https://example.com/a", "https://example.com:bad/b", False),
        ("https://example.com/a", "not-a-url", False),
        ("https://example.com/a", "", False),
        ("", "https://example.com/b", False),
    ],
)
def test_is_same_site(page_url, candidate, expected):
    assert is_same_site(page_url, candidate) is expected


@pytest.mark.tier0
def test_host_of_normalizes_case_and_trailing_dot():
    assert host_of("https://EXAMPLE.COM./path") == "example.com"
    assert host_of("not-a-url") is None
    assert host_of("https://example.com:bad/") is None


# --- R8: status -----------------------------------------------------------


@pytest.mark.tier0
def test_deep_research_reports_progress_and_always_terminates():
    request = _fake_request(concurrency=1)
    extra = _extra_params()
    pages = {
        "https://site.example/a": _page_with_links("/b"),
        "https://site.example/b": _html("<p>Second</p>"),
    }
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_result("https://site.example/a")]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetcher(pages)),
    ):
        _run(request, extra=extra)

    statuses = [c.args[0]["data"] for c in extra["__event_emitter__"].call_args_list]
    assert all(s["action"] == "deep_research" for s in statuses)
    assert statuses[0]["done"] is False
    assert statuses[-1]["done"] is True
    # Progress, not just start and end: one update per batch as pages land.
    assert len([s for s in statuses if s["done"] is False]) >= 2
    assert statuses[-1]["urls"] == ["https://site.example/a", "https://site.example/b"]


@pytest.mark.tier0
def test_deep_research_terminates_status_on_failure():
    request = _fake_request()
    extra = _extra_params()
    with (
        patch("selfai_ui.utils.middleware.search_web", side_effect=RuntimeError("searxng down")),
        patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()),
    ):
        result = _run(request, extra=extra)

    statuses = [c.args[0]["data"] for c in extra["__event_emitter__"].call_args_list]
    assert statuses[-1]["done"] is True
    assert statuses[-1]["error"] is True
    assert "Research failed" in result


# --- R7: provenance -------------------------------------------------------


@pytest.mark.tier0
def test_every_page_carries_its_source_url():
    request = _fake_request()
    seeds = [_result("https://one.example/x"), _result("https://two.example/y")]
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=seeds),
        patch("selfai_ui.utils.middleware.browse_fetch", new=_fetcher({}, default=_html("<p>Body</p>"))),
    ):
        result = _run(request)

    assert 'url="https://one.example/x"' in result
    assert 'url="https://two.example/y"' in result
    assert result.count("</source>") == 2
