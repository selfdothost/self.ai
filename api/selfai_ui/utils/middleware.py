import asyncio
import inspect
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from uuid import uuid4

from fastapi import Request
from langchain_core.documents import Document as LCDocument
from starlette.responses import StreamingResponse

from selfai_ui.browse.access_control import has_browsing_access
from selfai_ui.browse.connection import browse_fetch
from selfai_ui.browse.profiles import resolve_profile
from selfai_ui.constants import TASKS
from selfai_ui.env import (
    ENABLE_REALTIME_CHAT_SAVE,
    GLOBAL_LOG_LEVEL,
    SRC_LOG_LEVELS,
)
from selfai_ui.models.chats import Chats
from selfai_ui.models.functions import Functions
from selfai_ui.models.users import UserModel, Users
from selfai_ui.retrieval.utils import get_sources_from_files
from selfai_ui.routers.retrieval import save_docs_to_vector_db, search_web
from selfai_ui.routers.tasks import (
    TaskFormData,
    generate_chat_tags,
    generate_queries,
    generate_title,
)
from selfai_ui.socket.main import (
    get_active_status_by_user_id,
    get_event_call,
    get_event_emitter,
)
from selfai_ui.tasks import create_task
from selfai_ui.utils.chat import generate_chat_completion
from selfai_ui.utils.misc import (
    add_or_update_system_message,
    calculate_sha256_string,
    get_last_user_message,
    get_message_list,
    prepend_to_first_user_message_content,
)
from selfai_ui.utils.plugin import get_function_priority, load_function_module_by_id
from selfai_ui.utils.task import (
    get_task_model_id,
    rag_template,
)
from selfai_ui.utils.tools import get_tools
from selfai_ui.utils.webhook import post_webhook

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


async def chat_completion_filter_functions_handler(request, body, model, extra_params):
    skip_files = None

    def get_filter_function_ids(model):
        filter_ids = [function.id for function in Functions.get_global_filter_functions()]
        if "info" in model and "meta" in model["info"]:
            filter_ids.extend(model["info"]["meta"].get("filterIds", []))
            filter_ids = list(set(filter_ids))

        enabled_filter_ids = [function.id for function in Functions.get_functions_by_type("filter", active_only=True)]

        filter_ids = [filter_id for filter_id in filter_ids if filter_id in enabled_filter_ids]

        # Sort filter_ids by priority, using the shared get_function_priority helper
        filter_ids.sort(key=get_function_priority)
        return filter_ids

    filter_ids = get_filter_function_ids(model)
    for filter_id in filter_ids:
        filter = Functions.get_function_by_id(filter_id)
        if not filter:
            continue

        if filter_id in request.app.state.FUNCTIONS:
            function_module = request.app.state.FUNCTIONS[filter_id]
        else:
            function_module, _, _ = load_function_module_by_id(filter_id)
            request.app.state.FUNCTIONS[filter_id] = function_module

        # Check if the function has a file_handler variable
        if hasattr(function_module, "file_handler"):
            skip_files = function_module.file_handler

        # Apply valves to the function
        if hasattr(function_module, "valves") and hasattr(function_module, "Valves"):
            valves = Functions.get_function_valves_by_id(filter_id)
            function_module.valves = function_module.Valves(**(valves if valves else {}))

        if hasattr(function_module, "inlet"):
            try:
                inlet = function_module.inlet

                # Create a dictionary of parameters to be passed to the function
                params = {"body": body} | {
                    k: v
                    for k, v in {
                        **extra_params,
                        "__model__": model,
                        "__id__": filter_id,
                    }.items()
                    if k in inspect.signature(inlet).parameters
                }

                if "__user__" in params and hasattr(function_module, "UserValves"):
                    try:
                        params["__user__"]["valves"] = function_module.UserValves(
                            **Functions.get_user_valves_by_id_and_user_id(filter_id, params["__user__"]["id"])
                        )
                    except Exception as e:
                        print(e)

                if inspect.iscoroutinefunction(inlet):
                    body = await inlet(**params)
                else:
                    body = inlet(**params)

            except Exception as e:
                print(f"Error: {e}")
                raise e

    if skip_files and "files" in body.get("metadata", {}):
        del body["metadata"]["files"]

    return body, {}


