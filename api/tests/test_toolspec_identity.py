"""Payload identity after the conversion, through the real assembly path.

T-016, `cavekit-toolspec-model.md` R4 criteria 4 and 6, R5 criterion 2. This is
the **after-picture** to `test_toolspec_payload.py`'s before-picture.

WHY THIS FILE DOES NOT REPEAT THE BASELINE
------------------------------------------
`test_toolspec_payload.py` was written against unconverted code, where the inner
spec was a plain dict, and it **passed unchanged against the converted code** --
all 26 tests, not one line edited. The headline claim of this task ("the payload
the model receives did not change") is therefore already evidenced by that file
having been green, and re-asserting `openai_tools == expected` here would add a
second copy of an existing proof.

That claim is about the ToolSpec conversion specifically, and it still holds. It
does not mean the file is frozen forever: fixing the `mod:` origin-marker leak
in the collision qualifier (see `collide_name`) later touched its `SCENARIOS`
constant, because the collision case's expected value encoded the leak as
correct. That is a deliberate, subsequent behaviour change, not a regression in
the identity claim -- the byte-comparisons below hold against the current,
corrected values.

So this file adds only what the baseline could not, and makes the identity claim
explicit rather than incidental:

* **Bytes, not structure.** The baseline compares with `==`, which ignores key
  order: `{"name":…,"parameters":…}` and `{"parameters":…,"name":…}` are equal
  dicts and different wire bodies. Everything here compares serialized text, so
  a reordering fails.
* **The stored spec as the pre-conversion artefact.** R3 says the DB
  representation does not change, so the raw `tools.specs` JSON is a
  pre-conversion object that survived this build. The function object the model
  is offered is compared byte-for-byte against it -- which is what "serialized to
  the completion endpoint exactly as before" means for a user-authored tool --
  and the row is re-read afterwards to prove the drive did not write through to
  it (the in-place strip R4 criterion 1 removed).
* **That `to_openai()` is what the wrap site emits.** Not merely that the result
  looks right: each offered function is compared byte-for-byte with
  `spec.to_openai()` of the very `ToolSpec` that reached `admin_tools`, and the
  spec is asserted to still be a `ToolSpec` there while the payload entry is a
  plain `dict`. That pins the conversion to the wrap site.
* **Indistinguishability as a shape identity**, not a key-set check: mod tool,
  user tool and web-search tool are reduced to a shape signature (key order,
  value types) and asserted equal, over every scenario.
* **No shared structure between the payload and the specs that produced it.**
  The baseline never touches aliasing; this is the hazard the serializers'
  deep-copy exists for.

THE VERIFICATION CONVENTION
---------------------------
Nothing here constructs a `ToolSpec` and serializes it. Every value under test
came out of `get_tools()` / `assemble_for_user()` / `resolve_collisions()` via
the real `middleware.generate_chat_completion_with_tools`, reached through the
baseline's `drive` fixture -- the same harness, deliberately reused rather than
forked, so the two files cannot drift apart in what they exercise. The literal
snapshots imported from the baseline are the *expected* side only.

ONE THING DELIBERATELY NOT ASSERTED
------------------------------------
* **Mod tools do not open the tool-calling path** (self.ai#61): the gate at
  `middleware.py:630` is `not tool_ids and not web_search_enabled` and never
  consults mod tools. Filed, out of scope, and pinned by the baseline's
  `test_mod_tools_alone_do_not_open_the_tool_calling_path`. The `mod_only`
  scenario is only meaningful with web search on, which is why `CASES` omits the
  other half.

WHAT USED TO BE HERE
---------------------
A collision used to leak the mod id to the model: `resolve_collisions`
qualified as `<toolkit_id>__<name>`, and a mod's `toolkit_id` is `mod:<id>`, so
a colliding mod tool was offered as `mod:crew__lookup`. Fixed in `collide_name`,
which now strips the `mod:` scheme prefix before qualifying -- see
`test_a_collision_qualifies_both_names_with_the_same_shape` in
`test_toolspec_payload.py` for the property that fix restores. The no-origin
tests below still exclude the collision scenario, but for an unrelated reason
now: the fixture's user-authored toolkit id, `baseline_toolkit`, contains the
literal substring "toolkit", so a collision naturally puts that string in the
payload regardless of mod involvement.
"""

import json

import pytest

from selfai_ui.models.tools import Tools
from selfai_ui.utils import middleware
from selfai_ui.utils.toolspec import ToolSpec
from tests.test_toolspec_payload import (
    CASE_IDS,
    CASES,
    EXPECTED_BROWSING_FUNCTIONS,
    EXPECTED_WEB_SEARCH_FUNCTION,
    SCENARIOS,
    USER_TOOL_ID,
    assert_wire_shape,
    drive,  # noqa: F401 -- the shared harness fixture, bound by name
    expected_openai_tools,
)

