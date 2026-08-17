"""Model lines and versions — self.ai#131.

A **line** is the durable identity a user thinks of as "the model": the thing
that appears once in a picker and has a history. A **version** is one point on
that line, addressable by a self.corpus commit, in one of two kinds:

- ``base``    — a GGUF, whether pulled, quantized, or produced by a publish
- ``adapter`` — a LoRA fitted on top of a base

Both tables are additive. A ``model`` row that belongs to no line keeps serving
exactly as it does today; nothing here constrains that table.

Ordering is carried by an explicit ``sequence``, not by ``created_at`` — two
versions written in the same second must still have a defined order, and a
history that reorders itself under a clock change is not a history.

Decision record: selfai/gitlab-profile
context/treasuremaps/2026-08-11-tokenization-studio.md, Decision 5.
Kit: context/kits/cavekit-model-versioning.md R1, R2.
"""

import logging
import time
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, BigInteger, Column, Text

from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.internal.db import Base, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


VERSION_KINDS = ("base", "adapter")

# Every JSON column here is `sa.JSON`, NOT `internal.db.JSONField`, and the two
# are not interchangeable. JSONField is a TypeDecorator over Text that
# json.loads() on read; the migration that created these tables declared real
# `sa.JSON()` columns. On SQLite that mismatch is invisible — sa.JSON is stored
# as TEXT and the driver returns a string, so json.loads() succeeds — but on
# PostgreSQL psycopg returns a dict and json.loads(dict) raises
# "the JSON object must be str, bytes or bytearray, not dict".
#
# That shipped: every test in this repo runs on SQLite, so nothing failed until
# a dict was written on the live Postgres. Keep model and migration agreeing;
# tests/test_model_versions_schema.py pins it.


class ModelVersionError(Exception):
    """A version or line write that would produce an unwalkable history."""


####################
# DB Schema
####################


class ModelLine(Base):
    """A model line: stable identity, ordered version history, current pointer.

    Attributes:
        id (Text): Stable identifier; survives every version transition,
            including a publish that replaces the base GGUF entirely.
        name (Text): Human-readable display name.
        user_id (Text): Owner.
        corpus_repo (Text): The self.corpus repo backing this line
            (utils/self_corpus.repo_id_for_model_line).
        current_version_id (Text, optional): The version resolution and the
            model surface read. Null only before the first version lands.
        access_control (JSON, optional): Same convention as models.models.Model
            — None is public, {} is private-to-owner, otherwise read/write
            group_ids/user_ids.
        meta (JSON, optional): Free-form; carries the revert audit trail.
    """

    __tablename__ = "model_line"

    id = Column(Text, unique=True, primary_key=True)
    name = Column(Text)
    user_id = Column(Text)
    corpus_repo = Column(Text, nullable=True)
    current_version_id = Column(Text, nullable=True)
    access_control = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class ModelVersion(Base):
    """One point on a line, backed by a self.corpus commit.

    Attributes:
        line_id (Text): The line this version belongs to.
        sequence (BigInteger): Explicit position, 1-based, unique per line.
        kind (Text): "base" or "adapter"; constrained at write time by
            ModelVersionsTable.insert_new_version, not only in Pydantic.
        corpus_commit_id (Text): The commit this version resolves to. Never
            written before that commit succeeds.
        parent_version_id (Text, optional): The previous version on this line;
            null for the line's first version.
        base_version_id (Text, optional): For an ``adapter``, the base it was
            fitted against — which is not necessarily its parent, since the
            parent may itself be an adapter.
        produced_by (JSON, optional): {"job_kind": ..., "job_id": ...} —
            structured, so provenance is not parsed out of a filename.
        published_by (Text, optional): The user who published it.
        artifact_ref (Text, optional): Path/name of the artifact within the
            line's repo.
    """

    __tablename__ = "model_version"

    id = Column(Text, unique=True, primary_key=True)
    line_id = Column(Text)
    sequence = Column(BigInteger)
    kind = Column(Text)
    corpus_commit_id = Column(Text)
    parent_version_id = Column(Text, nullable=True)
    base_version_id = Column(Text, nullable=True)
    produced_by = Column(JSON, nullable=True)
    published_by = Column(Text, nullable=True)
    artifact_ref = Column(Text, nullable=True)
    meta = Column(JSON, nullable=True)
    created_at = Column(BigInteger)


