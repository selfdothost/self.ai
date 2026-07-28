"""R2 proof: the reference tool's `output_schema` survives REAL assembly and is
carried-but-inert on the wire (T-011).

`cavekit-mods-reference-implementation.md` R2 (criteria 4/5/6) asks that the
async-handle `{task_id, status}` result schema the `submit` tool declares in
`mods/reference/reference_mod.py` (`TOOL_OUTPUT_SCHEMA`) actually reaches a
serialized tool spec through the code path core runs -- not through a hand-built
`ToolSpec`. The property has two halves:

  * **It survives.** The value declared on the mod tool
    (`ModTool.output_schema`) is the value on the assembled `ToolSpec`
    (`ToolSpec.output_schema`), byte-for-byte, after `assemble_mod_tools` reads
    the REAL loaded mod off the booted app.
  * **It is inert on the live wire.** No provider core speaks today defines a
    result-schema field, so the surviving schema appears ONLY in the MCP
    serialization (`outputSchema`, camelCase) and in NEITHER the OpenAI nor the
    Anthropic tool object. A model called through core is offered a tool with no
    trace of the result schema; the declaration is future-proofing, not a live
    payload change.

Verification Convention (the load-bearing constraint of this task): the
`ToolSpec` under test MUST have travelled `ModTool.output_schema` ->
`ToolSpec.output_schema` via `assemble_mod_tools` reading
`boot.app.state.MODS.loaded["reference"]` -- the REAL loaded mod. A hand-built
`ToolSpec` proves only that the serializer works in isolation (already proven in
the ToolSpec kit); it proves nothing about THIS mod's declaration. So every
assertion below reads the spec that came out of the real assembly path.

Why `assemble_mod_tools` directly rather than the full chat-completion path:
`assemble_mod_tools` IS the real, sole producer of a mod tool's `ToolSpec` --
the chat path reaches it through `assemble_for_user` and then only renames specs
for collisions (`resolve_collisions`), never touching `output_schema`. Driving a
full `generate_chat_completion_with_tools` call would require standing up a model
selection, a mocked generation, and a request context well beyond what T-009's
boot fixture provides, and would exercise no additional code between the mod tool
and its serialized schema. Assembling against the real `LoadedMod` is the real
assembly path for this property, so it is the one proven here.

Cavekit: cavekit-mods-reference-implementation.md R2 -- T-011.
"""

from types import SimpleNamespace

import pytest

from selfai_ui.mods.tools import assemble_mod_tools
from selfai_ui.utils.access_control import has_permission

# `reference_booted_client` is provided by the tests.mods_reference_boot plugin
# (registered in tests/conftest.py), so it needs no import here. Only the module
# constants and the grant helper are imported.
from tests.mods_reference_boot import REFERENCE_MOD_ID, grant_reference_scope_for_everyone

#: The one tool the reference mod contributes.
SUBMIT_TOOL = "submit"


def _set_reference_scope(admin_client, value: bool) -> None:
    """Set the instance-default `mods.reference.use` leaf through the real admin
    permissions endpoint (GET -> mutate -> POST), the same round trip the grant
    helper uses. Used to REVOKE at teardown so the grant does not leak."""
    current = admin_client.get("/api/v1/users/default/permissions")
    assert current.status_code == 200, current.text
    perms = current.json()
    perms.setdefault("mods", {}).setdefault(REFERENCE_MOD_ID, {})["use"] = value
    saved = admin_client.post("/api/v1/users/default/permissions", json=perms)
    assert saved.status_code == 200, saved.text