WEB_SEARCH_TOOL_SPEC = {
    "name": "web_search",
    "description": (
        "Search the web for current, real-time, or otherwise unfamiliar information that "
        "is not already available in this conversation. Use this only when the existing "
        "context is insufficient to answer accurately — do not use it for general "
        "knowledge, conversation, or anything already covered above."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A concise, targeted web search query.",
            }
        },
        "required": ["query"],
    },
}


class _HTMLTextExtractor(HTMLParser):
    """cavekit-browse-search-migration.md R4: dependency-free HTML -> readable
    text + title extraction, so web_search's own content processing doesn't
    require the domain-crawl scraping backend to be present or running."""

    _SKIP_TAGS = {"script", "style", "noscript", "head"}
    _BREAK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag == "title":
            self._in_title = False
        if tag in self._BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> tuple[str, str]:
    """R4: extract (readable_text, title) from HTML. Never raises — a
    parse failure on genuinely malformed input just yields what could be
    recovered rather than failing the whole search."""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception as e:
        log.warning(f"html_to_text: parse error, using partial result: {e}")
    return parser.get_text(), parser.title.strip()


async def run_web_search_tool_call(request: Request, query: str, extra_params: dict, user) -> str:
    """Execute a web search the model itself chose to issue via native
    tool-calling, and return the retrieved content as a string for a
    role="tool" message. The decision of *whether* to search, and the query
    itself, both already came out of the model's own tool_calls — this only
    runs the search/scrape/embed/retrieve pipeline and formats the result,
    reusing the same event_emitter status shape as before so the frontend's
    WebSearchResults UI needs no changes.

    web_search is the first browsing-backed tool gated by
    cavekit-browse-access-control.md — checked before the core connection
    is ever invoked (its own R1)."""
    event_emitter = extra_params["__event_emitter__"]

    if not has_browsing_access(user, request.app.state.config.USER_PERMISSIONS):
        return "Web browsing is not permitted for this account."

    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "web_search",
                "description": 'Searching "{{searchQuery}}"',
                "query": query,
                "done": False,
            },
        }
    )

    try:
        # Search-PROVIDER lookup (SearXNG/etc, per RAG_WEB_SEARCH_ENGINE) is
        # unchanged — already pluggable, out of scope for this migration.
        # What's replaced is the scrape step below: cavekit-browse-connection
        # instead of the domain-crawl backend (self.crawl/Firecrawl), which
        # this tool no longer sends any traffic to (cavekit-browse-search
        # -migration.md R1, R7).
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as executor:
            search_results = await loop.run_in_executor(
                executor,
                lambda: search_web(request, request.app.state.config.RAG_WEB_SEARCH_ENGINE, query),
            )

        if not search_results:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "web_search",
                        "description": "No search results found",
                        "query": query,
                        "done": True,
                        "error": True,
                    },
                }
            )
            return "No search results found."

        profile = resolve_profile("general-search")

        # R2: fetched concurrently, not one after another — total wall-clock
        # is bounded by the slowest single fetch's timeout, not the sum of
        # all of them. R3 (fast-fail) falls out of this for free: a
        # blocked/slow URL can't delay the others since they're all
        # in flight at once, and general-search's retry_count=0 means no
        # URL gets retried extensively either.
        fetch_results = await asyncio.gather(
            *(browse_fetch(request, result.link, profile) for result in search_results)
        )

        docs: list[LCDocument] = []
        fetched_urls: list[str] = []
        for result, fetch_result in zip(search_results, fetch_results):
            if not fetch_result.success:
                log.debug(f"web_search: skipping {result.link}: {fetch_result.error}")
                continue

            text, title = html_to_text(fetch_result.content)
            if not text.strip():
                continue

            docs.append(
                LCDocument(
                    page_content=text,
                    metadata={"source": result.link, "title": title or result.title or ""},
                )
            )
            fetched_urls.append(result.link)

        if not docs:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "web_search",
                        "description": "No search results found",
                        "query": query,
                        "done": True,
                        "error": True,
                    },
                }
            )
            return "No search results found."

        collection_name = f"web-search-{calculate_sha256_string(query)}"[:63]

        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(
                executor,
                lambda: save_docs_to_vector_db(request, docs, collection_name, overwrite=True),
            )

        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_search",
                    "description": "Searched {{count}} sites",
                    "query": query,
                    "urls": fetched_urls,
                    "done": True,
                },
            }
        )

        sources = get_sources_from_files(
            files=[
                {
                    "collection_name": collection_name,
                    "name": query,
                    "type": "web_search_results",
                    "urls": fetched_urls,
                }
            ],
            queries=[query],
            embedding_function=request.app.state.EMBEDDING_FUNCTION,
            k=request.app.state.config.TOP_K,
            reranking_function=request.app.state.rf,
            r=request.app.state.config.RELEVANCE_THRESHOLD,
            hybrid_search=request.app.state.config.ENABLE_RAG_HYBRID_SEARCH,
        )

        parts = []
        for source in sources:
            name = source.get("source", {}).get("name", "web result")
            for doc in source.get("document", []):
                parts.append(f'<source name="{name}">\n{doc}\n</source>')

        return "\n\n".join(parts) if parts else "No relevant content retrieved."

    except Exception as e:
        log.exception(e)
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_search",
                    "description": 'Error searching "{{searchQuery}}"',
                    "query": query,
                    "done": True,
                    "error": True,
                },
            }
        )
        return f"Web search failed: {e}"


