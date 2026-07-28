# test_anthropic_conversion.py
#
# Covers the OpenAI <-> Anthropic Messages API adapters (self.ai#59).
#
# These converters are load-bearing: utils/middleware.py and self.chat's stream reader
# both hard-read the OpenAI chunk shape, so anything Anthropic-specific has to be
# normalized here rather than by branching those consumers.

import asyncio
import json

import pytest

from selfai_ui.utils.payload import (
    convert_messages_openai_to_anthropic,
    convert_payload_openai_to_anthropic,
    convert_tool_choice_openai_to_anthropic,
    convert_tools_openai_to_anthropic,
)
from selfai_ui.utils.response import (
    convert_response_anthropic_to_openai,
    convert_streaming_response_anthropic_to_openai,
)


class FakeStreamingResponse:
    """Yields the SSE blob in small chunks that deliberately split mid-line.

    aiohttp does not guarantee line-aligned chunks, so the converter must buffer and
    reassemble; feeding it whole lines would not exercise that.
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


def collect(streaming_response):
    """Drive the async generator to completion synchronously.

    pytest-asyncio is not a dependency here and there are no other async tests in the
    suite; adding the plugin just for these would be a heavier change than driving the
    loop directly, and a bare @pytest.mark.asyncio would silently skip instead of fail.
    """

    async def run():
        return [line async for line in convert_streaming_response_anthropic_to_openai(streaming_response)]

    return asyncio.run(run())


def chunks_from(lines):
    return [json.loads(line[len("data: ") :]) for line in lines if line.startswith("data: ") and "[DONE]" not in line]


##########################################
# Message conversion
##########################################


def test_system_messages_are_hoisted_out_of_messages():
    messages, system = convert_messages_openai_to_anthropic(
        [
            {"role": "system", "content": "be terse"},
            {"role": "system", "content": "cite sources"},
            {"role": "user", "content": "hi"},
        ]
    )

    # Anthropic has no system role inside messages -- it must become a top-level field.
    assert system == "be terse\n\ncite sources"
    assert [m["role"] for m in messages] == ["user"]


def test_data_url_image_becomes_base64_source():
    messages, _ = convert_messages_openai_to_anthropic(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
                ],
            }
        ]
    )

    assert messages[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
    }


def test_remote_image_url_passes_through_as_url_source():
    messages, _ = convert_messages_openai_to_anthropic(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]
    )

    assert messages[0]["content"][0] == {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}


def test_assistant_tool_calls_become_tool_use_blocks_with_parsed_input():
    messages, _ = convert_messages_openai_to_anthropic(
        [
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    }
                ],
            }
        ]
    )

    assert messages[0]["content"] == [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
    ]


def test_unparseable_tool_arguments_degrade_to_empty_input():
    # A truncated stream can leave invalid JSON in arguments; sending it raw is a 400.
    messages, _ = convert_messages_openai_to_anthropic(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "t", "function": {"name": "f", "arguments": '{"a":'}}],
            }
        ]
    )

    assert messages[0]["content"][0]["input"] == {}


def test_parallel_tool_results_merge_into_a_single_user_turn():
    # Anthropic requires every tool_result for a turn in one user message; emitting one
    # message per result is rejected.
    messages, _ = convert_messages_openai_to_anthropic(
        [
            {"role": "tool", "tool_call_id": "toolu_1", "content": "72F"},
            {"role": "tool", "tool_call_id": "toolu_2", "content": "sunny"},
        ]
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert [b["tool_use_id"] for b in messages[0]["content"]] == ["toolu_1", "toolu_2"]


##########################################
# Tool + payload conversion
##########################################


def test_tools_are_flattened_to_input_schema():
    assert convert_tools_openai_to_anthropic(
        [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}]
    ) == [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]


@pytest.mark.parametrize(
    "openai_choice,expected",
    [
        (None, None),
        ("auto", None),
        ("none", {"type": "none"}),
        ("required", {"type": "any"}),
        ({"type": "function", "function": {"name": "f"}}, {"type": "tool", "name": "f"}),
    ],
)
def test_tool_choice_mapping(openai_choice, expected):
    assert convert_tool_choice_openai_to_anthropic(openai_choice) == expected


def test_max_tokens_falls_back_to_the_models_own_ceiling():
    # Anthropic requires max_tokens; the ceiling comes from the live model list rather
    # than a magic constant (self.ai#59 Phase 0 decision 2).
    payload = convert_payload_openai_to_anthropic(
        {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]},
        default_max_tokens=64000,
    )
    assert payload["max_tokens"] == 64000


def test_explicit_max_tokens_wins_over_the_ceiling():
    payload = convert_payload_openai_to_anthropic(
        {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 512},
        default_max_tokens=64000,
    )
    assert payload["max_tokens"] == 512


def test_sampling_params_dropped_for_models_that_reject_them():
    # temperature/top_p/top_k are a 400 on these families.
    payload = convert_payload_openai_to_anthropic(
        {
            "model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "top_p": 0.5,
            "top_k": 10,
        },
        default_max_tokens=4096,
    )
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "top_k" not in payload


def test_sampling_params_kept_for_older_models_that_accept_them():
    payload = convert_payload_openai_to_anthropic(
        {"model": "claude-opus-4-6", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.7},
        default_max_tokens=4096,
    )
    assert payload["temperature"] == 0.7


def test_openai_only_params_are_dropped():
    payload = convert_payload_openai_to_anthropic(
        {
            "model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "hi"}],
            "frequency_penalty": 1,
            "presence_penalty": 1,
            "seed": 42,
        },
        default_max_tokens=4096,
    )
    for param in ("frequency_penalty", "presence_penalty", "seed"):
        assert param not in payload


def test_stop_string_becomes_stop_sequences_list():
    payload = convert_payload_openai_to_anthropic(
        {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}], "stop": "END"},
        default_max_tokens=4096,
    )
    assert payload["stop_sequences"] == ["END"]


def test_thinking_is_requested_with_summarized_display_when_supported():
    # The default display is "omitted", which streams empty thinking blocks and leaves
    # the reasoning panel blank.
    payload = convert_payload_openai_to_anthropic(
        {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]},
        default_max_tokens=4096,
        supports_adaptive_thinking=True,
    )
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_thinking_is_omitted_when_the_model_does_not_support_it():
    payload = convert_payload_openai_to_anthropic(
        {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]},
        default_max_tokens=4096,
        supports_adaptive_thinking=False,
    )
    assert "thinking" not in payload


##########################################
# Non-streaming response conversion
##########################################


def test_non_streaming_response_maps_text_thinking_and_tool_use():
    response = convert_response_anthropic_to_openai(
        {
            "id": "msg_1",
            "model": "claude-opus-4-8",
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_9", "name": "get_weather", "input": {"city": "Paris"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
        }
    )

    message = response["choices"][0]["message"]
    assert message["content"] == "Let me check."
    # Thinking rides on its own field -- inlining it would corrupt the saved message.
    assert message["reasoning_content"] == "hmm"
    assert message["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": '{"city": "Paris"}'}
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert response["usage"]["total_tokens"] == 15
    assert response["usage"]["cache_read_input_tokens"] == 3


def test_empty_thinking_blocks_do_not_produce_a_reasoning_field():
    # With display "omitted" the block is present but its text is empty.
    response = convert_response_anthropic_to_openai(
        {
            "model": "claude-opus-4-8",
            "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "hi"}],
        }
    )
    assert "reasoning_content" not in response["choices"][0]["message"]


@pytest.mark.parametrize(
    "stop_reason,finish_reason",
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
        ("stop_sequence", "stop"),
    ],
)
def test_stop_reason_mapping(stop_reason, finish_reason):
    response = convert_response_anthropic_to_openai(
        {"model": "m", "stop_reason": stop_reason, "content": [{"type": "text", "text": "x"}]}
    )
    assert response["choices"][0]["finish_reason"] == finish_reason


##########################################
# Streaming conversion
##########################################

STREAM = [
    "event: message_start",
    'data: {"type":"message_start","message":{"model":"claude-opus-4-8","usage":{"input_tokens":10}}}',
    "",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"pondering"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"xx"}}',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text"}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Hi"}}',
    'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_a","name":"f"}}',
    'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":"}}',
    'data: {"type":"content_block_start","index":3,"content_block":{"type":"tool_use","id":"toolu_b","name":"g"}}',
    'data: {"type":"content_block_delta","index":3,"delta":{"type":"input_json_delta","partial_json":"1}"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}',
    'data: {"type":"message_stop"}',
]


def test_stream_emits_openai_shaped_chunks_and_terminates():
    lines = collect(FakeStreamingResponse(STREAM))
    chunks = chunks_from(lines)

    assert lines[-1] == "data: [DONE]\n\n"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[0]["object"] == "chat.completion.chunk"


def test_stream_separates_reasoning_from_content():
    deltas = [c["choices"][0]["delta"] for c in chunks_from(collect(FakeStreamingResponse(STREAM)))]

    assert {"reasoning_content": "pondering"} in deltas
    assert {"content": "Hi"} in deltas
    # Reasoning must never leak into content -- that is what gets persisted as the answer.
    assert not any("pondering" in (d.get("content") or "") for d in deltas)


def test_stream_drops_signature_deltas():
    # Opaque thinking-block signature; no OpenAI representation.
    deltas = [c["choices"][0]["delta"] for c in chunks_from(collect(FakeStreamingResponse(STREAM)))]
    assert not any("signature" in json.dumps(d) for d in deltas)


def test_stream_reindexes_tool_calls_contiguously():
    # Anthropic indexes ALL content blocks in one sequence (here 2 and 3); OpenAI expects
    # tool_calls indexed 0..n among themselves, which middleware.py's accumulator assumes.
    deltas = [c["choices"][0]["delta"] for c in chunks_from(collect(FakeStreamingResponse(STREAM)))]
    tool_calls = [d["tool_calls"][0] for d in deltas if "tool_calls" in d]

    assert [t["index"] for t in tool_calls] == [0, 0, 1, 1]
    assert tool_calls[0]["id"] == "toolu_a"
    assert tool_calls[2]["id"] == "toolu_b"
    assert tool_calls[1]["function"]["arguments"] == '{"a":'
    assert tool_calls[3]["function"]["arguments"] == "1}"


def test_only_the_final_chunk_carries_finish_reason_and_usage():
    chunks = chunks_from(collect(FakeStreamingResponse(STREAM)))

    assert all(c["choices"][0]["finish_reason"] is None for c in chunks[:-1])
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"]["prompt_tokens"] == 10
    assert chunks[-1]["usage"]["completion_tokens"] == 7


def test_stream_error_event_terminates_cleanly():
    lines = collect(
        FakeStreamingResponse(
            [
                'data: {"type":"message_start","message":{"model":"m","usage":{}}}',
                'data: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}',
            ]
        )
    )

    assert lines[-1] == "data: [DONE]\n\n"
    assert "overloaded" in lines[-2]


def test_malformed_sse_payload_is_skipped_without_killing_the_stream():
    lines = collect(
        FakeStreamingResponse(
            [
                'data: {"type":"message_start","message":{"model":"m","usage":{}}}',
                "data: {not json",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}',
                'data: {"type":"message_stop"}',
            ]
        )
    )

    deltas = [c["choices"][0]["delta"] for c in chunks_from(lines)]
    assert {"content": "ok"} in deltas
    assert lines[-1] == "data: [DONE]\n\n"
