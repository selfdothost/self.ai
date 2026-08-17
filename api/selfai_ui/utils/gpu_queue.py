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
from pathlib import Path
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
from selfai_ui.models.model_versions import PublishJob, PublishJobModel, PublishJobs
from selfai_ui.models.models import Models
from selfai_ui.models.training import (
    TrainingJob,
    TrainingJobModel,
    TrainingJobs,
    TrainingJobStatusUpdate,
)
from selfai_ui.models.vram_leases import VramLeases
from selfai_ui.socket.utils import RedisLock
from selfai_ui.utils.model_versions import (
    complete_publish_job,
    record_version_for_artifact,
    start_publish_merge,
)
from selfai_ui.utils.service_auth import TICKET_HEADER, mint_service_ticket

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

# Same audience string as routers/llamolotl.py and routers/training.py — must
# match self.llamolotl's SERVICE_AUTH_AUDIENCE (self.llamolotl#12).
LLAMOLOTL_AUDIENCE = "self.llamolotl"

# Same audience string as routers/curator.py — must match self.curator's
# SERVICE_AUTH_AUDIENCE (self.curator#5 / self.ai#25).
CURATOR_AUDIENCE = "self.curator"

# self.ai#136. A publish merges adapters into a base through llamolotl's
# pipeline, which loads the base at fp16 with device_map="auto"
# (self.llamolotl api/merge_lora.py:51-52) — so it holds roughly the fp16
# footprint of the base ON THE CARD. It is a distinct consumer from
# `self.llamolotl`: the merge runs in a pipeline subprocess, and acquiring as
# llamolotl would mark the serving process the exclusive holder while the
# serving process is exactly what has to give up its VRAM first.
PUBLISH_AUDIENCE = "self.publish"

# Set by main.py lifespan handler so the dispatcher can access app config.
_app_state = None

POLL_INTERVAL = 30  # seconds
LOCK_TIMEOUT = 60  # seconds -- must be > POLL_INTERVAL
STALE_RUNNING_TIMEOUT = 24 * 3600  # seconds before a stuck "running" job is auto-failed

# Terminate a curator job that is still running after its window closed
# (self.ai#88). Default OFF, deliberately: NeMo Curator pipelines have no
# checkpointing, so terminate-and-requeue restarts the run from stage 0 — with a
# curation run longer than a window that is a treadmill that never finishes.
# Regardless of this switch, an overrun is always LOGGED and recorded on the job,
# and the exclusive lease is always released at window end so an overrunning job
# can never keep local inference locked out. Flip this on once curation runs are
# checkpointed or reliably window-sized.
CURATOR_ENFORCE_WINDOW_END = (
    os.environ.get("CURATOR_ENFORCE_WINDOW_END", "False").lower() == "true"
)


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
    await _sync_running_publish_jobs()
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
            if new_status == "completed":
                await _record_trained_adapter(job, remote)
            log.info(f"Training job {job.id} auto-synced to '{new_status}'")


async def _record_trained_adapter(job: TrainingJobModel, remote: dict) -> None:
    """Record a finished training run as a cheap `adapter` version (self.ai#131).

    This is the cheap tier of Decision 5, and this is the right hook for it: a
    training run fits a LoRA and touches no base weights, so the version costs
    an adapter rather than several GB. `routers/llamolotl.py`'s
    `/api/pipeline/bake` is deliberately NOT wired here — that path merges
    adapters into a base and emits a new GGUF, which is a publish (kit R6,
    build-site T-007), not a bake in this sense.

    A failed or cancelled run reaches this function not at all, so it records
    nothing — matching self.llamolotl#43's "a failed or cancelled run produces
    no commit for the step that failed".

    Best-effort and never raises: a self.corpus outage must not stall the queue
    tick that every other job's status reconciliation depends on.
    """
    if _app_state is None or not _app_state.config.ENABLE_SELF_CORPUS:
        return

    model = Models.get_model_by_id(job.model_id) if job.model_id else None
    meta = model.meta.model_dump() if model else {}
    line_id = meta.get("line_id")

    # The adapter's own path as llamolotl reports it. When it reports nothing,
    # the version is still recorded — with no artifact_ref, so resolution
    # refuses it explicitly instead of serving a guess. Structured output
    # identity is self.llamolotl#43's half of this.
    artifact_ref = remote.get("output_path") or remote.get("output_dir") or remote.get("adapter_path")

    try:
        await record_version_for_artifact(
            _app_state,
            user_id=job.user_id,
            kind="adapter",
            artifact_ref=artifact_ref,
            manifest={
                "training_job": job.id,
                "course": job.course_id,
                "base_model": job.model_id,
                "llamolotl_job": job.llamolotl_job_id,
                "metrics": remote.get("metrics"),
            },
            line_id=line_id,
            line_name=job.model_id or "training line",
            produced_by={"job_kind": "training", "job_id": job.id},
            commit_message=f"adapter from training job {job.id}",
        )
    except Exception as e:
        log.warning(f"Failed to record an adapter version for training job {job.id}: {e}")