# The collision scenario is excluded from the no-origin tests below, but not
# because a mod marker leaks there anymore -- it doesn't. The fixture's own
# user-authored toolkit id, `baseline_toolkit`, contains the literal substring
# "toolkit", so a collision naturally puts that string in the payload for
# reasons that have nothing to do with mods (see the module docstring).
CLEAN_CASES = [(name, web_search) for name, web_search in CASES if not SCENARIOS[name]["collision"]]
CLEAN_IDS = [f"{name}-websearch_{'on' if ws else 'off'}" for name, ws in CLEAN_CASES]

# The wire order of both objects. Asserted as a list, not a set: two dicts with
# the same keys in a different order are `==` and serialize to different bytes.
ENTRY_KEY_ORDER = ["type", "function"]
FUNCTION_KEY_ORDER = ["name", "description", "parameters"]


def _drive_case(drive, scenario, web_search, **kwargs):  # noqa: F811
    case = SCENARIOS[scenario]
    return drive(user_tool=case["user_tool"], mod_tool_names=case["mod_tool_names"], web_search=web_search, **kwargs)


def _shape(entry):
    """A tool entry reduced to everything except its declared content.

    Key order and value types survive; the name, the description text and the
    schema body do not. Two entries with the same shape are the same object as
    far as a model can tell -- which is exactly the R5 criterion 2 property."""
    return {
        "entry_keys": list(entry),
        "type": entry["type"],
        "function_keys": list(entry["function"]),
        "value_types": [type(value).__name__ for value in entry["function"].values()],
    }


# --- Byte identity -------------------------------------------------------------


@pytest.mark.tier0
@pytest.mark.parametrize(("scenario", "web_search"), CASES, ids=CASE_IDS)
def test_the_serialized_tools_array_is_byte_identical_to_the_baseline(drive, scenario, web_search):  # noqa: F811
    """The bytes, not the dict. `==` on the payload (what the baseline asserts)
    would pass with the keys in any order; a provider reads a byte stream."""
    captured = _drive_case(drive, scenario, web_search)
    expected = json.dumps(expected_openai_tools(scenario, web_search=web_search)).encode()

    assert json.dumps(captured["openai_tools"]).encode() == expected


@pytest.mark.tier0
@pytest.mark.parametrize(("scenario", "web_search"), CASES, ids=CASE_IDS)
def test_the_serialized_request_body_carries_those_exact_bytes(drive, scenario, web_search):  # noqa: F811
    """The round payload built at `middleware.py:433` is serialized whole -- no
    `default=` hook, so nothing typed may have survived into it -- and the
    `tools` slice of that body is byte-identical to the payload the wrap site
    produced. This is R4 criterion 4's "JSON-serialized to the completion
    endpoint exactly as before" read literally."""
    captured = _drive_case(drive, scenario, web_search)
    [round_payload] = captured["round_payloads"]

    body = json.dumps(round_payload).encode()
    expected = json.dumps(expected_openai_tools(scenario, web_search=web_search)).encode()

    assert json.dumps(round_payload["tools"]).encode() == expected
    assert json.dumps(json.loads(body)["tools"]).encode() == expected
    assert json.dumps(captured["openai_tools"]).encode() == expected


@pytest.mark.tier0
def test_the_streaming_body_is_byte_identical_to_the_buffered_one(drive):  # noqa: F811
    """The two round-payload builders (`middleware.py:433` and `:502`) are
    separate code. The baseline pins each against the snapshot; this pins them
    against each other, byte-wise, so a reordering introduced in one is caught
    even if the snapshot were ever relaxed."""
    buffered = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)
    streamed = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True, stream=True)

    [buffered_payload] = buffered["round_payloads"]
    [streamed_payload] = streamed["round_payloads"]
    assert json.dumps(streamed_payload["tools"]).encode() == json.dumps(buffered_payload["tools"]).encode()


# --- The stored spec: a pre-conversion object this build did not touch ---------


@pytest.mark.tier0
@pytest.mark.parametrize(
    ("scenario", "web_search"),
    [(name, ws) for name, ws in CASES if SCENARIOS[name]["user_tool"] and not SCENARIOS[name]["collision"]],
    ids=[
        f"{name}-websearch_{'on' if ws else 'off'}"
        for name, ws in CASES
        if SCENARIOS[name]["user_tool"] and not SCENARIOS[name]["collision"]
    ],
)
def test_the_offered_user_function_is_byte_identical_to_the_stored_spec(drive, scenario, web_search):  # noqa: F811
    """R3 keeps the DB representation unchanged, so the raw `tools.specs` JSON is
    a pre-conversion artefact: it is what the old wrap site wrapped directly.
    The function object the model is now offered -- built by
    `ToolSpec.from_openai(...).without_internal_params().to_openai()` across
    three modules -- must serialize to those same bytes.

    Excludes the collision scenario, where `resolve_collisions` deliberately
    rewrites the name."""
    captured = _drive_case(drive, scenario, web_search)
    [stored] = Tools.get_tool_by_id(USER_TOOL_ID).specs
    offered = next(e["function"] for e in captured["openai_tools"] if e["function"]["name"] == "lookup")

    assert json.dumps(offered).encode() == json.dumps(stored).encode()


