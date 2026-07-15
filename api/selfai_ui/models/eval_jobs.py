import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, BigInteger, Column, Text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import Base, get_db
from selfai_ui.models.users import UserResponse, Users

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# EvalJob DB Schema
####################


class EvalJob(Base):
    __tablename__ = "eval_job"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)

    eval_type = Column(Text, default="code-eval")  # "code-eval" or "language-eval"
    benchmark = Column(Text)  # e.g. humaneval, mbpp, apps, hellaswag, mmlu, etc.
    model_id = Column(Text)  # HuggingFace model ID to evaluate

    # States: pending -> scheduled -> queued -> running -> completed | failed | cancelled
    status = Column(Text)
    priority = Column(Text, default="normal")  # "run_now" | "high" | "normal"

    # Unix timestamp — when set, the job is auto-approved at this time
    scheduled_for = Column(BigInteger, nullable=True)

    error_message = Column(Text, nullable=True)
    meta = Column(JSON, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class EvalJobModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: str
    user_id: str

    eval_type: str = "code-eval"
    benchmark: str
    model_id: str
    status: str
    priority: str = "normal"

    scheduled_for: Optional[int] = None

    error_message: Optional[str] = None
    meta: Optional[dict] = None

    created_at: int
    updated_at: int


class EvalJobWithUser(EvalJobModel):
    user: Optional[UserResponse] = None


class EvalJobForm(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    eval_type: str = "code-eval"
    benchmark: str = Field(max_length=200)
    model_id: str = Field(max_length=200)
    total_samples: Optional[int] = None
    dry_run: bool = False


class EvalJobStatusUpdate(BaseModel):
    status: str
    error_message: Optional[str] = None


####################
# Table CRUD Class
####################


class EvalJobTable:
    def insert_new_job(self, user_id: str, form_data: EvalJobForm) -> Optional[EvalJobModel]:
        with get_db() as db:
            meta = {}
            if form_data.total_samples is not None:
                meta["total_samples"] = form_data.total_samples
            if form_data.dry_run:
                meta["dry_run"] = True
            job = EvalJobModel(
                **{
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "eval_type": form_data.eval_type,
                    "benchmark": form_data.benchmark,
                    "model_id": form_data.model_id,
                    "status": "pending",
                    "meta": meta or None,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )
            try:
                result = EvalJob(**job.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return EvalJobModel.model_validate(result) if result else None
            except Exception:
                return None

    def get_all_jobs(self) -> list[EvalJobWithUser]:
        with get_db() as db:
            jobs = []
            for job in db.query(EvalJob).order_by(EvalJob.created_at.desc()).all():
                user = Users.get_user_by_id(job.user_id)
                jobs.append(
                    EvalJobWithUser.model_validate(
                        {
                            **EvalJobModel.model_validate(job).model_dump(),
                            "user": user.model_dump() if user else None,
                        }
                    )
                )
            return jobs

    def get_jobs_by_user_id(self, user_id: str) -> list[EvalJobWithUser]:
        all_jobs = self.get_all_jobs()
        return [j for j in all_jobs if j.user_id == user_id]

    def get_job_by_id(self, id: str) -> Optional[EvalJobModel]:
        try:
            with get_db() as db:
                job = db.query(EvalJob).filter_by(id=id).first()
                return EvalJobModel.model_validate(job) if job else None
        except Exception:
            return None

    def update_job_status(self, id: str, update: EvalJobStatusUpdate) -> Optional[EvalJobModel]:
        try:
            with get_db() as db:
                fields: dict = {"status": update.status, "updated_at": int(time.time())}
                if update.error_message is not None:
                    fields["error_message"] = update.error_message
                db.query(EvalJob).filter_by(id=id).update(fields)
                db.commit()
                return self.get_job_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def update_job_meta(self, id: str, meta: dict) -> Optional[EvalJobModel]:
        try:
            with get_db() as db:
                job = db.query(EvalJob).filter_by(id=id).first()
                if not job:
                    return None
                existing = job.meta or {}
                existing.update(meta)
                db.query(EvalJob).filter_by(id=id).update({"meta": existing, "updated_at": int(time.time())})
                db.commit()
                return self.get_job_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def get_next_queued_job(self) -> Optional[EvalJobModel]:
        """Return the oldest job with status 'queued', or None."""
        try:
            with get_db() as db:
                job = db.query(EvalJob).filter_by(status="queued").order_by(EvalJob.created_at.asc()).first()
                return EvalJobModel.model_validate(job) if job else None
        except Exception:
            return None

    def get_jobs_by_status(self, status: str) -> list[EvalJobModel]:
        """Return all jobs with the given status."""
        try:
            with get_db() as db:
                jobs = db.query(EvalJob).filter_by(status=status).all()
                return [EvalJobModel.model_validate(j) for j in jobs]
        except Exception:
            return []

    def has_running_job(self) -> bool:
        """Check if any job is currently running."""
        try:
            with get_db() as db:
                return db.query(EvalJob).filter_by(status="running").first() is not None
        except Exception:
            return False

    def delete_job_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(EvalJob).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False

    def get_due_scheduled_jobs(self) -> list[EvalJobModel]:
        """Return scheduled jobs whose scheduled_for time has passed."""
        now = int(time.time())
        with get_db() as db:
            rows = (
                db.query(EvalJob)
                .filter(
                    EvalJob.status == "scheduled",
                    EvalJob.scheduled_for <= now,
                )
                .order_by(EvalJob.scheduled_for.asc())
                .all()
            )
            return [EvalJobModel.model_validate(r) for r in rows]

    def update_job_scheduled_for(self, id: str, scheduled_for: Optional[int]) -> Optional[EvalJobModel]:
        try:
            with get_db() as db:
                fields: dict = {"updated_at": int(time.time())}
                if scheduled_for is not None:
                    fields["scheduled_for"] = scheduled_for
                    fields["status"] = "scheduled"
                else:
                    fields["scheduled_for"] = None
                    fields["status"] = "pending"
                db.query(EvalJob).filter_by(id=id).update(fields)
                db.commit()
                return self.get_job_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None


EvalJobs = EvalJobTable()
