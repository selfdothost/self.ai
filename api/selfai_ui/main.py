import asyncio
import json
import logging
import mimetypes
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    applications,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

import selfai_ui.config as _selfai_config
from selfai_ui.audio.migration import (
    build_legacy_snapshot,
    migrate_legacy_audio_config,
)
from selfai_ui.config import (
    ADMIN_EMAIL,
    ANTHROPIC_API_CONFIGS,
    ANTHROPIC_BASE_URLS,
    API_KEY_ALLOWED_ENDPOINTS,
    # Audio
    AUDIO_CONNECTION_CONFIGS,
    AUDIO_STT_CONTROL_BASE_URL,
    AUDIO_STT_ENABLED_MODELS,
    AUDIO_STT_ENGINE,
    AUDIO_STT_MODEL,
    AUDIO_STT_OPENAI_API_BASE_URL,
    AUDIO_STT_OPENAI_API_KEY,
    AUDIO_TTS_API_KEY,
    AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT,
    AUDIO_TTS_AZURE_SPEECH_REGION,
    AUDIO_TTS_CONTROL_BASE_URL,
    AUDIO_TTS_ENABLED_VOICES,
    AUDIO_TTS_ENGINE,
    AUDIO_TTS_MODEL,
    AUDIO_TTS_OPENAI_API_BASE_URL,
    AUDIO_TTS_OPENAI_API_KEY,
    AUDIO_TTS_SPLIT_ON,
    AUDIO_TTS_VOICE,
    AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH,
    AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE,
    # Image
    AUTOMATIC1111_API_AUTH,
    AUTOMATIC1111_BASE_URL,
    AUTOMATIC1111_CFG_SCALE,
    AUTOMATIC1111_SAMPLER,
    AUTOMATIC1111_SCHEDULER,
    BING_SEARCH_V7_ENDPOINT,
    BING_SEARCH_V7_SUBSCRIPTION_KEY,
    BRAVE_SEARCH_API_KEY,
    BROWSE_FETCH_MAX_CHARS,
    BROWSE_MAX_LINKS_PER_PAGE,
    BROWSE_PLAYWRIGHT_API_KEY,
    BROWSE_PLAYWRIGHT_SERVICE_URL,
    BROWSE_USER_AGENT,
    CACHE_DIR,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CODE_EVAL_BASE_URLS,
    COMFYUI_API_KEY,
    COMFYUI_BASE_URL,
    COMFYUI_WORKFLOW,
    COMFYUI_WORKFLOW_NODES,
    CONTENT_EXTRACTION_ENGINE,
    CORS_ALLOW_ORIGIN,
    CURATOR_API_CONFIGS,
    CURATOR_BASE_URLS,
    CURATOR_CONTROL_BASE_URL,
    DEEP_RESEARCH_CONCURRENCY,
    DEEP_RESEARCH_MAX_CHARS_PER_PAGE,
    DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS,
    DEEP_RESEARCH_MAX_DEPTH,
    DEEP_RESEARCH_MAX_PAGES,
    DEEP_RESEARCH_MAX_SECONDS,
    DEEP_RESEARCH_RESPECT_ROBOTS,
    DEFAULT_LOCALE,
    DEFAULT_MODELS,
    DEFAULT_PROMPT_SUGGESTIONS,
    DEFAULT_USER_ROLE,
    # Admin
    ENABLE_ADMIN_CHAT_ACCESS,
    ENABLE_ADMIN_EXPORT,
    # Anthropic
    ENABLE_ANTHROPIC_API,
    ENABLE_API_KEY,
    ENABLE_API_KEY_ENDPOINT_RESTRICTIONS,
    ENABLE_AUTOCOMPLETE_GENERATION,
    ENABLE_CHANNELS,
    # code_eval
    ENABLE_CODE_EVAL_API,
    ENABLE_COMMUNITY_SHARING,
    # Curator
    ENABLE_CURATOR_API,
    ENABLE_DEEP_RESEARCH,
    ENABLE_EVALUATION_ARENA_MODELS,
    ENABLE_GOOGLE_DRIVE_INTEGRATION,
    ENABLE_IMAGE_GENERATION,
    # language-eval
    ENABLE_LANGUAGE_EVAL_API,
    # WebUI (LDAP)
    ENABLE_LDAP,
    # Llamolotl
    ENABLE_LLAMOLOTL_API,
    ENABLE_LOGIN_FORM,
    ENABLE_MESSAGE_RATING,
    # WebUI (OAuth)
    ENABLE_OAUTH_ROLE_MANAGEMENT,
    # Ollama
    ENABLE_OLLAMA_API,
    # OpenAI
    ENABLE_OPENAI_API,
    # Piston
    ENABLE_PISTON_EXECUTION,
    ENABLE_RAG_HYBRID_SEARCH,
    ENABLE_RAG_WEB_LOADER_SSL_VERIFICATION,
    ENABLE_RAG_WEB_SEARCH,
    ENABLE_RETRIEVAL_QUERY_GENERATION,
    ENABLE_SEARCH_QUERY_GENERATION,
    # self.corpus
    ENABLE_SELF_CORPUS,
    ENABLE_SIGNUP,
    ENABLE_TAGS_GENERATION,
    ENABLE_WEB_CRAWL,
    # Misc
    ENV,
    EVALUATION_ARENA_MODELS,
    FILE_UPLOAD_MIME_ALLOWLIST,
    FIRECRAWL_API_BASE_URL,
    FIRECRAWL_API_KEY,
    FRONTEND_BUILD_DIR,
    GOOGLE_DRIVE_API_KEY,
    GOOGLE_DRIVE_CLIENT_ID,
    GOOGLE_PSE_API_KEY,
    GOOGLE_PSE_ENGINE_ID,
    # Iceberg
    ICEBERG_BASE_URL,
    IMAGE_GENERATION_ENGINE,
    IMAGE_GENERATION_MODEL,
    IMAGE_SIZE,
    IMAGE_STEPS,
    IMAGES_OPENAI_API_BASE_URL,
    IMAGES_OPENAI_API_KEY,
    JINA_API_KEY,
    JWT_EXPIRES_IN,
    KAGI_SEARCH_API_KEY,
    KB_CRAWL_RESPECT_ROBOTS_DELAY,
    LANGUAGE_EVAL_BASE_URLS,
    LDAP_APP_DN,
    LDAP_APP_PASSWORD,
    LDAP_ATTRIBUTE_FOR_USERNAME,
    LDAP_CA_CERT_FILE,
    LDAP_CIPHERS,
    LDAP_SEARCH_BASE,
    LDAP_SEARCH_FILTERS,
    LDAP_SERVER_HOST,
    LDAP_SERVER_LABEL,
    LDAP_SERVER_PORT,
    LDAP_USE_TLS,
    LLAMOLOTL_API_CONFIGS,
    LLAMOLOTL_BASE_URLS,
    LLAMOLOTL_CONTROL_BASE_URLS,
    MODEL_ORDER_LIST,
    MOJEEK_SEARCH_API_KEY,
    OAUTH_ADMIN_ROLES,
    OAUTH_ALLOWED_ROLES,
    OAUTH_EMAIL_CLAIM,
    OAUTH_PICTURE_CLAIM,
    OAUTH_PROVIDERS,
    OAUTH_ROLES_CLAIM,
    OAUTH_USERNAME_CLAIM,
    OLLAMA_API_CONFIGS,
    OLLAMA_BASE_URLS,
    OPENAI_API_BASE_URLS,
    OPENAI_API_CONFIGS,
    OPENAI_API_KEYS,
    PDF_EXTRACT_IMAGES,
    # Piston
    PISTON_BASE_URL,
    QUERY_GENERATION_PROMPT_TEMPLATE,
    RAG_EMBEDDING_BATCH_SIZE,
    RAG_EMBEDDING_ENGINE,
    RAG_EMBEDDING_MODEL,
    RAG_EMBEDDING_MODEL_AUTO_UPDATE,
    RAG_FILE_MAX_COUNT,
    RAG_FILE_MAX_SIZE,
    RAG_OLLAMA_API_KEY,
    RAG_OLLAMA_BASE_URL,
    RAG_OPENAI_API_BASE_URL,
    RAG_OPENAI_API_KEY,
    RAG_RELEVANCE_THRESHOLD,
    RAG_RERANKING_MODEL,
    RAG_RERANKING_MODEL_AUTO_UPDATE,
    RAG_TEMPLATE,
    RAG_TEXT_SPLITTER,
    RAG_TOP_K,
    RAG_WEB_LOADER_ENGINE,
    RAG_WEB_SEARCH_CONCURRENT_REQUESTS,
    RAG_WEB_SEARCH_DOMAIN_FILTER_LIST,
    # Retrieval (Web Search)
    RAG_WEB_SEARCH_ENGINE,
    RAG_WEB_SEARCH_RESULT_COUNT,
    SEARCHAPI_API_KEY,
    SEARCHAPI_ENGINE,
    SEARXNG_QUERY_URL,
    # self.corpus
    SELF_CORPUS_LAKEFS_ACCESS_KEY_ID,
    SELF_CORPUS_LAKEFS_ENDPOINT,
    SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY,
    SERPER_API_KEY,
    SERPLY_API_KEY,
    SERPSTACK_API_KEY,
    SERPSTACK_HTTPS,
    SHOW_ADMIN_DETAILS,
    SKETCH_CONTROL_BASE_URL,
    STATIC_DIR,
    TAGS_GENERATION_PROMPT_TEMPLATE,
    # Tasks
    TASK_MODEL,
    TASK_MODEL_EXTERNAL,
    TAVILY_API_KEY,
    TIKA_SERVER_URL,
    TIKTOKEN_ENCODING_NAME,
    TITLE_GENERATION_PROMPT_TEMPLATE,
    TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE,
    USER_PERMISSIONS,
    WEB_CRAWL_MAX_DEPTH,
    WEB_CRAWL_MAX_PAGES,
    WEBHOOK_URL,
    # WebUI
    WEBUI_AUTH,
    WEBUI_BANNERS,
    WEBUI_NAME,
    WEBUI_URL,
    WHISPER_MODEL,
    YOUTUBE_LOADER_LANGUAGE,
    YOUTUBE_LOADER_PROXY_URL,
    AppConfig,
    reset_config,
)
from selfai_ui.env import (
    BYPASS_MODEL_ACCESS_CONTROL,
    CHANGELOG,
    DATA_DIR,
    ENABLE_WEBSOCKET_SUPPORT,
    GLOBAL_LOG_LEVEL,
    OFFLINE_MODE,
    RESET_CONFIG_ON_START,
    SAFE_MODE,
    SRC_LOG_LEVELS,
    VERSION,
    WEBUI_AUTH_TRUSTED_EMAIL_HEADER,
    WEBUI_AUTH_TRUSTED_NAME_HEADER,
    WEBUI_BUILD_HASH,
    WEBUI_SECRET_KEY,
    WEBUI_SESSION_COOKIE_SAME_SITE,
    WEBUI_SESSION_COOKIE_SECURE,
)
from selfai_ui.internal.db import Session
from selfai_ui.models.functions import Functions
from selfai_ui.models.models import Models
from selfai_ui.models.users import Users
from selfai_ui.routers import (
    anthropic,
    audio,
    audio_connections,
    auths,
    backups,
    benchmarks,
    channels,
    chats,
    code_eval,
    configs,
    curator,
    custom_evals,
    database,
    evaluations,
    files,
    folders,
    functions,
    groups,
    images,
    knowledge,
    language_eval,
    llamolotl,
    mcp_proxy,
    memories,
    mod_assets,
    mod_frontend_manifest,
    model_versions,
    model_weights,
    models,
    mods,
    ollama,
    openai,
    pipelines,
    prompts,
    queue,
    retrieval,
    system,
    tasks,
    tokenization,
    tools,
    training,
    transcribe,
    users,
    utils,
    voice_catalog,
    voices,
    vram_leases,
    windows,
)
from selfai_ui.routers.retrieval import (
    get_ef,
    get_embedding_function,
    get_rf,
)
from selfai_ui.socket.main import (
    app as socket_app,
)
from selfai_ui.socket.main import (
    periodic_usage_pool_cleanup,
)
from selfai_ui.tasks import list_tasks, stop_task  # Import from tasks.py
from selfai_ui.utils.access_control import has_access
from selfai_ui.utils.auth import (
    decode_token,
    get_admin_user,
    get_verified_user,
)
from selfai_ui.utils.chat import (
    chat_action as chat_action_handler,
)
from selfai_ui.utils.chat import (
    chat_completed as chat_completed_handler,
)
from selfai_ui.utils.chat import (
    generate_completion as completion_handler,
)
from selfai_ui.utils.middleware import (
    generate_chat_completion_with_tools,
    process_chat_payload,
    process_chat_response,
)
from selfai_ui.utils.model_context import inherited_context_fields
from selfai_ui.utils.models import (
    check_model_access,
    get_all_base_models,
    get_all_models,
)
from selfai_ui.utils.oauth import oauth_manager
from selfai_ui.utils.payload import convert_payload_anthropic_to_openai
from selfai_ui.utils.response import (
    convert_response_openai_to_anthropic,
    convert_streaming_response_openai_to_anthropic,
)
from selfai_ui.utils.security_headers import SecurityHeadersMiddleware
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket
from selfai_ui.utils.tokenization import (
    TokenizationParamError,
    assert_model_can_tokenize,
    resolve_logprobs_request,
)

if SAFE_MODE:
    print("SAFE MODE ENABLED")
    Functions.deactivate_all_functions()

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except (HTTPException, StarletteHTTPException) as ex:
            if ex.status_code == 404:
                return await super().get_response("index.html", scope)
            else:
                raise ex


