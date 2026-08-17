import logging
from typing import Optional

import aiohttp
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from pydantic import BaseModel

from selfai_ui.env import (
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST,
    SRC_LOG_LEVELS,
)
from selfai_ui.models.curator_jobs import (
    CuratorJobForm,
    CuratorJobModel,
    CuratorJobs,
)
from selfai_ui.utils.auth import get_admin_user, get_verified_user
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("CURATOR", logging.INFO))

# Audience string self.curator's control API (:8094) validates tickets
# against — must match SERVICE_AUTH_AUDIENCE on that side (self.curator#5 /
# self.ai#25). Same constant as gpu_queue.py's CURATOR_AUDIENCE — keep both
# in sync.
CURATOR_AUDIENCE = "self.curator"


##########################################
#
# Utility functions
#
##########################################


async def send_get_request(url, raise_on_error=False, ticket=None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                headers={**({TICKET_HEADER: ticket} if ticket else {})},
            ) as response:
                result = await response.json()
                if raise_on_error and response.status >= 400:
                    detail = None
                    if isinstance(result, dict):
                        detail = result.get("detail") or result.get("error")
                    raise HTTPException(
                        status_code=response.status,
                        detail=detail or "Self.AI UI: Curator Connection Error",
                    )
                return result
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Curator connection error: {e}")
        if raise_on_error:
            raise HTTPException(status_code=500, detail="Self.AI UI: Curator Connection Error")
        return None


async def send_post_request(url, payload, raise_on_error=False, ticket=None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
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
                        detail=detail or "Self.AI UI: Curator Connection Error",
                    )
                return result
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Curator connection error: {e}")
        if raise_on_error:
            raise HTTPException(status_code=500, detail="Self.AI UI: Curator Connection Error")
        return None


async def send_delete_request(url, raise_on_error=False, ticket=None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.delete(
                url,
                headers={**({TICKET_HEADER: ticket} if ticket else {})},
            ) as response:
                result = await response.json()
                if raise_on_error and response.status >= 400:
                    detail = None
                    if isinstance(result, dict):
                        detail = result.get("detail") or result.get("error")
                    raise HTTPException(
                        status_code=response.status,
                        detail=detail or "Self.AI UI: Curator Connection Error",
                    )
                return result
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Curator connection error: {e}")
        if raise_on_error:
            raise HTTPException(status_code=500, detail="Self.AI UI: Curator Connection Error")
        return None


def get_curator_url(request: Request, url_idx: Optional[int] = None) -> str:
    """Get the curator base URL for the given index."""
    base_urls = request.app.state.config.CURATOR_BASE_URLS

    if not base_urls:
        raise HTTPException(status_code=500, detail="No Curator URLs configured")

    idx = url_idx if url_idx is not None else 0
    if idx >= len(base_urls):
        raise HTTPException(status_code=400, detail="Invalid Curator URL index")

    return base_urls[idx].rstrip("/")


##########################################
#
# Router
#
##########################################

router = APIRouter()


##########################################
# Connection verification
##########################################


class ConnectionVerificationForm(BaseModel):
    url: str
    key: str = ""


@router.post("/verify")
async def verify_connection(form_data: ConnectionVerificationForm, user=Depends(get_admin_user)):
    url = form_data.url.rstrip("/")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    ) as session:
        try:
            async with session.get(f"{url}/health") as r:
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
            raise HTTPException(status_code=500, detail="Self.AI UI: Curator Connection Error")
        except Exception as e:
            log.exception(f"Connection error: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Self.AI UI: {str(e)}",
            )


##########################################
# Configuration
##########################################


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_CURATOR_API": request.app.state.config.ENABLE_CURATOR_API,
        "CURATOR_BASE_URLS": request.app.state.config.CURATOR_BASE_URLS,
        "CURATOR_API_CONFIGS": request.app.state.config.CURATOR_API_CONFIGS,
    }


class CuratorConfigForm(BaseModel):
    ENABLE_CURATOR_API: Optional[bool] = None
    CURATOR_BASE_URLS: list[str]
    CURATOR_API_CONFIGS: dict


@router.post("/config/update")
async def update_config(request: Request, form_data: CuratorConfigForm, user=Depends(get_admin_user)):
    request.app.state.config.ENABLE_CURATOR_API = form_data.ENABLE_CURATOR_API
    request.app.state.config.CURATOR_BASE_URLS = form_data.CURATOR_BASE_URLS
    request.app.state.config.CURATOR_API_CONFIGS = form_data.CURATOR_API_CONFIGS

    # Remove any extra configs
    config_urls = request.app.state.config.CURATOR_API_CONFIGS.keys()
    for url in list(request.app.state.config.CURATOR_BASE_URLS):
        if url not in config_urls:
            request.app.state.config.CURATOR_API_CONFIGS.pop(url, None)

    return {
        "ENABLE_CURATOR_API": request.app.state.config.ENABLE_CURATOR_API,
        "CURATOR_BASE_URLS": request.app.state.config.CURATOR_BASE_URLS,
        "CURATOR_API_CONFIGS": request.app.state.config.CURATOR_API_CONFIGS,
    }


