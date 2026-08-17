"""Admin-registered evaluations — the catalog half of "add an eval like a dataset" (self.ai#91).

Phase 1 (#89) made the *built-in* catalog visible: both harnesses already knew
thousands of tasks and nothing asked them, so the client shipped a hardcoded
array. This table is the other half — a place to record evaluations an admin
adds at runtime, which no existing table can hold. `benchmark_config` is a
timeout side-table (`GET`/`PUT` only, no insert) and cannot say what an added
eval points at, who added it, or whether it is built-in.

Why registration and delivery are separate facts
------------------------------------------------
The harness is a different pod with its own storage, and every PVC in this
tenant is ``ReadWriteOnce`` on ``local-path`` — node-local, single-writer. The
API pod therefore *cannot* share a filesystem with the harness and cannot write
a task file into it. Definitions are pushed over the harness control API, the
same way `routers/training.py::_upload_local_dataset` already pushes curated
JSONL to the trainer node for exactly this reason.

That makes "the admin registered it" and "the harness can run it" two different
facts that drift: a push can fail, a harness pod can be replaced, a PVC can be
lost. So :attr:`CustomEval.sync_status` is not bookkeeping — it is the honest
representation of a distributed state. A row that reads ``synced`` when the
harness has never heard of the task would be the eval-catalog version of a
silent zero, which is the failure mode Phase 1 went out of its way to avoid.

Name shadowing
--------------
A custom eval must not take the name of a built-in task: the harness resolves by
name, so a shadowing registration silently changes what a benchmark *means*, and
historical results would be attributed to the wrong thing. Built-in names are
reserved — the same posture `mcp_backends` takes toward GitOps-declared names.
Validation lives in the router, which can reach live discovery; the constraint
is recorded here because it is a property of the data, not of one code path.
"""

import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, BigInteger, Column, Text, UniqueConstraint

from selfai_ui.internal.db import Base, get_db

log = logging.getLogger(__name__)


####################
# CustomEval DB Schema
####################

#: An eval registered here is one of three shapes. The discriminator keeps the
#: table honest about the fact that "add an evaluation" means different things
#: to the two harnesses — language-eval takes a YAML task config, code-eval
#: takes a parameter appended to a generated family.
KIND_HF_TASK = "hf_task"  # language-eval: YAML generated from an HF dataset ref
KIND_RAW_YAML = "raw_yaml"  # language-eval: admin-supplied task YAML verbatim
KIND_CODE_LANGUAGE = "code_language"  # code-eval: a MultiPL-E language token

KINDS = {KIND_HF_TASK, KIND_RAW_YAML, KIND_CODE_LANGUAGE}

#: Which kinds belong to which harness. A mismatch is a validation error, not a
#: runtime surprise on the push.
KINDS_BY_EVAL_TYPE = {
    "language-eval": {KIND_HF_TASK, KIND_RAW_YAML},
    "code-eval": {KIND_CODE_LANGUAGE},
}

SYNC_PENDING = "pending"
SYNC_SYNCED = "synced"
SYNC_FAILED = "failed"


class CustomEval(Base):
    __tablename__ = "custom_eval"

    id = Column(Text, unique=True, primary_key=True)
    #: The task name the harness will expose. Unique per eval_type — the two
    #: harnesses have separate namespaces and a name may legitimately exist in
    #: both.
    name = Column(Text, nullable=False)
    eval_type = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    #: Kind-specific payload. Deliberately opaque JSON: the harnesses' task
    #: formats are theirs to define, and pinning a column per field here would
    #: mean a migration every time lm-eval's task schema grows a key.
    definition = Column(JSON, nullable=False)
    description = Column(Text, nullable=True)

    #: Delivery state — see the module docstring. `pending` is the honest
    #: initial value: the row exists, the harness has not been told yet.
    sync_status = Column(Text, nullable=False, default=SYNC_PENDING)
    #: Why the last push failed, surfaced verbatim so an admin sees the
    #: harness's own complaint rather than "sync failed".
    sync_error = Column(Text, nullable=True)
    synced_at = Column(BigInteger, nullable=True)

    owner_id = Column(Text, nullable=False)
    #: Denormalised for the listing, so the catalog can say who added what
    #: without a join. Not authoritative — owner_id is.
    owner_name = Column(Text)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)

    __table_args__ = (UniqueConstraint("name", "eval_type", name="uq_custom_eval_name_eval_type"),)


####################
# Pydantic Models
####################


class CustomEvalModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    eval_type: str
    kind: str
    definition: dict
    description: Optional[str] = None
    sync_status: str = SYNC_PENDING
    sync_error: Optional[str] = None
    synced_at: Optional[int] = None
    owner_id: str
    owner_name: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class CustomEvalForm(BaseModel):
    name: str
    eval_type: str
    kind: str
    definition: dict
    description: Optional[str] = None


