"""R3: scope enforcement, end to end, against the REAL installed reference mod.

The scope-guard mechanism itself is already proven for a SYNTHETIC fixture tool
in `tests/test_mods_tools.py` (a `_mod_tool()` built by hand). This file proves
the SAME property is REACHABLE for the real, installed `reference` mod: the tool
core actually loaded at boot (`boot.app.state.MODS.loaded["reference"]`), through
the real discovery/boot path, with the mod's real declared scope
(`mods.reference.use`). That is the Verification Convention distinction — a
fixture proving a mechanism works is not the same as the real mod proving the
mechanism is reachable through the real install.

Two properties this file pins that a synthetic test cannot:

  * The refusal is exercised for the real tool's real `_scope_guard` — built the
    way `assemble_mod_tools` builds it internally (`tools.py:213`), but for an
    UNSCOPED user, so the call-site check is proven authoritative independent of
    the offer-time filter (criterion 3).
  * The allow is granted through the REAL admin permissions round trip
    (`cavekit-mods-permissions.md` R3: GET -> mutate -> POST -> GET), not by
    mutating stored state directly (criterion 6).

Both allow and refuse are invoked the way core's dispatch invokes a mod tool --
`callable(**filtered_args)` with only the model's arguments, never passing the
acting user or the permission defaults in at call time (the F-003 shape
`_scope_guard` was built to survive, `tools.py:148-183`).

Cavekit: cavekit-mods-reference-implementation.md R3 -- T-012
"""

import asyncio
import types

import pytest

from selfai_ui.models.groups import GroupForm, Groups, GroupUpdateForm
from selfai_ui.mods.tools import _scope_guard, assemble_mod_tools
from tests.conftest import _create_test_user

# `reference_booted_client` is provided by the tests.mods_reference_boot plugin
# (registered in tests/conftest.py), so it needs no import here -- importing it
# would collide with the parameter name and trip ruff F811. Only the constant and
# the round-trip grant helper are imported.
from tests.mods_reference_boot import REFERENCE_MOD_ID, grant_reference_scope_for_everyone

#: The real reference mod's one declared scope (`mod.yaml` /
#: `reference_mod.py:TOOL_SCOPE`) and the name of its one tool
#: (`reference_mod.py:register_tools`). Stated here so the assertions read against
#: the real artifact's own contract, not a value re-derived per test.
REFERENCE_SCOPE = "mods.reference.use"
SUBMIT_TOOL = "submit"


def _as_user(user: dict):
    """A user object shaped the way `_scope_guard`/`assemble_mod_tools` read one:
    they take identity via `getattr(user, "id", None)`, so only `.id` matters."""
    return types.SimpleNamespace(id=user["id"])


def _group_granting(owner_id: str, member_id: str, permissions: dict, name: str) -> object:
    """A group carrying `permissions`, with `member_id` in it -- the group-grant
    path (`tests/routers/test_mods_permissions_closeout.py::_group_granting`).

    Used to make ONE specific user hold the scope while another stays unscoped,
    so the deny path and the allow path can be contrasted within one boot. This
    is a per-user grant; the instance-default grant for criterion 6 goes through
    the admin round trip instead (`grant_reference_scope_for_everyone`).
    """
    group = Groups.insert_new_group(owner_id, GroupForm(name=name, description="test"))
    assert group is not None
    return Groups.update_group_by_id(
        group.id,
        GroupUpdateForm(
            name=group.name,
            description=group.description,
            permissions=permissions,
            user_ids=[member_id],
        ),
    )


def _loaded_reference(boot):
    """The REAL `LoadedMod` core published at boot -- not a synthetic fixture.

    `.tools` is the loader's `validate_mod_tools(register_tools())` output (a list
    of the real `ModTool`), `.manifest.id` is `reference`. This is the artifact
    every assertion below runs against, per the Verification Convention.
    """
    mods = boot.app.state.MODS
    assert mods is not None, "boot did not publish app.state.MODS"
    assert REFERENCE_MOD_ID in mods.loaded, f"reference mod did not load; errors: {mods.errors}"
    loaded = mods.loaded[REFERENCE_MOD_ID]
    # The one real tool, so the deny/allow proofs run against exactly what the
    # loader validated at registration -- not a hand-built ModTool.
    assert [t.name for t in loaded.tools] == [SUBMIT_TOOL], [t.name for t in loaded.tools]
    (tool,) = loaded.tools
    assert tool.scope == REFERENCE_SCOPE, tool.scope
    return loaded