print(
    rf"""
 ____       _  __     _   _ ___
/ ___|  ___| |/ _|   | | | |_ _|
\___ \ / _ \ | |_    | | | || |
 ___) |  __/ |  _| _ | |_| || |
|____/ \___|_|_|  |_|\___/|___|



v{VERSION} - building a self hosted open-source AI user interface.
{f"Commit: {WEBUI_BUILD_HASH}" if WEBUI_BUILD_HASH != "dev-build" else ""}
https://github.com/njcurrin/self.ai/
"""
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if RESET_CONFIG_ON_START:
        reset_config()

    _register_browse_reference_profiles()
    # Mods load synchronously, BEFORE yield: routes must be mounted and scopes
    # seeded before the first request is served, unlike the fire-and-forget
    # background tasks below. boot_mods contains its own per-mod failure
    # isolation, so a broken mod cannot stop the app coming up.
    mods_result = _boot_mods(app)
    # MUST follow _boot_mods: the SPA catch-all is registered at import time and
    # would otherwise shadow every mod route mounted just above (self.ai#119).
    _move_spa_mount_last(app)
    asyncio.create_task(periodic_usage_pool_cleanup())
    asyncio.create_task(_resume_crawl_jobs(app.state))
    asyncio.create_task(_run_gpu_queue(app.state))
    _register_llamolotl_vram_consumer()
    _register_speak_vram_consumer()
    _register_sketch_vram_consumer()
    _register_curator_vram_consumer()
    _register_publish_vram_consumer()
    # Single consumer-aware dispatcher owning the llamolotl, speak AND sketch
    # transports — the broker has one global transport and does not route by
    # consumer_id, so this REPLACES a separate llamolotl install (which the
    # dispatcher subsumes) and prevents self.speak/self.sketch from clobbering the
    # deployed llamolotl transport (cavekit-vram-speak-consumer T-003; Color Phase
    # 2b). The R5 force-reap escalation (context/gpu-lease-force-reap) reads the
    # reaper off the broker, not the transport, so it is unaffected by this
    # dispatcher.
    _install_speak_release_transport(app.state)
    # R5 force-reap capability. Installed LAST of the broker wiring so the
    # trusted target map is in place before any grant can escalate (self.ai#75).
    _install_pod_reaper()
    # R6 consumer VRAM state poller — relays each consumer's real held VRAM into
    # the registry via heartbeat(), closing the drift gap live validation found.
    asyncio.create_task(_run_vram_poller(app.state))
    asyncio.create_task(_ensure_curator_classifier_models(app.state))
    asyncio.create_task(_backfill_self_corpus_repos(app.state))
    asyncio.create_task(_run_model_integrity_sweep(app.state))
    asyncio.create_task(_fail_interrupted_backup_jobs())
    yield
    _drain_mods(mods_result)


def _register_browse_reference_profiles() -> None:
    """Register the reference browse access profiles (general-search,
    weather-search) so they're resolvable for the lifetime of this process
    — cavekit-browse-profiles.md R2/R4."""
    from selfai_ui.browse.reference_profiles import register_reference_profiles

    register_reference_profiles()


def _move_spa_mount_last(app: FastAPI) -> None:
    """Push the SPA catch-all to the END of the route table, after mod routers.

    Starlette matches routes in REGISTRATION ORDER, and the SPA mount is
    ``Mount("/")`` -- it matches every path. It is registered at IMPORT time
    (bottom of this module, guarded by ``os.path.exists(FRONTEND_BUILD_DIR)``),
    while mod routers are mounted during the LIFESPAN by ``boot_mods``. So every
    mod route landed after the catch-all and was shadowed by it.

    The symptom is method-dependent and both halves are nasty:
      * POST /<mod-prefix>/... -> 405, because StaticFiles allows GET/HEAD only.
      * GET  /<mod-prefix>/... -> 200 with index.html, so a caller expecting JSON
        gets a web page and fails somewhere unrelated.

    This only bites when the API also serves the frontend -- i.e. the COMBINED
    IMAGE, which is the deployment we recommend for getting started. The split
    deployment (separate selfai-api / selfai-chat pods) has no frontend build in
    the API pod, takes the ``else`` branch, and was never affected.

    Reordering rather than moving the mount keeps behaviour identical for every
    setup that has no mods: same routes, same mount, only the position changes.
    Idempotent, and a no-op when the SPA is not mounted at all.

    self.ai#119.
    """
    routes = app.router.routes
    spa = [r for r in routes if getattr(r, "name", "") == "spa-static-files"]
    for route in spa:
        routes.remove(route)
        routes.append(route)
    if spa:
        log.info("mods: SPA catch-all moved after mod routes (%d mount(s))", len(spa))


def _boot_mods(app: FastAPI):
    """Discover, load, and seed the enabled mods at startup — the single boot
    entry point for the mods system (cavekit-mods-discovery.md R1/R4). Returns
    the LoadResult so the shutdown drain can run the loaded mods' hooks.

    Import is local so the mods package is only pulled in at startup, matching
    the other lifespan wrappers."""
    from selfai_ui.config import ENABLED_MODS, MODS_INSTALL_DIRS
    from selfai_ui.env import VERSION
    from selfai_ui.mods.loader import boot_mods

    result = boot_mods(app, MODS_INSTALL_DIRS, ENABLED_MODS.value, core_version=VERSION)
    for message in result.errors:
        log.error(message)
    if result.loaded:
        log.info("mods: loaded %s", ", ".join(result.loaded_ids))
    return result


def _drain_mods(result) -> None:
    """Run each loaded mod's shutdown hooks on orderly shutdown."""
    from selfai_ui.mods.loader import run_shutdown_hooks

    run_shutdown_hooks(result)


async def _resume_crawl_jobs(app_state) -> None:
    """Thin wrapper so the import stays local to the lifespan."""
    from selfai_ui.routers.retrieval import resume_crawl_jobs_on_startup

    await resume_crawl_jobs_on_startup(app_state)


async def _fail_interrupted_backup_jobs() -> None:
    """Backup jobs hold no checkpoint, so a restart mid-run leaves a row on
    `running` forever — which reads in the UI as "still working" rather than
    "this never finished". Fail them explicitly at startup (self.ai#93)."""
    from selfai_ui.utils.backup import fail_interrupted_jobs

    await fail_interrupted_jobs()


async def _backfill_self_corpus_repos(app_state) -> None:
    """Catch up any public KB/Dataset rows missing a self.corpus repo — a
    no-op once caught up, but the only automatic way rows created before
    self.corpus was enabled/reachable (self.ai/self.ai#32,
    self.corpus/self.corpus#3) ever get one, since repo creation otherwise
    only fires on KB create. Runs on every startup; every pod restart is
    already this deployment's only "connection (re-)established" signal
    (env-only config, no live admin toggle)."""
    from selfai_ui.utils.self_corpus import backfill_missing_repos

    await backfill_missing_repos(app_state)


async def _run_gpu_queue(app_state) -> None:
    """Start the unified GPU job queue (training + eval + curator)."""
    import selfai_ui.routers.training as training_mod
    import selfai_ui.utils.gpu_queue as gpu_queue

    gpu_queue._app_state = app_state
    training_mod._app_state = app_state
    await gpu_queue.process_gpu_queue_v2()


def _existing_vram_held(consumer_id: str) -> int:
    """Held bytes already recorded for ``consumer_id`` in the registry, or 0 if
    unknown/unreadable.

    Used by the config-driven ``_register_*_vram_consumer`` hooks so a
    re-registration at boot PRESERVES the persisted (and poller-relayed) held
    instead of clobbering it back to 0. ``register()`` is a trusted held-writer,
    but core registers llamolotl/speak/sketch on their behalf with NO knowledge of
    their real held — the form's held defaults to 0. On every pod restart that
    would reset held to 0 while ``last_reported_at`` is refreshed (so the row is
    NOT stale), making core briefly believe the shared 4090 is emptier than it is
    and over-grant -> OOM. Preserving the last-known held is the conservative
    direction (worst case a brief under-grant the R6 poller corrects within a
    cycle). Defensive: any read error yields 0, a fresh consumer's natural default.
    """
    try:
        from selfai_ui.models.vram_leases import VramLeases

        existing = VramLeases.get(consumer_id)
        if existing is not None and existing.held_bytes:
            return int(existing.held_bytes)
    except Exception:
        pass
    return 0


def _register_llamolotl_vram_consumer() -> None:
    """Config-driven registration of self.llamolotl in the VRAM lease registry
    at startup (cavekit-gpu-lease-broker R4/AC1).

    This phase has a single known VRAM consumer, so registration is config- not
    self-service-driven: read llamolotl's addressable capacity + reclamation
    priority from env and upsert it under the ``self.llamolotl`` identity (the
    same string ``_LLAMOLOTL_AUDIENCE`` uses for the outbound ticket audience).
    ``register`` is an idempotent upsert keyed on ``consumer_id``, so this is
    safe to run on every boot — a re-register just refreshes the row.

    If ``LLAMOLOTL_VRAM_CAPACITY_BYTES`` is unset/blank/malformed the consumer is
    simply not registered and we log that it's unconfigured — self.ai must boot
    fine on deployments with no llamolotl / no lease broker wired, so this never
    raises out of the lifespan.

    Import is local so the lease-broker models are only pulled in at startup,
    matching the other lifespan wrappers."""
    from selfai_ui.env import (
        LLAMOLOTL_K8S_NAMESPACE,
        LLAMOLOTL_K8S_POD_SELECTOR,
        LLAMOLOTL_VRAM_CAPACITY_BYTES,
        LLAMOLOTL_VRAM_LEASE_PRIORITY,
    )

    raw = (LLAMOLOTL_VRAM_CAPACITY_BYTES or "").strip()
    if not raw:
        log.info(
            "vram-lease: LLAMOLOTL_VRAM_CAPACITY_BYTES unset — skipping "
            "self.llamolotl registration (lease broker unconfigured)"
        )
        return

    try:
        capacity_bytes = int(raw)
    except ValueError:
        log.warning(
            "vram-lease: LLAMOLOTL_VRAM_CAPACITY_BYTES=%r is not an integer — "
            "skipping self.llamolotl registration",
            raw,
        )
        return

    # R5 force-reap pod identity (opt-in): blank env -> None, leaving
    # self.llamolotl registered but force-reap ineligible (cavekit R5/AC7).
    k8s_namespace = (LLAMOLOTL_K8S_NAMESPACE or "").strip() or None
    k8s_pod_selector = (LLAMOLOTL_K8S_POD_SELECTOR or "").strip() or None
    pod_identity_configured = k8s_namespace is not None and k8s_pod_selector is not None

    try:
        from selfai_ui.models.vram_leases import (
            VramConsumerRegisterForm,
            VramLeases,
        )

        VramLeases.register(
            VramConsumerRegisterForm(
                consumer_id=_LLAMOLOTL_AUDIENCE,
                total_capacity_bytes=capacity_bytes,
                priority=LLAMOLOTL_VRAM_LEASE_PRIORITY,
                held_bytes=_existing_vram_held(_LLAMOLOTL_AUDIENCE),
                k8s_namespace=k8s_namespace,
                k8s_pod_selector=k8s_pod_selector,
            )
        )
        log.info(
            "vram-lease: registered %s (capacity %d bytes, priority %d, "
            "pod identity %s)",
            _LLAMOLOTL_AUDIENCE,
            capacity_bytes,
            LLAMOLOTL_VRAM_LEASE_PRIORITY,
            # NOT "force-reap armed": this function only writes a registry
            # row. Whether force-reap can actually fire depends on the broker
            # having a reaper AND a trusted target installed, which
            # _install_pod_reaper() reports separately. Claiming "armed" here is
            # what made self.ai#75 (set_reaper had zero callers) invisible for
            # weeks while the log said otherwise.
            "configured"
            if pod_identity_configured
            else "not configured (never force-reap eligible)",
        )
    except Exception as e:
        # Registration is best-effort at boot: a registry/DB hiccup must not
        # stop the app coming up (mirrors the fire-and-forget lifespan tasks).
        log.warning("vram-lease: self.llamolotl registration failed: %r", e)


def _install_llamolotl_release_transport(app_state) -> None:
    """Install the concrete self.llamolotl release transport on the VRAM broker
    at startup (cavekit-gpu-lease-broker R4, T-016).

    This is what makes an R2 release-request against ``self.llamolotl`` actually
    call the sibling pod's ``/api/system/vram-release`` endpoint (authenticated
    with a ``system:write`` service ticket) instead of the broker raising its
    transport-less RuntimeError. It is installed UNCONDITIONALLY: the transport
    carries its own config guard (an unconfigured control URL resolves to a clean
    ``timeout``, never a silent success), so it degrades gracefully on
    deployments with no llamolotl control port wired.

    NOTE: this does NOT remove ``_ensure_llamolotl_model_ready`` from
    ``gpu_queue.py`` — the dispatch-side cutover onto the broker is separate
    follow-up work gated on live validation (T-018/T-019), not done here.

    Import is local so the transport (and httpx) is only pulled in at startup,
    matching the other lifespan wrappers. Best-effort: a wiring hiccup must not
    stop the app coming up."""
    try:
        from selfai_ui.utils.vram_llamolotl import install_llamolotl_release_transport

        install_llamolotl_release_transport(app_state)
    except Exception as e:
        log.warning("vram-lease: installing self.llamolotl release transport failed: %r", e)


async def _run_vram_poller(app_state) -> None:
    """Start the R6 consumer VRAM state poller (cavekit-gpu-lease-broker R6,
    T-010). Mirrors ``_run_gpu_queue``'s wrapper shape: local imports, build the
    ``{consumer_id: state_source}`` map, then hand off to the resilient interval
    loop.

    This is what closes the gap live R1-R4 validation found: ``held`` only ever
    moved through the lease protocol itself (register / heartbeat / confirmed-
    release / grant), so real VRAM a consumer loaded outside any lease
    negotiation (e.g. a plain chat request) was invisible to the registry. The
    poller periodically reads each configured consumer's self-reported state and
    relays HELD via the EXISTING ``heartbeat()`` entry point (R6-AC2).

    For this single-known-consumer phase the map has one candidate entry —
    ``self.llamolotl``, using :class:`LlamolotlVramStateSource` — added only when
    ``LLAMOLOTL_CONTROL_BASE_URLS`` is configured. When it is not, the map is
    EMPTY: the loop still runs but polls nobody (the graceful unconfigured case,
    the same posture as the release transport's config guard) so self.ai boots
    fine on deployments with no llamolotl control port wired.

    Best-effort, like the neighboring wrappers: a wiring hiccup here must never
    stop the app coming up (the outer ``try`` keeps a bad state-source
    construction from taking down the lifespan; the loop itself is separately
    resilient per R6-AC1)."""
    try:
        from selfai_ui.utils.vram_llamolotl import LlamolotlVramStateSource
        from selfai_ui.utils.vram_poller import process_vram_poll_loop

        state_sources = {}
        # Resolve the control-port config the SAME way the state source /
        # release transport do (app_state.config.LLAMOLOTL_CONTROL_BASE_URLS);
        # configured -> poll self.llamolotl, unconfigured -> empty map (poll
        # nobody). Absence of a state source for a registered consumer is a
        # clean skip (R6-AC3), so an empty map is a valid, no-op steady state.
        cfg = getattr(app_state, "config", None)
        control_urls = list(getattr(cfg, "LLAMOLOTL_CONTROL_BASE_URLS", None) or []) if cfg else []
        if control_urls:
            state_sources[_LLAMOLOTL_AUDIENCE] = LlamolotlVramStateSource(app_state=app_state)
            log.info(
                "vram-poll: state poller armed for %s (control port configured)",
                _LLAMOLOTL_AUDIENCE,
            )
        else:
            log.info(
                "vram-poll: LLAMOLOTL_CONTROL_BASE_URLS unset — poller running "
                "but polling no consumers (no configured state source)"
            )

        # self.speak + self.sketch are ALSO registered VRAM consumers (Color 2b),
        # but each SERVES a pull-based GET /api/system/vram-state the poller must
        # read — nothing pushes their held into the registry. Without a state source
        # here their held stays at its registration value and ages to stale, so core
        # sees an INVISIBLE holder and over-grants the shared 4090 (the exact hazard
        # the self.speak GPU deploy was gated on). Each uses a single-string control
        # base (config.TTS_CONTROL_BASE_URL / config.SKETCH_CONTROL_BASE_URL), armed
        # only when that base is configured — same guard posture as llamolotl above.
        from selfai_ui.utils.vram_state_source import ControlBaseVramStateSource

        for _audience, _attr in (
            (_SPEAK_AUDIENCE, "TTS_CONTROL_BASE_URL"),
            (_SKETCH_AUDIENCE, "SKETCH_CONTROL_BASE_URL"),
            # self.curator joins them (self.ai#88): it serves the same pull-based
            # GET /api/system/vram-state, and its held is the figure that decides
            # whether core thinks a curation run's VRAM is free to hand out.
            (_CURATOR_AUDIENCE, "CURATOR_CONTROL_BASE_URL"),
        ):
            _base = getattr(cfg, _attr, None) if cfg else None
            if _base and str(_base).strip():
                state_sources[_audience] = ControlBaseVramStateSource(
                    app_state=app_state, config_attr=_attr, audience=_audience
                )
                log.info(
                    "vram-poll: state poller armed for %s (control base configured)",
                    _audience,
                )
            else:
                log.info("vram-poll: %s control base unset — not polling it", _audience)

        await process_vram_poll_loop(state_sources)
    except Exception as e:
        # Never let a poller-wiring failure escape the lifespan (mirrors the
        # other best-effort lease-broker startup wrappers).
        log.warning("vram-poll: state poller wiring failed: %r", e)


def _register_speak_vram_consumer() -> None:
    """Config-driven registration of self.speak in the VRAM lease registry at
    startup (cavekit-vram-speak-consumer R1) — a same-shape analogue of
    ``_register_llamolotl_vram_consumer()``.

    self.speak is the broker's SECOND config-driven consumer: read its
    advertised capacity + reclamation priority from env and upsert it under the
    ``self.speak`` identity (the same string ``routers/audio.py`` mints
    self.speak service tickets with). ``register`` is an idempotent upsert keyed
    on ``consumer_id``, so this is safe to run on every boot — a re-register just
    refreshes the row.

    If ``SPEAK_VRAM_CAPACITY_BYTES`` is unset/blank/malformed the consumer is
    simply not registered and we log that it's unconfigured — self.ai must boot
    fine on deployments with no self.speak VRAM env wired, so this never raises
    out of the lifespan.

    Import is local so the lease-broker models are only pulled in at startup,
    matching the other lifespan wrappers."""
    from selfai_ui.env import (
        SPEAK_VRAM_CAPACITY_BYTES,
        SPEAK_VRAM_LEASE_PRIORITY,
    )

    raw = (SPEAK_VRAM_CAPACITY_BYTES or "").strip()
    if not raw:
        log.info(
            "vram-lease: SPEAK_VRAM_CAPACITY_BYTES unset — skipping self.speak "
            "registration (lease broker unconfigured for speak)"
        )
        return

    try:
        capacity_bytes = int(raw)
    except ValueError:
        log.warning(
            "vram-lease: SPEAK_VRAM_CAPACITY_BYTES=%r is not an integer — "
            "skipping self.speak registration",
            raw,
        )
        return

    try:
        from selfai_ui.models.vram_leases import (
            VramConsumerRegisterForm,
            VramLeases,
        )

        VramLeases.register(
            VramConsumerRegisterForm(
                consumer_id=_SPEAK_AUDIENCE,
                total_capacity_bytes=capacity_bytes,
                priority=SPEAK_VRAM_LEASE_PRIORITY,
                held_bytes=_existing_vram_held(_SPEAK_AUDIENCE),
            )
        )
        log.info(
            "vram-lease: registered %s (capacity %d bytes, priority %d)",
            _SPEAK_AUDIENCE,
            capacity_bytes,
            SPEAK_VRAM_LEASE_PRIORITY,
        )
    except Exception as e:
        # Registration is best-effort at boot: a registry/DB hiccup must not
        # stop the app coming up (mirrors the fire-and-forget lifespan tasks).
        log.warning("vram-lease: self.speak registration failed: %r", e)


def _register_sketch_vram_consumer() -> None:
    """Config-driven registration of self.sketch in the VRAM lease registry at
    startup (Color epic Phase 2b) — a same-shape analogue of
    ``_register_speak_vram_consumer()``.

    self.sketch (ComfyUI image generation on the shared 4090) is the broker's
    THIRD config-driven consumer: read its advertised capacity + reclamation
    priority from env and upsert it under the ``self.sketch`` identity (the same
    string the ComfyUI shim validates inbound tickets against). ``register`` is an
    idempotent upsert keyed on ``consumer_id``, so this is safe to run on every
    boot.

    If ``SKETCH_VRAM_CAPACITY_BYTES`` is unset/blank/malformed the consumer is
    simply not registered and we log that it's unconfigured — self.ai must boot
    fine on deployments with no self.sketch VRAM env wired, so this never raises
    out of the lifespan."""
    from selfai_ui.env import (
        SKETCH_VRAM_CAPACITY_BYTES,
        SKETCH_VRAM_LEASE_PRIORITY,
    )

    raw = (SKETCH_VRAM_CAPACITY_BYTES or "").strip()
    if not raw:
        log.info(
            "vram-lease: SKETCH_VRAM_CAPACITY_BYTES unset — skipping self.sketch "
            "registration (lease broker unconfigured for sketch)"
        )
        return

    try:
        capacity_bytes = int(raw)
    except ValueError:
        log.warning(
            "vram-lease: SKETCH_VRAM_CAPACITY_BYTES=%r is not an integer — "
            "skipping self.sketch registration",
            raw,
        )
        return

    try:
        from selfai_ui.models.vram_leases import (
            VramConsumerRegisterForm,
            VramLeases,
        )

        VramLeases.register(
            VramConsumerRegisterForm(
                consumer_id=_SKETCH_AUDIENCE,
                total_capacity_bytes=capacity_bytes,
                priority=SKETCH_VRAM_LEASE_PRIORITY,
                held_bytes=_existing_vram_held(_SKETCH_AUDIENCE),
            )
        )
        log.info(
            "vram-lease: registered %s (capacity %d bytes, priority %d)",
            _SKETCH_AUDIENCE,
            capacity_bytes,
            SKETCH_VRAM_LEASE_PRIORITY,
        )
    except Exception as e:
        # Registration is best-effort at boot: a registry/DB hiccup must not
        # stop the app coming up (mirrors the fire-and-forget lifespan tasks).
        log.warning("vram-lease: self.sketch registration failed: %r", e)


def _register_curator_vram_consumer() -> None:
    """Config-driven registration of self.curator in the VRAM lease registry at
    startup (self.ai#88) — a same-shape analogue of the three hooks above.

    self.curator was the last GPU pod on the shared 4090 the broker could not
    see. Unregistered, its curation runs were an INVISIBLE hold: ``free_capacity()``
    counted the VRAM a NeMo Curator pipeline had resident as free and core would
    grant it to llamolotl/speak/sketch, which is how you OOM a card that the
    ledger says has room.

    ``CURATOR_VRAM_LEASE_PRIORITY`` is 8 in the manifest — below llamolotl's 10
    so curation never pre-empts the inference brain, above self.speak (5) and
    self.sketch (3) so a curator window can actually clear voices and image
    generation off the card rather than merely being scheduled alongside them.

    If ``CURATOR_VRAM_CAPACITY_BYTES`` is unset/blank/malformed the consumer is
    simply not registered and we log that it's unconfigured — self.ai must boot
    fine on deployments with no self.curator, so this never raises out of the
    lifespan."""
    from selfai_ui.env import (
        CURATOR_VRAM_CAPACITY_BYTES,
        CURATOR_VRAM_LEASE_PRIORITY,
    )

    raw = (CURATOR_VRAM_CAPACITY_BYTES or "").strip()
    if not raw:
        log.info(
            "vram-lease: CURATOR_VRAM_CAPACITY_BYTES unset — skipping self.curator "
            "registration (lease broker unconfigured for curator)"
        )
        return

    try:
        capacity_bytes = int(raw)
    except ValueError:
        log.warning(
            "vram-lease: CURATOR_VRAM_CAPACITY_BYTES=%r is not an integer — "
            "skipping self.curator registration",
            raw,
        )
        return

    try:
        from selfai_ui.models.vram_leases import (
            VramConsumerRegisterForm,
            VramLeases,
        )

        VramLeases.register(
            VramConsumerRegisterForm(
                consumer_id=_CURATOR_AUDIENCE,
                total_capacity_bytes=capacity_bytes,
                priority=CURATOR_VRAM_LEASE_PRIORITY,
                held_bytes=_existing_vram_held(_CURATOR_AUDIENCE),
            )
        )
        log.info(
            "vram-lease: registered %s (capacity %d bytes, priority %d)",
            _CURATOR_AUDIENCE,
            capacity_bytes,
            CURATOR_VRAM_LEASE_PRIORITY,
        )
    except Exception as e:
        log.warning("vram-lease: self.curator registration failed: %r", e)


def _register_publish_vram_consumer() -> None:
    """Config-driven registration of self.publish in the VRAM lease registry at
    startup (self.ai#136) — the fifth hook, and the one whose consumer is not a
    pod.

    A publish merges adapters into a base through self.llamolotl's pipeline,
    which loads the base at fp16 with ``device_map="auto"``
    (``api/merge_lora.py:51-52``). That hold lands in a pipeline SUBPROCESS
    inside the llamolotl pod — not in llama-server, and not in anything the
    ``self.llamolotl`` row accounts for. Unregistered, a running publish is an
    invisible hold of most of the card: ``free_capacity()`` counts it as free
    and core grants it away, which is how you OOM a card the ledger says has
    room. That is the same failure self.curator's registration closed.

    It gets its OWN consumer id rather than borrowing llamolotl's because the
    acquire is exclusive: registering as ``self.llamolotl`` would mark the
    serving process the exclusive holder while the serving process is exactly
    what has to release its VRAM first.

    ``PUBLISH_VRAM_LEASE_PRIORITY`` is 8 in the manifest — the same tier as
    self.curator, since a publish is the same kind of act: a scheduled,
    window-gated job that must be able to clear self.speak (5) and self.sketch
    (3) off the card without casually pre-empting the inference brain at
    llamolotl's 10.

    If ``PUBLISH_VRAM_CAPACITY_BYTES`` is unset/blank/malformed the consumer is
    simply not registered and we log that it is unconfigured. That is the state
    every deployment was in before this hook existed, and
    ``gpu_queue._publish_is_lease_consumer()`` reports False for it, so
    publishes dispatch without a lease exactly as they did — this never raises
    out of the lifespan."""
    from selfai_ui.env import (
        PUBLISH_VRAM_CAPACITY_BYTES,
        PUBLISH_VRAM_LEASE_PRIORITY,
    )

    raw = (PUBLISH_VRAM_CAPACITY_BYTES or "").strip()
    if not raw:
        log.info("vram-lease: PUBLISH_VRAM_CAPACITY_BYTES unset — self.publish not registered")
        return

    try:
        capacity_bytes = int(raw)
    except ValueError:
        log.warning(
            "vram-lease: PUBLISH_VRAM_CAPACITY_BYTES=%r is not an integer — "
            "skipping self.publish registration",
            raw,
        )
        return

    try:
        from selfai_ui.models.vram_leases import (
            VramConsumerRegisterForm,
            VramLeases,
        )

        VramLeases.register(
            VramConsumerRegisterForm(
                consumer_id=_PUBLISH_AUDIENCE,
                total_capacity_bytes=capacity_bytes,
                priority=PUBLISH_VRAM_LEASE_PRIORITY,
                held_bytes=_existing_vram_held(_PUBLISH_AUDIENCE),
            )
        )
        log.info(
            "vram-lease: registered %s (capacity %d bytes, priority %d)",
            _PUBLISH_AUDIENCE,
            capacity_bytes,
            PUBLISH_VRAM_LEASE_PRIORITY,
        )
    except Exception as e:
        log.warning("vram-lease: self.publish registration failed: %r", e)


def _install_pod_reaper() -> None:
    """Install the R5 force-reap capability on the VRAM broker (self.ai#75).

    This is the wiring whose ABSENCE was self.ai#75: ``set_reaper()`` had zero
    callers anywhere in the repo, so ``VramBroker._reaper`` was permanently None
    and the whole force-reap escalation in ``_decide_lease`` was unreachable —
    while the deployment carried a pods:delete ServiceAccount for it and the boot
    log claimed it was armed. Nothing could fire.

    Two things are installed together, deliberately:

    * the concrete ``KubernetesPodReaper`` (in-cluster ServiceAccount creds), and
    * the TRUSTED ``{consumer_id: (namespace, selector)}`` target map, built here
      from core's own environment.

    The target map is the security half (#79). ``_decide_lease`` used to aim the
    reaper using the consumer's REGISTRY ROW, and ``/vram-leases/register`` is
    gated on a shared-secret service ticket with no caller identity — so any mesh
    service could have re-registered a consumer with a selector pointing at
    another pod in the namespace and had a grant delete it. Sourcing the target
    from env removes the redirect; a consumer with no env-configured identity is
    simply never reap-eligible, the same opt-in as before.

    Best-effort, like the neighbouring wrappers: a wiring failure must never stop
    the app coming up. With nothing configured we log it and leave the broker
    exactly as it was — reaper-less, escalation skipped, R5 inert.
    """
    from selfai_ui.env import (
        CURATOR_K8S_NAMESPACE,
        CURATOR_K8S_POD_SELECTOR,
        LLAMOLOTL_K8S_NAMESPACE,
        LLAMOLOTL_K8S_POD_SELECTOR,
    )

    targets = {}
    for _audience, _ns, _sel in (
        (_LLAMOLOTL_AUDIENCE, LLAMOLOTL_K8S_NAMESPACE, LLAMOLOTL_K8S_POD_SELECTOR),
        # self.curator (self.ai#88). Same opt-in contract: both blank leaves it
        # registered but never reap-eligible. Worth configuring — a curation pod
        # whose pipeline has wedged is exactly the stale-holder case R5 exists
        # for, and it is the one consumer whose work is restartable, so losing
        # the pod costs a requeue rather than a live conversation.
        (_CURATOR_AUDIENCE, CURATOR_K8S_NAMESPACE, CURATOR_K8S_POD_SELECTOR),
    ):
        namespace = (_ns or "").strip()
        selector = (_sel or "").strip()
        if namespace and selector:
            targets[_audience] = (namespace, selector)

    if not targets:
        log.info(
            "vram-reap: no consumer has an env-configured pod identity — NOT "
            "installing the force-reap capability (R5 stays inert)"
        )
        return

    try:
        from selfai_ui.utils.vram_broker import VramBroker
        from selfai_ui.utils.vram_k8s import KubernetesPodReaper

        VramBroker.set_reap_targets(targets)
        VramBroker.set_reaper(KubernetesPodReaper())
        log.info(
            "vram-reap: force-reap capability ARMED on VramBroker for %s "
            "(k8s pod-delete via core's ServiceAccount)",
            sorted(targets),
        )
    except Exception as e:
        log.warning("vram-reap: installing the force-reap capability failed: %r", e)


def _install_speak_release_transport(app_state) -> None:
    """Install a consumer-aware release-transport dispatcher on the VRAM broker
    that keeps BOTH self.llamolotl and self.speak reachable through the broker's
    single global transport (cavekit-vram-speak-consumer R2, T-003 — the one
    production-critical piece).

    This REPLACES the separate ``_install_llamolotl_release_transport`` call:
    ``VramBrokerImpl`` holds ONE global transport (``set_transport``) and does
    NOT route by ``consumer_id``, so installing the speak transport alone would
    CLOBBER the deployed llamolotl transport — every release, including
    llamolotl's own, would then POST to self.speak's endpoint, breaking live
    VRAM arbitration in production. Instead we build both concrete transports and
    wrap them in a :class:`ConsumerAwareReleaseTransport` that dispatches by
    ``consumer_id`` (``self.speak`` → speak; else → llamolotl) — preserving
    llamolotl's deployed URL/audience/scope exactly while adding self.speak.

    Each concrete transport carries its own config guard (an unconfigured
    control URL resolves to a clean ``timeout``, never a silent success), so this
    degrades gracefully on deployments with either control port unwired. Import
    is local so the transports (and httpx) are only pulled in at startup.
    Best-effort: a wiring hiccup must not stop the app coming up."""
    try:
        from selfai_ui.utils.vram_broker import VramBroker
        from selfai_ui.utils.vram_curator import CuratorReleaseTransport
        from selfai_ui.utils.vram_llamolotl import LlamolotlReleaseTransport
        from selfai_ui.utils.vram_sketch import SketchReleaseTransport
        from selfai_ui.utils.vram_speak import (
            ConsumerAwareReleaseTransport,
            SpeakReleaseTransport,
        )

        dispatcher = ConsumerAwareReleaseTransport(
            speak_transport=SpeakReleaseTransport(app_state=app_state),
            llamolotl_transport=LlamolotlReleaseTransport(app_state=app_state),
            sketch_transport=SketchReleaseTransport(app_state=app_state),
            # Wired through the map rather than a fourth positional branch
            # (self.ai#88): the dispatcher's else-fallthrough routes anything
            # unrecognised to self.llamolotl, so a consumer added without its own
            # branch silently POSTs its release to llamolotl's endpoint. This is
            # also what puts self.curator inside the system-wide e-stop, which
            # fans out over exactly these transports with force=True.
            extra_transports={
                _CURATOR_AUDIENCE: CuratorReleaseTransport(app_state=app_state),
            },
        )
        VramBroker.set_transport(dispatcher)
        log.info(
            "vram-lease: installed consumer-aware release transport "
            "(self.llamolotl + self.speak + self.sketch + self.curator) on VramBroker"
        )
    except Exception as e:
        log.warning("vram-lease: installing consumer-aware release transport failed: %r", e)


async def _run_model_integrity_sweep(app_state) -> None:
    """Periodic /models integrity sweep -- self.ai/self.ai#38. Checks
    models llama-server reports as known against what self.llamolotl's
    control server reports as actually present on disk, logging a
    warning for anything missing or undersized (truncated/corrupt)."""
    from selfai_ui.utils.model_integrity import run_periodic_sweep

    await run_periodic_sweep(app_state)


# Same audience string as routers/llamolotl.py, routers/training.py, and
# utils/gpu_queue.py — must match self.llamolotl's SERVICE_AUTH_AUDIENCE
# (self.llamolotl#12).
_LLAMOLOTL_AUDIENCE = "self.llamolotl"

# The self.speak service identity — the same string routers/audio.py mints
# self.speak service tickets with, and the registry consumer_id for the
# self.speak VRAM lease (cavekit-vram-speak-consumer).
_SPEAK_AUDIENCE = "self.speak"

# The self.sketch (ComfyUI) service identity — the registry consumer_id for the
# self.sketch VRAM lease and the audience core mints release tickets against
# (Color epic Phase 2b). Must match SketchReleaseTransport.SKETCH_AUDIENCE and
# the ComfyUI shim's SERVICE_AUTH_AUDIENCE.
_SKETCH_AUDIENCE = "self.sketch"

# The self.curator service identity — the registry consumer_id for the
# self.curator VRAM lease and the audience core mints release tickets against
# (self.ai#88). Must match vram_curator.CURATOR_AUDIENCE, routers/curator.py's
# and gpu_queue.py's CURATOR_AUDIENCE, and self.curator's own
# SERVICE_AUTH_AUDIENCE.
_CURATOR_AUDIENCE = "self.curator"

# The publish consumer id (self.ai#136). Unlike the four above it names an
# ACTIVITY, not a service: the VRAM is held by a pipeline subprocess inside
# self.llamolotl's pod, and no process ever authenticates as this. Core is the
# only thing that registers, acquires and releases it, so there is no
# SERVICE_AUTH_AUDIENCE to match — only gpu_queue.py's PUBLISH_AUDIENCE.
_PUBLISH_AUDIENCE = "self.publish"


async def _ensure_curator_classifier_models(app_state) -> None:
    """Once curator and llamolotl are both healthy, trigger classifier model
    pre-fetching via llamolotl so the curator container can stay airgapped."""
    POLL_INTERVAL = 15  # seconds between health checks
    MAX_WAIT = 600  # give up after 10 minutes

    elapsed = 0
    curator_ok = False
    llamolotl_ok = False

    while elapsed < MAX_WAIT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        cfg = app_state.config

        if not (
            cfg.ENABLE_CURATOR_API
            and cfg.CURATOR_BASE_URLS
            and cfg.ENABLE_LLAMOLOTL_API
            and cfg.LLAMOLOTL_CONTROL_BASE_URLS
        ):
            return  # one or both services not configured — nothing to do

        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{cfg.CURATOR_BASE_URLS[0]}/health") as r:
                    curator_ok = r.status == 200
        except Exception:
            curator_ok = False

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{cfg.LLAMOLOTL_CONTROL_BASE_URLS[0]}/health") as r:
                    llamolotl_ok = r.status == 200
        except Exception:
            llamolotl_ok = False

        if curator_ok and llamolotl_ok:
            break

    if not (curator_ok and llamolotl_ok):
        log.warning(
            "classifier model pre-fetch skipped: "
            f"curator={'ok' if curator_ok else 'unreachable'}, "
            f"llamolotl={'ok' if llamolotl_ok else 'unreachable'}"
        )
        return

    log.info("curator + llamolotl healthy — ensuring classifier models are cached")
    llamolotl_url = app_state.config.LLAMOLOTL_CONTROL_BASE_URLS[0].rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{llamolotl_url}/api/models/hf-cache/ensure",
                json={},
                timeout=aiohttp.ClientTimeout(total=None),
                headers={TICKET_HEADER: mint_service_ticket(_LLAMOLOTL_AUDIENCE, "models:pull")},
            ) as r:
                async for line in r.content:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        repo = event.get("repo_id", "")
                        status = event.get("status", "")
                        if status == "error":
                            log.warning(f"classifier model fetch failed: {repo} — {event.get('error')}")
                        elif status in ("done", "cached"):
                            log.info(f"classifier model {status}: {repo}")
                    except Exception:
                        pass
    except Exception as e:
        log.warning(f"classifier model pre-fetch request failed: {e}")


