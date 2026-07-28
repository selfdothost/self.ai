"""Regression tests for the admin default-permissions round trip.

`POST /api/v1/users/default/permissions` validates its body against
`UserPermissions` (`selfai_ui/routers/users.py`) and then assigns
`form_data.model_dump()` straight onto `app.state.config.USER_PERMISSIONS`.

That model declares only `workspace{models,knowledge,prompts,tools}` and
`chat{...}`. The configured default (`selfai_ui/config.py`, `USER_PERMISSIONS`)
carries more than that -- `workspace.training` and a whole `features` block.
Anything the model does not declare is absent from `model_dump()`, so the
assignment silently deletes it: an admin who opens the permissions page and
saves it, changing nothing, destroys settings they never touched.

These tests read the live config rather than a hardcoded key list, so they keep
holding as keys are added. `test_roundtrip_preserves_every_key` is the general
statement of the property; the two that follow name the specific keys known to
be lost today so a failure says which one went missing.

Cavekit: cavekit-mods-permissions.md R4 -- T-001
"""

import pytest


def _post_back_unmodified(client, current):
    """GET -> POST the body straight back -> GET. Returns the final state."""
    response = client.post("/api/v1/users/default/permissions", json=current)
    assert response.status_code == 200, response.text
    return client.get("/api/v1/users/default/permissions").json()


@pytest.mark.tier1
def test_roundtrip_preserves_every_key(authenticated_admin, test_app):
    """Saving the permissions form unchanged must not drop any key.

    The general property: whatever the instance is configured with survives a
    no-op save. Written against the live config so it does not need updating
    each time a permission is added.
    """
    before = authenticated_admin.get("/api/v1/users/default/permissions").json()
    after = _post_back_unmodified(authenticated_admin, before)

    def _paths(node, prefix=""):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                yield from _paths(value, path)
            else:
                yield path

    lost = sorted(set(_paths(before)) - set(_paths(after)))
    assert not lost, f"round trip dropped permission keys: {lost}"


@pytest.mark.tier1
def test_roundtrip_preserves_workspace_training(authenticated_admin):
    """`workspace.training` is configured but absent from WorkspacePermissions."""
    before = authenticated_admin.get("/api/v1/users/default/permissions").json()
    if "training" not in before.get("workspace", {}):
        pytest.skip("workspace.training not configured on this instance")

    after = _post_back_unmodified(authenticated_admin, before)

    assert "training" in after["workspace"], "workspace.training was dropped by the round trip"
    assert after["workspace"]["training"] == before["workspace"]["training"]


@pytest.mark.tier1
def test_roundtrip_preserves_features_block(authenticated_admin):
    """The whole `features` block is absent from UserPermissions."""
    before = authenticated_admin.get("/api/v1/users/default/permissions").json()
    if "features" not in before:
        pytest.skip("features block not configured on this instance")

    after = _post_back_unmodified(authenticated_admin, before)

    assert "features" in after, "the entire features block was dropped by the round trip"
    assert after["features"] == before["features"]


@pytest.mark.tier1
def test_roundtrip_preserves_an_unmodelled_namespace(authenticated_admin, test_app):
    """A namespace the model does not declare survives the round trip.

    This is the property mod scopes depend on: they live under `mods.<id>`,
    which `UserPermissions` will never declare. If an unmodelled top-level
    namespace cannot survive a save, no mod scope can either.

    Cross-reference: cavekit-mods-permissions.md R3.
    """
    original = test_app.state.config.USER_PERMISSIONS
    seeded = {**original, "mods": {"example": {"thing": {"read": False}}}}
    test_app.state.config.USER_PERMISSIONS = seeded
    try:
        before = authenticated_admin.get("/api/v1/users/default/permissions").json()
        assert before.get("mods", {}).get("example", {}).get("thing", {}) == {"read": False}

        after = _post_back_unmodified(authenticated_admin, before)

        assert "mods" in after, "an unmodelled namespace was dropped by the round trip"
        assert after["mods"]["example"]["thing"]["read"] is False
    finally:
        test_app.state.config.USER_PERMISSIONS = original