MAX_TOOL_CALL_ROUNDS = 5


async def _dispatch_tool_call(
    request: Request, tool_call: dict, admin_tools: dict, extra_params: dict, user, messages: list
) -> str:
    """Execute one tool_calls entry (web_search or an admin Tool) and return
    its string result for a role="tool" message. Shared by the buffered and
    streaming round-runners so dispatch logic lives in exactly one place."""
    function = tool_call.get("function", {})
    name = function.get("name")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except Exception:
        arguments = {}

    if name == "web_search":
        query = arguments.get("query") or get_last_user_message(messages)
        return await run_web_search_tool_call(request, query, extra_params, user)

    if name in admin_tools:
        event_emitter = extra_params["__event_emitter__"]
        await event_emitter(
            {"type": "status", "data": {"action": "tool_calls", "description": f"Calling {name}...", "done": False}}
        )
        try:
            allowed_params = admin_tools[name].get("spec", {}).get("parameters", {}).get("properties", {})
            tool_function = admin_tools[name]["callable"]
            filtered_args = {k: v for k, v in arguments.items() if k in allowed_params}
            tool_output = await tool_function(**filtered_args)
            if not isinstance(tool_output, str):
                tool_output = json.dumps(tool_output)
        except Exception as e:
            tool_output = str(e)
        await event_emitter(
            {"type": "status", "data": {"action": "tool_calls", "description": f"Called {name}", "done": True}}
        )
        return tool_output

    return f"Unknown tool: {name}"


async def _run_tool_calling_buffered(
    request: Request, form_data: dict, messages: list, openai_tools: list, admin_tools: dict, extra_params: dict, user
):
    """Non-streaming tool-calling loop: each round is a single non-streaming
    completion call, easy to inspect for tool_calls. Used whenever the
    client asked for stream=False (e.g. eval jobs) — those want a plain
    dict response anyway, so there's nothing to gain from streaming
    internally."""
    original_stream = form_data.get("stream", False)

    for _ in range(MAX_TOOL_CALL_ROUNDS):
        round_payload = {
            **form_data,
            "messages": messages,
            "tools": openai_tools,
            "tool_choice": "auto",
            "stream": False,
        }

        try:
            response = await generate_chat_completion(request, round_payload, user)
        except Exception as e:
            # Model/template likely doesn't support tool-calling — fall back
            # to a plain completion rather than failing the whole turn.
            log.exception(e)
            return await generate_chat_completion(
                request, {**form_data, "messages": messages, "stream": original_stream}, user
            )

        message = (response.get("choices") or [{}])[0].get("message", {})
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            return response

        messages.append(message)

        for tool_call in tool_calls:
            tool_output = await _dispatch_tool_call(request, tool_call, admin_tools, extra_params, user, messages)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", tool_call.get("function", {}).get("name")),
                    "content": tool_output,
                }
            )

    # Exceeded MAX_TOOL_CALL_ROUNDS while the model kept calling tools —
    # force a final answer without offering any more.
    return await generate_chat_completion(request, {**form_data, "messages": messages, "stream": original_stream}, user)