####################################
# Publish jobs (self.ai#136)
####################################


def _publish_is_lease_consumer() -> bool:
    """Whether a publish lease is registered for this deployment.

    Unregistered means nothing wired ``PUBLISH_VRAM_CAPACITY_BYTES``, so there
    is no lease to acquire and dispatch proceeds as it did before. Same posture
    as `_curator_is_lease_consumer`: the lease must never become a hidden
    precondition that silently stops publishing on a deployment that never
    opted in.
    """
    try:
        return VramLeases.get(PUBLISH_AUDIENCE) is not None
    except Exception as e:
        log.warning("GPU queue: could not read the publish lease registration (%r)", e)
        return False


async def _acquire_publish_card(job: PublishJobModel) -> tuple:
    """Seize the whole card for a merge. Returns ``(ok, reason)``.

    Exclusive, deliberately. The merge loads the base at fp16 and a resident
    chat model routinely holds most of the rest of the 4090; sharing bets that
    the sum fits, with an OOM inside llamolotl as the losing outcome — which
    surfaces as somebody else's chat request dying while an artist published.
    The cost is that inference is locked out for the length of a merge, which
    is the same trade curation already makes.
    """
    if not _publish_is_lease_consumer():
        return (True, None)

    from selfai_ui.utils.vram_broker import VramBroker

    try:
        result = await VramBroker.acquire_exclusive(PUBLISH_AUDIENCE)
    except ValueError as e:
        log.warning("GPU queue: publish exclusive-acquire precondition failed (%r)", e)
        return (True, None)
    except Exception as e:
        # An unknown broker state must not silently hand the card over.
        log.error("GPU queue: publish exclusive-acquire errored (%r)", e)
        return (False, f"Could not acquire the GPU for the publish: {e}")

    if result.acquired:
        log.info(
            "GPU queue: publish job %s holds the card exclusively (reclaimed %d holder(s))",
            job.id,
            len(result.reclaimed),
        )
        return (True, None)

    parts = [result.reason]
    for holder in result.reclaimed:
        if holder.outcome == "confirmed":
            continue
        said = f" — {holder.reason}" if holder.reason else ""
        parts.append(f"{holder.consumer_id}: {holder.outcome}{said}")
    return (False, "Waiting for the GPU. " + "; ".join(p for p in parts if p))


async def _release_publish_card(context: str) -> None:
    """Drop the exclusive lease once no publish is left holding the card.

    Guarded on there being no other running publish, for the same reason
    `_release_curator_card` is: with `max_concurrent` above 1, one merge
    finishing must not hand the card back while a sibling is still running.
    """
    if not _publish_is_lease_consumer():
        return
    if _running_count("publish") > 0:
        return
    try:
        from selfai_ui.utils.vram_broker import VramBroker

        if await VramBroker.release_exclusive(PUBLISH_AUDIENCE):
            log.info("GPU queue: publish released its exclusive lease (%s)", context)
    except Exception as e:
        log.warning("GPU queue: releasing the publish exclusive lease failed (%r)", e)


async def _dispatch_publish_job(job: PublishJobModel) -> None:
    """Take the card, then ask llamolotl to merge.

    Order matters: a merge dispatched before the lease is held is exactly the
    race this job type exists to close. On refusal nothing is dispatched, the
    job stays `queued`, and the reason is recorded so an operator can see WHY
    rather than watching it sit there.
    """
    if _app_state is None:
        log.warning("GPU queue: app state not set, cannot dispatch publish job")
        return

    ok, reason = await _acquire_publish_card(job)
    if not ok:
        PublishJobs.record_wait_reason(job.id, reason)
        return

    try:
        task_id = await start_publish_merge(_app_state, job)
    except Exception as e:
        # The card was taken for a merge that never started; give it back
        # before failing, or the next consumer waits on a lease nobody uses.
        PublishJobs.update_status(job.id, "failed", error_message=str(e))
        await _release_publish_card(f"publish job {job.id} failed to dispatch")
        log.warning("GPU queue: publish job %s failed to dispatch (%r)", job.id, e)
        return

    PublishJobs.record_wait_reason(job.id, None)
    PublishJobs.mark_dispatched(job.id, task_id)
    log.info("GPU queue: publish job %s dispatched as llamolotl task %s", job.id, task_id)