app = FastAPI(
    docs_url="/docs" if ENV == "dev" else None,
    openapi_url="/openapi.json" if ENV == "dev" else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.state.config = AppConfig()


########################################
#
# CURATOR
#
########################################


app.state.config.ENABLE_CURATOR_API = ENABLE_CURATOR_API
app.state.config.CURATOR_BASE_URLS = CURATOR_BASE_URLS
app.state.config.CURATOR_CONTROL_BASE_URL = CURATOR_CONTROL_BASE_URL
app.state.config.CURATOR_API_CONFIGS = CURATOR_API_CONFIGS

########################################
#
# SELF.CORPUS
#
########################################


app.state.config.ENABLE_SELF_CORPUS = ENABLE_SELF_CORPUS
app.state.config.SELF_CORPUS_LAKEFS_ENDPOINT = SELF_CORPUS_LAKEFS_ENDPOINT
app.state.config.SELF_CORPUS_LAKEFS_ACCESS_KEY_ID = SELF_CORPUS_LAKEFS_ACCESS_KEY_ID
app.state.config.SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY = SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY

########################################
#
# LANGUAGE-EVAL
#
########################################


app.state.config.ENABLE_LANGUAGE_EVAL_API = ENABLE_LANGUAGE_EVAL_API
app.state.config.LANGUAGE_EVAL_BASE_URLS = LANGUAGE_EVAL_BASE_URLS

########################################
#
# CODE-EVAL
#
########################################


app.state.config.ENABLE_CODE_EVAL_API = ENABLE_CODE_EVAL_API
app.state.config.CODE_EVAL_BASE_URLS = CODE_EVAL_BASE_URLS

########################################
#
# PISTON
#
########################################


app.state.config.ENABLE_PISTON_EXECUTION = ENABLE_PISTON_EXECUTION
app.state.config.PISTON_BASE_URL = PISTON_BASE_URL

########################################
#
# ICEBERG
#
########################################


app.state.config.ICEBERG_BASE_URL = ICEBERG_BASE_URL

########################################
#
# LLAMOLOTL
#
########################################


app.state.config.ENABLE_LLAMOLOTL_API = ENABLE_LLAMOLOTL_API
app.state.config.LLAMOLOTL_BASE_URLS = LLAMOLOTL_BASE_URLS
app.state.config.LLAMOLOTL_CONTROL_BASE_URLS = LLAMOLOTL_CONTROL_BASE_URLS
app.state.config.LLAMOLOTL_API_CONFIGS = LLAMOLOTL_API_CONFIGS

app.state.LLAMOLOTL_MODELS = {}
# Populated by utils/model_integrity.run_periodic_sweep() -- self.ai/self.ai#38.
app.state.MODEL_INTEGRITY_WARNINGS = {}

########################################
#
# OLLAMA
#
########################################


app.state.config.ENABLE_OLLAMA_API = ENABLE_OLLAMA_API
app.state.config.OLLAMA_BASE_URLS = OLLAMA_BASE_URLS
app.state.config.OLLAMA_API_CONFIGS = OLLAMA_API_CONFIGS

app.state.OLLAMA_MODELS = {}

########################################
#
# OPENAI
#
########################################

app.state.config.ENABLE_OPENAI_API = ENABLE_OPENAI_API
app.state.config.OPENAI_API_BASE_URLS = OPENAI_API_BASE_URLS
app.state.config.OPENAI_API_KEYS = OPENAI_API_KEYS
app.state.config.OPENAI_API_CONFIGS = OPENAI_API_CONFIGS

app.state.OPENAI_MODELS = {}

########################################
#
# ANTHROPIC
#
########################################

app.state.config.ENABLE_ANTHROPIC_API = ENABLE_ANTHROPIC_API
app.state.config.ANTHROPIC_BASE_URLS = ANTHROPIC_BASE_URLS
app.state.config.ANTHROPIC_API_CONFIGS = ANTHROPIC_API_CONFIGS

app.state.ANTHROPIC_MODELS = {}

########################################
#
# WEBUI
#
########################################

app.state.config.WEBUI_URL = WEBUI_URL
app.state.config.ENABLE_SIGNUP = ENABLE_SIGNUP
app.state.config.ENABLE_LOGIN_FORM = ENABLE_LOGIN_FORM

app.state.config.ENABLE_API_KEY = ENABLE_API_KEY
app.state.config.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS = ENABLE_API_KEY_ENDPOINT_RESTRICTIONS
app.state.config.API_KEY_ALLOWED_ENDPOINTS = API_KEY_ALLOWED_ENDPOINTS

app.state.config.JWT_EXPIRES_IN = JWT_EXPIRES_IN

app.state.config.SHOW_ADMIN_DETAILS = SHOW_ADMIN_DETAILS
app.state.config.ADMIN_EMAIL = ADMIN_EMAIL


app.state.config.DEFAULT_MODELS = DEFAULT_MODELS
app.state.config.DEFAULT_PROMPT_SUGGESTIONS = DEFAULT_PROMPT_SUGGESTIONS
app.state.config.DEFAULT_USER_ROLE = DEFAULT_USER_ROLE

app.state.config.USER_PERMISSIONS = USER_PERMISSIONS
app.state.config.WEBHOOK_URL = WEBHOOK_URL
app.state.config.BANNERS = WEBUI_BANNERS
app.state.config.MODEL_ORDER_LIST = MODEL_ORDER_LIST


app.state.config.ENABLE_CHANNELS = ENABLE_CHANNELS
app.state.config.ENABLE_COMMUNITY_SHARING = ENABLE_COMMUNITY_SHARING
app.state.config.ENABLE_MESSAGE_RATING = ENABLE_MESSAGE_RATING

app.state.config.ENABLE_EVALUATION_ARENA_MODELS = ENABLE_EVALUATION_ARENA_MODELS
app.state.config.EVALUATION_ARENA_MODELS = EVALUATION_ARENA_MODELS

app.state.config.OAUTH_USERNAME_CLAIM = OAUTH_USERNAME_CLAIM
app.state.config.OAUTH_PICTURE_CLAIM = OAUTH_PICTURE_CLAIM
app.state.config.OAUTH_EMAIL_CLAIM = OAUTH_EMAIL_CLAIM

app.state.config.ENABLE_OAUTH_ROLE_MANAGEMENT = ENABLE_OAUTH_ROLE_MANAGEMENT
app.state.config.OAUTH_ROLES_CLAIM = OAUTH_ROLES_CLAIM
app.state.config.OAUTH_ALLOWED_ROLES = OAUTH_ALLOWED_ROLES
app.state.config.OAUTH_ADMIN_ROLES = OAUTH_ADMIN_ROLES

app.state.config.ENABLE_LDAP = ENABLE_LDAP
app.state.config.LDAP_SERVER_LABEL = LDAP_SERVER_LABEL
app.state.config.LDAP_SERVER_HOST = LDAP_SERVER_HOST
app.state.config.LDAP_SERVER_PORT = LDAP_SERVER_PORT
app.state.config.LDAP_ATTRIBUTE_FOR_USERNAME = LDAP_ATTRIBUTE_FOR_USERNAME
app.state.config.LDAP_APP_DN = LDAP_APP_DN
app.state.config.LDAP_APP_PASSWORD = LDAP_APP_PASSWORD
app.state.config.LDAP_SEARCH_BASE = LDAP_SEARCH_BASE
app.state.config.LDAP_SEARCH_FILTERS = LDAP_SEARCH_FILTERS
app.state.config.LDAP_USE_TLS = LDAP_USE_TLS
app.state.config.LDAP_CA_CERT_FILE = LDAP_CA_CERT_FILE
app.state.config.LDAP_CIPHERS = LDAP_CIPHERS


app.state.AUTH_TRUSTED_EMAIL_HEADER = WEBUI_AUTH_TRUSTED_EMAIL_HEADER
app.state.AUTH_TRUSTED_NAME_HEADER = WEBUI_AUTH_TRUSTED_NAME_HEADER

app.state.TOOLS = {}
app.state.FUNCTIONS = {}


########################################
#
# RETRIEVAL
#
########################################


app.state.config.TOP_K = RAG_TOP_K
app.state.config.RELEVANCE_THRESHOLD = RAG_RELEVANCE_THRESHOLD
app.state.config.FILE_MAX_SIZE = RAG_FILE_MAX_SIZE
app.state.config.FILE_MAX_COUNT = RAG_FILE_MAX_COUNT
app.state.config.FILE_UPLOAD_MIME_ALLOWLIST = FILE_UPLOAD_MIME_ALLOWLIST

app.state.config.ENABLE_RAG_HYBRID_SEARCH = ENABLE_RAG_HYBRID_SEARCH
app.state.config.ENABLE_RAG_WEB_LOADER_SSL_VERIFICATION = ENABLE_RAG_WEB_LOADER_SSL_VERIFICATION

app.state.config.CONTENT_EXTRACTION_ENGINE = CONTENT_EXTRACTION_ENGINE
app.state.config.TIKA_SERVER_URL = TIKA_SERVER_URL

app.state.config.TEXT_SPLITTER = RAG_TEXT_SPLITTER
app.state.config.TIKTOKEN_ENCODING_NAME = TIKTOKEN_ENCODING_NAME

app.state.config.CHUNK_SIZE = CHUNK_SIZE
app.state.config.CHUNK_OVERLAP = CHUNK_OVERLAP

app.state.config.RAG_EMBEDDING_ENGINE = RAG_EMBEDDING_ENGINE
app.state.config.RAG_EMBEDDING_MODEL = RAG_EMBEDDING_MODEL
app.state.config.RAG_EMBEDDING_BATCH_SIZE = RAG_EMBEDDING_BATCH_SIZE
app.state.config.RAG_RERANKING_MODEL = RAG_RERANKING_MODEL
app.state.config.RAG_TEMPLATE = RAG_TEMPLATE

app.state.config.RAG_OPENAI_API_BASE_URL = RAG_OPENAI_API_BASE_URL
app.state.config.RAG_OPENAI_API_KEY = RAG_OPENAI_API_KEY

app.state.config.RAG_OLLAMA_BASE_URL = RAG_OLLAMA_BASE_URL
app.state.config.RAG_OLLAMA_API_KEY = RAG_OLLAMA_API_KEY

app.state.config.PDF_EXTRACT_IMAGES = PDF_EXTRACT_IMAGES

app.state.config.YOUTUBE_LOADER_LANGUAGE = YOUTUBE_LOADER_LANGUAGE
app.state.config.YOUTUBE_LOADER_PROXY_URL = YOUTUBE_LOADER_PROXY_URL


app.state.config.ENABLE_RAG_WEB_SEARCH = ENABLE_RAG_WEB_SEARCH
app.state.config.RAG_WEB_SEARCH_ENGINE = RAG_WEB_SEARCH_ENGINE
app.state.config.RAG_WEB_SEARCH_DOMAIN_FILTER_LIST = RAG_WEB_SEARCH_DOMAIN_FILTER_LIST
app.state.config.RAG_WEB_LOADER_ENGINE = RAG_WEB_LOADER_ENGINE

app.state.config.ENABLE_GOOGLE_DRIVE_INTEGRATION = ENABLE_GOOGLE_DRIVE_INTEGRATION
app.state.config.SEARXNG_QUERY_URL = SEARXNG_QUERY_URL
app.state.config.GOOGLE_PSE_API_KEY = GOOGLE_PSE_API_KEY
app.state.config.GOOGLE_PSE_ENGINE_ID = GOOGLE_PSE_ENGINE_ID
app.state.config.BRAVE_SEARCH_API_KEY = BRAVE_SEARCH_API_KEY
app.state.config.KAGI_SEARCH_API_KEY = KAGI_SEARCH_API_KEY
app.state.config.MOJEEK_SEARCH_API_KEY = MOJEEK_SEARCH_API_KEY
app.state.config.SERPSTACK_API_KEY = SERPSTACK_API_KEY
app.state.config.SERPSTACK_HTTPS = SERPSTACK_HTTPS
app.state.config.SERPER_API_KEY = SERPER_API_KEY
app.state.config.SERPLY_API_KEY = SERPLY_API_KEY
app.state.config.TAVILY_API_KEY = TAVILY_API_KEY
app.state.config.SEARCHAPI_API_KEY = SEARCHAPI_API_KEY
app.state.config.SEARCHAPI_ENGINE = SEARCHAPI_ENGINE
app.state.config.JINA_API_KEY = JINA_API_KEY
app.state.config.BING_SEARCH_V7_ENDPOINT = BING_SEARCH_V7_ENDPOINT
app.state.config.BING_SEARCH_V7_SUBSCRIPTION_KEY = BING_SEARCH_V7_SUBSCRIPTION_KEY
app.state.config.FIRECRAWL_API_BASE_URL = FIRECRAWL_API_BASE_URL
app.state.config.FIRECRAWL_API_KEY = FIRECRAWL_API_KEY
app.state.config.KB_CRAWL_RESPECT_ROBOTS_DELAY = KB_CRAWL_RESPECT_ROBOTS_DELAY
app.state.config.ENABLE_WEB_CRAWL = ENABLE_WEB_CRAWL
app.state.config.WEB_CRAWL_MAX_PAGES = WEB_CRAWL_MAX_PAGES
app.state.config.WEB_CRAWL_MAX_DEPTH = WEB_CRAWL_MAX_DEPTH
app.state.config.BROWSE_PLAYWRIGHT_SERVICE_URL = BROWSE_PLAYWRIGHT_SERVICE_URL
app.state.config.BROWSE_PLAYWRIGHT_API_KEY = BROWSE_PLAYWRIGHT_API_KEY
app.state.config.BROWSE_FETCH_MAX_CHARS = BROWSE_FETCH_MAX_CHARS
app.state.config.BROWSE_MAX_LINKS_PER_PAGE = BROWSE_MAX_LINKS_PER_PAGE
app.state.config.DEEP_RESEARCH_MAX_DEPTH = DEEP_RESEARCH_MAX_DEPTH
app.state.config.DEEP_RESEARCH_MAX_PAGES = DEEP_RESEARCH_MAX_PAGES
app.state.config.DEEP_RESEARCH_MAX_SECONDS = DEEP_RESEARCH_MAX_SECONDS
app.state.config.DEEP_RESEARCH_CONCURRENCY = DEEP_RESEARCH_CONCURRENCY
app.state.config.DEEP_RESEARCH_MAX_CHARS_PER_PAGE = DEEP_RESEARCH_MAX_CHARS_PER_PAGE
app.state.config.DEEP_RESEARCH_RESPECT_ROBOTS = DEEP_RESEARCH_RESPECT_ROBOTS
app.state.config.DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS = DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS
app.state.config.BROWSE_USER_AGENT = BROWSE_USER_AGENT
app.state.config.ENABLE_DEEP_RESEARCH = ENABLE_DEEP_RESEARCH

app.state.config.RAG_WEB_SEARCH_RESULT_COUNT = RAG_WEB_SEARCH_RESULT_COUNT
app.state.config.RAG_WEB_SEARCH_CONCURRENT_REQUESTS = RAG_WEB_SEARCH_CONCURRENT_REQUESTS

app.state.EMBEDDING_FUNCTION = None
app.state.ef = None
app.state.rf = None

app.state.YOUTUBE_LOADER_TRANSLATION = None


try:
    app.state.ef = get_ef(
        app.state.config.RAG_EMBEDDING_ENGINE,
        app.state.config.RAG_EMBEDDING_MODEL,
        RAG_EMBEDDING_MODEL_AUTO_UPDATE,
    )

    app.state.rf = get_rf(
        app.state.config.RAG_RERANKING_MODEL,
        RAG_RERANKING_MODEL_AUTO_UPDATE,
    )
except Exception as e:
    log.error(f"Error updating models: {e}")
    pass


app.state.EMBEDDING_FUNCTION = get_embedding_function(
    app.state.config.RAG_EMBEDDING_ENGINE,
    app.state.config.RAG_EMBEDDING_MODEL,
    app.state.ef,
    (
        app.state.config.RAG_OPENAI_API_BASE_URL
        if app.state.config.RAG_EMBEDDING_ENGINE == "openai"
        else app.state.config.RAG_OLLAMA_BASE_URL
    ),
    (
        app.state.config.RAG_OPENAI_API_KEY
        if app.state.config.RAG_EMBEDDING_ENGINE == "openai"
        else app.state.config.RAG_OLLAMA_API_KEY
    ),
    app.state.config.RAG_EMBEDDING_BATCH_SIZE,
)


########################################
#
# IMAGES
#
########################################

app.state.config.IMAGE_GENERATION_ENGINE = IMAGE_GENERATION_ENGINE
app.state.config.ENABLE_IMAGE_GENERATION = ENABLE_IMAGE_GENERATION

app.state.config.IMAGES_OPENAI_API_BASE_URL = IMAGES_OPENAI_API_BASE_URL
app.state.config.IMAGES_OPENAI_API_KEY = IMAGES_OPENAI_API_KEY

app.state.config.IMAGE_GENERATION_MODEL = IMAGE_GENERATION_MODEL

app.state.config.AUTOMATIC1111_BASE_URL = AUTOMATIC1111_BASE_URL
app.state.config.AUTOMATIC1111_API_AUTH = AUTOMATIC1111_API_AUTH
app.state.config.AUTOMATIC1111_CFG_SCALE = AUTOMATIC1111_CFG_SCALE
app.state.config.AUTOMATIC1111_SAMPLER = AUTOMATIC1111_SAMPLER
app.state.config.AUTOMATIC1111_SCHEDULER = AUTOMATIC1111_SCHEDULER
app.state.config.COMFYUI_BASE_URL = COMFYUI_BASE_URL
app.state.config.COMFYUI_API_KEY = COMFYUI_API_KEY
app.state.config.COMFYUI_WORKFLOW = COMFYUI_WORKFLOW
app.state.config.COMFYUI_WORKFLOW_NODES = COMFYUI_WORKFLOW_NODES
# self.sketch VRAM-lease control base (Color Phase 2b) — read by
# SketchReleaseTransport off app.state.config, mirroring TTS_CONTROL_BASE_URL.
app.state.config.SKETCH_CONTROL_BASE_URL = SKETCH_CONTROL_BASE_URL

app.state.config.IMAGE_SIZE = IMAGE_SIZE
app.state.config.IMAGE_STEPS = IMAGE_STEPS


########################################
#
# AUDIO
#
########################################

app.state.config.STT_OPENAI_API_BASE_URL = AUDIO_STT_OPENAI_API_BASE_URL
app.state.config.STT_OPENAI_API_KEY = AUDIO_STT_OPENAI_API_KEY
app.state.config.STT_ENGINE = AUDIO_STT_ENGINE
app.state.config.STT_MODEL = AUDIO_STT_MODEL
app.state.config.STT_CONTROL_BASE_URL = AUDIO_STT_CONTROL_BASE_URL
app.state.config.STT_ENABLED_MODELS = AUDIO_STT_ENABLED_MODELS

app.state.config.WHISPER_MODEL = WHISPER_MODEL

app.state.config.TTS_OPENAI_API_BASE_URL = AUDIO_TTS_OPENAI_API_BASE_URL
app.state.config.TTS_OPENAI_API_KEY = AUDIO_TTS_OPENAI_API_KEY
app.state.config.TTS_ENGINE = AUDIO_TTS_ENGINE
app.state.config.TTS_MODEL = AUDIO_TTS_MODEL
app.state.config.TTS_VOICE = AUDIO_TTS_VOICE
app.state.config.TTS_API_KEY = AUDIO_TTS_API_KEY
app.state.config.TTS_SPLIT_ON = AUDIO_TTS_SPLIT_ON
app.state.config.TTS_CONTROL_BASE_URL = AUDIO_TTS_CONTROL_BASE_URL
app.state.config.TTS_ENABLED_VOICES = AUDIO_TTS_ENABLED_VOICES


app.state.config.TTS_AZURE_SPEECH_REGION = AUDIO_TTS_AZURE_SPEECH_REGION
app.state.config.TTS_AZURE_SPEECH_OUTPUT_FORMAT = AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT


# --- Relocate legacy flat audio settings into typed connections (R5, T-006) ---
# One-time and idempotent: only when no typed connection exists yet, mint a
# SavedAudioConnection per previously-configured engine (STT and/or TTS) from the
# old flat *_ENGINE knobs, copying every address/credential verbatim so an
# already-configured backend keeps working with no re-entry or re-auth. Which
# connection actually serves a request is untouched here (that is T-007).
_existing_audio_connections = AUDIO_CONNECTION_CONFIGS.value or {}
if not _existing_audio_connections:
    _migrated_audio_store = migrate_legacy_audio_config(
        build_legacy_snapshot(_selfai_config),
        existing_configs=_existing_audio_connections,
    )
    if len(_migrated_audio_store):
        AUDIO_CONNECTION_CONFIGS.value = _migrated_audio_store.to_serializable()
        AUDIO_CONNECTION_CONFIGS.save()
app.state.config.AUDIO_CONNECTION_CONFIGS = AUDIO_CONNECTION_CONFIGS


app.state.faster_whisper_model = None
app.state.speech_synthesiser = None
app.state.speech_speaker_embeddings_dataset = None


########################################
#
# TASKS
#
########################################


app.state.config.TASK_MODEL = TASK_MODEL
app.state.config.TASK_MODEL_EXTERNAL = TASK_MODEL_EXTERNAL


app.state.config.ENABLE_SEARCH_QUERY_GENERATION = ENABLE_SEARCH_QUERY_GENERATION
app.state.config.ENABLE_RETRIEVAL_QUERY_GENERATION = ENABLE_RETRIEVAL_QUERY_GENERATION
app.state.config.ENABLE_AUTOCOMPLETE_GENERATION = ENABLE_AUTOCOMPLETE_GENERATION
app.state.config.ENABLE_TAGS_GENERATION = ENABLE_TAGS_GENERATION


app.state.config.TITLE_GENERATION_PROMPT_TEMPLATE = TITLE_GENERATION_PROMPT_TEMPLATE
app.state.config.TAGS_GENERATION_PROMPT_TEMPLATE = TAGS_GENERATION_PROMPT_TEMPLATE
app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE = TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE
app.state.config.QUERY_GENERATION_PROMPT_TEMPLATE = QUERY_GENERATION_PROMPT_TEMPLATE
app.state.config.AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE = AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE
app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH = AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH


########################################
#
# WEBUI
#
########################################

app.state.MODELS = {}


class RedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Check if the request is a GET request
        if request.method == "GET":
            path = request.url.path
            query_params = dict(parse_qs(urlparse(str(request.url)).query))

            # Check for the specific watch path and the presence of 'v' parameter
            if path.endswith("/watch") and "v" in query_params:
                video_id = query_params["v"][0]  # Extract the first 'v' parameter
                encoded_video_id = urlencode({"youtube": video_id})
                redirect_url = f"/?{encoded_video_id}"
                return RedirectResponse(url=redirect_url)

        # Proceed with the normal flow of other requests
        response = await call_next(request)
        return response


# Add the middleware to the app
app.add_middleware(RedirectMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.middleware("http")
async def check_url(request: Request, call_next):
    start_time = int(time.time())
    log.debug("Hello There")
    request.state.enable_api_key = app.state.config.ENABLE_API_KEY
    response = await call_next(request)
    process_time = int(time.time()) - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


@app.middleware("http")
async def inspect_websocket(request: Request, call_next):
    if "/ws/socket.io" in request.url.path and request.query_params.get("transport") == "websocket":
        upgrade = (request.headers.get("Upgrade") or "").lower()
        connection = (request.headers.get("Connection") or "").lower().split(",")
        # Check that there's the correct headers for an upgrade, else reject the connection
        # This is to work around this upstream issue: https://github.com/miguelgrinberg/python-engineio/issues/367
        if upgrade != "websocket" or "upgrade" not in connection:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Invalid WebSocket upgrade request"},
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGIN,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/ws", socket_app)


app.include_router(curator.router, prefix="/curator", tags=["curator"])
app.include_router(language_eval.router, prefix="/language-eval", tags=["language-eval"])
app.include_router(code_eval.router, prefix="/code-eval", tags=["code-eval"])
app.include_router(windows.router, prefix="/api/windows", tags=["windows"])
app.include_router(vram_leases.router, prefix="/api/vram-leases", tags=["vram-leases"])
app.include_router(benchmarks.router, prefix="/api/benchmarks", tags=["benchmarks"])
app.include_router(queue.router, prefix="/api", tags=["queue"])
app.include_router(backups.router, prefix="/api/backups", tags=["backups"])
app.include_router(llamolotl.router, prefix="/llamolotl", tags=["llamolotl"])
app.include_router(ollama.router, prefix="/ollama", tags=["ollama"])
app.include_router(openai.router, prefix="/openai", tags=["openai"])
app.include_router(anthropic.router, prefix="/anthropic", tags=["anthropic"])

# MCP front door (self.ai#25): <your-host>/mcp/{server} proxies
# Streamable-HTTP MCP traffic to backends behind self.ai auth. No prefix — the
# route names its full /mcp/{server} path. First backend is the dead-simple
# `echo` server; the real crew-system servers wire in behind the same paths.
# NOTE: if API_KEY_ENDPOINT_RESTRICTIONS is on, /mcp/* must be in
# API_KEY_ALLOWED_ENDPOINTS (the trailing /* is a prefix match -- the plain
# path "/mcp" never matches, since every real route is "/mcp/{server}") or
# sk- callers 403 while JWT/cookie callers work. See _path_is_allowed in
# utils/auth.py (selfai/gitlab-profile#30).
app.include_router(mcp_proxy.router, tags=["mcp"])


app.include_router(pipelines.router, prefix="/api/v1/pipelines", tags=["pipelines"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["tasks"])
app.include_router(tokenization.router, prefix="/api/v1/tokenization", tags=["tokenization"])
app.include_router(images.router, prefix="/api/v1/images", tags=["images"])
app.include_router(audio.router, prefix="/api/v1/audio", tags=["audio"])
app.include_router(
    audio_connections.router,
    prefix="/api/v1/audio/connections",
    tags=["audio-connections"],
)
app.include_router(transcribe.router, prefix="/api/v1/transcribe", tags=["transcribe"])
app.include_router(voice_catalog.router, prefix="/api/v1/voice-catalog", tags=["voice-catalog"])
app.include_router(retrieval.router, prefix="/api/v1/retrieval", tags=["retrieval"])

app.include_router(configs.router, prefix="/api/v1/configs", tags=["configs"])

# Connection/pool/schema readout for Admin > Settings > Database (#94). Read
# only -- the backup concerns that used to live on that page are under
# /api/backups now.
app.include_router(database.router, prefix="/api/v1/db", tags=["database"])

app.include_router(auths.router, prefix="/api/v1/auths", tags=["auths"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
# The mod registry: which mods are enabled and loaded, filtered by the caller's
# scopes. Reads app.state.MODS, which boot_mods publishes; before any mod is
# enabled the response is an empty list, not an error.
app.include_router(mods.router, prefix="/api/v1/mods", tags=["mods"])
# Per-mod static assets at `/static/mods/<id>/<path>` (frontend-api R3). Included
# HERE, before the `/static` StaticFiles mount below (main.py:1718), so
# `/static/mods/...` resolves to this traversal-safe handler; other `/static/...`
# paths fall through to the core mount unchanged. No prefix -- the route names its
# full `/static/mods/...` path itself.
app.include_router(mod_assets.router, tags=["mods"])

# The always-fresh per-mod frontend manifest (frontend-api R4 -- T-A05): resolves
# a mod id to its current content-hashed bundle URL with no-cache headers. The
# route names its full `/api/v1/mods/{id}/frontend-manifest` path itself.
app.include_router(mod_frontend_manifest.router, tags=["mods"])


app.include_router(channels.router, prefix="/api/v1/channels", tags=["channels"])
app.include_router(chats.router, prefix="/api/v1/chats", tags=["chats"])

app.include_router(models.router, prefix="/api/v1/models", tags=["models"])
app.include_router(
    model_versions.router, prefix="/api/v1/model-lines", tags=["model-lines"]
)
app.include_router(
    model_weights.router, prefix="/api/v1/model-weights", tags=["model-weights"]
)
app.include_router(knowledge.router, prefix="/api/v1/knowledge", tags=["knowledge"])
app.include_router(voices.router, prefix="/api/v1/voices", tags=["voices"])
app.include_router(training.router, prefix="/api/v1/training", tags=["training"])
app.include_router(prompts.router, prefix="/api/v1/prompts", tags=["prompts"])
app.include_router(tools.router, prefix="/api/v1/tools", tags=["tools"])

app.include_router(memories.router, prefix="/api/v1/memories", tags=["memories"])
app.include_router(folders.router, prefix="/api/v1/folders", tags=["folders"])
app.include_router(groups.router, prefix="/api/v1/groups", tags=["groups"])
app.include_router(files.router, prefix="/api/v1/files", tags=["files"])
app.include_router(functions.router, prefix="/api/v1/functions", tags=["functions"])
app.include_router(evaluations.router, prefix="/api/v1/evaluations", tags=["evaluations"])
app.include_router(custom_evals.router, prefix="/api/v1/evaluations/custom", tags=["evaluations"])
app.include_router(utils.router, prefix="/api/v1/utils", tags=["utils"])
app.include_router(system.router, prefix="/api/system", tags=["system"])


##################################
#
# Chat Endpoints
#
##################################


@app.get("/api/models")
async def get_models(request: Request, user=Depends(get_verified_user)):
    """Models this user may call, one entry per model.

    **Context budgeting (self.ai#87).** Entries carry two optional integer
    fields a caller can budget a context window against:

    - `context_length` — tokens one request may use, prompt plus generation.
      This is the window the model is *served* with, not the context its
      weights were trained to; for locally served models the two routinely
      differ by an order of magnitude. Present for `unloaded` models too, since
      that is the normal state and clients build their model table before
      anything is loaded.
    - `max_output_tokens` — a cap on generated tokens, when the backend sets one
      below the window itself.

    Either field is **omitted when it is not known** — for models behind an
    external API that publishes no such number, for instance. Omitted means
    "unknown, apply your own policy"; it never means unlimited. A field that is
    present is a value to rely on, not a hint.
    """

    def get_filtered_models(models, user):
        filtered_models = []
        for model in models:
            if model.get("arena"):
                if has_access(
                    user.id,
                    type="read",
                    access_control=model.get("info", {}).get("meta", {}).get("access_control", {}),
                ):
                    filtered_models.append(model)
                continue

            model_info = Models.get_model_by_id(model["id"])
            if model_info:
                if user.id == model_info.user_id or has_access(
                    user.id, type="read", access_control=model_info.access_control
                ):
                    filtered_models.append(model)

        return filtered_models

    models = await get_all_models(request)

    # Filter out filter pipelines
    models = [model for model in models if "pipeline" not in model or model["pipeline"].get("type", None) != "filter"]

    model_order_list = request.app.state.config.MODEL_ORDER_LIST
    if model_order_list:
        model_order_dict = {model_id: i for i, model_id in enumerate(model_order_list)}
        # Sort models by order list priority, with fallback for those not in the list
        models.sort(key=lambda x: (model_order_dict.get(x["id"], float("inf")), x["name"]))

    # Filter out models that the user does not have access to
    if user.role == "user" and not BYPASS_MODEL_ACCESS_CONTROL:
        models = get_filtered_models(models, user)

    log.debug(
        f"/api/models returned filtered models accessible to the user: {json.dumps([model['id'] for model in models])}"
    )
    return {"data": models}


@app.get("/api/models/base")
async def get_base_models(request: Request, user=Depends(get_admin_user)):
    models = await get_all_base_models(request)
    return {"data": models}


@app.get("/api/models/public")
async def get_public_models(request: Request):
    """Unauthenticated free-tier model listing (GitLab issue #6).

    Returns the subset of models that are public — i.e. carry no
    `access_control` restriction, the same "visible to any 'user' role"
    semantics already used by `/api/models` (see `has_access`). Arena
    models are excluded since they don't represent a single, nameable
    model. The response is deliberately minimal: no `info` (params/meta
    lineage such as `hf_repo`, connection details), no per-backend raw
    payload (`openai`/`ollama`/`llamolotl`), no user/ownership data —
    just enough to populate a free-tier model picker, plus the context
    fields `/api/models` publishes (`context_length`, `max_output_tokens`),
    which a free-tier client has to budget against just as much (self.ai#87)
    and which say nothing about lineage or connections.
    """
    models = await get_all_models(request)

    # Filter out filter pipelines, same as /api/models.
    models = [model for model in models if "pipeline" not in model or model["pipeline"].get("type", None) != "filter"]

    public_models = []
    for model in models:
        if model.get("arena"):
            continue

        model_info = Models.get_model_by_id(model["id"])
        access_control = model_info.access_control if model_info else None
        if access_control is not None:
            # Restricted to specific users/groups; not part of the free tier.
            continue

        public_models.append(
            {
                "id": model["id"],
                "name": model.get("name", model["id"]),
                "object": model.get("object", "model"),
                "created": model.get("created"),
                "owned_by": model.get("owned_by"),
                **inherited_context_fields(model),
            }
        )

    return {"data": public_models}


############################
# Eval Live Event Logger
############################

# Counter per eval job for live event indexing
_eval_event_counters: dict[str, int] = {}
# Cache for total sample count per eval job (looked up once from job meta)
_eval_total_cache: dict[str, int | None] = {}


def _get_eval_total(job_id: str) -> int | None:
    """Return the total sample count for an eval job, cached after first lookup."""
    if job_id in _eval_total_cache:
        return _eval_total_cache[job_id]
    try:
        from selfai_ui.models.eval_jobs import EvalJobs

        job = EvalJobs.get_job_by_id(id=job_id)
        if job and job.meta:
            total = job.meta.get("total_samples")
            if total is not None:
                _eval_total_cache[job_id] = int(total)
                return _eval_total_cache[job_id]
    except Exception:
        pass
    _eval_total_cache[job_id] = None
    return None


def _log_eval_event(
    job_id: str,
    eval_type: str | None,
    form_data: dict,
    response,
) -> None:
    """Log a prompt/response pair from an eval job for live streaming.

    Writes a JSONL event to DATA_DIR/eval-events/{job_id}.jsonl so the
    live streaming endpoint can pick it up.
    """
    from datetime import datetime

    events_dir = Path(DATA_DIR) / "eval-events"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_file = events_dir / f"{job_id}.jsonl"

    # Extract prompt from messages (chat) or prompt field (text completions)
    messages = form_data.get("messages", [])
    prompt = ""
    if messages:
        last_msg = messages[-1] if isinstance(messages, list) else messages
        if isinstance(last_msg, dict):
            prompt = last_msg.get("content", "")
        else:
            prompt = str(last_msg)
    elif "prompt" in form_data:
        prompt = form_data["prompt"]
        if isinstance(prompt, list):
            prompt = prompt[0] if prompt else ""

    # Extract response content — handle dict (non-streaming) and Response objects
    response_text = ""
    thinking_text = ""
    resp_data = None
    if isinstance(response, dict):
        resp_data = response
    elif hasattr(response, "body"):
        # JSONResponse / Response objects have a .body attribute
        try:
            resp_data = json.loads(response.body)
        except Exception:
            pass
    if resp_data:
        choices = resp_data.get("choices", [])
        if choices:
            choice = choices[0]
            # Chat completions format: choices[0].message.content
            msg = choice.get("message", {})
            response_text = msg.get("content") or ""
            thinking_text = msg.get("reasoning_content") or ""
            # If no content but has reasoning, model ran out of tokens on thinking
            if not response_text and thinking_text:
                response_text = "(thinking only, no response generated)"
            # Text completions format: choices[0].text (used by code-eval)
            if not response_text:
                response_text = choice.get("text", "")

    # Increment counter
    counter = _eval_event_counters.get(job_id, 0)
    _eval_event_counters[job_id] = counter + 1

    # Look up total sample count from job meta
    total = _get_eval_total(job_id)

    event = {
        "type": "progress",
        "index": counter,
        "total": total,
        "eval_type": eval_type,
        "job_id": job_id,
        "prompt": str(prompt)[:2000],
        "thinking": str(thinking_text)[:4000],
        "response": str(response_text)[:2000],
        "model": form_data.get("model", ""),
        "timestamp": datetime.now().isoformat(),
    }

    with open(events_file, "a") as f:
        f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")


@app.post("/api/chat/completions")
async def chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
):
    if not request.app.state.MODELS:
        await get_all_models(request)

    tasks = form_data.pop("background_tasks", None)
    try:
        model_id = form_data.get("model", None)
        if model_id not in request.app.state.MODELS:
            raise Exception("Model not found")
        model = request.app.state.MODELS[model_id]

        # Check if user has access to the model
        if not BYPASS_MODEL_ACCESS_CONTROL and user.role == "user":
            try:
                check_model_access(user, model)
            except Exception as e:
                raise e

        metadata = {
            "user_id": user.id,
            "chat_id": form_data.pop("chat_id", None),
            "message_id": form_data.pop("id", None),
            "session_id": form_data.pop("session_id", None),
            "tool_ids": form_data.get("tool_ids", None),
            "files": form_data.get("files", None),
            "features": form_data.get("features", None),
            # Popped, not read: this must reach the tool layer via metadata but
            # must NOT stay in the payload sent to the model. metadata is built
            # from this explicit allowlist, so a new top-level field the client
            # sends is dropped unless it is named here.
            "web_crawl_kb_id": form_data.pop("web_crawl_kb_id", None),
        }

        # T-301 (LR/R1). Bound `top_logprobs` HERE, at the core boundary, so an
        # absurd value fails with a reason instead of being clamped silently by
        # llama.cpp's `std::min(max_probs, n_probs_request)` -- which succeeds
        # and returns a smaller set than was asked for, saying nothing.
        #
        # Returns None for every request that did not ask for logprobs, which is
        # all ordinary chat: nothing is added to those payloads and nothing about
        # them changes. The resolved settings ride on `metadata` rather than the
        # payload so the relay can echo them to the client without sending them
        # back upstream as a parameter.
        try:
            logprobs_settings = resolve_logprobs_request(form_data)
        except TokenizationParamError as e:
            # 400 with the reason as the detail, raised explicitly rather than
            # left to the generic handler below: that one wraps the exception as
            # `str(e)`, which is fine for a bare message but is the same
            # collapse the self.ai#35 comment further down warns about. The
            # client shows this text to an artist, so it should arrive intact.
            raise HTTPException(status_code=400, detail=str(e))
        if logprobs_settings is not None:
            # T-302 (LR/R1-AC5). Refuse rather than downgrade: a session that
            # silently answers without distributions looks like a broken feature
            # rather than an unsupported model.
            try:
                assert_model_can_tokenize(model)
            except TokenizationParamError as e:
                raise HTTPException(status_code=400, detail=str(e))
            metadata["logprobs_settings"] = logprobs_settings

        form_data["metadata"] = metadata

        form_data, events = await process_chat_payload(request, form_data, metadata, user, model)
    except HTTPException:
        # A downstream handler already chose a status and, increasingly, a
        # MACHINE-READABLE detail body. Collapsing that into 400 + str(e)
        # destroys both: `str()` on a Starlette HTTPException renders
        # "503: {'detail': ..., 'gpu_locked_by': 'training'}" -- the status
        # becomes a prefix inside a string and the dict becomes a Python repr.
        # See the paired handler below for the self.ai#35 case this broke.
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # Detect eval job requests (authenticated via JIT token)
    eval_job_id = getattr(request.state, "eval_job_id", None)
    eval_type = getattr(request.state, "eval_type", None)

    # Force non-streaming for eval requests so we get a dict response
    # that _log_eval_event can extract content from.
    # Allow thinking so reasoning is captured in the live view.
    if eval_job_id:
        form_data["stream"] = False

    try:
        response = await generate_chat_completion_with_tools(request, form_data, user)

        # Log eval request prompt/response for live streaming
        if eval_job_id:
            try:
                _log_eval_event(eval_job_id, eval_type, form_data, response)
            except Exception:
                pass  # never break eval inference

        return await process_chat_response(request, response, form_data, user, events, metadata, tasks)
    except HTTPException:
        # self.ai#35: the GPU-window lease checkpoint (utils/lease_admission)
        # refuses local generation with HTTPException(503, detail={"detail": ...,
        # "gpu_locked_by": "training"}) so a caller can tell "GPU policy" from
        # "llamolotl is down". Re-wrapping it as 400 + str(e) turned BOTH of
        # those signals into one opaque string -- measured live 2026-08-04
        # against an active training window, this endpoint answered
        #   400 {"detail": "503: {'detail': 'GPU dedicated to the active
        #        training window ...', 'gpu_locked_by': 'training'}"}
        # while /llamolotl/chat/completions, which does not re-wrap, correctly
        # answered 503 with the structured body. Pass HTTPExceptions through
        # untouched; only non-HTTP exceptions become a 400.
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# Alias for chat_completion (Legacy)
generate_chat_completions = chat_completion
generate_chat_completion = chat_completion


# Anthropic's documented status -> error.type pairing. An Anthropic SDK reads
# error.type, not the prose message, to decide whether a failure is retryable.
ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


def _anthropic_error(status_code: int, detail) -> JSONResponse:
    """Render an error in the Anthropic envelope, preserving a structured detail.

    self.ai#103 is the cautionary case in the other direction: a structured body
    (the VRAM router's "needs an estimated N MiB") collapsed into a bare status
    string and became undiagnosable. A dict detail is kept whole under `detail`
    here for exactly that reason.
    """
    if isinstance(detail, dict):
        message = detail.get("detail") or json.dumps(detail)
    else:
        message = str(detail)

    error = {"type": ANTHROPIC_ERROR_TYPES.get(status_code, "api_error"), "message": message}
    if isinstance(detail, dict):
        error["detail"] = detail

    return JSONResponse(status_code=status_code, content={"type": "error", "error": error})


@app.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
):
    """Serve the Anthropic Messages API from self.ai's own model catalog (self.ai#112).

    **This is the inbound direction, and it is not `/anthropic/*`.** The router
    mounted at `/anthropic` is an *outbound adapter* -- OpenAI shape in, Anthropic
    upstream out -- so despite the name it cannot serve a client that speaks
    Anthropic natively. This endpoint is its mirror: Anthropic shape in, converted
    to OpenAI, handed to the ordinary chat-completions path, and converted back on
    the way out.

    Because the request goes through `chat_completion` rather than around it, the
    `model` is a **self.ai catalog id**, not an Anthropic one, and it may resolve to
    anything self.ai serves -- a local llamolotl model, an opencode/Zen gateway,
    Anthropic itself. Per-user model access control, the GPU-window checkpoint, and
    the persistence path all apply unchanged, because they are that function's job
    and not re-implemented here.

    Not implemented, deliberately (see the issue): server-side tools, `thinking`
    blocks, and the wider Messages surface. `thinking` is the load-bearing omission
    -- a thinking block carries a signature only the originating model can produce,
    so reasoning from a local model cannot be re-emitted as one that an SDK will
    accept. Reasoning is dropped rather than forged.
    """
    if not request.app.state.MODELS:
        await get_all_models(request)

    model_id = form_data.get("model")
    if not model_id:
        return _anthropic_error(400, "field required: model")
    if model_id not in request.app.state.MODELS:
        # chat_completion answers 400 "Model not found" for this; Anthropic clients
        # expect 404, and a wrong model id is by far the most common way this
        # endpoint is misconfigured, so it is worth naming precisely.
        return _anthropic_error(404, f"model: {model_id}")
    if form_data.get("max_tokens") is None:
        # Required on Anthropic, optional on OpenAI -- so the conversion would
        # silently succeed and the client would get a reply it considers invalid.
        return _anthropic_error(400, "field required: max_tokens")

    openai_payload = convert_payload_anthropic_to_openai(form_data)
    stream = openai_payload.get("stream", False)

    try:
        response = await chat_completion(request, openai_payload, user)
    except HTTPException as e:
        return _anthropic_error(e.status_code, e.detail)

    if stream:
        return StreamingResponse(
            convert_streaming_response_openai_to_anthropic(response, model=model_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            background=getattr(response, "background", None),
        )

    if not isinstance(response, dict):
        # A non-streaming request that came back as a stream anyway. Rather than
        # hand the client a body it cannot parse, drain it through the same
        # converter and let it build the message.
        return StreamingResponse(
            convert_streaming_response_openai_to_anthropic(response, model=model_id),
            media_type="text/event-stream",
            background=getattr(response, "background", None),
        )

    return convert_response_openai_to_anthropic(response, model=model_id)


def _anthropic_created_at(created):
    """``created`` as an RFC-3339 string, or None when it cannot be one.

    The catalog is NOT type-uniform in this field and that is what broke this
    endpoint: llamolotl models carry an int epoch, while every Anthropic-provider
    entry carries an ISO-8601 STRING (``'2026-07-24T00:00:00Z'``). The original
    guard tested truthiness rather than type, so those strings sailed past it
    into ``datetime.fromtimestamp()`` and raised

        TypeError: 'str' object cannot be interpreted as an integer

    which surfaced as a bare HTTP 500 on the whole listing. Not for an edge-case
    model either: any Anthropic model in the catalog poisoned the entire
    response, and there are always ten. An Anthropic client pointed at self.ai
    therefore could not discover ANY model -- the exact failure this endpoint
    exists to prevent (self.ai#112), and a plausible cause of an agent CLI
    falling back to a default context window and then overrunning ours.

    Never raises. A malformed value costs one model its timestamp; it must not
    cost the caller the catalog.
    """
    # Local import: this module has no top-level `datetime`/`UTC` (the one other
    # user imports it inside its own function), and a module-scope NameError here
    # would take the whole app down at import rather than one endpoint.
    from datetime import UTC, datetime

    if created is None or isinstance(created, bool):
        return None
    if isinstance(created, (int, float)):
        try:
            return datetime.fromtimestamp(created, UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(created, str):
        text = created.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return None


@app.get("/v1/models")
async def anthropic_list_models(request: Request, user=Depends(get_verified_user)):
    """Anthropic-shaped model listing, so an Anthropic client pointed at self.ai
    can discover what it may call (self.ai#112).

    Same catalog and the same per-user access filtering as `/api/models` -- this
    only reshapes it. Pagination fields are reported honestly rather than omitted:
    the underlying listing is not paginated, so `has_more` is always false.
    """
    listing = await get_models(request, user)
    models = listing.get("data") or []

    data = [
        {
            "type": "model",
            "id": model.get("id"),
            "display_name": model.get("name") or model.get("id"),
            "created_at": _anthropic_created_at(model.get("created")),
        }
        for model in models
    ]

    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }


@app.post("/api/completions")
async def text_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
):
    if not request.app.state.MODELS:
        await get_all_models(request)

    try:
        model_id = form_data.get("model", None)
        if model_id not in request.app.state.MODELS:
            raise Exception("Model not found")

        model = request.app.state.MODELS[model_id]

        # Check if user has access to the model
        if not BYPASS_MODEL_ACCESS_CONTROL and user.role == "user":
            try:
                check_model_access(user, model)
            except Exception as e:
                raise e

    except HTTPException:
        # Same passthrough as /api/chat/completions above -- notably this keeps
        # check_model_access's 403 a 403 instead of reporting it as a 400 whose
        # detail is the string "403: Model not found".
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # Detect eval job requests (authenticated via JIT token)
    eval_job_id = getattr(request.state, "eval_job_id", None)
    eval_type = getattr(request.state, "eval_type", None)

    # Force non-streaming for eval requests so we get a dict response
    if eval_job_id:
        form_data["stream"] = False

    try:
        response = await completion_handler(request, form_data, user)

        # Log eval request for live streaming
        if eval_job_id:
            try:
                _log_eval_event(eval_job_id, eval_type, form_data, response)
            except Exception:
                pass  # never break eval inference

        return response
    except HTTPException:
        # self.ai#35: the lease checkpoint guards generate_completion too, so
        # this endpoint flattened the GPU-window 503 exactly like the chat one.
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@app.post("/api/chat/completed")
async def chat_completed(request: Request, form_data: dict, user=Depends(get_verified_user)):
    try:
        return await chat_completed_handler(request, form_data, user)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@app.post("/api/chat/actions/{action_id}")
async def chat_action(request: Request, action_id: str, form_data: dict, user=Depends(get_verified_user)):
    try:
        return await chat_action_handler(request, action_id, form_data, user)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@app.post("/api/tasks/stop/{task_id}")
async def stop_task_endpoint(task_id: str, user=Depends(get_verified_user)):
    try:
        result = await stop_task(task_id)  # Use the function from tasks.py
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@app.get("/api/tasks")
async def list_tasks_endpoint(user=Depends(get_verified_user)):
    return {"tasks": list_tasks()}  # Use the function from tasks.py


##################################
#
# Config Endpoints
#
##################################


@app.get("/api/config")
async def get_app_config(request: Request):
    user = None
    if "token" in request.cookies:
        token = request.cookies.get("token")
        try:
            data = decode_token(token)
        except Exception as e:
            log.debug(e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )
        if data is not None and "id" in data:
            user = Users.get_user_by_id(data["id"])

    onboarding = False
    if user is None:
        user_count = Users.get_num_users()
        onboarding = user_count == 0

    return {
        **({"onboarding": True} if onboarding else {}),
        "status": True,
        "name": WEBUI_NAME,
        "version": VERSION,
        "default_locale": str(DEFAULT_LOCALE),
        "oauth": {"providers": {name: config.get("name", name) for name, config in OAUTH_PROVIDERS.items()}},
        "features": {
            "auth": WEBUI_AUTH,
            "auth_trusted_header": bool(app.state.AUTH_TRUSTED_EMAIL_HEADER),
            "enable_ldap": app.state.config.ENABLE_LDAP,
            "enable_api_key": app.state.config.ENABLE_API_KEY,
            "enable_signup": app.state.config.ENABLE_SIGNUP,
            "enable_login_form": app.state.config.ENABLE_LOGIN_FORM,
            "enable_websocket": ENABLE_WEBSOCKET_SUPPORT,
            **(
                {
                    "enable_channels": app.state.config.ENABLE_CHANNELS,
                    "enable_web_search": app.state.config.ENABLE_RAG_WEB_SEARCH,
                    "enable_deep_research": app.state.config.ENABLE_DEEP_RESEARCH,
                    "enable_web_crawl": app.state.config.ENABLE_WEB_CRAWL,
                    "enable_google_drive_integration": app.state.config.ENABLE_GOOGLE_DRIVE_INTEGRATION,
                    "enable_image_generation": app.state.config.ENABLE_IMAGE_GENERATION,
                    "enable_community_sharing": app.state.config.ENABLE_COMMUNITY_SHARING,
                    "enable_message_rating": app.state.config.ENABLE_MESSAGE_RATING,
                    "enable_admin_export": ENABLE_ADMIN_EXPORT,
                    "enable_admin_chat_access": ENABLE_ADMIN_CHAT_ACCESS,
                    "enable_curator": app.state.config.ENABLE_CURATOR_API,
                    "enable_piston_execution": app.state.config.ENABLE_PISTON_EXECUTION,
                }
                if user is not None
                else {}
            ),
        },
        **(
            {
                "google_drive": {
                    "client_id": GOOGLE_DRIVE_CLIENT_ID.value,
                    "api_key": GOOGLE_DRIVE_API_KEY.value,
                },
                "default_models": app.state.config.DEFAULT_MODELS,
                "default_prompt_suggestions": app.state.config.DEFAULT_PROMPT_SUGGESTIONS,
                "audio": {
                    "tts": {
                        "engine": app.state.config.TTS_ENGINE,
                        "voice": app.state.config.TTS_VOICE,
                        "split_on": app.state.config.TTS_SPLIT_ON,
                    },
                    "stt": {
                        "engine": app.state.config.STT_ENGINE,
                    },
                },
                "file": {
                    "max_size": app.state.config.FILE_MAX_SIZE,
                    "max_count": app.state.config.FILE_MAX_COUNT,
                },
                "permissions": {**app.state.config.USER_PERMISSIONS},
            }
            if user is not None
            else {}
        ),
    }


class UrlForm(BaseModel):
    url: str


@app.get("/api/webhook")
async def get_webhook_url(user=Depends(get_admin_user)):
    return {
        "url": app.state.config.WEBHOOK_URL,
    }


@app.post("/api/webhook")
async def update_webhook_url(form_data: UrlForm, user=Depends(get_admin_user)):
    app.state.config.WEBHOOK_URL = form_data.url
    app.state.WEBHOOK_URL = app.state.config.WEBHOOK_URL
    return {"url": app.state.config.WEBHOOK_URL}


@app.get("/api/version")
async def get_app_version():
    return {
        "version": VERSION,
    }


@app.get("/api/version/updates")
async def get_app_latest_release_version():
    if OFFLINE_MODE:
        log.debug("Offline mode is enabled, returning current version as latest version")
        return {"current": VERSION, "latest": VERSION}
    try:
        timeout = aiohttp.ClientTimeout(total=1)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get("https://api.github.com/repos/open-webui/open-webui/releases/latest") as response:
                response.raise_for_status()
                data = await response.json()
                latest_version = data["tag_name"]

                return {"current": VERSION, "latest": latest_version[1:]}
    except Exception as e:
        log.debug(e)
        return {"current": VERSION, "latest": VERSION}


@app.get("/api/changelog")
async def get_app_changelog():
    return {key: CHANGELOG[key] for idx, key in enumerate(CHANGELOG) if idx < 5}


############################
# OAuth Login & Callback
############################

# SessionMiddleware is used by authlib for oauth
if len(OAUTH_PROVIDERS) > 0:
    app.add_middleware(
        SessionMiddleware,
        secret_key=WEBUI_SECRET_KEY,
        session_cookie="oui-session",
        same_site=WEBUI_SESSION_COOKIE_SAME_SITE,
        https_only=WEBUI_SESSION_COOKIE_SECURE,
    )


@app.get("/oauth/{provider}/login")
async def oauth_login(provider: str, request: Request):
    return await oauth_manager.handle_login(provider, request)


# OAuth login logic is as follows:
# 1. Attempt to find a user with matching subject ID, tied to the provider
# 2. If OAUTH_MERGE_ACCOUNTS_BY_EMAIL is true, find a user with the email address provided via OAuth
#    - This is considered insecure in general, as OAuth providers do not always verify email addresses
# 3. If there is no user, and ENABLE_OAUTH_SIGNUP is true, create a user
#    - Email addresses are considered unique, so we fail registration if the email address is already taken
@app.get("/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request, response: Response):
    return await oauth_manager.handle_callback(provider, request, response)


@app.get("/manifest.json")
async def get_manifest_json():
    return {
        "name": WEBUI_NAME,
        "short_name": WEBUI_NAME,
        "description": (
            "Self.AI UI is an open, extensible, user-friendly interface for " "AI that adapts to your workflow."
        ),
        "start_url": "/",
        "display": "standalone",
        "background_color": "#343541",
        "orientation": "natural",
        "icons": [
            {
                "src": "/static/logo.png",
                "type": "image/png",
                "sizes": "500x500",
                "purpose": "any",
            },
            {
                "src": "/static/logo.png",
                "type": "image/png",
                "sizes": "500x500",
                "purpose": "maskable",
            },
        ],
    }


@app.get("/opensearch.xml")
async def get_opensearch_xml():
    xml_content = rf"""
    <OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/" xmlns:moz="http://www.mozilla.org/2006/browser/search/">
    <ShortName>{WEBUI_NAME}</ShortName>
    <Description>Search {WEBUI_NAME}</Description>
    <InputEncoding>UTF-8</InputEncoding>
    <Image width="16" height="16" type="image/x-icon">{app.state.config.WEBUI_URL}/static/favicon.png</Image>
    <Url type="text/html" method="get" template="{app.state.config.WEBUI_URL}/?q={"{searchTerms}"}"/>
    <moz:SearchForm>{app.state.config.WEBUI_URL}</moz:SearchForm>
    </OpenSearchDescription>
    """
    return Response(content=xml_content, media_type="application/xml")


@app.get("/health")
async def healthcheck():
    return {"status": True}


@app.get("/health/db")
async def healthcheck_with_db():
    Session.execute(text("SELECT 1;")).all()
    return {"status": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/cache", StaticFiles(directory=CACHE_DIR), name="cache")


def swagger_ui_html(*args, **kwargs):
    return get_swagger_ui_html(
        *args,
        **kwargs,
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger-ui/favicon.png",
    )


applications.get_swagger_ui_html = swagger_ui_html

if os.path.exists(FRONTEND_BUILD_DIR):
    mimetypes.add_type("text/javascript", ".js")
    app.mount(
        "/",
        SPAStaticFiles(directory=FRONTEND_BUILD_DIR, html=True),
        name="spa-static-files",
    )
else:
    log.warning(f"Frontend build directory not found at '{FRONTEND_BUILD_DIR}'. Serving API only.")
