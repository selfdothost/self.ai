import logging
import os
from typing import Optional

import aiohttp
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import AIOHTTP_CLIENT_TIMEOUT, SRC_LOG_LEVELS
from selfai_ui.models.files import Files
from selfai_ui.models.knowledge import KnowledgeFiles, Knowledges
from selfai_ui.models.training import (
    TrainingCourseForm,
    TrainingCourseModel,
    TrainingCourses,
    TrainingCourseUserModel,
    TrainingJobForm,
    TrainingJobModel,
    TrainingJobs,
    TrainingJobStatusUpdate,
    TrainingJobWithDetails,
)
from selfai_ui.utils.access_control import has_access, has_permission
from selfai_ui.utils.auth import get_admin_user, get_verified_user
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

# Reference to the running FastAPI app state — set by gpu_queue at startup.
# Used by _dispatch_scheduled_job to access config (e.g. LLAMOLOTL_CONTROL_BASE_URLS).
_app_state = None

# Same audience string as routers/llamolotl.py — must match self.llamolotl's
# SERVICE_AUTH_AUDIENCE (self.llamolotl#12). This module makes its own direct
# aiohttp calls to the control port instead of going through llamolotl.py's
# send_get_request/send_post_request/send_delete_request helpers, so it mints
# and attaches tickets independently here.
LLAMOLOTL_AUDIENCE = "self.llamolotl"

router = APIRouter()

HF_DATASETS_SERVER = "https://datasets-server.huggingface.co"

# Maps advanced_config["offload"] to the DeepSpeed config path inside the
# training container.
# "none"      → no DeepSpeed (fastest, may OOM on large models)
# "optimizer" → ZeRO-2: optimizer states offloaded to CPU (default)
# "full"      → ZeRO-3: model params + optimizer on CPU (~3-5x slower)
# ZeRO-2 is the default: compatible with bnb 8-bit model quantization,
# uses DeepSpeed's native CPUAdam for optimizer CPU offloading.
_DEEPSPEED_CONFIGS = {
    "optimizer": "/workspace/training/deepspeed_configs/zero2_no_nvcc.json",
    "full": "/workspace/training/deepspeed_configs/zero3_bf16_cpuoffload_all.json",
}


async def _detect_hf_dataset_format(hf_path: str) -> dict:
    """
    Query the HF datasets-server API to auto-detect dataset config keys.

    Returns one of:
      {"type": "chat_template", "field_messages": "<column>"}  — OpenAI role/content format
      {"type": "sharegpt",      "field": "<column>"}           — ShareGPT from/value format
      {"type": "alpaca"}                                       — Alpaca instruction/input/output
      {"type": "completion", "field": "<column>"}              — Single text column

    Falls back to {"type": "chat_template", "field_messages": "messages"} on any error.
    """
    fallback = {"type": "chat_template", "field_messages": "messages"}
    try:
        url = f"{HF_DATASETS_SERVER}/info?dataset={hf_path}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return fallback
                data = await resp.json()

        # Navigate to features — dataset_info may be flat or keyed by config name
        dataset_info = data.get("dataset_info", {})
        features: dict = {}
        if isinstance(dataset_info, dict):
            if "features" in dataset_info:
                features = dataset_info["features"]
            else:
                for v in dataset_info.values():
                    if isinstance(v, dict) and "features" in v:
                        features = v["features"]
                        break

        column_names = set(features.keys())

        # 1. Check for Sequence-typed columns (chat_template or sharegpt)
        for field_name, field_info in features.items():
            if not isinstance(field_info, dict):
                continue
            if field_info.get("_type") != "Sequence":
                continue
            item_keys = set(field_info.get("feature", {}).keys())

            if {"role", "content"}.issubset(item_keys):
                return {"type": "chat_template", "field_messages": field_name}

            if {"from", "value"}.issubset(item_keys):
                return {"type": "sharegpt", "field": field_name}

        # 2. Check for Alpaca format (instruction + output columns)
        if "instruction" in column_names and "output" in column_names:
            return {"type": "alpaca"}

        # 3. Check for completion format (single "text" column)
        if "text" in column_names:
            return {"type": "completion", "field": "text"}

        log.warning(
            "HF dataset format detection: no known format matched for %s (columns: %s)",
            hf_path,
            column_names,
        )
        return fallback
    except Exception as exc:
        log.warning("HF dataset format detection failed for %s: %s", hf_path, exc)
        return fallback