####################
# Pydantic models
####################


class ModelLineModel(BaseModel):
    id: str
    name: str
    user_id: str
    corpus_repo: Optional[str] = None
    current_version_id: Optional[str] = None
    access_control: Optional[dict] = None
    meta: Optional[dict] = None
    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


class ModelVersionModel(BaseModel):
    id: str
    line_id: str
    sequence: int
    kind: Literal["base", "adapter"]
    corpus_commit_id: str
    parent_version_id: Optional[str] = None
    base_version_id: Optional[str] = None
    produced_by: Optional[dict] = None
    published_by: Optional[str] = None
    artifact_ref: Optional[str] = None
    meta: Optional[dict] = None
    created_at: int

    model_config = ConfigDict(from_attributes=True)


class ModelLineForm(BaseModel):
    name: str
    corpus_repo: Optional[str] = None
    access_control: Optional[dict] = None
    meta: Optional[dict] = None


class ModelVersionForm(BaseModel):
    kind: Literal["base", "adapter"]
    corpus_commit_id: str
    parent_version_id: Optional[str] = None
    base_version_id: Optional[str] = None
    produced_by: Optional[dict] = None
    published_by: Optional[str] = None
    artifact_ref: Optional[str] = None
    meta: Optional[dict] = None


####################
# Tables
####################


