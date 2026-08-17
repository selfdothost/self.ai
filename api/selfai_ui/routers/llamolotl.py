import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse

import aiohttp
from aiocache import cached
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import (
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST,
    BYPASS_MODEL_ACCESS_CONTROL,
    SRC_LOG_LEVELS,
)
from selfai_ui.models.models import ModelForm, ModelMeta, ModelParams, Models
from selfai_ui.models.vram_leases import VramLeases, effective_held
from selfai_ui.utils.access_control import has_access, has_permission
from selfai_ui.utils.auth import get_admin_user, get_verified_user
from selfai_ui.utils.lease_admission import AdmissionAction, evaluate_admission
from selfai_ui.utils.model_context import llamolotl_context_fields
from selfai_ui.utils.model_modalities import architecture_fields
from selfai_ui.utils.model_versions import record_version_for_artifact
from selfai_ui.utils.model_vram import llamolotl_vram_fields
from selfai_ui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_model_system_prompt_to_body,
)
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket
from selfai_ui.utils.vram_admission import LLAMOLOTL_CONSUMER_ID, ensure_llamolotl_vram

# Audience string self.llamolotl's control port (:8093) validates tickets
# against — must match SERVICE_AUTH_AUDIENCE on that side (self.llamolotl#12).
LLAMOLOTL_AUDIENCE = "self.llamolotl"

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["LLAMOLOTL"])


##########################################
#
# Utility functions
#
##########################################


def require_training_access(request: Request, user) -> None:
    if user.role == "admin":
        return

    if has_permission(user.id, "studio.training", request.app.state.config.USER_PERMISSIONS):
        return

    raise HTTPException(status_code=401, detail=ERROR_MESSAGES.UNAUTHORIZED)


async def send_get_request(url, key=None, params=None, raise_on_error=False, ticket=None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                params=params,
                headers={
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                    **({TICKET_HEADER: ticket} if ticket else {}),
                },
            ) as response:
                result = await response.json()
                if raise_on_error and response.status >= 400:
                    detail = None
                    if isinstance(result, dict):
                        detail = result.get("detail") or result.get("error")
                    raise HTTPException(
                        status_code=response.status,
                        detail=(detail if detail else "Self.AI UI: Server Connection Error"),
                    )
                return result
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Connection error: {e}")
        if raise_on_error:
            raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
        return None


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


def router_error_detail(body) -> Optional[str]:
    """The most specific human-readable thing in an upstream error body.

    llama.cpp answers ``{"error": {"code", "message", "type"}}``; our own routes
    answer ``{"detail": ...}``. Both carry the sentence worth showing, and in
    the capacity case that sentence is the whole diagnostic --

        model name=X does not fit in VRAM: needs an estimated 22784 MiB but
        only 22755 MiB is free, and no more resident models are safe to evict

    Returns a STRING rather than the structured dict deliberately: every caller
    of this module renders ``detail`` straight into a toast, and handing them a
    dict shows "[object Object]" -- a different way to lose the same message.
    """
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            if message:
                return str(message)
        elif err:
            return str(err)
        detail = body.get("detail")
        if detail:
            return detail if isinstance(detail, str) else str(detail)
    elif isinstance(body, str) and body.strip():
        return body.strip()
    return None


async def read_error_body(r):
    """Read an errored response's payload BEFORE anything releases it.

    self.ai#103: this used to be ``r.raise_for_status()`` followed by a
    ``await r.json()`` inside the except branch -- but aiohttp's
    ``raise_for_status()`` calls ``response.release()``, which discards the
    payload. So that json() could never succeed and the handler always fell
    through to the ClientResponseError repr, rendering the router's precise
    capacity answer as a bare "503, message='Service Unavailable'". The body has
    to be read here, while it still exists.
    """
    try:
        return await r.json()
    except Exception:
        try:
            # Not every upstream error is JSON (a proxy 502, an HTML page). Cap
            # it so a giant body cannot become the error message.
            return (await r.text())[:2000]
        except Exception:
            return None
def _substitution_payload(substitution: dict) -> dict:
    """The body keys that announce an eval-window model substitution (T-016).

    ``selected_model_id`` is DELIBERATELY REUSED rather than invented: it is the
    existing "a different model actually answered" channel (built for arena
    models) and is already plumbed end to end — the SSE reader yields it, the
    response middleware persists it onto the message as ``selectedModelId``, and
    the client already treats it as the authority for regenerate and for rating.
    Emitting it here makes the stored chat history honest about which model
    answered, for free, instead of adding a second source of truth.

    ``model_substitution`` carries the part ``selected_model_id`` cannot: WHY.
    An arena pick and an eval-window substitution both leave served != requested,
    and the client must not have to infer which one happened from the model's
    ``owned_by``.
    """
    return {
        "selected_model_id": substitution["served"],
        "model_substitution": substitution,
    }


async def _prepend_substitution_event(substitution: dict, content):
    """Announce the substitution as the FIRST SSE event, then relay upstream
    unchanged — the same shape ``utils/chat.py`` uses for the arena pick, so the
    client's existing stream reader needs no new framing."""
    yield f"data: {json.dumps(_substitution_payload(substitution))}\n\n".encode()
    async for chunk in content:
        yield chunk


