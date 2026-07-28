"""ToolSpec: field surface, forbidden extras, opaque schema bodies, serializers.

Cavekit: cavekit-toolspec-model.md R1 -- T-001 (criteria 1-4),
R2 -- T-003/T-004 (criteria 1-4), R3 -- T-005 (criterion 1),
R4 -- T-007 (criterion 1).

The exhaustive serializer conformance matrix is T-008 and the stored-shape round
trip is T-010; these are direct unit tests of the four methods themselves.
"""

import json

import pytest
from pydantic import ValidationError

from selfai_ui.utils.toolspec import ToolSpec, ToolSpecError

EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}}

# A body exercising the JSON-Schema constructs the model must NOT try to type.
NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"$ref": "#/$defs/Target"},
        "mode": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "opts": {
            "type": "object",
            "properties": {
                "retries": {"type": "integer"},
                "nested": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"deep": {"type": "boolean"}}},
                },
            },
        },
    },
    "required": ["target"],
    "$defs": {"Target": {"type": "string", "enum": ["a", "b"]}},
}


@pytest.mark.tier0
def test_a_fully_populated_spec_round_trips_every_field():
    spec = ToolSpec(
        name="spawn",
        description="Spawn a crew session.",
        input_schema={"type": "object", "properties": {"agent": {"type": "string"}}},
        title="Spawn Session",
        output_schema={"type": "object", "properties": {"task_id": {"type": "string"}}},
    )

    assert spec.name == "spawn"
    assert spec.description == "Spawn a crew session."
    assert spec.input_schema == {"type": "object", "properties": {"agent": {"type": "string"}}}
    assert spec.title == "Spawn Session"
    assert spec.output_schema == {"type": "object", "properties": {"task_id": {"type": "string"}}}


@pytest.mark.tier0
def test_a_minimal_spec_validates_and_defaults():
    spec = ToolSpec(name="x")

    assert spec.name == "x"
    assert spec.description == ""
    assert spec.input_schema == EMPTY_OBJECT_SCHEMA
    assert spec.title is None
    assert spec.output_schema is None


@pytest.mark.tier0
def test_default_input_schemas_are_not_shared_between_instances():
    # The live path hands these dicts to callers; a shared default would let one
    # spec's in-place edit corrupt every other.
    first = ToolSpec(name="a")
    second = ToolSpec(name="b")

    assert first.input_schema is not second.input_schema

    first.input_schema["properties"]["injected"] = {"type": "string"}

    assert second.input_schema == EMPTY_OBJECT_SCHEMA


@pytest.mark.tier0
def test_an_unexpected_key_is_a_validation_error():
    with pytest.raises(ValidationError):
        ToolSpec(name="x", parameters={"type": "object"})


@pytest.mark.tier0
def test_a_missing_name_is_a_validation_error():
    with pytest.raises(ValidationError):
        ToolSpec()


@pytest.mark.tier0
def test_a_nested_ref_and_oneof_body_passes_through_untouched():
    spec = ToolSpec(name="complex", input_schema=NESTED_SCHEMA, output_schema=NESTED_SCHEMA)

    assert spec.input_schema == NESTED_SCHEMA
    assert spec.output_schema == NESTED_SCHEMA
    assert spec.input_schema["properties"]["target"] == {"$ref": "#/$defs/Target"}
    assert spec.input_schema["properties"]["mode"]["oneOf"] == [{"type": "string"}, {"type": "null"}]
    assert spec.input_schema["properties"]["opts"]["properties"]["nested"]["items"]["properties"] == {
        "deep": {"type": "boolean"}
    }
    assert spec.input_schema["$defs"] == {"Target": {"type": "string", "enum": ["a", "b"]}}


# --- R2 / T-003: to_openai() and to_anthropic() ------------------------------

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "the query"}},
    "required": ["query"],
}

