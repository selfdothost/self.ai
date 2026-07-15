"""
Model factories for test data generation.

Replaces hand-rolled `_make_*()` helpers with factory_boy-style factories.
All factories accept keyword arguments to override defaults.

Usage:
    from tests.factories import UserFactory, ChatFactory

    def test_something(db_session):
        user = UserFactory.create(db_session, role="admin")
        chat = ChatFactory.create(db_session, user_id=user.id)
"""

import time
import uuid
from typing import Any

from selfai_ui.models.auths import Auth
from selfai_ui.models.chats import Chat
from selfai_ui.models.curator_jobs import CuratorJob
from selfai_ui.models.eval_jobs import EvalJob
from selfai_ui.models.files import File
from selfai_ui.models.functions import Function
from selfai_ui.models.job_windows import JobWindow, JobWindowSlot
from selfai_ui.models.knowledge import Knowledge
from selfai_ui.models.tools import Tool
from selfai_ui.models.training import TrainingCourse, TrainingJob
from selfai_ui.models.users import User


def _now() -> int:
    return int(time.time())


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Base factory
# ---------------------------------------------------------------------------


class _Factory:
    model: Any = None

    @classmethod
    def build(cls, **overrides):
        """Build an unsaved model instance."""
        defaults = cls.defaults()
        defaults.update(overrides)
        return cls.model(**defaults)

    @classmethod
    def create(cls, db_session, **overrides):
        """Build and persist a model instance."""
        instance = cls.build(**overrides)
        db_session.add(instance)
        db_session.commit()
        db_session.refresh(instance)
        return instance

    @classmethod
    def defaults(cls) -> dict:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# User + Auth
# ---------------------------------------------------------------------------


class UserFactory(_Factory):
    model = User

    _counter = 0

    @classmethod
    def defaults(cls) -> dict:
        cls._counter += 1
        n = cls._counter
        return {
            "id": _uid(),
            "name": f"Test User {n}",
            "email": f"test{n}@test.local",
            "role": "user",
            "profile_image_url": "/user.png",
            "last_active_at": _now(),
            "updated_at": _now(),
            "created_at": _now(),
            "api_key": None,
            "settings": None,
            "info": None,
            "oauth_sub": None,
        }


class AuthFactory(_Factory):
    model = Auth

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "email": f"auth-{_uid()[:8]}@test.local",
            "password": "unused-in-tests",
            "active": True,
        }


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatFactory(_Factory):
    model = Chat

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "title": "Test Chat",
            "chat": {"messages": []},
            "created_at": _now(),
            "updated_at": _now(),
            "share_id": None,
            "archived": False,
            "pinned": False,
            "meta": {},
            "folder_id": None,
        }


# ---------------------------------------------------------------------------
# File / Knowledge
# ---------------------------------------------------------------------------


class FileFactory(_Factory):
    model = File

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "hash": None,
            "filename": "test.txt",
            "path": f"/tmp/{_uid()}_test.txt",
            "data": None,
            "meta": {"name": "test.txt", "content_type": "text/plain", "size": 11},
            "access_control": None,
            "created_at": _now(),
            "updated_at": _now(),
        }


class KnowledgeFactory(_Factory):
    model = Knowledge

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "name": "Test KB",
            "description": "Test knowledge base",
            "data": None,
            "meta": None,
            "access_control": None,
            "created_at": _now(),
            "updated_at": _now(),
        }


# ---------------------------------------------------------------------------
# Tool / Function
# ---------------------------------------------------------------------------


class ToolFactory(_Factory):
    model = Tool

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "name": "Test Tool",
            "content": "def example():\n    return 'hello'",
            "specs": [],
            "meta": {"description": "Test tool"},
            "valves": None,
            "access_control": None,
            "updated_at": _now(),
            "created_at": _now(),
        }


class FunctionFactory(_Factory):
    model = Function

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "name": "Test Function",
            "type": "filter",
            "content": "def filter_fn():\n    return True",
            "meta": {"description": "Test function"},
            "valves": None,
            "is_active": True,
            "is_global": False,
            "updated_at": _now(),
            "created_at": _now(),
        }


# ---------------------------------------------------------------------------
# Training / Eval / Curator Jobs
# ---------------------------------------------------------------------------


class TrainingCourseFactory(_Factory):
    model = TrainingCourse

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "name": "Test Course",
            "description": "Test training course",
            "data": {},
            "meta": {},
            "access_control": None,
            "created_at": _now(),
            "updated_at": _now(),
        }


class TrainingJobFactory(_Factory):
    model = TrainingJob

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "course_id": _uid(),
            "user_id": _uid(),
            "model_id": "test-model",
            "status": "pending",
            "priority": "normal",
            "scheduled_for": None,
            "llamolotl_job_id": None,
            "llamolotl_url_idx": None,
            "error_message": None,
            "meta": {},
            "created_at": _now(),
            "updated_at": _now(),
        }


class EvalJobFactory(_Factory):
    model = EvalJob

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "eval_type": "language-eval",
            "benchmark": "mmlu",
            "model_id": "test-model",
            "status": "pending",
            "priority": "normal",
            "scheduled_for": None,
            "error_message": None,
            "meta": {},
            "created_at": _now(),
            "updated_at": _now(),
        }


class CuratorJobFactory(_Factory):
    model = CuratorJob

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "user_id": _uid(),
            "pipeline_id": "test-pipeline",
            "status": "pending",
            "priority": "normal",
            "curator_job_id": None,
            "curator_url_idx": None,
            "dataset_name": None,
            "created_knowledge_id": None,
            "error_message": None,
            "scheduled_for": None,
            "meta": {"name": "Test Curator Job"},
            "created_at": _now(),
            "updated_at": _now(),
        }


# ---------------------------------------------------------------------------
# Job Windows
# ---------------------------------------------------------------------------


class JobWindowFactory(_Factory):
    model = JobWindow

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "name": "Test Window",
            "start_time": "00:00",
            "end_time": "23:59",
            "days_of_week": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
            "timezone": "UTC",
            "enabled": True,
            "created_at": _now(),
            "updated_at": _now(),
        }


class JobWindowSlotFactory(_Factory):
    model = JobWindowSlot

    @classmethod
    def defaults(cls) -> dict:
        return {
            "id": _uid(),
            "window_id": _uid(),
            "job_type": "training",
            "max_concurrent": 1,
            "priority_order": 0,
        }
