"""Baseline characterization: the exact tool payload the model receives.

T-002, `cavekit-toolspec-model.md` R4 criterion 6. This is a **before-picture**,
written against and passing on UNCONVERTED code (inner tool specs are still
plain dicts). T-016 re-runs these same scenarios against the converted code and
asserts the payload is byte-identical, so the constants and the
`expected_openai_tools()` helper below are the shared contract between the two
tasks. Nothing here constructs a tool spec by hand: every spec under test
travelled the real assembly path (`get_tools()` / `assemble_for_user()` /
`resolve_collisions()`) to reach its assertion. The literal dicts in this module
are the *expected* side of the comparison — the snapshot — never the input.

WHERE THIS CUTS INTO THE PATH
-----------------------------
The entry point is the real `middleware.generate_chat_completion_with_tools`
(`utils/middleware.py:608`), driven with:

* a real user-authored toolkit created through `POST /api/v1/tools/create`, so
  `tools.specs` is genuine `get_tools_specs()` / langchain
  `convert_to_openai_function()` output read back off the DB;
* a real mod load-result on `app.state.MODS`, assembled by the real
  `assemble_for_user()` / `assemble_mod_tools()` under the real
  `has_permission()` against `app.state.config.USER_PERMISSIONS`;
* a `SimpleNamespace(app=test_app)` stand-in for `Request` — the function only
  ever touches `request.app.state`.

So the following production code really runs: `get_tools()` (module load,
valves, the `__`-prefixed parameter strip at `utils/tools.py:62-64`, the name
read at `:66`), `resolve_collisions()` in both its call sites, both
`openai_tools` wrap sites (`middleware.py:679,681`), `WEB_SEARCH_TOOL_SPEC`
(`:137`), the buffered round payload built at `:433`, the streaming round
payload at `:502`, and — in the dispatch test — the argument filter at `:403`
and `_dispatch_tool_call`.

WHAT IS STUBBED
---------------
* `middleware.generate_chat_completion` — the outbound model call. It records
  each `round_payload` and returns a canned response. This is the only way to
  observe the request body without a model.
* `middleware._run_tool_calling_buffered` — wrapped by a pass-through spy that
  records `openai_tools` and `admin_tools` and then awaits the real runner. The
  real runner still executes; nothing about the payload is synthesised.
* `middleware.get_event_emitter` / `get_event_call` — replaced with no-op async
  callables. These are the websocket side-channel; they are not part of the tool
  payload, and the real emitter needs a live socket session plus a persisted
  chat row.

WHAT THIS LEAVES UNCOVERED
--------------------------
* The HTTP serialization inside `generate_chat_completion` itself (provider
  client, headers, the actual wire write). We pin the dict handed to it and that
  `json.dumps` of it succeeds with no `default=` hook.
* Rounds beyond the first, and the `MAX_TOOL_CALL_ROUNDS` exhaustion fallback.
* The streaming SSE relay past a single non-tool-calling round, and the
  streaming fallback path when the model rejects a tools array.
* Tool *execution* semantics beyond the one dispatch test (errors, async tools,
  the mod scope guard's denial branch — covered by `test_mods_tools.py`).
* Anything provider-specific: only the OpenAI-shaped payload exists today.
"""

import asyncio
import copy
import json
import types

import pytest

from selfai_ui.models.tools import Tools
from selfai_ui.models.users import Users
from selfai_ui.mods.tools import TOOL_NAME_PATTERN, ModTool, validate_mod_tools
from selfai_ui.utils import middleware

MODEL_ID = "baseline-model"
USER_TOOL_ID = "baseline_toolkit"

USER_TOOL_CONTENT = '''"""
title: BaselineToolkit
description: A user-authored toolkit used to characterize the tool payload.
"""


class Tools:
    def __init__(self):
        pass

    def lookup(self, query: str, __user__: dict = None) -> str:
        """
        Look a term up in the baseline index.

        :param query: The term to look up.
        """
        return f"looked up {query}"
'''

