"""Mod tools: spec shape, scope enforcement at the call site, collisions, handles.

Cavekit: cavekit-mods-surfaces.md R3-R6 -- T-063..T-069, T-072;
cavekit-toolspec-model.md R5 criterion 3 -- T-017 (the last section).
"""

import asyncio
import types
from dataclasses import fields
from functools import partial

import pytest

from selfai_ui.mods.tools import (
    TOOL_NAME_RULE,
    ModTool,
    ModToolError,
    assemble_for_user,
    assemble_mod_tools,
    collide_name,
    is_valid_tool_name,
    resolve_collisions,
    validate_mod_tools,
)
from selfai_ui.utils.toolspec import ToolSpec

SCOPE = "mods.crew.session.connect"


def _manifest(mod_id="crew"):
    return types.SimpleNamespace(id=mod_id)


def _user(uid="u1"):
    return types.SimpleNamespace(id=uid)


def _checker(granted):
    def _has(uid, key, defaults):
        return key in granted

    return _has


def _mod_tool(name="spawn", scope=SCOPE, handler=None, parameters=None, output_schema=None):
    return ModTool(
        name=name,
        description=f"{name} tool",
        handler=handler or (lambda **k: {"ok": name}),
        scope=scope,
        parameters=parameters,
        output_schema=output_schema,
    )


def _assemble(tools, granted=()):
    """Short wrapper around assemble_mod_tools for the common case.

    The guard captures user + defaults at assembly, so `granted` is the set of
    scopes the assembled user holds. `defaults={}` is fine because the checker
    stub keys off `granted`, not the defaults tree."""
    return assemble_mod_tools(
        tools, _manifest(), user=_user(), defaults={}, has_permission_fn=_checker(set(granted))
    )


# --- T-066: registration-time scope validation --------------------------------
@pytest.mark.tier0
def test_a_tool_without_a_scope_is_rejected_at_registration():
    with pytest.raises(ModToolError) as excinfo:
        validate_mod_tools([_mod_tool(scope="")])
    assert "no required scope" in str(excinfo.value)


@pytest.mark.tier0
def test_one_scopeless_tool_rejects_the_whole_set():
    bad = [_mod_tool(name="a", scope="mods.crew.x"), _mod_tool(name="b", scope="")]
    with pytest.raises(ModToolError):
        validate_mod_tools(bad)


@pytest.mark.tier0
@pytest.mark.parametrize(
    "bad_name",
    [
        "Start Session",  # space
        "session:connect",  # colon
        "café_lookup",  # non-ASCII letter, a valid Python identifier but not a valid tool name
        "",  # empty
        "x" * 65,  # one over OpenAI's 64-char bound
    ],
    ids=["space", "colon", "non-ascii", "empty", "too-long"],
)
def test_a_tool_with_an_invalid_name_is_rejected_at_registration(bad_name):
    """Nothing enforced this before ToolSpec existed. A mod could declare a
    tool named however it liked and it would validate today, reach
    `assemble_mod_tools` unexamined, and fail only when a provider rejected it
    at call time -- with an error naming neither the mod nor the tool."""
    with pytest.raises(ModToolError) as excinfo:
        validate_mod_tools([_mod_tool(name=bad_name)])
    assert "not a valid tool name" in str(excinfo.value)
    assert TOOL_NAME_RULE in str(excinfo.value)


@pytest.mark.tier0
@pytest.mark.parametrize("good_name", ["spawn", "look-up", "session_connect", "x" * 64])
def test_a_charset_valid_name_registers_fine(good_name):
    [tool] = validate_mod_tools([_mod_tool(name=good_name)])
    assert tool.name == good_name


@pytest.mark.tier0
def test_a_mod_declaring_no_tools_validates_to_empty():
    assert validate_mod_tools([]) == []


@pytest.mark.tier0
def test_dict_inputs_are_coerced_to_modtool():
    raw = [{"name": "x", "description": "x", "handler": lambda **k: 1, "scope": "mods.crew.x"}]
    [tool] = validate_mod_tools(raw)
    assert isinstance(tool, ModTool) and tool.name == "x"


