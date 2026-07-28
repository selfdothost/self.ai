import json
import logging
from typing import Callable

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.utils.misc import (
    add_or_update_system_message,
)
from selfai_ui.utils.task import prompt_template
from selfai_ui.utils.toolspec import ToolSpec

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["ANTHROPIC"])


# inplace function: form_data is modified
def apply_model_system_prompt_to_body(params: dict, form_data: dict, user) -> dict:
    system = params.get("system", None)
    if not system:
        return form_data

    if user:
        template_params = {
            "user_name": user.name,
            "user_location": user.info.get("location") if user.info else None,
        }
    else:
        template_params = {}
    system = prompt_template(system, **template_params)
    form_data["messages"] = add_or_update_system_message(system, form_data.get("messages", []))
    return form_data


# inplace function: form_data is modified
def apply_model_params_to_body(params: dict, form_data: dict, mappings: dict[str, Callable]) -> dict:
    if not params:
        return form_data

    for key, cast_func in mappings.items():
        if (value := params.get(key)) is not None:
            form_data[key] = cast_func(value)

    return form_data


# inplace function: form_data is modified
def apply_model_params_to_body_openai(params: dict, form_data: dict) -> dict:
    mappings = {
        "temperature": float,
        "top_p": float,
        "max_tokens": int,
        "frequency_penalty": float,
        "seed": lambda x: x,
        "stop": lambda x: [bytes(s, "utf-8").decode("unicode_escape") for s in x],
    }
    return apply_model_params_to_body(params, form_data, mappings)


def apply_model_params_to_body_ollama(params: dict, form_data: dict) -> dict:
    opts = [
        "temperature",
        "top_p",
        "seed",
        "mirostat",
        "mirostat_eta",
        "mirostat_tau",
        "num_ctx",
        "num_batch",
        "num_keep",
        "repeat_last_n",
        "tfs_z",
        "top_k",
        "min_p",
        "use_mmap",
        "use_mlock",
        "num_thread",
        "num_gpu",
    ]
    mappings = {i: lambda x: x for i in opts}
    form_data = apply_model_params_to_body(params, form_data, mappings)

    name_differences = {
        "max_tokens": "num_predict",
        "frequency_penalty": "repeat_penalty",
    }

    for key, value in name_differences.items():
        if (param := params.get(key, None)) is not None:
            form_data[value] = param

    return form_data


def convert_messages_openai_to_ollama(messages: list[dict]) -> list[dict]:
    ollama_messages = []

    for message in messages:
        # Initialize the new message structure with the role
        new_message = {"role": message["role"]}

        content = message.get("content", [])

        # Check if the content is a string (just a simple message)
        if isinstance(content, str):
            # If the content is a string, it's pure text
            new_message["content"] = content
        else:
            # Otherwise, assume the content is a list of dicts, e.g., text followed by an image URL
            content_text = ""
            images = []

            # Iterate through the list of content items
            for item in content:
                # Check if it's a text type
                if item.get("type") == "text":
                    content_text += item.get("text", "")

                # Check if it's an image URL type
                elif item.get("type") == "image_url":
                    img_url = item.get("image_url", {}).get("url", "")
                    if img_url:
                        # If the image url starts with data:, it's a base64 image and should be trimmed
                        if img_url.startswith("data:"):
                            img_url = img_url.split(",")[-1]
                        images.append(img_url)

            # Add content text (if any)
            if content_text:
                new_message["content"] = content_text.strip()

            # Add images (if any)
            if images:
                new_message["images"] = images

        # Append the new formatted message to the result
        ollama_messages.append(new_message)

    return ollama_messages