# --- The snapshot -------------------------------------------------------------
# The expected side of every comparison. Written out literally so that a change
# in what the model is offered fails HERE rather than passing silently.

EXPECTED_USER_FUNCTION = {
    "name": "lookup",
    "description": "Look a term up in the baseline index.",
    "parameters": {
        "properties": {"query": {"description": "The term to look up.", "type": "string"}},
        "required": ["query"],
        "type": "object",
    },
}

EXPECTED_WEB_SEARCH_FUNCTION = {
    "name": "web_search",
    "description": (
        "Search the web for current, real-time, or otherwise unfamiliar information that "
        "is not already available in this conversation. Use this only when the existing "
        "context is insufficient to answer accurately — do not use it for general "
        "knowledge, conversation, or anything already covered above."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A concise, targeted web search query."}},
        "required": ["query"],
    },
}

EXPECTED_WEB_FETCH_FUNCTION = {
    "name": "web_fetch",
    "description": (
        "Read the contents of one specific web page whose URL is already known — because "
        "the user gave it, or because it appeared in earlier results in this conversation. "
        "Use this when a particular page needs to be read; use web_search instead when you "
        "need to find out which page to read. Returns the page's text, not a summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full absolute URL of the page to read, including its scheme (https://).",
            }
        },
        "required": ["url"],
    },
}

EXPECTED_DEEP_RESEARCH_FUNCTION = {
    "name": "deep_research",
    "description": (
        "Research a topic in depth: search the web, read the most promising results, and "
        "follow relevant links from those pages to gather more detail. Use this for "
        "questions that need more than a quick answer — comparisons, how something works, "
        "gathering evidence from several sources. Use web_search instead for a quick "
        "factual lookup, and web_fetch when you already know the one page you need. This "
        "reads several pages and takes noticeably longer than a search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The research question or topic, phrased as a search query.",
            }
        },
        "required": ["query"],
    },
}

# What the Web Search toggle offers, in append order: both ways of getting a
# page (search for it, or name it), and NOT deep_research — that is its own
# toggle now (features.deep_research), because crawling is a different
# capability with a different cost. web_search's own snapshot above is
# deliberately unchanged, which is what keeps this file's original guarantee
# (ToolSpec altered no wire bytes) intact rather than merely relaxed.
EXPECTED_BROWSING_FUNCTIONS = [
    EXPECTED_WEB_SEARCH_FUNCTION,
    EXPECTED_WEB_FETCH_FUNCTION,
]

FUNCTION_KEYS = {"name", "description", "parameters"}
ENTRY_KEYS = {"type", "function"}


def _user_function(offered_as="lookup"):
    """The user-authored tool as the model should see it, under `offered_as`."""
    return {**copy.deepcopy(EXPECTED_USER_FUNCTION), "name": offered_as}


def _mod_function(declared, offered_as=None):
    """A mod tool as the model should see it. No-parameter tools get the
    empty-object schema `mods/tools.py:_no_param_schema()` produces."""
    return {
        "name": offered_as or declared,
        "description": f"{declared} mod tool",
        "parameters": {"type": "object", "properties": {}},
    }


# Each scenario names what to assemble and the exact functions the model must be
# offered, in order. `collision` marks the case where a user-authored tool and a
# mod tool share a name; `resolve_collisions` then qualifies BOTH with their
# owner. `collide_name` strips the `mod:` scheme prefix from a mod's
# `toolkit_id` before qualifying, so the mod's owner-half here is `crew`, not
# `mod:crew` -- collision qualifies both entries with a bare identifier, the
# same shape whichever kind of tool holds it (see
# test_a_collision_qualifies_both_names_with_their_owner).
SCENARIOS = {
    "user_only": {
        "user_tool": True,
        "mod_tool_names": (),
        "functions": [_user_function()],
        "collision": False,
    },
    "mod_only": {
        "user_tool": False,
        "mod_tool_names": ("spawn",),
        "functions": [_mod_function("spawn")],
        "collision": False,
    },
    "both": {
        "user_tool": True,
        "mod_tool_names": ("spawn",),
        "functions": [_user_function(), _mod_function("spawn")],
        "collision": False,
    },
    "collision": {
        "user_tool": True,
        "mod_tool_names": ("lookup",),
        "functions": [
            _user_function("baseline_toolkit__lookup"),
            _mod_function("lookup", offered_as="crew__lookup"),
        ],
        "collision": True,
    },
}