@pytest.fixture
def assembled_submit_spec(reference_booted_client):
    """Yield the `submit` tool's `ToolSpec` as produced by the REAL per-user
    assembly against the REAL loaded reference mod.

    Grants the mod's scope instance-wide through the real admin round trip (so the
    assembly filter includes the tool), assembles the tool set out of
    `boot.app.state.MODS.loaded["reference"]` -- the genuine `LoadedMod` the app
    booted with, its `.tools` the list `validate_mod_tools` produced at
    registration -- and returns the `submit` spec. Nothing here constructs a
    `ToolSpec` by hand; the spec yielded is the one `assemble_mod_tools` built.

    Teardown REVOKES the grant. `USER_PERMISSIONS` is a `PersistentConfig` written
    to the boot-session DB and reloaded at every boot, so a grant left in place
    would flip the seeded deny-by-default leaf True for every later boot in the
    session and break a sibling suite's precondition check. Setting the leaf back
    to False through the same endpoint restores the pre-test instance default.
    """
    boot = reference_booted_client
    admin = boot.as_admin()

    # 1. Grant `mods.reference.use` instance-wide via the real permissions
    #    endpoint, which writes back to app.state.config.USER_PERMISSIONS -- the
    #    same defaults object the assembly reads below.
    grant_reference_scope_for_everyone(admin)
    defaults = boot.app.state.config.USER_PERMISSIONS
    assert defaults["mods"][REFERENCE_MOD_ID]["use"] is True, defaults

    # 2. Take the REAL loaded mod off the booted app.
    loaded = boot.app.state.MODS.loaded[REFERENCE_MOD_ID]

    # 3. A caller who now holds the granted scope (the grant is instance-wide, so
    #    any id resolves True through the default fallback).
    user = SimpleNamespace(id=boot.admin["id"])
    assert has_permission(user.id, "mods.reference.use", defaults), "the granted user must hold the scope"

    # 4. The REAL assembly path -- ModTool.output_schema -> ToolSpec.output_schema.
    assembled = assemble_mod_tools(
        loaded.tools,
        loaded.manifest,
        user=user,
        defaults=defaults,
        has_permission_fn=has_permission,
    )
    assert SUBMIT_TOOL in assembled, f"the submit tool was not assembled for a scoped user: {list(assembled)}"

    try:
        yield assembled[SUBMIT_TOOL]["spec"]
    finally:
        # Restore deny-by-default so the persisted grant does not leak into later
        # boots in this session (see the fixture docstring).
        _set_reference_scope(admin, False)


@pytest.mark.tier1
def test_output_schema_survives_real_assembly(assembled_submit_spec):
    """R2 criterion 4: the `{task_id, status}` schema declared on the mod tool is
    the schema on the assembled `ToolSpec`, byte-for-byte, after real assembly."""
    from mods.reference.reference_mod import TOOL_OUTPUT_SCHEMA

    spec = assembled_submit_spec

    assert spec.output_schema == TOOL_OUTPUT_SCHEMA, (
        "the tool's declared output_schema did not survive assemble_mod_tools; " f"got {spec.output_schema!r}"
    )
    # Guard against a coincidental match on an empty/None value: the schema is the
    # real async-handle shape, not a default.
    assert spec.output_schema is not None
    assert set(spec.output_schema.get("properties", {})) == {"task_id", "status"}


@pytest.mark.tier1
def test_output_schema_is_carried_on_the_mcp_wire(assembled_submit_spec):
    """R2 criterion 5: the surviving schema appears as `outputSchema` (camelCase)
    in the MCP serialization of the REAL assembled spec."""
    from mods.reference.reference_mod import TOOL_OUTPUT_SCHEMA

    spec = assembled_submit_spec
    mcp = spec.to_mcp()

    assert "outputSchema" in mcp, f"MCP serialization dropped the output schema: {sorted(mcp)}"
    assert mcp["outputSchema"] == TOOL_OUTPUT_SCHEMA
    # A detached copy, not the spec's own dict (the serializer deep-copies).
    assert mcp["outputSchema"] is not spec.output_schema


@pytest.mark.tier1
def test_output_schema_is_inert_on_the_openai_wire(assembled_submit_spec):
    """R2 criterion 6 (OpenAI half): the OpenAI tool object built from the REAL
    assembled spec carries only `name`, `description`, `parameters` -- no
    output-schema key under any spelling."""
    openai = assembled_submit_spec.to_openai()

    assert set(openai) == {"name", "description", "parameters"}, openai
    # No result-schema field leaked under any casing the model could see.
    assert not any("output" in key.lower() for key in openai), openai


@pytest.mark.tier1
def test_output_schema_is_inert_on_the_anthropic_wire(assembled_submit_spec):
    """R2 criterion 6 (Anthropic half): the Anthropic tool object built from the
    REAL assembled spec carries only `name`, `description`, `input_schema` -- no
    output-schema key under any spelling."""
    anthropic = assembled_submit_spec.to_anthropic()

    assert set(anthropic) == {"name", "description", "input_schema"}, anthropic
    assert not any("output" in key.lower() for key in anthropic), anthropic