async def _stream_tool_calling(
    request: Request, form_data: dict, messages: list, openai_tools: list, admin_tools: dict, extra_params: dict, user
):
    """Streaming tool-calling loop: each round is a real streaming
    completion call. content deltas are relayed to the client the instant
    they arrive — nothing buffers them — while tool_calls deltas are
    accumulated (by index, per the OpenAI/llama.cpp streaming contract:
    the first delta for an index carries id + function.name, every delta
    for that index may carry a function.arguments fragment to concatenate)
    without being relayed, since a tool-deciding round naturally carries
    little or no content to hold back. When a round ends with accumulated
    tool_calls, they're executed and the loop opens the *next* streaming
    round, transparently continuing the same outer stream — so a model
    that narrates before calling a tool ("let me check...") has that
    narration stream live too, then the real answer streams live right
    after the tool result comes back.
    """

    async def iter_lines(response):
        # response.body_iterator (aiohttp StreamReader under the hood, for
        # llamolotl-owned models) yields one already-complete line per item —
        # the same assumption process_chat_response's own SSE consumption
        # relies on.
        async for raw in response.body_iterator:
            yield raw.decode("utf-8") if isinstance(raw, bytes) else raw

    async def close(response):
        if response.background is not None:
            await response.background()

    for _ in range(MAX_TOOL_CALL_ROUNDS):
        round_payload = {
            **form_data,
            "messages": messages,
            "tools": openai_tools,
            "tool_choice": "auto",
            "stream": True,
        }

        try:
            response = await generate_chat_completion(request, round_payload, user)
        except Exception as e:
            log.exception(e)
            try:
                fallback = await generate_chat_completion(
                    request, {**form_data, "messages": messages, "tools": None, "stream": True}, user
                )
                async for line in iter_lines(fallback):
                    yield line
                await close(fallback)
            except Exception as e2:
                log.exception(e2)
                yield f"data: {json.dumps({'error': {'message': str(e2)}})}\n\n"
                yield "data: [DONE]\n\n"
            return

        tool_calls_by_index = {}
        content_parts = []

        async for line in iter_lines(response):
            if not line.strip() or not line.startswith("data: "):
                continue
            data_str = line[len("data: ") :]
            if data_str.strip() == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
            except Exception:
                continue

            choices = chunk.get("choices") or []
            if not choices:
                # Trailer chunk (usage/timings with no choices) — nothing to relay.
                continue

            delta = choices[0].get("delta", {}) or {}

            content = delta.get("content")
            if content:
                content_parts.append(content)
                yield f"data: {json.dumps(chunk)}\n\n"

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                entry = tool_calls_by_index.setdefault(
                    idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                fct = tc.get("function") or {}
                if fct.get("name"):
                    entry["function"]["name"] += fct["name"]
                if fct.get("arguments"):
                    entry["function"]["arguments"] += fct["arguments"]

        await close(response)

        if not tool_calls_by_index:
            # This round was the final answer, and it already streamed live.
            yield "data: [DONE]\n\n"
            return

        tool_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
        messages.append(
            {
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": tool_calls,
            }
        )

        for tool_call in tool_calls:
            tool_output = await _dispatch_tool_call(request, tool_call, admin_tools, extra_params, user, messages)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", tool_call.get("function", {}).get("name")),
                    "content": tool_output,
                }
            )

    # Exceeded MAX_TOOL_CALL_ROUNDS while the model kept calling tools —
    # force a final streamed answer without offering any more.
    try:
        fallback = await generate_chat_completion(
            request, {**form_data, "messages": messages, "tools": None, "stream": True}, user
        )
        async for line in iter_lines(fallback):
            yield line
        await close(fallback)
    except Exception as e:
        log.exception(e)
        yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
        yield "data: [DONE]\n\n"