FULL_SPEC_KWARGS = {
    "name": "search",
    "description": "Search the web.",
    "input_schema": PARAMS_SCHEMA,
    "title": "Web Search",
    "output_schema": {"type": "object", "properties": {"hits": {"type": "array"}}},
}


@pytest.mark.tier0
def test_to_openai_emits_exactly_name_description_parameters():
    spec = ToolSpec(**FULL_SPEC_KWARGS)

    payload = spec.to_openai()

    # `==`, not `<=`: title/output_schema/strict are not fields of the OpenAI
    # function object, so emitting one is a failure, not a superset.
    assert set(payload) == {"name", "description", "parameters"}
    assert payload["name"] == "search"
    assert payload["description"] == "Search the web."
    assert payload["parameters"] == PARAMS_SCHEMA


@pytest.mark.tier0
def test_to_anthropic_emits_exactly_name_description_input_schema():
    spec = ToolSpec(**FULL_SPEC_KWARGS)

    payload = spec.to_anthropic()

    assert set(payload) == {"name", "description", "input_schema"}
    assert payload["name"] == "search"
    assert payload["description"] == "Search the web."
    assert payload["input_schema"] == PARAMS_SCHEMA


@pytest.mark.tier0
def test_openai_and_anthropic_emit_all_three_keys_for_a_minimal_spec():
    # No conditional omission: an empty description and a default schema are
    # still emitted, because both formats require the keys.
    spec = ToolSpec(name="x")

    assert spec.to_openai() == {"name": "x", "description": "", "parameters": EMPTY_OBJECT_SCHEMA}
    assert spec.to_anthropic() == {"name": "x", "description": "", "input_schema": EMPTY_OBJECT_SCHEMA}


@pytest.mark.tier0
def test_serializer_output_does_not_share_the_models_schema_dict():
    # The wire payload is detached on purpose: it is handed onward on the live
    # path, and the spec it came from may be cached off a DB model.
    spec = ToolSpec(name="x", input_schema=PARAMS_SCHEMA, output_schema={"type": "object"})

    for payload, key in (
        (spec.to_openai(), "parameters"),
        (spec.to_anthropic(), "input_schema"),
        (spec.to_mcp(), "inputSchema"),
    ):
        assert payload[key] is not spec.input_schema
        assert payload[key]["properties"] is not spec.input_schema["properties"]

        payload[key]["properties"]["injected"] = {"type": "string"}
        assert "injected" not in spec.input_schema["properties"]

    mcp = spec.to_mcp()
    assert mcp["outputSchema"] is not spec.output_schema


@pytest.mark.tier0
def test_every_serializer_output_is_json_serializable_as_is():
    spec = ToolSpec(**FULL_SPEC_KWARGS)

    for payload in (spec.to_openai(), spec.to_anthropic(), spec.to_mcp()):
        # No `default=` hook: a plain dict of plain values, or this raises.
        assert json.loads(json.dumps(payload)) == payload


# --- R2 / T-004: to_mcp() ----------------------------------------------------


@pytest.mark.tier0
def test_to_mcp_omits_both_optionals_when_neither_is_set():
    payload = ToolSpec(name="x", description="d", input_schema=PARAMS_SCHEMA).to_mcp()

    assert set(payload) == {"name", "description", "inputSchema"}
    assert payload["inputSchema"] == PARAMS_SCHEMA


@pytest.mark.tier0
def test_to_mcp_includes_title_alone_when_only_title_is_set():
    payload = ToolSpec(name="x", title="Ex").to_mcp()

    assert set(payload) == {"name", "description", "inputSchema", "title"}
    assert payload["title"] == "Ex"


@pytest.mark.tier0
def test_to_mcp_includes_output_schema_alone_when_only_output_schema_is_set():
    payload = ToolSpec(name="x", output_schema={"type": "object"}).to_mcp()

    assert set(payload) == {"name", "description", "inputSchema", "outputSchema"}
    assert payload["outputSchema"] == {"type": "object"}


