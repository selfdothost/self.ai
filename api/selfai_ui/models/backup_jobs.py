import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, BigInteger, Column, Text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import Base, get_db
from selfai_ui.models.users import UserResponse, Users

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# BackupJob DB Schema
####################


class BackupJob(Base):
    """A backup or restore run.

    Modelled on `eval_job` (same id/user_id/status/error_message/timestamps
    shape) but a sibling table, not a generalisation of it — the two share a
    shape and nothing else, and merging them buys a coupling we would have to
    unpick the first time either grows a field the other does not want.
    """

    __tablename__ = "backup_job"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)

    # "backup" | "restore"
    kind = Column(Text, default="backup")

    # pending -> running -> completed | failed | cancelled
    status = Column(Text)

    # Entity scopes included in this archive, e.g. ["chats", "prompts"].
    scopes = Column(JSON, nullable=True)

    # Where the archive landed in self.corpus. repo is always
    # SELFAI_BACKUP_REPO; path is the object key on the branch; commit is the
    # LakeFS commit id the archive was sealed into.
    archive_repo = Column(Text, nullable=True)
    archive_path = Column(Text, nullable=True)
    archive_commit = Column(Text, nullable=True)
    archive_bytes = Column(BigInteger, nullable=True)

    # Alembic revision the archive was taken at (backup) or requires (restore).
    # self.ai#93: an archive with no revision stamp is a restore hazard the
    # moment the schema advances, and boot migrations are strict now (#82).
    schema_revision = Column(Text, nullable=True)

    # Per-entity row/file counts, filled in as the job runs.
    progress = Column(JSON, nullable=True)

    error_message = Column(Text, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class BackupJobModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str

    kind: str = "backup"
    status: str
    scopes: Optional[list[str]] = None

    archive_repo: Optional[str] = None
    archive_path: Optional[str] = None
    archive_commit: Optional[str] = None
    archive_bytes: Optional[int] = None

    schema_revision: Optional[str] = None
    progress: Optional[dict] = None
    error_message: Optional[str] = None

    created_at: int
    updated_at: int


class BackupJobWithUser(BackupJobModel):
    user: Optional[UserResponse] = None


class BackupJobForm(BaseModel):
    """Scope selection for a new backup.

    An empty/omitted `scopes` means "everything the registry knows about" —
    resolved at job creation so the archive records what it actually took,
    not a wildcard that means something different when replayed later.
    """

    scopes: Optional[list[str]] = None


####################
# Table CRUD Class
####################


class BackupJobTable:
    def insert_new_job(
        self,
        user_id: str,
        scopes: list[str],
        kind: str = "backup",
        schema_revision: Optional[str] = None,
    ) -> Optional[BackupJobModel]:
        with get_db() as db:
            job = BackupJobModel(
                **{
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "kind": kind,
                    "status": "pending",
                    "scopes": scopes,
                    "schema_revision": schema_revision,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )
            try:
                result = BackupJob(**job.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return BackupJobModel.model_validate(result) if result else None
            except Exception as e:
                log.exception(e)
                return None

    def get_all_jobs(self) -> list[BackupJobWithUser]:
        with get_db() as db:
            jobs = []
            for job in db.query(BackupJob).order_by(BackupJob.created_at.desc()).all():
                user = Users.get_user_by_id(job.user_id)
                jobs.append(
                    BackupJobWithUser.model_validate(
                        {
                            **BackupJobModel.model_validate(job).model_dump(),
                            "user": user.model_dump() if user else None,
                        }
                    )
                )
            return jobs

    def get_job_by_id(self, id: str) -> Optional[BackupJobModel]:
        try:
            with get_db() as db:
                job = db.query(BackupJob).filter_by(id=id).first()
                return BackupJobModel.model_validate(job) if job else None
        except Exception:
            return None

    def update_job(self, id: str, **fields) -> Optional[BackupJobModel]:
        """Patch a job row. Unknown keys are dropped rather than raising —
        callers are background tasks, and a typo should not kill a running
        backup, but it must not silently write a column that isn't there."""
        allowed = {
            "status",
            "scopes",
            "archive_repo",
            "archive_path",
            "archive_commit",
            "archive_bytes",
            "schema_revision",
            "progress",
            "error_message",
        }
        patch = {k: v for k, v in fields.items() if k in allowed}
        dropped = set(fields) - allowed
        if dropped:
            log.warning(f"backup job {id}: ignoring unknown field(s) {sorted(dropped)}")
        if not patch:
            return self.get_job_by_id(id)
        patch["updated_at"] = int(time.time())
        try:
            with get_db() as db:
                db.query(BackupJob).filter_by(id=id).update(patch)
                db.commit()
                return self.get_job_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def get_interrupted_jobs(self) -> list[BackupJobModel]:
        """Jobs left mid-flight by a pod restart.

        A backup job holds no lock and has no resumable checkpoint, so these
        are failed at startup rather than resumed — a job row stuck on
        `running` forever is indistinguishable in the UI from one that is
        actually working, which is the failure mode this whole feature exists
        to avoid.
        """
        try:
            with get_db() as db:
                jobs = db.query(BackupJob).filter(BackupJob.status.in_(["pending", "running"])).all()
                return [BackupJobModel.model_validate(j) for j in jobs]
        except Exception as e:
            log.exception(e)
            return []

    def delete_job_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(BackupJob).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception as e:
            log.exception(e)
            return False


BackupJobs = BackupJobTable()
