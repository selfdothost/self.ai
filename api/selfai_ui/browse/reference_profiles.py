"""Reference access profiles (cavekit-browse-profiles.md R4).

Two profiles proving the registration/resolution mechanism works:
GENERAL_SEARCH_PROFILE (broad, arbitrary in-chat search — this is what
cavekit-browse-search-migration.md's web_search tool names) and
WEATHER_SEARCH_PROFILE (a narrower, location/topic-specific example with a
tighter allowlist). These two are illustrative, not the point — the point
is that the mechanism (define, register, resolve by name, enforce
different allow/blocklists per profile) works. Other features are free to
define and register their own profiles the same way.
"""

from selfai_ui.browse.profiles import BrowseProfile, register_profile

GENERAL_SEARCH_PROFILE = BrowseProfile(
    name="general-search",
    allowlist=[],  # broad/arbitrary search — no allowlist restriction
    blocklist=[],
    timeout_seconds=10.0,
    retry_count=0,
)

WEATHER_SEARCH_PROFILE = BrowseProfile(
    name="weather-search",
    allowlist=[
        "weather.com",
        "wunderground.com",
        "noaa.gov",
        "accuweather.com",
    ],
    blocklist=[],
    timeout_seconds=8.0,
    retry_count=1,
    extra={"location_based": True},
)


def register_reference_profiles() -> None:
    """Register both reference profiles. Idempotent — safe to call more
    than once (re-registering just overwrites with the same values)."""
    register_profile(GENERAL_SEARCH_PROFILE)
    register_profile(WEATHER_SEARCH_PROFILE)
