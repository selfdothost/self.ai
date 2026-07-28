"""Admin config surface for Web Crawl (slice 2).

`ENABLE_WEB_CRAWL` and its crawl budget are exposed through the same retrieval
config endpoint the Web Search / Deep Research controls use, so an admin can
turn model-driven crawling on without a redeploy.

Writes are guarded (a client that omits a field must not reset it) and the
budget is clamped in code — a crawl writes persisted pages into a knowledge
base, so a bad value costs storage and Firecrawl load, not just a slow request.
"""

from types import SimpleNamespace

import pytest

from selfai_ui.routers.retrieval import WebSearchConfig


def _config(**over):
    base = dict(
        ENABLE_WEB_CRAWL=False,
        WEB_CRAWL_MAX_PAGES=25,
        WEB_CRAWL_MAX_DEPTH=2,
        ENABLE_DEEP_RESEARCH=False,
        DEEP_RESEARCH_MAX_PAGES=10,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _apply(config, **fields):
    """Mirror the guarded/clamped assignment the update endpoint performs."""
    search = WebSearchConfig(enabled=True, **fields)
    if search.web_crawl_enabled is not None:
        config.ENABLE_WEB_CRAWL = search.web_crawl_enabled
    if search.web_crawl_max_pages is not None:
        config.WEB_CRAWL_MAX_PAGES = max(1, min(500, search.web_crawl_max_pages))
    if search.web_crawl_max_depth is not None:
        config.WEB_CRAWL_MAX_DEPTH = max(1, min(10, search.web_crawl_max_depth))
    return config


# --- the form model -------------------------------------------------------


@pytest.mark.tier0
def test_the_fields_are_optional_so_an_older_client_can_omit_them():
    """A client that predates these fields must not blank them out."""
    search = WebSearchConfig(enabled=True)
    assert search.web_crawl_enabled is None
    assert search.web_crawl_max_pages is None
    assert search.web_crawl_max_depth is None


@pytest.mark.tier0
def test_omitting_the_fields_leaves_stored_values_untouched():
    config = _config(ENABLE_WEB_CRAWL=True, WEB_CRAWL_MAX_PAGES=40, WEB_CRAWL_MAX_DEPTH=3)
    _apply(config)  # nothing sent
    assert config.ENABLE_WEB_CRAWL is True
    assert config.WEB_CRAWL_MAX_PAGES == 40
    assert config.WEB_CRAWL_MAX_DEPTH == 3


# --- enable/disable -------------------------------------------------------


@pytest.mark.tier0
def test_enabling_and_disabling_round_trips():
    config = _config()
    _apply(config, web_crawl_enabled=True)
    assert config.ENABLE_WEB_CRAWL is True
    _apply(config, web_crawl_enabled=False)
    assert config.ENABLE_WEB_CRAWL is False


# --- budget clamps --------------------------------------------------------


@pytest.mark.tier0
@pytest.mark.parametrize(
    "sent,expected",
    [(1, 1), (25, 25), (500, 500), (100000, 500), (0, 1), (-5, 1)],
)
def test_max_pages_is_clamped_to_1_500(sent, expected):
    config = _config()
    _apply(config, web_crawl_max_pages=sent)
    assert config.WEB_CRAWL_MAX_PAGES == expected


@pytest.mark.tier0
@pytest.mark.parametrize(
    "sent,expected",
    [(1, 1), (2, 2), (10, 10), (99, 10), (0, 1), (-1, 1)],
)
def test_max_depth_is_clamped_to_1_10(sent, expected):
    config = _config()
    _apply(config, web_crawl_max_depth=sent)
    assert config.WEB_CRAWL_MAX_DEPTH == expected


@pytest.mark.tier0
def test_web_crawl_settings_do_not_disturb_deep_research():
    """The two live in the same config block but are independent capabilities."""
    config = _config(ENABLE_DEEP_RESEARCH=True, DEEP_RESEARCH_MAX_PAGES=10)
    _apply(config, web_crawl_enabled=True, web_crawl_max_pages=100)
    assert config.ENABLE_DEEP_RESEARCH is True
    assert config.DEEP_RESEARCH_MAX_PAGES == 10
