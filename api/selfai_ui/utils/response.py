import json
import logging
import time
import uuid

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.utils.misc import (
    openai_chat_chunk_message_template,
    openai_chat_completion_message_template,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["ANTHROPIC"])


def convert_response_ollama_to_openai(ollama_response: dict) -> dict:
    model = ollama_response.get("model", "ollama")
    message_content = ollama_response.get("message", {}).get("content", "")

    response = openai_chat_completion_message_template(model, message_content)
    return response


async def convert_streaming_response_ollama_to_openai(ollama_streaming_response):
    async for data in ollama_streaming_response.body_iterator:
        data = json.loads(data)

        model = data.get("model", "ollama")
        message_content = data.get("message", {}).get("content", "")
        done = data.get("done", False)

        usage = None
        if done:
            usage = {
                "response_token/s": (
                    round(
                        ((data.get("eval_count", 0) / ((data.get("eval_duration", 0) / 10_000_000))) * 100),
                        2,
                    )
                    if data.get("eval_duration", 0) > 0
                    else "N/A"
                ),
                "prompt_token/s": (
                    round(
                        (
                            (data.get("prompt_eval_count", 0) / ((data.get("prompt_eval_duration", 0) / 10_000_000)))
                            * 100
                        ),
                        2,
                    )
                    if data.get("prompt_eval_duration", 0) > 0
                    else "N/A"
                ),
                "total_duration": data.get("total_duration", 0),
                "load_duration": data.get("load_duration", 0),
                "prompt_eval_count": data.get("prompt_eval_count", 0),
                "prompt_eval_duration": data.get("prompt_eval_duration", 0),
                "eval_count": data.get("eval_count", 0),
                "eval_duration": data.get("eval_duration", 0),
                "approximate_total": (lambda s: f"{s // 3600}h{(s % 3600) // 60}m{s % 60}s")(
                    (data.get("total_duration", 0) or 0) // 1_000_000_000
                ),
            }

        data = openai_chat_chunk_message_template(model, message_content if not done else None, usage)

        line = f"data: {json.dumps(data)}\n\n"
        yield line

    yield "data: [DONE]\n\n"


##########################################
#
# Anthropic (Messages API)
#
##########################################

# Anthropic stop_reason -> OpenAI finish_reason.
ANTHROPIC_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def _anthropic_usage_to_openai(usage: dict) -> dict:
    prompt_tokens = usage.get("input_tokens", 0) or 0
    completion_tokens = usage.get("output_tokens", 0) or 0
    converted = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    # Surface cache accounting when present -- without it a heavily-cached request
    # looks almost free and the numbers in the UI stop adding up.
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if field in usage:
            converted[field] = usage[field]
    return converted


def _anthropic_chunk(model: str, delta: dict, finish_reason=None, usage=None) -> dict:
    """Build an OpenAI-shaped streaming chunk.

    Deliberately not openai_chat_chunk_message_template(): that helper forces
    finish_reason="stop" whenever the message is falsy, which would terminate the
    stream on the first reasoning-only or tool-call-only delta.
    """
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
    }
    if usage:
        chunk["usage"] = usage
    return chunk


def convert_response_anthropic_to_openai(anthropic_response: dict) -> dict:
    """Convert a non-streaming Anthropic Messages response to OpenAI chat.completion shape."""
    model = anthropic_response.get("model", "anthropic")

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in anthropic_response.get("content", []) or []:
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "thinking":
            # May be an empty string when display is "omitted" -- keep only real content.
            if thinking := block.get("thinking"):
                thinking_parts.append(thinking)
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if thinking_parts:
        message["reasoning_content"] = "".join(thinking_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls

    stop_reason = anthropic_response.get("stop_reason")
    response = {
        "id": anthropic_response.get("id", f"chatcmpl-{uuid.uuid4()}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": ANTHROPIC_STOP_REASON_MAP.get(stop_reason, "stop"),
            }
        ],
    }

    if usage := anthropic_response.get("usage"):
        response["usage"] = _anthropic_usage_to_openai(usage)

    return response