async def _resolve_course_datasets(course_data: dict, control_url: str) -> tuple[list[dict], list[str]]:
    """Resolve a course's dataset_ids to trainer dataset configs.

    Two kinds of dataset-flagged Knowledge:
     - HuggingFace-backed (kb.data.hf_path): reference the HF id directly.
     - Local/curated (curator output, no hf_path): push its JSONL file(s) to
       the trainer node and reference the uploaded local path.

    Returns (datasets, failures) — failures is a list of "<name/id>: <reason>"
    strings for every dataset_id that didn't produce a trainer entry, so a
    caller with an empty `datasets` result can report exactly which
    dataset(s) it looked at and why each one was skipped, instead of a single
    undifferentiated "no valid datasets" message.
    """
    dataset_ids = course_data.get("dataset_ids", [])
    datasets: list[dict] = []
    failures: list[str] = []
    for ds_id in dataset_ids:
        kb = Knowledges.get_knowledge_by_id(ds_id)
        if not kb:
            failures.append(f"{ds_id}: dataset not found (it may have been deleted)")
            continue
        kb_data = kb.data or {}
        if kb_data.get("hf_path"):
            hf_path = kb_data["hf_path"]
            detected = await _detect_hf_dataset_format(hf_path)
            if kb_data.get("type"):
                detected["type"] = kb_data["type"]
            if kb_data.get("field_messages"):
                detected["field_messages"] = kb_data["field_messages"]
            datasets.append({"path": hf_path, **detected})
        else:
            uploaded = await _upload_local_dataset(kb, control_url)
            if not uploaded:
                failures.append(
                    f"{kb.name or kb.id}: no valid local dataset files found "
                    "(expected a curated .json/.jsonl file attached to this Knowledge)"
                )
            datasets.extend(uploaded)
    return datasets, failures


def _no_valid_datasets_detail(dataset_ids: list, failures: list[str]) -> str:
    if not dataset_ids:
        return "Course has no datasets attached — attach at least one Dataset in the course editor."
    return "Course has no valid datasets configured: " + "; ".join(failures)


############################
# Courses
############################


@router.get("/courses", response_model=list[TrainingCourseUserModel])
async def get_courses(request: Request, user=Depends(get_verified_user)):
    if user.role == "admin":
        return TrainingCourses.get_all_courses()
    return TrainingCourses.get_courses_by_user_id(user.id, "read")


@router.get("/courses/list", response_model=list[TrainingCourseUserModel])
async def list_writable_courses(request: Request, user=Depends(get_verified_user)):
    if user.role == "admin":
        return TrainingCourses.get_all_courses()
    return TrainingCourses.get_courses_by_user_id(user.id, "write")


@router.post("/courses/create", response_model=Optional[TrainingCourseModel])
async def create_course(
    request: Request,
    form_data: TrainingCourseForm,
    user=Depends(get_verified_user),
):
    if user.role != "admin" and not has_permission(
        user.id, "studio.training", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    course = TrainingCourses.insert_new_course(user.id, form_data)
    if course:
        return course
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ERROR_MESSAGES.DEFAULT("Failed to create training course"),
    )


@router.get("/courses/{id}", response_model=Optional[TrainingCourseModel])
async def get_course_by_id(id: str, user=Depends(get_verified_user)):
    course = TrainingCourses.get_course_by_id(id=id)
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if user.role != "admin" and course.user_id != user.id and not has_access(user.id, "read", course.access_control):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    return course


@router.post("/courses/{id}/update", response_model=Optional[TrainingCourseModel])
async def update_course_by_id(
    id: str,
    form_data: TrainingCourseForm,
    user=Depends(get_verified_user),
):
    course = TrainingCourses.get_course_by_id(id=id)
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if course.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    updated = TrainingCourses.update_course_by_id(id=id, form_data=form_data)
    if updated:
        return updated
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ERROR_MESSAGES.DEFAULT("Failed to update training course"),
    )


@router.delete("/courses/{id}/delete", response_model=bool)
async def delete_course_by_id(id: str, user=Depends(get_verified_user)):
    course = TrainingCourses.get_course_by_id(id=id)
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if course.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return TrainingCourses.delete_course_by_id(id=id)


############################
# Jobs
############################


@router.get("/jobs", response_model=list[TrainingJobWithDetails])
async def get_jobs(user=Depends(get_verified_user)):
    if user.role == "admin":
        return TrainingJobs.get_all_jobs()
    return TrainingJobs.get_jobs_by_user_id(user.id)


