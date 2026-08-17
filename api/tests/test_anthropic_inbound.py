# test_anthropic_inbound.py
#
# Covers the INBOUND Anthropic Messages adapters (self.ai#112) -- the direction where
# self.ai *is* the provider and an Anthropic-native client (claude-code, crew-code,
# the Anthropic SDKs) is the caller.
#
# The sibling file test_anthropic_conversion.py covers the outbound direction. These
# are mirrors, so several tests here assert a round trip through both: a drift between
# the two shows up as a request that survives one hop and not the other, which is
# exactly the failure that is hardest to read from a client-side error.

import asyncio
import json

import pytest

from selfai_ui.utils.payload import (
    convert_messages_anthropic_to_openai,
    convert_messages_openai_to_anthropic,
    convert_payload_anthropic_to_openai,
    convert_tool_choice_anthropic_to_openai,
    convert_tools_anthropic_to_openai,
)
from selfai_ui.utils.response import (
    convert_response_openai_to_anthropic,
    convert_streaming_response_openai_to_anthropic,
)


class FakeStreamingResponse:
    """Yields the SSE blob in small chunks that deliberately split mid-line.

    Same rationale as the outbound test file: the upstream body iterator does not
    guarantee line-aligned chunks, so the converter must buffer and reassemble.
    Feeding it whole lines would not exercise that.
    """

    def __init__(self, lines, chunk_size=17):
        self._blob = ("\n".join(lines) + "\n").encode()
        self._chunk_size = chunk_size

    @property
    def body_iterator(self):
        async def gen():
            for i in range(0, len(self._blob), self._chunk_size):
                yield self._blob[i : i + self._chunk_size]

        return gen()


def collect(streaming_response, model=None):
    """Drive the async generator to completion synchronously.

    pytest-asyncio is not a dependency here; driving the loop directly matches the
    outbound test file, and a bare @pytest.mark.asyncio would silently skip.
    """

    async def run():
        return [
            frame async for frame in convert_streaming_response_openai_to_anthropic(streaming_response, model=model)
        ]

    return asyncio.run(run())


def events_from(frames):
    """Parse Anthropic SSE frames into (event_name, payload) pairs.

    Asserting on the `event:` name and not just the payload is deliberate -- the SDKs
    dispatch on it, so a stream that carries the right JSON under a missing or wrong
    event name is broken in a way curl will not show.
    """
    parsed = []
    for frame in frames:
        event_name = None
        data = None
        for line in frame.strip().split("\n"):
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
        parsed.append((event_name, data))
    return parsed


def openai_chunk(delta=None, finish_reason=None, usage=None, model="local-model"):
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage:
        chunk["usage"] = usage
    return chunk


def sse(chunks):
    return [f"data: {json.dumps(chunk)}" for chunk in chunks] + ["data: [DONE]"]


##########################################
# Request: messages
##########################################


def test_system_field_becomes_a_leading_system_message():
    messages = convert_messages_anthropic_to_openai(
        [{"role": "user", "content": "hi"}],
        system="be terse",
    )

    assert messages[0] == {"role": "system", "content": "be terse"}
    assert [m["role"] for m in messages] == ["system", "user"]


def test_system_block_list_is_joined_with_a_blank_line():
    # Mirrors how the outbound direction splits a system prompt apart, so a prompt
    # that makes a round trip comes back byte-identical.
    messages = convert_messages_anthropic_to_openai(
        [{"role": "user", "content": "hi"}],
        system=[{"type": "text", "text": "be terse"}, {"type": "text", "text": "cite sources"}],
    )

    assert messages[0]["content"] == "be terse\n\ncite sources"


def test_system_prompt_survives_a_round_trip_through_both_adapters():
    original = [
        {"role": "system", "content": "be terse"},
        {"role": "system", "content": "cite sources"},
        {"role": "user", "content": "hi"},
    ]
    anthropic_messages, system = convert_messages_openai_to_anthropic(original)
    back = convert_messages_anthropic_to_openai(anthropic_messages, system)

    assert back == [
        {"role": "system", "content": "be terse\n\ncite sources"},
        {"role": "user", "content": "hi"},
    ]


