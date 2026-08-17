"""What the Admin > Settings > Database page reports (self.ai#94).

The page has never said anything about the database. It held three
export/backup controls, all of which moved to the backup surface (#93) — one of
which, `GET /utils/db/download`, 400s on anything but SQLite and so had never
once succeeded against this deployment's Postgres.

This module answers the questions the page's name promises: what are we
connected to, is the connection pooled and how, is the schema at head, and how
big is it. Read-only by design — with `ENABLE_PERSISTENT_CONFIG=False` an
editable field with no manifest env line silently reverts on the next pod
restart, so every value here is surfaced, not settable.

**Nothing here returns a credential.** `DATABASE_URL` carries the password, so
the URL is decomposed into host/port/database/username and the password is
never read out of it. See self.ai#109 for the wider credential posture.

Each section is gathered independently and degrades to an `error` string rather
than failing the whole readout: a page that shows the pool but not the size is
more useful than a 500.
"""

import logging
import sqlite3
import time
from typing import Optional

from sqlalchemy import inspect, text

from selfai_ui.env import (
    DATABASE_POOL_MAX_OVERFLOW,
    DATABASE_POOL_RECYCLE,
    DATABASE_POOL_SIZE,
    DATABASE_POOL_TIMEOUT,
    SRC_LOG_LEVELS,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["DB"])


# SQLite gained ALTER TABLE ... DROP COLUMN in 3.35.0 (March 2021). The
# migration chain has 16 plain `op.drop_column` sites, so anything older cannot
# replay it. Asserted in tests/test_sqlite_migration_chain.py; surfaced here so
# an operator running the demo or CI image can see the floor is met rather than
# discovering it from a failed boot.
MINIMUM_SQLITE = (3, 35, 0)


def _version_tuple(text_version: str) -> tuple:
    return tuple(int(part) for part in text_version.split(".")[:3])


def connection_info() -> dict:
    """Where we are connected, how fast it answers, and what it is running.

    `reachable` cannot realistically report False through the HTTP surface —
    admin authentication needs a database read, so a fully-down database fails
    the request before this runs. It is still measured here because this module
    is also callable from a boot probe or a test, where that is not true.
    """
    from selfai_ui.internal.db import engine

    url = engine.url
    info = {
        "dialect": engine.dialect.name,
        "driver": engine.dialect.driver,
        # Deliberately field-by-field. Rendering the URL — even with
        # hide_password=True — puts the credential one flag change away from
        # the response body.
        "host": url.host,
        "port": url.port,
        "database": url.database,
        "username": url.username,
        "server_version": None,
        "reachable": False,
        "latency_ms": None,
        "error": None,
    }

    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar()
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            info["reachable"] = True
            info["server_version"] = _server_version(connection, engine.dialect.name)
    except Exception as e:
        log.exception(e)
        info["error"] = str(e)

    return info


def _server_version(connection, dialect: str) -> Optional[str]:
    """The engine's own version string, asked of the engine.

    `dialect.server_version_info` is a parsed tuple that loses the build detail
    an operator wants when a Postgres minor is the thing under suspicion, so
    each dialect is asked directly and the tuple is only the fallback.
    """
    try:
        if dialect == "postgresql":
            return connection.execute(text("SHOW server_version")).scalar()
        if dialect == "sqlite":
            return connection.execute(text("SELECT sqlite_version()")).scalar()
    except Exception as e:
        log.exception(e)

    parsed = connection.engine.dialect.server_version_info
    return ".".join(str(part) for part in parsed) if parsed else None