@router.post("/jobs/create", response_model=Optional[TrainingJobWithDetails])
async def create_job(
    request: Request,
    form_data: TrainingJobForm,
    user=Depends(get_verified_user),
):
    # Allow special "heretic" course_id without a real course record
    if form_data.course_id != "heretic":
        # Verify the course exists and user can read it
        course = TrainingCourses.get_course_by_id(id=form_data.course_id)
        if not course:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
        if (
            user.role != "admin"
            and course.user_id != user.id
            and not has_access(user.id, "read", course.access_control)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.UNAUTHORIZED,
            )

    job = TrainingJobs.insert_new_job(user.id, form_data)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Failed to create training job"),
        )

    # Return with full details
    all_jobs = TrainingJobs.get_all_jobs()
    detailed = next((j for j in all_jobs if j.id == job.id), None)
    return detailed or job


@router.get("/jobs/{id}", response_model=Optional[TrainingJobWithDetails])
async def get_job_by_id(id: str, user=Depends(get_verified_user)):
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    all_jobs = TrainingJobs.get_all_jobs()
    return next((j for j in all_jobs if j.id == id), job)


@router.post("/jobs/{id}/cancel", response_model=Optional[TrainingJobModel])
async def cancel_job(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
):
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )
    if job.status == "cancelled":
        # R2-AC1: cancel-on-cancelled is 2xx no-op
        return job
    if job.status not in ("pending", "scheduled", "queued", "running"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job cannot be cancelled in its current state",
        )

    # If the job is running/queued on a llamolotl node, cancel it there too
    if job.llamolotl_job_id and job.llamolotl_url_idx is not None:
        try:
            control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
            if job.llamolotl_url_idx < len(control_urls):
                control_url = control_urls[job.llamolotl_url_idx]
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
                ) as session:
                    await session.delete(
                        f"{control_url}/api/jobs/{job.llamolotl_job_id}",
                        headers={TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:write")},
                    )
        except Exception as e:
            log.warning(f"Failed to cancel llamolotl job {job.llamolotl_job_id}: {e}")

    return TrainingJobs.update_job_status(
        id=id,
        update=TrainingJobStatusUpdate(status="cancelled"),
    )


############################
# Admin-only Job Management
############################


async def _upload_local_dataset(kb, control_url: str) -> list[dict]:
    """Push a curated/local KB's JSONL file(s) to the trainer node and return
    dataset config entries pointing at the uploaded paths.

    Curated datasets (curator output) have no HuggingFace path — their JSONL
    lives in the API's Files storage, which the trainer pod cannot read. We
    upload the content over the control API (POST /api/datasets) and reference
    the local path it returns. train.py loads such existing-on-disk paths via
    load_dataset("json").
    """
    kb_data = kb.data or {}
    ds_type = kb_data.get("type", "completion")
    file_ids = KnowledgeFiles.get_file_ids_by_knowledge_id(kb.id)
    files = Files.get_files_by_ids(file_ids) if file_ids else []

    entries: list[dict] = []
    for f in files:
        path = getattr(f, "path", None)
        if not path or not os.path.exists(path) or not path.endswith((".jsonl", ".json")):
            continue
        try:
            with open(path, "r") as fh:
                content = fh.read()
        except OSError as e:
            log.warning(f"Could not read curated dataset file {path}: {e}")
            continue
        if not content.strip():
            continue

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)) as session:
                async with session.post(
                    f"{control_url}/api/datasets",
                    json={"name": kb.name or kb.id, "content": content},
                    headers={
                        "Content-Type": "application/json",
                        TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:write"),
                    },
                ) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Trainer rejected the dataset upload: {body}",
                        )
                    uploaded = await resp.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to upload curated dataset to trainer: {e}",
            )

        entry = {"path": uploaded["path"], "type": ds_type}
        if kb_data.get("field"):
            entry["field"] = kb_data["field"]
        entries.append(entry)

    return entries


