"""GPU job queue v2 — window-aware, multi-node-safe dispatcher.

Replaces the original process_gpu_queue with a RedisLock-protected loop that:
  1. Syncs running job status from remote workers
  2. Promotes due scheduled jobs -> queued
  3. Dispatches run_now jobs immediately (bypasses windows)
  4. If an active window exists, dispatches jobs in priority/preference order

Priority tiers:
  run_now  - bypass windows, admin-created, dispatched unconditionally
  high     - respect windows, dispatched before all normal jobs across all types
  normal   - respect windows, dispatched in preferred-type order then FIFO

Only one UI instance runs dispatch per cycle (RedisLock).
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Optional

import httpx

from selfai_ui.env import SRC_LOG_LEVELS, WEBSOCKET_REDIS_URL
from selfai_ui.internal.db import get_db
from selfai_ui.models.benchmark_config import BenchmarkConfigs
from selfai_ui.models.curator_jobs import (
    CuratorJob,
    CuratorJobModel,
    CuratorJobs,
    CuratorJobStatusUpdate,
)
from selfai_ui.models.eval_jobs import (
    EvalJob,
    EvalJobModel,
    EvalJobs,
    EvalJobStatusUpdate,
)
from selfai_ui.models.files import FileForm, Files
from selfai_ui.models.job_windows import JobWindows, JobWindowWithSlots
from selfai_ui.models.knowledge import KnowledgeFiles, KnowledgeForm, Knowledges
from selfai_ui.models.training import (
    TrainingJob,
    TrainingJobModel,
    TrainingJobs,
    TrainingJobStatusUpdate,
)
from selfai_ui.socket.utils import RedisLock
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# Same audience string as routers/llamolotl.py and routers/training.py — must
# match self.llamolotl's SERVICE_AUTH_AUDIENCE (self.llamolotl#12).
LLAMOLOTL_AUDIENCE = "self.llamolotl"

# Same audience string as routers/curator.py — must match self.curator's
# SERVICE_AUTH_AUDIENCE (self.curator#5 / self.ai#25).
CURATOR_AUDIENCE = "self.curator"

# Set by main.py lifespan handler so the dispatcher can access app config.
_app_state = None

POLL_INTERVAL = 30  # seconds
LOCK_TIMEOUT = 60  # seconds -- must be > POLL_INTERVAL
STALE_RUNNING_TIMEOUT = 24 * 3600  # seconds before a stuck "running" job is auto-failed


####################################
# Worker pool resolution
####################################


def _resolve_worker_url(job_type: str) -> Optional[str]:
    """Return an idle worker URL for the given job type, or None."""
    if _app_state is None:
        return None

    cfg = _app_state.config

    if job_type == "training":
        base_urls = list(cfg.LLAMOLOTL_BASE_URLS or [])
        busy = set()
        with get_db() as db:
            for row in db.query(TrainingJob).filter_by(status="running").all():
                if row.llamolotl_url_idx is not None:
                    busy.add(row.llamolotl_url_idx)
        for i, url in enumerate(base_urls):
            if i not in busy:
                return url
        return None

    elif job_type == "language-eval":
        base_urls = list(cfg.LANGUAGE_EVAL_BASE_URLS or [])
        busy = set()
        with get_db() as db:
            if db.query(EvalJob).filter(EvalJob.status == "running", EvalJob.eval_type == "language-eval").count():
                busy.add(0)
        for i, url in enumerate(base_urls):
            if i not in busy:
                return url
        return None

    elif job_type == "code-eval":
        base_urls = list(cfg.CODE_EVAL_BASE_URLS or [])
        busy = set()
        with get_db() as db:
            if db.query(EvalJob).filter(EvalJob.status == "running", EvalJob.eval_type == "code-eval").count():
                busy.add(0)
        for i, url in enumerate(base_urls):
            if i not in busy:
                return url
        return None

    elif job_type == "curator":
        base_urls = list(cfg.CURATOR_BASE_URLS or [])
        busy = set()
        with get_db() as db:
            for row in db.query(CuratorJob).filter_by(status="running").all():
                if row.curator_url_idx is not None:
                    busy.add(row.curator_url_idx)
        for i, url in enumerate(base_urls):
            if i not in busy:
                return url
        return None

    return None


####################################
# llamolotl model residency coordination
####################################


async def _ensure_llamolotl_model_ready(model_id: str) -> None:
    """Unload any other model llamolotl has loaded before dispatching to model_id.

    llamolotl's router evicts by slot count (MODELS_MAX), not by available
    VRAM (self.llamolotl#22) -- requesting a second large model while one is
    already resident can OOM the new load outright, crash that child
    process, and leave the router stuck reporting status "loading" forever
    until someone manually restarts the pod. Hit this repeatedly running
    eval sweeps across GLM-4.5-Air/Qwen3-Coder-Next/Gemma 4 back to back.

    This is a best-effort mitigation, not a fix -- self.llamolotl#22 is the
    real one (VRAM-aware eviction in the router itself). If model_id isn't
    something llamolotl actually serves (a cloud/remote model config), the
    /v1/models lookup just won't find it and this is a no-op.
    """
    if _app_state is None:
        return

    base_urls = list(_app_state.config.LLAMOLOTL_BASE_URLS or [])
    if not base_urls:
        return

    for url in base_urls:
        base = url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base}/v1/models")
                resp.raise_for_status()
                models = resp.json().get("data", [])
        except Exception as e:
            log.debug(f"llamolotl model-residency check skipped for {base}: {e}")
            continue

        known_ids = {m.get("id") for m in models}
        if model_id not in known_ids:
            # Not a model this llamolotl instance serves at all -- nothing to do.
            continue

        for m in models:
            other_id = m.get("id")
            status = (m.get("status") or {}).get("value")
            if other_id == model_id or status not in ("loaded", "loading"):
                continue
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    unload_resp = await client.post(f"{base}/models/unload", json={"model": other_id})
                log.info(
                    f"llamolotl: unloaded {other_id!r} to make room for {model_id!r} "
                    f"(status={unload_resp.status_code})"
                )
            except Exception as e:
                log.warning(f"llamolotl: failed to unload {other_id!r} before loading {model_id!r}: {e}")


####################################
# Sync running jobs
####################################


async def _sync_running_jobs() -> None:
    """Poll remote workers for status of all running jobs."""
    from selfai_ui.routers.evaluations import (
        _reconcile_missing_results,
    )
    from selfai_ui.routers.evaluations import (
        _sync_running_jobs as _sync_eval,
    )

    await _sync_eval()
    # Backfill results for completed code-eval jobs whose details never persisted
    # (e.g. the code-eval harness was busy at finish). Idempotent + capped.
    await _reconcile_missing_results()
    await _sync_running_curator_jobs()
    await _sync_running_training_jobs()
    _expire_stale_training_jobs()


async def _sync_running_training_jobs() -> None:
    """Poll llamolotl for non-terminal training jobs and reconcile their status.

    Without this, a finished training job stays 'running' locally until an admin
    manually hits /jobs/{id}/sync. Mirrors _sync_running_curator_jobs and runs
    each queue tick, so completed/failed trainings settle on their own.
    """
    if _app_state is None:
        return

    control_urls = list(_app_state.config.LLAMOLOTL_CONTROL_BASE_URLS or [])
    if not control_urls:
        return

    with get_db() as db:
        rows = db.query(TrainingJob).filter(TrainingJob.status.in_(("running", "queued"))).all()
        jobs = [TrainingJobModel.model_validate(r) for r in rows]

    status_map = {
        "pending": "queued",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }

    for job in jobs:
        if not job.llamolotl_job_id or job.llamolotl_url_idx is None:
            continue
        if job.llamolotl_url_idx >= len(control_urls):
            continue
        control_url = control_urls[job.llamolotl_url_idx].rstrip("/")

        # Heretic jobs report via the pipeline-task API, not the training-jobs API.
        if job.course_id == "heretic":
            endpoint = f"{control_url}/api/heretic/status/{job.llamolotl_job_id}"
        else:
            endpoint = f"{control_url}/api/jobs/{job.llamolotl_job_id}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    endpoint,
                    headers={TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "jobs:read")},
                )
                if resp.status_code == 404:
                    TrainingJobs.update_job_status(
                        id=job.id,
                        update=TrainingJobStatusUpdate(
                            status="failed",
                            error_message="Job not found in llamolotl (may have been lost on restart)",
                        ),
                    )
                    continue
                resp.raise_for_status()
                remote = resp.json()
        except Exception as e:
            log.error(f"Failed to check training job {job.llamolotl_job_id}: {e}")
            stale_before = int(time.time()) - STALE_RUNNING_TIMEOUT
            if job.updated_at < stale_before:
                TrainingJobs.update_job_status(
                    id=job.id,
                    update=TrainingJobStatusUpdate(
                        status="failed",
                        error_message=f"Trainer unreachable for >{STALE_RUNNING_TIMEOUT // 3600}h: {e}",
                    ),
                )
            continue

        new_status = status_map.get(remote.get("status", ""))
        if new_status and new_status != job.status:
            TrainingJobs.update_job_status(
                id=job.id,
                update=TrainingJobStatusUpdate(
                    status=new_status,
                    error_message=remote.get("error_message"),
                ),
            )
            log.info(f"Training job {job.id} auto-synced to '{new_status}'")


def _expire_stale_training_jobs() -> None:
    """Mark training jobs stuck in 'running' for too long as failed."""
    stale_before = int(time.time()) - STALE_RUNNING_TIMEOUT
    with get_db() as db:
        rows = (
            db.query(TrainingJob).filter(TrainingJob.status == "running", TrainingJob.updated_at < stale_before).all()
        )
        for row in rows:
            age_h = (int(time.time()) - row.updated_at) / 3600
            log.warning(f"Training job {row.id} stuck running for {age_h:.1f}h — marking failed")
        if rows:
            db.query(TrainingJob).filter(TrainingJob.status == "running", TrainingJob.updated_at < stale_before).update(
                {"status": "failed", "updated_at": int(time.time())}
            )
            db.commit()


async def _finalize_curator_job(job: CuratorJobModel, curator_url: str) -> bool:
    """After a curator job completes: create a Dataset, pull the output back over
    HTTP (the curator wrote it to its own volume), register the files, link them."""
    try:
        dataset_name = job.dataset_name or job.pipeline_id[:8]

        knowledge = Knowledges.insert_new_knowledge(
            user_id=job.user_id,
            form_data=KnowledgeForm(
                name=dataset_name,
                description=f"Curated dataset from pipeline {job.pipeline_id}",
                # The UI recognizes datasets via meta.dataset (the same flag the
                # "Add a Dataset" form sets). Setting only data.dataset made the
                # curated output render as a plain Collection. `curated` marks it
                # as pipeline-produced (no hf_path) so the detail view samples the
                # local JSONL instead of HuggingFace.
                meta={"dataset": True, "curated": True, "pipeline_id": job.pipeline_id},
                data={"dataset": True},
            ),
        )
        if not knowledge:
            log.error(f"Failed to create dataset for curator job {job.id}")
            return False

        # Pull the curated output back over HTTP and save it onto the API's own
        # volume, then register those local files on the new dataset.
        from selfai_ui.config import UPLOAD_DIR

        local_out = os.path.join(UPLOAD_DIR, "curator_output", job.id)
        os.makedirs(local_out, exist_ok=True)
        downloaded = 0
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                listing = await client.get(
                    f"{curator_url}/api/jobs/{job.curator_job_id}/output",
                    headers={TICKET_HEADER: mint_service_ticket(CURATOR_AUDIENCE, "jobs:read")},
                )
                files = listing.json().get("files", []) if listing.status_code == 200 else []
                for f in files:
                    fn = f.get("filename")
                    if not fn:
                        continue
                    r = await client.get(
                        f"{curator_url}/api/jobs/{job.curator_job_id}/output/{fn}",
                        headers={TICKET_HEADER: mint_service_ticket(CURATOR_AUDIENCE, "jobs:read")},
                    )
                    if r.status_code != 200:
                        continue
                    dest = os.path.join(local_out, fn)
                    with open(dest, "wb") as fh:
                        fh.write(r.content)
                    file_record = Files.insert_new_file(
                        user_id=job.user_id,
                        form_data=FileForm(
                            id=str(uuid.uuid4()),
                            filename=fn,
                            path=dest,
                            meta={
                                "name": fn,
                                "content_type": "application/jsonl",
                                "size": os.path.getsize(dest),
                            },
                        ),
                    )
                    if file_record:
                        KnowledgeFiles.add_file_to_knowledge(knowledge.id, file_record.id)
                        downloaded += 1
                        log.info(f"Pulled + registered {fn} in dataset {knowledge.id}")
        except Exception as e:
            log.error(f"Failed to pull curator output for job {job.id}: {e}")

        if downloaded == 0:
            log.warning(f"Curator job {job.id}: no output pulled — dataset created empty")

        CuratorJobs.update_created_knowledge_id(job.id, knowledge.id)
        log.info(f"Finalized curator job {job.id} → dataset '{dataset_name}' ({knowledge.id})")
        return True

    except Exception as e:
        log.error(f"Error finalizing curator job {job.id}: {e}")
        return False


async def _sync_running_curator_jobs() -> None:
    running = CuratorJobs.get_jobs_by_status("running")
    if not running:
        return

    for job in running:
        if not job.curator_job_id:
            continue
        if _app_state is None:
            continue

        cfg = _app_state.config
        base_urls = list(cfg.CURATOR_BASE_URLS or [])
        idx = job.curator_url_idx or 0
        if idx >= len(base_urls):
            continue
        curator_url = base_urls[idx].rstrip("/")

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{curator_url}/api/jobs/{job.curator_job_id}",
                    headers={TICKET_HEADER: mint_service_ticket(CURATOR_AUDIENCE, "jobs:read")},
                )
                if resp.status_code == 404:
                    log.warning(f"Curator job {job.curator_job_id} not found -- marking failed")
                    CuratorJobs.update_job_status(
                        id=job.id,
                        update=CuratorJobStatusUpdate(
                            status="failed",
                            error_message="Job not found in curator (may have been lost on restart)",
                        ),
                    )
                    continue
                resp.raise_for_status()
                remote = resp.json()
        except Exception as e:
            log.error(f"Failed to check curator job {job.curator_job_id}: {e}")
            stale_before = int(time.time()) - STALE_RUNNING_TIMEOUT
            if job.updated_at < stale_before:
                age_h = (int(time.time()) - job.updated_at) / 3600
                log.warning(f"Curator job {job.id} unreachable and stuck for {age_h:.1f}h — marking failed")
                CuratorJobs.update_job_status(
                    id=job.id,
                    update=CuratorJobStatusUpdate(
                        status="failed",
                        error_message=f"Worker unreachable for >{STALE_RUNNING_TIMEOUT//3600}h: {e}",
                    ),
                )
            continue

        remote_status = remote.get("status", "")
        if remote_status in ("completed", "failed", "cancelled"):
            CuratorJobs.update_job_status(
                id=job.id,
                update=CuratorJobStatusUpdate(
                    status=remote_status,
                    error_message=remote.get("error_message"),
                ),
            )
            log.info(f"Curator job {job.id} synced to '{remote_status}'")

            if remote_status == "completed":
                await _finalize_curator_job(job, curator_url)


####################################
# Promote scheduled jobs
####################################


async def _promote_scheduled_jobs() -> None:
    """Move due scheduled -> queued, and pending (no schedule) -> queued for all job types."""
    # Scheduled jobs whose time has come
    for job in EvalJobs.get_due_scheduled_jobs():
        log.info(f"GPU queue: eval job {job.id} due, promoting to queued")
        EvalJobs.update_job_status(id=job.id, update=EvalJobStatusUpdate(status="queued"))

    for job in TrainingJobs.get_due_scheduled_jobs():
        log.info(f"GPU queue: training job {job.id} due, promoting to queued")
        TrainingJobs.update_job_status(id=job.id, update=TrainingJobStatusUpdate(status="queued"))

    for job in CuratorJobs.get_due_scheduled_jobs():
        log.info(f"GPU queue: curator job {job.id} due, promoting to queued")
        CuratorJobs.update_job_status(id=job.id, update=CuratorJobStatusUpdate(status="queued"))

    # Unscheduled pending jobs — promote to queued so the window dispatcher can pick them up
    with get_db() as db:
        pending_eval = db.query(EvalJob).filter_by(status="pending").all()
        for row in pending_eval:
            log.info(f"GPU queue: eval job {row.id} pending with no schedule, promoting to queued")
            EvalJobs.update_job_status(id=row.id, update=EvalJobStatusUpdate(status="queued"))

        pending_training = db.query(TrainingJob).filter_by(status="pending").all()
        for row in pending_training:
            log.info(f"GPU queue: training job {row.id} pending with no schedule, promoting to queued")
            TrainingJobs.update_job_status(id=row.id, update=TrainingJobStatusUpdate(status="queued"))

        pending_curator = (
            db.query(CuratorJob)
            .filter(
                CuratorJob.status == "pending",
                CuratorJob.scheduled_for == None,  # noqa: E711
            )
            .all()
        )
        for row in pending_curator:
            log.info(f"GPU queue: curator job {row.id} pending with no schedule, promoting to queued")
            CuratorJobs.update_job_status(id=row.id, update=CuratorJobStatusUpdate(status="queued"))


####################################
# Dispatch helpers
####################################


async def _dispatch_training_job(job: TrainingJobModel) -> None:
    from selfai_ui.routers.training import _dispatch_scheduled_job

    await _dispatch_scheduled_job(job)


async def _dispatch_eval_job_by_type(job: EvalJobModel) -> None:
    from selfai_ui.routers.evaluations import (
        _dispatch_eval_job as _code_eval_dispatch,
    )
    from selfai_ui.routers.evaluations import (
        _dispatch_language_eval_job as _language_eval_dispatch,
    )

    # Best-effort: free up any other resident model before this one tries to
    # load. See _ensure_llamolotl_model_ready's docstring / self.llamolotl#22
    # for why this is needed and what it doesn't fix.
    await _ensure_llamolotl_model_ready(job.model_id)

    eval_type = getattr(job, "eval_type", "code-eval") or "code-eval"
    if eval_type == "language-eval":
        await _language_eval_dispatch(job)
    else:
        await _code_eval_dispatch(job)


async def _dispatch_curator_job(job: CuratorJobModel) -> None:
    """Dispatch a curator job to the curator container."""
    if _app_state is None:
        log.warning("GPU queue: app state not set, cannot dispatch curator job")
        CuratorJobs.update_job_status(
            id=job.id,
            update=CuratorJobStatusUpdate(status="failed", error_message="App state not available"),
        )
        return

    cfg = _app_state.config
    base_urls = list(cfg.CURATOR_BASE_URLS or [])
    if not base_urls:
        CuratorJobs.update_job_status(
            id=job.id,
            update=CuratorJobStatusUpdate(status="failed", error_message="No Curator URLs configured"),
        )
        return

    busy = set()
    with get_db() as db:
        for row in db.query(CuratorJob).filter_by(status="running").all():
            if row.curator_url_idx is not None:
                busy.add(row.curator_url_idx)

    url_idx = next((i for i in range(len(base_urls)) if i not in busy), None)
    if url_idx is None:
        log.debug(f"GPU queue: no idle curator worker for job {job.id}")
        return

    curator_url = base_urls[url_idx].rstrip("/")
    pipeline_config = (job.meta or {}).get("pipeline_config")
    if not pipeline_config:
        CuratorJobs.update_job_status(
            id=job.id,
            update=CuratorJobStatusUpdate(
                status="failed",
                error_message=f"No pipeline_config in job meta (pipeline_id={job.pipeline_id})",
            ),
        )
        return

    # Ship the input to the curator over the same HTTP connection the config uses,
    # instead of a shared volume — the curator pod can't see the API's data PVC.
    # The stored input_path is the API-visible path (DATA_DIR rewritten to
    # /workspace/ui-data); read the real file from DATA_DIR, upload it, and adopt
    # the curator-local input/output paths it hands back. Output is pulled back
    # the same way in _finalize_curator_job.
    from selfai_ui.config import DATA_DIR

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            api_input = (pipeline_config.get("input_path") or "").replace("/workspace/ui-data", str(DATA_DIR))
            if api_input and os.path.exists(api_input):
                with open(api_input, "rb") as fh:
                    content = fh.read()
                up = await client.post(
                    f"{curator_url}/api/data/upload",
                    data={"name": str(job.id)},
                    files={"file": ("input.jsonl", content, "application/jsonl")},
                    headers={TICKET_HEADER: mint_service_ticket(CURATOR_AUDIENCE, "data:write")},
                )
                up.raise_for_status()
                paths = up.json()
                pipeline_config["input_path"] = paths["input_path"]
                pipeline_config["output_path"] = paths["output_path"]
            else:
                log.warning(
                    f"Curator job {job.id}: input {api_input!r} not on the API volume; " "dispatching config as-is"
                )
            resp = await client.post(
                f"{curator_url}/api/jobs",
                json=pipeline_config,
                headers={TICKET_HEADER: mint_service_ticket(CURATOR_AUDIENCE, "jobs:create")},
            )
            resp.raise_for_status()
            remote_job = resp.json()
            # Curator jobs start PENDING — approve starts execution immediately.
            # Curator is a pure executor; the daemon owns all dispatch decisions.
            await client.post(
                f"{curator_url}/api/jobs/{remote_job['job_id']}/approve",
                headers={TICKET_HEADER: mint_service_ticket(CURATOR_AUDIENCE, "jobs:write")},
            )
    except Exception as e:
        log.error(f"Curator dispatch failed for job {job.id}: {e}")
        CuratorJobs.update_job_status(
            id=job.id,
            update=CuratorJobStatusUpdate(
                status="failed",
                error_message=f"Failed to dispatch to curator: {e}",
            ),
        )
        return

    CuratorJobs.update_job_status(
        id=job.id,
        update=CuratorJobStatusUpdate(
            status="running",
            curator_job_id=remote_job.get("job_id"),
            curator_url_idx=url_idx,
        ),
    )
    log.info(f"Curator job {job.id} dispatched (remote_id={remote_job.get('job_id')})")


async def _dispatch_job(job_type: str, job) -> None:
    if job_type == "training":
        await _dispatch_training_job(job)
    elif job_type in ("language-eval", "code-eval"):
        await _dispatch_eval_job_by_type(job)
    elif job_type == "curator":
        await _dispatch_curator_job(job)


####################################
# Dispatch: run_now
####################################


async def _dispatch_run_now_jobs() -> None:
    """Dispatch all run_now+queued jobs immediately, bypassing windows."""
    with get_db() as db:
        training_rows = (
            db.query(TrainingJob)
            .filter(TrainingJob.priority == "run_now", TrainingJob.status == "queued")
            .order_by(TrainingJob.created_at.asc())
            .all()
        )
        eval_rows = (
            db.query(EvalJob)
            .filter(EvalJob.priority == "run_now", EvalJob.status == "queued")
            .order_by(EvalJob.created_at.asc())
            .all()
        )
        curator_rows = (
            db.query(CuratorJob)
            .filter(CuratorJob.priority == "run_now", CuratorJob.status == "queued")
            .order_by(CuratorJob.created_at.asc())
            .all()
        )
        training_jobs = [TrainingJobModel.model_validate(r) for r in training_rows]
        eval_jobs = [EvalJobModel.model_validate(r) for r in eval_rows]
        curator_jobs = [CuratorJobModel.model_validate(r) for r in curator_rows]

    for job in training_jobs:
        log.info(f"GPU queue: dispatching run_now training job {job.id}")
        await _dispatch_training_job(job)

    for job in eval_jobs:
        log.info(f"GPU queue: dispatching run_now eval job {job.id}")
        await _dispatch_eval_job_by_type(job)

    for job in curator_jobs:
        log.info(f"GPU queue: dispatching run_now curator job {job.id}")
        await _dispatch_curator_job(job)


####################################
# Active window query
####################################


def _get_active_window() -> Optional[JobWindowWithSlots]:
    return JobWindows.get_active_window()


####################################
# Fit check
####################################


def _fits_in_window(
    benchmark: str,
    eval_type: str,
    remaining_minutes: float,
    min_remaining_minutes: int,
) -> bool:
    cfg = BenchmarkConfigs.get_by_benchmark(benchmark, eval_type)
    if not cfg:
        return True  # unknown benchmark -- allow it
    return cfg.max_duration_minutes <= (remaining_minutes - min_remaining_minutes)


####################################
# Running count helpers
####################################


def _running_count(job_type: str) -> int:
    with get_db() as db:
        if job_type == "training":
            return db.query(TrainingJob).filter_by(status="running").count()
        elif job_type in ("language-eval", "code-eval"):
            return db.query(EvalJob).filter(EvalJob.status == "running", EvalJob.eval_type == job_type).count()
        elif job_type == "curator":
            return db.query(CuratorJob).filter_by(status="running").count()
    return 0


####################################
# Window dispatch
####################################


async def _dispatch_window_jobs(window: JobWindowWithSlots) -> None:
    """Three-pass priority dispatch within an active window."""
    now = int(time.time())
    remaining_minutes = max(0.0, (window.end_at - now) / 60)
    slots = {s.job_type: s for s in window.slots}

    if not slots:
        return

    # Track how many we've dispatched this cycle (to avoid exceeding max_concurrent
    # before the DB write is visible)
    dispatched: dict[str, int] = {jt: 0 for jt in slots}

    # ----------------------------------------------------------------
    # Pass 1 -- high priority, FIFO across all slot types
    # ----------------------------------------------------------------
    high_candidates: list[tuple] = []

    with get_db() as db:
        for jtype in slots:
            if jtype == "training":
                for r in (
                    db.query(TrainingJob).filter(TrainingJob.priority == "high", TrainingJob.status == "queued").all()
                ):
                    high_candidates.append((r.created_at, jtype, TrainingJobModel.model_validate(r)))
            elif jtype in ("language-eval", "code-eval"):
                for r in (
                    db.query(EvalJob)
                    .filter(
                        EvalJob.priority == "high",
                        EvalJob.status == "queued",
                        EvalJob.eval_type == jtype,
                    )
                    .all()
                ):
                    high_candidates.append((r.created_at, jtype, EvalJobModel.model_validate(r)))
            elif jtype == "curator":
                for r in (
                    db.query(CuratorJob).filter(CuratorJob.priority == "high", CuratorJob.status == "queued").all()
                ):
                    high_candidates.append((r.created_at, jtype, CuratorJobModel.model_validate(r)))

    high_candidates.sort(key=lambda x: x[0])

    for _, jtype, job in high_candidates:
        slot = slots[jtype]
        if _running_count(jtype) + dispatched.get(jtype, 0) >= slot.max_concurrent:
            continue
        if jtype in ("language-eval", "code-eval"):
            if not _fits_in_window(job.benchmark, jtype, remaining_minutes, slot.min_remaining_minutes):
                continue
        log.info(f"GPU queue: Pass 1 -- high {jtype} job {job.id}")
        await _dispatch_job(jtype, job)
        dispatched[jtype] = dispatched.get(jtype, 0) + 1

    # ----------------------------------------------------------------
    # Pass 2 -- normal priority: preferred type first, then others
    # Training is deferred to Pass 3.
    # ----------------------------------------------------------------
    preferred = window.preferred_job_type
    pass2_types = [preferred] + [jt for jt in slots if jt != preferred and jt != "training"]

    for jtype in pass2_types:
        slot = slots.get(jtype)
        if not slot:
            continue
        with get_db() as db:
            if jtype in ("language-eval", "code-eval"):
                rows = (
                    db.query(EvalJob)
                    .filter(
                        EvalJob.priority == "normal",
                        EvalJob.status == "queued",
                        EvalJob.eval_type == jtype,
                    )
                    .order_by(EvalJob.created_at.asc())
                    .all()
                )
                candidates = [EvalJobModel.model_validate(r) for r in rows]
            elif jtype == "curator":
                rows = (
                    db.query(CuratorJob)
                    .filter(CuratorJob.priority == "normal", CuratorJob.status == "queued")
                    .order_by(CuratorJob.created_at.asc())
                    .all()
                )
                candidates = [CuratorJobModel.model_validate(r) for r in rows]
            else:
                candidates = []

        for job in candidates:
            if _running_count(jtype) + dispatched.get(jtype, 0) >= slot.max_concurrent:
                break
            if jtype in ("language-eval", "code-eval"):
                if not _fits_in_window(job.benchmark, jtype, remaining_minutes, slot.min_remaining_minutes):
                    continue
            log.info(f"GPU queue: Pass 2 -- normal {jtype} job {job.id}")
            await _dispatch_job(jtype, job)
            dispatched[jtype] = dispatched.get(jtype, 0) + 1

    # ----------------------------------------------------------------
    # Pass 3 -- training normal fallback
    # ----------------------------------------------------------------
    if "training" not in slots:
        return

    slot = slots["training"]
    capacity = slot.max_concurrent - _running_count("training") - dispatched.get("training", 0)
    if capacity <= 0:
        return

    with get_db() as db:
        rows = (
            db.query(TrainingJob)
            .filter(TrainingJob.priority == "normal", TrainingJob.status == "queued")
            .order_by(TrainingJob.created_at.asc())
            .limit(capacity)
            .all()
        )
        training_candidates = [TrainingJobModel.model_validate(r) for r in rows]

    for job in training_candidates:
        if _running_count("training") + dispatched.get("training", 0) >= slot.max_concurrent:
            break
        log.info(f"GPU queue: Pass 3 -- training fallback job {job.id}")
        await _dispatch_job("training", job)
        dispatched["training"] = dispatched.get("training", 0) + 1


####################################
# Main loop
####################################


def _make_lock() -> RedisLock:
    return RedisLock(
        redis_url=WEBSOCKET_REDIS_URL,
        lock_name="selfai:gpu_queue_lock",
        timeout_secs=LOCK_TIMEOUT,
    )


async def process_gpu_queue_v2() -> None:
    """Multi-node-safe, window-aware GPU job dispatcher. Polls every 30s.

    The RedisLock prevents double-dispatch in multi-node deployments. If Redis
    is unavailable the lock is skipped and dispatch runs anyway — safe for
    single-node setups and still correct for multi-node (DB status checks
    prevent actual duplicate work).
    """
    import redis.exceptions

    lock = _make_lock()

    while True:
        lock_acquired = False
        try:
            try:
                lock_acquired = bool(await lock.aquire_lock())
            except redis.exceptions.ConnectionError as e:
                log.warning(f"GPU queue: Redis unavailable, running without lock: {e}")
                lock = _make_lock()
                lock_acquired = True  # proceed anyway

            if not lock_acquired:
                # Another node holds the lock — skip this cycle
                await asyncio.sleep(POLL_INTERVAL)
                continue

            try:
                await _sync_running_jobs()
                await _promote_scheduled_jobs()
                await _dispatch_run_now_jobs()

                window = _get_active_window()
                if window:
                    await _dispatch_window_jobs(window)

            except Exception as e:
                log.error(f"GPU queue dispatch error: {e}", exc_info=True)
            finally:
                try:
                    await lock.release_lock()
                except Exception as e:
                    # Lock expires naturally after LOCK_TIMEOUT seconds, but log
                    # in case release is failing for a different reason.
                    log.debug(f"GPU queue lock release failed (will expire naturally): {e}")

        except Exception as e:
            log.error(f"GPU queue error: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)


# Backward-compatible alias
process_gpu_queue = process_gpu_queue_v2
