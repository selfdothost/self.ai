"""
Migration integrity tests.

Validates that Alembic's migrations produce a schema consistent with
the SQLAlchemy models. Catches schema drift between model definitions
and what migrations actually create — the exact issue flagged in
research-brief-ui-test-suite.md Section 3.

Peewee was dropped entirely (selfshipyard/selfai/self.ai#27): its 18
legacy migrations only ever carried forward schema history from
OpenWebUI's pre-Alembic era, which this fork never shipped. Alembic's
own root migration builds the full schema from scratch.
"""

import os

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture(scope="module")
def migrated_engine():
    """An engine pointing at the main test DB (which has all migrations applied)."""
    return create_engine(os.environ["DATABASE_URL"])


@pytest.mark.tier0
def test_all_declared_tables_exist(migrated_engine):
    """Every SQLAlchemy model table has a corresponding table in the DB."""
    from selfai_ui.internal.db import Base

    inspector = inspect(migrated_engine)
    existing = set(inspector.get_table_names())

    missing = []
    for table_name in Base.metadata.tables.keys():
        if table_name not in existing:
            missing.append(table_name)

    assert not missing, (
        f"SQLAlchemy-declared tables missing from the DB: {missing}. "
        f"Likely cause: the model was declared but no migration creates "
        f"the table."
    )


@pytest.mark.tier0
def test_curator_job_has_dataset_columns(migrated_engine):
    """
    curator_job.dataset_name and created_knowledge_id used to be bolted
    on by a later Peewee ALTER, which broke on a fresh DB (Postgres-only
    `ADD COLUMN IF NOT EXISTS` syntax). Alembic migration b3c4d5e6f7a8
    now declares them directly on the table at creation time instead.
    """
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("curator_job")}

    assert "dataset_name" in columns, (
        "curator_job.dataset_name missing — check Alembic migration " "b3c4d5e6f7a8_create_curator_job_table.py."
    )
    assert "created_knowledge_id" in columns, "curator_job.created_knowledge_id missing. Same root cause."


@pytest.mark.tier0
def test_training_job_has_scheduled_for(migrated_engine):
    """training_job.scheduled_for was added by Alembic migration d4e5f6a7b8c9."""
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("training_job")}
    assert "scheduled_for" in columns


@pytest.mark.tier0
def test_training_job_has_priority(migrated_engine):
    """training_job.priority was added by Alembic migration a2b3c4d5e6f7."""
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("training_job")}
    assert "priority" in columns


@pytest.mark.tier0
def test_eval_job_has_eval_type(migrated_engine):
    """eval_job.eval_type was added by Alembic migration c3d4e5f6a7b8."""
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("eval_job")}
    assert "eval_type" in columns


@pytest.mark.tier0
def test_benchmark_config_seed_present(migrated_engine, seeded_benchmarks):
    """Migration d5e6f7a8b9c0 seeds benchmark_config with known benchmarks."""
    with migrated_engine.connect() as conn:
        rows = conn.execute(text("SELECT benchmark, eval_type FROM benchmark_config")).fetchall()
    benchmarks = {(r[0], r[1]) for r in rows}

    # These are required for the UI benchmark selector
    required = {
        ("humaneval", "code-eval"),
        ("mbpp", "code-eval"),
        ("mmlu", "language-eval"),
        ("gsm8k", "language-eval"),
    }
    missing = required - benchmarks
    assert not missing, f"Seeded benchmarks missing: {missing}. " f"The benchmark_config seeding is broken."


@pytest.mark.tier0
def test_job_window_and_slot_tables_related(migrated_engine):
    """job_window_slot has a foreign key to job_window."""
    inspector = inspect(migrated_engine)
    # NOTE: this only checks the column exists, not that a real FK constraint
    # does -- the docstring claims more than this test verifies. Computing
    # the FK list and then not asserting on it (dead code, removed here) hints
    # this was deliberately relaxed, maybe because SQLite's FK reflection is
    # unreliable, but that reasoning isn't recorded anywhere. Worth a human
    # decision on whether to assert the real constraint instead.
    columns = {col["name"] for col in inspector.get_columns("job_window_slot")}
    assert "window_id" in columns, "job_window_slot.window_id missing — foreign relationship broken."


@pytest.mark.tier0
def test_alembic_version_is_head(migrated_engine):
    """alembic_version table exists and has a single current version."""
    inspector = inspect(migrated_engine)
    assert (
        "alembic_version" in inspector.get_table_names()
    ), "alembic_version table missing — Alembic migrations never ran"
    with migrated_engine.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert len(rows) == 1, f"Expected exactly 1 alembic version row, got {len(rows)}"


@pytest.mark.tier0
def test_known_tables_accounted_for(migrated_engine):
    """
    Every table in the DB should be a known table (SQLAlchemy model,
    legacy Peewee-managed, or Alembic system). Flags unexpected tables
    that suggest a stale migration left garbage behind.
    """
    # Import all models so Base.metadata is populated
    import selfai_ui.models.auths  # noqa: F401
    import selfai_ui.models.benchmark_config  # noqa: F401
    import selfai_ui.models.channels  # noqa: F401
    import selfai_ui.models.chats  # noqa: F401
    import selfai_ui.models.curator_jobs  # noqa: F401
    import selfai_ui.models.eval_jobs  # noqa: F401
    import selfai_ui.models.feedbacks  # noqa: F401
    import selfai_ui.models.files  # noqa: F401
    import selfai_ui.models.folders  # noqa: F401
    import selfai_ui.models.functions  # noqa: F401
    import selfai_ui.models.groups  # noqa: F401
    import selfai_ui.models.job_windows  # noqa: F401
    import selfai_ui.models.knowledge  # noqa: F401
    import selfai_ui.models.memories  # noqa: F401
    import selfai_ui.models.messages  # noqa: F401
    import selfai_ui.models.models  # noqa: F401
    import selfai_ui.models.prompts  # noqa: F401
    import selfai_ui.models.tags  # noqa: F401
    import selfai_ui.models.tools  # noqa: F401
    import selfai_ui.models.training  # noqa: F401
    import selfai_ui.models.users  # noqa: F401
    from selfai_ui.internal.db import Base
    from selfai_ui.mods.naming import table_prefix_for

    inspector = inspect(migrated_engine)
    existing = set(inspector.get_table_names())
    declared = set(Base.metadata.tables.keys())

    # Known Alembic-system / legacy tables not registered via the imports above
    known_extras = {
        "alembic_version",
        "config",  # SQLAlchemy model, but not imported by this test module
        "chatidtag",  # Legacy — pre-tag-table-rename
        "document",  # Legacy — pre-knowledge-migration
        "channel_member",  # Join table (no SQLAlchemy model)
        # Mod-owned tables: created by a real, enablement-gated Alembic
        # migration but accessed via raw SQL through the mods facade, not a
        # SQLAlchemy model, so they never populate Base.metadata. The
        # reference mod's boot fixtures (tests/mods_reference_boot.py) create
        # this table in the shared test DB, and it persists for the rest of
        # the session — a genuine table, not stale migration garbage.
        f"{table_prefix_for('reference')}handles",
    }
    transient_prefixes = ("_alembic_tmp_", "sqlite_")

    unexpected = []
    for t in existing - declared - known_extras:
        if not any(t.startswith(p) for p in transient_prefixes):
            unexpected.append(t)

    assert not unexpected, (
        f"Tables in DB not accounted for by model or known-extras list: "
        f"{unexpected}. Either add a model, add to known_extras, or "
        f"remove via migration."
    )