@router.post("/jobs/{id}/approve", response_model=Optional[TrainingJobModel])
async def approve_job(
    request: Request,
    id: str,
    user=Depends(get_admin_user),
):
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status not in ("pending", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending or scheduled jobs can be approved",
        )

    # Determine which llamolotl node to dispatch to (round-robin over available nodes)
    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if not control_urls:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No Llamolotl nodes configured",
        )

    # Pick the first available node (can be made smarter later)
    url_idx = 0
    control_url = control_urls[url_idx]

    # ── Heretic jobs: dispatch to /api/heretic/run instead of training ──
    if job.course_id == "heretic":
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)) as session:
                payload = {"model_name": job.model_id}
                async with session.post(
                    f"{control_url}/api/heretic/run",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:create"),
                    },
                ) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Llamolotl rejected the heretic job: {body}",
                        )
                    heretic_task = await resp.json()
                    llamolotl_job_id = heretic_task.get("task_id")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to dispatch heretic job to Llamolotl: {e}",
            )

        return TrainingJobs.update_job_status(
            id=id,
            update=TrainingJobStatusUpdate(
                status="running",
                llamolotl_job_id=llamolotl_job_id,
                llamolotl_url_idx=url_idx,
            ),
        )

    # ── Standard training jobs ─────────────────────────────────────────
    course = TrainingCourses.get_course_by_id(id=job.course_id)
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Training course not found",
        )

    # Build a complete training config from the course form data
    course_data = course.data or {}
    advanced_config = course_data.get("advanced_config", {})

    dataset_ids = course_data.get("dataset_ids", [])
    datasets, dataset_failures = await _resolve_course_datasets(course_data, control_url)

    if not datasets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_no_valid_datasets_detail(dataset_ids, dataset_failures),
        )

    training_config = {
        "base_model": job.model_id,
        "datasets": datasets,
        "dataset_prepared_path": None,
        "val_set_size": advanced_config.get("val_set_size", 0.05),
        "output_dir": f"./outputs/{course.name.replace(' ', '-').lower()[:40]}",
        "sequence_len": 2048,
        "sample_packing": True,
        "adapter": advanced_config.get("adapter", "lora"),
        "lora_r": advanced_config.get("lora_r", 32),
        "lora_alpha": advanced_config.get("lora_alpha", 16),
        "lora_dropout": advanced_config.get("lora_dropout", 0.05),
        "lora_target_linear": True,
        "load_in_4bit": True,
        "load_in_8bit": False,
        "gradient_accumulation_steps": 4,
        "micro_batch_size": 2,
        "num_epochs": 1,
        "optimizer": "adamw_torch",
        "lr_scheduler": advanced_config.get("lr_scheduler", "cosine"),
        "learning_rate": advanced_config.get("learning_rate", 0.0002),
        "warmup_ratio": advanced_config.get("warmup_ratio", 0.1),
        "weight_decay": advanced_config.get("weight_decay", 0.0),
        "bf16": advanced_config.get("bf16", "auto"),
        "tf32": False,
        "gradient_checkpointing": True,
        "flash_attention": False,
        "deepspeed": _DEEPSPEED_CONFIGS.get(advanced_config.get("offload", "optimizer")),
        "logging_steps": 1,
        "eval_batch_size": 1,
        "evals_per_epoch": advanced_config.get("evals_per_epoch", 4),
        "saves_per_epoch": advanced_config.get("saves_per_epoch", 1),
    }

    config_yaml = yaml.dump(
        {k: v for k, v in training_config.items() if v is not None},
        default_flow_style=False,
    )

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)) as session:
            payload = {
                "config_inline": config_yaml,
            }
            async with session.post(
                f"{control_url}/api/jobs",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:create"),
                },
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Llamolotl rejected the job: {body}",
                    )
                llamolotl_job = await resp.json()
                llamolotl_job_id = llamolotl_job.get("job_id")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to dispatch to Llamolotl: {e}",
        )

    # Approve the job in llamolotl too
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)) as session:
            await session.post(
                f"{control_url}/api/jobs/{llamolotl_job_id}/approve",
                headers={
                    "Content-Type": "application/json",
                    TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:write"),
                },
            )
    except Exception as e:
        log.warning(f"Failed to auto-approve llamolotl job {llamolotl_job_id}: {e}")

    return TrainingJobs.update_job_status(
        id=id,
        update=TrainingJobStatusUpdate(
            status="queued",
            llamolotl_job_id=llamolotl_job_id,
            llamolotl_url_idx=url_idx,
        ),
    )


@router.post("/jobs/{id}/reject", response_model=Optional[TrainingJobModel])
async def reject_job(id: str, user=Depends(get_admin_user)):
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status not in ("pending", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending or scheduled jobs can be rejected",
        )
    return TrainingJobs.update_job_status(
        id=id,
        update=TrainingJobStatusUpdate(
            status="cancelled",
            error_message="Rejected by administrator",
        ),
    )


@router.delete("/jobs/{id}/delete", response_model=bool)
async def delete_job(id: str, user=Depends(get_admin_user)):
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return TrainingJobs.delete_job_by_id(id=id)


