"""Client-supplied tools survive the request and are dispatched by the caller.

self.ai#71. `chat/completions` used to build `openai_tools` purely from the
server side and splat it over `form_data`, so an OpenAI-compatible client that
sent its own `tools` had them silently replaced -- no error, no warning -- and
self.ai's own `web_search` could never be offered in the same session as a
client's `bash`. These tests pin the merged behaviour from both ends: what the
model is offered, and who executes what comes back.

WHERE THIS CUTS INTO THE PATH
-----------------------------
Same harness shape as `test_toolspec_payload.py`, and deliberately so: the entry
point is the real `middleware.generate_chat_completion_with_tools`, driven with
a real mod load-result assembled by the real `assemble_for_user()` under the
real `has_permission()`. The real round-runners execute -- both the buffered and
the streaming one -- so the partition, the deferral stub, the SSE re-emission,
and the collision qualification are all production code here.

WHAT IS STUBBED
---------------
* `middleware.generate_chat_completion` -- the outbound model call. Records each
  round payload and returns a scripted response per round, which is how a
  multi-round conversation is expressed without a model.
* `middleware.get_event_emitter` / `get_event_call` -- the websocket
  side-channel, which needs a live socket session and a persisted chat row.

WHAT THIS LEAVES UNCOVERED
--------------------------
* The HTTP serialization inside `generate_chat_completion` itself.
* `MAX_TOOL_CALL_ROUNDS` exhaustion with client tools present.
* Whether a real model honours the deferral stub's instruction to re-issue a
  client call on its own. The server behaves correctly either way -- a model
  that keeps mixing simply burns rounds -- but the happy path is a model
  behaviour, not a server one, and is not asserted here.
"""

import asyncio
import json
import types

import pytest

from selfai_ui.models.users import Users
from selfai_ui.mods.tools import ModTool, validate_mod_tools
from selfai_ui.utils import middleware

MODEL_ID = "client-tools-model"

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


def _call(name, call_id, arguments="{}"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _offered_names(openai_tools):
    return [entry["function"]["name"] for entry in openai_tools]


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


def _sse(delta):
    """One SSE line, in the envelope a real round carries."""
    chunk = {"id": "cmpl-1", "model": MODEL_ID, "choices": [{"index": 0, "delta": delta}]}
    return f"data: {json.dumps(chunk)}\n\n"


def _sse_tool_call_round(tool_calls, *, content=None):
    """The SSE lines a model emits for one round that ends in tool_calls."""
    lines = []
    if content:
        lines.append(_sse({"content": content}))
    for index, call in enumerate(tool_calls):
        lines.append(
            _sse({"tool_calls": [{"index": index, "id": call["id"], "type": "function", "function": call["function"]}]})
        )
    lines.append("data: [DONE]\n\n")
    return lines


@pytest.fixture
def drive(test_app, test_admin, monkeypatch):
    """Run the real merge + round-runner path over a scripted model."""

    def _drive(
        *,
        client_tools=(),
        mod_tool_names=("spawn",),
        web_search=False,
        stream=False,
        rounds=(),
        tool_choice=None,
    ):
        monkeypatch.setattr(
            test_app.state, "MODELS", {MODEL_ID: {"id": MODEL_ID, "owned_by": "llamolotl"}}, raising=False
        )
        monkeypatch.setattr(test_app.state, "MODS", _mods_state(mod_tool_names), raising=False)
        permissions = dict(test_app.state.config.USER_PERMISSIONS or {})
        permissions["mods"] = {"crew": dict.fromkeys(mod_tool_names, True)}
        monkeypatch.setattr(test_app.state.config, "USER_PERMISSIONS", permissions)

        captured = {"round_payloads": []}
        counter = {"n": 0}

        async def fake_completion(request, payload, user, **kwargs):
            captured["round_payloads"].append(payload)
            index = counter["n"]
            counter["n"] += 1
            scripted = rounds[index] if index < len(rounds) else None

            if payload.get("stream"):
                if scripted:
                    return _StubStreamResponse(_sse_tool_call_round(scripted))
                return _StubStreamResponse([_sse({"content": "done"}), "data: [DONE]\n\n"])

            if scripted:
                return {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": None, "tool_calls": scripted},
                        }
                    ]
                }
            answer = {"role": "assistant", "content": "done"}
            return {"choices": [{"index": 0, "finish_reason": "stop", "message": answer}]}

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(middleware, "generate_chat_completion", fake_completion)
        monkeypatch.setattr(middleware, "get_event_emitter", lambda *a, **k: _noop)
        monkeypatch.setattr(middleware, "get_event_call", lambda *a, **k: _noop)

        request = types.SimpleNamespace(app=test_app)
        user = Users.get_user_by_id(test_admin["id"])
        form_data = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
            "metadata": {"tool_ids": [], "features": {"web_search": web_search}},
        }
        if client_tools:
            form_data["tools"] = list(client_tools)
        if tool_choice is not None:
            form_data["tool_choice"] = tool_choice

        async def _main():
            result = await middleware.generate_chat_completion_with_tools(request, form_data, user)
            if hasattr(result, "body_iterator"):
                captured["stream_chunks"] = [chunk async for chunk in result.body_iterator]
            else:
                captured["result"] = result

        asyncio.run(_main())
        return captured

    return _drive