def convert_payload_openai_to_ollama(openai_payload: dict) -> dict:
    """
    Converts a payload formatted for OpenAI's API to be compatible with Ollama's API endpoint for chat completions.

    Args:
        openai_payload (dict): The payload originally designed for OpenAI API usage.

    Returns:
        dict: A modified payload compatible with the Ollama API.
    """
    ollama_payload = {}

    # Mapping basic model and message details
    ollama_payload["model"] = openai_payload.get("model")
    ollama_payload["messages"] = convert_messages_openai_to_ollama(openai_payload.get("messages"))
    ollama_payload["stream"] = openai_payload.get("stream", False)

    if "format" in openai_payload:
        ollama_payload["format"] = openai_payload["format"]

    # If there are advanced parameters in the payload, format them in Ollama's options field
    ollama_options = {}

    if openai_payload.get("options"):
        ollama_payload["options"] = openai_payload["options"]
        ollama_options = openai_payload["options"]

    # Handle parameters which map directly
    for param in ["temperature", "top_p", "seed"]:
        if param in openai_payload:
            ollama_options[param] = openai_payload[param]

    # Mapping OpenAI's `max_tokens` -> Ollama's `num_predict`
    if "max_completion_tokens" in openai_payload:
        ollama_options["num_predict"] = openai_payload["max_completion_tokens"]
    elif "max_tokens" in openai_payload:
        ollama_options["num_predict"] = openai_payload["max_tokens"]

    # Handle frequency / presence_penalty, which needs renaming and checking
    if "frequency_penalty" in openai_payload:
        ollama_options["repeat_penalty"] = openai_payload["frequency_penalty"]

    if "presence_penalty" in openai_payload and "penalty" not in ollama_options:
        # We are assuming presence penalty uses a similar concept in Ollama, which needs custom handling if exists.
        ollama_options["new_topic_penalty"] = openai_payload["presence_penalty"]

    # Add options to payload if any have been set
    if ollama_options:
        ollama_payload["options"] = ollama_options

    return ollama_payload


##########################################
#
# Anthropic (Messages API)
#
##########################################


# Model families that reject temperature/top_p/top_k with a 400. Anthropic's
# GET /v1/models capabilities block does not advertise sampling support, so this
# prefix list is the only signal available -- it is maintenance duct tape and
# needs a new entry each time a model family drops sampling params. Older
# families (opus-4-6, sonnet-4-6, haiku-4-5) still accept them.
ANTHROPIC_NO_SAMPLING_PREFIXES = (
    "claude-fable-",
    "claude-mythos-",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
)

# Dropped outright: Anthropic has no equivalent and sending them is a 400.
ANTHROPIC_UNSUPPORTED_PARAMS = (
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "logit_bias",
    "n",
    "logprobs",
    "top_logprobs",
    "response_format",
)


def _anthropic_image_block(img_url: str) -> dict | None:
    """Convert an OpenAI image_url value to an Anthropic image content block."""
    if not img_url:
        return None

    if img_url.startswith("data:"):
        # data:image/png;base64,<payload>
        try:
            header, data = img_url.split(",", 1)
        except ValueError:
            log.warning("Skipping malformed data: image URL")
            return None
        media_type = header.split(";")[0].removeprefix("data:") or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }

    return {"type": "image", "source": {"type": "url", "url": img_url}}


def _anthropic_content_blocks(content) -> list[dict]:
    """Normalize an OpenAI message `content` (str or multipart list) to Anthropic blocks."""
    if content is None:
        return []

    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []

    blocks = []
    for item in content:
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif item_type == "image_url":
            block = _anthropic_image_block(item.get("image_url", {}).get("url", ""))
            if block:
                blocks.append(block)
        else:
            log.debug(f"Dropping unsupported content block type: {item_type}")

    return blocks


def convert_messages_openai_to_anthropic(messages: list[dict]) -> tuple[list[dict], str | None]:
    """
    Split an OpenAI messages array into (anthropic_messages, system_prompt).

    Anthropic has no `system` role inside `messages` -- system content is hoisted to a
    top-level `system` field. Tool results move from their own `tool` role into
    `tool_result` blocks on a user message, and assistant `tool_calls` become
    `tool_use` blocks. Consecutive same-role messages are merged so that parallel
    tool results land in a single user turn, which the API requires.
    """
    system_parts: list[str] = []
    converted: list[dict] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            for block in _anthropic_content_blocks(content):
                if block["type"] == "text":
                    system_parts.append(block["text"])
            continue

        if role == "tool":
            # OpenAI tool result -> Anthropic tool_result block on a user turn.
            tool_content = content if isinstance(content, str) else json.dumps(content)
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": tool_content,
            }
            if message.get("is_error"):
                block["is_error"] = True
            converted.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            blocks = _anthropic_content_blocks(content)
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                arguments = function.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments or "{}")
                    except json.JSONDecodeError:
                        log.warning(f"Unparseable tool_call arguments for {function.get('name')}; sending empty input")
                        arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "input": arguments,
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue

        # user (and anything unrecognized, treated as user)
        blocks = _anthropic_content_blocks(content)
        if blocks:
            converted.append({"role": "user", "content": blocks})

    # Merge consecutive same-role turns -- required so parallel tool_results share one
    # user message, and harmless otherwise.
    merged: list[dict] = []
    for message in converted:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"].extend(message["content"])
        else:
            merged.append({"role": message["role"], "content": list(message["content"])})

    system = "\n\n".join(system_parts) if system_parts else None
    return merged, system


