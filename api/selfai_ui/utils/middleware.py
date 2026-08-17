import asyncio
import inspect
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlparse
from uuid import uuid4

from fastapi import Request
from langchain_core.documents import Document as LCDocument
from starlette.responses import StreamingResponse

from selfai_ui.browse.access_control import has_browsing_access
from selfai_ui.browse.connection import browse_fetch
from selfai_ui.browse.hop_policy import is_same_site
from selfai_ui.browse.profiles import resolve_profile
from selfai_ui.browse.robots import RobotsCache
from selfai_ui.constants import TASKS
from selfai_ui.env import (
    ENABLE_REALTIME_CHAT_SAVE,
    GLOBAL_LOG_LEVEL,
    SRC_LOG_LEVELS,
)
from selfai_ui.models.chats import Chats
from selfai_ui.models.functions import Functions
from selfai_ui.models.knowledge import Knowledges
from selfai_ui.models.users import UserModel, Users
from selfai_ui.retrieval.utils import get_sources_from_files
from selfai_ui.retrieval.web.utils import validate_url
from selfai_ui.routers.retrieval import (
    ProcessWebCrawlForm,
    _run_crawl_background,
    create_crawl_job,
    save_docs_to_vector_db,
    search_web,
)
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
from selfai_ui.tasks import create_task, register_cancel_hook
from selfai_ui.utils.access_control import has_access
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
from selfai_ui.utils.toolspec import ToolSpec
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


WEB_SEARCH_TOOL_SPEC = ToolSpec(
    name="web_search",
    description=(
        "Search the web for current, real-time, or otherwise unfamiliar information that "
        "is not already available in this conversation. Use this only when the existing "
        "context is insufficient to answer accurately — do not use it for general "
        "knowledge, conversation, or anything already covered above."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A concise, targeted web search query.",
            }
        },
        "required": ["query"],
    },
)


# RFC 3986 scheme grammar. Used to tell "no scheme at all" (a bare host, which
# we may reasonably read as https) from "a scheme that is not http(s)" (which
# must be refused, not rewritten).
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


WEB_FETCH_TOOL_SPEC = ToolSpec(
    name="web_fetch",
    description=(
        "Read the contents of one specific web page whose URL is already known — because "
        "the user gave it, or because it appeared in earlier results in this conversation. "
        "Use this when a particular page needs to be read; use web_search instead when you "
        "need to find out which page to read. Returns the page's text, not a summary."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full absolute URL of the page to read, including its scheme (https://).",
            }
        },
        "required": ["url"],
    },
)


DEEP_RESEARCH_TOOL_SPEC = ToolSpec(
    name="deep_research",
    description=(
        "Research a topic in depth: search the web, read the most promising results, and "
        "follow relevant links from those pages to gather more detail. Use this for "
        "questions that need more than a quick answer — comparisons, how something works, "
        "gathering evidence from several sources. Use web_search instead for a quick "
        "factual lookup, and web_fetch when you already know the one page you need. This "
        "reads several pages and takes noticeably longer than a search."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The research question or topic, phrased as a search query.",
            }
        },
        "required": ["query"],
    },
)


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
        # cavekit-browse-web-access.md R3: anchors in document order, as
        # (raw href, anchor text). Resolution to absolute URLs, filtering, and
        # deduplication all happen in extract_links() — this class's job is
        # only to stop throwing hrefs away, which is what it used to do.
        self._anchors: list[tuple[str, list[str]]] = []
        self._anchor_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a" and self._skip_depth == 0:
            href = next((v for k, v in attrs if k == "href" and v), None)
            if href:
                self._anchors.append((href, []))
                self._anchor_depth += 1

    def handle_startendtag(self, tag, attrs):
        # A self-closing <a/> opens and closes in one token; routing it through
        # handle_starttag would leave _anchor_depth permanently raised and
        # capture every following run of text as that anchor's label.
        if tag in self._SKIP_TAGS or tag == "a":
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._anchor_depth > 0:
            self._anchor_depth -= 1
        if tag in self._BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            self._parts.append(data)
            if self._anchor_depth > 0 and self._anchors:
                self._anchors[-1][1].append(data)

    def get_text(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)

    def get_anchors(self) -> list[tuple[str, str]]:
        return [(href, " ".join("".join(text).split())) for href, text in self._anchors]


def html_to_text(html: str) -> tuple[str, str]:
    """R4: extract (readable_text, title) from HTML. Never raises — a
    parse failure on genuinely malformed input just yields what could be
    recovered rather than failing the whole search."""
    text, title, _links = html_to_text_and_links(html)
    return text, title


# cavekit-browse-web-access.md R3: schemes that never navigate to a fetchable
# document. Excluded before resolution so they can never reach a fetch.
_NON_NAVIGATIONAL_SCHEMES = ("javascript:", "mailto:", "data:", "tel:", "sms:", "blob:", "file:")


