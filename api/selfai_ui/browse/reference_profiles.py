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
    # Playwright's own page.goto navigation timeout defaults to 15s
    # (selftools/playwright, not configurable from here — browse_fetch's
    # request body doesn't pass a `timeout` override). A client-side
    # timeout shorter than that always wins the race on a slow page,
    # discarding Playwright's real result (success or a specific error)
    # in favor of a bare, uninformative asyncio.TimeoutError.
    timeout_seconds=20.0,
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
    # Same reasoning as GENERAL_SEARCH_PROFILE — must clear Playwright's own
    # ~15s page-load timeout, or the client preempts it every time.
    timeout_seconds=20.0,
    retry_count=1,
    extra={"location_based": True},
)


DIRECT_FETCH_PROFILE = BrowseProfile(
    name="direct-fetch",
    # No allowlist (cavekit-browse-web-access.md R1). The URL came from the
    # user, not from fetched page content. Restricting *which* pages a user may
    # ask to read is cavekit-browse-access-control.md's job — a per-user gate,
    # not a per-URL one. What a fetched page may in turn point at is a separate
    # and much tighter policy (R6), and it does not live here either.
    allowlist=[],
    blocklist=[],
    # More patient than general-search: this is one page a human deliberately
    # asked for, not one of N results where a slow site can simply be dropped.
    # Still clears Playwright's own ~15s page-load default with margin.
    timeout_seconds=25.0,
    # One retry, for the same reason — there is no other result to fall back on,
    # so a single transient failure should not end the read.
    retry_count=1,
)


LINK_FOLLOW_PROFILE = BrowseProfile(
    name="link-follow",
    # No allowlist here either — but for a different reason than direct-fetch,
    # and it must not be read as "anything goes". Which URLs a traversal may
    # follow is decided by the hop policy (cavekit-browse-web-access.md R6,
    # browse/hop_policy.py) against the page a link was found on, which a
    # static list in a profile cannot express: the answer depends on where the
    # link came from, not on the destination alone.
    allowlist=[],
    blocklist=[],
    # Fast-fail, like general-search and unlike direct-fetch: there are other
    # pages in the traversal, so a slow one should be dropped rather than
    # spending the shared wall-clock budget on it.
    timeout_seconds=20.0,
    retry_count=0,
)


def register_reference_profiles() -> None:
    """Register the reference profiles. Idempotent — safe to call more
    than once (re-registering just overwrites with the same values)."""
    register_profile(GENERAL_SEARCH_PROFILE)
    register_profile(WEATHER_SEARCH_PROFILE)
    register_profile(DIRECT_FETCH_PROFILE)
    register_profile(LINK_FOLLOW_PROFILE)
