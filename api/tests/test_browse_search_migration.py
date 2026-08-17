"""cavekit-browse-search-migration.md R1 (switch scrape backend), R2
(parallel fetch), and R3 (fast-fail policy) — the web_search tool now
fetches through the core browse connection instead of the domain-crawl
backend (self.crawl/Firecrawl), naming the broad-search profile, fetching
concurrently, and the old backend receives no traffic from this path."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.connection import BrowseFetchResult
from selfai_ui.browse.reference_profiles import register_reference_profiles
from selfai_ui.utils.middleware import html_to_text, run_web_search_tool_call


@pytest.fixture(autouse=True)
def _profiles_registered():
    profiles_module._PROFILE_REGISTRY.clear()
    register_reference_profiles()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


def _fake_request():
    config = SimpleNamespace(
        RAG_WEB_SEARCH_ENGINE="searxng",
        BROWSE_PLAYWRIGHT_SERVICE_URL="http://fake-playwright:3000",
        BROWSE_PLAYWRIGHT_API_KEY="test-key",
        TOP_K=3,
        RELEVANCE_THRESHOLD=0.0,
        ENABLE_RAG_HYBRID_SEARCH=False,
        USER_PERMISSIONS={"features": {"web_browsing": True}},
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config, EMBEDDING_FUNCTION=None, rf=None)))


def _search_result(link, title="Example"):
    return SimpleNamespace(link=link, title=title, snippet="")


def _extra_params():
    return {"__event_emitter__": AsyncMock()}


@pytest.mark.tier0
def test_web_search_routes_through_browse_connection_not_firecrawl():
    request = _fake_request()
    # role="admin" deliberately: has_browsing_access's admin path is a pure
    # Python shortcut with no DB call, keeping this tier0 test dependency
    # -free. The permission gate's own behavior (including the DB-backed
    # has_permission path for non-admins) is covered by
    # test_browse_access_control.py, not re-tested here.
    user = SimpleNamespace(id="u1", role="admin")

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_search_result("https://example.com/a")]),
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content="<p>hello world</p>")),
        ) as mock_browse_fetch,
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db", return_value=True),
        patch(
            "selfai_ui.utils.middleware.get_sources_from_files",
            return_value=[{"source": {"name": "web result"}, "document": ["hello world"]}],
        ),
        # If this migration regressed, the old backend would be imported
        # and called here — asserting it's simply gone from the module
        # namespace is the strongest possible proof it's not used.
    ):
        result = asyncio.run(run_web_search_tool_call(request, "what is the weather", _extra_params(), user))

    mock_browse_fetch.assert_called_once()
    called_url = mock_browse_fetch.call_args.args[1]
    assert called_url == "https://example.com/a"
    assert "hello world" in result


@pytest.mark.tier0
def test_web_search_names_the_general_search_profile():
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")  # DB-free admin bypass, see note above

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_search_result("https://example.com/a")]),
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content="<p>content</p>")),
        ) as mock_browse_fetch,
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db", return_value=True),
        patch("selfai_ui.utils.middleware.get_sources_from_files", return_value=[]),
    ):
        asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), user))

    used_profile = mock_browse_fetch.call_args.args[2]
    assert used_profile.name == "general-search"


@pytest.mark.tier0
def test_middleware_module_does_not_import_the_old_backend():
    # R1 AC3 / R7: the previous scraping backend (Firecrawl / self.crawl)
    # receives no traffic from this tool — checked at the import level (not
    # a raw source-text search, which would also flag this file's own
    # explanatory comments about what got replaced and why).
    import selfai_ui.utils.middleware as middleware_module

    assert not hasattr(middleware_module, "process_web_search")
    assert not hasattr(middleware_module, "SafeFirecrawlLoader")
    assert not hasattr(middleware_module, "get_web_loader")


@pytest.mark.tier0
def test_no_search_results_short_circuits_before_any_fetch():
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")  # DB-free admin bypass, see note above

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[]),
        patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()) as mock_browse_fetch,
    ):
        result = asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), user))

    mock_browse_fetch.assert_not_called()
    assert "no search results" in result.lower()


@pytest.mark.tier0
def test_all_fetches_failing_is_distinct_from_no_search_results():
    # Stage 1 (the search engine) returned links, but every stage-2 page
    # fetch fails (bot-protection, 5xx — general-search does not retry). The
    # tool must report this as its OWN failure, not collapse it into the
    # stage-1 "no search results" message: otherwise "search found nothing"
    # and "search found links but none were readable" are indistinguishable
    # to both the user and the model. Regression guard for the SF-events
    # diagnosis (chat 7352e218).
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")  # DB-free admin bypass, see note above

    links = [_search_result("https://a.example/1"), _search_result("https://b.example/2")]
    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=links),
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=False, error="HTTP 500")),
        ) as mock_browse_fetch,
    ):
        result = asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), user))

    # It DID attempt to read the found links...
    assert mock_browse_fetch.call_count == len(links)
    # ...so it must not claim the search returned nothing...
    assert "no search results" not in result.lower()
    # ...and it must report the read failure, naming how many links it found.
    assert "2" in result
    assert "no page content" in result.lower()


@pytest.mark.tier0
def test_denied_user_never_reaches_search_or_fetch():
    # Mocking has_browsing_access directly (rather than routing a non-admin
    # user through the real, DB-backed has_permission chain) tests exactly
    # what this function is responsible for — reacting correctly to a
    # denial — without re-testing has_permission's own internals (covered
    # by test_browse_access_control.py) or touching a DB from a tier0 test.
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="user")

    with (
        patch("selfai_ui.utils.middleware.has_browsing_access", return_value=False),
        patch("selfai_ui.utils.middleware.search_web") as mock_search_web,
        patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()) as mock_browse_fetch,
    ):
        result = asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), user))

    mock_search_web.assert_not_called()
    mock_browse_fetch.assert_not_called()
    assert "not permitted" in result.lower()


@pytest.mark.tier0
def test_admin_bypasses_the_browsing_gate():
    # Genuinely exercises the real admin-bypass code path in
    # has_browsing_access (no has_permission/DB call for role="admin"),
    # unlike the mocked-denial test above.
    request = _fake_request()
    request.app.state.config.USER_PERMISSIONS = {"features": {"web_browsing": False}}
    admin = SimpleNamespace(id="a1", role="admin")

    with (patch("selfai_ui.utils.middleware.search_web", return_value=[]),):
        result = asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), admin))

    assert "not permitted" not in result.lower()


# --- html_to_text (R4 groundwork, formally covered at T-020) ---------------


@pytest.mark.tier0
def test_html_to_text_extracts_readable_text_and_title():
    html = "<html><head><title>Sacramento Weather</title></head><body><p>Sunny, 75F</p></body></html>"
    text, title = html_to_text(html)
    assert "Sunny, 75F" in text
    assert title == "Sacramento Weather"


@pytest.mark.tier0
def test_html_to_text_skips_script_and_style_content():
    html = "<html><body><style>.x{color:red}</style><script>evil()</script><p>real content</p></body></html>"
    text, _ = html_to_text(html)
    assert "evil" not in text
    assert "color:red" not in text
    assert "real content" in text


# --- R2: parallel fetch -----------------------------------------------------


@pytest.mark.tier0
def test_fetches_run_concurrently_not_sequentially():
    # 3 URLs, each fetch artificially takes ~0.1s. Sequential total would be
    # ~0.3s; concurrent total should be close to the single slowest ~0.1s.
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")
    search_results = [_search_result(f"https://example.com/{i}") for i in range(3)]

    async def slow_fetch(_request, _url, _profile):
        await asyncio.sleep(0.1)
        return BrowseFetchResult(success=True, content="<p>content</p>")

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=search_results),
        patch("selfai_ui.utils.middleware.browse_fetch", new=slow_fetch),
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db", return_value=True),
        patch("selfai_ui.utils.middleware.get_sources_from_files", return_value=[]),
    ):
        start = time.monotonic()
        asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), user))
        elapsed = time.monotonic() - start

    # Generous upper bound (well under the ~0.3s sequential sum) to absorb
    # scheduler jitter without being a flaky, razor-thin timing assertion.
    assert elapsed < 0.25, f"expected concurrent fetches (~0.1s), took {elapsed:.3f}s — looks sequential"


# --- R3: fast-fail policy ----------------------------------------------------


@pytest.mark.tier0
def test_failed_urls_are_excluded_without_failing_the_whole_search():
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")
    search_results = [
        _search_result("https://good.example/a"),
        _search_result("https://bad.example/b"),
        _search_result("https://good.example/c"),
    ]

    async def mixed_fetch(_request, url, _profile):
        if "bad" in url:
            return BrowseFetchResult(success=False, error="simulated failure")
        return BrowseFetchResult(success=True, content=f"<p>content for {url}</p>")

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=search_results),
        patch("selfai_ui.utils.middleware.browse_fetch", new=mixed_fetch),
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db", return_value=True) as mock_save,
        patch("selfai_ui.utils.middleware.get_sources_from_files", return_value=[]),
    ):
        result = asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), user))

    assert "failed" not in result.lower()
    saved_docs = mock_save.call_args.args[1]
    saved_sources = {doc.metadata["source"] for doc in saved_docs}
    assert saved_sources == {"https://good.example/a", "https://good.example/c"}


@pytest.mark.tier0
def test_general_search_profile_does_not_retry_extensively():
    # R3 AC3: consistent with the broad-search profile's own tuning —
    # general-search is registered with retry_count=0.
    from selfai_ui.browse.profiles import resolve_profile

    profile = resolve_profile("general-search")
    assert profile.retry_count == 0


@pytest.mark.tier0
def test_all_urls_failing_reports_a_read_failure_not_a_hard_error():
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")
    search_results = [_search_result("https://bad.example/a")]

    async def always_fails(_request, _url, _profile):
        return BrowseFetchResult(success=False, error="simulated failure")

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=search_results),
        patch("selfai_ui.utils.middleware.browse_fetch", new=always_fails),
    ):
        result = asyncio.run(run_web_search_tool_call(request, "query", _extra_params(), user))

    # Graceful string, never a raised/hard error. And it's the page-read
    # failure, kept distinct from the stage-1 "no search results" case (the
    # engine DID return a link here) — see
    # test_all_fetches_failing_is_distinct_from_no_search_results.
    assert "no search results" not in result.lower()
    assert "no page content" in result.lower()


# --- R4: own content extraction (formal close-out) --------------------------


@pytest.mark.tier0
def test_html_to_text_has_no_dependency_on_the_removed_backend():
    # R4 AC2: extraction doesn't require the removed scraping backend to be
    # present or running — checked structurally (html_to_text takes a raw
    # string and stdlib-parses it; nothing here can reach a network
    # service, let alone specifically Firecrawl).
    import inspect

    from selfai_ui.utils.middleware import _HTMLTextExtractor, html_to_text

    for obj in (html_to_text, _HTMLTextExtractor):
        source = inspect.getsource(obj)
        assert "firecrawl" not in source.lower()
        assert "request" not in source  # no Request/network dependency at all


# --- R6: frontend-visible status events unchanged ---------------------------


@pytest.mark.tier0
def test_status_event_sequence_matches_pre_migration_shape():
    # R6: same event types/fields at the same points in the lifecycle, so
    # no frontend change is required. This is the literal contract
    # WebSearchResults.svelte depends on (action="web_search", specific
    # description strings, urls on the "done" event).
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")
    emitter = AsyncMock()

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_search_result("https://example.com/a")]),
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content="<p>hello</p>")),
        ),
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db", return_value=True),
        patch(
            "selfai_ui.utils.middleware.get_sources_from_files",
            return_value=[{"source": {"name": "web result"}, "document": ["hello"]}],
        ),
    ):
        asyncio.run(run_web_search_tool_call(request, "weather today", {"__event_emitter__": emitter}, user))

    events = [call.args[0] for call in emitter.call_args_list]
    assert len(events) == 2

    searching, done = events
    assert searching["type"] == "status"
    assert searching["data"]["action"] == "web_search"
    assert searching["data"]["done"] is False
    assert searching["data"]["query"] == "weather today"

    assert done["type"] == "status"
    assert done["data"]["action"] == "web_search"
    assert done["data"]["done"] is True
    assert done["data"]["urls"] == ["https://example.com/a"]
    assert "error" not in done["data"]


@pytest.mark.tier0
def test_no_results_event_matches_pre_migration_shape():
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")
    emitter = AsyncMock()

    with patch("selfai_ui.utils.middleware.search_web", return_value=[]):
        asyncio.run(run_web_search_tool_call(request, "query", {"__event_emitter__": emitter}, user))

    events = [call.args[0] for call in emitter.call_args_list]
    done = events[-1]
    assert done["data"]["action"] == "web_search"
    assert done["data"]["description"] == "No search results found"
    assert done["data"]["done"] is True
    assert done["data"]["error"] is True


# R7 (domain-crawl feature untouched, zero shared code) is covered by:
# - test_middleware_module_does_not_import_the_old_backend, above
#   (import-level: no coupling from the consumer side)
# - a one-time repo-history fact, not an ongoing unit-testable property:
#   `git diff origin/main...HEAD --stat -- api/selfai_ui/routers/
#   retrieval.py api/selfai_ui/retrieval/` returns empty — the file that
#   actually implements process_web_search/search_web/get_web_loader/
#   Firecrawl has zero changes anywhere in this branch. Recorded in
#   context/impl/impl-browse-connection.md rather than as a fake test
#   that can never fail.


# --- R5: downstream pipeline unchanged ---------------------------------------


@pytest.mark.tier0
def test_retrieval_call_shape_is_unchanged_from_pre_migration():
    # R5 AC2: embedding and relevance-retrieval steps unchanged by this
    # migration — proven by asserting get_sources_from_files (and
    # save_docs_to_vector_db) receive exactly the same argument shape they
    # always did; only *how* docs/collection_name/urls get populated
    # changed (browse_fetch instead of Firecrawl), not how they're
    # processed downstream.
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_search_result("https://example.com/a")]),
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content="<p>hello</p>")),
        ),
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db", return_value=True) as mock_save,
        patch("selfai_ui.utils.middleware.get_sources_from_files", return_value=[]) as mock_sources,
    ):
        asyncio.run(run_web_search_tool_call(request, "weather today", _extra_params(), user))

    # save_docs_to_vector_db: request, docs, collection_name, overwrite=True
    save_args = mock_save.call_args
    assert save_args.args[0] is request
    assert save_args.kwargs.get("overwrite") is True

    # get_sources_from_files: same files/queries/embedding/k/reranking/r/hybrid shape
    sources_kwargs = mock_sources.call_args.kwargs
    assert sources_kwargs["queries"] == ["weather today"]
    assert sources_kwargs["embedding_function"] is request.app.state.EMBEDDING_FUNCTION
    assert sources_kwargs["k"] == request.app.state.config.TOP_K
    assert sources_kwargs["reranking_function"] is request.app.state.rf
    assert sources_kwargs["r"] == request.app.state.config.RELEVANCE_THRESHOLD
    assert sources_kwargs["hybrid_search"] == request.app.state.config.ENABLE_RAG_HYBRID_SEARCH

    files_arg = sources_kwargs["files"]
    assert len(files_arg) == 1
    assert files_arg[0]["type"] == "web_search_results"
    assert files_arg[0]["name"] == "weather today"
    assert set(files_arg[0].keys()) == {"collection_name", "name", "type", "urls"}


@pytest.mark.tier0
def test_output_source_formatting_is_unchanged():
    # R5 AC1: given equivalent retrieved content, the excerpts returned to
    # the model have the same output shape as pre-migration — the literal
    # <source name="...">...</source> wrapping the model reads as tool
    # message content.
    request = _fake_request()
    user = SimpleNamespace(id="u1", role="admin")

    with (
        patch("selfai_ui.utils.middleware.search_web", return_value=[_search_result("https://example.com/a")]),
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content="<p>hello</p>")),
        ),
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db", return_value=True),
        patch(
            "selfai_ui.utils.middleware.get_sources_from_files",
            return_value=[{"source": {"name": "weather.com"}, "document": ["It is sunny today."]}],
        ),
    ):
        result = asyncio.run(run_web_search_tool_call(request, "weather today", _extra_params(), user))

    assert result == '<source name="weather.com">\nIt is sunny today.\n</source>'
