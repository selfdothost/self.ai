"""Serializer conformance: the published field set of each tool wire format.

Cavekit: cavekit-toolspec-model.md R2 -- T-008 (criteria 1-5), T-009 (criterion 6).

The expected field sets live in ``PUBLISHED_FIELDS`` below as data, transcribed
from the wire-format table in the decision record
(``2026-07-22-toolspec-provider-neutral-model.md``). The point is that the shape
these tests enforce can be checked against the published spec at a glance rather
than reconstructed from scattered assertions. Every key-set assertion uses ``==``,
never ``<=``: a serializer that emits a key its target format does not define is
as much a failure as one that omits a key the format requires.

T-009 -- the duplication, and its resolution. Core already shipped an Anthropic
tool translation when ``to_anthropic()`` was written:
``convert_tools_openai_to_anthropic`` (``utils/payload.py``), reached from
``routers/anthropic.py`` via ``convert_payload_openai_to_anthropic``. It emitted
the same ``{name, description, input_schema}`` shape by hand, which made
``to_anthropic()`` a *second* implementation of a translation core already
performed. These tests were written to pin the two equal so they could not drift
before anything unified them -- and they found two places where they already had.

The converter now builds a ``ToolSpec`` and returns ``to_anthropic()``, so the
Anthropic wire shape is defined once. The agreement tests below therefore can no
longer fail by drift; they fail if the delegation is undone, which is the
property worth keeping.

What did NOT get unified, deliberately: the converter's leniency about its
*input*. It runs at an API boundary where ``form_data`` is an untyped dict from
any client, so it drops an unnamed tool, reads a falsy schema as "no arguments",
and ignores keys OpenAI defines that core does not consume. ``from_openai`` is
strict about all three and would raise -- correct when core reads back a spec it
wrote, wrong at the edge. Those three behaviours are tested at the bottom of this
file as boundary policy rather than as divergence.
"""

import json

import pytest

from selfai_ui.utils.payload import convert_tools_openai_to_anthropic
from selfai_ui.utils.toolspec import ToolSpec

# The published field set of each format, transcribed from the treasuremap's
# wire-format table. `required` is emitted unconditionally; `optional` only when
# the corresponding model field is set (MCP alone declares any).
PUBLISHED_FIELDS = {
    "openai": {
        "required": {"name", "description", "parameters"},
        "optional": set(),
        "schema_key": "parameters",
    },
    "anthropic": {
        "required": {"name", "description", "input_schema"},
        "optional": set(),
        "schema_key": "input_schema",
    },
    "mcp": {
        "required": {"name", "description", "inputSchema"},
        "optional": {"title", "outputSchema"},
        "schema_key": "inputSchema",
    },
}

SERIALIZERS = {
    "openai": ToolSpec.to_openai,
    "anthropic": ToolSpec.to_anthropic,
    "mcp": ToolSpec.to_mcp,
}

FORMATS = sorted(PUBLISHED_FIELDS)

ARGS_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "where to look"}},
    "required": ["path"],
}

RESULT_SCHEMA = {"type": "object", "properties": {"found": {"type": "boolean"}}}

# A body using the JSON-Schema constructs the model must carry opaquely.
REF_SCHEMA = {
    "type": "object",
    "properties": {"target": {"$ref": "#/$defs/Target"}},
    "required": ["target"],
    "$defs": {"Target": {"type": "object", "properties": {"id": {"type": "string"}}}},
}

MINIMAL = ToolSpec(name="ping")
POPULATED = ToolSpec(
    name="lookup",
    description="Look something up.",
    input_schema=ARGS_SCHEMA,
    title="Lookup",
    output_schema=RESULT_SCHEMA,
)


def _serialize(fmt: str, spec: ToolSpec) -> dict:
    return SERIALIZERS[fmt](spec)


# --- R2 criteria 1-3, 5: the emitted key set is exactly the published one -----


@pytest.mark.tier0
@pytest.mark.parametrize("fmt", FORMATS)
def test_a_minimal_spec_emits_exactly_the_formats_required_fields(fmt):
    # Nothing optional is set, so every format must emit its required keys and
    # nothing else -- including the formats that have no optional keys at all.
    payload = _serialize(fmt, MINIMAL)

    assert set(payload) == PUBLISHED_FIELDS[fmt]["required"]


@pytest.mark.tier0
@pytest.mark.parametrize("fmt", FORMATS)
def test_a_fully_populated_spec_emits_required_plus_only_the_declared_optionals(fmt):
    # title/output_schema are set. MCP declares both; OpenAI and Anthropic
    # declare neither, so they must still emit exactly their three keys -- this
    # is the assertion that catches a serializer leaking an MCP-only field.
    published = PUBLISHED_FIELDS[fmt]
    payload = _serialize(fmt, POPULATED)

    assert set(payload) == published["required"] | published["optional"]


