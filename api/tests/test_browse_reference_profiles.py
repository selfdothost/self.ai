"""cavekit-browse-profiles.md R4 — reference profiles exist and enforce
distinct allow/blocklists."""

import pytest

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.profiles import resolve_profile
from selfai_ui.browse.reference_profiles import (
    GENERAL_SEARCH_PROFILE,
    WEATHER_SEARCH_PROFILE,
    register_reference_profiles,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    profiles_module._PROFILE_REGISTRY.clear()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


@pytest.mark.tier0
def test_general_search_profile_resolves():
    register_reference_profiles()
    assert resolve_profile("general-search") is GENERAL_SEARCH_PROFILE


@pytest.mark.tier0
def test_weather_search_profile_resolves():
    register_reference_profiles()
    assert resolve_profile("weather-search") is WEATHER_SEARCH_PROFILE


@pytest.mark.tier0
def test_profiles_enforce_distinct_allowlists():
    # weather-search has a real, tight allowlist; general-search doesn't
    # restrict at all — the same hostname must be treated differently.
    register_reference_profiles()
    general = resolve_profile("general-search")
    weather = resolve_profile("weather-search")

    unrelated_host = "random-blog.example"
    assert unrelated_host not in weather.allowlist
    assert general.allowlist == []  # broad search: nothing excluded by allowlist
    assert weather.allowlist != general.allowlist
    assert "weather.com" in weather.allowlist