async def generate_chat_completion_with_tools(request: Request, form_data: dict, user: UserModel):
    """Drive a native OpenAI-spec tool-calling loop: attach the tools array
    to the actual completion request and let the model itself emit
    tool_calls (self.llama already supports this, including
    parallel_tool_calls, per-model template permitting) instead of running a
    separate hand-rolled single-shot JSON-decision prompt beforehand.
    Execute whatever the model calls, feed results back as role="tool"
    messages, and repeat until it answers without calling anything else or
    MAX_TOOL_CALL_ROUNDS is hit.

    Falls straight through to a normal single completion call when there's
    nothing to offer the model (no admin tool_ids, web search off) — zero
    overhead for the common case. Otherwise dispatches to a real streaming
    loop (content relayed live, round-by-round) or the simpler buffered
    loop, matching whatever the client asked for.
    """
    metadata = form_data.get("metadata", {}) or {}

    tool_ids = metadata.get("tool_ids", None)
    features = metadata.get("features", None) or {}
    web_search_enabled = bool(features.get("web_search"))

    if not tool_ids and not web_search_enabled:
        return await generate_chat_completion(request, form_data, user)

    models = request.app.state.MODELS
    event_emitter = get_event_emitter(metadata)
    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": get_event_call(metadata),
        "__user__": {"id": user.id, "email": user.email, "name": user.name, "role": user.role},
        "__metadata__": metadata,
        "__request__": request,
    }

    task_model_id = get_task_model_id(
        form_data["model"],
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    admin_tools = (
        get_tools(
            request,
            tool_ids,
            user,
            {
                **extra_params,
                "__model__": models[task_model_id],
                "__messages__": form_data["messages"],
                "__files__": metadata.get("files", []),
            },
        )
        if tool_ids
        else {}
    )

    openai_tools = [{"type": "function", "function": tool["spec"]} for tool in admin_tools.values()]
    if web_search_enabled:
        openai_tools.append({"type": "function", "function": WEB_SEARCH_TOOL_SPEC})

    messages = list(form_data["messages"])

    if not form_data.get("stream", True):
        return await _run_tool_calling_buffered(
            request, form_data, messages, openai_tools, admin_tools, extra_params, user
        )

    return StreamingResponse(
        _stream_tool_calling(request, form_data, messages, openai_tools, admin_tools, extra_params, user),
        media_type="text/event-stream",
    )


async def chat_completion_files_handler(request: Request, body: dict, user: UserModel) -> tuple[dict, dict[str, list]]:
    sources = []

    if files := body.get("metadata", {}).get("files", None):
        try:
            queries_response = await generate_queries(
                request,
                TaskFormData(
                    model=body["model"],
                    messages=body["messages"],
                    type="retrieval",
                ),
                user,
            )
            queries_response = queries_response["choices"][0]["message"]["content"]

            try:
                bracket_start = queries_response.find("{")
                bracket_end = queries_response.rfind("}") + 1

                if bracket_start == -1 or bracket_end == -1:
                    raise Exception("No JSON object found in the response")

                queries_response = queries_response[bracket_start:bracket_end]
                queries_response = json.loads(queries_response)
            except Exception:
                queries_response = {"queries": [queries_response]}

            queries = queries_response.get("queries", [])
        except Exception:
            queries = []

        if len(queries) == 0:
            queries = [get_last_user_message(body["messages"])]

        sources = get_sources_from_files(
            files=files,
            queries=queries,
            embedding_function=request.app.state.EMBEDDING_FUNCTION,
            k=request.app.state.config.TOP_K,
            reranking_function=request.app.state.rf,
            r=request.app.state.config.RELEVANCE_THRESHOLD,
            hybrid_search=request.app.state.config.ENABLE_RAG_HYBRID_SEARCH,
        )

        log.debug(f"rag_contexts:sources: {sources}")
    return body, {"sources": sources}


def apply_params_to_form_data(form_data, model):
    params = form_data.pop("params", {})
    if model.get("ollama"):
        form_data["options"] = params

        if "format" in params:
            form_data["format"] = params["format"]

        if "keep_alive" in params:
            form_data["keep_alive"] = params["keep_alive"]
    else:
        if "seed" in params:
            form_data["seed"] = params["seed"]

        if "stop" in params:
            form_data["stop"] = params["stop"]

        if "temperature" in params:
            form_data["temperature"] = params["temperature"]

        if "top_p" in params:
            form_data["top_p"] = params["top_p"]

        if "frequency_penalty" in params:
            form_data["frequency_penalty"] = params["frequency_penalty"]
    return form_data


async def process_chat_payload(request, form_data, metadata, user, model):
    form_data = apply_params_to_form_data(form_data, model)
    log.debug(f"form_data: {form_data}")

    event_emitter = get_event_emitter(metadata)
    event_call = get_event_call(metadata)

    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": event_call,
        "__user__": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
        "__metadata__": metadata,
        "__request__": request,
    }

    # Initialize events to store additional event to be sent to the client
    # Initialize contexts and citation
    events = []
    sources = []

    user_message = get_last_user_message(form_data["messages"])
    model_knowledge = model.get("info", {}).get("meta", {}).get("knowledge", False)

    if model_knowledge:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "knowledge_search",
                    "query": user_message,
                    "done": False,
                },
            }
        )

        knowledge_files = []
        for item in model_knowledge:
            if item.get("collection_name"):
                knowledge_files.append(
                    {
                        "id": item.get("collection_name"),
                        "name": item.get("name"),
                        "legacy": True,
                    }
                )
            elif item.get("collection_names"):
                knowledge_files.append(
                    {
                        "name": item.get("name"),
                        "type": "collection",
                        "collection_names": item.get("collection_names"),
                        "legacy": True,
                    }
                )
            else:
                knowledge_files.append(item)

        files = form_data.get("files", [])
        files.extend(knowledge_files)
        form_data["files"] = files

    # features (web_search, etc.) are read later by generate_chat_completion_with_tools
    # via form_data["metadata"]["features"] — not consumed here, just stripped from the
    # payload that eventually goes to the model.
    form_data.pop("features", None)

    try:
        form_data, flags = await chat_completion_filter_functions_handler(request, form_data, model, extra_params)
    except Exception as e:
        raise Exception(f"Error: {e}")

    tool_ids = form_data.pop("tool_ids", None)
    files = form_data.pop("files", None)
    # Remove files duplicates
    if files:
        files = list({json.dumps(f, sort_keys=True): f for f in files}.values())

    metadata = {
        **metadata,
        "tool_ids": tool_ids,
        "files": files,
    }
    form_data["metadata"] = metadata

    try:
        form_data, flags = await chat_completion_files_handler(request, form_data, user)
        sources.extend(flags.get("sources", []))
    except Exception as e:
        log.exception(e)

    # If context sources are not empty, insert it into the messages
    if len(sources) > 0:
        context_string = ""
        for source_idx, source in enumerate(sources):
            source_id = source.get("source", {}).get("name", "")

            if "document" in source:
                for doc_idx, doc_context in enumerate(source["document"]):
                    metadata = source.get("metadata")
                    doc_source_id = None

                    if metadata:
                        doc_source_id = metadata[doc_idx].get("source", source_id)

                    if source_id:
                        context_string += (
                            f"<source><source_id>{doc_source_id if doc_source_id is not None else source_id}"
                            f"</source_id><source_context>{doc_context}</source_context></source>\n"
                        )
                    else:
                        # If there is no source_id, then do not include the source_id tag
                        context_string += f"<source><source_context>{doc_context}</source_context></source>\n"

        context_string = context_string.strip()
        prompt = get_last_user_message(form_data["messages"])

        if prompt is None:
            raise Exception("No user message found")
        if request.app.state.config.RELEVANCE_THRESHOLD == 0 and context_string.strip() == "":
            log.debug("With a 0 relevancy threshold for RAG, the context cannot be empty")

        # Workaround for Ollama 2.0+ system prompt issue
        # TODO: replace with add_or_update_system_message
        if model["owned_by"] == "ollama":
            form_data["messages"] = prepend_to_first_user_message_content(
                rag_template(request.app.state.config.RAG_TEMPLATE, context_string, prompt),
                form_data["messages"],
            )
        else:
            form_data["messages"] = add_or_update_system_message(
                rag_template(request.app.state.config.RAG_TEMPLATE, context_string, prompt),
                form_data["messages"],
            )

    # If there are citations, add them to the data_items
    sources = [source for source in sources if source.get("source", {}).get("name", "")]

    if len(sources) > 0:
        events.append({"sources": sources})

    if model_knowledge:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "knowledge_search",
                    "query": user_message,
                    "done": True,
                    "hidden": True,
                },
            }
        )

    return form_data, events


