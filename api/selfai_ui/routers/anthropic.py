import asyncio
import json
import logging
from typing import Optional

import aiohttp
from aiocache import cached
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
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
from selfai_ui.models.models import Models
from selfai_ui.utils.access_control import has_access
from selfai_ui.utils.auth import get_admin_user, get_verified_user
from selfai_ui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_model_system_prompt_to_body,
    convert_payload_openai_to_anthropic,
)
from selfai_ui.utils.response import (
    convert_response_anthropic_to_openai,
    convert_streaming_response_anthropic_to_openai,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["ANTHROPIC"])

# Pinned per Anthropic's versioning scheme -- this is a date-stamped API contract,
# not a model version, and it does not track model releases.
ANTHROPIC_API_VERSION = "2023-06-01"

# Fallback only. The real value comes from the model's own max_tokens in the live
# model list (self.ai#59 Phase 0 decision 2); this is what we send when a model
# predates the Models API exposing max_tokens.
DEFAULT_MAX_TOKENS = 4096


##########################################
#
# Utility functions
#
##########################################


def get_api_key(url: str, configs: dict) -> Optional[str]:
    """Anthropic keys live inside the per-URL config bag, not a parallel keys array."""
    return (configs.get(url) or {}).get("key")


def anthropic_headers(key: Optional[str]) -> dict:
    return {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_API_VERSION,
        **({"x-api-key": key} if key else {}),
    }


async def send_get_request(url: str, key: Optional[str] = None, raise_on_error: bool = False):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers=anthropic_headers(key)) as response:
                result = await response.json()
                if raise_on_error and response.status >= 400:
                    detail = None
                    if isinstance(result, dict):
                        error = result.get("error")
                        detail = error.get("message") if isinstance(error, dict) else error
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


async def send_post_request(url: str, payload: str, stream: bool, key: Optional[str] = None):
    r = None
    session = None
    try:
        session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT))
        r = await session.post(url, data=payload, headers=anthropic_headers(key))
        r.raise_for_status()

        if stream:
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers={"Content-Type": "text/event-stream"},
                background=BackgroundTask(cleanup_response, response=r, session=session),
            )

        res = await r.json()
        await cleanup_response(r, session)
        return res

    except Exception as e:
        detail = None
        if r is not None:
            try:
                res = await r.json()
                error = res.get("error")
                if isinstance(error, dict):
                    detail = f"Anthropic: {error.get('message', 'Unknown error')}"
                elif error:
                    detail = f"Anthropic: {error}"
            except Exception:
                detail = f"Anthropic: {e}"

        status = r.status if r is not None else 500
        await cleanup_response(r, session)
        raise HTTPException(
            status_code=status,
            detail=detail if detail else "Self.AI UI: Server Connection Error",
        )


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
    url = form_data.url.rstrip("/")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    ) as session:
        try:
            async with session.get(f"{url}/v1/models", headers=anthropic_headers(form_data.key)) as r:
                if r.status != 200:
                    detail = f"HTTP Error: {r.status}"
                    try:
                        res = await r.json()
                        error = res.get("error")
                        if isinstance(error, dict):
                            detail = f"External Error: {error.get('message', error)}"
                        elif error:
                            detail = f"External Error: {error}"
                    except Exception:
                        pass
                    raise Exception(detail)

                return await r.json()
        except aiohttp.ClientError as e:
            log.exception(f"Client error: {str(e)}")
            raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_ANTHROPIC_API": request.app.state.config.ENABLE_ANTHROPIC_API,
        "ANTHROPIC_BASE_URLS": request.app.state.config.ANTHROPIC_BASE_URLS,
        "ANTHROPIC_API_CONFIGS": request.app.state.config.ANTHROPIC_API_CONFIGS,
    }


class AnthropicConfigForm(BaseModel):
    ENABLE_ANTHROPIC_API: Optional[bool] = None
    ANTHROPIC_BASE_URLS: list[str]
    ANTHROPIC_API_CONFIGS: dict


@router.post("/config/update")
async def update_config(request: Request, form_data: AnthropicConfigForm, user=Depends(get_admin_user)):
    request.app.state.config.ENABLE_ANTHROPIC_API = form_data.ENABLE_ANTHROPIC_API
    request.app.state.config.ANTHROPIC_BASE_URLS = [url.rstrip("/") for url in form_data.ANTHROPIC_BASE_URLS]
    request.app.state.config.ANTHROPIC_API_CONFIGS = form_data.ANTHROPIC_API_CONFIGS

    # Drop configs for URLs that are no longer configured.
    configured_urls = set(request.app.state.config.ANTHROPIC_BASE_URLS)
    for url in list(request.app.state.config.ANTHROPIC_API_CONFIGS.keys()):
        if url not in configured_urls:
            request.app.state.config.ANTHROPIC_API_CONFIGS.pop(url, None)

    return {
        "ENABLE_ANTHROPIC_API": request.app.state.config.ENABLE_ANTHROPIC_API,
        "ANTHROPIC_BASE_URLS": request.app.state.config.ANTHROPIC_BASE_URLS,
        "ANTHROPIC_API_CONFIGS": request.app.state.config.ANTHROPIC_API_CONFIGS,
    }


##########################################
#
# Model discovery
#
##########################################


def _supports_adaptive_thinking(model: dict) -> bool:
    capabilities = model.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    thinking = capabilities.get("thinking")
    if not isinstance(thinking, dict):
        return False
    adaptive = (thinking.get("types") or {}).get("adaptive")
    return bool(isinstance(adaptive, dict) and adaptive.get("supported"))