def html_to_text_and_links(
    html: str, base_url: Optional[str] = None, max_links: Optional[int] = None
) -> tuple[str, str, list[tuple[str, str]]]:
    """R3: extract (readable_text, title, links) from HTML, where each link is
    an ``(absolute_url, link_text)`` pair.

    `base_url` is the address the HTML was fetched from; relative and
    root-relative hrefs are resolved against it. Without it, only already-
    absolute http(s) URLs survive — a relative href has no meaning we can
    honestly guess at, so it is dropped rather than fabricated.

    Links are deduplicated by destination (first occurrence wins, so document
    order is preserved and the cap is deterministic rather than dependent on
    set iteration) and truncated to `max_links`.

    Never raises, for the same reason html_to_text doesn't: malformed markup on
    a real page must degrade to a partial result, not fail the caller.
    """
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception as e:
        log.warning(f"html_to_text_and_links: parse error, using partial result: {e}")

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in parser.get_anchors():
        href = href.strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(_NON_NAVIGATIONAL_SCHEMES):
            continue

        absolute = urljoin(base_url, href) if base_url else href

        # Post-resolution scheme check: only http(s) is fetchable, and a base
        # URL cannot turn a non-web scheme into one.
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue

        # The fragment names a position within a document, not a different
        # document — dropping it collapses the several in-page anchors a page
        # typically carries into the one destination they actually share.
        absolute = urldefrag(absolute).url

        if absolute in seen:
            continue
        seen.add(absolute)
        links.append((absolute, text))

        if max_links is not None and len(links) >= max_links:
            break

    return parser.get_text(), parser.title.strip(), links


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
            # Distinct from the stage-1 "no results" case above: the search
            # engine DID return links, we just couldn't read any of the pages
            # (bot-protection, 5xx, timeouts — general-search does not retry).
            # Report it as its own failure so the user and the model can tell
            # "search found nothing" apart from "search found links but every
            # fetch failed" — the two used to collapse into one message.
            found = len(search_results)
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "web_search",
                        "description": f"Found {found} result(s) but could not read any page",
                        "query": query,
                        "done": True,
                        "error": True,
                    },
                }
            )
            return (
                f"Web search found {found} result link(s) for this query, but none of "
                "the pages could be retrieved (they may be bot-protected, rate-limited, "
                "or temporarily unavailable). No page content is available."
            )

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


async def run_web_fetch_tool_call(request: Request, url: str, extra_params: dict, user) -> str:
    """R1: read one page the model named, through the core Playwright
    connection under the direct-fetch profile.

    Deliberately NOT the search pipeline. No search provider is contacted (R1),
    and the result is the page's own readable text rather than the excerpts of
    it that best match some query (R2) — "read this page" is a different
    question from "what in this page matches my query", and there may be no
    query at all.

    This tool is a leaf: it never follows a link. Multi-hop traversal is
    deep_research's job (R4), where it can be budgeted (R5) and where hops
    sourced from page content can be constrained (R6).
    """
    event_emitter = extra_params["__event_emitter__"]

    # cavekit-browse-access-control.md R1: checked before the connection is
    # ever invoked, exactly as web_search does.
    if not has_browsing_access(user, request.app.state.config.USER_PERMISSIONS):
        return "Web browsing is not permitted for this account."

    url = (url or "").strip()
    if not url:
        return "No URL was provided to read."

    # A bare "example.com/page" is what a model most often emits when it means
    # a URL. Assume https for it — but only when there is genuinely no scheme.
    # Testing for "://" is not good enough: "javascript:alert(1)" contains no
    # "://", so that test would prepend a scheme to it and hand urlparse a
    # string that superficially parses as an https URL with a netloc.
    if not _URL_SCHEME_RE.match(url):
        url = f"https://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        # R1/R2: a stated failure, never empty content presented as success.
        return f"Cannot read {url!r}: only http and https URLs can be read."

    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "web_fetch",
                "description": "Reading {{url}}",
                "url": url,
                "done": False,
            },
        }
    )

    async def _failed(message: str) -> str:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_fetch",
                    "description": "Could not read {{url}}",
                    "url": url,
                    "done": True,
                    "error": True,
                },
            }
        )
        return message

    try:
        profile = resolve_profile("direct-fetch")
        fetch_result = await browse_fetch(request, url, profile)

        if not fetch_result.success:
            return await _failed(f"Could not read {url}: {fetch_result.error}")

        text, title, _links = html_to_text_and_links(fetch_result.content, base_url=url)

        if not text.strip():
            # R2: no usable text is a stated failure, not an empty success.
            return await _failed(f"Could not read {url}: the page returned no readable text.")

        # R2: bound what the model receives, and say so when it was cut. A
        # truncated page must never be indistinguishable from a complete one.
        max_chars = request.app.state.config.BROWSE_FETCH_MAX_CHARS
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_fetch",
                    "description": "Read {{url}}",
                    "url": url,
                    "title": title,
                    "done": True,
                },
            }
        )

        # R7: attributed to the origin it came from. Everything inside this
        # boundary is untrusted third-party text, and the model is never handed
        # it without the source in view.
        header = f'<source name="{title or url}" url="{url}">'
        footer = "</source>"
        if truncated:
            footer = (
                f"\n[Truncated: this page was longer than {max_chars} characters and was cut "
                f"off here. The text above is the beginning of the page, not all of it.]\n"
            ) + footer

        return f"{header}\n{text}\n{footer}"

    except Exception as e:
        log.exception(e)
        return await _failed(f"Could not read {url}: {e}")