class CustomEvalUpdateForm(BaseModel):
    definition: Optional[dict] = None
    description: Optional[str] = None


####################
# Validation
####################

#: Task names are used as a filename stem on the harness side and as a
#: `--tasks` CLI argument, so keep them boring: no path separators, no shell
#: metacharacters, no leading dot.
_NAME_MAX = 96


def validate_name(name: str) -> Optional[str]:
    """Return an error string, or None if the name is acceptable."""
    if not name or len(name) > _NAME_MAX:
        return f"name must be 1-{_NAME_MAX} characters"
    if not all(c.isalnum() or c in "-_." for c in name):
        return "name may contain only letters, digits, '-', '_' and '.'"
    if not name[0].isalnum():
        return "name must start with a letter or digit"
    if name != name.lower():
        return "name must be lowercase"
    return None


def validate_kind(kind: str, eval_type: str) -> Optional[str]:
    """Return an error string, or None if this kind is valid for this harness."""
    if kind not in KINDS:
        return f"unknown kind {kind!r} (expected one of: {', '.join(sorted(KINDS))})"
    allowed = KINDS_BY_EVAL_TYPE.get(eval_type)
    if allowed is None:
        return f"unknown eval_type {eval_type!r} (expected one of: {', '.join(sorted(KINDS_BY_EVAL_TYPE))})"
    if kind not in allowed:
        return f"kind {kind!r} is not valid for {eval_type} (expected one of: {', '.join(sorted(allowed))})"
    return None


####################
# Table CRUD Class
####################


class CustomEvalTable:
    def get_all(self) -> list[CustomEvalModel]:
        with get_db() as db:
            rows = db.query(CustomEval).order_by(CustomEval.eval_type, CustomEval.name).all()
            return [CustomEvalModel.model_validate(r) for r in rows]

    def get_by_eval_type(self, eval_type: str) -> list[CustomEvalModel]:
        with get_db() as db:
            rows = db.query(CustomEval).filter_by(eval_type=eval_type).order_by(CustomEval.name).all()
            return [CustomEvalModel.model_validate(r) for r in rows]

    def get_by_id(self, id: str) -> Optional[CustomEvalModel]:
        try:
            with get_db() as db:
                row = db.query(CustomEval).filter_by(id=id).first()
                return CustomEvalModel.model_validate(row) if row else None
        except Exception:
            return None

    def get_by_name(self, name: str, eval_type: str) -> Optional[CustomEvalModel]:
        try:
            with get_db() as db:
                row = db.query(CustomEval).filter_by(name=name, eval_type=eval_type).first()
                return CustomEvalModel.model_validate(row) if row else None
        except Exception:
            return None

    def insert(
        self,
        form: CustomEvalForm,
        owner_id: str,
        owner_name: Optional[str] = None,
    ) -> Optional[CustomEvalModel]:
        now = int(time.time())
        row = CustomEval(
            id=str(uuid.uuid4()),
            name=form.name,
            eval_type=form.eval_type,
            kind=form.kind,
            definition=form.definition,
            description=form.description,
            sync_status=SYNC_PENDING,
            sync_error=None,
            synced_at=None,
            owner_id=owner_id,
            owner_name=owner_name,
            created_at=now,
            updated_at=now,
        )
        try:
            with get_db() as db:
                db.add(row)
                db.commit()
                db.refresh(row)
                return CustomEvalModel.model_validate(row)
        except Exception as e:
            log.exception(e)
            return None

    def update(self, id: str, update: CustomEvalUpdateForm) -> Optional[CustomEvalModel]:
        try:
            with get_db() as db:
                fields = {"updated_at": int(time.time())}
                if update.definition is not None:
                    fields["definition"] = update.definition
                    # The harness holds a copy of the old definition; changing it
                    # here makes the two disagree until a fresh push lands.
                    fields["sync_status"] = SYNC_PENDING
                    fields["sync_error"] = None
                if update.description is not None:
                    fields["description"] = update.description
                db.query(CustomEval).filter_by(id=id).update(fields)
                db.commit()
                return self.get_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def set_sync_state(self, id: str, status: str, error: Optional[str] = None) -> Optional[CustomEvalModel]:
        """Record the outcome of a push to the harness.

        Separate from :meth:`update` because it is written by the delivery path,
        not by an admin edit — conflating them would let a failed push look like
        a definition change.
        """
        try:
            with get_db() as db:
                db.query(CustomEval).filter_by(id=id).update(
                    {
                        "sync_status": status,
                        "sync_error": error,
                        "synced_at": int(time.time()) if status == SYNC_SYNCED else None,
                    }
                )
                db.commit()
                return self.get_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def delete(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(CustomEval).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception as e:
            log.exception(e)
            return False


CustomEvals = CustomEvalTable()
