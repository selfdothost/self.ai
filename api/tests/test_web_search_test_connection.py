"""Admin "Test Connection" probe for the web_search pipeline
(retrieval.test_web_search_connection).

The probe reports the two stages of the in-chat web_search tool separately —
SearXNG search, then Playwright fetch — so a failure names the stage that
caused it instead of collapsing to one opaque "No search results found". These
tests pin the three real-world outcomes:

  * both stages green,
  * search returns nothing (upstream engines rate-limited — the failure that
    prompted this feature),
  * search green but fetch red (the "search.home works but chat says none
    found" split).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.connection import BrowseFetchResult
from selfai_ui.browse.reference_profiles import register_reference_profiles
from selfai_ui.routers.retrieval import test_web_search_connection


@pytest.fixture(autouse=True)
def _profiles_registered():
    # The probe resolves the "general-search" profile for its fetch stage.
    profiles_module._PROFILE_REGISTRY.clear()
    register_reference_profiles()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


def _request(engine="searxng", searxng_url="https://search.example/search?q=<query>"):
    config = SimpleNamespace(
        RAG_WEB_SEARCH_ENGINE=engine,
        SEARXNG_QUERY_URL=searxng_url,
        BROWSE_PLAYWRIGHT_SERVICE_URL="http://fake-playwright:3000",
        BROWSE_PLAYWRIGHT_API_KEY="test-key",
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def _result(link, title="Example"):
    return SimpleNamespace(link=link, title=title, snippet="")


def _admin():
    return SimpleNamespace(id="u1", role="admin")


def _run(request):
    return asyncio.run(test_web_search_connection(request, _admin()))


@pytest.mark.tier0
def test_reports_both_stages_ok_with_engine_health():
    request = _request()
    unresponsive = {"unresponsive_engines": [["brave", "Suspended: too many requests"]]}
    with (
        patch(
            "selfai_ui.routers.retrieval.search_web",
            return_value=[_result("https://example.com/a", "A"), _result("https://example.com/b", "B")],
        ),
        patch(
            "selfai_ui.routers.retrieval.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content="hello world content")),
        ),
        patch(
            "requests.get",
            return_value=SimpleNamespace(ok=True, json=lambda: unresponsive),
        ),
    ):
        out = _run(request)

    assert out["search"]["ok"] is True
    assert out["search"]["count"] == 2
    assert out["search"]["samples"][0] == {"title": "A", "link": "https://example.com/a"}
    # unresponsive_engines is surfaced, not discarded as search_searxng would.
    assert out["search"]["unresponsive_engines"] == [
        {"engine": "brave", "reason": "Suspended: too many requests"}
    ]
    assert out["fetch"]["attempted"] is True
    assert out["fetch"]["ok"] is True
    assert out["fetch"]["url"] == "https://example.com/a"
    assert out["fetch"]["content_chars"] == len("hello world content")


@pytest.mark.tier0
def test_empty_search_is_a_search_stage_failure_and_skips_fetch():
    # The exact failure that prompted this feature: SearXNG returns zero
    # results (upstream engines suspended), so the fetch stage never runs.
    request = _request()
    with (
        patch("selfai_ui.routers.retrieval.search_web", return_value=[]),
        patch(
            "selfai_ui.routers.retrieval.browse_fetch",
            new=AsyncMock(side_effect=AssertionError("fetch must not run when search is empty")),
        ),
        patch("requests.get", return_value=SimpleNamespace(ok=True, json=lambda: {})),
    ):
        out = _run(request)

    assert out["search"]["ok"] is False
    assert out["search"]["count"] == 0
    assert out["fetch"]["attempted"] is False


@pytest.mark.tier0
def test_search_ok_but_fetch_fails_is_reported_as_a_fetch_failure():
    # The "search.home returns results but chat says none found" signature:
    # search is green, the Playwright fetch is red, and the probe keeps them
    # distinct instead of collapsing to a single opaque failure.
    request = _request()
    with (
        patch("selfai_ui.routers.retrieval.search_web", return_value=[_result("https://blocked.example/x")]),
        patch(
            "selfai_ui.routers.retrieval.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=False, error="Playwright service returned HTTP 500")),
        ),
        patch("requests.get", return_value=SimpleNamespace(ok=True, json=lambda: {})),
    ):
        out = _run(request)

    assert out["search"]["ok"] is True
    assert out["fetch"]["attempted"] is True
    assert out["fetch"]["ok"] is False
    assert "500" in out["fetch"]["error"]


@pytest.mark.tier0
def test_non_searxng_engine_skips_the_unresponsive_engines_probe():
    request = _request(engine="brave", searxng_url="")
    with (
        patch("selfai_ui.routers.retrieval.search_web", return_value=[_result("https://example.com/a")]),
        patch(
            "selfai_ui.routers.retrieval.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content="x")),
        ),
        patch("requests.get", side_effect=AssertionError("no engine-health probe for non-searxng engines")),
    ):
        out = _run(request)

    assert out["search"]["engine"] == "brave"
    assert out["search"]["unresponsive_engines"] == []