# `generate_chat_completion_with_tools` returns early -- straight to a plain
# completion with no `tools` key at all -- only when the user has nothing to be
# offered: no `tool_ids`, no mod tool they hold the scope for, and web search
# off. A scenario with any one of those runs the full assembly.
#
# The mod-only-with-web-search-off combination used to be excluded here, because
# the gate did not consult mod tools and the comparison would have compared two
# empty payloads (self.ai#61). It is included now that the gate is fixed, which
# is the point: that combination is the one a mod-provided capability is most
# likely to be used in.
CASES = [
    (name, web_search)
    for name in SCENARIOS
    for web_search in (False, True)
    if SCENARIOS[name]["user_tool"] or SCENARIOS[name]["mod_tool_names"] or web_search
]
CASE_IDS = [f"{name}-websearch_{'on' if ws else 'off'}" for name, ws in CASES]


def expected_openai_tools(scenario_name, *, web_search, deep_research=False):
    """The full `openai_tools` list for a scenario. Shared with T-016.

    Web Search offers the browsing set (search + fetch); Deep Research is a
    separate toggle appending its own tool after them."""
    functions = [copy.deepcopy(f) for f in SCENARIOS[scenario_name]["functions"]]
    if web_search:
        functions.extend(copy.deepcopy(f) for f in EXPECTED_BROWSING_FUNCTIONS)
    if deep_research:
        functions.append(copy.deepcopy(EXPECTED_DEEP_RESEARCH_FUNCTION))
    return [{"type": "function", "function": f} for f in functions]


def assert_wire_shape(openai_tools):
    """Every entry is a two-key wrapper around a three-key function object.
    Key sets asserted with `==`, so an added key fails."""
    for entry in openai_tools:
        assert set(entry) == ENTRY_KEYS, f"unexpected wrapper keys: {sorted(entry)}"
        assert entry["type"] == "function"
        assert set(entry["function"]) == FUNCTION_KEYS, f"unexpected function keys: {sorted(entry['function'])}"


def _mods_state(tool_names):
    """A LoadedMod-shaped `app.state.MODS` carrying real, validated ModTools."""
    tools = validate_mod_tools(
        [
            ModTool(
                name=name,
                description=f"{name} mod tool",
                handler=lambda **kwargs: {"ok": True},
                scope=f"mods.crew.{name}",
            )
            for name in tool_names
        ]
    )
    return types.SimpleNamespace(
        loaded={"crew": types.SimpleNamespace(tools=tools, manifest=types.SimpleNamespace(id="crew"))}
    )


class _StubStreamResponse:
    """What a streaming `generate_chat_completion` returns: an object exposing
    `body_iterator` (already-complete SSE lines) and `background`."""

    background = None

    def __init__(self, lines):
        self._lines = lines

    @property
    def body_iterator(self):
        async def _gen():
            for line in self._lines:
                yield line

        return _gen()