def test_string_content_is_accepted_as_well_as_blocks():
    # The Messages API allows a bare string for content; a client that sends one must
    # not be treated as having sent no content at all.
    assert convert_messages_anthropic_to_openai([{"role": "user", "content": "hi"}]) == [
        {"role": "user", "content": "hi"}
    ]


def test_text_only_user_turn_collapses_to_a_plain_string():
    messages = convert_messages_anthropic_to_openai(
        [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]
    )

    assert messages[0] == {"role": "user", "content": "ab"}


def test_base64_image_becomes_a_data_url_part():
    messages = convert_messages_anthropic_to_openai(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}},
                ],
            }
        ]
    )

    assert messages[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,QUJD"},
    }


def test_url_image_source_passes_through():
    messages = convert_messages_anthropic_to_openai(
        [{"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}]
    )

    assert messages[0]["content"] == [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]


def test_image_round_trips_through_both_adapters():
    original = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
            ],
        }
    ]
    anthropic_messages, system = convert_messages_openai_to_anthropic(original)

    assert convert_messages_anthropic_to_openai(anthropic_messages, system) == original


def test_assistant_tool_use_becomes_openai_tool_calls():
    messages = convert_messages_anthropic_to_openai(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
                ],
            }
        ]
    )

    assert messages[0]["content"] == "checking"
    assert messages[0]["tool_calls"] == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})},
        }
    ]


def test_tool_results_become_tool_role_messages_before_the_user_text():
    # Anthropic carries tool results on a user turn; OpenAI wants each as its own
    # `tool` message. Ordering matters -- the results must sit adjacent to the
    # assistant turn that requested them, not after unrelated user prose.
    messages = convert_messages_anthropic_to_openai(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "18C"},
                    {"type": "tool_result", "tool_use_id": "toolu_2", "content": "rain"},
                    {"type": "text", "text": "thanks"},
                ],
            }
        ]
    )

    assert messages == [
        {"role": "tool", "tool_call_id": "toolu_1", "content": "18C"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": "rain"},
        {"role": "user", "content": "thanks"},
    ]


def test_tool_result_block_list_content_is_flattened_to_text():
    messages = convert_messages_anthropic_to_openai(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "18C"}],
                    }
                ],
            }
        ]
    )

    assert messages == [{"role": "tool", "tool_call_id": "toolu_1", "content": "18C"}]


def test_thinking_blocks_are_dropped_not_replayed_as_content():
    # A thinking block is the upstream model's reasoning. Echoing it back as visible
    # assistant content would put reasoning into the prompt as if it had been said.
    messages = convert_messages_anthropic_to_openai(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "text", "text": "42"},
                ],
            }
        ]
    )

    assert messages == [{"role": "assistant", "content": "42"}]


def test_empty_assistant_turn_is_dropped_entirely():
    assert convert_messages_anthropic_to_openai([{"role": "assistant", "content": []}]) == []


##########################################
# Request: tools and payload
##########################################


def test_tools_are_unflattened_into_the_openai_function_wrapper():
    tools = convert_tools_anthropic_to_openai(
        [
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ]
    )

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]


def test_server_side_tools_are_skipped():
    # web_search runs on Anthropic's infrastructure. Advertising it as a function
    # tool would offer the model something that can never return a result.
    tools = convert_tools_anthropic_to_openai(
        [
            {"type": "web_search_20260209", "name": "web_search"},
            {"name": "get_weather", "input_schema": {"type": "object"}},
        ]
    )

    assert [t["function"]["name"] for t in tools] == ["get_weather"]


def test_unnamed_tool_is_dropped_rather_than_raising():
    assert convert_tools_anthropic_to_openai([{"description": "no name"}]) == []


def test_tool_with_no_schema_gets_the_empty_object_schema():
    tools = convert_tools_anthropic_to_openai([{"name": "ping"}])

    assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {}}