def pool_info() -> dict:
    """Configured pool settings alongside what the engine actually built.

    These diverge silently and the divergence matters. `internal/db.py:49`
    takes the SQLite branch on a SQLite URL and ignores every pool setting, and
    the Postgres branch only builds a `QueuePool` when `DATABASE_POOL_SIZE > 0`
    — which defaults to 0, meaning the default deployment runs `NullPool` and
    all four `DATABASE_POOL_*` env vars are inert. Reporting only the
    configured values would show an operator four numbers that do nothing.
    """
    from selfai_ui.internal.db import engine

    pool = engine.pool
    configured = {
        "DATABASE_POOL_SIZE": DATABASE_POOL_SIZE,
        "DATABASE_POOL_MAX_OVERFLOW": DATABASE_POOL_MAX_OVERFLOW,
        "DATABASE_POOL_TIMEOUT": DATABASE_POOL_TIMEOUT,
        "DATABASE_POOL_RECYCLE": DATABASE_POOL_RECYCLE,
    }

    # Mirror the branch in `internal/db.py` rather than inferring from the pool
    # class: SQLAlchemy defaults a file-backed SQLite engine to `QueuePool` on
    # its own, so "a QueuePool exists" does not mean these settings built it.
    settings_applied = "sqlite" not in str(engine.url) and DATABASE_POOL_SIZE > 0

    info = {
        "class": type(pool).__name__,
        "pooling": type(pool).__name__ != "NullPool",
        "configured": configured,
        "settings_applied": settings_applied,
        "size": None,
        "checked_out": None,
        "checked_in": None,
        "overflow": None,
        "error": None,
    }

    # NullPool implements none of these, and StaticPool only some.
    for field, method in (
        ("size", "size"),
        ("checked_out", "checkedout"),
        ("checked_in", "checkedin"),
        ("overflow", "overflow"),
    ):
        accessor = getattr(pool, method, None)
        if accessor is None:
            continue
        try:
            info[field] = accessor()
        except Exception as e:
            log.exception(e)
            info["error"] = str(e)

    return info


def schema_info() -> dict:
    """Alembic revision the database is at, against the head(s) this build has.

    Operationally the sharpest value on the page: boot migrations are strict
    (#82), so not-at-head is the difference between a serving pod and a
    CrashLoop, and until now there was no way to see it from the UI.

    More than one head means the chain has branched — the next roll CrashLoops
    (#102) — so it is reported as a list, not a scalar.
    """
    from selfai_ui.utils.backup import current_schema_revision, script_heads

    try:
        current = current_schema_revision()
        heads = sorted(script_heads())
    except Exception as e:
        log.exception(e)
        return {"current": None, "heads": [], "at_head": False, "branched": False, "error": str(e)}

    return {
        "current": current,
        "heads": heads,
        "at_head": current in heads,
        "branched": len(heads) > 1,
        "error": None,
    }


def storage_info() -> dict:
    """How much the database holds, in the units each engine can answer."""
    from selfai_ui.internal.db import engine

    info = {"table_count": None, "size_bytes": None, "error": None}

    try:
        info["table_count"] = len(inspect(engine).get_table_names())
    except Exception as e:
        log.exception(e)
        info["error"] = str(e)

    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                info["size_bytes"] = connection.execute(
                    text("SELECT pg_database_size(current_database())")
                ).scalar()
            elif engine.dialect.name == "sqlite":
                # File size via pragma rather than os.stat: it is correct for
                # a URL we never resolve to a path, and honest about pages the
                # file has allocated.
                page_count = connection.execute(text("PRAGMA page_count")).scalar()
                page_size = connection.execute(text("PRAGMA page_size")).scalar()
                if page_count is not None and page_size is not None:
                    info["size_bytes"] = page_count * page_size
    except Exception as e:
        log.exception(e)
        info["error"] = str(e)

    return info


def sqlite_info() -> dict:
    """The SQLite version floor, reported whatever the live dialect is.

    Surfaced even on Postgres because the number that matters is the one in
    *this image* — it is what decides whether the demo and CI images built from
    it can replay the chain (#94 Part 3).
    """
    runtime = sqlite3.sqlite_version
    return {
        "runtime_version": runtime,
        "minimum_supported": ".".join(str(part) for part in MINIMUM_SQLITE),
        "meets_minimum": _version_tuple(runtime) >= MINIMUM_SQLITE,
    }


def database_info() -> dict:
    return {
        "connection": connection_info(),
        "pool": pool_info(),
        "schema": schema_info(),
        "storage": storage_info(),
        "sqlite": sqlite_info(),
    }
