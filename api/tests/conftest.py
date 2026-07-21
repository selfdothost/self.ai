# conftest.py — Root test fixtures for self.ai API backend
#
# Architecture:
#   - Uses app.dependency_overrides (not monkey-patching) for DI
#   - Truncation-based teardown (transaction rollback doesn't compose with
#     the app's own session-scoped engine)
#   - Session-scoped DB engine; function-scoped truncation
#   - Alembic migrations run at session start
#
import os

# ---------------------------------------------------------------------------
# Override env BEFORE importing any selfai_ui modules.
# Use direct assignment (not setdefault) because container env may have
# empty values that would block setdefault but still fail validation.
# ---------------------------------------------------------------------------
# Use a file-backed SQLite DB so all connections (alembic, test engine,
# app engine) see the same database. In-memory DBs are per-connection.
import tempfile as _tempfile  # noqa: E402
import time
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

_test_db_file = _tempfile.NamedTemporaryFile(prefix="selfai_test_", suffix=".db", delete=False)
_test_db_file.close()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_test_db_file.name}")
os.environ["WEBUI_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["ENV"] = "test"
os.environ["WEBUI_AUTH"] = "True"
# service_auth.py (self.ai#25) raises ServiceAuthNotConfigured if this is
# unset, which any test exercising a real llamolotl-calling code path (job
# cancel/sync, model pull, etc.) will hit even though the actual HTTP call is
# mocked (aioresponses/respx) -- minting happens before the request is made.
# Downstream ticket *validation* is self.llamolotl's own test suite's concern,
# not this one's, so any non-empty value is fine here.
os.environ["SERVICE_AUTH_SECRET"] = "test-service-auth-secret-not-for-production"

from pathlib import Path as _Path  # noqa: E402

from alembic import command as _alembic_command  # noqa: E402

# Run Alembic migrations against the test DB BEFORE importing selfai_ui.config.
# This is necessary because selfai_ui.config runs get_config() at module load
# time, which queries the config table.
from alembic.config import Config as _AlembicConfig  # noqa: E402

from selfai_ui.internal.db import get_session  # noqa: E402
from selfai_ui.utils.auth import create_token  # noqa: E402

_alembic_ini = _Path("/app/backend/selfai_ui/alembic.ini")
if not _alembic_ini.exists():
    _alembic_ini = _Path(__file__).parent.parent / "selfai_ui" / "alembic.ini"

_alembic_migrations = _Path("/app/backend/selfai_ui/migrations")
if not _alembic_migrations.exists():
    _alembic_migrations = _Path(__file__).parent.parent / "selfai_ui" / "migrations"

_alembic_cfg = _AlembicConfig(str(_alembic_ini))
_alembic_cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
_alembic_cfg.set_main_option("script_location", str(_alembic_migrations))
_alembic_command.upgrade(_alembic_cfg, "head")


# ---------------------------------------------------------------------------
# Table truncation order (respects FK constraints — children before parents)
# ---------------------------------------------------------------------------
# Seed benchmarks matching migration d5e6f7a8b9c0
_SEED_BENCHMARKS = [
    # language-eval
    ("mmlu", "language-eval"),
    ("hellaswag", "language-eval"),
    ("arc_easy", "language-eval"),
    ("arc_challenge", "language-eval"),
    ("truthfulqa_mc2", "language-eval"),
    ("winogrande", "language-eval"),
    ("gsm8k", "language-eval"),
    # code-eval
    ("humaneval", "code-eval"),
    ("mbpp", "code-eval"),
    ("apps", "code-eval"),
    ("multiple_e", "code-eval"),
    ("ds1000", "code-eval"),
    ("humanevalpack", "code-eval"),
    ("mbpp_plus", "code-eval"),
    ("humaneval_plus", "code-eval"),
]


def _reseed_benchmarks(engine):
    """Re-seed benchmark_config after truncation."""
    import uuid as _uuid

    now = int(time.time())
    with engine.connect() as conn:
        for benchmark, eval_type in _SEED_BENCHMARKS:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO benchmark_config "
                    "(id, benchmark, eval_type, max_duration_minutes, notes, created_at, updated_at) "
                    "VALUES (:id, :b, :e, 120, NULL, :t, :t)"
                ),
                {"id": str(_uuid.uuid4()), "b": benchmark, "e": eval_type, "t": now},
            )
        conn.commit()