# --- What the model is offered ------------------------------------------------


@pytest.mark.tier0
def test_client_tools_are_merged_not_substituted(drive):
    """The bug as filed: a client's `bash` reached the model not at all. It is
    now offered alongside the mod's tool, with its own spelling."""
    captured = drive(client_tools=[BASH_TOOL, READ_TOOL])
    [round_payload] = captured["round_payloads"]

    assert _offered_names(round_payload["tools"]) == ["spawn", "bash", "read"]
    # Verbatim: the caller's entry is the object it sent, not a re-serialization.
    assert round_payload["tools"][1] == BASH_TOOL


@pytest.mark.tier0
def test_client_tools_coexist_with_the_web_tools(drive):
    """The follow-up half of #71: web_search and a client's tools in one
    request. Before, the choice was binary."""
    captured = drive(client_tools=[BASH_TOOL], web_search=True)
    [round_payload] = captured["round_payloads"]

    assert _offered_names(round_payload["tools"]) == ["spawn", "web_search", "web_fetch", "bash"]


@pytest.mark.tier0
def test_a_request_with_no_client_tools_is_unchanged(drive):
    """The WebUI's own path must not shift. No `tools` key, so nothing merges
    and `tool_choice` stays the "auto" it has always been."""
    captured = drive(client_tools=(), web_search=True)
    [round_payload] = captured["round_payloads"]

    assert _offered_names(round_payload["tools"]) == ["spawn", "web_search", "web_fetch"]
    assert round_payload["tool_choice"] == "auto"


@pytest.mark.tier0
def test_a_stated_tool_choice_is_honoured(drive):
    """`tool_choice` used to be overwritten with "auto" by the same splat."""
    captured = drive(client_tools=[BASH_TOOL], tool_choice="required")
    [round_payload] = captured["round_payloads"]

    assert round_payload["tool_choice"] == "required"


@pytest.mark.tier0
def test_a_malformed_client_tool_is_dropped_not_forwarded(drive):
    """A nameless entry would make a provider reject the whole request, taking
    the well-formed tools with it."""
    captured = drive(client_tools=[BASH_TOOL, {"type": "function", "function": {}}, "nonsense"])
    [round_payload] = captured["round_payloads"]

    assert _offered_names(round_payload["tools"]) == ["spawn", "bash"]


# --- Collisions: the client's spelling wins -----------------------------------


@pytest.mark.tier0
def test_a_client_name_beats_a_mod_tool_of_the_same_name(drive):
    """Both survive. The client keeps `spawn` bare -- it dispatches by name --
    and the mod's is qualified by its owner, the same way a mod/user collision
    already resolves."""
    client_spawn = {"type": "function", "function": {"name": "spawn", "description": "client spawn"}}
    captured = drive(client_tools=[client_spawn], mod_tool_names=("spawn",))
    [round_payload] = captured["round_payloads"]

    assert _offered_names(round_payload["tools"]) == ["crew__spawn", "spawn"]


@pytest.mark.tier0
def test_a_client_name_beats_a_builtin_and_the_builtin_still_dispatches(drive):
    """A client that brings its own `web_search` gets the name; ours is offered
    as `selfai__web_search` and must still route to the real web search when the
    model calls it."""
    client_search = {"type": "function", "function": {"name": "web_search", "description": "client search"}}
    dispatched = {}

    async def fake_web_search(request, query, extra_params, user):
        dispatched["query"] = query
        return "server search result"

    original = middleware.run_web_search_tool_call
    middleware.run_web_search_tool_call = fake_web_search
    try:
        captured = drive(
            client_tools=[client_search],
            web_search=True,
            rounds=[[_call("selfai__web_search", "call_1", '{"query": "kernel"}')]],
        )
    finally:
        middleware.run_web_search_tool_call = original

    first, second = captured["round_payloads"]
    assert "selfai__web_search" in _offered_names(first["tools"])
    assert "web_search" in _offered_names(first["tools"])
    assert dispatched["query"] == "kernel"
    assert second["messages"][-1]["content"] == "server search result"


# --- Who executes what ---------------------------------------------------------


