import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text, JSON

from selfai_ui.internal.db import Base, get_db
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.users import Users, UserResponse
from selfai_ui.utils.access_control import has_access

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# TrainingCourse DB Schema
####################


class TrainingCourse(Base):
    __tablename__ = "training_course"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)

    name = Column(Text)
    description = Column(Text)

    # Stores: {base_config, knowledge_ids, dataset_ids, prompt_ids, advanced_config}
    data = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)

    access_control = Column(JSON, nullable=True)
    # - None: public (all users with "user" role can read)
    # - {}:   private (owner only)
    # - Custom: {read: {group_ids, user_ids}, write: {group_ids, user_ids}}

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class TrainingCourseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str

    name: str
    description: str

    data: Optional[dict] = None
    meta: Optional[dict] = None

    access_control: Optional[dict] = None

    created_at: int
    updated_at: int


class TrainingCourseUserModel(TrainingCourseModel):
    user: Optional[UserResponse] = None


class TrainingCourseForm(BaseModel):
    name: str
    description: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None


####################
# TrainingJob DB Schema
####################


class TrainingJob(Base):
    __tablename__ = "training_job"

    id = Column(Text, unique=True, primary_key=True)
    course_id = Column(Text)
    user_id = Column(Text)
    model_id = Column(Text)

    # States: pending -> scheduled -> queued -> running -> completed | failed | cancelled
    status = Column(Text)
    priority = Column(Text, default="normal")  # "run_now" | "high" | "normal"

    # Unix timestamp — when set, the job is auto-approved at this time
    scheduled_for = Column(BigInteger, nullable=True)

    # Set when job is dispatched to a llamolotl node
    llamolotl_job_id = Column(Text, nullable=True)
    llamolotl_url_idx = Column(BigInteger, nullable=True)

    error_message = Column(Text, nullable=True)
    meta = Column(JSON, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class TrainingJobModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: str
    course_id: str
    user_id: str
    model_id: str
    status: str
    priority: str = "normal"

    scheduled_for: Optional[int] = None

    llamolotl_job_id: Optional[str] = None
    llamolotl_url_idx: Optional[int] = None

    error_message: Optional[str] = None
    meta: Optional[dict] = None

    created_at: int
    updated_at: int


class TrainingJobWithDetails(TrainingJobModel):
    course: Optional[TrainingCourseModel] = None
    user: Optional[UserResponse] = None


class TrainingJobForm(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    course_id: str
    model_id: str
    scheduled_for: Optional[int] = None  # Unix timestamp for scheduled execution


class TrainingJobStatusUpdate(BaseModel):
    status: str
    llamolotl_job_id: Optional[str] = None
    llamolotl_url_idx: Optional[int] = None
    error_message: Optional[str] = None


####################
# Table CRUD Classes
####################


class TrainingCourseTable:
    def insert_new_course(
        self, user_id: str, form_data: TrainingCourseForm
    ) -> Optional[TrainingCourseModel]:
        with get_db() as db:
            course = TrainingCourseModel(
                **{
                    **form_data.model_dump(),
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )
            try:
                result = TrainingCourse(**course.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return TrainingCourseModel.model_validate(result) if result else None
            except Exception:
                return None

    def get_all_courses(self) -> list[TrainingCourseUserModel]:
        with get_db() as db:
            courses = []
            for course in (
                db.query(TrainingCourse)
                .order_by(TrainingCourse.updated_at.desc())
                .all()
            ):
                user = Users.get_user_by_id(course.user_id)
                courses.append(
                    TrainingCourseUserModel.model_validate(
                        {
                            **TrainingCourseModel.model_validate(course).model_dump(),
                            "user": user.model_dump() if user else None,
                        }
                    )
                )
            return courses

    def get_courses_by_user_id(
        self, user_id: str, permission: str = "read"
    ) -> list[TrainingCourseUserModel]:
        all_courses = self.get_all_courses()
        return [
            c
            for c in all_courses
            if c.user_id == user_id
            or has_access(user_id, permission, c.access_control)
        ]

    def get_course_by_id(self, id: str) -> Optional[TrainingCourseModel]:
        try:
            with get_db() as db:
                course = db.query(TrainingCourse).filter_by(id=id).first()
                return TrainingCourseModel.model_validate(course) if course else None
        except Exception:
            return None

    def update_course_by_id(
        self, id: str, form_data: TrainingCourseForm
    ) -> Optional[TrainingCourseModel]:
        try:
            with get_db() as db:
                db.query(TrainingCourse).filter_by(id=id).update(
                    {
                        **form_data.model_dump(),
                        "updated_at": int(time.time()),
                    }
                )
                db.commit()
                return self.get_course_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def delete_course_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(TrainingCourse).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False


class TrainingJobTable:
    def insert_new_job(
        self, user_id: str, form_data: TrainingJobForm
    ) -> Optional[TrainingJobModel]:
        with get_db() as db:
            initial_status = "scheduled" if form_data.scheduled_for else "pending"
            job = TrainingJobModel(
                **{
                    "id": str(uuid.uuid4()),
                    "course_id": form_data.course_id,
                    "user_id": user_id,
                    "model_id": form_data.model_id,
                    "status": initial_status,
                    "scheduled_for": form_data.scheduled_for,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )
            try:
                result = TrainingJob(**job.model_dump())
                db.add(result)
                db.commit()
                db.refresh(result)
                return TrainingJobModel.model_validate(result) if result else None
            except Exception:
                return None

    def get_all_jobs(self) -> list[TrainingJobWithDetails]:
        with get_db() as db:
            jobs = []
            for job in (
                db.query(TrainingJob)
                .order_by(TrainingJob.created_at.desc())
                .all()
            ):
                user = Users.get_user_by_id(job.user_id)
                course = TrainingCourses.get_course_by_id(job.course_id)
                jobs.append(
                    TrainingJobWithDetails.model_validate(
                        {
                            **TrainingJobModel.model_validate(job).model_dump(),
                            "user": user.model_dump() if user else None,
                            "course": course.model_dump() if course else None,
                        }
                    )
                )
            return jobs

    def get_jobs_by_user_id(self, user_id: str) -> list[TrainingJobWithDetails]:
        all_jobs = self.get_all_jobs()
        return [j for j in all_jobs if j.user_id == user_id]

    def get_job_by_id(self, id: str) -> Optional[TrainingJobModel]:
        try:
            with get_db() as db:
                job = db.query(TrainingJob).filter_by(id=id).first()
                return TrainingJobModel.model_validate(job) if job else None
        except Exception:
            return None

    def update_job_status(
        self, id: str, update: TrainingJobStatusUpdate
    ) -> Optional[TrainingJobModel]:
        try:
            with get_db() as db:
                fields: dict = {"status": update.status, "updated_at": int(time.time())}
                if update.llamolotl_job_id is not None:
                    fields["llamolotl_job_id"] = update.llamolotl_job_id
                if update.llamolotl_url_idx is not None:
                    fields["llamolotl_url_idx"] = update.llamolotl_url_idx
                if update.error_message is not None:
                    fields["error_message"] = update.error_message
                db.query(TrainingJob).filter_by(id=id).update(fields)
                db.commit()
                return self.get_job_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None

    def delete_job_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(TrainingJob).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False


    def get_due_scheduled_jobs(self) -> list[TrainingJobModel]:
        """Return scheduled jobs whose scheduled_for time has passed."""
        now = int(time.time())
        with get_db() as db:
            rows = (
                db.query(TrainingJob)
                .filter(
                    TrainingJob.status == "scheduled",
                    TrainingJob.scheduled_for <= now,
                )
                .order_by(TrainingJob.scheduled_for.asc())
                .all()
            )
            return [TrainingJobModel.model_validate(r) for r in rows]

    def update_job_scheduled_for(
        self, id: str, scheduled_for: Optional[int]
    ) -> Optional[TrainingJobModel]:
        try:
            with get_db() as db:
                fields: dict = {"updated_at": int(time.time())}
                if scheduled_for is not None:
                    fields["scheduled_for"] = scheduled_for
                    fields["status"] = "scheduled"
                else:
                    fields["scheduled_for"] = None
                    fields["status"] = "pending"
                db.query(TrainingJob).filter_by(id=id).update(fields)
                db.commit()
                return self.get_job_by_id(id=id)
        except Exception as e:
            log.exception(e)
            return None


TrainingCourses = TrainingCourseTable()
TrainingJobs = TrainingJobTable()