async def send_post_request(
    url: str,
    payload: str,
    stream: bool = True,
    key: Optional[str] = None,
    ticket: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    substitution: Optional[dict] = None,
):
    # extra_headers are added to the RESPONSE we return to our caller (not the
    # outbound request to llama.cpp) — used for the X-Selfai-Model-Substituted
    # marker (Decision 6 / R3, T-016) so a caller can always tell which model
    # actually answered.
    #
    # `substitution` is the same fact carried in the BODY, which is the only
    # place that survives the whole pipeline: the chat path is consumed
    # server-side and relayed over a websocket, so a response header never
    # reaches the browser at all. {"requested": ..., "served": ..., "reason": ...}.
    #
    # NOTE the non-streaming branch below returns a PLAIN DICT in every case.
    # It used to return a JSONResponse whenever extra_headers was set — i.e.
    # ONLY when a substitution had happened — so substituting silently changed
    # the return TYPE, and process_chat_response's non-streaming branch does
    # `if "selected_model_id" in response`, which raises
    # `TypeError: argument of type 'JSONResponse' is not iterable` on a Response
    # object. A non-streaming chat request that got substituted therefore blew up
    # instead of answering. Carrying the fact in the body removes the need for
    # the type switch entirely.
    r = None
    try:
        session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT))

        r = await session.post(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {key}"} if key else {}),
                **({TICKET_HEADER: ticket} if ticket else {}),
            },
        )
        if r.status >= 400:
            # Read-then-raise, NOT raise_for_status() -- see read_error_body().
            body = await read_error_body(r)
            status_code = r.status
            await cleanup_response(r, session)
            message = router_error_detail(body)
            raise HTTPException(
                status_code=status_code,
                detail=f"Llamolotl: {message}" if message else "Self.AI UI: Server Connection Error",
            )

        if stream:
            response_headers = dict(r.headers)
            if extra_headers:
                response_headers.update(extra_headers)
            return StreamingResponse(
                _prepend_substitution_event(substitution, r.content) if substitution else r.content,
                status_code=r.status,
                headers=response_headers,
                background=BackgroundTask(cleanup_response, response=r, session=session),
            )
        else:
            res = await r.json()
            await cleanup_response(r, session)
            if substitution and isinstance(res, dict):
                res = {**res, **_substitution_payload(substitution)}
            return res

    except HTTPException:
        # Already shaped above with the upstream's own message -- re-raising it
        # unchanged is the point of #103. Without this branch the generic
        # handler below would catch it and overwrite the message we just
        # recovered with the ClientResponseError repr all over again.
        raise
    except Exception as e:
        # A transport-level failure (connect refused, timeout, DNS): there is no
        # upstream body to report, so the exception itself is the best available
        # description.
        raise HTTPException(
            status_code=r.status if r is not None else 500,
            detail=f"Llamolotl: {e}",
        )


def get_api_key(url, configs):
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return configs.get(base_url, {}).get("key", None)


async def send_delete_request(url: str, key: Optional[str] = None, ticket: Optional[str] = None):
    r = None
    try:
        async with aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        ) as session:
            r = await session.delete(
                url,
                headers={
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                    **({TICKET_HEADER: ticket} if ticket else {}),
                },
            )
            r.raise_for_status()
            return await r.json()
    except Exception as e:
        detail = None

        if r is not None:
            try:
                res = await r.json()
                detail = res.get("detail") or res.get("error")
            except Exception:
                detail = f"Llamolotl: {e}"

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Self.AI UI: Server Connection Error",
        )


def get_llamolotl_connection(request: Request, url_idx: Optional[int] = None):
    base_urls = request.app.state.config.LLAMOLOTL_BASE_URLS
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS

    if not base_urls:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    idx = url_idx if url_idx is not None else 0
    if idx < 0 or idx >= len(base_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")
    if idx >= len(control_urls):
        raise HTTPException(
            status_code=500,
            detail="No matching Llamolotl control URL configured",
        )

    base_url = base_urls[idx]
    control_url = control_urls[idx]
    key = get_api_key(base_url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    return {
        "url_idx": idx,
        "base_url": base_url,
        "control_url": control_url,
        "key": key,
    }


def normalize_training_config_identifier(config_path: str) -> str:
    config_name = Path(config_path).name
    if config_name.endswith((".yaml", ".yml")):
        return Path(config_name).stem
    return config_name


##########################################
#
# API routes
#
##########################################

router = APIRouter()


@router.head("/")
@router.get("/")
async def get_status():
    return {"status": True}


class ConnectionVerificationForm(BaseModel):
    url: str
    key: Optional[str] = None


@router.post("/verify")
async def verify_connection(form_data: ConnectionVerificationForm, user=Depends(get_admin_user)):
    url = form_data.url
    key = form_data.key

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    ) as session:
        try:
            async with session.get(
                f"{url}/health",
                headers={**({"Authorization": f"Bearer {key}"} if key else {})},
            ) as r:
                if r.status != 200:
                    detail = f"HTTP Error: {r.status}"
                    try:
                        res = await r.json()
                        if "error" in res:
                            detail = f"External Error: {res['error']}"
                    except Exception:
                        pass
                    raise Exception(detail)

                data = await r.json()
                return data
        except aiohttp.ClientError as e:
            log.exception(f"Client error: {str(e)}")
            raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            error_detail = f"Unexpected error: {str(e)}"
            raise HTTPException(status_code=500, detail=error_detail)


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_LLAMOLOTL_API": request.app.state.config.ENABLE_LLAMOLOTL_API,
        "LLAMOLOTL_BASE_URLS": request.app.state.config.LLAMOLOTL_BASE_URLS,
        "LLAMOLOTL_API_CONFIGS": request.app.state.config.LLAMOLOTL_API_CONFIGS,
    }


@router.get("/api/status")
async def get_training_status(request: Request, user=Depends(get_verified_user)):
    require_training_access(request, user)
    return {
        "ENABLE_LLAMOLOTL_API": request.app.state.config.ENABLE_LLAMOLOTL_API,
        "LLAMOLOTL_BASE_URLS": request.app.state.config.LLAMOLOTL_BASE_URLS,
    }


class LlamolotlConfigForm(BaseModel):
    ENABLE_LLAMOLOTL_API: Optional[bool] = None
    LLAMOLOTL_BASE_URLS: list[str]
    LLAMOLOTL_API_CONFIGS: dict


@router.post("/config/update")
async def update_config(request: Request, form_data: LlamolotlConfigForm, user=Depends(get_admin_user)):
    request.app.state.config.ENABLE_LLAMOLOTL_API = form_data.ENABLE_LLAMOLOTL_API
    request.app.state.config.LLAMOLOTL_BASE_URLS = form_data.LLAMOLOTL_BASE_URLS
    request.app.state.config.LLAMOLOTL_API_CONFIGS = form_data.LLAMOLOTL_API_CONFIGS

    # Remove any extra configs
    config_urls = request.app.state.config.LLAMOLOTL_API_CONFIGS.keys()
    for url in list(request.app.state.config.LLAMOLOTL_BASE_URLS):
        if url not in config_urls:
            request.app.state.config.LLAMOLOTL_API_CONFIGS.pop(url, None)

    return {
        "ENABLE_LLAMOLOTL_API": request.app.state.config.ENABLE_LLAMOLOTL_API,
        "LLAMOLOTL_BASE_URLS": request.app.state.config.LLAMOLOTL_BASE_URLS,
        "LLAMOLOTL_API_CONFIGS": request.app.state.config.LLAMOLOTL_API_CONFIGS,
    }


##########################################
#
# Model discovery
#
##########################################


# A GGUF shard filename: "<base>-00001-of-00002", optionally still carrying
# .gguf. llama.cpp's own split naming, so the shape is fixed rather than a
# guess about how someone named a file.
_SHARD_SUFFIX = re.compile(r"^(?P<base>.+)-\d{5}-of-\d{5}(?:\.gguf)?$")