async def _sync_running_publish_jobs() -> None:
    """Poll llamolotl for running merges and settle them.

    The version is recorded HERE, on completion — not at dispatch. llamolotl's
    POST returns as soon as the pipeline task is queued, so recording then
    would claim a base GGUF that does not exist yet and would release the card
    while the merge was still using it.
    """
    if _app_state is None:
        return

    control_urls = list(_app_state.config.LLAMOLOTL_CONTROL_BASE_URLS or [])
    if not control_urls:
        return

    for job in PublishJobs.get_jobs_by_status("running"):
        if not job.llamolotl_job_id:
            continue
        url_idx = job.llamolotl_url_idx or 0
        if url_idx >= len(control_urls):
            continue

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{control_urls[url_idx].rstrip('/')}/api/pipeline/tasks/{job.llamolotl_job_id}",
                    headers={TICKET_HEADER: mint_service_ticket(LLAMOLOTL_AUDIENCE, "pipeline:read")},
                )
                if resp.status_code == 404:
                    PublishJobs.update_status(
                        job.id,
                        "failed",
                        error_message="Merge task not found in llamolotl (may have been lost on restart)",
                    )
                    await _release_publish_card(f"publish job {job.id} lost its remote task")
                    continue
                resp.raise_for_status()
                remote = resp.json()
        except Exception as e:
            # Unreachable is not the same as failed: leave it running and try
            # again next tick rather than releasing a card still in use.
            log.warning("GPU queue: could not poll publish job %s (%r)", job.id, e)
            continue

        status = remote.get("status")
        if status == "completed":
            artifact = None
            output_path = remote.get("output_path")
            if output_path:
                artifact = Path(output_path).name
            try:
                await complete_publish_job(_app_state, job, artifact_ref=artifact)
            except Exception as e:
                log.warning("GPU queue: publish job %s completed but could not be recorded (%r)", job.id, e)
            await _release_publish_card(f"publish job {job.id} completed")
        elif status == "failed":
            PublishJobs.update_status(
                job.id,
                "failed",
                error_message=remote.get("error_message") or "the merge failed in llamolotl",
            )
            await _release_publish_card(f"publish job {job.id} failed")


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
                await _release_curator_card(f"job {job.id} gave up as unreachable")
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

            # The run is over however it ended — give the card back so chat,
            # voices and image generation resume without waiting for a window to
            # elapse (self.ai#88).
            await _release_curator_card(f"job {job.id} settled as {remote_status}")

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

    for job in PublishJobs.get_due_scheduled_jobs():
        log.info(f"GPU queue: publish job {job.id} due, promoting to queued")
        PublishJobs.update_status(job.id, "queued")

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

    # Reserve the card through the broker first (self.ai#107). This is the part
    # _ensure_llamolotl_model_ready() never could do: it only ever unloads
    # llamolotl's OWN models, so a lower-priority tenant holding several GiB
    # (self.speak, self.sketch) stayed put and the eval's load allocated around
    # it into an OOM.
    #
    # A denial is logged, not fatal. Failing a queued eval on a capacity read is
    # a worse trade than dispatching and letting llamolotl's own admission check
    # refuse cleanly — it has the authoritative view and a structured 503 for it.
    from selfai_ui.utils.vram_admission import ensure_llamolotl_vram

    vram = await ensure_llamolotl_vram(_app_state, job.model_id)
    if vram.denied:
        log.warning(
            "GPU queue: VRAM lease denied for eval job %s model %r (%s) — dispatching "
            "anyway; llamolotl's own check decides",
            job.id,
            job.model_id,
            vram.detail,
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


####################################
# Curator VRAM lease (self.ai#88)
####################################


def _curator_is_lease_consumer() -> bool:
    """Whether self.curator is registered in the VRAM lease registry.

    Unregistered means the deployment has no ``CURATOR_VRAM_CAPACITY_BYTES``
    wired, so there is no lease to acquire and dispatch proceeds exactly as it
    did before this feature. The lease must never become a hidden precondition
    that silently stops curation on a deployment that never opted into it."""
    try:
        return VramLeases.get(CURATOR_AUDIENCE) is not None
    except Exception as e:
        # A registry read failure is not evidence of anything. Treat it as
        # unconfigured so a yard-pg hiccup degrades to today's behaviour rather
        # than wedging the queue.
        log.warning("GPU queue: could not read the curator lease registration (%r)", e)
        return False


async def _acquire_curator_card(job: CuratorJobModel) -> tuple:
    """Seize the whole 4090 for a curation run. Returns ``(ok, reason)``.

    A curation pipeline loads its own classifier models and Ray object store; it
    cannot share the card with a resident llama-server, which is exactly why
    ``lease_admission.GPU_EXCLUSIVE_WINDOW_JOB_TYPES`` already treats an active
    curator window as a hard inference lockout. That lockout was WINDOW-driven
    only — nothing ever told the broker, so the broker kept granting VRAM to
    other consumers during a curator window and self.curator itself was an
    invisible holder.

    This is the missing half: ``acquire_exclusive`` clears every other holder
    cooperatively (and force-reaps confirmed-stale ones), and only then reports
    success. It has existed and been tested since the broker landed with ZERO
    production callers — self.curator is the first.

    On refusal nothing is dispatched and the reason is returned for recording on
    the job, so an admin can see WHY their curation run did not start rather than
    watching it sit in `queued`."""
    from selfai_ui.utils.vram_broker import VramBroker

    try:
        result = await VramBroker.acquire_exclusive(CURATOR_AUDIENCE)
    except ValueError as e:
        # Requester not registered — the guard above should have caught it, but
        # a race with a config change is possible. Not fatal: fall through to a
        # plain dispatch rather than stranding the job.
        log.warning("GPU queue: curator exclusive-acquire precondition failed (%r)", e)
        return (True, None)
    except Exception as e:
        # An unknown broker state must not silently hand the card over.
        log.error("GPU queue: curator exclusive-acquire errored (%r)", e)
        return (False, f"Could not acquire the GPU for curation: {e}")

    if result.acquired:
        log.info(
            "GPU queue: curator job %s holds the card exclusively (reclaimed %d holder(s))",
            job.id,
            len(result.reclaimed),
        )
        return (True, None)

    # Build a reason an operator can act on: who refused, and in their own words.
    parts = [result.reason]
    for holder in result.reclaimed:
        if holder.outcome == "confirmed":
            continue
        said = f" — {holder.reason}" if holder.reason else ""
        parts.append(f"{holder.consumer_id}: {holder.outcome}{said}")
    return (False, "Waiting for the GPU. " + "; ".join(p for p in parts if p))


async def _release_curator_card(context: str) -> None:
    """Drop the exclusive lease once no curation run is left holding the card.

    Guarded on there being no other running curator job: with `max_concurrent`
    above 1 in a window slot, one job finishing must not hand the card back while
    a sibling is still mid-pipeline."""
    if not _curator_is_lease_consumer():
        return
    if _running_count("curator") > 0:
        return
    try:
        from selfai_ui.utils.vram_broker import VramBroker

        if await VramBroker.release_exclusive(CURATOR_AUDIENCE):
            log.info("GPU queue: curator released its exclusive lease (%s)", context)
    except Exception as e:
        log.warning("GPU queue: releasing the curator exclusive lease failed (%r)", e)


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

    # Take the card BEFORE shipping any input: a curation run needs the whole
    # 4090, and a job that cannot get it must stay queued rather than start and
    # collide with a resident model (self.ai#88). Left `queued`, so the next
    # cycle retries — with the refusal recorded so the wait is legible.
    if _curator_is_lease_consumer():
        acquired, refusal = await _acquire_curator_card(job)
        if not acquired:
            log.info("GPU queue: curator job %s not dispatched — %s", job.id, refusal)
            CuratorJobs.update_job_status(
                id=job.id,
                update=CuratorJobStatusUpdate(status="queued", error_message=refusal),
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
        # The card was seized for a run that never started. Hand it back rather
        # than leaving every other consumer locked out by a failed dispatch.
        await _release_curator_card(f"dispatch of job {job.id} failed")
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
    elif job_type == "publish":
        await _dispatch_publish_job(job)


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
        publish_rows = (
            db.query(PublishJob)
            .filter(PublishJob.priority == "run_now", PublishJob.status == "queued")
            .order_by(PublishJob.created_at.asc())
            .all()
        )
        training_jobs = [TrainingJobModel.model_validate(r) for r in training_rows]
        eval_jobs = [EvalJobModel.model_validate(r) for r in eval_rows]
        curator_jobs = [CuratorJobModel.model_validate(r) for r in curator_rows]
        publish_jobs = [PublishJobModel.model_validate(r) for r in publish_rows]

    for job in training_jobs:
        log.info(f"GPU queue: dispatching run_now training job {job.id}")
        await _dispatch_training_job(job)

    for job in eval_jobs:
        log.info(f"GPU queue: dispatching run_now eval job {job.id}")
        await _dispatch_eval_job_by_type(job)

    # NOTE (self.ai#88): `run_now` is an ADMIN escalation, and for curator it is
    # enforced as one at the entry point (routers/curator.py refuses `run_now`
    # from a non-admin). It stays a real window bypass here — that is what the
    # tier is for — but it can no longer be self-granted by any verified user
    # posting `priority: "run_now"` to /curator/queue.
    for job in curator_jobs:
        log.info(f"GPU queue: dispatching run_now curator job {job.id} (admin window bypass)")
        await _dispatch_curator_job(job)

    # `run_now` bypasses the WINDOW; it does not bypass the LEASE. A publish
    # holds the fp16 base on the card (self.ai#136), so an admin escalation
    # still asks the broker first and still records the refusal if it is denied
    # — otherwise run_now would be a way to OOM llamolotl on demand.
    for job in publish_jobs:
        log.info(f"GPU queue: dispatching run_now publish job {job.id} (admin window bypass)")
        await _dispatch_publish_job(job)


####################################
# Dispatch: Test Mode (dry_run) evals
####################################


async def _dispatch_dry_run_eval_jobs() -> None:
    """Dispatch queued Test Mode (dry_run) eval jobs immediately, bypassing
    windows (self.ai#64).

    Test Mode is documented (and labelled in the submit UI) as synthetic
    data / no model inference -- it doesn't touch the GPU, so the window gate
    that exists to arbitrate real GPU contention between training/evals/
    curation doesn't apply to it. Before this, a dry_run job queued outside an
    active window sat there indefinitely, making "Test Mode" -- meant as a
    safe, cheap way to validate the harness -- unusable whenever no window
    happened to be open.

    Unlike `_dispatch_run_now_jobs`, this is not priority-gated: "needs no
    GPU" is a property of the job itself (`meta.dry_run`), not an admin
    escalation, so every queued dry_run job qualifies regardless of its
    `priority`. A job that's also `run_now` will already have been dispatched
    (and moved off `status == "queued"`) by `_dispatch_run_now_jobs` earlier in
    the same cycle, so there's no double-dispatch risk from querying again here.
    """
    with get_db() as db:
        rows = (
            db.query(EvalJob)
            .filter(EvalJob.status == "queued")
            .order_by(EvalJob.created_at.asc())
            .all()
        )
        jobs = [EvalJobModel.model_validate(r) for r in rows]

    for job in jobs:
        if (job.meta or {}).get("dry_run"):
            log.info(f"GPU queue: dispatching dry_run eval job {job.id} (no GPU needed, window gate bypassed)")
            await _dispatch_eval_job_by_type(job)


####################################
# Active window query
####################################


def _get_active_window() -> Optional[JobWindowWithSlots]:
    return JobWindows.get_active_window()


def _window_allows(window: Optional[JobWindowWithSlots], job_type: str) -> bool:
    """Whether ``window`` is open for ``job_type`` (has a slot for it)."""
    if window is None:
        return False
    return any(slot.job_type == job_type for slot in (window.slots or []))


####################################
# Window-end enforcement (self.ai#88)
####################################


async def _enforce_curator_window_end(window: Optional[JobWindowWithSlots]) -> None:
    """Handle curator jobs still running once their window has closed.

    Nothing used to enforce window END at all: ``_fits_in_window`` is consulted
    only for eval types, so a curator job dispatched a minute before a window
    closed kept the whole 4090 indefinitely — the admin's schedule bounded when
    curation *started*, never when it stopped.

    Two things happen here, and they are deliberately different in weight:

    * **Always** — the overrun is logged and recorded on the job, and the
      exclusive lease is released. Releasing it does NOT hand curator's VRAM
      away: the poller reports what the pipeline actually holds, so the ledger
      still accounts for it and self.curator (priority 8) simply becomes
      reclaimable by self.llamolotl (10) — which asks politely and gets a
      reasoned refusal. What it does stop is an overrunning job hard-locking
      every chat request out for as long as it runs.
    * **Only when ``CURATOR_ENFORCE_WINDOW_END``** — the run is cancelled and
      requeued for the next window. Off by default because NeMo Curator has no
      checkpointing: a run longer than a window would restart from stage 0 every
      time and never finish. Enable it once curation runs are checkpointed or
      reliably window-sized."""
    if _window_allows(window, "curator"):
        return

    running = CuratorJobs.get_jobs_by_status("running")
    if not running:
        return

    for job in running:
        log.warning(
            "GPU queue: curator job %s is still running with no active curator "
            "window — it is holding the 4090 outside the admin's schedule",
            job.id,
        )

    if not CURATOR_ENFORCE_WINDOW_END:
        for job in running:
            CuratorJobs.update_job_status(
                id=job.id,
                update=CuratorJobStatusUpdate(
                    status="running",
                    error_message=(
                        "Overrunning its GPU window: the curator window has closed "
                        "and this run is still holding the GPU. It will finish, but "
                        "outside the schedule (set CURATOR_ENFORCE_WINDOW_END=true "
                        "to stop and requeue overruns instead)."
                    ),
                ),
            )
        # Drop the hard inference lockout even though the run continues.
        try:
            from selfai_ui.utils.vram_broker import VramBroker

            if _curator_is_lease_consumer() and await VramBroker.release_exclusive(
                CURATOR_AUDIENCE
            ):
                log.warning(
                    "GPU queue: curator exclusive lease released at window end while "
                    "%d job(s) still run — chat is no longer locked out, and the "
                    "pipeline's real VRAM is still on the ledger via the poller",
                    len(running),
                )
        except Exception as e:
            log.warning("GPU queue: window-end exclusive release failed (%r)", e)
        return

    for job in running:
        await _stop_and_requeue_curator_job(
            job,
            "Stopped at the end of its GPU window and requeued for the next one.",
        )
    await _release_curator_card("window ended")


async def _stop_and_requeue_curator_job(job: CuratorJobModel, reason: str) -> None:
    """Cancel a running curation run remotely and put OUR row back in the queue.

    The remote job is abandoned, not reused: self.curator's records are per-run
    and a cancelled one cannot be resumed, so ``curator_job_id``/``curator_url_idx``
    are cleared and the next window dispatches a fresh run from the same stored
    ``pipeline_config``. Requeueing rather than cancelling is the deliberate
    choice — curation work is restartable, and an operator reclaiming the GPU
    should not also have to rebuild every queued pipeline by hand."""
    if job.curator_job_id and _app_state is not None:
        base_urls = list(_app_state.config.CURATOR_BASE_URLS or [])
        idx = job.curator_url_idx or 0
        if idx < len(base_urls):
            url = base_urls[idx].rstrip("/")
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.post(
                        f"{url}/api/jobs/{job.curator_job_id}/cancel",
                        headers={
                            TICKET_HEADER: mint_service_ticket(CURATOR_AUDIENCE, "jobs:write")
                        },
                    )
            except Exception as e:
                # Best-effort: the local requeue must happen regardless, and an
                # abandoned remote run is settled by the e-stop or by curator's
                # own restart handling.
                log.warning(
                    "GPU queue: could not cancel remote curator job %s (%r)",
                    job.curator_job_id,
                    e,
                )

    CuratorJobs.requeue_for_next_window(job.id, reason)
    log.warning("GPU queue: curator job %s stopped and requeued — %s", job.id, reason)


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
        elif job_type == "publish":
            return db.query(PublishJob).filter_by(status="running").count()
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
            elif jtype == "publish":
                for r in (
                    db.query(PublishJob).filter(PublishJob.priority == "high", PublishJob.status == "queued").all()
                ):
                    high_candidates.append((r.created_at, jtype, PublishJobModel.model_validate(r)))

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
            elif jtype == "publish":
                rows = (
                    db.query(PublishJob)
                    .filter(PublishJob.priority == "normal", PublishJob.status == "queued")
                    .order_by(PublishJob.created_at.asc())
                    .all()
                )
                candidates = [PublishJobModel.model_validate(r) for r in rows]
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
                await _dispatch_dry_run_eval_jobs()

                window = _get_active_window()
                # Checked every cycle, window or not: a curator run that outlives
                # its window is exactly the case with no window to notice it
                # (self.ai#88).
                await _enforce_curator_window_end(window)
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