async def run_deep_research_tool_call(request: Request, query: str, extra_params: dict, user) -> str:
    """R4: search, read, follow links from what was read, and assemble — the
    whole traversal inside a single tool call.

    Why one call rather than letting the outer tool-calling loop do the hopping:
    MAX_TOOL_CALL_ROUNDS is a budget shared by every tool in a turn, so a
    research chain run through it would starve everything else and would have
    its depth governed by a constant that exists for an unrelated reason.
    Keeping traversal here is also what makes the budget enforceable (R5) and
    the hop policy applicable (R6) at all.
    """
    event_emitter = extra_params["__event_emitter__"]

    if not has_browsing_access(user, request.app.state.config.USER_PERMISSIONS):
        return "Web browsing is not permitted for this account."

    config = request.app.state.config
    # R5: read once, at the start. These come from configuration and never from
    # the model's arguments — deep_research's tool spec exposes only `query`.
    max_depth = config.DEEP_RESEARCH_MAX_DEPTH
    max_pages = config.DEEP_RESEARCH_MAX_PAGES
    max_seconds = config.DEEP_RESEARCH_MAX_SECONDS
    concurrency = max(1, config.DEEP_RESEARCH_CONCURRENCY)
    per_page_chars = config.DEEP_RESEARCH_MAX_CHARS_PER_PAGE
    max_links = config.BROWSE_MAX_LINKS_PER_PAGE
    max_crawl_delay = config.DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS

    loop = asyncio.get_running_loop()
    # R5: one deadline for the WHOLE traversal. Bounding each fetch instead
    # would let N pages at the profile timeout add up without limit.
    deadline = loop.time() + max_seconds

    # robots.txt honoring. deep_research follows links across a site, which is
    # crawler behavior, so it respects the file when enabled: a disallowed URL
    # is skipped, and a declared Crawl-delay spaces out fetches to that origin.
    # One cache for the whole traversal (fetch each origin's robots.txt once).
    robots = RobotsCache(config.BROWSE_USER_AGENT) if config.DEEP_RESEARCH_RESPECT_ROBOTS else None
    # Per-origin next-available time and lock: same-origin fetches serialize and
    # space by the Crawl-delay; different origins stay concurrent.
    origin_next_ok: dict[tuple, float] = {}
    origin_locks: dict[tuple, asyncio.Lock] = {}
    disallowed_by_robots = 0

    async def _gated_fetch(url: str):
        """browse_fetch, gated by robots.txt when honoring is on. Returns
        ("ok", result) | ("fail", result) | ("robots", None). The "robots" kind
        is a URL the origin disallows for our agent, or one whose Crawl-delay
        can't be honored inside the remaining budget — counted, not fetched."""
        if robots is not None:
            if not await robots.is_allowed(request, url, profile):
                return ("robots", None)
            delay = await robots.crawl_delay(request, url, profile)
            if delay:
                if delay > max_crawl_delay:
                    # Honoring it would stall this origin past our cap; don't
                    # sleep that long, just leave the origin alone.
                    return ("robots", None)
                origin = (urlparse(url).scheme, urlparse(url).netloc)
                lock = origin_locks.setdefault(origin, asyncio.Lock())
                async with lock:
                    wait = origin_next_ok.get(origin, 0.0) - loop.time()
                    if wait > 0:
                        if loop.time() + wait >= deadline:
                            # Waiting would blow the whole-traversal deadline.
                            return ("robots", None)
                        await asyncio.sleep(wait)
                    origin_next_ok[origin] = loop.time() + delay
                    result = await browse_fetch(request, url, profile)
                    return ("ok" if result.success else "fail", result)
        result = await browse_fetch(request, url, profile)
        return ("ok" if result.success else "fail", result)

    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "deep_research",
                "description": 'Researching "{{searchQuery}}"',
                "query": query,
                "urls": [],
                "done": False,
            },
        }
    )

    async def _terminal(description: str, urls: list, error: bool = False) -> None:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "deep_research",
                    "description": description,
                    "query": query,
                    "urls": urls,
                    "done": True,
                    **({"error": True} if error else {}),
                },
            }
        )

    try:
        with ThreadPoolExecutor() as executor:
            search_results = await loop.run_in_executor(
                executor,
                lambda: search_web(request, config.RAG_WEB_SEARCH_ENGINE, query),
            )

        if not search_results:
            await _terminal("No search results found", [], error=True)
            return "No search results found."

        profile = resolve_profile("link-follow")

        # R6: depth-0 URLs came from the search provider, not from any page's
        # content, so no fetched page chose them — they are eligible whatever
        # their origin. Everything discovered later is subject to the hop policy.
        frontier: list[tuple[str, int]] = [(result.link, 0) for result in search_results]
        queued: set[str] = {url for url, _ in frontier}

        pages: list[dict] = []
        refused_hops = 0
        stop_reason = ""

        while frontier and len(pages) < max_pages:
            remaining = deadline - loop.time()
            if remaining <= 0:
                stop_reason = f"the {max_seconds}s time limit was reached"
                break

            # Never fetch more than the remaining page budget, so the last
            # batch cannot overshoot max_pages.
            batch = frontier[: min(concurrency, max_pages - len(pages))]
            frontier = frontier[len(batch) :]

            try:
                fetched = await asyncio.wait_for(
                    asyncio.gather(
                        *(_gated_fetch(url) for url, _ in batch),
                        return_exceptions=True,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                stop_reason = f"the {max_seconds}s time limit was reached"
                break

            for (url, depth), outcome in zip(batch, fetched):
                if isinstance(outcome, BaseException):
                    log.debug(f"deep_research: skipping {url}: {outcome}")
                    continue
                kind, fetch_result = outcome
                if kind == "robots":
                    # Disallowed by robots.txt, or a Crawl-delay we won't sit
                    # out. Recorded, not silently dropped.
                    disallowed_by_robots += 1
                    log.debug(f"deep_research: robots.txt skip {url}")
                    continue
                if kind != "ok" or not fetch_result.success:
                    # R4: a failed hop excludes that page; it never fails the call.
                    log.debug(f"deep_research: skipping {url}: {fetch_result and fetch_result.error}")
                    continue

                text, title, links = html_to_text_and_links(fetch_result.content, base_url=url, max_links=max_links)
                if not text.strip():
                    continue

                pages.append({"url": url, "title": title, "text": text, "depth": depth})

                if depth + 1 > max_depth:
                    continue

                for link_url, _link_text in links:
                    if link_url in queued:
                        continue
                    if not is_same_site(url, link_url):
                        # R6: recorded, not silently dropped. This is the branch
                        # that refuses an injected page's attempt to steer the
                        # traversal off-site.
                        refused_hops += 1
                        log.debug(f"deep_research: refusing off-site hop {url} -> {link_url}")
                        continue
                    queued.add(link_url)
                    frontier.append((link_url, depth + 1))

            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "deep_research",
                        "description": "Read {{count}} pages",
                        "query": query,
                        "urls": [page["url"] for page in pages],
                        "done": False,
                    },
                }
            )

        if not stop_reason and len(pages) >= max_pages:
            stop_reason = f"the {max_pages}-page limit was reached"

        if not pages:
            await _terminal("No pages could be read", [], error=True)
            return f'No pages could be read while researching "{query}".'

        urls = [page["url"] for page in pages]
        await _terminal("Read {{count}} pages", urls)

        # R7: every block attributed to the origin it came from. All of this is
        # untrusted third-party text and is never handed over unattributed.
        parts = []
        for page in pages:
            text = page["text"]
            note = ""
            if len(text) > per_page_chars:
                text = text[:per_page_chars]
                note = f"\n[Excerpt: this page was longer than {per_page_chars} characters and was cut off here.]"
            parts.append(f'<source name="{page["title"] or page["url"]}" url="{page["url"]}">\n{text}{note}\n</source>')

        # R5: say what happened. Reaching a limit is not an error, but it must
        # not look like the traversal ran to completion either.
        preamble = f'Researched "{query}" — read {len(pages)} page(s).'
        if stop_reason:
            preamble += f" Stopped early because {stop_reason}; there may be more to find."
        if refused_hops:
            preamble += f" {refused_hops} off-site link(s) found on those pages were not followed."
        if disallowed_by_robots:
            preamble += f" {disallowed_by_robots} URL(s) were skipped to respect robots.txt."

        return preamble + "\n\n" + "\n\n".join(parts)

    except Exception as e:
        log.exception(e)
        await _terminal('Error researching "{{searchQuery}}"', [], error=True)
        return f"Research failed: {e}"