@pytest.mark.tier0
@pytest.mark.parametrize("fmt", FORMATS)
def test_the_argument_schema_lands_under_the_formats_own_key(fmt):
    # The three formats disagree on one thing only: what the argument schema is
    # called. That rename is the whole reason this model exists.
    published = PUBLISHED_FIELDS[fmt]
    payload = _serialize(fmt, POPULATED)

    assert payload[published["schema_key"]] == ARGS_SCHEMA
    assert payload["name"] == "lookup"
    assert payload["description"] == "Look something up."


@pytest.mark.tier0
@pytest.mark.parametrize("fmt", FORMATS)
def test_no_format_emits_a_key_outside_its_published_set(fmt):
    # Belt and braces against a future field being added to the model and
    # falling out of a serializer by accident: check every spec in the table,
    # not just the two canonical ones.
    published = PUBLISHED_FIELDS[fmt]
    allowed = published["required"] | published["optional"]

    for spec in (MINIMAL, POPULATED, ToolSpec(name="ref", input_schema=REF_SCHEMA, output_schema=RESULT_SCHEMA)):
        payload = _serialize(fmt, spec)

        assert set(payload) <= allowed
        assert published["required"] <= set(payload)


# --- R2 criterion 3: the to_mcp optional matrix, all four cases --------------

MCP_OPTIONAL_MATRIX = [
    ("neither", {}, set()),
    ("title only", {"title": "Lookup"}, {"title"}),
    ("output_schema only", {"output_schema": RESULT_SCHEMA}, {"outputSchema"}),
    ("both", {"title": "Lookup", "output_schema": RESULT_SCHEMA}, {"title", "outputSchema"}),
]


@pytest.mark.tier0
@pytest.mark.parametrize(
    "extra_kwargs,expected_optionals",
    [case[1:] for case in MCP_OPTIONAL_MATRIX],
    ids=[case[0] for case in MCP_OPTIONAL_MATRIX],
)
def test_to_mcp_emits_exactly_the_optionals_that_are_set(extra_kwargs, expected_optionals):
    published = PUBLISHED_FIELDS["mcp"]
    payload = ToolSpec(name="lookup", input_schema=ARGS_SCHEMA, **extra_kwargs).to_mcp()

    assert set(payload) == published["required"] | expected_optionals


@pytest.mark.tier0
@pytest.mark.parametrize(
    "extra_kwargs,expected_optionals",
    [case[1:] for case in MCP_OPTIONAL_MATRIX],
    ids=[case[0] for case in MCP_OPTIONAL_MATRIX],
)
def test_to_mcp_omits_an_unset_optional_rather_than_emitting_null(extra_kwargs, expected_optionals):
    # Omission, not null: to an MCP client "absent" and "present but null" are
    # different statements, and only the first is what an unset field means.
    payload = ToolSpec(name="lookup", input_schema=ARGS_SCHEMA, **extra_kwargs).to_mcp()

    for optional in PUBLISHED_FIELDS["mcp"]["optional"] - expected_optionals:
        assert optional not in payload

    assert None not in payload.values()


# --- R2 criterion 4: json.dumps-able with no default= hook -------------------

JSON_SPEC_TABLE = [
    ("minimal", MINIMAL),
    ("populated", POPULATED),
    ("nested $ref body", ToolSpec(name="ref", input_schema=REF_SCHEMA, output_schema=REF_SCHEMA)),
    ("empty description", ToolSpec(name="quiet", description="", input_schema=ARGS_SCHEMA)),
    ("title only", ToolSpec(name="titled", title="Titled")),
]


@pytest.mark.tier0
@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("spec", [case[1] for case in JSON_SPEC_TABLE], ids=[case[0] for case in JSON_SPEC_TABLE])
def test_every_serializer_output_survives_json_dumps_with_no_default_hook(fmt, spec):
    # No `default=`: if any value were a pydantic model (or anything else not
    # natively JSON-encodable) this raises. That is the failure mode this
    # criterion exists for -- the live path json.dumps's these payloads
    # straight into the completion request.
    payload = _serialize(fmt, spec)

    assert json.loads(json.dumps(payload)) == payload


# --- R2 criterion 6 / T-009: pinned equal to the shipped converter -----------

ANTHROPIC_SPEC_TABLE = [
    ("no params", ToolSpec(name="ping", description="Ping the service.")),
    ("params", ToolSpec(name="lookup", description="Look something up.", input_schema=ARGS_SCHEMA)),
    ("nested $ref body", ToolSpec(name="ref", description="Takes a $ref body.", input_schema=REF_SCHEMA)),
    ("empty description", ToolSpec(name="quiet", description="", input_schema=ARGS_SCHEMA)),
    ("mcp-only fields set", POPULATED),
]