@pytest.fixture
def drive(test_app, authenticated_admin, test_admin, db_session, monkeypatch):
    """Run the real tool-assembly path and capture what it hands onward."""
    # Bound once, before any patching: a test that drives twice must not have
    # its second spy wrap its first (which would overwrite the first capture).
    real_buffered = middleware._run_tool_calling_buffered

    def _drive(
        *,
        user_tool=False,
        mod_tool_names=(),
        web_search=False,
        deep_research=False,
        stream=False,
        tool_calls=None,
        grant_scopes=True,
    ):
        tool_ids = []
        if user_tool:
            # Created through the real router so `tools.specs` is genuine
            # `get_tools_specs()` output. Guarded because a test may drive twice
            # and the create endpoint rejects a duplicate id.
            if Tools.get_tool_by_id(USER_TOOL_ID) is None:
                resp = authenticated_admin.post(
                    "/api/v1/tools/create",
                    json={
                        "id": USER_TOOL_ID,
                        "name": "Baseline Toolkit",
                        "content": USER_TOOL_CONTENT,
                        "meta": {"description": "baseline"},
                        "access_control": None,
                    },
                )
                assert resp.status_code == 200, resp.text[:400]
            tool_ids = [USER_TOOL_ID]

        monkeypatch.setattr(
            test_app.state, "MODELS", {MODEL_ID: {"id": MODEL_ID, "owned_by": "llamolotl"}}, raising=False
        )
        monkeypatch.setattr(test_app.state, "MODS", _mods_state(mod_tool_names), raising=False)
        permissions = dict(test_app.state.config.USER_PERMISSIONS or {})
        # `grant_scopes=False` installs the mod but withholds the grant, which is
        # how a scope-gated tool is made invisible to this user without changing
        # what is loaded.
        permissions["mods"] = {"crew": dict.fromkeys(mod_tool_names, grant_scopes)}
        monkeypatch.setattr(test_app.state.config, "USER_PERMISSIONS", permissions)

        captured = {"round_payloads": []}
        rounds = {"n": 0}

        async def spy_buffered(request, form_data, messages, openai_tools, admin_tools, extra_params, user, **kwargs):
            captured["openai_tools"] = openai_tools
            captured["admin_tools"] = admin_tools
            return await real_buffered(
                request, form_data, messages, openai_tools, admin_tools, extra_params, user, **kwargs
            )

        async def fake_completion(request, payload, user, **kwargs):
            captured["round_payloads"].append(payload)
            rounds["n"] += 1
            if payload.get("stream"):
                return _StubStreamResponse(['data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', "data: [DONE]\n\n"])
            if tool_calls and rounds["n"] == 1:
                return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(middleware, "_run_tool_calling_buffered", spy_buffered)
        monkeypatch.setattr(middleware, "generate_chat_completion", fake_completion)
        monkeypatch.setattr(middleware, "get_event_emitter", lambda *a, **k: _noop)
        monkeypatch.setattr(middleware, "get_event_call", lambda *a, **k: _noop)

        request = types.SimpleNamespace(app=test_app)
        user = Users.get_user_by_id(test_admin["id"])
        form_data = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
            "metadata": {
                "tool_ids": tool_ids,
                "features": {"web_search": web_search, "deep_research": deep_research},
            },
        }

        async def _main():
            result = await middleware.generate_chat_completion_with_tools(request, form_data, user)
            if hasattr(result, "body_iterator"):
                captured["stream_chunks"] = [chunk async for chunk in result.body_iterator]
            else:
                captured["result"] = result

        asyncio.run(_main())
        return captured

    return _drive


# --- The payload the model is offered -----------------------------------------


@pytest.mark.tier0
@pytest.mark.parametrize(("scenario", "web_search"), CASES, ids=CASE_IDS)
def test_the_openai_tools_payload_matches_the_baseline(drive, scenario, web_search):
    """The exact list handed to the round-runner, for every combination of
    user-authored tool, mod tool, name collision, and web search."""
    case = SCENARIOS[scenario]
    captured = drive(user_tool=case["user_tool"], mod_tool_names=case["mod_tool_names"], web_search=web_search)

    assert captured["openai_tools"] == expected_openai_tools(scenario, web_search=web_search)
    assert_wire_shape(captured["openai_tools"])


@pytest.mark.tier0
@pytest.mark.parametrize(("scenario", "web_search"), CASES, ids=CASE_IDS)
def test_the_serialized_request_body_matches_the_baseline(drive, scenario, web_search):
    """The body built at `middleware.py:433` serializes with a plain
    `json.dumps` — no `default=` hook — and its `tools` array is the payload."""
    case = SCENARIOS[scenario]
    captured = drive(user_tool=case["user_tool"], mod_tool_names=case["mod_tool_names"], web_search=web_search)
    [round_payload] = captured["round_payloads"]

    assert round_payload["tools"] == expected_openai_tools(scenario, web_search=web_search)
    assert round_payload["tool_choice"] == "auto"
    assert round_payload["stream"] is False
    assert round_payload["model"] == MODEL_ID

    body = json.dumps(round_payload)
    assert json.loads(body)["tools"] == expected_openai_tools(scenario, web_search=web_search)