@pytest.mark.tier0
def test_to_mcp_includes_both_optionals_when_both_are_set():
    payload = ToolSpec(**FULL_SPEC_KWARGS).to_mcp()

    assert set(payload) == {"name", "description", "inputSchema", "title", "outputSchema"}
    assert payload["title"] == "Web Search"
    assert payload["outputSchema"] == {"type": "object", "properties": {"hits": {"type": "array"}}}


@pytest.mark.tier0
def test_to_mcp_omits_unset_optionals_rather_than_emitting_nulls():
    # An absent optional and a present-but-null one are different statements to
    # an MCP client; the model must make the first.
    payload = ToolSpec(name="x").to_mcp()

    assert "title" not in payload
    assert "outputSchema" not in payload
    assert None not in payload.values()


# --- R3 / T-005: from_openai() -----------------------------------------------


@pytest.mark.tier0
def test_from_openai_maps_parameters_to_input_schema():
    spec = ToolSpec.from_openai({"name": "search", "description": "Search.", "parameters": PARAMS_SCHEMA})

    assert spec.name == "search"
    assert spec.description == "Search."
    assert spec.input_schema == PARAMS_SCHEMA
    assert spec.title is None
    assert spec.output_schema is None


@pytest.mark.tier0
def test_from_openai_tolerates_a_missing_description():
    spec = ToolSpec.from_openai({"name": "search", "parameters": PARAMS_SCHEMA})

    assert spec.description == ""
    assert spec.input_schema == PARAMS_SCHEMA


@pytest.mark.tier0
def test_from_openai_tolerates_a_missing_parameters():
    spec = ToolSpec.from_openai({"name": "search", "description": "Search."})

    assert spec.input_schema == EMPTY_OBJECT_SCHEMA


@pytest.mark.tier0
def test_from_openai_tolerates_both_omissions_at_once():
    spec = ToolSpec.from_openai({"name": "search"})

    assert spec.description == ""
    assert spec.input_schema == EMPTY_OBJECT_SCHEMA


@pytest.mark.tier0
def test_from_openai_still_rejects_an_unknown_key():
    # The parameters rename must not become a general "drop what you don't
    # recognize" -- an unsupported key is refused, not silently dropped.
    with pytest.raises(ToolSpecError):
        ToolSpec.from_openai({"name": "search", "parameters": PARAMS_SCHEMA, "strict": True})


@pytest.mark.tier0
def test_the_rejection_names_the_spec_the_key_and_the_fix():
    """This runs on the live chat path, so the message is the whole point.

    `get_tools` calls `from_openai` for every spec of every selected toolkit, so
    a stored row core cannot read surfaces to a user mid-conversation. A bare
    pydantic "Extra inputs are not permitted" names neither the tool nor the row
    and leaves an operator reading a stack trace backwards.
    """
    with pytest.raises(ToolSpecError) as excinfo:
        ToolSpec.from_openai({"name": "search", "parameters": PARAMS_SCHEMA, "strict": True})

    message = str(excinfo.value)
    assert "search" in message, "must name the offending spec"
    assert "strict" in message, "must name the offending key"
    assert "Re-saving" in message, "must say what to do about it"


@pytest.mark.tier0
def test_the_rejection_names_every_unsupported_key_not_just_the_first():
    """An operator fixing these one error at a time is an operator we failed."""
    with pytest.raises(ToolSpecError) as excinfo:
        ToolSpec.from_openai(
            {"name": "search", "parameters": PARAMS_SCHEMA, "strict": True, "cache_control": {}}
        )

    message = str(excinfo.value)
    assert "strict" in message and "cache_control" in message, message


@pytest.mark.tier0
def test_tool_spec_error_is_catchable_as_a_value_error():
    """A distinct type for callers that want it; a ValueError so existing broad
    handlers on the chat path still catch it rather than 500-ing."""
    assert issubclass(ToolSpecError, ValueError)


