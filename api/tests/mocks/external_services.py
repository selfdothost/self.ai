"""
External service mock fixtures for backend tests.

Provides respx-based fixtures that intercept outbound HTTP calls to each
external service. Each fixture supports success, error, and streaming
response scenarios.

Usage in tests:
    def test_something(mock_ollama):
        mock_ollama.success()
        # ... test code that hits Ollama endpoints ...
"""

import json
from contextlib import contextmanager

import httpx
import pytest
import respx

# ---------------------------------------------------------------------------
# Service base URLs (match selfai_ui/config.py and env defaults)
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL = "http://self-ollama:11434"
OPENAI_BASE_URL = "https://api.openai.com"
LLAMOLOTL_BASE_URL = "http://self-llamolotl:8080"
CURATOR_BASE_URL = "http://self-curator:8000"
LANGUAGE_EVAL_BASE_URL = "http://self-language-eval:5000"
CODE_EVAL_BASE_URL = "http://self-code-eval:5001"


# ---------------------------------------------------------------------------
# Helper: SSE stream builder
# ---------------------------------------------------------------------------


def _sse_stream(events: list[dict]) -> bytes:
    """Build a well-formed SSE byte stream from a list of event dicts."""
    lines = []
    for event in events:
        lines.append(f"data: {json.dumps(event)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


# ---------------------------------------------------------------------------
# Mock wrapper class
# ---------------------------------------------------------------------------


class ServiceMock:
    """Wrapper around a respx router for a single external service."""

    def __init__(self, router: respx.MockRouter, base_url: str):
        self.router = router
        self.base_url = base_url

    def success(
        self,
        path: str = "",
        method: str = "GET",
        json_body: dict | None = None,
        status_code: int = 200,
    ):
        """Mock a successful JSON response."""
        body = json_body or {"status": "ok"}
        route = self.router.request(method, f"{self.base_url}{path}")
        route.mock(return_value=httpx.Response(status_code, json=body))
        return route

    def error(
        self,
        path: str = "",
        method: str = "GET",
        status_code: int = 500,
        detail: str = "Internal Server Error",
    ):
        """Mock an error response."""
        route = self.router.request(method, f"{self.base_url}{path}")
        route.mock(return_value=httpx.Response(status_code, json={"detail": detail}))
        return route

    def timeout(self, path: str = "", method: str = "GET"):
        """Mock a connection timeout."""
        route = self.router.request(method, f"{self.base_url}{path}")
        route.mock(side_effect=httpx.ConnectTimeout("Connection timed out"))
        return route

    def stream(self, path: str = "", method: str = "POST", events: list[dict] | None = None):
        """Mock an SSE streaming response."""
        events = events or [{"token": "Hello"}, {"token": " world"}]
        content = _sse_stream(events)
        route = self.router.request(method, f"{self.base_url}{path}")
        route.mock(
            return_value=httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
            )
        )
        return route

    def catch_all(self, status_code: int = 200, json_body: dict | None = None):
        """Mock all requests to this service's base URL."""
        body = json_body or {"status": "ok"}
        route = self.router.route(url__startswith=self.base_url)
        route.mock(return_value=httpx.Response(status_code, json=body))
        return route


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ollama_base_urls(test_app):
    """Read current Ollama base URLs from app config (list)."""
    try:
        urls = list(test_app.state.config.OLLAMA_BASE_URLS)
    except AttributeError:
        urls = [OLLAMA_BASE_URL]
    return urls or [OLLAMA_BASE_URL]


def _openai_base_urls(test_app):
    try:
        urls = list(test_app.state.config.OPENAI_API_BASE_URLS)
    except AttributeError:
        urls = [OPENAI_BASE_URL]
    return urls or [OPENAI_BASE_URL]


def _llamolotl_base_urls(test_app):
    try:
        urls = list(test_app.state.config.LLAMOLOTL_BASE_URLS)
    except AttributeError:
        urls = [LLAMOLOTL_BASE_URL]
    return urls or [LLAMOLOTL_BASE_URL]