@pytest.mark.tier0
def test_the_tools_array_alone_is_json_serializable_with_no_default_hook(drive):
    """`json.dumps` on the payload with no `default=` — the round-trip must be
    lossless, i.e. the payload is plain dicts/lists/strings all the way down."""
    captured = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)
    tools = captured["openai_tools"]
    assert json.loads(json.dumps(tools)) == tools


# --- No origin marker ---------------------------------------------------------


@pytest.mark.tier0
@pytest.mark.parametrize("scenario", [n for n in SCENARIOS if not SCENARIOS[n]["collision"]])
def test_the_model_sees_no_toolkit_or_mod_origin(drive, scenario):
    """Nothing that reaches the model names a toolkit or a mod. The outer
    six-key dict carries `toolkit_id` (`mod:crew` for mod tools); none of it is
    wrapped into `openai_tools`.

    Still scoped to the non-collision scenarios, but no longer because a mod
    marker would leak there -- that is fixed (see
    `test_a_collision_qualifies_both_names_with_the_same_shape` below). It is
    scoped here because the fixture's OWN user-authored toolkit id,
    `baseline_toolkit`, contains the substring "toolkit" -- so a collision
    naturally puts that substring in the payload for entirely unrelated
    reasons, on the user-authored side, regardless of mod involvement. That is
    a property of this fixture's naming, not something to assert against."""
    case = SCENARIOS[scenario]
    captured = drive(user_tool=case["user_tool"], mod_tool_names=case["mod_tool_names"], web_search=True)
    serialized = json.dumps(captured["openai_tools"])

    assert "toolkit_id" not in serialized
    assert "mod:" not in serialized
    assert "toolkit" not in serialized.lower()
    assert "callable" not in serialized and "pydantic_model" not in serialized
    assert "file_handler" not in serialized and "citation" not in serialized


@pytest.mark.tier0
def test_a_mod_tool_and_a_user_tool_are_indistinguishable_to_the_model(drive):
    """Same wrapper, same function key set, nothing marking either as a mod or
    a user-authored toolkit. Whichever produced it, the model sees one shape."""
    captured = drive(user_tool=True, mod_tool_names=("spawn",), web_search=False)
    user_entry, mod_entry = captured["openai_tools"]

    assert set(user_entry) == set(mod_entry) == ENTRY_KEYS
    assert set(user_entry["function"]) == set(mod_entry["function"]) == FUNCTION_KEYS
    assert user_entry["type"] == mod_entry["type"] == "function"
    # The outer dict DOES know the origin -- it just never reaches the payload.
    assert captured["admin_tools"]["spawn"]["toolkit_id"] == "mod:crew"
    assert captured["admin_tools"]["lookup"]["toolkit_id"] == USER_TOOL_ID


# --- Collisions ---------------------------------------------------------------


@pytest.mark.tier0
def test_a_collision_qualifies_both_names_with_their_owner(drive):
    """F-001's shape, pinned. `resolve_collisions` renames EVERY holder of a
    shared name to `<owner>__<name>` and rewrites `spec["name"]` to match, so
    the name the model is offered is a dispatchable key.

    The mod's owner-half is `crew`, not `mod:crew`: `collide_name` strips the
    `mod:` scheme prefix before qualifying (fixed alongside this test -- it
    used to leak that prefix verbatim). See
    `test_a_collision_qualifies_both_names_with_the_same_shape` for the
    property that fix is actually for."""
    captured = drive(user_tool=True, mod_tool_names=("lookup",), web_search=False)

    assert list(captured["admin_tools"]) == ["baseline_toolkit__lookup", "crew__lookup"]
    offered = [entry["function"]["name"] for entry in captured["openai_tools"]]
    assert offered == ["baseline_toolkit__lookup", "crew__lookup"]
    # Every offered name is a dispatch key -- no "Unknown tool" gap.
    assert offered == list(captured["admin_tools"])
    assert captured["openai_tools"] == expected_openai_tools("collision", web_search=False)