@pytest.mark.tier0
def test_driving_the_path_does_not_write_through_to_the_stored_spec(drive):  # noqa: F811
    """The strip at `utils/tools.py:62-64` used to be an in-place
    `spec["parameters"]["properties"] = {...}` into a dict read off a cached
    `Tools` row. R4 criterion 1 replaced it with `without_internal_params()`,
    which returns a copy. Proven here end-to-end rather than on the method: the
    row's bytes are identical before and after a full request."""
    captured = drive(user_tool=True, mod_tool_names=(), web_search=False)
    before = json.dumps(Tools.get_tool_by_id(USER_TOOL_ID).specs).encode()

    captured = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)
    assert captured["openai_tools"], "the assembly path must actually have run"

    assert json.dumps(Tools.get_tool_by_id(USER_TOOL_ID).specs).encode() == before


# --- Both wrap sites emit `.to_openai()` (R4 criterion 4) ----------------------


@pytest.mark.tier0
@pytest.mark.parametrize(("scenario", "web_search"), CASES, ids=CASE_IDS)
def test_each_offered_function_is_exactly_its_specs_to_openai(drive, scenario, web_search):  # noqa: F811
    """The first wrap site (`middleware.py:679`). What reaches `admin_tools` is
    still a `ToolSpec` -- typed all the way to the edge -- and what leaves the
    wrap site is a plain `dict` whose bytes are that spec's `to_openai()`. An
    entry built any other way (a `model_dump`, a hand-assembled dict, a spec
    serialized somewhere upstream) fails this."""
    captured = _drive_case(drive, scenario, web_search)
    assembled = list(captured["admin_tools"].values())
    offered = captured["openai_tools"][: len(assembled)]

    assert len(offered) == len(assembled), "every assembled tool must be offered, in order"
    for tool, entry in zip(assembled, offered):
        spec = tool["spec"]
        assert isinstance(spec, ToolSpec), f"the assembled spec is a {type(spec).__name__}, not a ToolSpec"
        assert type(entry["function"]) is dict, "the wrap site must emit a plain dict, not a typed object"
        assert json.dumps(entry["function"]).encode() == json.dumps(spec.to_openai()).encode()


@pytest.mark.tier0
def test_the_web_search_wrap_site_emits_to_openai_of_a_toolspec(drive):  # noqa: F811
    """The second wrap site (`middleware.py:681`) and R4 criterion 5.
    `WEB_SEARCH_TOOL_SPEC` is the production module-level constant, asserted to
    be a `ToolSpec`; the entry appended to the payload is its `to_openai()`,
    byte-for-byte, and byte-identical to the baseline snapshot."""
    assert isinstance(middleware.WEB_SEARCH_TOOL_SPEC, ToolSpec)

    captured = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)
    # Selected by name rather than by position: web_search is no longer the
    # last entry now that the toggle offers the whole browsing set (R10), and
    # this test is about what that wrap site emits, not where it lands.
    entry = next(e for e in captured["openai_tools"] if e["function"]["name"] == "web_search")

    assert json.dumps(entry["function"]).encode() == json.dumps(middleware.WEB_SEARCH_TOOL_SPEC.to_openai()).encode()
    assert json.dumps(entry["function"]).encode() == json.dumps(EXPECTED_WEB_SEARCH_FUNCTION).encode()


# --- Indistinguishable: R4 criterion 6, R5 criterion 2 -------------------------


@pytest.mark.tier0
@pytest.mark.parametrize(("scenario", "web_search"), CASES, ids=CASE_IDS)
def test_every_offered_entry_has_the_identical_key_order(drive, scenario, web_search):  # noqa: F811
    """Key ORDER, over the whole matrix including the collision. The baseline
    asserts key sets; two dicts differing only in order compare equal there and
    differ on the wire. Whatever produced a tool, the model reads the same
    sequence of keys."""
    captured = _drive_case(drive, scenario, web_search)
    assert_wire_shape(captured["openai_tools"])

    for entry in captured["openai_tools"]:
        assert list(entry) == ENTRY_KEY_ORDER
        assert list(entry["function"]) == FUNCTION_KEY_ORDER