TRUNCATION_ORDER = [
    "feedback",
    "message_reaction",
    "message",
    "chat",
    "tag",
    "knowledge_file",
    "knowledge",
    "file",
    "folder",
    "memory",
    "model",
    "prompt",
    "tool",
    "function",
    "training_job",
    "training_course",
    "eval_job",
    "curator_job",
    "job_window_slot",
    "job_window",
    "benchmark_config",
    "group",
    "auth",
    "user",
    "channel",
]


# ---------------------------------------------------------------------------
# Session-scoped: engine + schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_engine():
    """Create a test database engine pointing at the same file-backed SQLite
    DB that the real engine uses (set via DATABASE_URL above).
    Alembic already ran, so all tables exist."""
    engine = create_engine(
        os.environ["DATABASE_URL"],
        connect_args={"check_same_thread": False},
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_session_factory(test_engine):
    """SessionLocal replacement bound to the test engine."""
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Function-scoped: session + truncation
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_benchmarks(test_engine):
    """Ensure benchmark_config seed data is present for the test.

    Tests that rely on the seeded benchmarks (from Alembic migration
    d5e6f7a8b9c0) should request this fixture. Seeds are idempotent.
    """
    _reseed_benchmarks(test_engine)
    yield


@pytest.fixture
def db_session(test_session_factory, test_engine):
    """Provide a fresh DB session with truncation-based cleanup.

    Also patches get_db in model modules that import it at module level
    (needed because model classes use `with get_db() as db:` pattern and
    don't go through FastAPI's dependency injection).
    """
    session = test_session_factory()

    @contextmanager
    def _mock_get_db():
        yield session

    # Patch get_db in modules that import it directly (bypass DI)
    import selfai_ui.internal.db as db_module

    patched_modules = [db_module]
    for mod_name in [
        "selfai_ui.models.job_windows",
        "selfai_ui.utils.gpu_queue",
        "selfai_ui.models.benchmark_config",
        "selfai_ui.models.curator_jobs",
        "selfai_ui.models.training",
        "selfai_ui.models.eval_jobs",
        "selfai_ui.models.users",
        "selfai_ui.models.auths",
        "selfai_ui.models.chats",
        "selfai_ui.models.files",
        "selfai_ui.models.knowledge",
        "selfai_ui.models.tools",
        "selfai_ui.models.functions",
        "selfai_ui.routers.queue",
        "selfai_ui.routers.retrieval",
    ]:
        try:
            mod = __import__(mod_name, fromlist=["get_db"])
            if hasattr(mod, "get_db"):
                patched_modules.append(mod)
        except ImportError:
            pass

    originals = {id(m): m.get_db for m in patched_modules}
    for m in patched_modules:
        m.get_db = _mock_get_db

    yield session

    # Restore
    for m in patched_modules:
        m.get_db = originals[id(m)]
    session.close()
    # Truncate all tables in FK-safe order
    with test_engine.connect() as conn:
        for table in TRUNCATION_ORDER:
            try:
                conn.execute(text(f"DELETE FROM [{table}]"))
            except Exception:
                pass  # Table may not exist in SQLite schema
        conn.commit()


# ---------------------------------------------------------------------------
# Override get_db to use test session
# ---------------------------------------------------------------------------


@pytest.fixture
def _override_db(test_session_factory):
    """Override the app's get_db dependency to use our test session factory."""

    @contextmanager
    def _test_get_db():
        session = test_session_factory()
        try:
            yield session
        finally:
            session.close()

    from selfai_ui.main import app

    original_override = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = lambda: _test_get_db()

    # Also patch module-level get_db for model classes that call it directly
    import selfai_ui.internal.db as db_module

    original_get_db = db_module.get_db
    db_module.get_db = _test_get_db

    yield

    app.dependency_overrides = original_override
    db_module.get_db = original_get_db


# ---------------------------------------------------------------------------
# Startup task isolation (T-219)
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_startup_tasks():
    """Prevent fire-and-forget startup tasks from running during tests.

    NOT autouse — only applies when test_app fixture is requested.
    Existing model-only tests don't import main.py and don't need this.
    """
    with patch("selfai_ui.main.periodic_usage_pool_cleanup", new_callable=AsyncMock), patch(
        "selfai_ui.main._resume_crawl_jobs", new_callable=AsyncMock
    ), patch("selfai_ui.main._run_gpu_queue", new_callable=AsyncMock), patch(
        "selfai_ui.main._ensure_curator_classifier_models", new_callable=AsyncMock
    ), patch("selfai_ui.main._run_model_integrity_sweep", new_callable=AsyncMock):
        yield


# ---------------------------------------------------------------------------
# Test app / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app(_override_db, _isolate_startup_tasks):
    """Provide the FastAPI app with test overrides applied."""
    from selfai_ui.main import app

    return app


@pytest.fixture
def client(test_app):
    """HTTP test client (no real server started)."""
    with TestClient(test_app) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth helper fixtures
# ---------------------------------------------------------------------------


def _create_test_user(db_session, role="user"):
    """Insert a test user + auth record, return (user_model, jwt_token)."""
    user_id = str(uuid.uuid4())
    email = f"test-{role}-{user_id[:8]}@test.local"
    now = int(time.time())

    # Insert user row directly via SQL to avoid import-chain issues
    db_session.execute(
        text(
            "INSERT INTO [user] (id, name, email, role, profile_image_url, "
            "last_active_at, updated_at, created_at) "
            "VALUES (:id, :name, :email, :role, :pic, :now, :now, :now)"
        ),
        {
            "id": user_id,
            "name": f"Test {role.title()}",
            "email": email,
            "role": role,
            "pic": "/user.png",
            "now": now,
        },
    )
    db_session.execute(
        text("INSERT INTO [auth] (id, email, password, active) " "VALUES (:id, :email, :password, :active)"),
        {"id": user_id, "email": email, "password": "unused-in-tests", "active": True},
    )
    db_session.commit()

    token = create_token(data={"id": user_id})
    return {"id": user_id, "email": email, "role": role, "token": token}


@pytest.fixture
def test_user(db_session):
    """A test user with 'user' role. Returns dict with id, email, role, token."""
    return _create_test_user(db_session, role="user")


@pytest.fixture
def test_admin(db_session):
    """A test user with 'admin' role. Returns dict with id, email, role, token."""
    return _create_test_user(db_session, role="admin")


@pytest.fixture
def authenticated_user(client, test_user):
    """Test client pre-configured with a valid user-role Bearer token."""
    client.headers["Authorization"] = f"Bearer {test_user['token']}"
    return client


@pytest.fixture
def authenticated_admin(client, test_admin):
    """Test client pre-configured with a valid admin-role Bearer token."""
    client.headers["Authorization"] = f"Bearer {test_admin['token']}"
    return client


@pytest.fixture
def user_without_workspace_permissions(test_app, authenticated_user):
    """User-role client with ALL workspace permissions explicitly denied.

    Use for `cannot_X` tests that must verify a permission rejection,
    not an ambient `accepted-because-user-has-permission` response.
    """
    original = test_app.state.config.USER_PERMISSIONS
    denied = {
        "workspace": {
            "models": False,
            "knowledge": False,
            "prompts": False,
            "training": False,
            "tools": False,
            "evaluations": False,
        },
        "chat": {
            "file_upload": False,
            "delete": False,
            "edit": False,
            "temporary": False,
        },
    }
    test_app.state.config.USER_PERMISSIONS = denied
    yield authenticated_user
    test_app.state.config.USER_PERMISSIONS = original