@pytest.mark.parametrize(
    "anthropic_choice,expected",
    [
        ({"type": "auto"}, "auto"),
        ({"type": "none"}, "none"),
        ({"type": "any"}, "required"),
        ({"type": "tool", "name": "f"}, {"type": "function", "function": {"name": "f"}}),
        ({"type": "tool"}, None),
        (None, None),
    ],
)
def test_tool_choice_mapping(anthropic_choice, expected):
    assert convert_tool_choice_anthropic_to_openai(anthropic_choice) == expected


def test_max_tokens_carries_across():
    payload = convert_payload_anthropic_to_openai(
        {"model": "m", "max_tokens": 512, "messages": [{"role": "user", "content": "hi"}]}
    )

    assert payload["max_tokens"] == 512
    assert payload["model"] == "m"
    assert payload["stream"] is False


def test_stop_sequences_become_the_openai_stop_list():
    payload = convert_payload_anthropic_to_openai(
        {"model": "m", "max_tokens": 1, "messages": [], "stop_sequences": ["END", "STOP"]}
    )

    assert payload["stop"] == ["END", "STOP"]


def test_sampling_params_pass_through_but_top_k_is_dropped():
    payload = convert_payload_anthropic_to_openai(
        {"model": "m", "max_tokens": 1, "messages": [], "temperature": 0.5, "top_p": 0.9, "top_k": 40}
    )

    assert payload["temperature"] == 0.5
    assert payload["top_p"] == 0.9
    assert "top_k" not in payload


def test_anthropic_only_fields_are_ignored_not_forwarded():
    # An SDK sets several of these by default; forwarding them would confuse a
    # downstream OpenAI-shaped backend, and rejecting them would make the endpoint
    # unusable with the clients it exists for.
    payload = convert_payload_anthropic_to_openai(
        {
            "model": "m",
            "max_tokens": 1,
            "messages": [],
            "metadata": {"user_id": "u"},
            "thinking": {"type": "adaptive"},
            "service_tier": "auto",
        }
    )

    assert "metadata" not in payload
    assert "thinking" not in payload
    assert "service_tier" not in payload


def test_tool_choice_is_omitted_when_no_tools_are_declared():
    payload = convert_payload_anthropic_to_openai(
        {"model": "m", "max_tokens": 1, "messages": [], "tool_choice": {"type": "any"}}
    )

    assert "tool_choice" not in payload


##########################################
# Response: non-streaming
##########################################


def test_non_streaming_response_maps_text_and_tool_use():
    converted = convert_response_openai_to_anthropic(
        {
            "id": "chatcmpl-1",
            "model": "local-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "here you go",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }
    )

    assert converted["type"] == "message"
    assert converted["role"] == "assistant"
    assert converted["content"] == [
        {"type": "text", "text": "here you go"},
        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}},
    ]
    assert converted["stop_reason"] == "tool_use"
    assert converted["stop_sequence"] is None
    assert converted["usage"] == {"input_tokens": 10, "output_tokens": 4}


def test_message_id_is_msg_prefixed():
    # SDKs assert on the prefix; an OpenAI chatcmpl- id must not leak through.
    converted = convert_response_openai_to_anthropic({"id": "chatcmpl-1", "choices": [{"message": {}}]})

    assert converted["id"].startswith("msg_")


def test_model_override_wins_over_the_pipeline_model():
    # The pipeline may rewrite `model` internally; a client told it got a model it
    # did not ask for will often reject the reply.
    converted = convert_response_openai_to_anthropic(
        {"model": "resolved-base-model", "choices": [{"message": {"content": "x"}}]},
        model="requested-id",
    )

    assert converted["model"] == "requested-id"


def test_empty_message_still_yields_one_text_block():
    # SDKs index content[0] freely; an empty content array is not something the real
    # API ever returns.
    converted = convert_response_openai_to_anthropic({"choices": [{"message": {"content": None}}]})

    assert converted["content"] == [{"type": "text", "text": ""}]


def test_unparseable_tool_arguments_degrade_to_empty_input():
    converted = convert_response_openai_to_anthropic(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "{not json"}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )

    assert converted["content"][0]["input"] == {}