# --- T-063 / T-064: spec shape and no origin marker ---------------------------
@pytest.mark.tier0
def test_assembled_tools_match_cores_dict_shape():
    tools = validate_mod_tools([_mod_tool()])
    [entry] = _assemble(tools, {SCOPE}).values()
    expected = {"toolkit_id", "callable", "spec", "pydantic_model", "file_handler", "citation"}
    assert set(entry) == expected
    assert entry["toolkit_id"] == "mod:crew"
    assert isinstance(entry["spec"], ToolSpec)
    assert entry["spec"].name == "spawn"
    assert entry["file_handler"] is False and entry["citation"] is False


@pytest.mark.tier0
def test_the_model_sees_no_origin_marker():
    """A mod tool is indistinguishable from a user-authored one in what the model
    receives -- the serialized spec carries only name/description/parameters.

    Asserted on `to_openai()` rather than on the model's own fields: that dict is
    what the wrap site in `middleware.generate_chat_completion_with_tools` puts on
    the wire, so it is the surface the "no origin marker" property is about."""
    tools = validate_mod_tools([_mod_tool()])
    [entry] = _assemble(tools, {SCOPE}).values()
    offered = entry["spec"].to_openai()
    assert set(offered) == {"name", "description", "parameters"}
    assert "mod:" not in str(offered) and "toolkit" not in str(offered).lower()


@pytest.mark.tier0
def test_a_mod_with_no_tools_contributes_nothing():
    assert _assemble([], {SCOPE}) == {}


# --- T-066 / T-067: assembly-time filter and call-site enforcement ------------
@pytest.mark.tier0
def test_a_user_who_lacks_the_scope_sees_no_tool():
    assert _assemble(validate_mod_tools([_mod_tool()]), set()) == {}


@pytest.mark.tier0
def test_the_guard_runs_with_no_injected_params_the_way_dispatch_calls_it():
    """F-003 regression. The tool-calling dispatch invokes a mod callable as
    `callable(**filtered_args)` — only the model's arguments, no `__user__`/
    `__request__`. The guard must still enforce, because it captured the user
    and defaults at assembly. A guard that read `__user__` from kwargs saw None
    and denied EVERY call. Here a holder is called with zero injected params and
    the handler runs."""
    tools = validate_mod_tools([_mod_tool(handler=lambda **k: {"ran": True})])
    guarded = _assemble(tools, {SCOPE})["spawn"]["callable"]
    assert asyncio.run(guarded(city="Paris")) == {"ran": True}


@pytest.mark.tier0
def test_a_tool_assembled_for_a_user_without_the_scope_is_absent_and_uncallable():
    """A user lacking the scope never gets the tool assembled at all -- the
    guessed-name-invoked-directly case can't arise because there is no callable
    to reach. (The tool is absent from the dict; see the sees-no-tool test.)"""
    assert _assemble(validate_mod_tools([_mod_tool()]), set()) == {}


@pytest.mark.tier0
def test_the_guard_denies_when_the_captured_grant_is_false():
    """A tool assembled for a holder, but the captured defaults say the scope is
    False -> the call-site re-check refuses. Proves the guard re-evaluates
    rather than trusting the assembly filter."""
    # Assemble as a holder (so the tool exists), but capture defaults that deny.
    tools = validate_mod_tools([_mod_tool(handler=lambda **k: {"ran": True})])
    denied_defaults = {"mods": {"crew": {"session": {"connect": False}}}}
    assembled = assemble_mod_tools(
        tools, _manifest(), user=_user(), defaults=denied_defaults,
        # checker consults the real defaults tree here, not the granted-set stub
        has_permission_fn=lambda uid, key, defaults: _walk(defaults, key),
    )
    # Assembly filtered it out because the real checker saw False.
    assert assembled == {}


def _walk(defaults, key):
    node = defaults
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return bool(node)


# --- T-072: a tool may return a handle rather than a result -------------------
@pytest.mark.tier0
def test_a_sync_tool_returning_a_handle_reaches_the_model_unchanged():
    """A fast return with a job id is just a return value; nothing blocks."""
    handler = lambda **k: {"task_id": "t1", "status": "submitted"}  # noqa: E731
    tools = validate_mod_tools([_mod_tool(handler=handler)])
    guarded = _assemble(tools, {SCOPE})["spawn"]["callable"]
    assert asyncio.run(guarded()) == {"task_id": "t1", "status": "submitted"}


