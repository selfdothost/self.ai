import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import get_db
from selfai_ui.models.curator_jobs import CuratorJob, CuratorJobModel, CuratorJobs
from selfai_ui.models.eval_jobs import EvalJob, EvalJobModel, EvalJobs
from selfai_ui.models.training import TrainingJob, TrainingJobModel, TrainingJobs
from selfai_ui.utils.auth import get_admin_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))

router = APIRouter()

ACTIVE_STATUSES = ("pending", "queued", "running", "paused")
JOB_TYPES = ("training", "language-eval", "code-eval", "curator")


class QueueItem(BaseModel):
    id: str
    job_type: str
    priority: str
    status: str
    created_at: int
    # type-specific label fields
    label: str  # human-readable description
    model_id: Optional[str] = None


def _training_to_item(job: TrainingJobModel) -> QueueItem:
    return QueueItem(
        id=job.id,
        job_type="training",
        priority=job.priority,
        status=job.status,
        created_at=job.created_at,
        label=f"Course {job.course_id}",
        model_id=job.model_id,
    )


def _eval_to_item(job: EvalJobModel) -> QueueItem:
    return QueueItem(
        id=job.id,
        job_type=job.eval_type,  # "language-eval" or "code-eval"
        priority=job.priority,
        status=job.status,
        created_at=job.created_at,
        label=job.benchmark,
        model_id=job.model_id,
    )


def _curator_to_item(job: CuratorJobModel) -> QueueItem:
    return QueueItem(
        id=job.id,
        job_type="curator",
        priority=job.priority,
        status=job.status,
        created_at=job.created_at,
        label=f"Pipeline {job.pipeline_id}",
        model_id=None,
    )


@router.get("/queue", response_model=list[QueueItem])
async def get_queue(user=Depends(get_admin_user)):
    """Return all jobs in pending/queued/running/paused across all job types."""
    items: list[QueueItem] = []

    with get_db() as db:
        for status in ACTIVE_STATUSES:
            for job in db.query(TrainingJob).filter_by(status=status).all():
                items.append(_training_to_item(TrainingJobModel.model_validate(job)))

            for job in db.query(EvalJob).filter_by(status=status).all():
                items.append(_eval_to_item(EvalJobModel.model_validate(job)))

            for job in db.query(CuratorJob).filter_by(status=status).all():
                items.append(_curator_to_item(CuratorJobModel.model_validate(job)))

    items.sort(key=lambda x: x.created_at)
    return items


def _get_job_and_table(job_type: str, job_id: str):
    """Return (job_model, table_instance) for the given type and id, or raise 404."""
    if job_type == "training":
        job = TrainingJobs.get_job_by_id(job_id)
        return job, TrainingJobs
    elif job_type in ("language-eval", "code-eval"):
        job = EvalJobs.get_job_by_id(job_id)
        return job, EvalJobs
    elif job_type == "curator":
        job = CuratorJobs.get_job_by_id(job_id)
        return job, CuratorJobs
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown job_type '{job_type}'. Must be one of: {', '.join(JOB_TYPES)}",
        )


def _update_priority(job_type: str, job_id: str, priority: str):
    """Set priority on a job row directly via db."""
    with get_db() as db:
        if job_type == "training":
            db.query(TrainingJob).filter_by(id=job_id).update({"priority": priority, "updated_at": int(time.time())})
        elif job_type in ("language-eval", "code-eval"):
            db.query(EvalJob).filter_by(id=job_id).update({"priority": priority, "updated_at": int(time.time())})
        elif job_type == "curator":
            db.query(CuratorJob).filter_by(id=job_id).update({"priority": priority, "updated_at": int(time.time())})
        db.commit()


def _update_status_and_priority(job_type: str, job_id: str, priority: str, status: str):
    """Set both priority and status on a job row."""
    with get_db() as db:
        fields = {
            "priority": priority,
            "status": status,
            "updated_at": int(time.time()),
        }
        if job_type == "training":
            db.query(TrainingJob).filter_by(id=job_id).update(fields)
        elif job_type in ("language-eval", "code-eval"):
            db.query(EvalJob).filter_by(id=job_id).update(fields)
        elif job_type == "curator":
            db.query(CuratorJob).filter_by(id=job_id).update(fields)
        db.commit()


@router.post("/jobs/{job_type}/{job_id}/run-now")
async def run_now(job_type: str, job_id: str, user=Depends(get_admin_user)):
    """Set priority=run_now and status=queued. Admin only."""
    job, _ = _get_job_and_table(job_type, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    terminal = {"completed", "failed", "cancelled"}
    if job.status in terminal:
        raise HTTPException(status_code=400, detail="Cannot run-now a job that has already completed")

    _update_status_and_priority(job_type, job_id, priority="run_now", status="queued")
    return {"message": "Job set to run_now"}


@router.post("/jobs/{job_type}/{job_id}/promote")
async def promote(job_type: str, job_id: str, user=Depends(get_admin_user)):
    """Set priority=high. Admin only. No-op if already run_now."""
    job, _ = _get_job_and_table(job_type, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    terminal = {"completed", "failed", "cancelled"}
    if job.status in terminal:
        raise HTTPException(status_code=400, detail="Cannot promote a job that has already completed")
    if job.priority == "run_now":
        raise HTTPException(status_code=400, detail="Job is already run_now")

    _update_priority(job_type, job_id, priority="high")
    return {"message": "Job promoted to high priority"}
