"""
Unit tests for includePaths / excludePaths / regexOnFullUrl / crawlEntireDomain
options in SafeFirecrawlLoader.crawl_with_progress().

All external I/O is mocked — no real network calls or Firecrawl instance needed.

Run from the backend container:
    pytest selfai_ui/test/retrieval/test_firecrawl_crawl_options.py -v
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

from selfai_ui.retrieval.web.firecrawl import SafeFirecrawlLoader


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_firecrawl_403.py)
# ---------------------------------------------------------------------------

class _FakeStart:
    id = "fake-crawl-id"


def _make_loader() -> SafeFirecrawlLoader:
    return SafeFirecrawlLoader(
        urls="https://example.com",
        api_key="no-key",
        api_base_url="http://fake-firecrawl",
    )


def _completed_response(pages=None):
    resp = MagicMock()
    resp.json.return_value = {
        "status": "completed",
        "completed": len(pages or []),
        "total": len(pages or []),
        "data": pages or [],
    }
    return resp


def _run_crawl(loader, job_state, mock_poll_responses, **kwargs):
    """Patch AsyncFirecrawlApp + httpx, run crawl_with_progress, return (docs, mock_app_instance)."""
    with patch("selfai_ui.retrieval.web.firecrawl.AsyncFirecrawlApp") as MockApp:
        app_instance = MockApp.return_value
        app_instance.start_crawl = AsyncMock(return_value=_FakeStart())
        app_instance.cancel_crawl = AsyncMock()

        with patch("httpx.AsyncClient") as MockHttpx:
            http = AsyncMock()
            http.__aenter__ = AsyncMock(return_value=http)
            http.__aexit__ = AsyncMock(return_value=False)
            http.get = AsyncMock(side_effect=mock_poll_responses)
            MockHttpx.return_value = http

            docs = asyncio.run(loader.crawl_with_progress(job_state, **kwargs))

    return docs, app_instance


def _fresh_job_state():
    return {
        "job_id": "test",
        "cancelled": False,
        "cancel_reason": None,
        "pages": [],
        "completed": 0,
        "total": 0,
    }


# ---------------------------------------------------------------------------
# Tests: start_crawl kwargs
# ---------------------------------------------------------------------------

def test_include_paths_passed_to_start_crawl():
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=5,
        include_paths=["/docs/.*", "/blog/.*"],
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert kwargs["include_paths"] == ["/docs/.*", "/blog/.*"]


def test_exclude_paths_passed_to_start_crawl():
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=5,
        exclude_paths=["/admin/.*", "/login"],
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert kwargs["exclude_paths"] == ["/admin/.*", "/login"]


def test_regex_on_full_url_passed_to_start_crawl():
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=5,
        regex_on_full_url=True,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert kwargs["regex_on_full_url"] is True


def test_regex_on_full_url_false_is_passed():
    """Explicitly False should still be forwarded (not omitted)."""
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=5,
        regex_on_full_url=False,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert kwargs["regex_on_full_url"] is False


def test_regex_on_full_url_none_omitted():
    """None (default) should not send the key at all."""
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=5,
        regex_on_full_url=None,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert "regex_on_full_url" not in kwargs


# ---------------------------------------------------------------------------
# Tests: crawlEntireDomain overrides limit
# ---------------------------------------------------------------------------

def test_crawl_entire_domain_omits_limit():
    """When crawl_entire_domain=True, limit must NOT be sent to Firecrawl."""
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=10,
        crawl_entire_domain=True,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert "limit" not in kwargs
    assert kwargs["crawl_entire_domain"] is True


def test_crawl_entire_domain_false_includes_limit():
    """When crawl_entire_domain=False, limit is still sent normally."""
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=7,
        crawl_entire_domain=False,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert kwargs["limit"] == 7
    assert kwargs["crawl_entire_domain"] is False


def test_default_no_crawl_entire_domain_includes_limit():
    """Default (crawl_entire_domain=None) keeps the limit."""
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=3,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert kwargs["limit"] == 3
    assert "crawl_entire_domain" not in kwargs


def test_crawl_entire_domain_with_include_exclude_paths():
    """crawl_entire_domain + path filters should all reach start_crawl."""
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=10,
        crawl_entire_domain=True,
        include_paths=["/products/.*"],
        exclude_paths=["/cart", "/checkout"],
        regex_on_full_url=True,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert "limit" not in kwargs
    assert kwargs["crawl_entire_domain"] is True
    assert kwargs["include_paths"] == ["/products/.*"]
    assert kwargs["exclude_paths"] == ["/cart", "/checkout"]
    assert kwargs["regex_on_full_url"] is True


# ---------------------------------------------------------------------------
# Tests: empty/None paths are omitted
# ---------------------------------------------------------------------------

def test_empty_include_paths_not_sent():
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=5,
        include_paths=[],
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert "include_paths" not in kwargs


def test_none_exclude_paths_not_sent():
    loader = _make_loader()
    docs, app = _run_crawl(
        loader, _fresh_job_state(),
        [_completed_response()],
        limit=5,
        exclude_paths=None,
        poll_interval=0,
        timeout=10,
    )
    _, kwargs = app.start_crawl.call_args
    assert "exclude_paths" not in kwargs
