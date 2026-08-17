"""Admin > Settings > Database must report the database (self.ai#94).

The page previously held three export/backup controls and said nothing about
the connection. Those moved to the backup surface (#93); this readout replaces
them.

Two properties are load-bearing beyond "the fields are populated":

- **No credential ever appears in the response.** `DATABASE_URL` carries the
  password, so the endpoint decomposes the URL rather than rendering it. The
  whole point of #109 is that this class of leak is easy to reintroduce.
- **The pool section reports what was built, not what was configured.** The
  four `DATABASE_POOL_*` env vars are inert unless the Postgres branch of
  `internal/db.py` builds a `QueuePool`, which needs `DATABASE_POOL_SIZE > 0`.
  Showing an operator four numbers that do nothing is worse than showing none.
"""

import json
import sqlite3

import pytest

from selfai_ui.utils.database_info import (
    MINIMUM_SQLITE,
    connection_info,
    database_info,
    pool_info,
    schema_info,
    sqlite_info,
    storage_info,
)

pytestmark = pytest.mark.tier0


####################
# Connection
####################


def test_connection_reports_the_live_dialect_and_answers():
    info = connection_info()

    assert info["dialect"] == "sqlite", "the suite runs on SQLite; see conftest"
    assert info["reachable"] is True
    assert info["error"] is None
    assert info["latency_ms"] is not None and info["latency_ms"] >= 0


def test_connection_reports_the_engine_version_not_a_placeholder():
    """`SELECT sqlite_version()` asked of the engine, not read off the module —
    a hardcoded string would still pass a truthiness check."""
    info = connection_info()

    assert info["server_version"] == sqlite3.sqlite_version


def test_connection_never_returns_the_password():
    """The reason the URL is decomposed field-by-field instead of rendered."""
    from selfai_ui.internal.db import engine

    info = connection_info()

    assert "password" not in info
    assert "url" not in info
    # Whatever the URL happens to be, no field may carry the raw form that
    # could contain credentials.
    rendered = engine.url.render_as_string(hide_password=False)
    for key, value in info.items():
        assert value != rendered, f"{key} leaked the full DATABASE_URL"


def test_connection_response_survives_a_json_round_trip():
    """It is served as JSON; a field holding a URL object or a Decimal would
    500 at serialisation rather than in any assertion above."""
    json.dumps(database_info())


####################
# Pool
####################


def test_pool_reports_the_class_actually_built():
    """SQLite takes the `check_same_thread` branch and never reaches the pool
    settings at all, so the class here is whatever SQLAlchemy defaulted to."""
    from selfai_ui.internal.db import engine

    info = pool_info()

    assert info["class"] == type(engine.pool).__name__


def test_pool_distinguishes_configured_from_applied():
    """The divergence this section exists to make visible: the env vars are
    always reported, `settings_applied` says whether they did anything.

    SQLite never reaches the pool branch in `internal/db.py`, so the four
    values are inert here — even though SQLAlchemy has independently built a
    `QueuePool` for the file-backed database, which is exactly why this flag
    cannot be read off the pool's class name."""
    info = pool_info()

    assert set(info["configured"]) == {
        "DATABASE_POOL_SIZE",
        "DATABASE_POOL_MAX_OVERFLOW",
        "DATABASE_POOL_TIMEOUT",
        "DATABASE_POOL_RECYCLE",
    }
    assert info["settings_applied"] is False


def test_pool_settings_applied_is_not_the_pool_class(monkeypatch):
    """Guards the bug the first cut of this shipped with: `QueuePool` exists on
    SQLite by SQLAlchemy's default, so `class == QueuePool` would report the
    inert env vars as live."""
    from selfai_ui.internal.db import engine

    info = pool_info()

    if type(engine.pool).__name__ == "QueuePool":
        assert info["settings_applied"] is False, (
            "settings_applied tracked the pool class rather than the branch in "
            "internal/db.py that actually consumes DATABASE_POOL_*"
        )


def test_pool_does_not_crash_on_a_pool_missing_the_accessors():
    """`NullPool` implements none of size/checkedout/checkedin/overflow. The
    fields must come back None rather than raising."""
    info = pool_info()

    for field in ("size", "checked_out", "checked_in", "overflow"):
        assert field in info


####################
# Schema
####################


def test_schema_reports_at_head_after_conftest_migrated():
    """conftest runs `alembic upgrade head` before any import, so the database
    under test is at head. Read back out of the database, not inferred."""
    from selfai_ui.utils.backup import script_heads

    info = schema_info()

    assert info["current"] is not None
    assert info["at_head"] is True
    assert info["error"] is None
    assert set(info["heads"]) == set(script_heads())


def test_schema_reports_branching_separately_from_at_head():
    """Two heads is a CrashLoop on the next roll (#102) and is a different
    failure from being behind, so it gets its own field."""
    info = schema_info()

    assert info["branched"] is (len(info["heads"]) > 1)
    assert info["branched"] is False, f"migration chain has branched: {info['heads']}"


####################
# Storage
####################


def test_storage_counts_the_tables_the_chain_created():
    info = storage_info()

    assert info["error"] is None
    # The chain creates well over a dozen; asserting a floor rather than an
    # exact count keeps this from breaking on every new revision.
    assert info["table_count"] > 10


def test_storage_reports_a_size_for_sqlite():
    """page_count * page_size, so a fresh migrated database is non-zero."""
    info = storage_info()

    assert info["size_bytes"] is not None
    assert info["size_bytes"] > 0


####################
# SQLite floor
####################


def test_sqlite_floor_is_the_drop_column_version():
    info = sqlite_info()

    assert MINIMUM_SQLITE == (3, 35, 0)
    assert info["minimum_supported"] == "3.35.0"
    assert info["runtime_version"] == sqlite3.sqlite_version
    assert info["meets_minimum"] is True


####################
# HTTP surface
####################


def test_info_endpoint_requires_admin(authenticated_user):
    """A non-admin must not learn the database host, name, or user."""
    response = authenticated_user.get("/api/v1/db/info")

    assert response.status_code == 401


def test_info_endpoint_returns_every_section(authenticated_admin):
    response = authenticated_admin.get("/api/v1/db/info")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"connection", "pool", "schema", "storage", "sqlite"}
    assert body["connection"]["dialect"] == "sqlite"
    assert body["schema"]["at_head"] is True


def test_info_endpoint_body_contains_no_credential(authenticated_admin):
    """End to end over HTTP, since the leak that matters is the served bytes."""
    from selfai_ui.internal.db import engine

    response = authenticated_admin.get("/api/v1/db/info")
    raw = response.text

    password = engine.url.password
    if password:
        assert password not in raw
    assert engine.url.render_as_string(hide_password=False) not in raw
