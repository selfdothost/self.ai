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
