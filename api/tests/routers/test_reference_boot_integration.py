"""Smoke test for the shared real-boot fixture (T-009).

This is NOT one of the Tier-2 requirement proofs — T-010…T-015 own those. It
exists purely to prove `reference_booted_client` itself works end to end before
six other tasks build on it: the real app boots with the REAL reference mod
loaded, its route serves through its HTTP path, its scope is seeded, and the
fixture can boot repeatedly within one session (the process-global `sio`
namespace and router cleanup actually work).

Cavekit: cavekit-mods-reference-implementation.md — T-009 (fixture vehicle).
"""

import pytest

# `reference_booted_client` is provided by the tests.mods_reference_boot plugin
# (registered in tests/conftest.py), so it needs no import here. Only the module
# constants and the grant helper are imported.
from tests.mods_reference_boot import REFERENCE_MOD_ID, grant_reference_scope_for_everyone


@pytest.mark.tier1
def test_the_real_reference_mod_loads_at_boot(reference_booted_client):
    """The whole point of the fixture: the real app, at boot, loaded the REAL
    reference mod from its real repo location."""
    loaded = reference_booted_client.app.state.MODS
    assert loaded is not None, "app.state.MODS must be published by boot"
    assert REFERENCE_MOD_ID in loaded.loaded, f"the reference mod did not load; errors: {loaded.errors}"


@pytest.mark.tier1
def test_the_reference_route_serves_for_an_authenticated_user(reference_booted_client):
    """R1's read surface, reachable through the fixture: `/reference/state`
    requires a verified user (`get_verified_user`) and returns the state
    snapshot. An anonymous request is refused; the admin gets a 200 with the
    snapshot shape the tool writes into."""
    boot = reference_booted_client

    anon = boot.client.get("/reference/state")
    assert anon.status_code in (401, 403), f"the route must require auth (get_verified_user); got {anon.status_code}"

    resp = boot.as_admin().get("/reference/state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"latest", "count"}, body
    assert body["latest"] is None and body["count"] == 0, "state resets clean at boot"


@pytest.mark.tier1
def test_the_reference_scope_is_seeded_deny_by_default(reference_booted_client):
    """R3's precondition, visible through the fixture: enabling the mod seeds its
    one scope deny-by-default in the instance permission defaults."""
    defaults = reference_booted_client.app.state.config.USER_PERMISSIONS
    assert (
        defaults.get("mods", {}).get(REFERENCE_MOD_ID, {}).get("use") is False
    ), "the reference mod's scope must be seeded deny-by-default at boot"


@pytest.mark.tier1
def test_the_registry_reports_the_reference_mod_for_an_admin(reference_booted_client):
    """The registry producer ran and reflects the loaded mod, read through the
    endpoint (not by inspecting boot output)."""
    resp = reference_booted_client.as_admin().get("/api/v1/mods/enabled")
    assert resp.status_code == 200, resp.text
    assert REFERENCE_MOD_ID in {e["id"] for e in resp.json()}


@pytest.mark.tier1
def test_the_scope_grant_helper_round_trips(reference_booted_client):
    """The `grant_reference_scope_for_everyone` helper works through the real
    admin permissions round trip — so T-012/T-016 can rely on it rather than
    re-deriving the GET -> POST -> GET shape.

    `USER_PERMISSIONS` is a `PersistentConfig` persisted to the DB, and boot's
    `seed_defaults` never overwrites an existing value — so a grant made here
    would otherwise survive into whichever test runs next and flip its
    deny-by-default precondition to True. Snapshot the defaults right after
    boot and restore them on teardown through the same admin endpoint, the
    same pattern `scoped_boot` in `test_reference_state_chain.py` uses.
    """
    admin = reference_booted_client.as_admin()
    original = admin.get("/api/v1/users/default/permissions")
    assert original.status_code == 200, original.text
    snapshot = original.json()
    try:
        result = grant_reference_scope_for_everyone(admin)
        assert result["mods"][REFERENCE_MOD_ID]["use"] is True
    finally:
        admin.post("/api/v1/users/default/permissions", json=snapshot)


@pytest.mark.tier1
def test_the_fixture_boots_cleanly_a_second_time(reference_booted_client):
    """A second test requesting the fixture in the same session must boot without
    hitting the loader's 'namespace already registered' guard — i.e. the teardown
    cleanup of the shared `sio` namespace actually works. If this and
    `test_the_real_reference_mod_loads_at_boot` both pass in one run, the cleanup
    is proven."""
    loaded = reference_booted_client.app.state.MODS
    assert REFERENCE_MOD_ID in loaded.loaded, f"second boot failed to load the mod; errors: {loaded.errors}"