@pytest.mark.tier0
def test_a_mod_tool_a_user_tool_and_web_search_are_one_shape(drive):  # noqa: F811
    """R5 criterion 2, as a shape identity rather than a key-set check. Strip
    the declared content -- name, description text, schema body -- and the three
    entries are the same object. This is the old T-065 property, now a
    consequence of the shared type rather than of three producers agreeing by
    inspection."""
    captured = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)
    user_entry, mod_entry, *browsing_entries = captured["openai_tools"]

    # Every browsing tool is included, not just web_search: a tool added to the
    # set later must be the same shape as the rest or this fails.
    assert len(browsing_entries) == len(EXPECTED_BROWSING_FUNCTIONS)
    for browsing_entry in browsing_entries:
        assert _shape(user_entry) == _shape(mod_entry) == _shape(browsing_entry)
    # ...and the origin really is known one level up, so this is not vacuous.
    assert captured["admin_tools"]["spawn"]["toolkit_id"] == "mod:crew"
    assert captured["admin_tools"]["lookup"]["toolkit_id"] == USER_TOOL_ID


@pytest.mark.tier0
@pytest.mark.parametrize(("scenario", "web_search"), CLEAN_CASES, ids=CLEAN_IDS)
def test_nothing_in_the_whole_request_body_names_a_mod_or_a_toolkit(drive, scenario, web_search):  # noqa: F811
    """Widened from the baseline in two directions: the entire serialized request
    body rather than the tools array alone, and both web-search settings rather
    than only `on`. The model reads the body, not the array, so the outer
    six-key dict's `toolkit_id` (`mod:crew` for a mod tool) must not surface
    anywhere in it -- nor must any other field of that dict.

    `metadata` is dropped first, deliberately and with a reason: it carries
    `tool_ids` -- the user's toolkit id in plain text -- but it is core's own
    envelope and never goes on the wire. `routers/openai.py:516` (and `:681`)
    `del payload["metadata"]` before the outbound request. The harness captures
    the payload one layer above that deletion, so asserting over it would be
    asserting about a field the provider never sees. Everything else in the body
    does reach the provider."""
    captured = _drive_case(drive, scenario, web_search)
    outbound = {key: value for key, value in captured["round_payloads"][0].items() if key != "metadata"}
    body = json.dumps(outbound)

    assert "toolkit_id" not in body
    assert "mod:" not in body
    assert "toolkit" not in body.lower()
    for leaked in ("callable", "pydantic_model", "file_handler", "citation"):
        assert leaked not in body, f"the outer tool dict's {leaked!r} reached the request body"


@pytest.mark.tier0
def test_the_collision_no_longer_leaks_the_mod_id(drive):  # noqa: F811
    """This test used to pin the opposite, deliberately: a collision qualified
    a mod tool as `mod:crew__lookup`, and the assertion bounded the leak rather
    than asserting it away, since nothing at the time removed it.

    `collide_name` now strips the `mod:` scheme prefix before qualifying, so
    there is nothing left to bound -- the whole request body, offered names
    included, is checked directly for the absence of the marker. `toolkit_id`
    (the outer dict's bookkeeping field) is checked too, since that is the
    field the prefix comes from and its absence from the body is what makes
    the offered name the only place origin information could have leaked
    through in the first place."""
    captured = drive(user_tool=True, mod_tool_names=("lookup",), web_search=True)
    [round_payload] = captured["round_payloads"]
    outbound = {key: value for key, value in round_payload.items() if key != "metadata"}
    body = json.dumps(outbound)

    # Browsing tools are appended last (test_the_browsing_tools_are_appended_last...
    # in the baseline); the collision pair is everything before them.
    offered = [entry["function"]["name"] for entry in outbound["tools"]]
    assert offered[: -len(EXPECTED_BROWSING_FUNCTIONS)] == ["baseline_toolkit__lookup", "crew__lookup"]

    assert "mod:" not in body
    assert "toolkit_id" not in body


# --- No shared structure between the payload and the specs ---------------------


@pytest.mark.tier0
def test_the_payload_shares_no_structure_with_the_specs_that_produced_it(drive):  # noqa: F811
    """`to_openai()` deep-copies its schema body on purpose: a spec read off a
    cached `Tools` row is shared structure, and handing its dict onward lets any
    downstream edit land back on the cache. Asserted on the live payload, for a
    user-authored and a mod tool alike -- the emitted `parameters` is a distinct
    object, and mutating the payload the way a provider client might leaves both
    the spec and the next request untouched."""
    captured = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)
    before = json.dumps(captured["openai_tools"]).encode()

    for name in ("lookup", "spawn"):
        spec = captured["admin_tools"][name]["spec"]
        entry = next(e for e in captured["openai_tools"] if e["function"]["name"] == name)
        assert entry["function"]["parameters"] is not spec.input_schema
        entry["function"]["parameters"]["properties"]["injected"] = {"type": "string"}
        assert "injected" not in spec.input_schema.get("properties", {}), f"{name}: the payload aliases its spec"

    again = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)
    assert json.dumps(again["openai_tools"]).encode() == before