# --- criterion 1: the scope is seeded deny-by-default, read through the surface -
@pytest.mark.tier1
def test_the_scope_is_seeded_deny_by_default_read_through_the_endpoint(reference_booted_client):
    """After the mod is enabled at boot, its declared scope is present in the
    instance default permissions and set to False -- read through the admin
    permissions endpoint (the caller-facing surface), not by inspecting
    `USER_PERMISSIONS` as the primary assertion."""
    boot = reference_booted_client

    resp = boot.as_admin().get("/api/v1/users/default/permissions")
    assert resp.status_code == 200, resp.text
    perms = resp.json()
    assert (
        perms.get("mods", {}).get(REFERENCE_MOD_ID, {}).get("use") is False
    ), "the reference scope must be seeded deny-by-default, visible through the endpoint"

    # Cross-check: the live in-memory defaults the guard/assembly read agree with
    # what the endpoint served.
    live = boot.app.state.config.USER_PERMISSIONS
    assert live["mods"][REFERENCE_MOD_ID]["use"] is False


# --- criterion 2 (+ the group-grant allow): per-user discrimination at assembly -
@pytest.mark.tier1
def test_assembly_discriminates_unscoped_absent_scoped_present_and_invocable(reference_booted_client, db_session):
    """A user lacking the scope does not receive the tool in their assembled set;
    a user granted it (via the group-grant path, to that specific user) does --
    and the granted user's assembled callable, invoked the dispatch way, succeeds.

    Proving both within one boot shows the assembly filter is a genuine per-user
    check against the live defaults, not a uniform "nobody has it" artifact of
    deny-by-default.
    """
    boot = reference_booted_client
    loaded = _loaded_reference(boot)
    defaults = boot.app.state.config.USER_PERMISSIONS

    unscoped = _create_test_user(db_session, role="user")
    scoped = _create_test_user(db_session, role="user")
    _group_granting(
        scoped["id"],
        scoped["id"],
        {"mods": {REFERENCE_MOD_ID: {"use": True}}},
        name="reference-grantees",
    )

    unscoped_tools = assemble_mod_tools(loaded.tools, loaded.manifest, user=_as_user(unscoped), defaults=defaults)
    assert SUBMIT_TOOL not in unscoped_tools, "an unscoped user must not receive the reference tool"

    scoped_tools = assemble_mod_tools(loaded.tools, loaded.manifest, user=_as_user(scoped), defaults=defaults)
    assert SUBMIT_TOOL in scoped_tools, "the group-granted user must receive the reference tool"

    # The group-granted user can invoke it, via the real dispatch shape (only the
    # model's arguments -- submit takes none).
    result = asyncio.run(scoped_tools[SUBMIT_TOOL]["callable"]())
    assert set(result) == {"task_id", "status"}, result
    assert result["status"] == "submitted", result