def _shipped_converter(spec: ToolSpec) -> dict:
    """Run the spec through core's live OpenAI -> Anthropic tool translation."""
    return convert_tools_openai_to_anthropic([{"type": "function", "function": spec.to_openai()}])[0]


@pytest.mark.tier0
@pytest.mark.parametrize(
    "spec", [case[1] for case in ANTHROPIC_SPEC_TABLE], ids=[case[0] for case in ANTHROPIC_SPEC_TABLE]
)
def test_to_anthropic_agrees_with_the_shipped_dict_level_converter(spec):
    # Two translations nobody has compared are two translations that drift.
    # This was that comparison, written while they really were two
    # implementations. The converter now emits through to_anthropic(), so this
    # can no longer fail by drift -- it fails if the delegation is ever undone,
    # which is the property worth keeping.
    assert spec.to_anthropic() == _shipped_converter(spec)


@pytest.mark.tier0
def test_the_mcp_only_fields_are_invisible_to_both_anthropic_translations():
    # title/output_schema cannot survive the converter -- it never sees them,
    # since to_openai() does not emit them. to_anthropic() must drop them too,
    # or the two disagree the moment a mod tool declares an output_schema.
    payload = POPULATED.to_anthropic()

    assert set(payload) == PUBLISHED_FIELDS["anthropic"]["required"]
    assert payload == _shipped_converter(POPULATED)


# --- The two places the boundary converter deliberately differs -----------------
#
# T-009 found these as *divergences* between two hand-written implementations of
# one translation, and pinned them so they could not drift further. The converter
# now emits through `to_anthropic()`, so the wire shape is defined once and
# cannot drift at all -- but these two behaviours survived the unification on
# purpose, because they are boundary policy rather than shape.
#
# The boundary is `routers/anthropic.py`'s completion endpoint, whose `form_data`
# is an untyped dict off the wire. `ToolSpec.from_openai` is deliberately strict
# (extra="forbid", name required) and is the wrong tool there: a client sending
# an OpenAI payload with `strict: true` would get a 500 over a field we are free
# to ignore. So the converter stays lenient about its *input* while delegating
# its *output*.


@pytest.mark.tier0
def test_the_boundary_drops_an_unnamed_tool_rather_than_failing_the_request():
    """A tool with no name cannot be called anyway, and taking down a whole
    conversation over one malformed entry in an otherwise fine list is the wrong
    trade at an API edge. `to_anthropic()` itself has no such filter -- it would
    happily emit an empty name and leave the rejection to the provider -- so the
    drop is the boundary's decision, made here and only here."""
    nameless = ToolSpec(name="")

    assert convert_tools_openai_to_anthropic([{"type": "function", "function": nameless.to_openai()}]) == []
    # The model itself does not filter; that asymmetry is the point.
    assert nameless.to_anthropic()["name"] == ""


@pytest.mark.tier0
def test_the_boundary_reads_a_falsy_schema_as_takes_no_arguments():
    """`{}` and `None` both mean "no arguments" from a client that did not think
    about it. Anthropic wants a schema object, so the boundary supplies the one
    that says exactly that -- by omitting the key and letting `ToolSpec`'s own
    default fill it, so "the empty schema" is defined in one place.

    `to_anthropic()` on a spec whose `input_schema` really is `{}` passes it
    through untouched, per Decision 2: the schema body is data core does not
    second-guess. Both behaviours are correct for where they sit."""
    assert convert_tools_openai_to_anthropic([{"type": "function", "function": {"name": "x", "parameters": {}}}]) == [
        {"name": "x", "description": "", "input_schema": {"type": "object", "properties": {}}}
    ]

    assert ToolSpec(name="x", input_schema={}).to_anthropic()["input_schema"] == {}


@pytest.mark.tier0
def test_the_boundary_ignores_a_key_openai_defines_and_core_does_not_consume():
    """The reason `from_openai` is not used here. It is `extra="forbid"` and
    would raise; this endpoint takes an untyped dict from any client, and
    `strict` is a real OpenAI field. Ignoring it must not become raising on it."""
    [converted] = convert_tools_openai_to_anthropic(
        [{"type": "function", "function": {"name": "x", "description": "d", "parameters": {}, "strict": True}}]
    )

    assert set(converted) == {"name", "description", "input_schema"}
    assert "strict" not in converted


@pytest.mark.tier0
def test_the_boundary_emits_through_to_anthropic_so_the_shape_cannot_drift():
    """The unification itself. Every field the converter emits comes from
    `to_anthropic()`, so a change to the Anthropic wire shape lands in one place
    and both paths move together. Asserted over the same spec table the
    agreement test uses."""
    for _label, spec in ANTHROPIC_SPEC_TABLE:
        assert _shipped_converter(spec) == spec.to_anthropic()
