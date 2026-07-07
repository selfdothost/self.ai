import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Boolean, Column, Integer, Text

from selfai_ui.internal.db import Base, get_db
from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# JobWindow DB Schema
####################


class JobWindow(Base):
    __tablename__ = "job_window"

    id = Column(Text, unique=True, primary_key=True)
    name = Column(Text)
    notes = Column(Text, nullable=True)
    start_at = Column(BigInteger)
    end_at = Column(BigInteger)
    preferred_job_type = Column(Text)
    enabled = Column(Boolean, default=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class JobWindowSlot(Base):
    __tablename__ = "job_window_slot"

    id = Column(Text, unique=True, primary_key=True)
    window_id = Column(Text)
    job_type = Column(Text)
    max_concurrent = Column(Integer, default=1)
    min_remaining_minutes = Column(Integer, default=0)


####################
# Pydantic Models
####################


class JobWindowSlotModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    window_id: str
    job_type: str
    max_concurrent: int = 1
    min_remaining_minutes: int = 0


class JobWindowModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    notes: Optional[str] = None
    start_at: int
    end_at: int
    preferred_job_type: str
    enabled: bool = True
    created_at: int
    updated_at: int


class JobWindowWithSlots(JobWindowModel):
    slots: list[JobWindowSlotModel] = []
    status: str = "upcoming"  # derived: upcoming | active | completed


class JobWindowSlotForm(BaseModel):
    id: Optional[str] = None
    job_type: str
    max_concurrent: int = 1
    min_remaining_minutes: int = 0


class JobWindowForm(BaseModel):
    id: Optional[str] = None
    name: str
    notes: Optional[str] = None
    start_at: int
    end_at: int
    preferred_job_type: str
    enabled: bool = True
    slots: list[JobWindowSlotForm] = []


####################
# Table CRUD Class
####################


def _window_status(w: JobWindow) -> str:
    now = int(time.time())
    if w.start_at > now:
        return "upcoming"
    if w.start_at <= now <= w.end_at:
        return "active" if w.enabled else "disabled"
    return "completed"


class JobWindowTable:
    def insert_new_window(self, form_data: JobWindowForm) -> Optional[JobWindowWithSlots]:
        with get_db() as db:
            now = int(time.time())
            window_id = form_data.id or str(uuid.uuid4())
            window = JobWindow(
                id=window_id,
                name=form_data.name,
                notes=form_data.notes,
                start_at=form_data.start_at,
                end_at=form_data.end_at,
                preferred_job_type=form_data.preferred_job_type,
                enabled=form_data.enabled,
                created_at=now,
                updated_at=now,
            )
            try:
                db.add(window)
                db.flush()
                for slot_form in form_data.slots:
                    db.add(JobWindowSlot(
                        id=slot_form.id or str(uuid.uuid4()),
                        window_id=window_id,
                        job_type=slot_form.job_type,
                        max_concurrent=slot_form.max_concurrent,
                        min_remaining_minutes=slot_form.min_remaining_minutes,
                    ))
                db.commit()
                db.refresh(window)
                slots = db.query(JobWindowSlot).filter_by(window_id=window_id).all()
                return JobWindowWithSlots(
                    **JobWindowModel.model_validate(window).model_dump(),
                    slots=[JobWindowSlotModel.model_validate(s) for s in slots],
                    status=_window_status(window),
                )
            except Exception as e:
                log.exception(e)
                return None

    def get_all_windows(self) -> list[JobWindowWithSlots]:
        with get_db() as db:
            windows = db.query(JobWindow).order_by(JobWindow.start_at.desc()).all()
            result = []
            for w in windows:
                slots = db.query(JobWindowSlot).filter_by(window_id=w.id).all()
                result.append(JobWindowWithSlots(
                    **JobWindowModel.model_validate(w).model_dump(),
                    slots=[JobWindowSlotModel.model_validate(s) for s in slots],
                    status=_window_status(w),
                ))
            return result

    def get_window_by_id(self, id: str) -> Optional[JobWindowWithSlots]:
        try:
            with get_db() as db:
                window = db.query(JobWindow).filter_by(id=id).first()
                if not window:
                    return None
                slots = db.query(JobWindowSlot).filter_by(window_id=id).all()
                return JobWindowWithSlots(
                    **JobWindowModel.model_validate(window).model_dump(),
                    slots=[JobWindowSlotModel.model_validate(s) for s in slots],
                    status=_window_status(window),
                )
        except Exception:
            return None

    def update_window(self, id: str, form_data: dict) -> Optional[JobWindowWithSlots]:
        try:
            with get_db() as db:
                window = db.query(JobWindow).filter_by(id=id).first()
                if not window:
                    return None
                for key in ("name", "notes", "start_at", "end_at", "preferred_job_type", "enabled"):
                    if key in form_data:
                        setattr(window, key, form_data[key])
                window.updated_at = int(time.time())
                if "slots" in form_data:
                    db.query(JobWindowSlot).filter_by(window_id=id).delete()
                    for slot in form_data["slots"]:
                        db.add(JobWindowSlot(
                            id=slot.get("id") or str(uuid.uuid4()),
                            window_id=id,
                            job_type=slot["job_type"],
                            max_concurrent=slot.get("max_concurrent", 1),
                            min_remaining_minutes=slot.get("min_remaining_minutes", 0),
                        ))
                db.commit()
                db.refresh(window)
                slots = db.query(JobWindowSlot).filter_by(window_id=id).all()
                return JobWindowWithSlots(
                    **JobWindowModel.model_validate(window).model_dump(),
                    slots=[JobWindowSlotModel.model_validate(s) for s in slots],
                    status=_window_status(window),
                )
        except Exception as e:
            log.exception(e)
            return None

    def delete_window(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(JobWindowSlot).filter_by(window_id=id).delete()
                db.query(JobWindow).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False

    def get_active_window(self) -> Optional[JobWindowWithSlots]:
        """Return the most recently started currently-active window, or None."""
        now = int(time.time())
        try:
            with get_db() as db:
                window = (
                    db.query(JobWindow)
                    .filter(
                        JobWindow.start_at <= now,
                        JobWindow.end_at >= now,
                        JobWindow.enabled.is_(True),
                    )
                    .order_by(JobWindow.start_at.desc())
                    .first()
                )
                if not window:
                    return None
                slots = db.query(JobWindowSlot).filter_by(window_id=window.id).all()
                return JobWindowWithSlots(
                    **JobWindowModel.model_validate(window).model_dump(),
                    slots=[JobWindowSlotModel.model_validate(s) for s in slots],
                    status="active",
                )
        except Exception:
            return None


JobWindows = JobWindowTable()
