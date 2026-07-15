"""cavekit-browse-connection.md R1 (mutual authentication), R2 (fetch
request/response contract), R3 (script/tracker stripping), R4 (SSRF
protection — literal-IP and hostname-resolution), R5 (allow/blocklist
enforcement), and R6 (tuning enforcement)."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.browse.connection import (
    _hostname_resolves_to_blocked_ip,
    browse_fetch,
    strip_scripts_and_trackers,
)
from selfai_ui.browse.profiles import BrowseProfile
from tests.mocks.external_services import aioresponses_strict

PLAYWRIGHT_URL = "http://fake-playwright:3000"


def _fake_request(service_url=PLAYWRIGHT_URL, api_key="test-key"):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    BROWSE_PLAYWRIGHT_SERVICE_URL=service_url,
                    BROWSE_PLAYWRIGHT_API_KEY=api_key,
                )
            )
        )
    )


def _profile(**overrides):
    defaults = {"name": "general-search", "timeout_seconds": 5.0, "retry_count": 0}
    defaults.update(overrides)
    return BrowseProfile(**defaults)


@pytest.mark.tier0
def test_valid_fetch_returns_content_and_success():
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": "<p>hello</p>"})
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", _profile()))
    assert result.success is True
    assert result.content == "<p>hello</p>"
    assert result.error is None


@pytest.mark.tier0
def test_network_failure_is_a_clear_distinguishable_failure():
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=502)
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", _profile()))
    assert result.success is False
    assert result.content is None
    assert result.error is not None


@pytest.mark.tier0
def test_empty_content_is_reported_as_failure_not_silent_success():
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": ""})
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", _profile()))
    assert result.success is False
    assert result.error is not None


@pytest.mark.tier0
def test_rejected_credentials_is_a_clear_failure():
    # R1: a service response indicating our credentials were rejected must
    # surface as a clear, distinguishable failure.
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=401)
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", _profile()))
    assert result.success is False
    assert "credentials" in result.error.lower()


@pytest.mark.tier0
def test_outbound_request_carries_configured_credentials():
    # R1: credentials come from configuration, not a literal in source —
    # verified here by confirming the configured key is what gets sent.
    captured_headers = {}

    def capture_callback(url, **kwargs):
        from aioresponses import CallbackResult

        captured_headers.update(kwargs.get("headers") or {})
        return CallbackResult(status=200, payload={"content": "ok"})

    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", callback=capture_callback)
        asyncio.run(browse_fetch(_fake_request(api_key="secret-abc-123"), "https://example.com", _profile()))

    assert captured_headers.get("Authorization") == "Bearer secret-abc-123"


# --- R3: script/tracker stripping ------------------------------------------


@pytest.mark.tier0
def test_strips_inline_script_tags():
    html = '<html><body><p>hello</p><script>alert("x")</script><p>world</p></body></html>'
    result = strip_scripts_and_trackers(html)
    assert "<script" not in result
    assert "alert" not in result
    assert "hello" in result and "world" in result


@pytest.mark.tier0
def test_strips_known_tracker_resource_tags():
    html = '<html><body><img src="https://www.google-analytics.com/collect?x=1"><p>content</p></body></html>'
    result = strip_scripts_and_trackers(html)
    assert "google-analytics.com" not in result
    assert "content" in result


@pytest.mark.tier0
def test_fetch_strips_scripts_before_returning_content():
    with aioresponses_strict() as m:
        m.post(
            f"{PLAYWRIGHT_URL}/scrape",
            status=200,
            payload={"content": "<p>real</p><script>evil()</script>"},
        )
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", _profile()))
    assert result.success is True
    assert "<script" not in result.content
    assert "real" in result.content


@pytest.mark.tier0
def test_fetch_content_that_is_only_a_script_is_a_failure():
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": "<script>only()</script>"})
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", _profile()))
    assert result.success is False


# --- R4: SSRF protection (literal IP addresses) -----------------------------


@pytest.mark.tier0
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/secret",
        "http://172.31.254.1/secret",
        "http://192.168.1.1/secret",
        "http://127.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_refuses_private_loopback_link_local_literal_ips_with_no_fetch(url):
    from aioresponses import aioresponses

    # Deliberately NOT aioresponses_strict() — that variant asserts at
    # least one mock was called, which is the opposite of what this test
    # proves. Plain aioresponses() lets us assert zero requests happened.
    with aioresponses() as m:
        result = asyncio.run(browse_fetch(_fake_request(), url, _profile()))
        assert not m.requests, f"expected no network fetch for {url}, but one was attempted"
    assert result.success is False
    assert result.content is None


@pytest.mark.tier0
def test_public_literal_ip_is_not_blocked():
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": "<p>public</p>"})
        result = asyncio.run(browse_fetch(_fake_request(), "http://8.8.8.8/", _profile()))
    assert result.success is True


# --- R4: SSRF protection (hostname resolution) ------------------------------


def _addrinfo(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.mark.tier0
def test_hostname_resolving_to_private_ip_is_blocked():
    with patch("asyncio.get_running_loop") as mock_get_loop:
        mock_get_loop.return_value.getaddrinfo = AsyncMock(return_value=_addrinfo("10.0.0.5"))
        assert asyncio.run(_hostname_resolves_to_blocked_ip("internal.example.com")) is True


@pytest.mark.tier0
def test_hostname_resolving_to_public_ip_is_not_blocked():
    with patch("asyncio.get_running_loop") as mock_get_loop:
        mock_get_loop.return_value.getaddrinfo = AsyncMock(return_value=_addrinfo("93.184.216.34"))
        assert asyncio.run(_hostname_resolves_to_blocked_ip("example.com")) is False


@pytest.mark.tier0
def test_resolution_failure_is_not_treated_as_blocked():
    # DNS failure isn't an SSRF concern — the fetch attempt itself will
    # surface a clear network-failure result instead.
    with patch("asyncio.get_running_loop") as mock_get_loop:
        mock_get_loop.return_value.getaddrinfo = AsyncMock(side_effect=socket.gaierror("nxdomain"))
        assert asyncio.run(_hostname_resolves_to_blocked_ip("does-not-exist.invalid")) is False


@pytest.mark.tier0
def test_fetch_refuses_hostname_resolving_to_blocked_ip_with_no_network_call():
    from aioresponses import aioresponses

    with patch(
        "selfai_ui.browse.connection._hostname_resolves_to_blocked_ip",
        new=AsyncMock(return_value=True),
    ):
        with aioresponses() as m:
            result = asyncio.run(browse_fetch(_fake_request(), "http://internal.example.com/", _profile()))
            assert not m.requests
    assert result.success is False
    assert result.content is None


# --- R5: allow/blocklist enforcement ----------------------------------------


@pytest.mark.tier0
def test_url_outside_allowlist_is_refused_with_no_fetch():
    from aioresponses import aioresponses

    profile = _profile(allowlist=["weather.com"])
    with aioresponses() as m:
        result = asyncio.run(browse_fetch(_fake_request(), "https://random-blog.example/", profile))
        assert not m.requests
    assert result.success is False
    assert "allowlist" in result.error


@pytest.mark.tier0
def test_url_inside_allowlist_is_fetched():
    profile = _profile(allowlist=["weather.com"])
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": "<p>forecast</p>"})
        result = asyncio.run(browse_fetch(_fake_request(), "https://weather.com/forecast", profile))
    assert result.success is True


@pytest.mark.tier0
def test_allowlist_matches_subdomains():
    profile = _profile(allowlist=["weather.com"])
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": "<p>forecast</p>"})
        result = asyncio.run(browse_fetch(_fake_request(), "https://www.weather.com/forecast", profile))
    assert result.success is True


@pytest.mark.tier0
def test_url_matching_blocklist_is_refused_with_no_fetch():
    from aioresponses import aioresponses

    profile = _profile(blocklist=["blocked.example"])
    with aioresponses() as m:
        result = asyncio.run(browse_fetch(_fake_request(), "https://blocked.example/", profile))
        assert not m.requests
    assert result.success is False
    assert "blocklist" in result.error


@pytest.mark.tier0
def test_blocklist_wins_over_allowlist():
    # A URL that satisfies the allowlist but also matches the blocklist
    # must still be refused — blocklist is the more specific override.
    from aioresponses import aioresponses

    profile = _profile(allowlist=["example.com"], blocklist=["bad.example.com"])
    with aioresponses() as m:
        result = asyncio.run(browse_fetch(_fake_request(), "https://bad.example.com/", profile))
        assert not m.requests
    assert result.success is False


# --- R6: tuning enforcement (timeout + retry count) -------------------------


@pytest.mark.tier0
def test_timeout_comes_from_the_resolved_profile():
    import aiohttp as aiohttp_module

    profile = _profile(timeout_seconds=3.5)
    # side_effect=the real constructor: ClientTimeout is still genuinely
    # built (so aioresponses/ClientSession behave normally), we just also
    # record what it was called with.
    with patch("selfai_ui.browse.connection.aiohttp.ClientTimeout", side_effect=aiohttp_module.ClientTimeout) as mt:
        with aioresponses_strict() as m:
            m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": "<p>ok</p>"})
            asyncio.run(browse_fetch(_fake_request(), "https://example.com", profile))
    mt.assert_called_once_with(total=3.5)


@pytest.mark.tier0
def test_retry_count_matches_profile_on_repeated_failure():
    # retry_count=2 -> up to 3 total attempts (1 initial + 2 retries).
    profile = _profile(retry_count=2)
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=502, repeat=True)
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", profile))
    assert result.success is False
    assert sum(len(calls) for calls in m.requests.values()) == 3


@pytest.mark.tier0
def test_zero_retry_count_means_a_single_attempt():
    profile = _profile(retry_count=0)
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=502)
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", profile))
    assert result.success is False
    assert sum(len(calls) for calls in m.requests.values()) == 1


@pytest.mark.tier0
def test_succeeds_on_retry_after_transient_failure():
    profile = _profile(retry_count=2)
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=502)
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=200, payload={"content": "<p>ok</p>"})
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", profile))
    assert result.success is True
    assert sum(len(calls) for calls in m.requests.values()) == 2


@pytest.mark.tier0
def test_credential_rejection_is_not_retried():
    # R1: bad credentials won't fix themselves on retry — this should
    # return immediately on the first 401, not burn through retry_count.
    profile = _profile(retry_count=3)
    with aioresponses_strict() as m:
        m.post(f"{PLAYWRIGHT_URL}/scrape", status=401)
        result = asyncio.run(browse_fetch(_fake_request(), "https://example.com", profile))
    assert result.success is False
    assert sum(len(calls) for calls in m.requests.values()) == 1