@pytest.fixture
def mock_ollama(test_app):
    """Mock all outbound HTTP to Ollama with strict assertion mode.

    An unmatched request fails the test. A registered mock that is
    never called also fails the test. This enforces the zero-real-network
    contract from cavekit-ui-router-tests-proxies.md R9.
    """
    urls = _ollama_base_urls(test_app)
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        mock = ServiceMock(router, urls[0])
        yield mock


@pytest.fixture
def mock_openai(test_app):
    """Mock OpenAI endpoints with strict assertion mode."""
    urls = _openai_base_urls(test_app)
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        mock = ServiceMock(router, urls[0])
        yield mock


@pytest.fixture
def mock_llamolotl(test_app):
    """Mock Llamolotl endpoints with strict assertion mode."""
    urls = _llamolotl_base_urls(test_app)
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        mock = ServiceMock(router, urls[0])
        yield mock


@pytest.fixture
def mock_curator():
    """Mock Curator endpoints with strict assertion mode."""
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        mock = ServiceMock(router, CURATOR_BASE_URL)
        yield mock


# ---------------------------------------------------------------------------
# Lenient variants — for tests that only want to block real-network calls
# without enforcing that all registered mocks were called. Use sparingly.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ollama_lenient(test_app):
    """Ollama mock that blocks unmocked requests but does not require all mocks to be called."""
    urls = _ollama_base_urls(test_app)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        mock = ServiceMock(router, urls[0])
        # Health check pre-registered to avoid surprise hits during startup
        router.get(f"{urls[0]}/").mock(return_value=httpx.Response(200, text="Ollama is running"))
        router.head(f"{urls[0]}/").mock(return_value=httpx.Response(200))
        yield mock


@pytest.fixture
def mock_llamolotl_lenient(test_app):
    urls = _llamolotl_base_urls(test_app)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        mock = ServiceMock(router, urls[0])
        router.get(f"{urls[0]}/").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        router.head(f"{urls[0]}/").mock(return_value=httpx.Response(200))
        yield mock


@pytest.fixture
def mock_curator_lenient():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        mock = ServiceMock(router, CURATOR_BASE_URL)
        router.get(f"{CURATOR_BASE_URL}/health").mock(return_value=httpx.Response(200, json={"status": "healthy"}))
        yield mock


@pytest.fixture
def mock_eval_harness():
    """Mock all outbound HTTP to language-eval and code-eval harnesses with strict assertion mode."""
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        lm_mock = ServiceMock(router, LANGUAGE_EVAL_BASE_URL)
        code_eval_mock = ServiceMock(router, CODE_EVAL_BASE_URL)
        yield {"language_eval": lm_mock, "code_eval": code_eval_mock}


@pytest.fixture
def mock_eval_harness_lenient():
    """Eval harness mocks that block unmocked URLs but don't require all-called."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        lm_mock = ServiceMock(router, LANGUAGE_EVAL_BASE_URL)
        code_eval_mock = ServiceMock(router, CODE_EVAL_BASE_URL)
        yield {"language_eval": lm_mock, "code_eval": code_eval_mock}


# ---------------------------------------------------------------------------
# aioresponses strict helper — enforces "at least one mock was called"
# ---------------------------------------------------------------------------


@contextmanager
def aioresponses_strict(*args, **kwargs):
    """aioresponses context manager that fails if no registered mock was called.

    Use in place of `aioresponses()` for forwarding tests:

        with aioresponses_strict() as m:
            m.get(...)
            resp = client.get(...)
            # On exit: fails if m.requests is empty
    """
    try:
        from aioresponses import aioresponses as _aioresponses
    except ImportError:  # pragma: no cover
        raise RuntimeError("aioresponses not installed")

    with _aioresponses(*args, **kwargs) as m:
        yield m
        if not m.requests:
            raise AssertionError(
                "aioresponses: no registered mock was called. "
                "The test registered mocks but the router never hit upstream. "
                "Either remove the unused mocks or verify the code path."
            )