async def process_chat_response(request, response, form_data, user, events, metadata, tasks):
    async def background_tasks_handler():
        message_map = Chats.get_messages_by_chat_id(metadata["chat_id"])
        message = message_map.get(metadata["message_id"]) if message_map else None

        if message:
            messages = get_message_list(message_map, message.get("id"))

            if tasks:
                if TASKS.TITLE_GENERATION in tasks:
                    if tasks[TASKS.TITLE_GENERATION]:
                        res = await generate_title(
                            request,
                            TaskFormData(
                                model=message["model"],
                                messages=messages,
                                chat_id=metadata["chat_id"],
                            ),
                            user,
                        )

                        if res and isinstance(res, dict):
                            title = (
                                res.get("choices", [])[0]
                                .get("message", {})
                                .get(
                                    "content",
                                    message.get("content", "New Chat"),
                                )
                            ).strip()

                            if not title:
                                title = messages[0].get("content", "New Chat")

                            Chats.update_chat_title_by_id(metadata["chat_id"], title)

                            await event_emitter(
                                {
                                    "type": "chat:title",
                                    "data": title,
                                }
                            )
                    elif len(messages) == 2:
                        title = messages[0].get("content", "New Chat")

                        Chats.update_chat_title_by_id(metadata["chat_id"], title)

                        await event_emitter(
                            {
                                "type": "chat:title",
                                "data": message.get("content", "New Chat"),
                            }
                        )

                if TASKS.TAGS_GENERATION in tasks and tasks[TASKS.TAGS_GENERATION]:
                    res = await generate_chat_tags(
                        request,
                        TaskFormData(
                            model=message["model"],
                            messages=messages,
                            chat_id=metadata["chat_id"],
                        ),
                        user,
                    )

                    if res and isinstance(res, dict):
                        tags_string = res.get("choices", [])[0].get("message", {}).get("content", "")

                        tags_string = tags_string[tags_string.find("{") : tags_string.rfind("}") + 1]

                        try:
                            tags = json.loads(tags_string).get("tags", [])
                            Chats.update_chat_tags_by_id(metadata["chat_id"], tags, user)

                            await event_emitter(
                                {
                                    "type": "chat:tags",
                                    "data": tags,
                                }
                            )
                        except Exception as e:
                            print(f"Error: {e}")

    event_emitter = None
    if (
        "session_id" in metadata
        and metadata["session_id"]
        and "chat_id" in metadata
        and metadata["chat_id"]
        and "message_id" in metadata
        and metadata["message_id"]
    ):
        event_emitter = get_event_emitter(metadata)

    if not isinstance(response, StreamingResponse):
        if event_emitter:

            if "selected_model_id" in response:
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        "selectedModelId": response["selected_model_id"],
                    },
                )

            if response.get("choices", [])[0].get("message", {}).get("content"):
                content = response["choices"][0]["message"]["content"]

                if content:

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": response,
                        }
                    )

                    title = Chats.get_chat_title_by_id(metadata["chat_id"])

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": {
                                "done": True,
                                "content": content,
                                "title": title,
                            },
                        }
                    )

                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "content": content,
                        },
                    )

                    # Send a webhook notification if the user is not active
                    if get_active_status_by_user_id(user.id) is None:
                        webhook_url = Users.get_user_webhook_url_by_id(user.id)
                        if webhook_url:
                            post_webhook(
                                webhook_url,
                                f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{content}",
                                {
                                    "action": "chat",
                                    "message": content,
                                    "title": title,
                                    "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                                },
                            )

                    await background_tasks_handler()

            return response
        else:
            return response

    if not any(
        content_type in response.headers["Content-Type"]
        for content_type in ["text/event-stream", "application/x-ndjson"]
    ):
        return response

    if event_emitter:

        task_id = str(uuid4())  # Create a unique task ID.

        # Handle as a background task
        async def post_response_handler(response, events):
            message = Chats.get_message_by_id_and_message_id(metadata["chat_id"], metadata["message_id"])
            content = message.get("content", "") if message else ""

            try:
                for event in events:
                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": event,
                        }
                    )

                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            **event,
                        },
                    )

                async for line in response.body_iterator:
                    line = line.decode("utf-8") if isinstance(line, bytes) else line
                    data = line

                    # Skip empty lines
                    if not data.strip():
                        continue

                    # "data: " is the prefix for each event
                    if not data.startswith("data: "):
                        continue

                    # Remove the prefix
                    data = data[len("data: ") :]

                    try:
                        data = json.loads(data)

                        if "selected_model_id" in data:
                            Chats.upsert_message_to_chat_by_id_and_message_id(
                                metadata["chat_id"],
                                metadata["message_id"],
                                {
                                    "selectedModelId": data["selected_model_id"],
                                },
                            )

                        else:
                            value = data.get("choices", [])[0].get("delta", {}).get("content")

                            if value:
                                content = f"{content}{value}"

                                if ENABLE_REALTIME_CHAT_SAVE:
                                    # Save message in the database
                                    Chats.upsert_message_to_chat_by_id_and_message_id(
                                        metadata["chat_id"],
                                        metadata["message_id"],
                                        {
                                            "content": content,
                                        },
                                    )
                                else:
                                    data = {
                                        "content": content,
                                    }

                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": data,
                            }
                        )

                    except Exception:
                        done = "data: [DONE]" in line

                        if done:
                            pass
                        else:
                            continue

                title = Chats.get_chat_title_by_id(metadata["chat_id"])
                data = {"done": True, "content": content, "title": title}

                if not ENABLE_REALTIME_CHAT_SAVE:
                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "content": content,
                        },
                    )

                # Send a webhook notification if the user is not active
                if get_active_status_by_user_id(user.id) is None:
                    webhook_url = Users.get_user_webhook_url_by_id(user.id)
                    if webhook_url:
                        post_webhook(
                            webhook_url,
                            f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{content}",
                            {
                                "action": "chat",
                                "message": content,
                                "title": title,
                                "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                            },
                        )

                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": data,
                    }
                )

                await background_tasks_handler()
            except asyncio.CancelledError:
                print("Task was cancelled!")
                await event_emitter({"type": "task-cancelled"})

                if not ENABLE_REALTIME_CHAT_SAVE:
                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "content": content,
                        },
                    )

            if response.background is not None:
                await response.background()

        # background_tasks.add_task(post_response_handler, response, events)
        task_id, _ = create_task(post_response_handler(response, events))
        return {"status": True, "task_id": task_id}

    else:

        # Fallback to the original response
        async def stream_wrapper(original_generator, events):
            def wrap_item(item):
                return f"data: {item}\n\n"

            for event in events:
                yield wrap_item(json.dumps(event))

            async for data in original_generator:
                yield data

        return StreamingResponse(
            stream_wrapper(response.body_iterator, events),
            headers=dict(response.headers),
            background=response.background,
        )