WEB_CRAWL_TOOL_SPEC = ToolSpec(
    name="web_crawl",
    description=(
        "Crawl a website and save its pages into the knowledge base the user selected for this "
        "conversation. Use this when the user wants a site ingested for later reference — "
        "documentation, a wiki, a blog archive — rather than answered right now. The crawl runs "
        "in the background and can take several minutes; this returns immediately with a job "
        "reference, not the page contents. Use web_search or deep_research instead when the user "
        "wants an answer in this conversation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The site or page URL to start crawling from, including its scheme (https://).",
            }
        },
        "required": ["url"],
    },
)


async def run_web_crawl_tool_call(request: Request, url: str, extra_params: dict, user) -> str:
    """Start a Knowledge-Base domain crawl into the KB the user bound to this
    conversation, and return a handle. Fire-and-report: the crawl runs in the
    background and this never waits for it.

    This is the same crawl the KB UI runs — same ProcessWebCrawlForm, same
    create_crawl_job registration, same _run_crawl_background pipeline (so the
    same batched embedding, job persistence, startup-resume, and robots.txt
    Crawl-delay honoring). What differs is only who starts it and how the task
    is scheduled: a request handler has BackgroundTasks, a tool call does not.
    """
    event_emitter = extra_params["__event_emitter__"]
    metadata = extra_params.get("__metadata__") or {}
    config = request.app.state.config

    if not has_browsing_access(user, config.USER_PERMISSIONS):
        return "Web browsing is not permitted for this account."

    # The admin flag is checked here too, not just at offer time: a stale client
    # must not keep writing into knowledge bases after an admin turns this off.
    if not config.ENABLE_WEB_CRAWL:
        return "Web crawl is not enabled on this instance."

    if not (metadata.get("features") or {}).get("web_crawl"):
        return "Web crawl is not enabled for this conversation."

    kb_id = metadata.get("web_crawl_kb_id")
    if not kb_id:
        return "No knowledge base is selected for web crawl. Choose one in the Web Crawl settings first."

    knowledge = Knowledges.get_knowledge_by_id(kb_id)
    if knowledge is None:
        return "The selected knowledge base no longer exists. Choose another in the Web Crawl settings."

    # D3: the picker only ever shows write-access knowledge bases, but that is a
    # convenience, not the boundary. Re-check here, because the side effect is
    # writing into someone's knowledge base and the request metadata is
    # attacker-shaped input like any other.
    if not (knowledge.user_id == user.id or has_access(user.id, "write", knowledge.access_control)):
        log.warning(f"web_crawl: refused — user {user.id} lacks write access to KB {kb_id}")
        return "You do not have write access to the selected knowledge base."

    if not config.FIRECRAWL_API_KEY and not config.FIRECRAWL_API_BASE_URL:
        return "Crawling is not configured on this instance (no Firecrawl endpoint)."

    url = (url or "").strip()
    if not url:
        return "No URL was provided to crawl."
    if not _URL_SCHEME_RE.match(url):
        url = f"https://{url}"

    try:
        validate_url(url)
    except Exception as e:
        return f"Cannot crawl {url!r}: {e}"

    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "web_crawl",
                "description": "Crawling {{url}}",
                "url": url,
                "knowledge_name": knowledge.name,
                "done": False,
            },
        }
    )

    try:
        # D6: budget is admin configuration, never a tool argument.
        form_data = ProcessWebCrawlForm(
            url=url,
            collection_name=knowledge.id,
            limit=config.WEB_CRAWL_MAX_PAGES,
            max_depth=config.WEB_CRAWL_MAX_DEPTH,
        )
        job_state = create_crawl_job(request, form_data, knowledge.id, user.id)

        # D4: fire-and-report. A request handler would hand this to
        # BackgroundTasks; a tool call schedules it itself. Held on app.state so
        # the task isn't garbage-collected mid-crawl.
        task = asyncio.create_task(
            _run_crawl_background(request, job_state, form_data, knowledge.id, user.id)
        )
        if not hasattr(request.app.state, "crawl_tasks"):
            request.app.state.crawl_tasks = set()
        request.app.state.crawl_tasks.add(task)
        task.add_done_callback(request.app.state.crawl_tasks.discard)

        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_crawl",
                    "description": "Started crawling {{url}}",
                    "url": url,
                    "knowledge_name": knowledge.name,
                    "job_id": job_state["job_id"],
                    "done": True,
                },
            }
        )

        return (
            f'Started crawling {url} into the "{knowledge.name}" knowledge base '
            f'(job {job_state["job_id"]}, up to {config.WEB_CRAWL_MAX_PAGES} pages). '
            "This runs in the background and is not finished yet — pages appear in the knowledge "
            "base as they are ingested. Do not summarize the site from this result; tell the user "
            "the crawl has started and that they can ask about the knowledge base once it finishes."
        )

    except Exception as e:
        log.exception(e)
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_crawl",
                    "description": "Could not start crawling {{url}}",
                    "url": url,
                    "done": True,
                    "error": True,
                },
            }
        )
        return f"Could not start crawling {url}: {e}"


