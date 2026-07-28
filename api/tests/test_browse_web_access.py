"""cavekit-browse-web-access.md R1 (direct page read), R2 (result shape and
budget), R3 (link extraction), R7 (source provenance), and R10 (existing
surfaces unchanged).

web_fetch reads one page the model named, through the core browse connection
under the direct-fetch profile, with no search provider in the path — and it is
a leaf: it never follows a link. Traversal (R4-R6) is deep_research's, and is
not covered here.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.connection import BrowseFetchResult
from selfai_ui.browse.reference_profiles import register_reference_profiles
from selfai_ui.utils.middleware import (
    WEB_FETCH_TOOL_SPEC,
    WEB_SEARCH_TOOL_SPEC,
    html_to_text,
    html_to_text_and_links,
    run_web_fetch_tool_call,
)


@pytest.fixture(autouse=True)
def _profiles_registered():
    profiles_module._PROFILE_REGISTRY.clear()
    register_reference_profiles()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


def _fake_request(max_chars=50000, browsing=True):
    config = SimpleNamespace(
        BROWSE_PLAYWRIGHT_SERVICE_URL="http://fake-playwright:3000",
        BROWSE_PLAYWRIGHT_API_KEY="test-key",
        BROWSE_FETCH_MAX_CHARS=max_chars,
        BROWSE_MAX_LINKS_PER_PAGE=50,
        USER_PERMISSIONS={"features": {"web_browsing": browsing}},
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def _extra_params():
    return {"__event_emitter__": AsyncMock()}


def _admin():
    # role="admin" deliberately: has_browsing_access's admin path is a pure
    # Python shortcut with no DB call, keeping these tier0 tests dependency
    # -free. The gate's own behavior is covered by test_browse_access_control.py.
    return SimpleNamespace(id="u1", role="admin")


def _page(body, title="Example Page"):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _run(request, url, user=None, extra=None):
    return asyncio.run(run_web_fetch_tool_call(request, url, extra or _extra_params(), user or _admin()))


# --- R1: direct page read -------------------------------------------------


@pytest.mark.tier0
def test_web_fetch_reads_the_named_url_through_the_browse_connection():
    request = _fake_request()
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page("<p>Body text</p>"))),
    ) as mock_fetch:
        result = _run(request, "https://example.com/page")

    mock_fetch.assert_called_once()
    assert mock_fetch.call_args.args[1] == "https://example.com/page"
    assert "Body text" in result


@pytest.mark.tier0
def test_web_fetch_names_the_direct_fetch_profile():
    request = _fake_request()
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page("<p>Body</p>"))),
    ) as mock_fetch:
        _run(request, "https://example.com/page")

    assert mock_fetch.call_args.args[2].name == "direct-fetch"


@pytest.mark.tier0
def test_web_fetch_contacts_no_search_provider():
    """R1: a direct read is not a search. The search provider must see nothing."""
    request = _fake_request()
    with (
        patch("selfai_ui.utils.middleware.search_web") as mock_search,
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page("<p>Body</p>"))),
        ),
    ):
        _run(request, "https://example.com/page")

    mock_search.assert_not_called()


@pytest.mark.tier0
def test_web_fetch_does_not_embed_or_retrieve():
    """R2/D3: the page comes back as itself, not as query-relevance excerpts."""
    request = _fake_request()
    with (
        patch("selfai_ui.utils.middleware.save_docs_to_vector_db") as mock_save,
        patch("selfai_ui.utils.middleware.get_sources_from_files") as mock_sources,
        patch(
            "selfai_ui.utils.middleware.browse_fetch",
            new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page("<p>Body</p>"))),
        ),
    ):
        _run(request, "https://example.com/page")

    mock_save.assert_not_called()
    mock_sources.assert_not_called()


@pytest.mark.tier0
def test_web_fetch_is_gated_before_any_fetch():
    request = _fake_request(browsing=False)
    with patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()) as mock_fetch:
        result = _run(request, "https://example.com/page", user=SimpleNamespace(id="u2", role="user"))

    mock_fetch.assert_not_called()
    assert "not permitted" in result


@pytest.mark.tier0
@pytest.mark.parametrize("url", ["", "   ", "ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)"])
def test_web_fetch_refuses_unreadable_urls_without_fetching(url):
    request = _fake_request()
    with patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock()) as mock_fetch:
        result = _run(request, url)

    mock_fetch.assert_not_called()
    assert result  # a stated refusal, never empty
    assert "Cannot read" in result or "No URL" in result


@pytest.mark.tier0
def test_web_fetch_assumes_https_for_a_bare_host():
    request = _fake_request()
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page("<p>Body</p>"))),
    ) as mock_fetch:
        _run(request, "example.com/page")

    assert mock_fetch.call_args.args[1] == "https://example.com/page"


# --- R2: result shape and budget -----------------------------------------


@pytest.mark.tier0
def test_web_fetch_truncates_at_the_configured_budget_and_says_so():
    request = _fake_request(max_chars=100)
    long_body = "<p>" + ("x" * 5000) + "</p>"
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page(long_body))),
    ):
        result = _run(request, "https://example.com/long")

    assert "Truncated" in result
    assert "100 characters" in result
    assert len(result) < 1000


@pytest.mark.tier0
def test_web_fetch_does_not_claim_truncation_when_complete():
    request = _fake_request(max_chars=50000)
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page("<p>Short body</p>"))),
    ):
        result = _run(request, "https://example.com/short")

    assert "Truncated" not in result


@pytest.mark.tier0
def test_web_fetch_reports_a_failed_fetch_rather_than_returning_nothing():
    request = _fake_request()
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=False, error="Refused: blocked host")),
    ):
        result = _run(request, "https://example.com/nope")

    assert "Could not read" in result
    assert "blocked host" in result


@pytest.mark.tier0
def test_web_fetch_treats_an_empty_page_as_failure_not_empty_success():
    request = _fake_request()
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=True, content="<html><body></body></html>")),
    ):
        result = _run(request, "https://example.com/blank")

    assert "no readable text" in result


@pytest.mark.tier0
def test_web_fetch_never_raises_on_an_unexpected_error():
    request = _fake_request()
    with patch("selfai_ui.utils.middleware.browse_fetch", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = _run(request, "https://example.com/page")

    assert "Could not read" in result
    assert "boom" in result


# --- R7: source provenance ------------------------------------------------


@pytest.mark.tier0
def test_web_fetch_attributes_content_to_its_origin():
    request = _fake_request()
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=True, content=_page("<p>Body</p>", title="A Title"))),
    ):
        result = _run(request, "https://example.com/page")

    assert 'url="https://example.com/page"' in result
    assert "A Title" in result
    assert result.rstrip().endswith("</source>")


# --- R8: status -----------------------------------------------------------


@pytest.mark.tier0
def test_web_fetch_emits_its_own_action_and_always_terminates_it():
    request = _fake_request()
    extra = _extra_params()
    with patch(
        "selfai_ui.utils.middleware.browse_fetch",
        new=AsyncMock(return_value=BrowseFetchResult(success=False, error="nope")),
    ):
        _run(request, "https://example.com/page", extra=extra)

    actions = [c.args[0]["data"] for c in extra["__event_emitter__"].call_args_list]
    assert actions, "web_fetch emitted no status at all"
    assert all(a["action"] == "web_fetch" for a in actions)
    assert actions[0]["done"] is False
    assert actions[-1]["done"] is True
    assert actions[-1]["error"] is True


# --- R3: link extraction --------------------------------------------------

_LINK_PAGE = """<html><head><title>T</title>
<script>var evil = '<a href="https://attacker.example/steal">click</a>';</script></head>
<body>
<a href="/about">About</a>
<a href="https://other.example/x?a=1#frag">Other</a>
<a href="#top">Top</a>
<a href="javascript:alert(1)">JS</a>
<a href="mailto:a@b.c">Mail</a>
<a href="/about">About again</a>
<a href="/about#section">About section</a>
<a href="//cdn.example.net/lib">CDN</a>
<a href="">empty</a>
<a/>
<p>Tail</p>
</body></html>"""

_BASE = "https://site.example/docs/page.html"


@pytest.mark.tier0
def test_links_are_resolved_to_absolute_urls():
    _, _, links = html_to_text_and_links(_LINK_PAGE, base_url=_BASE)
    urls = [u for u, _ in links]
    assert "https://site.example/about" in urls
    assert "https://cdn.example.net/lib" in urls  # protocol-relative


@pytest.mark.tier0
def test_link_text_accompanies_each_url():
    _, _, links = html_to_text_and_links(_LINK_PAGE, base_url=_BASE)
    assert ("https://site.example/about", "About") in links


@pytest.mark.tier0
@pytest.mark.parametrize("bad", ["#top", "javascript:", "mailto:", "data:"])
def test_non_navigational_targets_are_excluded(bad):
    _, _, links = html_to_text_and_links(_LINK_PAGE, base_url=_BASE)
    assert not any(bad in u for u, _ in links)


@pytest.mark.tier0
def test_duplicate_destinations_collapse_to_one():
    _, _, links = html_to_text_and_links(_LINK_PAGE, base_url=_BASE)
    urls = [u for u, _ in links]
    # /about, /about again, and /about#section are one destination.
    assert urls.count("https://site.example/about") == 1


@pytest.mark.tier0
def test_link_cap_is_applied_in_document_order():
    _, _, links = html_to_text_and_links(_LINK_PAGE, base_url=_BASE, max_links=2)
    assert len(links) == 2
    assert links[0][0] == "https://site.example/about"
    assert links[1][0] == "https://other.example/x?a=1"


@pytest.mark.tier0
def test_links_inside_script_are_never_extracted():
    """R3 + D6: markup inside <script> is not document content. A link
    smuggled there must not become a fetchable destination."""
    _, _, links = html_to_text_and_links(_LINK_PAGE, base_url=_BASE)
    assert not any("attacker.example" in u for u, _ in links)


@pytest.mark.tier0
def test_relative_links_are_dropped_without_a_base_url():
    """A relative href has no meaning we can honestly guess at."""
    _, _, links = html_to_text_and_links(_LINK_PAGE)
    urls = [u for u, _ in links]
    assert "https://other.example/x?a=1" in urls
    assert not any(u.endswith("/about") for u in urls)


@pytest.mark.tier0
def test_self_closing_anchor_does_not_capture_following_text():
    _, _, links = html_to_text_and_links(_LINK_PAGE, base_url=_BASE)
    assert all("Tail" not in text for _, text in links)


@pytest.mark.tier0
def test_extraction_never_raises_on_malformed_html():
    text, _title, links = html_to_text_and_links("<p>unclosed <a href='/x'>link", base_url="https://s.example/")
    assert "unclosed" in text
    assert ("https://s.example/x", "link") in links


# --- R10: existing surfaces unchanged -------------------------------------


@pytest.mark.tier0
def test_html_to_text_keeps_its_two_value_contract():
    """R10: web_search's extraction call site is unchanged by R3."""
    text, title = html_to_text(_page("<p>Body</p>", title="A Title"))
    assert "Body" in text
    assert title == "A Title"