@pytest.mark.tier0
def test_from_openai_requires_a_name():
    with pytest.raises(ValidationError):
        ToolSpec.from_openai({"description": "no name"})


@pytest.mark.tier0
def test_from_openai_round_trips_the_stored_shape_through_to_openai():
    stored = {"name": "search", "description": "Search.", "parameters": PARAMS_SCHEMA}

    assert ToolSpec.from_openai(stored).to_openai() == stored


# --- R4 criterion 1 / T-007: without_internal_params() -----------------------

INTERNAL_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "__user__": {"type": "object"},
        "__event_emitter__": {"type": "object"},
    },
    "required": ["query"],
}


@pytest.mark.tier0
def test_without_internal_params_removes_dunder_prefixed_properties():
    spec = ToolSpec(name="search", input_schema=INTERNAL_PARAMS_SCHEMA)

    stripped = spec.without_internal_params()

    assert stripped.input_schema["properties"] == {"query": {"type": "string"}}
    assert stripped.input_schema["type"] == "object"
    assert stripped.input_schema["required"] == ["query"]


@pytest.mark.tier0
def test_without_internal_params_leaves_the_receiver_unmutated():
    # The bug being fixed: the old in-place write landed on a spec object cached
    # off a Tools DB model, so the strip leaked into every later reader.
    spec = ToolSpec(name="search", input_schema=INTERNAL_PARAMS_SCHEMA)

    stripped = spec.without_internal_params()

    assert spec.input_schema == INTERNAL_PARAMS_SCHEMA
    assert set(spec.input_schema["properties"]) == {"query", "__user__", "__event_emitter__"}
    assert stripped.input_schema is not spec.input_schema
    assert stripped.input_schema["properties"] is not spec.input_schema["properties"]

    stripped.input_schema["properties"]["added_later"] = {"type": "string"}
    stripped.input_schema["required"].append("added_later")

    assert "added_later" not in spec.input_schema["properties"]
    assert spec.input_schema["required"] == ["query"]


@pytest.mark.tier0
def test_without_internal_params_carries_the_other_fields_across():
    spec = ToolSpec(**FULL_SPEC_KWARGS)

    stripped = spec.without_internal_params()

    assert stripped.name == "search"
    assert stripped.description == "Search the web."
    assert stripped.title == "Web Search"
    assert stripped.output_schema == FULL_SPEC_KWARGS["output_schema"]
    assert stripped.output_schema is not spec.output_schema


@pytest.mark.tier0
def test_without_internal_params_returns_an_equal_copy_when_there_is_nothing_to_strip():
    spec = ToolSpec(name="search", input_schema=PARAMS_SCHEMA)

    stripped = spec.without_internal_params()

    assert stripped == spec
    assert stripped is not spec
    assert stripped.input_schema is not spec.input_schema
    assert stripped.input_schema["properties"] is not spec.input_schema["properties"]


@pytest.mark.tier0
def test_without_internal_params_handles_a_schema_with_no_properties_key():
    spec = ToolSpec(name="search", input_schema={"type": "object"})

    stripped = spec.without_internal_params()

    assert stripped.input_schema == {"type": "object"}
    assert stripped.input_schema is not spec.input_schema


@pytest.mark.tier0
def test_without_internal_params_also_drops_dunder_entries_from_required():
    # get_tools_specs cannot produce one (pydantic v2 drops leading-underscore
    # field names outright), but a stored or hand-authored spec can, and a
    # required entry naming a property we just removed is invalid JSON Schema.
    spec = ToolSpec(
        name="search",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "__user__": {"type": "object"}},
            "required": ["query", "__user__"],
        },
    )

    stripped = spec.without_internal_params()

    assert stripped.input_schema["required"] == ["query"]
    assert stripped.input_schema["properties"] == {"query": {"type": "string"}}