##########################################
# Proxy: Text stages
##########################################


@router.get("/api/text")
async def list_text_categories(request: Request, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/text",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "stages:read"),
    )
    return result


@router.get("/api/text/custom/stages")
async def list_custom_stages(request: Request, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/text/custom/stages",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "stages:read"),
    )
    return result


@router.get("/api/text/custom/stages/{stage_uuid}")
async def get_custom_stage(request: Request, stage_uuid: str, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/text/custom/stages/{stage_uuid}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "stages:read"),
    )
    return result


@router.post("/api/text/custom/stages")
async def create_custom_stage(request: Request, user=Depends(get_admin_user)):
    url = get_curator_url(request)
    body = await request.body()
    result = await send_post_request(
        f"{url}/api/text/custom/stages",
        body.decode(),
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "stages:write"),
    )
    return result


@router.delete("/api/text/custom/stages/{stage_uuid}")
async def delete_custom_stage(request: Request, stage_uuid: str, user=Depends(get_admin_user)):
    url = get_curator_url(request)
    result = await send_delete_request(
        f"{url}/api/text/custom/stages/{stage_uuid}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "stages:write"),
    )
    return result


@router.get("/api/text/{category}/stages")
async def list_category_stages(request: Request, category: str, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/text/{category}/stages",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "stages:read"),
    )
    return result


@router.get("/api/text/{category}/stages/{stage_id}")
async def get_stage_detail(request: Request, category: str, stage_id: str, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/text/{category}/stages/{stage_id}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "stages:read"),
    )
    return result


##########################################
# Queue: create a CuratorJob DB record
# for the GPU-queue daemon to dispatch
##########################################


class QueueCuratorJobForm(BaseModel):
    pipeline_id: str
    pipeline_config: dict  # full config blob sent to curator at dispatch time
    scheduled_for: Optional[int] = None
    priority: str = "normal"
    dataset_name: Optional[str] = None


# Priority tiers the queue understands (utils/gpu_queue.py). `run_now` bypasses
# the GPU window entirely, so it is admin-only; the rest respect windows and
# only order jobs within them.
VALID_JOB_PRIORITIES = frozenset({"normal", "high", "run_now"})
WINDOW_BYPASSING_PRIORITIES = frozenset({"run_now"})


@router.post("/queue", response_model=Optional[CuratorJobModel])
async def queue_curator_job(
    form_data: QueueCuratorJobForm,
    user=Depends(get_verified_user),
):
    """Create a CuratorJob record. The GPU-queue daemon dispatches it
    to the curator container when a window is active.

    ``priority`` is validated here rather than taken as sent (self.ai#88).
    ``_dispatch_run_now_jobs`` dispatches ``run_now`` jobs "immediately,
    bypassing windows" — so an unvalidated, caller-supplied priority let ANY
    verified user put a curation run on the shared 4090 outside every window the
    admin had scheduled, simply by asking for it. Choosing to override the GPU
    schedule is an operator decision, so ``run_now`` requires an admin; an
    unrecognised value is refused outright rather than silently coerced, since
    silently downgrading a caller's stated intent is its own surprise."""
    priority = (form_data.priority or "normal").strip()
    if priority not in VALID_JOB_PRIORITIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown priority {priority!r}; expected one of "
                f"{sorted(VALID_JOB_PRIORITIES)}"
            ),
        )
    if priority in WINDOW_BYPASSING_PRIORITIES and user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                f"priority {priority!r} bypasses the GPU job window and is "
                "admin-only; queue at 'normal' or 'high' to run in the next "
                "curator window"
            ),
        )
    form_data = form_data.model_copy(update={"priority": priority})

    job = CuratorJobs.insert_new_job(
        user_id=user.id,
        form_data=CuratorJobForm(
            pipeline_id=form_data.pipeline_id,
            scheduled_for=form_data.scheduled_for,
            priority=form_data.priority,
            dataset_name=form_data.dataset_name,
        ),
    )
    if not job:
        raise HTTPException(status_code=400, detail="Failed to create curator job")

    # Store the full pipeline config so the daemon can POST it to curator later
    CuratorJobs.update_job_meta(job.id, {"pipeline_config": form_data.pipeline_config})

    # Re-fetch to include the meta
    job = CuratorJobs.get_job_by_id(job.id)
    return job