@pytest.mark.parametrize(
    "finish_reason,stop_reason",
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("content_filter", "refusal"),
        (None, "end_turn"),
        ("something_new", "end_turn"),
    ],
)
def test_finish_reason_mapping(finish_reason, stop_reason):
    converted = convert_response_openai_to_anthropic(
        {"choices": [{"message": {"content": "x"}, "finish_reason": finish_reason}]}
    )

    assert converted["stop_reason"] == stop_reason


##########################################
# Response: streaming
##########################################


def test_stream_brackets_text_in_an_explicit_content_block():
    frames = collect(
        FakeStreamingResponse(
            sse(
                [
                    openai_chunk({"role": "assistant"}),
                    openai_chunk({"content": "Hel"}),
                    openai_chunk({"content": "lo"}),
                    openai_chunk({}, finish_reason="stop"),
                ]
            )
        ),
        model="m",
    )
    events = events_from(frames)

    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]

    start = events[0][1]["message"]
    assert start["type"] == "message"
    assert start["role"] == "assistant"
    assert start["model"] == "m"
    assert start["id"].startswith("msg_")

    assert events[1][1]["content_block"] == {"type": "text", "text": ""}
    assert [e[1]["delta"]["text"] for e in events[2:4]] == ["Hel", "lo"]
    assert events[-2][1]["delta"]["stop_reason"] == "end_turn"


def test_every_frame_carries_a_matching_event_name():
    # The `event:` line and the payload `type` must agree -- the SDKs dispatch on the
    # former, our own readers on the latter.
    frames = collect(FakeStreamingResponse(sse([openai_chunk({"content": "x"}, finish_reason="stop")])))

    for name, data in events_from(frames):
        assert name == data["type"]


def test_stream_emits_tool_use_blocks_with_input_json_deltas():
    frames = collect(
        FakeStreamingResponse(
            sse(
                [
                    openai_chunk({"content": "checking"}),
                    openai_chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ]
                        }
                    ),
                    openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
                    openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
                    openai_chunk({}, finish_reason="tool_calls"),
                ]
            )
        )
    )
    events = events_from(frames)
    by_name = [name for name, _ in events]

    # The text block must be closed before the tool block opens -- Anthropic allows
    # exactly one open content block at a time.
    assert by_name.index("content_block_stop") < by_name.index("content_block_start", 2)

    tool_start = next(data for name, data in events if name == "content_block_start" and data["index"] == 1)
    assert tool_start["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {},
    }

    partials = [d["delta"]["partial_json"] for n, d in events if n == "content_block_delta" and d["index"] == 1]
    assert "".join(partials) == '{"city":"Paris"}'

    assert events[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_parallel_tool_calls_get_distinct_block_indices():
    frames = collect(
        FakeStreamingResponse(
            sse(
                [
                    openai_chunk(
                        {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f", "arguments": ""}}]}
                    ),
                    openai_chunk(
                        {"tool_calls": [{"index": 1, "id": "b", "function": {"name": "g", "arguments": ""}}]}
                    ),
                    openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}),
                    openai_chunk({}, finish_reason="tool_calls"),
                ]
            )
        )
    )
    events = events_from(frames)

    starts = [(d["index"], d["content_block"]["id"]) for n, d in events if n == "content_block_start"]
    assert starts == [(0, "a"), (1, "b")]

    # A late delta for the first tool must be routed back to its own block index,
    # not to whichever block happens to be open.
    late = [d for n, d in events if n == "content_block_delta" and d["delta"]["type"] == "input_json_delta"]
    assert late[-1]["index"] == 0


def test_all_open_blocks_are_closed_before_message_delta():
    frames = collect(FakeStreamingResponse(sse([openai_chunk({"content": "x"}, finish_reason="stop")])))
    names = [name for name, _ in events_from(frames)]

    assert names.index("content_block_stop") < names.index("message_delta")
    assert names.count("content_block_start") == names.count("content_block_stop")