@pytest.mark.tier0
def test_an_async_handler_is_awaited():
    async def handler(**k):
        return {"ran": True}

    tools = validate_mod_tools([_mod_tool(handler=handler)])
    guarded = _assemble(tools, {SCOPE})["spawn"]["callable"]
    assert asyncio.run(guarded()) == {"ran": True}


# --- T-068 / T-069: deterministic, order-independent collisions ----------------
@pytest.mark.tier0
def test_collide_name_qualifies_with_the_owner():
    assert collide_name("mod:crew", "spawn") == "crew__spawn"


@pytest.mark.tier0
def test_collide_name_strips_the_mod_scheme_prefix_not_just_the_colon():
    """The `mod:` tag is a literal origin marker, not merely an invalid
    character. Stripped rather than sanitized in place -- `mod_crew__spawn`
    would still spell out "mod tool, id crew" just as plainly as
    `mod:crew__spawn` did, only in charset-valid form. A user-authored owner
    (no `mod:` prefix) is qualified with no stripping, so the two paths share
    everything except the strip itself."""
    assert collide_name("mod:crew", "spawn") == "crew__spawn"
    assert collide_name("baseline_toolkit", "spawn") == "baseline_toolkit__spawn"


@pytest.mark.tier0
def test_collide_name_sanitizes_a_charset_the_provider_wire_format_rejects():
    """`owner` is not guaranteed provider-safe on its own -- a user's
    `toolkit_id` is only checked to be a Python identifier
    (`routers/tools.py:80`), and `str.isidentifier()` admits non-ASCII letters
    OpenAI's and Anthropic's `^[a-zA-Z0-9_-]{1,64}$` does not. Before this fix
    `owner` was used unsanitized, so a collision involving such a toolkit was
    invalid at the wire, not merely an origin leak."""
    qualified = collide_name("café_tools", "spawn")
    assert is_valid_tool_name(qualified), qualified
    assert qualified == "caf__tools__spawn"


@pytest.mark.tier0
def test_a_unique_name_is_kept_bare():
    pairs = [("spawn", {"toolkit_id": "mod:crew"})]
    assert list(resolve_collisions(pairs)) == ["spawn"]


@pytest.mark.tier0
def test_every_holder_of_a_shared_name_is_qualified():
    pairs = [
        ("search", {"toolkit_id": "mod:alpha"}),
        ("search", {"toolkit_id": "mod:beta"}),
    ]
    resolved = resolve_collisions(pairs)
    assert set(resolved) == {"alpha__search", "beta__search"}
    assert "search" not in resolved


@pytest.mark.tier0
def test_a_mod_and_a_user_toolkit_collision_looks_structurally_identical():
    """The property `collide_name`'s fix is FOR: a mod's qualified name and a
    user-authored toolkit's qualified name have the same shape -- a bare
    identifier, `__`, the tool name -- with nothing that lets the model (or a
    user reading a transcript) tell which is which just by looking at it."""
    pairs = [
        ("search", {"toolkit_id": "mod:crew"}),
        ("search", {"toolkit_id": "baseline_toolkit"}),
    ]
    resolved = resolve_collisions(pairs)
    assert set(resolved) == {"crew__search", "baseline_toolkit__search"}
    for key in resolved:
        assert is_valid_tool_name(key), key
        assert "mod" not in key.split("__")[0], "the owner half must not tag its kind"


@pytest.mark.tier0
def test_a_collision_is_logged_naming_the_tool_and_every_owner(caplog):
    """cavekit-mods-surfaces.md R5: 'a collision produces a logged record
    naming both colliding tool names and their owners.' Previously
    unimplemented -- qualification ran silently."""
    pairs = [
        ("search", {"toolkit_id": "mod:alpha"}),
        ("search", {"toolkit_id": "mod:beta"}),
    ]
    with caplog.at_level("WARNING", logger="selfai_ui.mods.tools"):
        resolve_collisions(pairs)

    [record] = [r for r in caplog.records if "search" in r.getMessage()]
    assert "mod:alpha" in record.getMessage()
    assert "mod:beta" in record.getMessage()