##########################################
# Proxy: Jobs
##########################################


def require_curator_window(action: str) -> None:
    """Refuse a direct-to-curator action unless a curator GPU window is open.

    The window-respecting path is ``POST /curator/queue``: it creates a
    CuratorJob row the daemon dispatches inside a window, under an exclusive VRAM
    lease, and finalizes into a dataset when it completes. These raw proxies
    bypass ALL of that — a run started here is invisible to the queue, holds no
    lease, and produces no dataset, while occupying the same single 4090 as
    inference (self.ai#88).

    So the proxies are kept for operator use but bounded by the same schedule
    everything else obeys. Fails CLOSED on an unreadable window table: an unknown
    schedule must not become an open door to the GPU."""
    from selfai_ui.models.job_windows import JobWindows

    try:
        window = JobWindows.get_active_window()
    except Exception as e:
        log.warning("curator: window check failed (%r); refusing %s", e, action)
        raise HTTPException(
            status_code=503,
            detail="GPU window status unknown — refusing to start curation work",
        )

    if window is not None and any(
        slot.job_type == "curator" for slot in (window.slots or [])
    ):
        return

    raise HTTPException(
        status_code=409,
        detail=(
            f"No curator GPU window is currently open, so {action} is refused. "
            "Queue the pipeline via /curator/queue to run it in the next window, "
            "or open a window in Admin > Jobs."
        ),
    )


@router.post("/api/jobs")
async def create_job(request: Request, user=Depends(get_admin_user)):
    """Direct-to-curator job creation. Admin-only (self.ai#88): this is the
    legacy path the daemon itself uses, and the self.chat client marks it as
    such — the app queues via /curator/queue. Creating a job does not start it
    (self.curator creates them PENDING), so this is not window-gated; ``approve``
    is."""
    url = get_curator_url(request)
    body = await request.body()
    result = await send_post_request(
        f"{url}/api/jobs",
        body.decode(),
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:create"),
    )
    return result


@router.get("/api/jobs")
async def list_jobs(request: Request, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/jobs",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:read"),
    )
    return result


@router.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/jobs/{job_id}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:read"),
    )
    return result


@router.get("/api/jobs/{job_id}/logs")
async def get_job_logs(request: Request, job_id: str, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/jobs/{job_id}/logs",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:read"),
    )
    return result


@router.delete("/api/jobs/{job_id}")
async def cancel_job(request: Request, job_id: str, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_delete_request(
        f"{url}/api/jobs/{job_id}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:write"),
    )
    return result


@router.post("/api/jobs/{job_id}/schedule")
async def schedule_job(request: Request, job_id: str, user=Depends(get_admin_user)):
    """Admin-only (self.ai#88): scheduling work onto the shared 4090 is an
    operator decision. Note self.curator never auto-starts a scheduled job on its
    own — its poll loop deliberately leaves dispatch to this daemon — so this
    only records an intent."""
    url = get_curator_url(request)
    body = await request.body()
    return await send_post_request(
        f"{url}/api/jobs/{job_id}/schedule",
        body.decode(),
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:write"),
    )


@router.post("/api/jobs/{job_id}/unschedule")
async def unschedule_job(request: Request, job_id: str, user=Depends(get_admin_user)):
    url = get_curator_url(request)
    return await send_post_request(
        f"{url}/api/jobs/{job_id}/unschedule",
        "{}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:write"),
    )


@router.post("/api/jobs/{job_id}/approve")
async def approve_job(request: Request, job_id: str, user=Depends(get_admin_user)):
    """Start a curation run on the shared 4090, right now.

    This is the sharpest of the proxies: it is the ONE call that puts load on the
    GPU without going through the queue at all. It used to be reachable by any
    verified user with no window check whatsoever — the widest of the three
    window bypasses self.ai#88 found. Now admin-only AND gated on an open curator
    window, so the admin's schedule bounds every path to the card, not just the
    tidy one."""
    require_curator_window("starting a curation job directly")
    url = get_curator_url(request)
    return await send_post_request(
        f"{url}/api/jobs/{job_id}/approve",
        "{}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:write"),
    )


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job_post(request: Request, job_id: str, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    return await send_post_request(
        f"{url}/api/jobs/{job_id}/cancel",
        "{}",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "jobs:write"),
    )


##########################################
# Proxy: Data
##########################################


@router.get("/api/data")
async def list_data(request: Request, user=Depends(get_verified_user)):
    url = get_curator_url(request)
    result = await send_get_request(
        f"{url}/api/data",
        raise_on_error=True,
        ticket=mint_service_ticket(CURATOR_AUDIENCE, "data:read"),
    )
    return result