MAX_TOOL_CALL_ROUNDS = 5

#: The qualifier a built-in tool (web_search and friends) is offered under when a
#: client-supplied tool has claimed its bare name. Built-ins have no `toolkit_id`
#: to qualify by, so they get a fixed one; the resulting name is produced by the
#: same `collide_name` every other qualified tool goes through.
BUILTIN_TOOL_OWNER = "selfai"

#: Handed back to the model as the tool result for a client-owned tool call that
#: arrived in the SAME round as a server-owned one. Such a round cannot be
#: returned to the caller: half of it is already resolved here, and the caller
#: would owe us a result for a tool it has no way to run. So the server side
#: resolves, and the model is asked to re-issue the client call on its own --
#: which then arrives as a client-only round, and that one IS returnable.
CLIENT_TOOL_DEFERRAL = (
    "This tool runs on the client, which cannot be reached in the middle of a turn. "
    "The results of the other tool calls in this round are above. Re-issue this call "
    "on its own, with no other tool call alongside it, and it will be dispatched."
)


def extract_client_tools(form_data: dict) -> list[dict]:
    """The function tools the caller supplied on the request, verbatim.

    Verbatim is the contract: whatever the caller sent is what the model is
    offered and what comes back keeps the caller's spelling, because the caller
    matches the returned `tool_calls` against its own registry by name.
    Entries that could not be a function tool -- not a dict, a non-function
    `type`, no usable `name` -- are dropped here rather than passed to a
    provider that would reject the whole request over one of them.
    """
    client_tools = []
    for tool in form_data.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") not in (None, "function"):
            continue
        name = (tool.get("function") or {}).get("name")
        if not isinstance(name, str) or not name:
            log.warning("dropping a client-supplied tool with no usable function name: %r", tool)
            continue
        client_tools.append(tool)
    return client_tools


def _tool_call_name(tool_call: dict) -> str:
    return (tool_call.get("function") or {}).get("name") or ""


def _tool_call_id(tool_call: dict) -> str:
    return tool_call.get("id") or _tool_call_name(tool_call)


def _partition_tool_calls(tool_calls: list, client_tool_names: set) -> tuple[list, list]:
    """Split one round's tool_calls into the ones self.ai runs and the ones the
    caller runs. Ownership is by name, and a client name always wins it: the
    merge that built the offer list already qualified any server-side tool that
    wanted a name the caller had claimed, so a bare claimed name reaching here
    can only be the client's."""
    server_calls, client_calls = [], []
    for tool_call in tool_calls:
        target = client_calls if _tool_call_name(tool_call) in client_tool_names else server_calls
        target.append(tool_call)
    return server_calls, client_calls


async def _dispatch_tool_call(
    request: Request,
    tool_call: dict,
    admin_tools: dict,
    extra_params: dict,
    user,
    messages: list,
    builtin_aliases: dict | None = None,
) -> str:
    """Execute one tool_calls entry (web_search or an admin Tool) and return
    its string result for a role="tool" message. Shared by the buffered and
    streaming round-runners so dispatch logic lives in exactly one place.

    `builtin_aliases` maps an offered name back to the built-in it stands for,
    and is non-empty only when a client-supplied tool claimed a built-in's bare
    name on this request. Resolved first, so every branch below still compares
    against the canonical name."""
    function = tool_call.get("function", {})
    name = (builtin_aliases or {}).get(function.get("name"), function.get("name"))
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except Exception:
        arguments = {}

    if name == "web_search":
        query = arguments.get("query") or get_last_user_message(messages)
        return await run_web_search_tool_call(request, query, extra_params, user)

    if name == "web_fetch":
        return await run_web_fetch_tool_call(request, arguments.get("url") or "", extra_params, user)

    if name == "web_crawl":
        return await run_web_crawl_tool_call(request, arguments.get("url") or "", extra_params, user)

    if name == "deep_research":
        # Offered only when its own toggle is on; refused here too, because a
        # model can emit a tool name it was never offered and the offer list is
        # not a security boundary.
        features = (extra_params.get("__metadata__") or {}).get("features") or {}
        if not features.get("deep_research"):
            return "Deep research is not enabled for this conversation."
        query = arguments.get("query") or get_last_user_message(messages)
        return await run_deep_research_tool_call(request, query, extra_params, user)

    if name in admin_tools:
        event_emitter = extra_params["__event_emitter__"]
        await event_emitter(
            {"type": "status", "data": {"action": "tool_calls", "description": f"Calling {name}...", "done": False}}
        )
        try:
            # The declared argument names, read off the typed spec. A missing spec
            # or a schema body with no `properties` yields {}, so an argument the
            # model invented is still dropped rather than raising here.
            spec = admin_tools[name].get("spec")
            allowed_params = (getattr(spec, "input_schema", None) or {}).get("properties") or {}
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
    request: Request,
    form_data: dict,
    messages: list,
    openai_tools: list,
    admin_tools: dict,
    extra_params: dict,
    user,
    client_tool_names: set | None = None,
    builtin_aliases: dict | None = None,
):
    """Non-streaming tool-calling loop: each round is a single non-streaming
    completion call, easy to inspect for tool_calls. Used whenever the
    client asked for stream=False (e.g. eval jobs) — those want a plain
    dict response anyway, so there's nothing to gain from streaming
    internally.

    `client_tool_names` are the tools the CALLER owns. They are offered to the
    model alongside ours but dispatched by the caller, not here: a round made
    entirely of them is returned unresolved, in OpenAI's shape, and the caller
    drives the next request (self.ai#71)."""
    original_stream = form_data.get("stream", False)
    client_tool_names = client_tool_names or set()

    for _ in range(MAX_TOOL_CALL_ROUNDS):
        round_payload = {
            **form_data,
            "messages": messages,
            "tools": openai_tools,
            # A caller that stated a tool_choice gets it honoured; "auto" is the
            # default the WebUI has always run under, not an override.
            "tool_choice": form_data.get("tool_choice") or "auto",
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

        server_calls, client_calls = _partition_tool_calls(tool_calls, client_tool_names)

        if client_calls and not server_calls:
            # Nothing in this round is ours to run. Hand it back whole and let
            # the caller execute and re-post. finish_reason is normalised rather
            # than trusted: a client keys its agent loop off it, and an upstream
            # that says "stop" on a round carrying tool_calls would stall it.
            choices = response.get("choices") or []
            if choices:
                choices[0]["finish_reason"] = "tool_calls"
            return response

        messages.append(message)

        for tool_call in server_calls:
            tool_output = await _dispatch_tool_call(
                request, tool_call, admin_tools, extra_params, user, messages, builtin_aliases
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _tool_call_id(tool_call),
                    "content": tool_output,
                }
            )

        for tool_call in client_calls:
            # Mixed round — see CLIENT_TOOL_DEFERRAL.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _tool_call_id(tool_call),
                    "content": CLIENT_TOOL_DEFERRAL,
                }
            )

    # Exceeded MAX_TOOL_CALL_ROUNDS while the model kept calling tools —
    # force a final answer without offering any more. `tools: None` is what
    # makes that true when the caller supplied its own; without it the caller's
    # array rides along in form_data and the model can keep calling forever.
    return await generate_chat_completion(
        request, {**form_data, "messages": messages, "tools": None, "stream": original_stream}, user
    )