def drop_shard_duplicates(models: list) -> list:
    """Hide individual shards of a split model when the whole model is listed.

    The router discovers /models/GLM-4.5-Air-UD-Q4_K_XL/ as one model AND its
    first shard file as another, so the same 67 GB of weights appeared twice in
    the picker. Worse, only the consolidated entry matches the preset section,
    so the shard came through with no `vram-footprint-mib` -- the duplicate a
    user could actually select was the one with no VRAM declaration, which then
    loaded unguarded and was refused by the router with a message the proxy
    discarded (#103). Deactivating one of the pair to stop the double-listing
    hid the declared one, because the shard's Models row carried a trailing
    ".gguf" that never matches a router model id.

    An ORPHAN shard is kept. If no consolidated sibling is listed, that entry is
    the only way to reach those weights, and hiding it would remove a working
    model rather than a duplicate -- the same class of mistake this is fixing,
    pointed the other way.
    """
    ids = {m.get("id") for m in models}
    kept = []
    for model in models:
        match = _SHARD_SUFFIX.match(model.get("id") or "")
        if match and match.group("base") in ids:
            log.info(
                "llamolotl: hiding shard %r; the split model %r is listed",
                model.get("id"),
                match.group("base"),
            )
            continue
        kept.append(model)
    return kept


# key_builder ignores `request`: aiocache's default key builder stringifies
# every positional arg, and Starlette's Request has no stable __repr__ (falls
# back to object.__repr__, which includes the memory address) — a fresh
# Request per HTTP call means a guaranteed cache-key miss every time, so the
# ttl=3 cache never actually hit (same bug as openai.py/ollama.py's copies of
# this pattern). Confirmed live 2026-07-15: bulk-toggling ~20 models in the
# admin UI re-triggers GET /api/models per toggle, and with the cache
# defeated, each one re-fans-out to every LLAMOLOTL_BASE_URLS entry — root
# cause of the event-loop freeze/liveness-probe crash loop under normal bulk
# admin actions. Response only depends on global request.app.state.config,
# not anything per-request, so a fixed key is correct, not a workaround.
@cached(ttl=3, key_builder=lambda f, *args, **kwargs: "llamolotl:get_all_models")
async def get_all_models(request: Request):
    log.info("get_all_models()")
    if request.app.state.config.ENABLE_LLAMOLOTL_API:
        request_tasks = []

        for idx, url in enumerate(request.app.state.config.LLAMOLOTL_BASE_URLS):
            if url not in request.app.state.config.LLAMOLOTL_API_CONFIGS:
                request_tasks.append(send_get_request(f"{url}/v1/models"))
            else:
                api_config = request.app.state.config.LLAMOLOTL_API_CONFIGS.get(url, {})
                enable = api_config.get("enable", True)
                key = api_config.get("key", None)

                if enable:
                    request_tasks.append(send_get_request(f"{url}/v1/models", key))
                else:
                    request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

        responses = await asyncio.gather(*request_tasks)

        models = []
        # Raw upstream entries, kept alongside the reshaped list so the VRAM
        # admission check (self.ai#107) can size a lease request without a
        # second fan-out to every base URL. Deliberately separate: the list
        # above is a model-list contract other code consumes, this is a cache.
        raw_models = {}
        for idx, response in enumerate(responses):
            if response and "data" in response:
                url = request.app.state.config.LLAMOLOTL_BASE_URLS[idx]
                api_config = request.app.state.config.LLAMOLOTL_API_CONFIGS.get(url, {})

                prefix_id = api_config.get("prefix_id", None)
                model_ids = api_config.get("model_ids", [])

                for model in response["data"]:
                    model_id = model.get("id", "")

                    if len(model_ids) != 0 and model_id not in model_ids:
                        continue

                    if prefix_id:
                        model_id = f"{prefix_id}.{model_id}"

                    # Extract status from llama-server response
                    status_obj = model.get("status", {})
                    status_val = status_obj.get("value", "unloaded") if isinstance(status_obj, dict) else "unloaded"

                    if model_id not in [m["id"] for m in models]:
                        models.append(
                            {
                                "id": model_id,
                                "name": model_id,
                                "object": "model",
                                "created": model.get("created", 0),
                                "owned_by": "llamolotl",
                                "urls": [idx],
                                "status": status_val,
                                # Served window per request, from the loaded child
                                # if there is one and from the effective preset
                                # otherwise. Absent when llama-server pins no
                                # size (self.ai#87).
                                **llamolotl_context_fields(model),
                                # How much of the card this model takes and how
                                # the router knows it (self.ai#107). Omitted
                                # when unknown — utils/model_vram.py explains
                                # why that is not the same as zero.
                                **llamolotl_vram_fields(model),
                                # input/output modalities, passed through whole
                                # (self.ai#139). This reshape builds an explicit
                                # dict rather than spreading the upstream entry,
                                # so anything not named here is dropped — which
                                # is exactly how `architecture` was being lost
                                # before core could derive `vision` from it.
                                **architecture_fields(model),
                            }
                        )
                        raw_models[model_id] = model
                    else:
                        for m in models:
                            if m["id"] == model_id:
                                m["urls"].append(idx)
                                break

        result = {"data": drop_shard_duplicates(models)}
    else:
        result = {"data": []}
        raw_models = {}

    request.app.state.LLAMOLOTL_MODELS = {model["id"]: model for model in result["data"]}
    request.app.state.LLAMOLOTL_RAW_MODELS = raw_models
    return result


@router.get("/models")
@router.get("/models/{url_idx}")
async def get_llamolotl_models(request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)):
    if url_idx is None:
        models = await get_all_models(request)
    else:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
        key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
        models = await send_get_request(
            f"{url}/v1/models",
            key,
        )
        if models is None:
            raise HTTPException(
                status_code=500,
                detail="Self.AI UI: Server Connection Error",
            )

    return models


##########################################
#
# Chat completion
#
##########################################


async def get_llamolotl_url(request: Request, model: str, url_idx: Optional[int] = None):
    if url_idx is None:
        models = request.app.state.LLAMOLOTL_MODELS
        if model not in models:
            raise HTTPException(
                status_code=400,
                detail=ERROR_MESSAGES.MODEL_NOT_FOUND(model),
            )
        url_idx = random.choice(models[model].get("urls", []))
    url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    return url


# Track currently applied LoRAs to avoid unnecessary restarts
_current_applied_loras: list | None = None