# --- criterion 3: the call-site guard refuses even when the tool was never offered
@pytest.mark.tier1
def test_the_call_site_guard_refuses_the_unscoped_user_even_though_never_offered(reference_booted_client, db_session):
    """The authoritative check is at the call site, not the offer-time filter.

    Assembly never hands an unscoped user the tool, so the "guessed name invoked
    directly" case cannot be reached through `assemble_mod_tools` for that user.
    To prove the call-site guard nonetheless refuses -- the criterion's "even when
    the tool was not offered" -- build the SAME guard `assemble_mod_tools`
    constructs internally (`tools.py:213`), but for the UNSCOPED user, and invoke
    it. The refusal is a `PermissionError` (a permissions denial, distinguishable
    from a technical failure), raised against the live defaults captured at
    assembly, for the REAL reference tool.
    """
    boot = reference_booted_client
    loaded = _loaded_reference(boot)
    defaults = boot.app.state.config.USER_PERMISSIONS
    (tool,) = loaded.tools

    unscoped = _create_test_user(db_session, role="user")

    # First: confirm the offer-time filter would never hand this user the tool,
    # so the call below is genuinely the "not offered" case.
    assert SUBMIT_TOOL not in assemble_mod_tools(
        loaded.tools, loaded.manifest, user=_as_user(unscoped), defaults=defaults
    ), "precondition: the unscoped user is not offered the tool"

    # Bypass the offer-time filter: construct the guard directly for the unscoped
    # user (identity + live defaults captured now, the way tools.py:213 does).
    guarded = _scope_guard(tool, user=_as_user(unscoped), defaults=defaults)

    # Invoke it the way core's dispatch does -- only the model's arguments (none),
    # no __user__/__request__, no acting user or defaults passed at call time
    # (the F-003 shape). The guard must still refuse.
    with pytest.raises(PermissionError) as excinfo:
        asyncio.run(guarded())
    assert REFERENCE_SCOPE in str(excinfo.value), "the denial must name the required scope"


# --- criteria 4 + 6: admin round-trip grant, then a holder receives and invokes -
@pytest.mark.tier1
def test_after_the_admin_round_trip_grant_a_holder_receives_and_invokes_the_tool(reference_booted_client, db_session):
    """Before any grant the tool is absent and the guard refuses; after the admin
    grants the scope THROUGH THE REAL PERMISSIONS ROUND TRIP (GET -> mutate ->
    POST -> GET, not by mutating stored state directly), a holder receives the
    tool and invokes it successfully -- via the dispatch shape both sides use.

    The grant flips the seeded deny-by-default leaf on the instance defaults, so a
    user in no group now holds the scope by default. This is the criterion-6 grant
    path (the admin round trip), distinct from the group grant used above for the
    per-user contrast.
    """
    boot = reference_booted_client
    loaded = _loaded_reference(boot)
    (tool,) = loaded.tools
    holder = _create_test_user(db_session, role="user")

    # The POST reassigns `config.USER_PERMISSIONS` on the process-global app
    # (`routers/users.py:151`), and seeding never re-denies an existing grant, so
    # the flip would leak into every later test on the shared app. Capture the
    # seeded object and restore it after -- the grant is what this test proves, not
    # a durable instance change.
    original = boot.app.state.config.USER_PERMISSIONS
    try:
        # Before: deny-by-default. The tool is not offered, and the call-site guard
        # refuses when invoked the dispatch way.
        before_defaults = boot.app.state.config.USER_PERMISSIONS
        assert SUBMIT_TOOL not in assemble_mod_tools(
            loaded.tools, loaded.manifest, user=_as_user(holder), defaults=before_defaults
        )
        with pytest.raises(PermissionError):
            asyncio.run(_scope_guard(tool, user=_as_user(holder), defaults=before_defaults)())

        # Grant through the REAL admin permissions round trip -- NOT by mutating
        # config.USER_PERMISSIONS or a group row directly.
        confirmed = grant_reference_scope_for_everyone(boot.as_admin())
        assert confirmed["mods"][REFERENCE_MOD_ID]["use"] is True, "the round trip must flip the leaf to True"

        # After: the live defaults now grant the scope; the tool is offered and the
        # assembled callable, invoked with only the model's arguments, succeeds.
        after_defaults = boot.app.state.config.USER_PERMISSIONS
        assembled = assemble_mod_tools(loaded.tools, loaded.manifest, user=_as_user(holder), defaults=after_defaults)
        assert SUBMIT_TOOL in assembled, "after the grant the holder must receive the tool"

        result = asyncio.run(assembled[SUBMIT_TOOL]["callable"]())
        assert set(result) == {"task_id", "status"}, result
        assert result["status"] == "submitted", result
    finally:
        boot.app.state.config.USER_PERMISSIONS = original
