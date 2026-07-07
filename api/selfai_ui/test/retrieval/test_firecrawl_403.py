"""
Unit tests for the consecutive-403 cancellation logic in SafeFirecrawlLoader.

All external I/O (AsyncFirecrawlApp, httpx) is mocked — no real network
calls are made and no Firecrawl instance is required.

Run from the backend container:
    pytest selfai_ui/test/retrieval/test_firecrawl_403.py -v
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from selfai_ui.retrieval.web.firecrawl import SafeFirecrawlLoader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_page(url: str, status_code: int = 200, markdown: str = "content"):
    """Build a raw Firecrawl page dict as returned by the /v2/crawl poll."""
    return {
        "markdown": markdown,
        "metadata": {
            "sourceURL": url,
            "title": f"Page {url}",
            "statusCode": status_code,
        },
    }


def _poll_response(status: str, pages: list):
    """Build a mock httpx response for a crawl-status poll."""
    resp = MagicMock()
    resp.json.return_value = {
        "status": status,
        "completed": len(pages),
        "total": len(pages),
        "data": pages,
    }
    return resp


class _FakeStart:
    """Minimal stand-in for the object returned by async_app.start_crawl()."""
    id = "fake-crawl-id"


def _make_loader() -> SafeFirecrawlLoader:
    return SafeFirecrawlLoader(
        urls="https://example.com",
        api_key="no-key",
        api_base_url="http://fake-firecrawl",
    )


def _run(coro):
    """Run an async coroutine from a sync test."""
    return asyncio.run(coro)


def _patched_crawl(poll_responses: list, max_consecutive_403s=None, poll_interval=0):
    """
    Context manager that patches AsyncFirecrawlApp + httpx and runs
    crawl_with_progress, returning (job_state, docs, mock_app_instance).
    """
    loader = _make_loader()
    job_state = {
        "job_id": "test",
        "cancelled": False,
        "cancel_reason": None,
        "pages": [],
        "completed": 0,
        "total": 0,
    }

    with patch("selfai_ui.retrieval.web.firecrawl.AsyncFirecrawlApp") as MockApp:
        app_instance = MockApp.return_value
        app_instance.start_crawl = AsyncMock(return_value=_FakeStart())
        app_instance.cancel_crawl = AsyncMock()

        with patch("httpx.AsyncClient") as MockHttpx:
            http = AsyncMock()
            http.__aenter__ = AsyncMock(return_value=http)
            http.__aexit__ = AsyncMock(return_value=False)
            http.get = AsyncMock(side_effect=poll_responses)
            MockHttpx.return_value = http

            docs = _run(loader.crawl_with_progress(
                job_state,
                limit=10,
                poll_interval=poll_interval,
                timeout=60,
                max_consecutive_403s=max_consecutive_403s,
            ))

    return job_state, docs, app_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_consecutive_403s_at_threshold_cancels():
    """Exactly n=3 consecutive 403 pages should cancel the crawl."""
    pages = [_make_page(f"https://example.com/{i}", status_code=403, markdown="") for i in range(3)]
    job_state, docs, app = _patched_crawl(
        poll_responses=[
            _poll_response("scraping", []),          # first poll — no data yet
            _poll_response("scraping", pages),       # second poll — all 403s arrive
        ],
        max_consecutive_403s=3,
    )

    assert job_state["cancelled"] is True
    assert job_state["cancel_reason"] is not None
    assert "403" in job_state["cancel_reason"]
    assert docs == []
    app.cancel_crawl.assert_called_once_with("fake-crawl-id")


def test_consecutive_403s_below_threshold_does_not_cancel():
    """Two 403s followed by a 200, with threshold=3, should not cancel."""
    pages = [
        _make_page("https://example.com/1", status_code=403, markdown=""),
        _make_page("https://example.com/2", status_code=403, markdown=""),
        _make_page("https://example.com/3", status_code=200, markdown="good content"),
    ]
    job_state, docs, app = _patched_crawl(
        poll_responses=[_poll_response("completed", pages)],
        max_consecutive_403s=3,
    )

    assert job_state["cancelled"] is False
    assert len(docs) == 1
    assert docs[0].page_content == "good content"
    app.cancel_crawl.assert_not_called()


def test_403_counter_resets_after_200():
    """Counter should reset on a 200, so split sequences don't accumulate."""
    pages = [
        _make_page("https://example.com/1", status_code=403, markdown=""),
        _make_page("https://example.com/2", status_code=403, markdown=""),
        _make_page("https://example.com/3", status_code=200, markdown="good"),
        # Counter resets here — the following two 403s are only 2 in a row
        _make_page("https://example.com/4", status_code=403, markdown=""),
        _make_page("https://example.com/5", status_code=403, markdown=""),
    ]
    job_state, docs, app = _patched_crawl(
        poll_responses=[_poll_response("completed", pages)],
        max_consecutive_403s=3,
    )

    assert job_state["cancelled"] is False
    assert len(docs) == 1
    app.cancel_crawl.assert_not_called()


def test_403_disabled_when_max_is_none():
    """When max_consecutive_403s=None the feature is off; no cancellation."""
    pages = [_make_page(f"https://example.com/{i}", status_code=403, markdown="blocked") for i in range(10)]
    job_state, docs, app = _patched_crawl(
        poll_responses=[_poll_response("completed", pages)],
        max_consecutive_403s=None,
    )

    assert job_state["cancelled"] is False
    app.cancel_crawl.assert_not_called()


def test_mixed_pages_only_403s_cancelled_not_200s():
    """Pages with 200 status are saved; pages with 403 are not (no content)."""
    pages = [
        _make_page("https://example.com/a", status_code=200, markdown="hello"),
        _make_page("https://example.com/b", status_code=403, markdown=""),
        _make_page("https://example.com/c", status_code=200, markdown="world"),
    ]
    job_state, docs, app = _patched_crawl(
        poll_responses=[_poll_response("completed", pages)],
        max_consecutive_403s=3,
    )

    assert job_state["cancelled"] is False
    assert len(docs) == 2
    assert docs[0].page_content == "hello"
    assert docs[1].page_content == "world"


def test_extract_page_reads_status_code():
    """_extract_page should surface statusCode from dict metadata."""
    loader = _make_loader()
    page = {
        "markdown": "text",
        "metadata": {"sourceURL": "https://x.com", "title": "X", "statusCode": 403},
    }
    _, _, _, status_code = loader._extract_page(page, "https://fallback.com")
    assert status_code == 403


def test_extract_page_missing_status_code_returns_none():
    """_extract_page should return None for status_code when key is absent."""
    loader = _make_loader()
    page = {"markdown": "text", "metadata": {"sourceURL": "https://x.com"}}
    _, _, _, status_code = loader._extract_page(page, "https://fallback.com")
    assert status_code is None