class ModelLinesTable:
    def insert_new_line(self, user_id: str, form_data: ModelLineForm) -> Optional[ModelLineModel]:
        line_id = str(uuid.uuid4())
        now = int(time.time())
        line = ModelLineModel(
            **{
                **form_data.model_dump(),
                "id": line_id,
                "user_id": user_id,
                "current_version_id": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        try:
            with get_db() as db:
                result = ModelLine(**line.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return ModelLineModel.model_validate(result)
        except Exception as e:
            log.error(f"insert_new_line failed: {e}")
            return None

    def get_lines(self) -> list[ModelLineModel]:
        with get_db() as db:
            return [ModelLineModel.model_validate(line) for line in db.query(ModelLine).all()]

    def get_lines_by_user_id(self, user_id: str) -> list[ModelLineModel]:
        with get_db() as db:
            return [
                ModelLineModel.model_validate(line)
                for line in db.query(ModelLine).filter_by(user_id=user_id).all()
            ]

    def get_line_by_id(self, line_id: str) -> Optional[ModelLineModel]:
        try:
            with get_db() as db:
                line = db.query(ModelLine).filter_by(id=line_id).first()
                return ModelLineModel.model_validate(line) if line else None
        except Exception:
            return None

    def get_line_by_corpus_repo(self, corpus_repo: str) -> Optional[ModelLineModel]:
        with get_db() as db:
            line = db.query(ModelLine).filter_by(corpus_repo=corpus_repo).first()
            return ModelLineModel.model_validate(line) if line else None

    def update_corpus_repo(self, line_id: str, corpus_repo: str) -> Optional[ModelLineModel]:
        """Attach a line to its self.corpus repo.

        Split from creation because the derived repo id needs the line id,
        which only exists once the row does.
        """
        try:
            with get_db() as db:
                db.query(ModelLine).filter_by(id=line_id).update(
                    {"corpus_repo": corpus_repo, "updated_at": int(time.time())}
                )
                db.commit()
            return self.get_line_by_id(line_id)
        except Exception as e:
            log.error(f"update_corpus_repo failed for line {line_id}: {e}")
            return None

    def set_current_version(
        self, line_id: str, version_id: str, moved_by: Optional[str] = None
    ) -> Optional[ModelLineModel]:
        """Move a line's current-version pointer.

        This is the whole of a revert: no artifact is rebuilt and no commit is
        made. The move is recorded in the line's meta so "who reverted us to
        version 3, and when" is answerable afterwards.
        """
        version = ModelVersions.get_version_by_id(version_id)
        if version is None or version.line_id != line_id:
            raise ModelVersionError(f"version {version_id} does not belong to line {line_id}")

        line = self.get_line_by_id(line_id)
        if line is None:
            raise ModelVersionError(f"line {line_id} not found")

        now = int(time.time())
        meta = dict(line.meta or {})
        history = list(meta.get("version_moves") or [])
        history.append({"to": version_id, "from": line.current_version_id, "at": now, "by": moved_by})
        meta["version_moves"] = history

        try:
            with get_db() as db:
                db.query(ModelLine).filter_by(id=line_id).update(
                    {"current_version_id": version_id, "meta": meta, "updated_at": now}
                )
                db.commit()
            return self.get_line_by_id(line_id)
        except Exception as e:
            log.error(f"set_current_version failed for line {line_id}: {e}")
            return None

    def delete_line_by_id(self, line_id: str) -> bool:
        """Delete a line that has no versions.

        Deletion is **refused** while versions exist rather than cascading: a
        cascade would silently destroy the commit-backed provenance that is the
        entire point of the record, and a line whose versions outlived it is an
        orphan set with no walkable parent. Callers that mean it delete the
        versions explicitly first.
        """
        if ModelVersions.get_versions_by_line(line_id):
            raise ModelVersionError(f"line {line_id} still has versions; delete them explicitly first")
        try:
            with get_db() as db:
                db.query(ModelLine).filter_by(id=line_id).delete()
                db.commit()
                return True
        except Exception as e:
            log.error(f"delete_line_by_id failed for line {line_id}: {e}")
            return False


class ModelVersionsTable:
    def validate_append(
        self,
        line_id: str,
        kind: str,
        parent_version_id: Optional[str] = None,
        base_version_id: Optional[str] = None,
    ) -> None:
        """Check everything an append needs except the commit itself.

        Split out from insert_new_version so a caller can run it *before*
        paying for a self.corpus commit. Committing first and only then
        discovering the parent is on another line would leave a commit in the
        repo with no version record pointing at it — recoverable, but only by
        hand, and invisible until someone walks the line.

        Guards: the line exists; the kind is one of VERSION_KINDS; the parent
        exists and is on this line; a first version has no parent and a later
        one must name one; an adapter's base is a `base` on this line.
        """
        line = ModelLines.get_line_by_id(line_id)
        if line is None:
            raise ModelVersionError(f"line {line_id} not found")

        if kind not in VERSION_KINDS:
            raise ModelVersionError(f"invalid version kind {kind!r}; expected one of {VERSION_KINDS}")

        existing = self.get_versions_by_line(line_id)

        if parent_version_id is not None:
            parent = self.get_version_by_id(parent_version_id)
            if parent is None:
                raise ModelVersionError(f"parent version {parent_version_id} not found")
            if parent.line_id != line_id:
                raise ModelVersionError(f"parent version {parent_version_id} belongs to a different line")
        elif existing:
            raise ModelVersionError(f"line {line_id} already has versions; a later version must name its parent")

        if base_version_id is not None:
            base = self.get_version_by_id(base_version_id)
            if base is None or base.line_id != line_id:
                raise ModelVersionError(f"base version {base_version_id} is not on line {line_id}")
            if base.kind != "base":
                raise ModelVersionError(f"base version {base_version_id} is not of kind 'base'")

    def insert_new_version(self, line_id: str, form_data: ModelVersionForm) -> ModelVersionModel:
        """Append a version to a line.

        Raises ModelVersionError rather than returning None on an invalid
        write: an append that silently no-ops would leave the caller holding a
        commit with no record of it.
        """
        self.validate_append(
            line_id,
            form_data.kind,
            parent_version_id=form_data.parent_version_id,
            base_version_id=form_data.base_version_id,
        )

        if not form_data.corpus_commit_id:
            raise ModelVersionError("a version must carry the self.corpus commit id backing it")

        existing = self.get_versions_by_line(line_id)
        parent_id = form_data.parent_version_id
        version_id = str(uuid.uuid4())
        # A self-parent is unreachable by construction (the id is minted here
        # and the parent must already exist), and a cycle needs two versions to
        # point at each other — which the parent-must-exist rule also forbids,
        # since the second one cannot be written before the first. The check
        # below is the belt to that braces: it catches a caller passing the id
        # it is about to receive.
        if parent_id == version_id:
            raise ModelVersionError("a version cannot be its own parent")

        sequence = (max(v.sequence for v in existing) + 1) if existing else 1
        version = ModelVersionModel(
            **{
                **form_data.model_dump(),
                "id": version_id,
                "line_id": line_id,
                "sequence": sequence,
                "created_at": int(time.time()),
            }
        )
        try:
            with get_db() as db:
                result = ModelVersion(**version.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return ModelVersionModel.model_validate(result)
        except Exception as e:
            log.error(f"insert_new_version failed for line {line_id}: {e}")
            raise ModelVersionError(f"could not write version for line {line_id}: {e}") from e

    def get_versions_by_line(self, line_id: str) -> list[ModelVersionModel]:
        """A line's history, in explicit sequence order."""
        with get_db() as db:
            return [
                ModelVersionModel.model_validate(v)
                for v in db.query(ModelVersion).filter_by(line_id=line_id).order_by(ModelVersion.sequence).all()
            ]

    def get_version_by_id(self, version_id: str) -> Optional[ModelVersionModel]:
        try:
            with get_db() as db:
                version = db.query(ModelVersion).filter_by(id=version_id).first()
                return ModelVersionModel.model_validate(version) if version else None
        except Exception:
            return None

    def delete_versions_by_line(self, line_id: str) -> bool:
        try:
            with get_db() as db:
                db.query(ModelVersion).filter_by(line_id=line_id).delete()
                db.commit()
                return True
        except Exception as e:
            log.error(f"delete_versions_by_line failed for line {line_id}: {e}")
            return False


####################
# Publish jobs (R6)
####################


class PublishJob(Base):
    """One publish: merge a line's accumulated adapters into its base.

    Its own job kind, never a side effect of a bake. Field shape follows
    `models/training.py`'s TrainingJob and the canonical status vocabulary
    (`cavekit-ui-job-state-machines.md` R1) so the admin surface can treat it
    like every other job.

    Attributes:
        base_version_id (Text): The base this publish started from.
        adapter_version_ids (JSON): Every adapter being merged, recorded at
            creation so the job says what it intended even if it fails.
        result_version_id (Text, optional): The `base` version it produced.
            Null unless the job completed.
    """

    __tablename__ = "publish_job"

    id = Column(Text, unique=True, primary_key=True)
    line_id = Column(Text)
    user_id = Column(Text)

    base_version_id = Column(Text)
    adapter_version_ids = Column(JSON, nullable=True)
    output_name = Column(Text, nullable=True)
    quant_type = Column(Text, nullable=True)

    status = Column(Text)
    priority = Column(Text, default="normal")
    scheduled_for = Column(BigInteger, nullable=True)

    llamolotl_job_id = Column(Text, nullable=True)
    llamolotl_url_idx = Column(BigInteger, nullable=True)

    result_version_id = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    meta = Column(JSON, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class PublishJobModel(BaseModel):
    id: str
    line_id: str
    user_id: str
    base_version_id: str
    adapter_version_ids: Optional[list] = None
    output_name: Optional[str] = None
    quant_type: Optional[str] = None
    status: str
    priority: str = "normal"
    scheduled_for: Optional[int] = None
    llamolotl_job_id: Optional[str] = None
    llamolotl_url_idx: Optional[int] = None
    result_version_id: Optional[str] = None
    error_message: Optional[str] = None
    meta: Optional[dict] = None
    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


class PublishJobForm(BaseModel):
    output_name: Optional[str] = None
    quant_type: Optional[str] = None
    priority: str = "normal"
    #: Epoch seconds. A publish with a schedule waits for it before it is even
    #: a candidate for a window, matching training and curator jobs.
    scheduled_for: Optional[int] = None


class PublishJobsTable:
    def insert_new_job(
        self,
        line_id: str,
        user_id: str,
        base_version_id: str,
        adapter_version_ids: list,
        form_data: PublishJobForm,
    ) -> PublishJobModel:
        now = int(time.time())
        job = PublishJobModel(
            **{
                **form_data.model_dump(),
                "id": str(uuid.uuid4()),
                "line_id": line_id,
                "user_id": user_id,
                "base_version_id": base_version_id,
                "adapter_version_ids": adapter_version_ids,
                # Queued, not running: the GPU work happens when the queue says
                # so (self.ai#136). A merge holds the fp16 base on the card, so
                # dispatching from the request path raced every other consumer.
                "status": "scheduled" if form_data.scheduled_for else "queued",
                "created_at": now,
                "updated_at": now,
            }
        )
        try:
            with get_db() as db:
                result = PublishJob(**job.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return PublishJobModel.model_validate(result)
        except Exception as e:
            log.error(f"insert_new_job failed for line {line_id}: {e}")
            raise ModelVersionError(f"could not create a publish job for line {line_id}: {e}") from e

    def get_jobs_by_line(self, line_id: str) -> list[PublishJobModel]:
        with get_db() as db:
            return [
                PublishJobModel.model_validate(job)
                for job in db.query(PublishJob)
                .filter_by(line_id=line_id)
                .order_by(PublishJob.created_at.desc())
                .all()
            ]

    def get_job_by_id(self, job_id: str) -> Optional[PublishJobModel]:
        with get_db() as db:
            job = db.query(PublishJob).filter_by(id=job_id).first()
            return PublishJobModel.model_validate(job) if job else None

    def record_wait_reason(self, job_id: str, reason: Optional[str]) -> None:
        """Say why a queued publish has not started, without churning the row.

        Written only when it changes: a publish waiting through many queue
        ticks would otherwise rewrite the same sentence every tick and bury
        whatever it was actually waiting for under its own noise.
        """
        job = self.get_job_by_id(job_id)
        if job is None or (job.meta or {}).get("waiting_reason") == reason:
            return
        try:
            with get_db() as db:
                db.query(PublishJob).filter_by(id=job_id).update(
                    {"meta": {**(job.meta or {}), "waiting_reason": reason}, "updated_at": int(time.time())}
                )
                db.commit()
        except Exception as e:
            log.warning(f"record_wait_reason failed for publish job {job_id}: {e}")

    def get_due_scheduled_jobs(self) -> list[PublishJobModel]:
        """Scheduled publishes whose time has come."""
        now = int(time.time())
        with get_db() as db:
            return [
                PublishJobModel.model_validate(job)
                for job in db.query(PublishJob)
                .filter(PublishJob.status == "scheduled", PublishJob.scheduled_for <= now)
                .all()
            ]

    def get_jobs_by_status(self, status: str) -> list[PublishJobModel]:
        with get_db() as db:
            return [
                PublishJobModel.model_validate(job)
                for job in db.query(PublishJob).filter_by(status=status).order_by(PublishJob.created_at).all()
            ]

    def mark_dispatched(
        self, job_id: str, llamolotl_job_id: str, llamolotl_url_idx: int = 0
    ) -> Optional[PublishJobModel]:
        try:
            with get_db() as db:
                db.query(PublishJob).filter_by(id=job_id).update(
                    {
                        "status": "running",
                        "llamolotl_job_id": llamolotl_job_id,
                        "llamolotl_url_idx": llamolotl_url_idx,
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
            return self.get_job_by_id(job_id)
        except Exception as e:
            log.error(f"mark_dispatched failed for publish job {job_id}: {e}")
            return None

    def update_status(
        self,
        job_id: str,
        status: str,
        error_message: Optional[str] = None,
        result_version_id: Optional[str] = None,
    ) -> Optional[PublishJobModel]:
        try:
            with get_db() as db:
                db.query(PublishJob).filter_by(id=job_id).update(
                    {
                        "status": status,
                        "error_message": error_message,
                        "result_version_id": result_version_id,
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
            return self.get_job_by_id(job_id)
        except Exception as e:
            log.error(f"update_status failed for publish job {job_id}: {e}")
            return None


ModelLines = ModelLinesTable()
ModelVersions = ModelVersionsTable()
PublishJobs = PublishJobsTable()