async def convert_streaming_response_anthropic_to_openai(anthropic_streaming_response):
    """
    Re-emit an Anthropic SSE stream as OpenAI chat.completion.chunk events.

    Anthropic streams typed events (message_start, content_block_start/delta/stop,
    message_delta, message_stop) where each content block carries its own index.
    Everything downstream -- middleware.py's persistence path and self.chat's stream
    reader -- assumes the OpenAI shape, so the normalization happens here rather than
    branching those consumers:

      text_delta        -> delta.content
      thinking_delta    -> delta.reasoning_content   (never inlined into content)
      tool_use blocks   -> delta.tool_calls[] with a contiguous OpenAI-style index,
                           so middleware.py's accumulator needs no provider awareness
    """
    model = "anthropic"
    usage: dict = {}
    finish_reason = None

    # Anthropic content-block index -> OpenAI tool_calls index. Anthropic indexes all
    # content blocks in one sequence (text and tool_use interleaved); OpenAI expects
    # tool_calls to be indexed 0..n among themselves.
    tool_index_by_block: dict[int, int] = {}
    next_tool_index = 0

    buffer = ""
    async for raw in anthropic_streaming_response.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        buffer += raw

        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()

            if not line or line.startswith("event:"):
                # The event: name is redundant -- every data: payload carries its own
                # "type" field, which is what we switch on below.
                continue
            if not line.startswith("data:"):
                continue

            payload = line.removeprefix("data:").strip()
            if not payload:
                continue

            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                log.warning(f"Skipping unparseable Anthropic SSE payload: {payload[:200]}")
                continue

            event_type = event.get("type")

            if event_type == "message_start":
                message = event.get("message", {})
                model = message.get("model", model)
                if message_usage := message.get("usage"):
                    usage.update(message_usage)
                yield f"data: {json.dumps(_anthropic_chunk(model, {'role': 'assistant'}))}\n\n"

            elif event_type == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    block_index = event.get("index", 0)
                    tool_index_by_block[block_index] = next_tool_index
                    delta = {
                        "tool_calls": [
                            {
                                "index": next_tool_index,
                                "id": block.get("id"),
                                "type": "function",
                                "function": {"name": block.get("name"), "arguments": ""},
                            }
                        ]
                    }
                    next_tool_index += 1
                    yield f"data: {json.dumps(_anthropic_chunk(model, delta))}\n\n"

            elif event_type == "content_block_delta":
                delta_obj = event.get("delta", {})
                delta_type = delta_obj.get("type")

                if delta_type == "text_delta":
                    if text := delta_obj.get("text"):
                        yield f"data: {json.dumps(_anthropic_chunk(model, {'content': text}))}\n\n"

                elif delta_type == "thinking_delta":
                    if thinking := delta_obj.get("thinking"):
                        chunk = _anthropic_chunk(model, {"reasoning_content": thinking})
                        yield f"data: {json.dumps(chunk)}\n\n"

                elif delta_type == "input_json_delta":
                    block_index = event.get("index", 0)
                    tool_index = tool_index_by_block.get(block_index)
                    if tool_index is None:
                        continue
                    partial = delta_obj.get("partial_json", "")
                    if partial:
                        chunk = _anthropic_chunk(
                            model,
                            {
                                "tool_calls": [
                                    {
                                        "index": tool_index,
                                        "function": {"arguments": partial},
                                    }
                                ]
                            },
                        )
                        yield f"data: {json.dumps(chunk)}\n\n"

                # signature_delta carries the thinking-block signature; it is opaque
                # and has no OpenAI representation, so it is intentionally dropped.

            elif event_type == "message_delta":
                if stop_reason := event.get("delta", {}).get("stop_reason"):
                    finish_reason = ANTHROPIC_STOP_REASON_MAP.get(stop_reason, "stop")
                if delta_usage := event.get("usage"):
                    usage.update(delta_usage)

            elif event_type == "error":
                error = event.get("error", {})
                log.error(f"Anthropic stream error: {error}")
                chunk = _anthropic_chunk(
                    model,
                    {"content": f"\n\nError: {error.get('message', 'Anthropic stream error')}"},
                    finish_reason="stop",
                )
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return

            elif event_type == "message_stop":
                final = _anthropic_chunk(
                    model,
                    {},
                    finish_reason=finish_reason or "stop",
                    usage=_anthropic_usage_to_openai(usage) if usage else None,
                )
                yield f"data: {json.dumps(final)}\n\n"

    yield "data: [DONE]\n\n"


##########################################
#
# OpenAI -> Anthropic (INBOUND: we are the provider)
#
# The mirror of the two converters above. Those normalize an Anthropic upstream
# into the OpenAI shape everything internal expects; these take the OpenAI shape
# our own chat pipeline produces and re-emit it as the Anthropic Messages
# response an Anthropic-native client is waiting for.
#
##########################################

# OpenAI finish_reason -> Anthropic stop_reason. Inverse of ANTHROPIC_STOP_REASON_MAP,
# which is many-to-one, so this cannot be derived from it: "stop" maps back to
# "end_turn" (the common case) and "stop_sequence" is unrecoverable from here.
OPENAI_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _anthropic_message_id(openai_id) -> str:
    """Anthropic message ids are `msg_`-prefixed; some SDKs assert on that."""
    if isinstance(openai_id, str) and openai_id.startswith("msg_"):
        return openai_id
    return f"msg_{uuid.uuid4().hex}"


def _openai_usage_to_anthropic(usage: dict) -> dict:
    converted = {
        "input_tokens": usage.get("prompt_tokens", 0) or 0,
        "output_tokens": usage.get("completion_tokens", 0) or 0,
    }
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if field in usage:
            converted[field] = usage[field]
    return converted