@pytest.mark.tier0
def test_a_qualifier_level_collision_is_logged_not_silently_dropped(caplog):
    """The residual risk `collide_name` accepts, made observable: stripping
    `mod:` narrows the owner space, so a mod id and an identically-spelled
    user toolkit id now qualify to the same string. `resolve_collisions` does
    not prevent this -- the second write still wins -- but it must not do so
    silently."""
    pairs = [
        ("search", {"toolkit_id": "mod:crew"}),
        ("search", {"toolkit_id": "crew"}),
    ]
    with caplog.at_level("WARNING", logger="selfai_ui.mods.tools"):
        resolved = resolve_collisions(pairs)

    assert len(resolved) == 1, "the qualifier-level collision does overwrite -- this pins that it does"
    assert any("crew__search" in r.getMessage() for r in caplog.records)


@pytest.mark.tier0
def test_the_resulting_set_is_independent_of_order():
    a = [("search", {"toolkit_id": "mod:alpha"}), ("search", {"toolkit_id": "mod:beta"})]
    assert set(resolve_collisions(a)) == set(resolve_collisions(list(reversed(a))))


@pytest.mark.tier0
def test_no_tool_is_dropped_in_a_collision():
    pairs = [
        ("go", {"toolkit_id": "mod:alpha"}),
        ("go", {"toolkit_id": "mod:beta"}),
        ("go", {"toolkit_id": "mod:gamma"}),
    ]
    assert len(resolve_collisions(pairs)) == 3


@pytest.mark.tier0
def test_a_qualified_tools_spec_name_matches_its_key():
    """F-001 regression. The model is offered the spec's name and dispatch looks
    the call up by the dict key. If a collision renames the key but not the spec
    name, the model calls a name that is not a key -> "Unknown tool".

    The fixtures are `ToolSpec`s, deliberately: both live producers build typed
    specs now, and a dict fixture would keep this test green through exactly the
    break it exists to catch (a type-guarded rename that stops firing)."""
    pairs = [
        ("search", {"toolkit_id": "kitA", "spec": ToolSpec(name="search", description="A")}),
        ("search", {"toolkit_id": "kitB", "spec": ToolSpec(name="search", description="B")}),
    ]
    resolved = resolve_collisions(pairs)

    for key, tool in resolved.items():
        assert tool["spec"].name == key, (
            f"the model would be offered {tool['spec'].name!r} but dispatch keys on {key!r}"
        )
    # Every offered name is a dispatchable key — no "Unknown tool" gap.
    offered = {tool["spec"].name for tool in resolved.values()}
    assert offered == set(resolved)
    # Descriptions are not swapped between the holders by the rename.
    assert {tool["spec"].description for tool in resolved.values()} == {"A", "B"}


@pytest.mark.tier0
def test_a_qualified_dict_spec_is_still_renamed():
    """The outer tool dict is untyped, third-party-visible surface, so a dict spec
    stays representable. Renaming it must not be forgotten just because the live
    producers are typed -- a skipped rename is F-001 again."""
    pairs = [
        ("search", {"toolkit_id": "kitA", "spec": {"name": "search", "description": "A", "parameters": {}}}),
        ("search", {"toolkit_id": "kitB", "spec": {"name": "search", "description": "B", "parameters": {}}}),
    ]
    resolved = resolve_collisions(pairs)
    assert {key: tool["spec"]["name"] for key, tool in resolved.items()} == {
        "kitA__search": "kitA__search",
        "kitB__search": "kitB__search",
    }


@pytest.mark.tier0
def test_resolving_a_collision_does_not_mutate_the_shared_spec():
    """The spec derives from the cached tools.specs object off the DB model;
    qualifying a name must copy, not mutate it in place, or it corrupts every
    later request."""
    shared_spec = ToolSpec(name="search", description="A")
    pairs = [
        ("search", {"toolkit_id": "kitA", "spec": shared_spec}),
        ("search", {"toolkit_id": "kitB", "spec": ToolSpec(name="search", description="B")}),
    ]
    resolved = resolve_collisions(pairs)
    assert shared_spec.name == "search", "the original spec must be untouched"
    # The copy is a distinct object, not the shared one rebound.
    assert resolved["kitA__search"]["spec"] is not shared_spec
    assert resolved["kitA__search"]["spec"].input_schema is not shared_spec.input_schema