@pytest.mark.tier0
def test_a_client_only_round_is_returned_unresolved(drive):
    """The whole point: self.ai cannot run the caller's `bash`, so the round is
    handed back with finish_reason=tool_calls and the loop stops."""
    captured = drive(
        client_tools=[BASH_TOOL],
        rounds=[[_call("bash", "call_1", '{"command": "echo HI"}')]],
    )

    # One round only -- no second call to the model.
    assert len(captured["round_payloads"]) == 1
    choice = captured["result"]["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == [_call("bash", "call_1", '{"command": "echo HI"}')]


@pytest.mark.tier0
def test_an_unknown_name_is_still_an_unknown_tool(drive):
    """A name in neither set is not the caller's just because it is not ours.
    It dispatches server-side and gets the existing refusal."""
    captured = drive(
        client_tools=[BASH_TOOL],
        rounds=[[_call("invented", "call_1")]],
    )

    _, second = captured["round_payloads"]
    assert second["messages"][-1]["content"] == "Unknown tool: invented"


@pytest.mark.tier0
def test_a_mixed_round_resolves_ours_and_defers_theirs(drive):
    """A half-resolved round cannot be returned -- the caller would owe a result
    for a tool it cannot run -- so the server call executes and the client call
    comes back as a deferral the model can act on."""
    dispatched = {}

    async def fake_web_search(request, query, extra_params, user):
        dispatched["query"] = query
        return "server search result"

    original = middleware.run_web_search_tool_call
    middleware.run_web_search_tool_call = fake_web_search
    try:
        captured = drive(
            client_tools=[BASH_TOOL],
            web_search=True,
            rounds=[
                [_call("web_search", "call_1", '{"query": "kernel"}'), _call("bash", "call_2", '{"command": "ls"}')],
                [_call("bash", "call_3", '{"command": "ls"}')],
            ],
        )
    finally:
        middleware.run_web_search_tool_call = original

    assert dispatched["query"] == "kernel"

    # Round 2 carries both results: ours real, theirs a deferral.
    second = captured["round_payloads"][1]
    tool_messages = [m for m in second["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_1", "call_2"]
    assert tool_messages[0]["content"] == "server search result"
    assert tool_messages[1]["content"] == middleware.CLIENT_TOOL_DEFERRAL

    # The model re-issues the client call alone, and THAT round is returnable.
    assert captured["result"]["choices"][0]["finish_reason"] == "tool_calls"
    assert captured["result"]["choices"][0]["message"]["tool_calls"][0]["id"] == "call_3"


# --- Streaming -----------------------------------------------------------------


@pytest.mark.tier0
def test_a_client_only_round_ends_the_stream_with_tool_calls(drive):
    """Streaming suppresses tool_calls deltas on the way past, so a client-owned
    round has to be re-emitted or the caller sees an empty answer -- which is
    exactly what crew-code saw."""
    captured = drive(
        client_tools=[BASH_TOOL],
        stream=True,
        rounds=[[_call("bash", "call_1", '{"command": "echo HI"}')]],
    )

    assert len(captured["round_payloads"]) == 1
    chunks = [json.loads(c[len("data: ") :]) for c in captured["stream_chunks"] if not c.startswith("data: [DONE]")]
    assert captured["stream_chunks"][-1] == "data: [DONE]\n\n"

    delta_chunk, finish_chunk = chunks[-2], chunks[-1]
    emitted = delta_chunk["choices"][0]["delta"]["tool_calls"]
    assert [c["function"]["name"] for c in emitted] == ["bash"]
    assert emitted[0]["index"] == 0
    assert emitted[0]["function"]["arguments"] == '{"command": "echo HI"}'
    assert finish_chunk["choices"][0]["finish_reason"] == "tool_calls"
    # Stamped with the same envelope the round carried, not invented.
    assert delta_chunk["id"] == "cmpl-1"
    assert delta_chunk["model"] == MODEL_ID


@pytest.mark.tier0
def test_streaming_still_resolves_a_server_only_round(drive):
    """The existing streaming behaviour is untouched when the caller's tools are
    present but the model calls ours."""
    original = middleware.run_web_search_tool_call

    async def fake_web_search(request, query, extra_params, user):
        return "server search result"

    middleware.run_web_search_tool_call = fake_web_search
    try:
        captured = drive(
            client_tools=[BASH_TOOL],
            web_search=True,
            stream=True,
            rounds=[[_call("web_search", "call_1", '{"query": "kernel"}')]],
        )
    finally:
        middleware.run_web_search_tool_call = original

    # Two rounds: the tool round, then the answer round that streamed live.
    assert len(captured["round_payloads"]) == 2
    assert captured["round_payloads"][1]["messages"][-1]["content"] == "server search result"
    assert "".join(captured["stream_chunks"]).count("done") == 1