def test_usage_is_reported_on_message_delta():
    frames = collect(
        FakeStreamingResponse(
            sse(
                [
                    openai_chunk({"content": "x"}),
                    openai_chunk({}, finish_reason="stop", usage={"prompt_tokens": 7, "completion_tokens": 3}),
                ]
            )
        )
    )
    events = events_from(frames)

    assert events[-2][1]["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_empty_stream_still_produces_a_well_formed_message():
    # An SDK blocks waiting on message_start; an empty body would hang it rather
    # than surface an error.
    frames = collect(FakeStreamingResponse(["data: [DONE]"]))
    names = [name for name, _ in events_from(frames)]

    assert names == ["message_start", "message_delta", "message_stop"]


def test_malformed_sse_payload_is_skipped_without_killing_the_stream():
    frames = collect(
        FakeStreamingResponse(
            [
                "data: {not json",
                f"data: {json.dumps(openai_chunk({'content': 'ok'}, finish_reason='stop'))}",
                "data: [DONE]",
            ]
        )
    )
    events = events_from(frames)

    assert any(n == "content_block_delta" and d["delta"]["text"] == "ok" for n, d in events)
    assert events[-1][0] == "message_stop"


def test_reasoning_content_is_not_emitted_as_a_thinking_block():
    # A real thinking block carries a signature only the originating model can
    # produce; the SDKs reject an unsigned one, so dropping beats forging.
    frames = collect(
        FakeStreamingResponse(
            sse(
                [
                    openai_chunk({"reasoning_content": "hmm"}),
                    openai_chunk({"content": "42"}),
                    openai_chunk({}, finish_reason="stop"),
                ]
            )
        )
    )
    events = events_from(frames)

    assert not any(
        name == "content_block_start" and data["content_block"]["type"] == "thinking" for name, data in events
    )
    texts = [d["delta"]["text"] for n, d in events if n == "content_block_delta"]
    assert texts == ["42"]


####################
# /v1/models timestamps -- the catalog is NOT type-uniform (self.ai#112)
#
# This endpoint returned a bare HTTP 500 in production for every caller, because
# `created` is an int epoch on llamolotl models and an ISO-8601 STRING on every
# Anthropic-provider entry, and the original guard tested truthiness rather than
# type. The strings reached datetime.fromtimestamp() and raised
#
#     TypeError: 'str' object cannot be interpreted as an integer
#
# Ten Anthropic models are always in the catalog, so this was not an edge case:
# an Anthropic client pointed at self.ai could discover NOTHING. A model list it
# cannot read is how an agent CLI ends up assuming a default context window and
# then overrunning the one we actually serve.
####################


@pytest.mark.tier0
def test_an_iso_string_created_does_not_blow_up_the_listing():
    """The exact production value off a claude-* catalog entry."""
    from selfai_ui.main import _anthropic_created_at

    assert _anthropic_created_at("2026-07-24T00:00:00Z") == "2026-07-24T00:00:00Z"


@pytest.mark.tier0
def test_an_int_epoch_still_converts():
    """llamolotl entries carry epochs and must keep working -- the fix must not
    trade one half of the catalog for the other."""
    from selfai_ui.main import _anthropic_created_at

    assert _anthropic_created_at(0) == "1970-01-01T00:00:00Z"


@pytest.mark.tier0
@pytest.mark.parametrize(
    "created",
    [None, "", "   ", "not-a-date", True, False, [], {}, object()],
)
def test_an_unusable_created_costs_one_timestamp_not_the_catalog(created):
    """Never raise. A malformed value may cost that model its timestamp; it must
    not cost the caller the entire listing, which is what the 500 did."""
    from selfai_ui.main import _anthropic_created_at

    assert _anthropic_created_at(created) is None


@pytest.mark.tier0
def test_a_naive_iso_string_is_reported_as_utc():
    """Anthropic's field is RFC-3339; emitting a bare naive stamp would make the
    client guess a zone."""
    from selfai_ui.main import _anthropic_created_at

    assert _anthropic_created_at("2026-07-24T00:00:00") == "2026-07-24T00:00:00Z"