async def _stream_tool_calling(
    request: Request,
    form_data: dict,
    messages: list,
    openai_tools: list,
    admin_tools: dict,
    extra_params: dict,
    user,
    client_tool_names: set | None = None,
    builtin_aliases: dict | None = None,
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

    A round made entirely of CLIENT-owned tool calls ends the stream instead of
    opening another round: the accumulated calls are re-emitted as SSE deltas
    (they were suppressed on the way past, like every other tool_calls delta)
    followed by a `finish_reason: tool_calls` chunk, and the caller executes
    them and posts the results back as a new request (self.ai#71).
    """
    client_tool_names = client_tool_names or set()

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
            # See the buffered loop: a caller's stated tool_choice is honoured.
            "tool_choice": form_data.get("tool_choice") or "auto",
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
        # Kept only to stamp the synthesized client-tool_calls chunks below with
        # the same id/model/created the rest of this stream carried — a client
        # correlating chunks by id must not see a round appear from nowhere.
        chunk_envelope = {}

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

            if not chunk_envelope:
                chunk_envelope = {
                    key: chunk[key] for key in ("id", "created", "model", "system_fingerprint") if key in chunk
                }

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
        server_calls, client_calls = _partition_tool_calls(tool_calls, client_tool_names)

        if client_calls and not server_calls:
            # Every call in this round is the caller's to run. Re-emit them —
            # they were accumulated, not relayed — as one delta chunk carrying
            # the whole assembled array (legal: a client concatenates argument
            # fragments, and a single complete fragment concatenates to itself),
            # then close the round with finish_reason so the caller's agent loop
            # knows it owns the next move.
            emitted = [{"index": i, **call} for i, call in enumerate(client_calls)]
            yield (
                "data: "
                + json.dumps(
                    {
                        **chunk_envelope,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"tool_calls": emitted}, "finish_reason": None}],
                    }
                )
                + "\n\n"
            )
            yield (
                "data: "
                + json.dumps(
                    {
                        **chunk_envelope,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"
            return

        messages.append(
            {
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": tool_calls,
            }
        )

        for tool_call in server_calls:
            tool_output = await _dispatch_tool_call(
                request, tool_call, admin_tools, extra_params, user, messages, builtin_aliases
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _tool_call_id(tool_call),
                    "content": tool_output,
                }
            )

        for tool_call in client_calls:
            # Mixed round — see CLIENT_TOOL_DEFERRAL.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _tool_call_id(tool_call),
                    "content": CLIENT_TOOL_DEFERRAL,
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
    nothing to offer the model (no admin tool_ids, no mod tool this user may
    call, web search off) — zero overhead for the common case. Otherwise
    dispatches to a real streaming loop (content relayed live, round-by-round)
    or the simpler buffered loop, matching whatever the client asked for.

    Tools the CALLER supplied on the request are merged into the same offer,
    never replaced by it (self.ai#71). Before this, `openai_tools` was built
    purely from the server side and splatted over `form_data`, so an
    OpenAI-compatible client that sent its own `tools` had them silently
    substituted away — no error, no warning — and self.ai's own web tools could
    never be used in the same session as a client's. The two halves are
    dispatched by different parties: ours run here, the caller's are returned to
    it as `tool_calls`. Names are the seam, so a client name is never rewritten
    and any server-side tool wanting the same one yields.
    """
    metadata = form_data.get("metadata", {}) or {}

    tool_ids = metadata.get("tool_ids", None)
    features = metadata.get("features", None) or {}
    web_search_enabled = bool(features.get("web_search"))
    # Deep research is its own toggle, not part of Web Search. Searching, and
    # reading a page the user named, are one capability: ask the web something,
    # get a page back. Crawling is a different one -- it reads a dozen pages,
    # follows links between them, and can spend 90s doing it. Someone who
    # enabled "Web Search" did not ask for that, so enabling it must not opt
    # them in.
    deep_research_enabled = bool(features.get("deep_research"))
    # Web Crawl is not a bare boolean: it also needs the knowledge base the user
    # bound to this conversation. Without a destination there is nothing to
    # offer, so both must be present (cavekit/treasuremap D1).
    web_crawl_kb_id = metadata.get("web_crawl_kb_id")
    web_crawl_enabled = bool(features.get("web_crawl")) and bool(web_crawl_kb_id)

    # Mod tools are assembled BEFORE the fall-through gate, because holding one
    # is one of the things that opens it (self.ai#61). `tool_ids` is the user's
    # selected *user-authored* toolkits; a user whose only capabilities come from
    # an installed mod selects nothing, so a gate consulting only `tool_ids` sent
    # them to a plain completion carrying no tools at all. Their mod's tools
    # worked solely as a side effect of also having picked an unrelated toolkit,
    # which is the opposite of why a mod exists.
    #
    # Assembling here rather than below the gate costs nothing on an instance
    # running no mods: `assemble_for_user` returns immediately when nothing is
    # loaded, and is scope-gated per user, so "a mod is enabled" does not become
    # "every request pays for tool calling". It also removes what would be a
    # second assembly pass -- one result both decides the gate and merges below.
    from selfai_ui.mods.tools import assemble_for_user, collide_name, resolve_collisions, yield_names_to

    mod_defaults = getattr(request.app.state.config, "USER_PERMISSIONS", None) or {}
    mod_tool_pairs = assemble_for_user(getattr(request.app.state, "MODS", None), user, defaults=mod_defaults)

    client_tools = extract_client_tools(form_data)
    client_tool_names = {tool["function"]["name"] for tool in client_tools}

    # The fall-through gate is unchanged, and correct for client tools without
    # naming them: with nothing of ours to offer, `form_data` still carries the
    # caller's `tools` untouched, so a plain completion is exactly the
    # passthrough such a request wants. It is only when we DO have something to
    # offer that the two sets have to be merged rather than one overwriting the
    # other -- which is the whole of self.ai#71.
    if (
        not tool_ids
        and not web_search_enabled
        and not deep_research_enabled
        and not web_crawl_enabled
        and not mod_tool_pairs
    ):
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

    # Mod tools: scope-gated per user, in the same dict shape as user-authored
    # tools, merged through the same collision rule. Assembled above the gate;
    # this is only the merge, and it is a no-op for an instance running no mods.
    if mod_tool_pairs:
        admin_tools = resolve_collisions(list(admin_tools.items()) + mod_tool_pairs)

    # A name the caller claimed is the caller's. Our side is re-keyed here, once,
    # before anything is serialized -- dispatch looks a returned call up by
    # `admin_tools` key, so the key and the spec name must move together, which
    # is what `yield_names_to` guarantees. No-op when the caller sent no tools.
    admin_tools = yield_names_to(admin_tools, client_tool_names)

    # Serialized at the wrap site, not carried typed: what goes on the wire is a
    # plain dict, so the round payload below still `json.dumps` with no `default=`
    # hook. `to_openai()` deep-copies the schema body, so nothing downstream of
    # here can reach back into a cached spec.
    openai_tools = [{"type": "function", "function": tool["spec"].to_openai()} for tool in admin_tools.values()]

    # Built-ins have no `toolkit_id`, so a claimed one is qualified under a fixed
    # owner and remembered here: dispatch matches built-ins by literal name, and
    # `builtin_aliases` is what maps the offered name back to it.
    builtin_aliases: dict[str, str] = {}

    def offer_builtin(spec):
        function = spec.to_openai()
        if spec.name in client_tool_names:
            offered = collide_name(BUILTIN_TOOL_OWNER, spec.name)
            builtin_aliases[offered] = spec.name
            function = {**function, "name": offered}
            log.warning(
                "built-in tool %r is claimed by a client-supplied tool on this request; offering it as %r",
                spec.name,
                offered,
            )
        openai_tools.append({"type": "function", "function": function})

    if web_search_enabled:
        # The Web Search toggle covers both ways of getting a page: ask a search
        # engine which page, or name the page directly. Its stored key and label
        # are unchanged.
        offer_builtin(WEB_SEARCH_TOOL_SPEC)
        offer_builtin(WEB_FETCH_TOOL_SPEC)
    if deep_research_enabled:
        offer_builtin(DEEP_RESEARCH_TOOL_SPEC)
    if web_crawl_enabled:
        offer_builtin(WEB_CRAWL_TOOL_SPEC)

    # The caller's own tools, appended verbatim and last. Verbatim because the
    # caller matches what comes back by name; last because ours were qualified
    # around these, so this is the point at which the offer list is complete and
    # collision-free.
    openai_tools.extend(client_tools)

    messages = list(form_data["messages"])

    if not form_data.get("stream", True):
        return await _run_tool_calling_buffered(
            request,
            form_data,
            messages,
            openai_tools,
            admin_tools,
            extra_params,
            user,
            client_tool_names=client_tool_names,
            builtin_aliases=builtin_aliases,
        )

    return StreamingResponse(
        _stream_tool_calling(
            request,
            form_data,
            messages,
            openai_tools,
            admin_tools,
            extra_params,
            user,
            client_tool_names=client_tool_names,
            builtin_aliases=builtin_aliases,
        ),
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
                        # self.ai#35: an eval-window substitution also sets
                        # selected_model_id, but "served != requested" alone
                        # cannot say WHY -- an arena pick looks identical. Persist
                        # the reason so the stored message can explain itself
                        # later, not just at the moment it streamed.
                        **(
                            {"modelSubstitution": response["model_substitution"]}
                            if response.get("model_substitution")
                            else {}
                        ),
                    },
                )

            if response.get("choices", [])[0].get("message", {}).get("content"):
                content = response["choices"][0]["message"]["content"]
                reasoning = response["choices"][0]["message"].get("reasoning_content")

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
                                **({"reasoning": reasoning} if reasoning else {}),
                            },
                        }
                    )

                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "content": content,
                            **({"reasoning": reasoning} if reasoning else {}),
                        },
                    )

                    # Send a webhook notification if the user is not active
                    if await get_active_status_by_user_id(user.id) is None:
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

        # Filled in by post_response_handler as soon as the backend's first SSE
        # chunk arrives. Read lazily by the cancel hook below, so whatever has
        # been seen by the time Stop is pressed is what gets cancelled — an empty
        # dict (generation not started yet) just means no explicit cancel to send.
        upstream_completion: dict = {}

        # Handle as a background task
        async def post_response_handler(response, events):
            message = Chats.get_message_by_id_and_message_id(metadata["chat_id"], metadata["message_id"])
            content = message.get("content", "") if message else ""
            # Reasoning is accumulated separately from content and never merged into it --
            # it is model thinking, not the answer, and inlining it would corrupt the saved
            # message. Providers that don't emit it leave this empty (self.ai#59).
            reasoning = message.get("reasoning", "") if message else ""

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

                        # Remember the upstream completion id so Stop can send an
                        # explicit cancel instead of only dropping our socket
                        # (self.ai#39). First chunk carries it; later chunks repeat
                        # the same value, so only the first assignment matters.
                        if upstream_completion.get("id") is None and data.get("id"):
                            upstream_completion["id"] = data["id"]

                        if "selected_model_id" in data:
                            Chats.upsert_message_to_chat_by_id_and_message_id(
                                metadata["chat_id"],
                                metadata["message_id"],
                                {
                                    "selectedModelId": data["selected_model_id"],
                                    # self.ai#35 -- see the non-streaming branch
                                    # above; the reason rides the same event.
                                    **(
                                        {"modelSubstitution": data["model_substitution"]}
                                        if data.get("model_substitution")
                                        else {}
                                    ),
                                },
                            )

                        else:
                            delta = data.get("choices", [])[0].get("delta", {})
                            value = delta.get("content")
                            reasoning_value = delta.get("reasoning_content")

                            # T-303 (LR/R2). Per-token distributions for a
                            # tokenization session.
                            #
                            # This relay reads exactly two keys and, with
                            # ENABLE_REALTIME_CHAT_SAVE OFF, REPLACES the chunk
                            # with `update` before emitting -- so logprobs
                            # survive today only by accident of configuration.
                            # Captured here, re-attached below, and deliberately
                            # NEVER added to `update`: `update` is what gets
                            # persisted, and the treasuremap estimates order 1 MB
                            # of JSON per 1000-token reply against a chat blob
                            # that is loaded whole (models/chats.py).
                            #
                            # Guarded with a broad except rather than trusting
                            # the shape: a malformed logprobs payload must not
                            # break the rest of the message (R2-AC6).
                            logprobs_payload = None
                            if metadata.get("logprobs_settings"):
                                try:
                                    logprobs_payload = data.get("choices", [])[0].get("logprobs")
                                except Exception:
                                    logprobs_payload = None

                            if reasoning_value:
                                reasoning = f"{reasoning}{reasoning_value}"

                            if value or reasoning_value:
                                if value:
                                    content = f"{content}{value}"

                                update = {"content": content}
                                if reasoning:
                                    update["reasoning"] = reasoning

                                if ENABLE_REALTIME_CHAT_SAVE:
                                    # Save message in the database
                                    Chats.upsert_message_to_chat_by_id_and_message_id(
                                        metadata["chat_id"],
                                        metadata["message_id"],
                                        update,
                                    )
                                else:
                                    # The OFF path replaces the chunk with the
                                    # accumulated update, which is where the
                                    # distributions were being lost. Re-attach
                                    # to the EMITTED payload only -- `update`
                                    # itself, already persisted above in the ON
                                    # path, stays free of them.
                                    data = update
                                    if logprobs_payload is not None:
                                        data = {**update, "logprobs": logprobs_payload}

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
                if reasoning:
                    data["reasoning"] = reasoning

                if not ENABLE_REALTIME_CHAT_SAVE:
                    # Save message in the database
                    final_update = {"content": content}
                    if reasoning:
                        final_update["reasoning"] = reasoning
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        final_update,
                    )

                # Send a webhook notification if the user is not active
                if await get_active_status_by_user_id(user.id) is None:
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

        # Make Stop a real e-stop for locally-served models (self.ai#39).
        #
        # Only llamolotl exposes an explicit cancel; external gateways
        # (GO.*/ZEN.* via the OpenAI surface, anthropic) have no equivalent, so no
        # hook is registered for them and they keep the connection-drop behaviour
        # that is all their APIs offer anyway.
        model_id = form_data.get("model")
        model = (request.app.state.MODELS or {}).get(model_id) or {}

        if model.get("owned_by") == "llamolotl":
            # Imported here rather than at module scope: routers/ imports utils/,
            # so pulling a router in at import time is the direction that risks a
            # cycle. This runs per-response, well after startup.
            from selfai_ui.routers.llamolotl import cancel_chat_completion

            async def _cancel_upstream():
                completion_id = upstream_completion.get("id")
                if not completion_id:
                    # Stop pressed before the backend emitted its first chunk.
                    # There is no id to cancel by; dropping the connection is the
                    # only lever, and it is the one we already pull.
                    return
                await cancel_chat_completion(request, completion_id, model_id)

            register_cancel_hook(task_id, _cancel_upstream)

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