############################
# Status sync (called by llamolotl worker or admin poll)
############################


@router.post("/jobs/{id}/sync", response_model=Optional[TrainingJobModel])
async def sync_job_status(
    request: Request,
    id: str,
    user=Depends(get_admin_user),
):
    """Pull the latest status from llamolotl and update the DB record."""
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status in ("completed", "failed", "cancelled"):
        # R3-AC1/AC2: terminal local state is sticky; never rewind from upstream
        return job
    if not job.llamolotl_job_id or job.llamolotl_url_idx is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job has not been dispatched to a Llamolotl node yet",
        )

    control_urls = request.app.state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if job.llamolotl_url_idx >= len(control_urls):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Llamolotl node no longer configured",
        )

    control_url = control_urls[job.llamolotl_url_idx]

    # Heretic jobs use the pipeline task API, not the training jobs API
    if job.course_id == "heretic":
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(
                    f"{control_url}/api/heretic/status/{job.llamolotl_job_id}",
                    headers={TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read")},
                ) as resp:
                    if resp.status == 404:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Heretic task not found",
                        )
                    if resp.status >= 400:
                        # R3-AC4: surface upstream error; don't silently 200
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Llamolotl returned {resp.status}",
                        )
                    heretic_task = await resp.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to reach Llamolotl: {e}",
            )

        ll_status = heretic_task.get("status", "")
        status_map = {
            "running": "running",
            "completed": "completed",
            "failed": "failed",
        }
        new_status = status_map.get(ll_status, job.status)

        return TrainingJobs.update_job_status(
            id=id,
            update=TrainingJobStatusUpdate(
                status=new_status,
                error_message=heretic_task.get("error_message"),
            ),
        )

    # Standard training jobs
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(
                f"{control_url}/api/jobs/{job.llamolotl_job_id}",
                headers={TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read")},
            ) as resp:
                if resp.status == 404:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Llamolotl job not found",
                    )
                if resp.status >= 400:
                    # R3-AC4: surface upstream error; don't silently 200
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Llamolotl returned {resp.status}",
                    )
                llamolotl_job = await resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to reach Llamolotl: {e}",
        )

    ll_status = llamolotl_job.get("status", "")
    status_map = {
        "pending": "queued",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    new_status = status_map.get(ll_status, job.status)

    return TrainingJobs.update_job_status(
        id=id,
        update=TrainingJobStatusUpdate(
            status=new_status,
            error_message=llamolotl_job.get("error_message"),
        ),
    )


############################
# Scheduling
############################


class ScheduleJobForm(BaseModel):
    scheduled_for: int  # Unix timestamp


@router.post("/jobs/{id}/schedule", response_model=Optional[TrainingJobModel])
async def schedule_job(
    id: str,
    form_data: ScheduleJobForm,
    user=Depends(get_admin_user),
):
    """Set or update the scheduled time for a job. The job must be pending or already scheduled."""
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status not in ("pending", "scheduled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending or scheduled jobs can be scheduled",
        )

    import time as _time

    if form_data.scheduled_for < int(_time.time()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Scheduled time must be in the future",
        )

    return TrainingJobs.update_job_scheduled_for(id=id, scheduled_for=form_data.scheduled_for)


@router.post("/jobs/{id}/unschedule", response_model=Optional[TrainingJobModel])
async def unschedule_job(
    id: str,
    user=Depends(get_admin_user),
):
    """Remove the schedule from a job, returning it to pending status."""
    job = TrainingJobs.get_job_by_id(id=id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if job.status != "scheduled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only scheduled jobs can be unscheduled",
        )
    return TrainingJobs.update_job_scheduled_for(id=id, scheduled_for=None)


async def _dispatch_scheduled_job(job: TrainingJobModel) -> None:
    """Dispatch a single scheduled job — mirrors the approve_job logic."""
    global _app_state
    if _app_state is None:
        log.warning("Training scheduler: app state not set, skipping dispatch")
        return

    course = TrainingCourses.get_course_by_id(id=job.course_id)
    if not course:
        TrainingJobs.update_job_status(
            id=job.id,
            update=TrainingJobStatusUpdate(
                status="failed",
                error_message="Training course not found",
            ),
        )
        return

    control_urls = _app_state.config.LLAMOLOTL_CONTROL_BASE_URLS
    if not control_urls:
        TrainingJobs.update_job_status(
            id=job.id,
            update=TrainingJobStatusUpdate(
                status="failed",
                error_message="No Llamolotl nodes configured",
            ),
        )
        return

    url_idx = 0
    control_url = control_urls[url_idx]

    course_data = course.data or {}
    advanced_config = course_data.get("advanced_config", {})

    # Resolve datasets the same way approve_job does (shared helper).
    dataset_ids = course_data.get("dataset_ids", [])
    try:
        datasets, dataset_failures = await _resolve_course_datasets(course_data, control_url)
    except Exception as e:
        TrainingJobs.update_job_status(
            id=job.id,
            update=TrainingJobStatusUpdate(
                status="failed",
                error_message=f"Failed to prepare local dataset: {e}",
            ),
        )
        return

    if not datasets:
        TrainingJobs.update_job_status(
            id=job.id,
            update=TrainingJobStatusUpdate(
                status="failed",
                error_message=_no_valid_datasets_detail(dataset_ids, dataset_failures),
            ),
        )
        return

    training_config = {
        "base_model": job.model_id,
        "datasets": datasets,
        "dataset_prepared_path": None,
        "val_set_size": advanced_config.get("val_set_size", 0.05),
        "output_dir": f"./outputs/{course.name.replace(' ', '-').lower()[:40]}",
        "sequence_len": 2048,
        "sample_packing": True,
        "adapter": advanced_config.get("adapter", "lora"),
        "lora_r": advanced_config.get("lora_r", 32),
        "lora_alpha": advanced_config.get("lora_alpha", 16),
        "lora_dropout": advanced_config.get("lora_dropout", 0.05),
        "lora_target_linear": True,
        "load_in_4bit": True,
        "load_in_8bit": False,
        "gradient_accumulation_steps": 4,
        "micro_batch_size": 2,
        "num_epochs": 1,
        "optimizer": "adamw_torch",
        "lr_scheduler": advanced_config.get("lr_scheduler", "cosine"),
        "learning_rate": advanced_config.get("learning_rate", 0.0002),
        "warmup_ratio": advanced_config.get("warmup_ratio", 0.1),
        "weight_decay": advanced_config.get("weight_decay", 0.0),
        "bf16": advanced_config.get("bf16", "auto"),
        "tf32": False,
        "gradient_checkpointing": True,
        "flash_attention": False,
        "deepspeed": _DEEPSPEED_CONFIGS.get(advanced_config.get("offload", "optimizer")),
        "logging_steps": 1,
        "eval_batch_size": 1,
        "evals_per_epoch": advanced_config.get("evals_per_epoch", 4),
        "saves_per_epoch": advanced_config.get("saves_per_epoch", 1),
    }

    config_yaml = yaml.dump(
        {k: v for k, v in training_config.items() if v is not None},
        default_flow_style=False,
    )

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)) as session:
            payload = {"config_inline": config_yaml}
            async with session.post(
                f"{control_url}/api/jobs",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:create"),
                },
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    TrainingJobs.update_job_status(
                        id=job.id,
                        update=TrainingJobStatusUpdate(
                            status="failed",
                            error_message=f"Llamolotl rejected the job: {body}",
                        ),
                    )
                    return
                llamolotl_job = await resp.json()
                llamolotl_job_id = llamolotl_job.get("job_id")
    except Exception as e:
        TrainingJobs.update_job_status(
            id=job.id,
            update=TrainingJobStatusUpdate(
                status="failed",
                error_message=f"Failed to dispatch to Llamolotl: {e}",
            ),
        )
        return

    # Auto-approve on Llamolotl side
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)) as session:
            await session.post(
                f"{control_url}/api/jobs/{llamolotl_job_id}/approve",
                headers={
                    "Content-Type": "application/json",
                    TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:write"),
                },
            )
    except Exception as e:
        log.warning(f"Failed to auto-approve llamolotl job {llamolotl_job_id}: {e}")

    # Mark running (not queued) once handed to the trainer: it now occupies the
    # window's training slot. Leaving it "queued" made the window dispatcher
    # re-select it every tick (and _running_count never saw it), which spawned a
    # duplicate llamolotl job each cycle. sync_job_status reconciles to
    # completed/failed from llamolotl.
    TrainingJobs.update_job_status(
        id=job.id,
        update=TrainingJobStatusUpdate(
            status="running",
            llamolotl_job_id=llamolotl_job_id,
            llamolotl_url_idx=url_idx,
        ),
    )
    log.info(f"Scheduled training job {job.id} dispatched as llamolotl job {llamolotl_job_id}")

    # Standalone process_training_schedule removed — the unified
    # gpu_queue.process_gpu_queue() handles both training and eval jobs.