@pytest.mark.tier0
def test_web_search_tool_spec_is_untouched():
    assert WEB_SEARCH_TOOL_SPEC.name == "web_search"
    assert WEB_SEARCH_TOOL_SPEC.input_schema["required"] == ["query"]


@pytest.mark.tier0
def test_web_fetch_tool_spec_takes_a_url():
    assert WEB_FETCH_TOOL_SPEC.name == "web_fetch"
    assert WEB_FETCH_TOOL_SPEC.input_schema["required"] == ["url"]


@pytest.mark.tier0
def test_web_fetch_tool_spec_serializes_to_the_openai_wire_shape():
    """The tool-calling path sends .to_openai(); the argument schema must
    arrive under `parameters`, not the canonical `input_schema`."""
    wire = WEB_FETCH_TOOL_SPEC.to_openai()
    assert wire["name"] == "web_fetch"
    assert wire["parameters"]["required"] == ["url"]


@pytest.mark.tier0
def test_direct_fetch_profile_is_registered_alongside_the_existing_ones():
    """R10: adding a profile does not displace the ones web_search names."""
    assert profiles_module.resolve_profile("direct-fetch").name == "direct-fetch"
    assert profiles_module.resolve_profile("general-search").name == "general-search"
    assert profiles_module.resolve_profile("weather-search").name == "weather-search"