@pytest.mark.tier0
def test_resolving_a_collision_does_not_copy_the_callable():
    """The collision copy must be shallow, and this is the reason why.

    `apply_extra_params_to_tool_function` (`utils/tools.py:18`) returns a bare
    `functools.partial` for a coroutine tool, whose keywords hold the live
    `__request__`. `deepcopy` of a partial copies its keywords, so deep-copying
    the tool dict would try to deep-copy a FastAPI `Request` -- raising, or
    handing the tool a detached request that no longer refers to the live one.
    The sync path escaped it only because `deepcopy` of a plain function returns
    the same object, which is why a test using a sync callable would not have
    caught this.

    Introduced by the F-001 fix (478e52c), which deep-copied the whole tool dict
    when only the spec needed protecting. Removable once `_spec_renamed_to`
    began returning a fresh spec of its own.
    """
    request_sentinel = object()

    async def tool_body(query: str, __request__=None):
        return query

    live_callable = partial(tool_body, __request__=request_sentinel)

    pairs = [
        ("search", {"toolkit_id": "kitA", "spec": ToolSpec(name="search"), "callable": live_callable}),
        ("search", {"toolkit_id": "kitB", "spec": ToolSpec(name="search"), "callable": live_callable}),
    ]
    resolved = resolve_collisions(pairs)

    for key in ("kitA__search", "kitB__search"):
        carried = resolved[key]["callable"]
        assert carried is live_callable, f"{key}: the callable must be carried by reference, not copied"
        assert carried.keywords["__request__"] is request_sentinel, (
            f"{key}: the live request must survive the rename -- a copied one is a detached object"
        )


@pytest.mark.tier0
def test_a_unique_names_spec_is_left_exactly_as_is():
    spec = ToolSpec(name="solo", description="x")
    [(_, tool)] = resolve_collisions([("solo", {"toolkit_id": "kit", "spec": spec})]).items()
    assert tool["spec"] is spec, "no-collision path must not copy or rewrite"


# --- T-064: assemble_for_user merges across mods ------------------------------
@pytest.mark.tier0
def test_assemble_for_user_is_empty_with_no_mods():
    assert assemble_for_user(None, _user(), defaults={}) == []


@pytest.mark.tier0
def test_assemble_for_user_skips_a_mod_whose_tools_the_user_lacks():
    crew = validate_mod_tools([_mod_tool(name="spawn", scope="mods.crew.spawn")])
    other = validate_mod_tools([_mod_tool(name="run", scope="mods.other.run")])
    load_result = types.SimpleNamespace(
        loaded={
            "crew": types.SimpleNamespace(tools=crew, manifest=_manifest("crew")),
            "other": types.SimpleNamespace(tools=other, manifest=_manifest("other")),
        }
    )
    pairs = assemble_for_user(load_result, _user(), defaults={}, has_permission_fn=_checker({"mods.crew.spawn"}))
    assert [name for name, _ in pairs] == ["spawn"]


# --- T-017: output_schema on a mod tool ---------------------------------------
# `cavekit-toolspec-model.md` R5 criterion 3: "a mod tool may set
# `output_schema` (e.g. a handle-returning tool declaring `{task_id, status}`);
# it appears in `to_mcp()` and is absent from `to_openai()`/`to_anthropic()`."
#
# FINDING: a mod tool CANNOT set one. `ModTool` declares no result-schema field
# and `assemble_mod_tools` passes none through, so every spec the mod path
# produces has `output_schema is None`. The `to_mcp()` half of the criterion is
# therefore unreachable from a mod today. That gap is pinned by the first test
# below rather than papered over; adding the field to `ModTool` is a production
# change and is not this task's to make.

HANDLE_SCHEMA = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}},
    "required": ["task_id", "status"],
}