@pytest.mark.tier0
def test_a_collision_qualifies_both_names_with_the_same_shape(drive):
    """The property the `mod:`-stripping fix exists for. Before it, a colliding
    mod tool was offered as `mod:crew__lookup` -- a literal, unconditional
    watermark that told the model (and anyone reading a transcript) a mod was
    involved, in a case the module's own "no origin marker" contract did not
    carve out as an exception. Now both qualified names have the identical
    shape: a bare identifier, `__`, the tool name. Nothing about the string
    tells you which one came from a mod."""
    captured = drive(user_tool=True, mod_tool_names=("lookup",), web_search=False)
    offered = [entry["function"]["name"] for entry in captured["openai_tools"]]

    assert offered == ["baseline_toolkit__lookup", "crew__lookup"]
    for name in offered:
        assert TOOL_NAME_PATTERN.match(name), name
        owner_half = name.split("__", 1)[0]
        assert "mod" not in owner_half, f"{owner_half!r} still tags its kind"


@pytest.mark.tier0
def test_a_collision_leaves_the_bare_name_unoffered(drive):
    """Neither holder keeps the bare name, and no tool is dropped."""
    captured = drive(user_tool=True, mod_tool_names=("lookup",), web_search=False)
    offered = [entry["function"]["name"] for entry in captured["openai_tools"]]

    assert "lookup" not in offered
    assert len(offered) == 2


# --- Web search ---------------------------------------------------------------


@pytest.mark.tier0
def test_the_browsing_tools_are_appended_last_and_only_when_enabled(drive):
    """The browsing wrap sites come after every admin/mod tool, and the Web
    Search set appears or disappears together with its toggle."""
    off = drive(user_tool=True, mod_tool_names=("spawn",), web_search=False)
    on = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True)

    assert [e["function"]["name"] for e in off["openai_tools"]] == ["lookup", "spawn"]
    assert [e["function"]["name"] for e in on["openai_tools"]] == [
        "lookup",
        "spawn",
        "web_search",
        "web_fetch",
    ]
    assert on["openai_tools"][-len(EXPECTED_BROWSING_FUNCTIONS) :] == [
        {"type": "function", "function": f} for f in EXPECTED_BROWSING_FUNCTIONS
    ]


@pytest.mark.tier0
def test_deep_research_is_its_own_toggle_separate_from_web_search(drive):
    """Deep Research is gated on features.deep_research, not folded into Web
    Search. Web Search on / Deep Research off must NOT offer deep_research, and
    Deep Research on offers exactly it, appended after any browsing tools."""
    ws_only = drive(user_tool=False, mod_tool_names=(), web_search=True, deep_research=False)
    assert [e["function"]["name"] for e in ws_only["openai_tools"]] == ["web_search", "web_fetch"]

    dr_only = drive(user_tool=False, mod_tool_names=(), web_search=False, deep_research=True)
    assert [e["function"]["name"] for e in dr_only["openai_tools"]] == ["deep_research"]

    both = drive(user_tool=False, mod_tool_names=(), web_search=True, deep_research=True)
    assert [e["function"]["name"] for e in both["openai_tools"]] == [
        "web_search",
        "web_fetch",
        "deep_research",
    ]


@pytest.mark.tier0
def test_deep_research_alone_opens_the_tool_calling_path(drive):
    """Its toggle must open the fall-through gate on its own — a user with only
    Deep Research on, everything else off, still gets tools offered."""
    captured = drive(user_tool=False, mod_tool_names=(), web_search=False, deep_research=True)

    assert "openai_tools" in captured, "deep_research alone must open the gate"
    assert [e["function"]["name"] for e in captured["openai_tools"]] == ["deep_research"]


