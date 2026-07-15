"""cavekit-browse-profiles.md R1 (profile definition shape), R2 (startup
registration — not a runtime/admin API, not per-request), R3 (resolution
by name), and R5 (fail loudly on malformed registration)."""

import pytest
from pydantic import ValidationError

from selfai_ui.browse import profiles as profiles_module
from selfai_ui.browse.profiles import (
    BrowseProfile,
    ProfileNotFoundError,
    register_profile,
    resolve_profile,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Registration is process-lifetime global state by design (R2) — reset
    it around each test so tests don't leak into each other."""
    profiles_module._PROFILE_REGISTRY.clear()
    yield
    profiles_module._PROFILE_REGISTRY.clear()


@pytest.mark.tier0
def test_profile_carries_allowlist_blocklist_and_tuning():
    profile = BrowseProfile(
        name="general-search",
        allowlist=["example.com"],
        blocklist=["blocked.example.com"],
        timeout_seconds=5.0,
        retry_count=0,
    )
    assert profile.allowlist == ["example.com"]
    assert profile.blocklist == ["blocked.example.com"]
    assert profile.timeout_seconds == 5.0
    assert profile.retry_count == 0


@pytest.mark.tier0
def test_profile_values_retrievable_by_name_field():
    profile = BrowseProfile(name="weather-search", timeout_seconds=3.0, retry_count=1)
    assert profile.name == "weather-search"


@pytest.mark.tier0
def test_profile_may_carry_optional_extra_behavior():
    profile = BrowseProfile(
        name="weather-search",
        timeout_seconds=3.0,
        retry_count=1,
        extra={"location_based": True},
    )
    assert profile.extra == {"location_based": True}


@pytest.mark.tier0
def test_extra_defaults_to_none_when_not_provided():
    profile = BrowseProfile(name="general-search", timeout_seconds=5.0, retry_count=0)
    assert profile.extra is None


# --- R2: startup registration ------------------------------------------


@pytest.mark.tier0
def test_registered_profile_is_stored_by_name():
    profile = BrowseProfile(name="general-search", timeout_seconds=5.0, retry_count=0)
    register_profile(profile)
    assert profiles_module._PROFILE_REGISTRY["general-search"] is profile


@pytest.mark.tier0
def test_registry_persists_across_multiple_lookups():
    # R2 AC1/AC3: the same registered profile is what gets resolved on
    # each call by name — it is not rebuilt per-request.
    profile = BrowseProfile(name="general-search", timeout_seconds=5.0, retry_count=0)
    register_profile(profile)
    first = profiles_module._PROFILE_REGISTRY["general-search"]
    second = profiles_module._PROFILE_REGISTRY["general-search"]
    assert first is second is profile


# --- R3: resolution by name --------------------------------------------


@pytest.mark.tier0
def test_resolve_registered_profile_returns_full_config():
    profile = BrowseProfile(
        name="general-search",
        allowlist=["example.com"],
        timeout_seconds=5.0,
        retry_count=0,
    )
    register_profile(profile)
    resolved = resolve_profile("general-search")
    assert resolved is profile
    assert resolved.allowlist == ["example.com"]


@pytest.mark.tier0
def test_resolve_unregistered_name_fails_clearly():
    with pytest.raises(ProfileNotFoundError):
        resolve_profile("never-registered")


@pytest.mark.tier0
def test_resolve_unregistered_name_does_not_fall_back_to_a_default():
    register_profile(BrowseProfile(name="general-search", timeout_seconds=5.0, retry_count=0))
    # A DIFFERENT, unregistered name must still fail — not silently return
    # the one profile that does happen to be registered.
    with pytest.raises(ProfileNotFoundError):
        resolve_profile("weather-search")


# --- R5: fail loudly on malformed registration --------------------------


@pytest.mark.tier0
def test_profile_missing_required_field_cannot_be_constructed():
    # Required fields (name, timeout_seconds, retry_count) make this raise
    # at construction time — before there is anything to register at all.
    with pytest.raises(ValidationError):
        BrowseProfile(name="broken")  # missing timeout_seconds, retry_count


@pytest.mark.tier0
def test_malformed_profile_never_becomes_resolvable():
    with pytest.raises(ValidationError):
        BrowseProfile(name="broken")
    # It was never constructed, so it was never registered, so resolving
    # its name fails — the instance never comes up "serving" it.
    with pytest.raises(ProfileNotFoundError):
        resolve_profile("broken")


@pytest.mark.tier0
def test_register_profile_rejects_non_profile_values():
    with pytest.raises(TypeError):
        register_profile({"name": "not-a-real-profile"})
    with pytest.raises(ProfileNotFoundError):
        resolve_profile("not-a-real-profile")