def convert_tools_openai_to_anthropic(tools: list[dict]) -> list[dict]:
    """OpenAI ``{"type":"function","function":{...}}`` -> flat Anthropic tool.

    The Anthropic wire shape is defined in exactly one place -- ``ToolSpec`` --
    and emitted by ``to_anthropic()``. This function's job is no longer to
    *know* that shape; it is to get an arbitrary inbound dict into a
    ``ToolSpec`` safely. It used to build the Anthropic dict by hand, which made
    it a second implementation of a translation core already performed, and the
    two had already drifted in two places before anything called them together.

    **Why this does not simply call ``ToolSpec.from_openai``.** That classmethod
    is strict on purpose -- ``extra="forbid"``, and a missing name is a
    validation error -- which is right for a spec core produced itself and is
    reading back, and wrong here. This runs on the completion endpoint of
    ``routers/anthropic.py``, whose ``form_data`` is an untyped dict straight
    off the wire, so a client may legitimately send an OpenAI payload carrying
    ``strict: true`` or any other key OpenAI defines and we do not consume.
    Being strict here would turn a request that works today into a 500 over a
    field we were always free to ignore.

    So the two leniencies below are boundary policy -- deliberate and tested --
    rather than the accidental divergence they used to be:

    * **An unnamed tool is dropped, not raised on.** It could not be called
      anyway, and failing a whole conversation over one malformed entry in an
      otherwise fine list is the wrong trade at an API edge.
    * **A falsy argument schema becomes the empty-object schema.** ``{}`` and
      ``None`` both mean "takes no arguments" from a client that did not think
      about it; Anthropic wants a schema object, so it gets the one that says
      exactly that rather than something it may reject. The key is omitted at
      construction so ``ToolSpec``'s own default supplies it -- what "the empty
      schema" is stays defined in one place too.

    Unknown keys are ignored rather than rejected, for the same reason.
    """
    converted = []
    for tool in tools or []:
        function = tool.get("function", tool)
        name = function.get("name")
        if not name:
            continue
        fields = {"name": name, "description": function.get("description") or ""}
        if schema := function.get("parameters"):
            fields["input_schema"] = schema
        converted.append(ToolSpec(**fields).to_anthropic())
    return converted


def convert_tool_choice_openai_to_anthropic(tool_choice):
    if tool_choice in (None, "auto"):
        return None
    if tool_choice == "none":
        return {"type": "none"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = tool_choice.get("function", {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def convert_payload_openai_to_anthropic(
    openai_payload: dict,
    default_max_tokens: int = 4096,
    supports_adaptive_thinking: bool = False,
) -> dict:
    """
    Convert an OpenAI chat-completions payload to an Anthropic Messages API payload.

    `default_max_tokens` should be the target model's own max output, read from the live
    model list -- Anthropic requires max_tokens and we would rather not invent a constant.
    `supports_adaptive_thinking` comes from the model's advertised capabilities; when true
    we opt into summarized thinking so the reasoning panel has something to render.
    """
    model = openai_payload.get("model")
    messages, system = convert_messages_openai_to_anthropic(openai_payload.get("messages") or [])

    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": (
            openai_payload.get("max_tokens") or openai_payload.get("max_completion_tokens") or default_max_tokens
        ),
        "stream": openai_payload.get("stream", False),
    }

    if system:
        payload["system"] = system

    if tools := convert_tools_openai_to_anthropic(openai_payload.get("tools")):
        payload["tools"] = tools
        if tool_choice := convert_tool_choice_openai_to_anthropic(openai_payload.get("tool_choice")):
            payload["tool_choice"] = tool_choice

    if stop := openai_payload.get("stop"):
        payload["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    if supports_adaptive_thinking:
        # display defaults to "omitted" on current models, which would stream empty
        # thinking blocks and leave the reasoning panel blank.
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}

    if effort := openai_payload.get("effort"):
        payload["output_config"] = {"effort": effort}

    model_id = model or ""
    rejects_sampling = model_id.startswith(ANTHROPIC_NO_SAMPLING_PREFIXES)
    for param in ("temperature", "top_p", "top_k"):
        if (value := openai_payload.get(param)) is None:
            continue
        if rejects_sampling:
            log.debug(f"Dropping {param}: {model_id} rejects sampling parameters")
            continue
        payload[param] = value

    for param in ANTHROPIC_UNSUPPORTED_PARAMS:
        if param in openai_payload:
            log.debug(f"Dropping {param}: no Anthropic equivalent")

    return payload