# key_builder ignores `request` for the same reason as openai.py/ollama.py/llamolotl.py:
# aiocache's default builder stringifies every positional arg, and a fresh Starlette
# Request per call has no stable repr, so the ttl cache never hits. The response depends
# only on global app.state.config, so a fixed key is correct.
@cached(ttl=3, key_builder=lambda f, *args, **kwargs: "anthropic:get_all_models")
async def get_all_models(request: Request):
    log.info("get_all_models()")

    if not request.app.state.config.ENABLE_ANTHROPIC_API:
        request.app.state.ANTHROPIC_MODELS = {}
        return {"data": []}

    base_urls = request.app.state.config.ANTHROPIC_BASE_URLS
    configs = request.app.state.config.ANTHROPIC_API_CONFIGS

    request_tasks = []
    for url in base_urls:
        api_config = configs.get(url, {})
        if api_config.get("enable", True):
            request_tasks.append(send_get_request(f"{url}/v1/models", api_config.get("key")))
        else:
            request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

    responses = await asyncio.gather(*request_tasks)

    models = []
    for idx, response in enumerate(responses):
        if not response or "data" not in response:
            continue

        url = base_urls[idx]
        api_config = configs.get(url, {})
        prefix_id = api_config.get("prefix_id")
        model_ids = api_config.get("model_ids", [])

        for model in response["data"]:
            model_id = model.get("id", "")
            if not model_id:
                continue
            if model_ids and model_id not in model_ids:
                continue

            # The upstream id is what we send back to Anthropic; model_id may carry a
            # prefix for display and must be stripped again on dispatch.
            upstream_id = model_id
            if prefix_id:
                model_id = f"{prefix_id}.{model_id}"

            existing = next((m for m in models if m["id"] == model_id), None)
            if existing:
                existing["urls"].append(idx)
                continue

            models.append(
                {
                    "id": model_id,
                    "name": model.get("display_name") or model_id,
                    "object": "model",
                    "created": model.get("created_at", 0),
                    "owned_by": "anthropic",
                    "urls": [idx],
                    # Cached off the live list so convert_payload_openai_to_anthropic can
                    # fill in Anthropic's required max_tokens without a magic constant,
                    # and so we only ask for thinking on models that support it.
                    "anthropic": {
                        "upstream_id": upstream_id,
                        "max_tokens": model.get("max_tokens") or DEFAULT_MAX_TOKENS,
                        "max_input_tokens": model.get("max_input_tokens"),
                        "supports_adaptive_thinking": _supports_adaptive_thinking(model),
                    },
                }
            )

    result = {"data": models}
    request.app.state.ANTHROPIC_MODELS = {model["id"]: model for model in models}
    return result


@router.get("/models")
@router.get("/models/{url_idx}")
async def get_anthropic_models(request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)):
    if url_idx is None:
        return await get_all_models(request)

    try:
        url = request.app.state.config.ANTHROPIC_BASE_URLS[url_idx]
    except IndexError:
        raise HTTPException(status_code=400, detail="Invalid URL index")

    models = await send_get_request(
        f"{url}/v1/models",
        get_api_key(url, request.app.state.config.ANTHROPIC_API_CONFIGS),
    )
    if models is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")

    return models


##########################################
#
# Chat completion
#
##########################################


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
    payload.pop("metadata", None)

    model_id = payload.get("model")
    model_info = Models.get_model_by_id(model_id)

    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id
            model_id = model_info.base_model_id

        params = model_info.params.model_dump()
        payload = apply_model_params_to_body_openai(params, payload)
        payload = apply_model_system_prompt_to_body(params, payload, user)

        if not bypass_filter and user.role == "user":
            if not (
                user.id == model_info.user_id
                or has_access(user.id, type="read", access_control=model_info.access_control)
            ):
                raise HTTPException(status_code=403, detail="Model not found")
    elif not bypass_filter:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Model not found")

    # Model list is populated by get_all_models(); a cold app.state means nobody has
    # listed models yet this process, so prime it rather than 400 on a valid model.
    if model_id not in (request.app.state.ANTHROPIC_MODELS or {}):
        await get_all_models(request)

    model = (request.app.state.ANTHROPIC_MODELS or {}).get(model_id)
    if not model:
        raise HTTPException(status_code=400, detail=ERROR_MESSAGES.MODEL_NOT_FOUND(model_id))

    url_idx = model.get("urls", [0])[0]
    url = request.app.state.config.ANTHROPIC_BASE_URLS[url_idx]
    key = get_api_key(url, request.app.state.config.ANTHROPIC_API_CONFIGS)

    metadata = model.get("anthropic", {})
    payload["model"] = metadata.get("upstream_id", model_id)

    stream = payload.get("stream", False)
    anthropic_payload = convert_payload_openai_to_anthropic(
        payload,
        default_max_tokens=metadata.get("max_tokens") or DEFAULT_MAX_TOKENS,
        supports_adaptive_thinking=metadata.get("supports_adaptive_thinking", False),
    )

    response = await send_post_request(
        url=f"{url}/v1/messages",
        payload=json.dumps(anthropic_payload),
        stream=stream,
        key=key,
    )

    if stream:
        return StreamingResponse(
            convert_streaming_response_anthropic_to_openai(response),
            headers={"Content-Type": "text/event-stream"},
            background=response.background,
        )

    return convert_response_anthropic_to_openai(response)


@router.post("/completions")
async def generate_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    bypass_filter: Optional[bool] = False,
):
    """Anthropic has no text-completions endpoint; adapt the prompt into a single user turn."""
    payload = {**form_data}
    prompt = payload.pop("prompt", "")
    payload["messages"] = [{"role": "user", "content": prompt}]
    return await generate_chat_completion(request, payload, user, bypass_filter)