@pytest.mark.tier0
def test_a_mod_tool_can_declare_an_output_schema():
    """T-017 found `ModTool` had no field for a result shape, so R5 criterion 3
    was unreachable from a mod at all. The field exists now, on both construction
    paths: the dataclass directly, and the dict-coercion path in
    `validate_mod_tools` that lets a mod return plain dicts if it prefers.

    Named `output_schema` to match `ToolSpec.output_schema` -- that is the name a
    mod author already sees on the facade, and one concept deserves one name."""
    assert {f.name for f in fields(ModTool)} == {
        "name",
        "description",
        "handler",
        "scope",
        "parameters",
        "output_schema",
    }

    direct = ModTool(name="spawn", description="x", handler=lambda **k: 1, scope=SCOPE, output_schema=HANDLE_SCHEMA)
    assert direct.output_schema == HANDLE_SCHEMA

    [coerced] = validate_mod_tools(
        [
            {
                "name": "spawn",
                "description": "x",
                "handler": lambda **k: 1,
                "scope": SCOPE,
                "output_schema": HANDLE_SCHEMA,
            }
        ]
    )
    assert coerced.output_schema == HANDLE_SCHEMA, "the dict-coercion path must carry it too"

    # Omitting it stays legal -- most tools have no result shape to declare.
    assert ModTool(name="s", description="x", handler=lambda **k: 1, scope=SCOPE).output_schema is None


@pytest.mark.tier0
def test_an_assembled_mod_spec_declares_no_result_shape():
    """The consequence on the live path. A handle-returning tool -- the
    `{task_id, status}` async pattern of `cavekit-mods-surfaces.md` T-072 --
    returns that shape at runtime (see
    test_a_sync_tool_returning_a_handle_reaches_the_model_unchanged) but
    declares nothing about it. `to_mcp()` omits `outputSchema` rather than
    emitting null, so an MCP consumer is told nothing, not told "no schema"."""
    handler = lambda **k: {"task_id": "t1", "status": "submitted"}  # noqa: E731
    tools = validate_mod_tools([_mod_tool(handler=handler)])
    spec = _assemble(tools, {SCOPE})["spawn"]["spec"]

    assert spec.output_schema is None
    assert "outputSchema" not in spec.to_mcp()
    assert set(spec.to_mcp()) == {"name", "description", "inputSchema"}


@pytest.mark.tier0
def test_an_output_schema_on_an_assembled_spec_reaches_mcp_and_no_other_format():
    """R5 criterion 3, now over the real declaration path: a mod declares
    `output_schema` on its `ModTool` and the assembled `ToolSpec` carries it.
    No `model_copy` -- the earlier version of this test needed one because
    `ModTool` had no field to declare it with.

    Carried but inert: `outputSchema` appears in the MCP payload, and neither
    the OpenAI nor the Anthropic function object gains a key -- both define no
    such field, so what the live chat path puts on the wire is unchanged by the
    declaration. That last property is the one worth having: a mod can describe
    its result shape without altering a single byte the model receives today."""
    plain = _assemble(validate_mod_tools([_mod_tool()]), {SCOPE})["spawn"]["spec"]
    before_openai = plain.to_openai()
    before_anthropic = plain.to_anthropic()

    declaring = validate_mod_tools([_mod_tool(output_schema=HANDLE_SCHEMA)])
    spec = _assemble(declaring, {SCOPE})["spawn"]["spec"]
    assert spec.output_schema == HANDLE_SCHEMA, "the declaration must survive assembly"

    mcp = spec.to_mcp()
    assert mcp["outputSchema"] == HANDLE_SCHEMA
    assert set(mcp) == {"name", "description", "inputSchema", "outputSchema"}

    assert set(spec.to_openai()) == {"name", "description", "parameters"}
    assert set(spec.to_anthropic()) == {"name", "description", "input_schema"}
    assert "output" not in str(spec.to_openai()) and "output" not in str(spec.to_anthropic())
    # Declaring a result shape changes nothing about what the model is offered.
    assert spec.to_openai() == before_openai
    assert spec.to_anthropic() == before_anthropic