async def _ensure_loras_applied(request: Request, model_info):
    """Ensure the correct LoRAs are applied to llama-server for this model.

    Compares the model's active_loras metadata with what's currently applied,
    and only calls apply-loras (which restarts llama-server) if they differ.
    """
    global _current_applied_loras

    desired_loras = []
    if model_info.meta:
        meta = model_info.meta.model_dump() if hasattr(model_info.meta, "model_dump") else model_info.meta
        desired_loras = meta.get("active_loras") or []

    # Normalize for comparison: sort by file name
    desired_sorted = sorted(desired_loras, key=lambda lora: lora.get("file", ""))
    current_sorted = sorted(_current_applied_loras or [], key=lambda lora: lora.get("file", ""))

    if desired_sorted == current_sorted:
        return  # Already in the right state

    # Apply the LoRAs via the control server
    try:
        connection = get_llamolotl_connection(request)
        result = await send_post_request(
            url=f"{connection['control_url']}/api/system/apply-loras",
            payload=json.dumps({"loras": desired_loras}),
            stream=False,
            key=connection["key"],
            ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "system:write"),
        )
        if result and result.get("status") == "applied":
            _current_applied_loras = desired_loras
            log.info(f"Applied LoRAs for model {model_info.id}: {desired_loras}")
        else:
            log.warning(f"Failed to apply LoRAs for model {model_info.id}: {result}")
    except Exception as e:
        log.warning(f"Error applying LoRAs for model {model_info.id}: {e}")


@router.post("/chat/completions")
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    bypass_filter: Optional[bool] = False,
):
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    payload = {**form_data}
    if "metadata" in payload:
        del payload["metadata"]

    model_id = payload.get("model")
    model_info = Models.get_model_by_id(model_id)

    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id
            model_id = model_info.base_model_id

        params = model_info.params.model_dump()
        payload = apply_model_params_to_body_openai(params, payload)
        payload = apply_model_system_prompt_to_body(params, payload, user)

        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            if not (
                user.id == model_info.user_id
                or has_access(user.id, type="read", access_control=model_info.access_control)
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Model not found",
                )
    elif not bypass_filter:
        if user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Model not found",
            )

    # Decision 6 / R3: lease admission checkpoint — consult the broker/registry
    # BEFORE dispatching a local generation (T-014 seam; T-015 exclusive-refuse,
    # T-016 eval-substitute, T-019 staleness fill the logic against this seam).
    admission = evaluate_admission(request, model_id)
    substituted_from = None
    if admission.action == AdmissionAction.REFUSE:
        raise HTTPException(
            status_code=admission.refuse_status, detail=admission.refuse_detail
        )
    substitution = None
    if admission.action == AdmissionAction.SUBSTITUTE and admission.substitute_model:
        # Serve the already-loaded eval model instead of the requested one; record
        # the original id for the X-Selfai-Model-Substituted response header (T-016).
        substituted_from = admission.requested_model or model_id
        model_id = admission.substitute_model
        payload["model"] = admission.substitute_model
        # The same fact in the response BODY. The header alone never reaches the
        # browser: the chat path is consumed server-side and relayed over a
        # websocket, so the substitution was invisible to the user -- they asked
        # for one model, got another's answer, and the chat was saved against the
        # one they asked for.
        substitution = {
            "requested": substituted_from,
            "served": model_id,
            "reason": "eval_window",
        }
        # Re-resolve model_info so the SUBSTITUTED model's LoRA is applied below,
        # not the originally-requested one (#35-R2 substituted-LoRA).
        model_info = Models.get_model_by_id(model_id) or model_info

    # self.ai#107: reserve the card if this request is about to make llamolotl
    # AUTOLOAD. Kept separate from evaluate_admission() above on purpose — that
    # checkpoint is deliberately synchronous with no network in the request
    # path, and a broker grant may have to call other tenants to release. This
    # runs after substitution so it reserves for the model actually served, and
    # it short-circuits with no broker call at all when the model is already
    # resident, which is the overwhelmingly common case.
    vram = await ensure_llamolotl_vram(request.app.state, model_id)
    if vram.denied:
        raise HTTPException(status_code=503, detail=vram.detail)

    # Ensure the correct LoRAs are applied before generating
    if model_info:
        await _ensure_loras_applied(request, model_info)

    url = await get_llamolotl_url(request, model_id)
    api_config = request.app.state.config.LLAMOLOTL_API_CONFIGS.get(url, {})

    prefix_id = api_config.get("prefix_id", None)
    if prefix_id:
        payload["model"] = payload["model"].replace(f"{prefix_id}.", "")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    stream = payload.get("stream", True)

    # Convert max_completion_tokens to max_tokens for llama.cpp compatibility
    if "max_completion_tokens" in payload:
        payload["max_tokens"] = payload["max_completion_tokens"]
        del payload["max_completion_tokens"]

    return await send_post_request(
        url=f"{url}/v1/chat/completions",
        payload=json.dumps(payload),
        stream=stream,
        key=key,
        extra_headers=(
            {"X-Selfai-Model-Substituted": substituted_from} if substituted_from else None
        ),
        substitution=substitution,
    )


@router.post("/completions")
async def generate_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    bypass_filter: Optional[bool] = False,
):
    """Text completions endpoint (non-chat). Proxies to llama.cpp /v1/completions."""
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    payload = {**form_data}
    if "metadata" in payload:
        del payload["metadata"]

    model_id = payload.get("model")
    model_info = Models.get_model_by_id(model_id)

    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id
            model_id = model_info.base_model_id

        params = model_info.params.model_dump()
        payload = apply_model_params_to_body_openai(params, payload)
        # No system prompt for text completions - it's raw text continuation

        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            if not (
                user.id == model_info.user_id
                or has_access(user.id, type="read", access_control=model_info.access_control)
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Model not found",
                )
    elif not bypass_filter:
        if user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Model not found",
            )

    # Decision 6 / R3: lease admission checkpoint — same seam as the chat path
    # (T-014; T-015/T-016/T-019 fill the logic). Applies only to this llamolotl
    # local-generation path; Ollama/cloud paths are untouched.
    admission = evaluate_admission(request, model_id)
    substituted_from = None
    substitution = None
    if admission.action == AdmissionAction.REFUSE:
        raise HTTPException(
            status_code=admission.refuse_status, detail=admission.refuse_detail
        )
    if admission.action == AdmissionAction.SUBSTITUTE and admission.substitute_model:
        substituted_from = admission.requested_model or model_id
        model_id = admission.substitute_model
        payload["model"] = admission.substitute_model
        substitution = {
            "requested": substituted_from,
            "served": model_id,
            "reason": "eval_window",
        }
        model_info = Models.get_model_by_id(model_id) or model_info

    # Ensure the correct LoRAs are applied before generating
    if model_info:
        await _ensure_loras_applied(request, model_info)

    url = await get_llamolotl_url(request, model_id)
    api_config = request.app.state.config.LLAMOLOTL_API_CONFIGS.get(url, {})

    prefix_id = api_config.get("prefix_id", None)
    if prefix_id:
        payload["model"] = payload["model"].replace(f"{prefix_id}.", "")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    stream = payload.get("stream", False)

    # Convert max_completion_tokens to max_tokens for llama.cpp compatibility
    if "max_completion_tokens" in payload:
        payload["max_tokens"] = payload["max_completion_tokens"]
        del payload["max_completion_tokens"]

    return await send_post_request(
        url=f"{url}/v1/completions",
        payload=json.dumps(payload),
        stream=stream,
        key=key,
        extra_headers=(
            {"X-Selfai-Model-Substituted": substituted_from} if substituted_from else None
        ),
        substitution=substitution,
    )