@pytest.mark.tier0
def test_mod_tools_alone_open_the_tool_calling_path(drive):
    """self.ai#61. This test previously pinned the exact opposite, deliberately.

    The gate was `not tool_ids and not web_search_enabled` and never consulted
    mod tools, so a user holding only mod tools, web search off, fell through to
    a plain completion carrying no `tools` key at all. Their mod's tools worked
    solely as a side effect of also having selected an unrelated user-authored
    toolkit -- which is the opposite of why a mod exists, and is precisely the
    configuration self.crew ships into."""
    captured = drive(user_tool=False, mod_tool_names=("spawn",), web_search=False)

    assert "openai_tools" in captured, "the round-runner must be reached -- the gate opened"
    assert [t["function"]["name"] for t in captured["openai_tools"]] == ["spawn"]
    assert_wire_shape(captured["openai_tools"])

    [payload] = captured["round_payloads"]
    assert [t["function"]["name"] for t in payload["tools"]] == ["spawn"]


@pytest.mark.tier0
def test_a_mod_tool_the_user_lacks_the_scope_for_does_not_open_the_path(drive):
    """The gate opens on tools the user may actually call, not on a mod merely
    being installed. `assemble_for_user` is scope-gated, so a user without the
    grant assembles nothing and still takes the cheap path. The fix must not
    turn "a mod is enabled" into "every request pays for tool calling"."""
    captured = drive(user_tool=False, mod_tool_names=("spawn",), web_search=False, grant_scopes=False)

    assert "openai_tools" not in captured, "no callable tool: the gate must stay shut"
    [payload] = captured["round_payloads"]
    assert "tools" not in payload


@pytest.mark.tier0
def test_no_tools_at_all_still_falls_straight_through(drive):
    """The common case stays free. No toolkits, no mods, no web search: one
    plain completion, no assembly, no `tools` key."""
    captured = drive(user_tool=False, mod_tool_names=(), web_search=False)

    assert "openai_tools" not in captured
    [payload] = captured["round_payloads"]
    assert "tools" not in payload
    assert "tool_choice" not in payload


@pytest.mark.tier0
def test_the_browsing_tools_alone_are_the_whole_payload(drive):
    """No tool ids and no mods, browsing on: the model is offered exactly the
    browsing set, and the assembly path still ran (it did not short-circuit)."""
    captured = drive(user_tool=False, mod_tool_names=(), web_search=True)

    assert captured["openai_tools"] == [{"type": "function", "function": f} for f in EXPECTED_BROWSING_FUNCTIONS]
    assert captured["admin_tools"] == {}


# --- The streaming round payload ----------------------------------------------


@pytest.mark.tier0
def test_the_streaming_round_payload_carries_the_same_tools(drive):
    """`_stream_tool_calling` builds its own round payload (`middleware.py:502`)
    from the same `openai_tools` list. The two must not drift."""
    captured = drive(user_tool=True, mod_tool_names=("spawn",), web_search=True, stream=True)
    [round_payload] = captured["round_payloads"]

    assert round_payload["tools"] == expected_openai_tools("both", web_search=True)
    assert round_payload["stream"] is True
    assert_wire_shape(round_payload["tools"])
    assert json.loads(json.dumps(round_payload))["tools"] == round_payload["tools"]


# --- Dispatch, so the argument filter is on this harness's path ----------------


@pytest.mark.tier0
def test_a_tool_call_dispatches_through_the_argument_filter(drive):
    """`middleware.py:403` reads the spec's declared properties and drops any
    argument the model invented. The offered name is dispatchable, the declared
    argument is passed, and the undeclared one is filtered rather than raising.

    T-013 rewrites that read; this test is what proves it kept its behaviour."""
    captured = drive(
        user_tool=True,
        mod_tool_names=(),
        web_search=False,
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query": "widgets", "invented": "x"}'},
            }
        ],
    )

    first, second = captured["round_payloads"]
    assert first["tools"] == expected_openai_tools("user_only", web_search=False)
    tool_message = second["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"] == "looked up widgets"
    # The second round still offers the identical payload.
    assert second["tools"] == first["tools"]