def _anthropic_tool_input(arguments) -> dict:
    """Tool arguments arrive as a JSON *string* on OpenAI and a JSON *object* on Anthropic."""
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        log.warning(f"Unparseable tool_call arguments; sending empty input: {str(arguments)[:200]}")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def convert_response_openai_to_anthropic(openai_response: dict, model: str | None = None) -> dict:
    """Convert a non-streaming OpenAI chat.completion to an Anthropic Messages response.

    `model` overrides the id echoed back to the client. The pipeline may rewrite
    `model` internally (a base-model id, a provider prefix), and an Anthropic client
    that asked for one model and is told it got another will often reject the reply.
    """
    choice = (openai_response.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    content: list[dict] = []
    if text := message.get("content"):
        content.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex}",
                "name": function.get("name"),
                "input": _anthropic_tool_input(function.get("arguments")),
            }
        )

    if not content:
        # A message with no content blocks at all is not something Anthropic ever
        # returns, and SDKs index content[0] freely. An empty text block is the
        # faithful representation of "the model said nothing".
        content.append({"type": "text", "text": ""})

    response = {
        "id": _anthropic_message_id(openai_response.get("id")),
        "type": "message",
        "role": "assistant",
        "model": model or openai_response.get("model"),
        "content": content,
        "stop_reason": OPENAI_FINISH_REASON_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": _openai_usage_to_anthropic(openai_response.get("usage") or {}),
    }
    return response


def _sse(event_type: str, data: dict) -> str:
    """Anthropic frames carry both an `event:` name and the typed `data:` payload.

    Our own reader switches on data.type and ignores the event name, but the
    official SDKs dispatch on `event:` -- omitting it is what makes a stream that
    looks correct in curl fail inside a real client.
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def convert_streaming_response_openai_to_anthropic(openai_streaming_response, model: str | None = None):
    """
    Re-emit an OpenAI chat.completion.chunk stream as Anthropic Messages SSE.

    The two formats disagree about where structure lives. OpenAI sends a flat
    sequence of deltas and lets the client work out which content is which;
    Anthropic brackets every run of content in an explicit block:

        message_start
          content_block_start(index=0, text) / content_block_delta(text_delta) / content_block_stop
          content_block_start(index=1, tool_use) / content_block_delta(input_json_delta) / content_block_stop
        message_delta(stop_reason, usage)
        message_stop

    So this is not a per-chunk mapping -- it is a state machine that opens a block
    on the first delta of a kind, and closes it when the kind changes or the stream
    ends. Exactly one block is open at a time, which is what lets a single
    `next_index` counter serve both text and tool blocks.

    `reasoning_content` is dropped rather than re-emitted as a `thinking` block: a
    real thinking block carries a `signature` that only the originating model can
    produce, and the SDKs reject an unsigned one when it is replayed.
    """
    message_id = f"msg_{uuid.uuid4().hex}"
    stream_model = model or "self-ai"

    started = False
    next_index = 0
    open_block: str | None = None  # "text" | "tool_use"
    # OpenAI tool_calls index -> the Anthropic content-block index it was opened at.
    tool_block_index: dict[int, int] = {}
    stop_reason = "end_turn"
    usage: dict = {}

    def start_message() -> str:
        return _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": stream_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    # Real input_tokens only arrive with the final usage block, if at
                    # all. Zero is the honest placeholder; the message_delta below
                    # carries the real numbers.
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def close_block(index: int) -> str:
        return _sse("content_block_stop", {"type": "content_block_stop", "index": index})

    buffer = ""
    async for raw in openai_streaming_response.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        buffer += raw

        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()

            if not line or not line.startswith("data:"):
                continue

            payload = line.removeprefix("data:").strip()
            if not payload or payload == "[DONE]":
                continue

            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                log.warning(f"Skipping unparseable OpenAI SSE payload: {payload[:200]}")
                continue

            if model is None and (chunk_model := chunk.get("model")):
                stream_model = chunk_model

            if chunk_usage := chunk.get("usage"):
                usage.update(chunk_usage)

            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}

            if not started:
                started = True
                yield start_message()

            if text := delta.get("content"):
                if open_block == "tool_use":
                    yield close_block(next_index - 1)
                    open_block = None
                if open_block is None:
                    yield _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": next_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    open_block = "text"
                    next_index += 1
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": next_index - 1,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )

            for call in delta.get("tool_calls") or []:
                call_index = call.get("index", 0)
                function = call.get("function") or {}

                if call_index not in tool_block_index:
                    if open_block is not None:
                        yield close_block(next_index - 1)
                    yield _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": next_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": call.get("id") or f"toolu_{uuid.uuid4().hex}",
                                "name": function.get("name"),
                                "input": {},
                            },
                        },
                    )
                    tool_block_index[call_index] = next_index
                    open_block = "tool_use"
                    next_index += 1

                if arguments := function.get("arguments"):
                    yield _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": tool_block_index[call_index],
                            "delta": {"type": "input_json_delta", "partial_json": arguments},
                        },
                    )

            if finish_reason := choice.get("finish_reason"):
                stop_reason = OPENAI_FINISH_REASON_MAP.get(finish_reason, "end_turn")

    # A stream that produced nothing at all still owes the client a well-formed
    # message rather than an empty body -- an SDK waiting on message_start hangs.
    if not started:
        yield start_message()

    if open_block is not None:
        yield close_block(next_index - 1)

    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": _openai_usage_to_anthropic(usage),
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})
