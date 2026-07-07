import asyncio
import json
import logging
from pathlib import Path
import random
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiocache import cached

from fastapi import (
    Depends,
    HTTPException,
    Query,
    Request,
    APIRouter,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from selfai_ui.models.models import ModelForm, ModelMeta, ModelParams, Models
from selfai_ui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_model_system_prompt_to_body,
)
from selfai_ui.utils.auth import get_admin_user, get_verified_user
from selfai_ui.utils.access_control import has_access, has_permission

from selfai_ui.env import (
    SRC_LOG_LEVELS,
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST,
    BYPASS_MODEL_ACCESS_CONTROL,
)
from selfai_ui.constants import ERROR_MESSAGES

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

    if has_permission(
        user.id, "workspace.training", request.app.state.config.USER_PERMISSIONS
    ):
        return

    raise HTTPException(status_code=401, detail=ERROR_MESSAGES.UNAUTHORIZED)


async def send_get_request(url, key=None, params=None, raise_on_error=False):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                params=params,
                headers={**({"Authorization": f"Bearer {key}"} if key else {})},
            ) as response:
                result = await response.json()
                if raise_on_error and response.status >= 400:
                    detail = None
                    if isinstance(result, dict):
                        detail = result.get("detail") or result.get("error")
                    raise HTTPException(
                        status_code=response.status,
                        detail=detail
                        if detail
                        else "Self.AI UI: Server Connection Error",
                    )
                return result
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Connection error: {e}")
        if raise_on_error:
            raise HTTPException(
                status_code=500, detail="Self.AI UI: Server Connection Error"
            )
        return None


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


async def send_post_request(
    url: str,
    payload: str,
    stream: bool = True,
    key: Optional[str] = None,
):
    r = None
    try:
        session = aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        )

        r = await session.post(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {key}"} if key else {}),
            },
        )
        r.raise_for_status()

        if stream:
            response_headers = dict(r.headers)
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=response_headers,
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            res = await r.json()
            await cleanup_response(r, session)
            return res

    except Exception as e:
        detail = None

        if r is not None:
            try:
                res = await r.json()
                if "error" in res:
                    detail = f"Llamolotl: {res.get('error', 'Unknown error')}"
            except Exception:
                detail = f"Llamolotl: {e}"

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Self.AI UI: Server Connection Error",
        )


def get_api_key(url, configs):
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return configs.get(base_url, {}).get("key", None)


async def send_delete_request(url: str, key: Optional[str] = None):
    r = None
    try:
        async with aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        ) as session:
            r = await session.delete(
                url,
                headers={**({"Authorization": f"Bearer {key}"} if key else {})},
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
async def verify_connection(
    form_data: ConnectionVerificationForm, user=Depends(get_admin_user)
):
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
            raise HTTPException(
                status_code=500, detail="Self.AI UI: Server Connection Error"
            )
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
async def update_config(
    request: Request, form_data: LlamolotlConfigForm, user=Depends(get_admin_user)
):
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


@cached(ttl=3)
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
                        models.append({
                            "id": model_id,
                            "name": model_id,
                            "object": "model",
                            "created": model.get("created", 0),
                            "owned_by": "llamolotl",
                            "urls": [idx],
                            "status": status_val,
                        })
                    else:
                        for m in models:
                            if m["id"] == model_id:
                                m["urls"].append(idx)
                                break

        result = {"data": models}
    else:
        result = {"data": []}

    request.app.state.LLAMOLOTL_MODELS = {
        model["id"]: model for model in result["data"]
    }
    return result


@router.get("/models")
@router.get("/models/{url_idx}")
async def get_llamolotl_models(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
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
        meta = model_info.meta.model_dump() if hasattr(model_info.meta, 'model_dump') else model_info.meta
        desired_loras = meta.get("active_loras") or []

    # Normalize for comparison: sort by file name
    desired_sorted = sorted(desired_loras, key=lambda l: l.get("file", ""))
    current_sorted = sorted(_current_applied_loras or [], key=lambda l: l.get("file", ""))

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
                or has_access(
                    user.id, type="read", access_control=model_info.access_control
                )
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
                or has_access(
                    user.id, type="read", access_control=model_info.access_control
                )
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
    )


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

    result = await send_get_request(f"{control_url}/api/models/available", key)
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


class HFModelRegisterForm(BaseModel):
    name: str


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
    )

    # Fetch metadata from llamolotl and persist lineage in the UI Model table
    try:
        available = await send_get_request(f"{control_url}/api/models/available", key)
        lineage = {}
        if available:
            for m in (available if isinstance(available, list) else []):
                if Path(m.get("name", "")).name == Path(form_data.name).name:
                    lineage = {
                        k: m.get(k)
                        for k in ("hf_repo", "quant", "source_type", "trainable", "pulled_at", "bake_info")
                        if m.get(k) is not None
                    }
                    break

        model_id = Path(form_data.name).name
        existing = Models.get_model_by_id(model_id)
        if existing:
            updated_meta = {**existing.meta.model_dump(), **lineage}
            Models.update_model_by_id(model_id, ModelForm(
                id=model_id,
                base_model_id=existing.base_model_id,
                name=existing.name,
                meta=ModelMeta(**updated_meta),
                params=existing.params,
                access_control=existing.access_control,
                is_active=existing.is_active,
            ))
        else:
            Models.insert_new_model(ModelForm(
                id=model_id,
                name=model_id,
                meta=ModelMeta(**lineage),
                params=ModelParams(),
                is_active=True,
            ), user.id)
    except Exception as e:
        log.warning(f"Failed to persist model lineage for {form_data.name}: {e}")

    return result


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
    )

    # Persist active LoRAs in the Model's meta
    try:
        model = Models.get_model_by_id(form_data.model_id)
        if model:
            updated_meta = {**model.meta.model_dump(), "active_loras": form_data.loras}
            Models.update_model_by_id(form_data.model_id, ModelForm(
                id=form_data.model_id,
                base_model_id=model.base_model_id,
                name=model.name,
                meta=ModelMeta(**updated_meta),
                params=model.params,
                access_control=model.access_control,
                is_active=model.is_active,
            ))
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
    payload = {
        "config_path": normalize_training_config_identifier(form_data.config_path)
    }
    if form_data.base_model:
        payload["base_model"] = form_data.base_model
    if form_data.output_dir:
        payload["overrides"] = {"output_dir": form_data.output_dir}

    return await send_post_request(
        url=f"{connection['control_url']}/api/jobs",
        payload=json.dumps(payload),
        stream=False,
        key=connection["key"],
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
async def get_props(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
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
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
    if url_idx is not None:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[url_idx]
    elif request.app.state.config.LLAMOLOTL_BASE_URLS:
        url = request.app.state.config.LLAMOLOTL_BASE_URLS[0]
    else:
        raise HTTPException(status_code=500, detail="No Llamolotl URLs configured")

    key = get_api_key(url, request.app.state.config.LLAMOLOTL_API_CONFIGS)
    result = await send_get_request(f"{url}/slots", key)
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result


@router.get("/lora-adapters")
@router.get("/lora-adapters/{url_idx}")
async def get_lora_adapters(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
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
async def get_model_status(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
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


@router.post("/models/unload-all")
@router.post("/models/unload-all/{url_idx}")
async def unload_all_models(
    request: Request,
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
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Self.AI UI: Server Connection Error")
    return result
