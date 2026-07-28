"""`get_tools()` builds typed specs without touching the stored one.

Cavekit: cavekit-toolspec-model.md R4 criteria 1 and 2 -- T-011.

The payload characterization in `test_toolspec_payload.py` pins what the model
is offered. It cannot pin what happens to the *source* spec, because the leak
this task fixes is invisible from the payload: `get_tools()` used to write

    spec["parameters"]["properties"] = {...}

straight into the dict it read off `tools.specs`, which is the spec list hanging
off a `Tools` DB model. Anything else holding that row -- the app's tools cache,
a concurrent request, a later read in the same session -- saw the stripped
schema. The tests here drive the real `get_tools()` over a spec object the test
still holds a reference to, and assert that object is byte-identical afterwards.

The stored spec below carries a `__`-prefixed property deliberately. The live
producer (`get_tools_specs` -> pydantic `create_model`) cannot emit one, since
pydantic v2 treats a leading-underscore name as a private attribute -- pinned by
`test_a_dunder_prefixed_argument_never_reaches_the_stored_schema` in
`test_toolspec_roundtrip.py`. The strip is load-bearing for the specs that did
not come from it: rows persisted by older versions, and hand-authored ones. That
is exactly the shape used here.
"""

import copy
import types

import pytest

from selfai_ui.utils import tools as tools_module
from selfai_ui.utils.tools import get_tools
from selfai_ui.utils.toolspec import ToolSpec

TOOL_ID = "legacy_toolkit"

STORED_SPEC = {
    "name": "lookup",
    "description": "Look a term up.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The term to look up."},
            "__user__": {"type": "object", "description": "Injected by core, never shown to the model."},
        },
        "required": ["query", "__user__"],
    },
}


class _Module:
    """A loaded toolkit module with no valves, so `get_tools` takes the plain
    path: no `Tools.get_tool_valves_by_id` / `UserValves` lookups."""

    @staticmethod
    def lookup(query: str) -> str:
        """Look a term up."""
        return f"looked up {query}"


@pytest.fixture
def stored_specs(monkeypatch):
    """Patch `Tools.get_tool_by_id` to hand back a spec list the test holds.

    Returning the SAME list object on every call is the point: it stands in for
    the shared row every reader of that toolkit gets."""
    specs = [copy.deepcopy(STORED_SPEC)]
    row = types.SimpleNamespace(id=TOOL_ID, specs=specs)
    monkeypatch.setattr(tools_module.Tools, "get_tool_by_id", staticmethod(lambda tool_id: row))
    return specs


def _run():
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(TOOLS={TOOL_ID: _Module})))
    return get_tools(request, [TOOL_ID], types.SimpleNamespace(id="u1"), {})


@pytest.mark.tier0
def test_the_spec_reaching_the_tool_dict_is_a_toolspec(stored_specs):
    """R4 criterion 2: the name is read off the typed field, so the value in the
    outer dict must actually be typed -- not a dict that happens to work."""
    assembled = _run()

    assert list(assembled) == ["lookup"]
    assert isinstance(assembled["lookup"]["spec"], ToolSpec)
    assert assembled["lookup"]["spec"].name == "lookup"


@pytest.mark.tier0
def test_the_stored_spec_is_not_mutated_by_the_internal_param_strip(stored_specs):
    """R4 criterion 1: the strip returns a copy; the original is untouched."""
    _run()

    assert stored_specs == [STORED_SPEC], "get_tools mutated the spec it read off the DB model"
    assert "__user__" in stored_specs[0]["parameters"]["properties"]
    assert stored_specs[0]["parameters"]["required"] == ["query", "__user__"]


@pytest.mark.tier0
def test_the_internal_param_is_absent_from_what_the_model_is_offered(stored_specs):
    """The strip still happens -- it just happens on the copy."""
    offered = _run()["lookup"]["spec"].to_openai()

    assert set(offered["parameters"]["properties"]) == {"query"}
    assert offered["parameters"]["required"] == ["query"]
    assert "__" not in str(offered)


@pytest.mark.tier0
def test_a_second_assembly_sees_the_unstripped_row_again(stored_specs):
    """The leak's actual symptom: with an in-place strip, the second read of the
    same row would already be stripped. Both calls must see the same source."""
    first = _run()["lookup"]["spec"]
    assert "__user__" in stored_specs[0]["parameters"]["properties"]
    second = _run()["lookup"]["spec"]

    assert first == second
    assert first is not second, "each assembly gets its own copy, not shared structure"


@pytest.mark.tier0
def test_the_outer_dict_still_has_its_six_keys(stored_specs):
    """Out of Scope, restated as a guard: the outer tool dict is a third-party
    contract and this build does not touch it."""
    [entry] = _run().values()

    assert set(entry) == {"toolkit_id", "callable", "spec", "pydantic_model", "file_handler", "citation"}
    assert entry["toolkit_id"] == TOOL_ID