##########################################
#
# Model pulling / management (via control server)
#
##########################################


class HFModelPullForm(BaseModel):
    name: str
    filename: Optional[str] = None


class HFModelDeleteForm(BaseModel):
    name: str


@router.post("/api/inspect")
@router.post("/api/inspect/{url_idx}")
async def inspect_model(
    request: Request,
    form_data: HFModelPullForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if url_idx >= len(control_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")

    control_url = control_urls[url_idx]
    base_url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    key = get_api_key(base_url, request.app.state.config.LLAMOLOTL_API_CONFIGS)

    return await send_post_request(
        url=f"{control_url}/api/models/inspect",
        payload=json.dumps({"name": form_data.name}),
        stream=False,
        key=key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:read"),
    )


@router.post("/api/pull/cancel")
@router.post("/api/pull/cancel/{url_idx}")
async def cancel_pull(
    request: Request,
    form_data: HFModelPullForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if url_idx >= len(control_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")

    control_url = control_urls[url_idx]
    base_url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    key = get_api_key(base_url, request.app.state.config.LLAMOLOTL_API_CONFIGS)

    return await send_post_request(
        url=f"{control_url}/api/models/pull/cancel",
        payload=json.dumps(form_data.model_dump(exclude_none=True)),
        stream=False,
        key=key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:pull"),
    )


@router.post("/api/pull")
@router.post("/api/pull/{url_idx}")
async def pull_model(
    request: Request,
    form_data: HFModelPullForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if url_idx >= len(control_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")

    control_url = control_urls[url_idx]
    base_url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    key = get_api_key(base_url, request.app.state.config.LLAMOLOTL_API_CONFIGS)

    return await send_post_request(
        url=f"{control_url}/api/models/pull",
        payload=json.dumps(form_data.model_dump(exclude_none=True)),
        stream=True,
        key=key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:pull"),
    )


@router.post("/api/delete")
@router.post("/api/delete/{url_idx}")
async def delete_model(
    request: Request,
    form_data: HFModelDeleteForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if url_idx >= len(control_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")

    control_url = control_urls[url_idx]
    base_url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    key = get_api_key(base_url, request.app.state.config.LLAMOLOTL_API_CONFIGS)

    return await send_post_request(
        url=f"{control_url}/api/models/delete",
        payload=json.dumps(form_data.model_dump()),
        stream=False,
        key=key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:delete"),
    )


##########################################
#
# Model integrity sweep (self.ai/self.ai#38)
#
##########################################


@router.get("/api/integrity")
async def get_model_integrity_warnings(request: Request, user=Depends(get_admin_user)):
    """Findings from the periodic /models integrity sweep
    (utils/model_integrity.run_periodic_sweep), keyed by LLAMOLOTL_BASE_URLS
    entry. Empty until the first sweep cycle completes, or if the sweep is
    disabled via ENABLE_MODEL_INTEGRITY_SWEEP=false."""
    return {"warnings": getattr(request.app.state, "MODEL_INTEGRITY_WARNINGS", {})}


@router.get("/api/available-models")
@router.get("/api/available-models/{url_idx}")
async def get_available_models(
    request: Request,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if url_idx >= len(control_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")

    control_url = control_urls[url_idx]
    base_url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    key = get_api_key(base_url, request.app.state.config.LLAMOLOTL_API_CONFIGS)

    result = await send_get_request(
        f"{control_url}/api/models/available",
        key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


class HFModelRegisterForm(BaseModel):
    name: str
    # self.ai#131: attach this registration to a model line. Name an existing
    # line to add a version to it (three quantizations of one source belong on
    # one line); leave both unset to start a new line named after the model.
    line_id: Optional[str] = None
    line_name: Optional[str] = None


@router.post("/api/register")
@router.post("/api/register/{url_idx}")
async def register_model(
    request: Request,
    form_data: HFModelRegisterForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if url_idx >= len(control_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")

    control_url = control_urls[url_idx]
    base_url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    key = get_api_key(base_url, request.app.state.config.LLAMOLOTL_API_CONFIGS)

    result = await send_post_request(
        url=f"{control_url}/api/models/register",
        payload=json.dumps(form_data.model_dump()),
        stream=False,
        key=key,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:write"),
    )

    # Fetch metadata from llamolotl and persist lineage in the UI Model table
    try:
        available = await send_get_request(
            f"{control_url}/api/models/available",
            key,
            ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:read"),
        )
        lineage = {}
        if available:
            for m in available if isinstance(available, list) else []:
                if Path(m.get("name", "")).name == Path(form_data.name).name:
                    lineage = {
                        k: m.get(k)
                        for k in (
                            "hf_repo",
                            "quant",
                            "source_type",
                            "trainable",
                            "pulled_at",
                            "bake_info",
                        )
                        if m.get(k) is not None
                    }
                    break

        model_id = Path(form_data.name).name
        existing = Models.get_model_by_id(model_id)
        if existing:
            updated_meta = {**existing.meta.model_dump(), **lineage}
            Models.update_model_by_id(
                model_id,
                ModelForm(
                    id=model_id,
                    base_model_id=existing.base_model_id,
                    name=existing.name,
                    meta=ModelMeta(**updated_meta),
                    params=existing.params,
                    access_control=existing.access_control,
                    is_active=existing.is_active,
                ),
            )
        else:
            Models.insert_new_model(
                ModelForm(
                    id=model_id,
                    name=model_id,
                    meta=ModelMeta(**lineage),
                    params=ModelParams(),
                    is_active=True,
                ),
                user.id,
            )
    except Exception as e:
        log.warning(f"Failed to persist model lineage for {form_data.name}: {e}")

    # self.ai#131: land the GGUF as a version on a line rather than as an
    # orphan. Its own try/except and its own log line, deliberately: a
    # self.corpus outage must not make a model unservable, and "lineage
    # metadata failed" and "line attachment failed" are different faults to
    # chase. With ENABLE_SELF_CORPUS off there is no line and no version, and
    # neither appears retroactively without an explicit backfill.
    if request.app.state.config.ENABLE_SELF_CORPUS:
        try:
            await _attach_registration_to_line(request, form_data, user)
        except Exception as e:
            log.warning(f"Failed to attach {form_data.name} to a model line: {e}")

    return result


async def _attach_registration_to_line(request: Request, form_data: HFModelRegisterForm, user) -> None:
    """Record a registered model as the next `base` version of a line.

    The lineage already written onto the Model row is carried into the
    version's provenance rather than copied — the version record is the source
    of truth for "what produced this", and the Model row keeps only a pointer
    to which version it is.
    """
    model_id = Path(form_data.name).name
    model = Models.get_model_by_id(model_id)
    meta = model.meta.model_dump() if model else {}

    version = await record_version_for_artifact(
        request.app.state,
        user_id=user.id,
        kind="base",
        artifact_ref=model_id,
        manifest={
            "model": model_id,
            "source": "llamolotl registration",
            "lineage": {
                key: meta.get(key)
                for key in ("hf_repo", "quant", "source_type", "trainable", "pulled_at", "bake_info")
                if meta.get(key) is not None
            },
        },
        line_id=form_data.line_id,
        line_name=form_data.line_name or model_id,
        produced_by={"job_kind": "register", "job_id": None},
    )

    if model is None:
        return
    Models.update_model_by_id(
        model_id,
        ModelForm(
            id=model_id,
            base_model_id=model.base_model_id,
            name=model.name,
            meta=ModelMeta(**{**meta, "line_id": version.line_id, "version_id": version.id}),
            params=model.params,
            access_control=model.access_control,
            is_active=model.is_active,
        ),
    )


##########################################
#
# Bake pipeline (via control server)
#
##########################################


class BakeModelForm(BaseModel):
    base_model: str
    adapters: list  # [{"path": "...", "weight": 0.7}, ...]
    output_name: str
    outtype: str = "f16"
    quant_type: Optional[str] = None


@router.post("/api/pipeline/bake")
@router.post("/api/pipeline/bake/{url_idx}")
async def bake_model(
    request: Request,
    form_data: BakeModelForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    connection = get_llamolotl_connection(request, url_idx)
    return await send_post_request(
        url=f"{connection['control_url']}/api/pipeline/bake",
        payload=json.dumps(form_data.model_dump(exclude_none=True)),
        stream=False,
        key=connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "pipeline:write"),
    )


##########################################
#
# LoRA management (via control server)
#
##########################################


class ApplyLorasForm(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_id: str
    loras: list  # [{"file": "adapter.gguf", "scale": 0.7}, ...]


@router.post("/api/system/apply-loras")
@router.post("/api/system/apply-loras/{url_idx}")
async def apply_loras(
    request: Request,
    form_data: ApplyLorasForm,
    url_idx: int = 0,
    user=Depends(get_admin_user),
):
    connection = get_llamolotl_connection(request, url_idx)

    # Forward to llamolotl
    result = await send_post_request(
        url=f"{connection['control_url']}/api/system/apply-loras",
        payload=json.dumps({"loras": form_data.loras}),
        stream=False,
        key=connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "system:write"),
    )

    # Persist active LoRAs in the Model's meta
    try:
        model = Models.get_model_by_id(form_data.model_id)
        if model:
            updated_meta = {**model.meta.model_dump(), "active_loras": form_data.loras}
            Models.update_model_by_id(
                form_data.model_id,
                ModelForm(
                    id=form_data.model_id,
                    base_model_id=model.base_model_id,
                    name=model.name,
                    meta=ModelMeta(**updated_meta),
                    params=model.params,
                    access_control=model.access_control,
                    is_active=model.is_active,
                ),
            )
    except Exception as e:
        log.warning(f"Failed to persist active LoRAs for {form_data.model_id}: {e}")

    return result


@router.get("/api/system/active-loras")
@router.get("/api/system/active-loras/{url_idx}")
async def get_active_loras(
    request: Request,
    url_idx: int = 0,
    user=Depends(get_verified_user),
):
    connection = get_llamolotl_connection(request, url_idx)
    return await send_get_request(
        f"{connection['control_url']}/api/system/active-loras",
        connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "system:read"),
    )


@router.get("/api/loras/available")
@router.get("/api/loras/available/{url_idx}")
async def list_available_loras(
    request: Request,
    url_idx: int = 0,
    user=Depends(get_verified_user),
):
    connection = get_llamolotl_connection(request, url_idx)
    return await send_get_request(
        f"{connection['control_url']}/api/loras/available",
        connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "pipeline:read"),
    )


##########################################
#
# Training management (via control server)
#
##########################################


class TrainingJobCreateForm(BaseModel):
    config_path: str
    output_dir: Optional[str] = None
    base_model: Optional[str] = None


@router.get("/api/configs")
async def list_training_configs(
    request: Request,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/configs",
        connection["key"],
        raise_on_error=True,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.get("/api/configs/{config_name}")
async def get_training_config(
    request: Request,
    config_name: str,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/configs/{config_name}",
        connection["key"],
        raise_on_error=True,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.get("/api/jobs")
async def list_training_jobs(
    request: Request,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/jobs",
        connection["key"],
        raise_on_error=True,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.post("/api/jobs")
async def create_training_job(
    request: Request,
    form_data: TrainingJobCreateForm,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    payload = {"config_path": normalize_training_config_identifier(form_data.config_path)}
    if form_data.base_model:
        payload["base_model"] = form_data.base_model
    if form_data.output_dir:
        payload["overrides"] = {"output_dir": form_data.output_dir}

    return await send_post_request(
        url=f"{connection['control_url']}/api/jobs",
        payload=json.dumps(payload),
        stream=False,
        key=connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:create"),
    )


@router.get("/api/jobs/{job_id}")
async def get_training_job(
    request: Request,
    job_id: str,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/jobs/{job_id}",
        connection["key"],
        raise_on_error=True,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.get("/api/jobs/{job_id}/logs")
async def get_training_job_logs(
    request: Request,
    job_id: str,
    tail: int = Query(default=200, ge=1, le=5000),
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/jobs/{job_id}/logs",
        connection["key"],
        params={"tail": tail},
        raise_on_error=True,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.post("/api/jobs/{job_id}/approve")
async def approve_training_job(
    request: Request,
    job_id: str,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_admin_user),
):
    connection = get_llamolotl_connection(request, url_idx)
    return await send_post_request(
        url=f"{connection['control_url']}/api/jobs/{job_id}/approve",
        payload="{}",
        stream=False,
        key=connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:write"),
    )


@router.delete("/api/jobs/{job_id}")
async def cancel_training_job(
    request: Request,
    job_id: str,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    return await send_delete_request(
        f"{connection['control_url']}/api/jobs/{job_id}",
        key=connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:write"),
    )


@router.get("/api/outputs")
async def list_training_outputs(
    request: Request,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/models",
        connection["key"],
        raise_on_error=True,
        # This calls control_url's /api/models (list_models), scoped models:read
        # on the validating side — not a jobs:read-scoped /api/outputs endpoint
        # (that endpoint doesn't appear to exist on self.llamolotl; pre-existing
        # behavior, not introduced by this pass — ticket scope matches the
        # actual endpoint being hit).
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "models:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


##########################################
#
# llama.cpp native endpoints (backend only)
#
##########################################


@router.get("/props")
@router.get("/props/{url_idx}")
async def get_props(request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)):
    if url_idx is not None:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    result = await send_get_request(f"{url}/props", key)
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.get("/slots")
@router.get("/slots/{url_idx}")
async def get_slots(
    request: Request,
    url_idx: Optional[int] = None,
    model: Optional[str] = None,
    user=Depends(get_verified_user),
):
    """In-flight generation slots for one model (self.ai#101).

    ``model`` is REQUIRED by the router in router mode — it is multi-process, one
    child ``llama-server`` per model, and /slots has to be addressed to a child.
    This proxy used to drop the parameter, so every call came back
    ``400 model name is missing from the request`` and nothing above the router
    could see whether a model was mid-generation. That is the input the
    drain-before-swap loader needs, so it is forwarded rather than defaulted:
    guessing a model here would report another model's slots as this one's.
    """
    if url_idx is not None:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    target = f"{url}/slots"
    if model:
        target = f"{target}?{urlencode({'model': model})}"
    result = await send_get_request(target, key)
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


async def _busy_slots(url: str, key, model_id: str) -> Optional[dict]:
    """``{"busy": n, "total": n}`` for one loaded model, or ``None`` if the
    router would not say.

    ``None`` is not zero, and the difference decides whether a swap may proceed:
    "no requests in flight" is a green light, "we could not find out" is not.
    Callers must not collapse the two.
    """
    raw = await send_get_request(f"{url}/slots?{urlencode({'model': model_id})}", key)
    if not isinstance(raw, list):
        # An error body ({"error": ...}) or a connection failure. Both mean the
        # same thing here: no answer.
        return None
    return {
        "busy": sum(1 for s in raw if isinstance(s, dict) and s.get("is_processing")),
        "total": len(raw),
    }


@router.get("/residency")
async def get_residency(request: Request, user=Depends(get_verified_user)):
    """What is on the card, what it costs, and what is mid-generation.

    One call, because the proactive loader in self.chat needs all three together
    to answer "can I swap yet?" and an N+1 from the browser would race itself —
    the model list and the slot reads would describe different moments.

    Slots are reduced to counts here rather than proxied. llama.cpp's /slots
    returns the full sampler parameter block per slot (~1.5 KB each); the panel
    needs a number.

    Only LOADED models are polled. An unloaded model has no child process to ask,
    so a request for its slots is a 400 from the router, not a zero.
    """
    models = (await get_all_models(request)).get("data", [])

    if request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
        key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
        loaded = [m for m in models if m.get("status") == "loaded"]
        slot_reads = await asyncio.gather(
            *(_busy_slots(url, key, m["id"]) for m in loaded),
            return_exceptions=True,
        )
        for model, slots in zip(loaded, slot_reads):
            model["slots"] = slots if isinstance(slots, dict) else None

    registered = VramLeases.get(LLAMOLOTL_CONSUMER_ID)
    return {
        "models": models,
        "capacity": {
            "total_bytes": VramLeases.total_capacity(),
            "free_bytes": VramLeases.free_capacity(),
            # What llamolotl could free by evicting its own residents — the term
            # that makes a swap admissible where the gross footprint would not
            # be (#114). The panel shows it so a refusal is legible.
            "llamolotl_held_bytes": effective_held(registered) if registered else 0,
        },
    }


@router.get("/lora-adapters")
@router.get("/lora-adapters/{url_idx}")
async def get_lora_adapters(request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)):
    if url_idx is not None:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    result = await send_get_request(f"{url}/lora-adapters", key)
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


##########################################
#
# Model load / unload
#
##########################################


class ModelActionForm(BaseModel):
    model: str


@router.get("/model-status")
@router.get("/model-status/{url_idx}")
async def get_model_status(request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)):
    """Get fresh model status (loaded/loading/unloaded) bypassing cache."""
    if url_idx is not None:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    result = await send_get_request(f"{url}/v1/models", key)
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")

    status_map = {}
    for model in result.get("data", []):
        model_id = model.get("id", "")
        status_obj = model.get("status", {})
        status_map[model_id] = status_obj.get("value", "unloaded") if isinstance(status_obj, dict) else "unloaded"

    return {"status": status_map}


@router.post("/models/reload-presets")
@router.post("/models/reload-presets/{url_idx}")
async def reload_model_presets(
    request: Request,
    url_idx: Optional[int] = None,
    user=Depends(get_admin_user),
):
    """Re-read llama-server's model presets from disk, without restarting anything.

    The router resolves each model's effective preset — the `[*]` section
    cascaded with that model's own section from `--models-preset`, plus the
    router's CLI args — once at startup, and again only when asked. `GET
    /v1/models?reload=1` is that ask (`server-models.cpp`: `reload` →
    `models.load_models()`). Without it, an edit to the models-preset ConfigMap
    is inert until llama-server restarts, and a restart evicts every resident
    model to change one number.

    A preset reload does NOT touch a running child: it was started with the old
    args and keeps them. To actually move a resident model onto new settings the
    sequence is reload-presets → unload → load. That sequence is what makes a
    VRAM/throughput sweep over `n-cpu-moe` (how many expert layers sit on the
    GPU versus in system RAM) a normal API loop instead of one pod restart per
    data point — see self.ai#40 and self.llamolotl#35.

    Returns the router's model list as re-read, so a caller can confirm the new
    preset actually landed rather than assuming it did. Deliberately bypasses
    `get_all_models()`'s 3s cache: the whole point of this call is to observe a
    change, and a cached answer would hide it.
    """
    if url_idx is None:
        url_idx = 0
    base_urls = request.app.state.config.LLAMOLOTL_BASE_URLS
    if url_idx >= len(base_urls):
        raise HTTPException(status_code=400, detail="Invalid URL index")

    url = base_urls[url_idx]
    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    result = await send_get_request(f"{url}/v1/models?reload=1", key)
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.post("/models/load")
@router.post("/models/load/{url_idx}")
async def load_model(
    request: Request,
    form_data: ModelActionForm,
    url_idx: Optional[int] = None,
    user=Depends(get_admin_user),
):
    if url_idx is not None:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    # Reserve the card BEFORE asking llamolotl to load (self.ai#107). Without
    # this the router allocates against whatever a lower-priority tenant has
    # left it and dies on CUDA OOM, even though llamolotl outranks them and the
    # broker would have reclaimed on request.
    admission = await ensure_llamolotl_vram(request.app.state, form_data.model)
    if admission.denied:
        raise HTTPException(status_code=409, detail=admission.detail)

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    return await send_post_request(
        url=f"{url}/models/load",
        payload=json.dumps({"model": form_data.model}),
        stream=False,
        key=key,
    )


@router.post("/models/unload")
@router.post("/models/unload/{url_idx}")
async def unload_model(
    request: Request,
    form_data: ModelActionForm,
    url_idx: Optional[int] = None,
    user=Depends(get_admin_user),
):
    if url_idx is not None:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    return await send_post_request(
        url=f"{url}/models/unload",
        payload=json.dumps({"model": form_data.model}),
        stream=False,
        key=key,
    )


async def cancel_chat_completion(request: Request, completion_id: str, model: str) -> bool:
    """Explicitly stop an in-flight generation on self.llamolotl (self.ai#39).

    Cancelling our own asyncio task only closes OUR socket, and llama.cpp frees
    the slot on a *poll* that notices the dead connection — so it keeps
    generating in the meantime. `POST /v1/chat/completions/control` with
    `action: "cancel"` is the push equivalent, added in self.llamolotl!31 and
    covered upstream by `test_explicit_cancel_request` (which asserts the slot
    frees within 1s instead of waiting out HTTP_POLLING_SECONDS).

    Note this goes to the **inference** port (LLAMOLOTL_BASE_URLS), not the
    ticket-gated control port (:8093) — llama-server registers this route itself,
    alongside `/v1/chat/completions`, so it takes the same api-key auth as any
    other inference call rather than a service ticket.

    `model` is required in router mode: it selects which child llama-server the
    control request is routed to. Returns True only on an acknowledged cancel.
    Never raises — the caller falls back to the connection-drop path.
    """
    if not request.app.state.config.LLAMOLOTL_BASE_URLS:
        return False

    url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.post(
                f"{url}/v1/chat/completions/control",
                json={"id": completion_id, "action": "cancel", "model": model},
                headers=headers,
            ) as res:
                body = await res.json()
                # An id that already finished matches nothing and comes back
                # success=false. That is the expected outcome of a benign race
                # (generation completed between Stop and this call), not an
                # error — log at debug, not warning.
                success = bool(body.get("success"))
                if not success:
                    log.debug(
                        f"llamolotl declined cancel for completion {completion_id}: "
                        f"{body.get('message', 'no message')}"
                    )
                return success
    except Exception as e:
        log.warning(f"explicit cancel to llamolotl failed for {completion_id}: {e}")
        return False


async def unload_all_llamolotl_models(app_state, url_idx: Optional[int] = None) -> dict:
    """Force-unload every currently loaded/loading model on a llamolotl server.

    Extracted from the ``/models/unload-all`` handler so it can be reused as the
    llamolotl leg of the system-wide GPU e-stop (``routers/vram_leases.py``
    ``/release-all``) WITHOUT going back through HTTP + auth. Takes
    ``app.state`` directly (config only) rather than a ``Request``. Raises
    ``HTTPException`` on no-URLs / connection error exactly as the endpoint did,
    so both callers surface the same failure. Returns
    ``{"unloaded": [...], "errors": [...]}``."""
    if url_idx is not None:
        url = app_state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif app_state.config.LLAMOLOTL_BASE_URLS:
        url = app_state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    key = get_api_key(url, app_state.config.LLAMOLOTL_API_CONFIGS)

    # Get current models and their status
    models_result = await send_get_request(f"{url}/v1/models", key)
    if models_result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")

    unloaded = []
    errors = []
    for model in models_result.get("data", []):
        model_id = model.get("id", "")
        status_obj = model.get("status", {})
        status_val = status_obj.get("value", "unloaded") if isinstance(status_obj, dict) else "unloaded"

        if status_val in ("loaded", "loading"):
            try:
                await send_post_request(
                    url=f"{url}/models/unload",
                    payload=json.dumps({"model": model_id}),
                    stream=False,
                    key=key,
                )
                unloaded.append(model_id)
            except Exception as e:
                errors.append({"model": model_id, "error": str(e)})

    return {"unloaded": unloaded, "errors": errors}


@router.post("/models/unload-all")
@router.post("/models/unload-all/{url_idx}")
async def unload_all_models(
    request: Request,
    url_idx: Optional[int] = None,
    user=Depends(get_admin_user),
):
    return await unload_all_llamolotl_models(request.app.state, url_idx)


##########################################
#
# Heretic endpoints
#
##########################################


class HereticRunForm(BaseModel):
    model_name: str
    quantization: str = "none"
    n_trials: Optional[int] = None
    save_strategy: str = "merge"


@router.post("/api/heretic/run")
async def run_heretic(
    request: Request,
    form_data: HereticRunForm,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    payload = {
        "model_name": form_data.model_name,
        "quantization": form_data.quantization,
        "save_strategy": form_data.save_strategy,
    }
    if form_data.n_trials is not None:
        payload["n_trials"] = form_data.n_trials

    return await send_post_request(
        url=f"{connection['control_url']}/api/heretic/run",
        payload=json.dumps(payload),
        stream=False,
        key=connection["key"],
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:create"),
    )


@router.get("/api/heretic/status/{task_id}")
async def get_heretic_status(
    request: Request,
    task_id: str,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/heretic/status/{task_id}",
        connection["key"],
        raise_on_error=True,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.get("/api/heretic/config")
async def get_heretic_config(
    request: Request,
    url_idx: Optional[int] = Query(default=None),
    user=Depends(get_verified_user),
):
    require_training_access(request, user)
    connection = get_llamolotl_connection(request, url_idx)
    result = await send_get_request(
        f"{connection['control_url']}/api/heretic/config",
        connection["key"],
        raise_on_error=True,
        ticket=mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read"),
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result
