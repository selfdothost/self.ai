"""Access profile definitions for the core browse (Playwright) connection
(cavekit-browse-profiles.md).

A profile is a named, code-defined bundle that parameterizes a call to the
core connection: an allowlist and/or blocklist plus tuning (timeout, retry
count), with room for profile-specific extras (e.g. a location/topic-search
profile's built-in behavior). This module covers only the profile shape
itself (R1). Startup registration (R2) and call-time resolution by name
(R3) are separate concerns layered on top of this.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


class BrowseProfile(BaseModel):
    """R1: a named bundle carrying an allowlist and/or blocklist, tuning
    parameters, and optional profile-specific extras — all retrievable via
    this instance's fields once you have it (by its `name`)."""

    name: str
    allowlist: list[str] = Field(default_factory=list)
    blocklist: list[str] = Field(default_factory=list)
    timeout_seconds: float
    retry_count: int
    extra: Optional[dict[str, Any]] = None


# R2: process-lifetime registry. Populated only via register_profile(),
# called once at startup by the feature that owns a given profile — never
# per-request, and never through a runtime/admin API or HTTP endpoint.
_PROFILE_REGISTRY: dict[str, BrowseProfile] = {}


def register_profile(profile: BrowseProfile) -> None:
    """R2: register `profile` so it is resolvable by name for the lifetime
    of this running instance. Intended to be called at startup, not
    per-request — there is deliberately no HTTP-reachable equivalent of
    this function.

    R5: a profile missing a required field can never reach this point in
    the first place — BrowseProfile's required fields (name, timeout_seconds,
    retry_count) make pydantic raise ValidationError at construction time,
    before there is anything to register. The isinstance check below closes
    the remaining gap: something that was never a valid BrowseProfile at
    all (wrong type entirely) still fails loudly here, rather than being
    silently accepted into the registry.
    """
    if not isinstance(profile, BrowseProfile):
        raise TypeError(f"register_profile() requires a BrowseProfile instance, got {type(profile).__name__}")
    _PROFILE_REGISTRY[profile.name] = profile


class ProfileNotFoundError(KeyError):
    """R3: raised when resolving a name that was never registered — a
    distinct type (not a bare KeyError) so callers can catch it
    specifically rather than accidentally swallowing an unrelated
    KeyError."""


def resolve_profile(name: str) -> BrowseProfile:
    """R3: resolve a profile by name, returning its full configuration.
    Raises ProfileNotFoundError for an unregistered name — never silently
    falls back to some default profile."""
    try:
        return _PROFILE_REGISTRY[name]
    except KeyError:
        raise ProfileNotFoundError(f"No browse profile registered under name {name!r}") from None
